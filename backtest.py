"""
backtest.py — Walk-forward backtester for RaanuTradingBot
==========================================================
Answers the question the live trade log cannot: does an entry strategy have a
positive edge, and what exit rule extracts it?

Design — two phases, because entry signals do not depend on exit rules:

  Phase 1  build_signals()  — walk the calendar, score every ticker with the
           SAME functions the live bot uses (strategy.score_from_df /
           strategy2.score_from_df_s2) on a slice of history ending that day.
           Expensive; cached to disk.

  Phase 2  simulate()       — replay the cached signals through a portfolio
           with a given exit config. Cheap, so exit parameters can be swept.

No lookahead: a signal computed from bars up to and including day i is entered
at day i+1's OPEN. Indicators only ever read trailing data.

Fill assumptions (deliberately pessimistic where ambiguous):
  * entries fill at the next session's open
  * a stop is checked against the day's LOW and fills AT the stop price
  * the trailing peak is updated from the day's HIGH
  * if a stop and a trail exit would both trigger on the same bar, the stop
    wins — the adverse move is assumed to come first

Usage:
    python3 backtest.py --strategy s1 --years 3
    python3 backtest.py --strategy s1 --sweep-atr
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import statistics as st
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from strategy import batch_download, score_from_df, ema
from strategy2 import score_from_df_s2
from strategy3 import score_from_df_s3

log = logging.getLogger("raanu.backtest")

HERE = Path(__file__).parent
CACHE_DIR = HERE / "backtest_reports"
CACHE_DIR.mkdir(exist_ok=True)

WARMUP_BARS = 210  # need EMA200 + a little room before the first signal


# ─────────────────────────── configuration ───────────────────────────────────
@dataclass
class ExitConfig:
    """
    Exit rules. `stop_mode` is the whole point of this module:

      "pct"  — legacy fixed percentage (what the live bot does today)
      "atr"  — stop placed stop_atr_mult x ATR(14) below entry, so the distance
               scales with how much the stock actually moves in a day
    """
    stop_mode: str = "atr"           # "pct" | "atr"
    stop_pct: float = 3.0
    stop_atr_mult: float = 2.0

    trail_mode: str = "atr"          # "pct" | "atr" | "off"
    trail_activate_pct: float = 5.0  # arm the trail once up this much
    trail_activate_atr: float = 2.0  # ...or this many ATR, when trail_mode="atr"
    trail_pct: float = 2.5
    trail_atr_mult: float = 1.5

    trail_min_pct: float = 3.0          # floor on the give-back distance
    trail_activate_min_pct: float = 2.5 # floor on the arm threshold

    hard_take_profit_pct: float = 0.0   # 0 = disabled
    daily_crash_pct: float = 8.0        # 0 = disabled
    max_hold_days: int = 0              # 0 = no time stop

    # Profit ladder: once PEAK gain reaches X%, never give back below Y%.
    # [(5,2),(10,6),...]. Empty list disables it.
    profit_ladder: list = field(default_factory=list)

    def label(self) -> str:
        s = f"{self.stop_atr_mult:.1f}xATR" if self.stop_mode == "atr" else f"{self.stop_pct:.1f}%"
        if self.trail_mode == "off":
            t = "no trail"
        elif self.trail_mode == "atr":
            t = f"trail {self.trail_atr_mult:.1f}xATR @+{self.trail_activate_atr:.1f}ATR"
        else:
            t = f"trail {self.trail_pct:.1f}% @+{self.trail_activate_pct:.1f}%"
        return f"stop {s} | {t}"


@dataclass
class PortfolioConfig:
    initial_capital: float = 100_000.0
    max_positions: int = 8
    per_trade_usd: float = 0.0      # 0 = use per_trade_pct of equity
    per_trade_pct: float = 5.0
    min_score: int = 60
    max_new_per_day: int = 2
    scan_every: int = 1             # trading days between scans

    # Position sizing.
    #   "equity_pct" — every trade gets the same dollar amount (what the bot
    #                  does today). With ATR stops this is incoherent: a 2.5xATR
    #                  stop on a 16%-ATR miner is a 41% stop, so an equal-dollar
    #                  position risks 8x what a quiet name risks.
    #   "risk_pct"   — size so the loss AT THE STOP equals risk_pct of equity.
    #                  Volatile names automatically get small positions. This is
    #                  what makes a high-beta universe survivable.
    sizing_mode: str = "risk_pct"
    risk_pct: float = 1.0           # % of equity lost if the stop is hit
    max_position_pct: float = 20.0  # cap so a tight stop cannot eat the account
    max_atr_pct: float = 0.0        # 0 = no filter; else skip names above this daily ATR%


@dataclass
class Trade:
    ticker: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    qty: float
    pnl: float
    pct: float
    exit_reason: str
    score: int
    atr_at_entry: float
    hold_days: int
    mfe_pct: float
    mae_pct: float


# ─────────────────────────── indicators ──────────────────────────────────────
def atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's Average True Range, in price units."""
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


