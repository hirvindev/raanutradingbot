"""
raanu.strategies.pullback — Strategy 1: pullback in a confirmed uptrend
========================================================================
Buys a healthy dip toward a rising 20-EMA in a name already in a confirmed
uptrend. The HARD UPTREND GATE is the load-bearing rule: a stock not in a
confirmed uptrend (price > EMA200 AND EMA50 > EMA200 AND EMA50 rising) has
its score capped at UPTREND_SCORE_CAP so it can never reach the actionable
threshold. The engine only surfaces stocks that can make money — it does
NOT buy falling knives.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from raanu.indicators import (
    UPTREND_SCORE_CAP,
    atr_pct_from_df,
    bollinger,
    ema,
    golden_pocket,
    macd,
    roc,
    rsi,
)
from raanu.market.prices import benchmark_return_3m, fetch_ohlc

log = logging.getLogger("raanu.strategies.pullback")


# ---------- SCORING ----------
def _empty(ticker: str, reason: str, err: str) -> dict:
    return {"ticker": ticker, "ok": False, "score": 0, "reasons": [reason], "error": err}


def _score_core(ticker: str, df: pd.DataFrame | None, bench_ret_3m: float | None = None) -> dict:
    """
    Score one ticker from a pre-fetched OHLCV DataFrame using a trend +
    momentum + pullback model. `bench_ret_3m` is SPY's 3-month return for
    relative-strength scoring (optional — skipped if None).
    """
    if df is None or len(df) < 60:
        return _empty(ticker, "Insufficient history (<60 bars)", "short series")

    close = df["Close"].dropna()
    if len(close) < 60:
        return _empty(ticker, "Insufficient history (<60 bars)", "short series")
    vol = df["Volume"].dropna() if "Volume" in df.columns else None
    atr_pct = atr_pct_from_df(df)
    high = df["High"].dropna() if "High" in df.columns else close
    low = df["Low"].dropna() if "Low" in df.columns else close

    last = float(close.iloc[-1])

    # ── indicators ──────────────────────────────────────────────────────────
    r_series = rsi(close)
    r_last = float(r_series.iloc[-1]) if not np.isnan(r_series.iloc[-1]) else 50.0

    macd_line, signal_line, hist = macd(close)
    macd_last = float(macd_line.iloc[-1])
    sig_last = float(signal_line.iloc[-1])
    hist_last = float(hist.iloc[-1])
    hist_prev = float(hist.iloc[-2]) if len(hist) >= 2 else hist_last

    e20_series = ema(close, 20)
    e50_series = ema(close, 50)
    e20 = float(e20_series.iloc[-1])
    e50 = float(e50_series.iloc[-1])
    e50_prev = float(e50_series.iloc[-11]) if len(e50_series) >= 11 else float(e50_series.iloc[0])
    long_period = 200 if len(close) >= 200 else max(50, len(close) // 2)
    e200 = float(ema(close, long_period).iloc[-1])

    bb_lo_s, bb_mid_s, _ = bollinger(close)
    bb_lo = float(bb_lo_s.iloc[-1])

    mom_1m = roc(close, 21)
    mom_3m = roc(close, 63)
    rel = (mom_3m - bench_ret_3m) if bench_ret_3m is not None else None
    dist_e20 = (last / e20 - 1.0) if e20 else 0.0  # +ve = above the 20-EMA

    # ── HARD UPTREND GATE ───────────────────────────────────────────────────
    e50_rising = e50 > e50_prev
    uptrend = (last > e200) and (e50 > e200) and e50_rising

    score = 0
    reasons: list[str] = []

    # 1) Trend structure (foundation) — max 20
    if uptrend:
        score += 15
        reasons.append(f"Confirmed uptrend — price > EMA{long_period}, EMA50 rising")
        if last > e50:
            score += 5
            reasons.append(f"Price ${last:.2f} above EMA50 ${e50:.2f}")
    else:
        if last <= e200:
            reasons.append(f"Below EMA{long_period} — not an uptrend")
        elif not e50_rising:
            reasons.append("EMA50 flat/falling — trend not confirmed")
        else:
            reasons.append("EMA50 below long EMA — not an uptrend")

    # 2) Momentum — max 15
    if mom_3m > 0:
        m_pts = int(max(0, min(15, round(mom_3m * 60))))
        if m_pts:
            score += m_pts
            reasons.append(f"3M momentum +{mom_3m * 100:.1f}%")
    elif mom_3m < -0.02:
        score -= 6
        reasons.append(f"3M momentum {mom_3m * 100:.1f}% — weak")

    # 3) Relative strength vs SPY — max 12
    if rel is not None:
        if rel > 0:
            rs_pts = int(max(0, min(12, round(rel * 50))))
            if rs_pts:
                score += rs_pts
                reasons.append(f"Beating market by {rel * 100:.1f}% (3M)")
        elif rel < -0.05:
            score -= 5
            reasons.append(f"Lagging market by {abs(rel) * 100:.1f}% (3M)")

    # 4) MACD regime — max 10
    if macd_last > sig_last and hist_last > 0:
        score += 6
        reasons.append("MACD bullish")
        if hist_prev <= 0 and hist_last > 0:
            score += 4
            reasons.append("MACD fresh crossover")
    elif macd_last < sig_last:
        score -= 6
        reasons.append("MACD bearish")

    # 5) Pullback quality (favoured entry) — max 15 / penalties
    if 40 <= r_last <= 55:
        score += 15
        reasons.append(f"RSI {r_last:.0f} — healthy pullback zone")
    elif 35 <= r_last < 40:
        score += 8
        reasons.append(f"RSI {r_last:.0f} — deeper dip in uptrend")
    elif 55 < r_last <= 63:
        score += 8
        reasons.append(f"RSI {r_last:.0f} — mild strength")
    elif 63 < r_last <= 72:
        score += 3
        reasons.append(f"RSI {r_last:.0f} — extended")
    elif r_last > 72:
        score -= 15
        reasons.append(f"RSI {r_last:.0f} — overbought, chasing")
    elif r_last < 35:
        score -= 8
        reasons.append(f"RSI {r_last:.0f} — weakening")

    # 6) Proximity to rising 20-EMA (pulled back to support, not extended) — max 10
    if -0.03 <= dist_e20 <= 0.05:
        score += 10
        reasons.append("Pulled back near rising 20-EMA (good entry)")
    elif 0.05 < dist_e20 <= 0.12:
        score += 3
    elif dist_e20 > 0.12:
        score -= 10
        reasons.append(f"Extended {dist_e20 * 100:.0f}% above 20-EMA — chasing")

    # 7) Volume / structure confirmation — max 10
    if vol is not None and len(vol) >= 20:
        avg_vol = float(vol.iloc[-20:].mean())
        up_day = last > float(close.iloc[-2])
        if up_day and float(vol.iloc[-1]) > avg_vol:
            score += 6
            reasons.append("Up day on above-average volume")
    if len(close) >= 6 and last > float(close.iloc[-6:-1].min()):
        score += 4  # holding above the last week's low (not breaking down)

    # 8) Fibonacci golden-pocket pullback (SMC structure layer) — max 12
    #    Only meaningful inside an uptrend: reward price retraced into the
    #    optimal entry zone of its last up-impulse, and confirmed turning up.
    gp = golden_pocket(high, low, close)
    turning_up = last > float(close.iloc[-2])  # bullish close vs prior bar
    if gp and uptrend:
        if gp["in_golden_pocket"] and turning_up:
            score += 12
            reasons.append(
                f"In Fib golden pocket ({gp['gp_lo']}-{gp['gp_hi']}) of last "
                f"up-swing, turning up — prime entry"
            )
        elif gp["in_golden_pocket"]:
            score += 7
            reasons.append(f"In Fib golden pocket ({gp['gp_lo']}-{gp['gp_hi']})")
        elif gp["in_ote"]:
            score += 5
            reasons.append("In optimal trade zone (0.5-0.62 retrace)")

    score = int(max(0, min(100, score)))

    # Enforce the gate: a non-uptrend stock can never be actionable.
    if not uptrend:
        score = min(score, UPTREND_SCORE_CAP)

    return {
        "ticker": ticker,
        "price": last,
        "rsi": round(r_last, 2),
        "macd": round(macd_last, 4),
        "macd_signal": round(sig_last, 4),
        "ema20": round(e20, 2),
        "ema50": round(e50, 2),
        "ema_long": round(e200, 2),
        "bb_low": round(bb_lo, 2),
        "mom_1m": round(mom_1m * 100, 1),
        "mom_3m": round(mom_3m * 100, 1),
        "rel_strength": round(rel * 100, 1) if rel is not None else None,
        # ATR as a % of price. The exit engine scales every stop and trail from
        # this, so without it a signal alert cannot state where the trade would
        # be closed — which is the number that decides whether to take it.
        "atr_pct": atr_pct,
        "uptrend": uptrend,
        "in_golden_pocket": bool(gp["in_golden_pocket"]) if gp else False,
        "swing_high": gp["swing_high"] if gp else None,
        "swing_low": gp["swing_low"] if gp else None,
        "fib_retrace": gp["retrace"] if gp else None,
        "score": score,
        "reasons": reasons,
        "ok": True,
        "error": None,
    }


def score_ticker(ticker: str, bench_ret_3m: float | None = None) -> dict:
    """Download daily OHLCV and score one ticker."""
    df = fetch_ohlc(ticker)
    if bench_ret_3m is None:
        bench_ret_3m = benchmark_return_3m()
    return _score_core(ticker, df, bench_ret_3m)


def score_from_df(ticker: str, df: pd.DataFrame, bench_ret_3m: float | None = None) -> dict:
    """Score a ticker from a pre-fetched OHLCV DataFrame (skip download)."""
    return _score_core(ticker, df, bench_ret_3m)


def scan(tickers: list[str]) -> list[dict]:
    """Score every ticker. Returns sorted by score desc."""
    bench = benchmark_return_3m()
    out = []
    for t in tickers:
        try:
            out.append(score_ticker(t.strip().upper(), bench_ret_3m=bench))
        except Exception as e:
            log.exception(f"Scan error for {t}: {e}")
            out.append({"ticker": t, "ok": False, "score": 0, "reasons": [str(e)], "error": str(e)})
    out.sort(key=lambda x: x.get("score", 0), reverse=True)
    return out
