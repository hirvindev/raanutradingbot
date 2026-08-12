"""
strategy5.py — S5 "Regime-Gated Momentum Leader"
=================================================
An attempt to beat S3, built from what this project's own backtests already
established rather than from a new idea.

What the evidence says
----------------------
  * S1 (pullback) and S2 (breakout) are profitable in the first half of the
    3-year window and negative in the second. Their ENTRY TIMING stopped
    working.
  * S3 (leader dip) survives both halves — but returns +42% against SPY's +79%.
  * A sizing sweep on S3 scales return and drawdown together (0.5% risk ->
    +17.4%/12.4%DD, 2.0% -> +53.7%/25.3%DD). Size cannot manufacture edge.

The structural gap is TIME IN MARKET. S1/S2/S3 all hold for days at a time and
sit in cash between trades. Over a window where the index rose 79%, cash is the
single largest drag, and no amount of entry cleverness recovers it.

So S5 does not try to time entries better. It tries to be invested in the right
names for longer, and to stand aside wholesale when the market itself is not
worth being in.

Two momentum layers
-------------------
  1. ABSOLUTE (regime) — SPY above its own 200-day SMA. When false, S5 emits
     nothing at all. This is the layer that cuts drawdown: the large losses in
     a momentum book come from holding through a market-wide decline, not from
     picking the wrong leader.
  2. RELATIVE (selection) — rank on blended 3 / 6 / 12-1 month return. The
     12-1 construction deliberately SKIPS the most recent month, the standard
     fix for short-term reversal: last month's biggest winner is disprop-
     ortionately likely to give it back next month.

Why this might beat S3 where S1 and S2 did not
-----------------------------------------------
S1 and S2 are timing strategies — they buy a specific bar (a pullback, a
breakout) and are wrong when that bar stops predicting. S5 is a RANKING
strategy: it holds whatever is strongest and rotates as the ranking changes,
so it degrades gracefully rather than breaking. Cross-sectional momentum is
also the effect with the most independent out-of-sample support in public
literature, which is worth something when this project cannot generate decades
of its own.

It may still fail. Momentum's known failure mode is the sharp reversal at a
market bottom, when yesterday's losers rip and a momentum book is positioned
exactly wrong. The regime gate exists to keep S5 out of the market at those
moments, and whether it does that well enough is precisely what the
first-half/second-half backtest has to answer.

Score range 0-100. Score >= 60 AND `momentum_leader == true` is actionable.

RESULT: FAILED. Not wired live — kept as a documented negative.
---------------------------------------------------------------
Backtested 3y, 472 tickers, scan-every-3, --robustness:

                       full period    first half    second half
    2.5x ATR             +21.01%       +21.02%        +0.31%
    3.0x ATR             +23.62%       +35.04%        -8.08%
    8% fixed             +29.83%       +32.58%        -1.61%
    3% fixed              +3.22%       +19.23%       -12.93%
    SPY buy & hold       +78.68%                    (maxDD 18.76%)

Every stop configuration is strongly profitable in the first half and flat to
negative in the second — the same shape as S1 and S2. THE REGIME GATE DID NOT
PREVENT THIS, which was the entire hypothesis: standing down when SPY is below
its 200-day SMA does not rescue a trend-following signal, because the second-half
damage happens while the index is still above its own trend line. Momentum
degraded without the market rolling over.

The wider lesson is worth more than the strategy: three independent
trend-following entries (S1 pullback, S2 breakout, S5 momentum ranking) all break
in the second half of this window, while the one mean-reversion strategy (S3)
does not. That is a property of the window, not of any one signal — do not spend
more effort on a fourth trend-following variant expecting a different answer.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger("raanu.s5")

# A name below its own 200-day trend is not a leader whatever its ranking says.
# Same discipline as UPTREND_SCORE_CAP in strategy.py and TREND_SCORE_CAP in
# strategy3.py: cap it below the actionable threshold rather than exclude it, so
# the score still reports how close it came.
TREND_SCORE_CAP = 45

# Trading days. 21 ~ 1 month.
M1, M3, M6, M12 = 21, 63, 126, 252


def _sma(close: pd.Series, period: int) -> pd.Series:
    return close.rolling(period).mean()


def _ret(close: pd.Series, lookback: int, skip: int = 0) -> Optional[float]:
    """Trailing return over `lookback` bars, ending `skip` bars ago."""
    need = lookback + skip + 1
    if len(close) < need:
        return None
    end = close.iloc[-1 - skip]
    start = close.iloc[-1 - skip - lookback]
    if start <= 0:
        return None
    return float(end / start - 1)


def _empty(ticker: str, reason: str, err: str) -> dict:
    return {"ticker": ticker, "score": 0, "ok": False, "error": err,
            "reasons": [reason], "momentum_leader": False}


def market_regime_on(spy_close: pd.Series) -> bool:
    """Absolute momentum: is SPY above its own 200-day SMA?

    Evaluated on trailing data only — the caller passes history up to and
    including the scan day, and entry happens at the NEXT day's open.
    """
    if spy_close is None or len(spy_close) < 200:
        return False
    return bool(float(spy_close.iloc[-1]) > float(_sma(spy_close, 200).iloc[-1]))


def _score_core_s5(ticker: str, df: Optional[pd.DataFrame],
                   bench_ret_3m: Optional[float] = None) -> dict:
    if df is None or df.empty:
        return _empty(ticker, "No price data", "empty dataframe")
    if len(df) < M12 + M1 + 5:
        return _empty(ticker, "Not enough history for 12-month momentum", "short history")

    try:
        close = df["Close"].astype(float)
        vol = df["Volume"].astype(float)
        price = float(close.iloc[-1])

        sma50 = float(_sma(close, 50).iloc[-1])
        sma200_series = _sma(close, 200)
        sma200 = float(sma200_series.iloc[-1])
        sma200_rising = bool(sma200 > float(sma200_series.iloc[-M1]))

        r3 = _ret(close, M3)
        r6 = _ret(close, M6)
        r12_1 = _ret(close, M12 - M1, skip=M1)     # 12-month, skipping last month

        if r3 is None or r6 is None or r12_1 is None:
            return _empty(ticker, "Momentum windows unavailable", "insufficient bars")

        # Blend. 12-1 carries the most weight — it is the horizon with the
        # strongest published persistence — with 3M and 6M confirming that the
        # trend is still live rather than a stale year-old move.
        blended = 0.5 * r12_1 + 0.3 * r6 + 0.2 * r3

        rel_strength = (r3 - bench_ret_3m) if bench_ret_3m is not None else None

        # Realized volatility, annualised from daily returns. Momentum's worst
        # drawdowns come from its most volatile names, and penalising vol is the
        # cheapest known improvement to the raw effect.
        daily = close.pct_change().dropna()
        vol_ann = float(daily.iloc[-M3:].std() * np.sqrt(252)) if len(daily) >= M3 else None

        # Consistency: share of the last 6 months' weeks that closed up. A
        # smooth advance is more likely to persist than one driven by a single
        # gap that the blended return cannot distinguish from steady strength.
        wk = close.iloc[-M6:].resample("W").last() if isinstance(close.index, pd.DatetimeIndex) else None
        consistency = float((wk.pct_change().dropna() > 0).mean()) if wk is not None and len(wk) > 4 else None

        in_trend = bool(price > sma200 and sma50 > sma200 and sma200_rising)

        score = 0.0
        reasons: list[str] = []

        # ── momentum, 12-1 (30) ──────────────────────────────────────────────
        # +40% over the 12-1 window earns full marks; scaled, not stepped, so
        # ranking stays continuous rather than bucketing everything at the top.
        m = max(0.0, min(1.0, r12_1 / 0.40))
        score += 30 * m
        reasons.append(f"12-1M momentum {r12_1*100:+.1f}%")

        # ── momentum, 6M (20) ────────────────────────────────────────────────
        score += 20 * max(0.0, min(1.0, r6 / 0.25))
        reasons.append(f"6M momentum {r6*100:+.1f}%")

        # ── relative strength vs SPY (15) ────────────────────────────────────
        if rel_strength is not None:
            score += 15 * max(0.0, min(1.0, rel_strength / 0.15))
            if rel_strength > 0:
                reasons.append(f"Beating SPY by {rel_strength*100:.1f}% (3M)")
            else:
                reasons.append(f"Lagging SPY by {abs(rel_strength)*100:.1f}% (3M)")

        # ── trend structure (20) ─────────────────────────────────────────────
        if price > sma200:
            score += 8
        if sma50 > sma200:
            score += 6
        if sma200_rising:
            score += 6
        reasons.append("Above rising 200-day trend" if in_trend
                       else "Trend structure incomplete")

        # ── quality: low vol + consistency (15) ──────────────────────────────
        if vol_ann is not None:
            # 25% annualised or lower is calm for this universe; 80%+ is a
            # crypto miner. Full marks at the calm end, nothing at the wild end.
            score += 8 * max(0.0, min(1.0, (0.80 - vol_ann) / 0.55))
            reasons.append(f"Realised vol {vol_ann*100:.0f}% (annualised)")
        if consistency is not None:
            score += 7 * max(0.0, min(1.0, (consistency - 0.40) / 0.30))
            reasons.append(f"{consistency*100:.0f}% of weeks closed up (6M)")

        score = float(max(0.0, min(100.0, score)))

        # Hard cap: not in its own uptrend means it cannot be actionable, no
        # matter how strong the trailing ranking looks.
        if not in_trend:
            score = min(score, TREND_SCORE_CAP)
            reasons.append(f"Below its own 200-day trend — capped at {TREND_SCORE_CAP}")

        momentum_leader = bool(
            in_trend
            and r6 > 0
            and (rel_strength is None or rel_strength > 0)
        )

        return {
            "ticker": ticker,
            "price": price,
            "sma50": round(sma50, 2),
            "sma200": round(sma200, 2),
            "mom_3m": round(r3 * 100, 1),
            "mom_6m": round(r6 * 100, 1),
            "mom_12_1": round(r12_1 * 100, 1),
            "blended_mom": round(blended * 100, 1),
            "rel_strength": round(rel_strength * 100, 1) if rel_strength is not None else None,
            "vol_ann": round(vol_ann * 100, 1) if vol_ann is not None else None,
            "consistency": round(consistency * 100, 0) if consistency is not None else None,
            "in_trend": in_trend,
            "momentum_leader": momentum_leader,
            "score": int(round(score)),
            "reasons": reasons,
            "ok": True,
            "error": None,
        }

    except Exception as e:
        return _empty(ticker, "Scoring failed", str(e))


def score_from_df_s5(ticker: str, df: pd.DataFrame,
                     bench_ret_3m: Optional[float] = None) -> dict:
    return _score_core_s5(ticker, df, bench_ret_3m)


def score_ticker_s5(ticker: str, bench_ret_3m: Optional[float] = None) -> dict:
    from strategy import fetch_ohlc
    return _score_core_s5(ticker, fetch_ohlc(ticker, period="2y"), bench_ret_3m)