# ─────────────────────── phase 1: signal generation ──────────────────────────
def build_signals(strategy: str, universe: list[str], years: int,
                  min_score: int, scan_every: int = 1,
                  use_cache: bool = True) -> dict:
    """
    Walk the calendar and record, for each scan date, which tickers were
    actionable. Returns {"dates": [...], "signals": {date: [{ticker, score}]}}.
    """
    cache = CACHE_DIR / f"signals_{strategy}_{years}y_{min_score}_{scan_every}_{len(universe)}.json"
    if use_cache and cache.exists():
        log.info(f"Using cached signals: {cache.name}")
        return json.loads(cache.read_text())

    period = f"{years + 1}y"  # extra year so the warmup does not eat the window
    log.info(f"Downloading {len(universe)} tickers, {period}...")
    data = batch_download(universe + ["SPY"], period=period)
    spy = data.pop("SPY", None)
    if spy is None or spy.empty:
        raise RuntimeError("Could not download SPY benchmark")

    # Point-in-time SPY 3-month return, used for relative-strength scoring.
    spy_close = spy["Close"].dropna()
    spy_roc = (spy_close / spy_close.shift(63) - 1.0)

    data = {t: d for t, d in data.items() if d is not None and len(d) > WARMUP_BARS}
    log.info(f"{len(data)} tickers with usable history")

    # Master calendar = SPY's trading days, trimmed to the requested window.
    calendar = list(spy_close.index)
    start_i = max(WARMUP_BARS, len(calendar) - years * 252)
    scan_dates = calendar[start_i::scan_every]

    scorer = {"s2": score_from_df_s2, "s3": score_from_df_s3}.get(strategy, score_from_df)
    gate   = {"s2": "stage2", "s3": "leader_dip"}.get(strategy, "uptrend")


    signals: dict[str, list] = {}
    for n, date in enumerate(scan_dates):
        bench = spy_roc.get(date)
        bench = float(bench) if bench is not None and not pd.isna(bench) else None

        hits = []
        for ticker, df in data.items():
            window = df[df.index <= date]
            if len(window) < WARMUP_BARS:
                continue
            try:
                r = scorer(ticker, window, bench)
            except Exception:
                continue
            if r.get("ok") and r.get(gate) and r.get("score", 0) >= min_score:
                hits.append({"ticker": ticker, "score": r["score"]})

        hits.sort(key=lambda x: x["score"], reverse=True)
        signals[date.strftime("%Y-%m-%d")] = hits[:20]

        if n % 25 == 0:
            log.info(f"  {date.date()}  ({n}/{len(scan_dates)})  {len(hits)} actionable")

    out = {
        "strategy": strategy,
        "years": years,
        "min_score": min_score,
        "scan_every": scan_every,
        "dates": [d.strftime("%Y-%m-%d") for d in calendar[start_i:]],
        "signals": signals,
    }
    cache.write_text(json.dumps(out))
    log.info(f"Cached signals to {cache.name}")
    return out


# ─────────────────────── phase 2: portfolio simulation ───────────────────────
def _price_frames(universe: list[str], years: int) -> dict[str, pd.DataFrame]:
    data = batch_download(universe, period=f"{years + 1}y")
    out = {}
    for t, df in data.items():
        if df is None or df.empty or "Open" not in df.columns:
            continue
        d = df.copy()
        d["ATR"] = atr_series(d)
        out[t] = d
    return out


def simulate(sig: dict, prices: dict[str, pd.DataFrame],
             exits: ExitConfig, pf: PortfolioConfig) -> dict:
    """Replay cached signals through a portfolio under one exit configuration."""
    dates = [pd.Timestamp(d) for d in sig["dates"]]
    signals = sig["signals"]

    cash = pf.initial_capital
    open_pos: dict[str, dict] = {}
    trades: list[Trade] = []
    equity_curve: list[tuple[str, float]] = []

    for date in dates:
        dkey = date.strftime("%Y-%m-%d")

        # ── 1. manage open positions on today's bar ──────────────────────────
        for ticker in list(open_pos.keys()):
            pos = open_pos[ticker]
            df = prices.get(ticker)
            if df is None or date not in df.index:
                continue
            bar = df.loc[date]
            high, low, close = float(bar["High"]), float(bar["Low"]), float(bar["Close"])
            entry, atr = pos["entry"], pos["atr"]

            pos["peak"] = max(pos["peak"], high)
            pos["trough"] = min(pos["trough"], low)
            pos["days"] += 1

            # Stop distance: fixed percent, or a multiple of entry-day ATR.
            if exits.stop_mode == "atr":
                stop_price = entry - exits.stop_atr_mult * atr
            else:
                stop_price = entry * (1 - exits.stop_pct / 100)

            exit_price = exit_reason = None

            if low <= stop_price:
                exit_price, exit_reason = stop_price, "stop"

            if exit_price is None and exits.hard_take_profit_pct > 0:
                tp = entry * (1 + exits.hard_take_profit_pct / 100)
                if high >= tp:
                    exit_price, exit_reason = tp, "take_profit"

            if exit_price is None and exits.trail_mode != "off":
                if exits.trail_mode == "atr":
                    arm_dist  = max(exits.trail_activate_atr * atr,
                                    entry * exits.trail_activate_min_pct / 100)
                    give_dist = max(exits.trail_atr_mult * atr,
                                    entry * exits.trail_min_pct / 100)
                    armed = pos["peak"] >= entry + arm_dist
                    trail_stop = pos["peak"] - give_dist
                else:
                    armed = pos["peak"] >= entry * (1 + exits.trail_activate_pct / 100)
                    trail_stop = pos["peak"] * (1 - exits.trail_pct / 100)
                if armed and low <= trail_stop:
                    exit_price, exit_reason = trail_stop, "trail"

            # Profit ladder — ratchets a floor up as the peak gain grows.
            if exit_price is None and exits.profit_ladder:
                peak_pct = (pos["peak"] - entry) / entry * 100
                floor = None
                for threshold, lock in sorted(exits.profit_ladder):
                    if peak_pct >= threshold:
                        floor = lock
                if floor is not None:
                    floor_price = entry * (1 + floor / 100)
                    if low <= floor_price:
                        exit_price, exit_reason = floor_price, "ladder"

            if exit_price is None and exits.daily_crash_pct > 0 and pos["prev_close"]:
                drop = (pos["prev_close"] - low) / pos["prev_close"] * 100
                if drop >= exits.daily_crash_pct:
                    exit_price = pos["prev_close"] * (1 - exits.daily_crash_pct / 100)
                    exit_reason = "daily_crash"

            if exit_price is None and exits.max_hold_days > 0 and pos["days"] >= exits.max_hold_days:
                exit_price, exit_reason = close, "time_stop"

            pos["prev_close"] = close

            if exit_price is not None:
                qty = pos["qty"]
                pnl = (exit_price - entry) * qty
                cash += exit_price * qty
                trades.append(Trade(
                    ticker=ticker,
                    entry_date=pos["entry_date"], entry_price=round(entry, 4),
                    exit_date=dkey, exit_price=round(exit_price, 4),
                    qty=round(qty, 4), pnl=round(pnl, 2),
                    pct=round((exit_price - entry) / entry * 100, 2),
                    exit_reason=exit_reason, score=pos["score"],
                    atr_at_entry=round(atr / entry * 100, 2),
                    hold_days=pos["days"],
                    mfe_pct=round((pos["peak"] - entry) / entry * 100, 2),
                    mae_pct=round((pos["trough"] - entry) / entry * 100, 2),
                ))
                del open_pos[ticker]

        # ── 2. mark to market ────────────────────────────────────────────────
        equity = cash
        for ticker, pos in open_pos.items():
            df = prices.get(ticker)
            if df is not None and date in df.index:
                pos["last"] = float(df.loc[date, "Close"])
            equity += pos["last"] * pos["qty"]
        equity_curve.append((dkey, round(equity, 2)))

        # ── 3. enter tomorrow's positions from today's signals ───────────────
        todays = signals.get(dkey)
        if not todays:
            continue
        opened = 0
        for cand in todays:
            if len(open_pos) >= pf.max_positions or opened >= pf.max_new_per_day:
                break
            ticker = cand["ticker"]
            if ticker in open_pos:
                continue
            df = prices.get(ticker)
            if df is None:
                continue
            # The signal calendar is SPY's; an individual name can be missing a
            # bar that day (halt, late listing, data gap) — use its last bar
            # on or before the signal date for ATR.
            prior = df.index[df.index <= date]
            future = df.index[df.index > date]
            if len(future) == 0 or len(prior) == 0:
                continue
            nxt = future[0]
            fill = float(df.loc[nxt, "Open"])
            atr = float(df.loc[prior[-1], "ATR"])
            if not (fill > 0) or not (atr > 0) or math.isnan(atr):
                continue

            atr_pct = atr / fill * 100
            if pf.max_atr_pct > 0 and atr_pct > pf.max_atr_pct:
                continue

            if pf.sizing_mode == "risk_pct":
                # Distance to the stop, in price units, for THIS name.
                if exits.stop_mode == "atr":
                    stop_dist = exits.stop_atr_mult * atr
                else:
                    stop_dist = fill * exits.stop_pct / 100
                if stop_dist <= 0:
                    continue
                risk_budget = equity * pf.risk_pct / 100
                size = (risk_budget / stop_dist) * fill          # shares -> dollars
                size = min(size, equity * pf.max_position_pct / 100)
            else:
                size = pf.per_trade_usd or equity * pf.per_trade_pct / 100

            size = min(size, cash)
            if size < 100:
                continue
            qty = size / fill
            cash -= size
            open_pos[ticker] = {
                "entry": fill, "qty": qty, "atr": atr,
                "entry_date": nxt.strftime("%Y-%m-%d"),
                "peak": fill, "trough": fill, "last": fill,
                "days": 0, "prev_close": None, "score": cand["score"],
            }
            opened += 1

    # Liquidate whatever is still open at the final close.
    if open_pos and dates:
        last = dates[-1]
        for ticker, pos in list(open_pos.items()):
            df = prices.get(ticker)
            px = float(df.loc[last, "Close"]) if df is not None and last in df.index else pos["last"]
            cash += px * pos["qty"]
            trades.append(Trade(
                ticker=ticker, entry_date=pos["entry_date"], entry_price=round(pos["entry"], 4),
                exit_date=last.strftime("%Y-%m-%d"), exit_price=round(px, 4),
                qty=round(pos["qty"], 4), pnl=round((px - pos["entry"]) * pos["qty"], 2),
                pct=round((px - pos["entry"]) / pos["entry"] * 100, 2),
                exit_reason="open_at_end", score=pos["score"],
                atr_at_entry=round(pos["atr"] / pos["entry"] * 100, 2),
                hold_days=pos["days"],
                mfe_pct=round((pos["peak"] - pos["entry"]) / pos["entry"] * 100, 2),
                mae_pct=round((pos["trough"] - pos["entry"]) / pos["entry"] * 100, 2),
            ))

    return {"trades": trades, "equity_curve": equity_curve, "final_equity": round(cash, 2)}


# ─────────────────────────────── statistics ──────────────────────────────────
def kelly_fraction(win_rate: float, payoff: float) -> float:
    """f* = (b*p - q) / b. Negative means no edge — do not size up, stand aside."""
    if payoff <= 0:
        return 0.0
    return (payoff * win_rate - (1 - win_rate)) / payoff


TRADING_DAYS = 252


def filter_signals(sig: dict, min_score: int, top_n: int) -> dict:
    """Re-cut cached signals to a higher score bar and a shorter per-day list.

    Signals are cached at the LOWEST threshold and narrowed here, so a sweep
    across thresholds costs one scan instead of one per setting. Every cut is
    the same underlying scoring run, which is what makes the comparison fair —
    rebuilding per threshold would also resample the noise.
    """
    out = {"dates": sig["dates"], "signals": {}}
    for d, hits in sig["signals"].items():
        keep = [h for h in hits if h.get("score", 0) >= min_score][:top_n]
        out["signals"][d] = keep
    return out


def _benchmark_frame(years: int):
    """SPY OHLCV for the window, for the risk maths. Cached by yfinance."""
    try:
        from strategy import batch_download
        return batch_download(["SPY"], period=f"{years + 1}y").get("SPY")
    except Exception as e:
        print(f"  (benchmark unavailable for risk stats: {e})")
        return None


def risk_stats(equity_curve: list, bench: "pd.DataFrame",
               trades: list = None, risk_free_pct: float = 4.0) -> dict:
    """Risk-ADJUSTED performance: alpha, beta, Sharpe, Sortino, exposure.

    Total return alone cannot tell skill from leverage. A strategy that returns
    less than the index while carrying half its risk is not losing — it is
    under-deployed, and the fix is size, not a new strategy. That distinction is
    invisible in every number this backtester printed before now, which is why
    the file has a comment at simulate() saying "the improvement is just beta"
    and no code anywhere that measures beta.

    - beta   : sensitivity to the index. 1.0 moves with it, 0.5 half as much.
    - alpha  : annualised return NOT explained by that exposure (Jensen's).
               This is the only number here that claims skill.
    - Sharpe : excess return per unit of total volatility.
    - Sortino: same, but punishing downside only — upside volatility is not a
               risk, and Sharpe treats it as one.
    - exposure: share of days holding anything. A strategy in cash 60% of the
               time is not comparable to buy-and-hold until this is known.

    risk_free_pct is annual and defaults to 4%, roughly US T-bills across the
    2023–2026 window. Passing 0 (the common shortcut) inflates every Sharpe.
    """
    import numpy as np

    if not equity_curve or len(equity_curve) < 30:
        return {"error": "not enough equity history"}

    dates = [pd.Timestamp(d) for d, _ in equity_curve]
    eq = np.array([v for _, v in equity_curve], dtype=float)

    # Align the benchmark to the strategy's own calendar — comparing series with
    # different trading days silently corrupts beta.
    bc = bench["Close"].reindex(pd.DatetimeIndex(dates)).ffill()
    bp = bc.to_numpy(dtype=float)
    ok = ~np.isnan(bp) & (eq > 0)
    eq, bp = eq[ok], bp[ok]
    if len(eq) < 30:
        return {"error": "benchmark did not align"}

    r_s = np.diff(eq) / eq[:-1]
    r_b = np.diff(bp) / bp[:-1]
    n = len(r_s)
    years = n / TRADING_DAYS
    rf_daily = (1 + risk_free_pct / 100) ** (1 / TRADING_DAYS) - 1

    cagr   = ((eq[-1] / eq[0]) ** (1 / years) - 1) * 100 if years > 0 else 0.0
    b_cagr = ((bp[-1] / bp[0]) ** (1 / years) - 1) * 100 if years > 0 else 0.0
    vol    = float(np.std(r_s, ddof=1)) * np.sqrt(TRADING_DAYS) * 100
    b_vol  = float(np.std(r_b, ddof=1)) * np.sqrt(TRADING_DAYS) * 100

    var_b = float(np.var(r_b, ddof=1))
    beta  = float(np.cov(r_s, r_b, ddof=1)[0][1] / var_b) if var_b > 0 else 0.0
    # Jensen's alpha, annualised: the daily return left over once the index
    # exposure implied by beta is subtracted.
    alpha_d = float(np.mean(r_s - rf_daily) - beta * np.mean(r_b - rf_daily))
    alpha   = ((1 + alpha_d) ** TRADING_DAYS - 1) * 100

    ex = r_s - rf_daily
    sharpe = float(np.mean(ex) / np.std(ex, ddof=1) * np.sqrt(TRADING_DAYS)) \
             if np.std(ex, ddof=1) > 0 else 0.0
    # The BENCHMARK's Sharpe, on the same window and risk-free rate. A strategy
    # Sharpe on its own is unreadable: 0.72 sounds respectable until the index
    # scored 1.13 over the identical period. Positive alpha and a lower Sharpe
    # can coexist, and that combination means the index is still the better
    # holding — which is exactly the conclusion a lone Sharpe hides.
    exb = r_b - rf_daily
    b_sharpe = float(np.mean(exb) / np.std(exb, ddof=1) * np.sqrt(TRADING_DAYS)) \
               if np.std(exb, ddof=1) > 0 else 0.0
    down = ex[ex < 0]
    sortino = float(np.mean(ex) / np.std(down, ddof=1) * np.sqrt(TRADING_DAYS)) \
              if len(down) > 1 and np.std(down, ddof=1) > 0 else 0.0
    diff = r_s - r_b
    info = float(np.mean(diff) / np.std(diff, ddof=1) * np.sqrt(TRADING_DAYS)) \
           if np.std(diff, ddof=1) > 0 else 0.0
    corr = float(np.corrcoef(r_s, r_b)[0][1]) if np.std(r_s, ddof=1) > 0 else 0.0

    # Exposure: share of trading days holding at least one position. Without it
    # a low return looks like weakness when it may be idleness.
    exposure = None
    if trades:
        held = set()
        idx = {d: i for i, d in enumerate(dates)}
        for t in trades:
            a, b = pd.Timestamp(t.entry_date), pd.Timestamp(t.exit_date)
            for d, i in idx.items():
                if a <= d <= b:
                    held.add(i)
        exposure = len(held) / len(dates) * 100

    return {
        "years": round(years, 2),
        "cagr": round(cagr, 2), "bench_cagr": round(b_cagr, 2),
        "vol": round(vol, 2), "bench_vol": round(b_vol, 2),
        "beta": round(beta, 3), "alpha": round(alpha, 2),
        "sharpe": round(sharpe, 2), "bench_sharpe": round(b_sharpe, 2),
        "sortino": round(sortino, 2),
        "info_ratio": round(info, 2), "corr": round(corr, 3),
        "exposure": round(exposure, 1) if exposure is not None else None,
        "risk_free_pct": risk_free_pct,
    }


def format_risk(r: dict, label: str = "") -> str:
    """One block, phrased so the conclusion is not left to the reader."""
    if r.get("error"):
        return f"  risk stats unavailable — {r['error']}"
    L = [f"\nRISK-ADJUSTED{(' — ' + label) if label else ''}"
         f"   ({r['years']}y, rf {r['risk_free_pct']}%)",
         f"  CAGR        {r['cagr']:+7.2f}%   vs SPY {r['bench_cagr']:+.2f}%",
         f"  Volatility  {r['vol']:7.2f}%   vs SPY {r['bench_vol']:.2f}%",
         f"  Beta        {r['beta']:7.3f}    correlation {r['corr']:.3f}",
         f"  ALPHA       {r['alpha']:+7.2f}%  <- return not explained by index exposure",
         f"  Sharpe      {r['sharpe']:7.2f}    vs SPY {r['bench_sharpe']:.2f}"
         f"     Sortino {r['sortino']:.2f}",
         f"  Info ratio  {r['info_ratio']:7.2f}    (consistency of out/under-performance)"]
    if r.get("exposure") is not None:
        L.append(f"  Exposure    {r['exposure']:7.1f}%   of days holding anything")
    a, be, sh, bsh = r["alpha"], r["beta"], r["sharpe"], r["bench_sharpe"]
    if a <= 0:
        L.append(f"  → Alpha {a:+.2f}%. Not beating what its beta of {be:.2f} alone "
                 f"would have\n    produced. Index exposure, not skill.")
    elif sh < bsh:
        L.append(f"  → Alpha is positive ({a:+.2f}%) but Sharpe {sh:.2f} is BELOW the "
                 f"index's {bsh:.2f}.\n    Both are true: it earns a little more than "
                 f"its beta implies, and still\n    pays more risk per unit of return "
                 f"than simply holding SPY. Buy & hold wins.")
    else:
        L.append(f"  → Alpha {a:+.2f}% AND Sharpe {sh:.2f} above the index's {bsh:.2f}. "
                 f"This is the\n    first configuration that is genuinely better "
                 f"risk-adjusted, not just different.")
    return "\n".join(L)


def stats(result: dict, pf: PortfolioConfig) -> dict:
    trades: list[Trade] = result["trades"]
    if not trades:
        return {"trades": 0}

    pnls = [t.pnl for t in trades]
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    avg_win = st.mean([t.pnl for t in wins]) if wins else 0.0
    avg_loss = abs(st.mean([t.pnl for t in losses])) if losses else 0.0
    p = len(wins) / len(trades)
    b = (avg_win / avg_loss) if avg_loss else 0.0
    f = kelly_fraction(p, b)

    curve = [e for _, e in result["equity_curve"]]
    peak, max_dd = curve[0] if curve else 0, 0.0
    for e in curve:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak * 100)

    total_ret = (result["final_equity"] - pf.initial_capital) / pf.initial_capital * 100
    years = max(len(curve) / 252, 0.01)

    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1

    return {
        "trades": len(trades),
        "win_rate": round(p * 100, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "payoff_b": round(b, 2),
        "expectancy": round(st.mean(pnls), 2),
        "net_pnl": round(sum(pnls), 2),
        "total_return_pct": round(total_ret, 2),
        "cagr_pct": round(((result["final_equity"] / pf.initial_capital) ** (1 / years) - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "kelly_full": round(f * 100, 1),
        "kelly_quarter": round(f * 25, 1),
        "median_hold_days": round(st.median([t.hold_days for t in trades]), 1),
        "exit_reasons": reasons,
    }


def benchmark_buy_hold(years: int) -> dict:
    """
    SPY buy-and-hold over the same window.

    Essential control: a wider stop keeps you in the market longer, so in a
    rising market it will look better whether or not the strategy has any edge.
    If the strategy cannot beat this, the "improvement" is just beta.
    """
    data = batch_download(["SPY"], period=f"{years + 1}y")
    spy = data.get("SPY")
    if spy is None or spy.empty:
        return {}
    close = spy["Close"].dropna()
    close = close.iloc[max(WARMUP_BARS, len(close) - years * 252):]
    total = (float(close.iloc[-1]) / float(close.iloc[0]) - 1) * 100
    peak, max_dd = float(close.iloc[0]), 0.0
    for v in close:
        peak = max(peak, float(v))
        max_dd = max(max_dd, (peak - float(v)) / peak * 100)
    yrs = max(len(close) / 252, 0.01)
    return {
        "total_return_pct": round(total, 2),
        "cagr_pct": round(((1 + total / 100) ** (1 / yrs) - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd, 2),
    }


def split_stats(result: dict, pf: PortfolioConfig) -> tuple[dict, dict]:
    """
    Re-score the run over its first and second half separately.

    A stop multiple chosen on one 3-year window is an in-sample pick. If it
    only works in one half, it is a fit to that period, not an edge.
    """
    trades: list[Trade] = result["trades"]
    curve = result["equity_curve"]
    if not trades or not curve:
        return {}, {}
    mid = curve[len(curve) // 2][0]

    def half(sel, cv):
        sub = {"trades": sel, "equity_curve": cv,
               "final_equity": cv[-1][1] if cv else pf.initial_capital}
        p = PortfolioConfig(**{**asdict(pf), "initial_capital": cv[0][1] if cv else pf.initial_capital})
        return stats(sub, p)

    first = half([t for t in trades if t.exit_date <= mid],
                 [c for c in curve if c[0] <= mid])
    second = half([t for t in trades if t.exit_date > mid],
                  [c for c in curve if c[0] > mid])
    return first, second


def print_stats(label: str, s: dict) -> None:
    if not s.get("trades"):
        print(f"  {label:34}  no trades")
        return
    print(
        f"  {label:34} n={s['trades']:4}  win {s['win_rate']:5.1f}%  "
        f"b={s['payoff_b']:5.2f}  exp ${s['expectancy']:+7.2f}  "
        f"ret {s['total_return_pct']:+7.2f}%  maxDD {s['max_drawdown_pct']:5.2f}%  "
        f"f*={s['kelly_full']:+6.1f}%  hold {s['median_hold_days']:.0f}d"
    )


# ──────────────────────────────── CLI ────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description="RaanuTradingBot backtester")
    ap.add_argument("--strategy", default="s1", choices=["s1", "s2", "s3"])
    ap.add_argument("--years", type=int, default=3)
    ap.add_argument("--min-score", type=int, default=60)
    ap.add_argument("--scan-every", type=int, default=1)
    ap.add_argument("--universe-limit", type=int, default=0, help="0 = full curated universe")
    ap.add_argument("--max-positions", type=int, default=8)
    ap.add_argument("--per-trade-pct", type=float, default=5.0)
    ap.add_argument("--sizing", default="risk_pct", choices=["risk_pct", "equity_pct"])
    ap.add_argument("--risk-pct", type=float, default=1.0)
    ap.add_argument("--max-atr-pct", type=float, default=0.0)
    ap.add_argument("--stop-atr", type=float, default=None,
                    help="ATR multiple for the stop (default: ExitConfig default). "
                         "The documented S3 result uses 3.0 — without this flag the "
                         "default run tested a config nobody chose.")
    ap.add_argument("--sweep-atr", action="store_true", help="compare stop rules")
    ap.add_argument("--sweep-rank", action="store_true",
                    help="does the SCORE carry information? sweeps the score bar "
                         "against portfolio concentration, judged on alpha and "
                         "Sharpe rather than raw return")
    ap.add_argument("--sweep-risk", action="store_true", help="compare sizing models")
    ap.add_argument("--sweep-ladder", action="store_true",
                    help="compare profit-ladder variants")
    ap.add_argument("--robustness", action="store_true",
                    help="split each config into first/second half to test stability")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--risk-free", type=float, default=4.0,
                    help="annual risk-free %% for Sharpe/alpha (default 4, ~T-bills "
                         "over this window; 0 inflates every Sharpe)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    from scanner import FALLBACK_UNIVERSE
    universe = FALLBACK_UNIVERSE[:args.universe_limit] if args.universe_limit else list(FALLBACK_UNIVERSE)

    pf = PortfolioConfig(
        max_positions=args.max_positions,
        per_trade_pct=args.per_trade_pct,
        min_score=args.min_score,
        scan_every=args.scan_every,
        sizing_mode=args.sizing,
        risk_pct=args.risk_pct,
        max_atr_pct=args.max_atr_pct,
    )

    sig = build_signals(args.strategy, universe, args.years, args.min_score,
                        args.scan_every, use_cache=not args.no_cache)
    n_sig = sum(len(v) for v in sig["signals"].values())
    print(f"\n{args.strategy.upper()}: {n_sig} raw signals across {len(sig['signals'])} scan days\n")

    prices = _price_frames(universe, args.years)

    if args.sweep_atr:
        configs = [
            ("LIVE today: fixed 3% stop", ExitConfig(stop_mode="pct", stop_pct=3.0,
                                                     trail_mode="pct", trail_activate_pct=5.0, trail_pct=2.5)),
            ("fixed 5% stop", ExitConfig(stop_mode="pct", stop_pct=5.0,
                                         trail_mode="pct", trail_activate_pct=5.0, trail_pct=2.5)),
            ("fixed 8% stop", ExitConfig(stop_mode="pct", stop_pct=8.0,
                                         trail_mode="pct", trail_activate_pct=8.0, trail_pct=4.0)),
        ]
        for m in (1.0, 1.5, 2.0, 2.5, 3.0, 4.0):
            configs.append((f"{m}x ATR stop", ExitConfig(stop_mode="atr", stop_atr_mult=m,
                                                         trail_mode="atr", trail_activate_atr=2.0,
                                                         trail_atr_mult=1.5)))
        bh = benchmark_buy_hold(args.years)
        if bh:
            print(f"CONTROL — SPY buy & hold: ret {bh['total_return_pct']:+.2f}%  "
                  f"CAGR {bh['cagr_pct']:+.2f}%  maxDD {bh['max_drawdown_pct']:.2f}%\n")
        print(f"stop-rule sweep (sizing={pf.sizing_mode}, risk={pf.risk_pct}%):")
        for label, ex in configs:
            print_stats(label, stats(simulate(sig, prices, ex, pf), pf))

    elif args.sweep_ladder:
        bh = benchmark_buy_hold(args.years)
        if bh:
            print(f"CONTROL — SPY buy & hold: ret {bh['total_return_pct']:+.2f}%  "
                  f"maxDD {bh['max_drawdown_pct']:.2f}%\n")
        base = dict(stop_mode="atr", stop_atr_mult=3.0 if args.strategy == "s2" else 2.5,
                    trail_mode="atr", trail_activate_atr=2.0, trail_atr_mult=1.5)
        LADDER = [(5, 2), (10, 6), (15, 11), (20, 15), (30, 24)]
        variants = [
            ("no ladder, 1.5% trail floor", ExitConfig(**base, trail_min_pct=1.5)),
            ("no ladder, 3% trail floor",   ExitConfig(**base, trail_min_pct=3.0)),
            ("LADDER + 3% trail floor",     ExitConfig(**base, trail_min_pct=3.0,
                                                       profit_ladder=LADDER)),
            ("LADDER only (trail off)",     ExitConfig(**{**base, "trail_mode": "off"},
                                                       profit_ladder=LADDER)),
        ]
        print("profit-ladder comparison:")
        for lbl, ex in variants:
            res = simulate(sig, prices, ex, pf)
            s = stats(res, pf)
            print_stats(lbl, s)
            print(f"      exits: {s.get('exit_reasons')}")
        print()

    elif args.robustness:
        bh = benchmark_buy_hold(args.years)
        if bh:
            print(f"CONTROL — SPY buy & hold: ret {bh['total_return_pct']:+.2f}%  "
                  f"maxDD {bh['max_drawdown_pct']:.2f}%\n")
        candidates = [
            ("3% fixed (live today)", ExitConfig(stop_mode="pct", stop_pct=3.0,
                                                 trail_mode="pct", trail_activate_pct=5.0, trail_pct=2.5)),
            ("8% fixed", ExitConfig(stop_mode="pct", stop_pct=8.0,
                                    trail_mode="pct", trail_activate_pct=8.0, trail_pct=4.0)),
            ("2.5x ATR", ExitConfig(stop_mode="atr", stop_atr_mult=2.5,
                                    trail_mode="atr", trail_activate_atr=2.0, trail_atr_mult=1.5)),
            ("3.0x ATR", ExitConfig(stop_mode="atr", stop_atr_mult=3.0,
                                    trail_mode="atr", trail_activate_atr=2.0, trail_atr_mult=1.5)),
        ]
        spy = _benchmark_frame(args.years)
        print(f"stability check — does the config work in BOTH halves?")
        print(f"  max_positions={pf.max_positions}   (rf {args.risk_free}%)\n")
        for label, ex in candidates:
            res = simulate(sig, prices, ex, pf)
            full = stats(res, pf)
            h1, h2 = split_stats(res, pf)
            print(f"  {label}")
            print_stats("    full period", full)
            print_stats("    first half", h1)
            print_stats("    second half", h2)

            # Risk-adjusted per half too. A config can hold its total return
            # across both halves while its alpha flips sign — return alone
            # cannot show that, and it is the thing the split exists to catch.
            if spy is not None:
                curve, trades = res["equity_curve"], res["trades"]
                mid = curve[len(curve) // 2][0]
                for name, cv, tr in (
                    ("full ", curve, trades),
                    ("1st  ", [c for c in curve if c[0] <= mid],
                              [t for t in trades if t.exit_date <= mid]),
                    ("2nd  ", [c for c in curve if c[0] > mid],
                              [t for t in trades if t.exit_date > mid])):
                    rk = risk_stats(cv, spy, tr, risk_free_pct=args.risk_free)
                    if rk.get("error"):
                        continue
                    print(f"      {name} beta {rk['beta']:>5.2f}  "
                          f"alpha {rk['alpha']:>+7.2f}%  "
                          f"Sharpe {rk['sharpe']:>5.2f}  (SPY {rk['bench_sharpe']:.2f})")
            print()

    elif args.sweep_rank:
        # The question this answers: if the score ranks candidates correctly,
        # then raising the bar and concentrating into fewer names should IMPROVE
        # risk-adjusted return. If the numbers are flat across the grid, the
        # score separates nothing and the ranking is decoration.
        #
        # Judged on alpha and Sharpe, not total return — a more concentrated
        # book almost always returns more in a bull market simply by carrying
        # more beta, and that is not evidence of anything.
        ex = ExitConfig(**({"stop_atr_mult": args.stop_atr} if args.stop_atr else {}))
        spy = _benchmark_frame(args.years)
        print(f"exit rule: {ex.label()}   (rf {args.risk_free}%)\n")
        print(f"{'score bar':>10} {'max pos':>8} {'trades':>7} {'CAGR':>8} "
              f"{'beta':>6} {'ALPHA':>8} {'Sharpe':>7} {'maxDD':>7}")
        base_sharpe = None
        for min_score in (60, 70, 80):
            for top_n in (4, 8, 15):
                p2 = PortfolioConfig(**{**asdict(pf), "max_positions": top_n})
                sub = filter_signals(sig, min_score, top_n)
                res = simulate(sub, prices, ex, p2)
                st = stats(res, p2)
                if not st.get("trades"):
                    print(f"{min_score:>10} {top_n:>8} {'—':>7}   no trades")
                    continue
                rk = risk_stats(res["equity_curve"], spy, res["trades"],
                                risk_free_pct=args.risk_free) if spy is not None else {}
                if base_sharpe is None:
                    base_sharpe = rk.get("bench_sharpe")
                print(f"{min_score:>10} {top_n:>8} {st['trades']:>7} "
                      f"{rk.get('cagr', 0):>+7.2f}% {rk.get('beta', 0):>6.2f} "
                      f"{rk.get('alpha', 0):>+7.2f}% {rk.get('sharpe', 0):>7.2f} "
                      f"{st['max_drawdown_pct']:>6.2f}%")
        if base_sharpe is not None:
            print(f"\n  SPY buy & hold Sharpe over the same window: {base_sharpe:.2f}")
            print("  A row only matters if its Sharpe beats that. Alpha alone does not.")

    elif args.sweep_risk:
        ex = ExitConfig(stop_mode="atr", stop_atr_mult=2.5,
                        trail_mode="atr", trail_activate_atr=2.0, trail_atr_mult=1.5)
        bh = benchmark_buy_hold(args.years)
        if bh:
            print(f"CONTROL — SPY buy & hold: ret {bh['total_return_pct']:+.2f}%  "
                  f"maxDD {bh['max_drawdown_pct']:.2f}%\n")
        print(f"sizing sweep at {ex.label()}:")
        for mode, val in [("equity_pct", 5.0), ("equity_pct", 10.0),
                          ("risk_pct", 0.5), ("risk_pct", 1.0), ("risk_pct", 2.0)]:
            p = PortfolioConfig(**{**asdict(pf), "sizing_mode": mode,
                                   "per_trade_pct": val if mode == "equity_pct" else pf.per_trade_pct,
                                   "risk_pct": val if mode == "risk_pct" else pf.risk_pct})
            lbl = f"{mode} {val}%"
            print_stats(lbl, stats(simulate(sig, prices, ex, p), p))
    else:
        ex = ExitConfig(**({"stop_atr_mult": args.stop_atr} if args.stop_atr else {}))
        res = simulate(sig, prices, ex, pf)
        s = stats(res, pf)
        print(f"exit rule: {ex.label()}")
        print_stats(args.strategy.upper(), s)
        print(f"\n  exit reasons: {s['exit_reasons']}")

        # Risk-adjusted view. Printed by default, not behind a flag: total
        # return alone cannot separate skill from index exposure, and every
        # conclusion drawn from this backtester so far has been drawn without it.
        spy = _benchmark_frame(args.years)
        if spy is not None:
            r = risk_stats(res["equity_curve"], spy, res["trades"],
                           risk_free_pct=args.risk_free)
            print(format_risk(r, args.strategy.upper()))
            s["risk"] = r
        out = CACHE_DIR / f"backtest_{args.strategy}_{datetime.now():%Y%m%d_%H%M%S}.json"
        out.write_text(json.dumps({
            "config": {"exits": asdict(ex), "portfolio": asdict(pf)},
            "stats": s,
            "trades": [asdict(t) for t in res["trades"]],
            "equity_curve": res["equity_curve"],
        }, indent=2))
        print(f"\n  report: {out.relative_to(HERE)}")


if __name__ == "__main__":
    main()
