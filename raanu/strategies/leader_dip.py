"""
strategy3.py — S3 "Market Leader Dip" (Bollinger + MACD mean reversion)
=======================================================================
Buys a *pullback to the lower Bollinger Band in a market leader*, confirmed by
MACD momentum turning back up. Where S1 buys pullbacks to a rising 20-EMA and
S2 buys breakouts to new highs, S3 buys temporary weakness in names that are
demonstrably beating the market.

Entry thesis
------------
  1. LEADER    — beating SPY over 3 months, and above its own 200-day trend.
                 Mean reversion is only safe inside an uptrend; buying dips in
                 a downtrend is buying a falling knife.
  2. STRETCHED — price at or below the lower Bollinger Band (2 std), i.e. an
                 unusually large deviation from its own 20-day mean.
  3. TURNING   — MACD histogram rising off its low: momentum is no longer
                 getting worse. Without this the band can be ridden all the way
                 down ("walking the band").

Deliberate design note on win rate
----------------------------------
Mean-reversion systems produce a HIGH win rate with SMALL wins, and lose it all
to a few large losses when a dip keeps going. Win rate alone says nothing about
profitability — expectancy is  p x avgWin - q x avgLoss.  In this project's own
S2 stop sweep the 68%-win configuration LOST money (payoff 0.46) while the
59%-win one made money (payoff 0.74). S3 therefore caps its score on names that
have already broken trend, and must be run with the ATR stop rather than a
tight one, or the "high win rate" evaporates into a few outsized losses.

Score range 0-100. Score >= 60 AND `leader_dip == true` is actionable.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger("raanu.s3")

# A name that has lost its 200-day trend is no longer a leader; cap its score
# below the actionable threshold so a deep dip can never qualify.
TREND_SCORE_CAP = 45


def _ema(close: pd.Series, period: int) -> pd.Series:
    return close.ewm(span=period, adjust=False).mean()


def _sma(close: pd.Series, period: int) -> pd.Series:
    return close.rolling(period).mean()


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def _macd(close: pd.Series, fast=12, slow=26, signal=9):
    macd_line = _ema(close, fast) - _ema(close, slow)
    signal_line = _ema(macd_line, signal)
    return macd_line, signal_line, macd_line - signal_line


def _bollinger(close: pd.Series, period: int = 20, num_std: float = 2.0):
    mid = _sma(close, period)
    sd = close.rolling(period).std()
    return mid + num_std * sd, mid, mid - num_std * sd


def _roc(close: pd.Series, n: int) -> float:
    if len(close) <= n:
        return 0.0
    prev = float(close.iloc[-1 - n])
    return 0.0 if prev == 0 else float(close.iloc[-1]) / prev - 1.0


def _empty(ticker: str, reason: str, err: str) -> dict:
    return {"ticker": ticker, "ok": False, "score": 0, "reasons": [reason],
            "error": err, "leader_dip": False, "strategy": "s3"}


def _score_core_s3(ticker: str, df: Optional[pd.DataFrame],
                   bench_ret_3m: Optional[float] = None) -> dict:
    if df is None or len(df) < 210:
        return _empty(ticker, "Insufficient history (<210 bars)", "short series")

    close = df["Close"].dropna()
    if len(close) < 210:
        return _empty(ticker, "Insufficient history (<210 bars)", "short series")
    vol  = df["Volume"].dropna() if "Volume" in df.columns else None
    low  = df["Low"].dropna()    if "Low"    in df.columns else close

    last = float(close.iloc[-1])
    if last <= 0:
        return _empty(ticker, "Bad price data", "non-positive close")

    # ── indicators ───────────────────────────────────────────────────────────
    bb_up, bb_mid, bb_low = _bollinger(close)
    bb_u, bb_m, bb_l = float(bb_up.iloc[-1]), float(bb_mid.iloc[-1]), float(bb_low.iloc[-1])
    band_width = (bb_u - bb_l)
    # %B: 0 = at lower band, 1 = at upper band. Negative = below the band.
    pct_b = (last - bb_l) / band_width if band_width > 0 else 0.5

    macd_line, sig_line, hist = _macd(close)
    macd_last = float(macd_line.iloc[-1])
    sig_last  = float(sig_line.iloc[-1])
    hist_last = float(hist.iloc[-1])
    hist_prev = float(hist.iloc[-2]) if len(hist) >= 2 else hist_last
    hist_prev2 = float(hist.iloc[-3]) if len(hist) >= 3 else hist_prev

    rsi_last = float(_rsi(close).iloc[-1])

    e200 = float(_ema(close, 200).iloc[-1])
    e50  = float(_ema(close, 50).iloc[-1])
    s200 = float(_sma(close, 200).iloc[-1])

    mom_3m = _roc(close, 63)
    mom_1m = _roc(close, 21)
    rel = (mom_3m - bench_ret_3m) if bench_ret_3m is not None else None

    vol_today   = float(vol.iloc[-1]) if vol is not None and len(vol) else 0.0
    vol_50d_avg = float(vol.iloc[-50:].mean()) if vol is not None and len(vol) >= 50 else 0.0
    vol_ratio   = (vol_today / vol_50d_avg) if vol_50d_avg > 0 else 1.0

    # ── gates ────────────────────────────────────────────────────────────────
    in_trend  = last > e200 and last > s200          # still a leader structurally
    is_leader = (rel is not None and rel > 0.0) or mom_3m > 0.10
    at_lower  = pct_b <= 0.20                        # in the lower fifth of the band
    turning   = hist_last > hist_prev                # MACD momentum improving

    leader_dip = bool(in_trend and is_leader and at_lower and turning)

    score = 0.0
    reasons: list[str] = []

    # 1. Trend integrity (max 20) — the thing that keeps this from buying knives
    if in_trend:
        score += 14
        reasons.append(f"Above 200-day trend (price ${last:.2f} > EMA200 ${e200:.2f})")
        if last > e50:
            score += 6
            reasons.append("Still above EMA50 — shallow dip")
    else:
        reasons.append("Below 200-day trend — not a leader, capped")

    # 2. Leadership / relative strength (max 22)
    if rel is not None:
        if rel > 0.20:
            score += 22; reasons.append(f"Market leader — beating SPY by {rel*100:.1f}% (3M)")
        elif rel > 0.10:
            score += 17; reasons.append(f"Strong RS +{rel*100:.1f}% vs SPY (3M)")
        elif rel > 0.0:
            score += 11; reasons.append(f"Beating SPY by {rel*100:.1f}% (3M)")
        else:
            reasons.append(f"Lagging SPY by {abs(rel)*100:.1f}% (3M)")
    elif mom_3m > 0.10:
        score += 11; reasons.append(f"3M momentum +{mom_3m*100:.1f}%")

    # 3. Bollinger stretch (max 26) — the actual entry trigger
    if pct_b <= 0.0:
        score += 26
        reasons.append(f"Price BELOW lower band (%B {pct_b:.2f}) — maximum stretch")
    elif pct_b <= 0.10:
        score += 22
        reasons.append(f"At lower Bollinger Band (%B {pct_b:.2f})")
    elif pct_b <= 0.20:
        score += 16
        reasons.append(f"Near lower band (%B {pct_b:.2f})")
    elif pct_b <= 0.35:
        score += 8
        reasons.append(f"Lower half of band (%B {pct_b:.2f})")
    elif pct_b >= 0.85:
        score -= 10
        reasons.append(f"At UPPER band (%B {pct_b:.2f}) — extended, not a dip")

    # 4. MACD momentum turning (max 20) — stops us catching a falling band
    if hist_last > hist_prev and hist_prev <= hist_prev2:
        score += 20
        reasons.append("MACD histogram turning up off its low")
    elif hist_last > hist_prev:
        score += 14
        reasons.append("MACD histogram rising")
    else:
        reasons.append("MACD still deteriorating — no turn yet")
    if macd_last > sig_last:
        score += 4
        reasons.append("MACD above signal line")

    # 5. Oversold quality (max 12)
    if 30 <= rsi_last <= 45:
        score += 12
        reasons.append(f"RSI {rsi_last:.0f} — oversold but not broken")
    elif rsi_last < 30:
        score += 6
        reasons.append(f"RSI {rsi_last:.0f} — deeply oversold (higher risk)")
    elif rsi_last <= 55:
        score += 5
        reasons.append(f"RSI {rsi_last:.0f} — neutral")
    else:
        reasons.append(f"RSI {rsi_last:.0f} — not oversold")

    # 6. Capitulation volume (max 6) — a flush often marks the low
    if vol_ratio > 1.5:
        score += 6
        reasons.append(f"Volume {vol_ratio:.1f}x average — capitulation flush")
    elif vol_ratio > 1.1:
        score += 3
        reasons.append(f"Volume {vol_ratio:.1f}x average")

    # Hard cap: a broken trend can never be actionable, however oversold.
    if not in_trend:
        score = min(score, TREND_SCORE_CAP)

    score = int(max(0, min(100, round(score))))

    return {
        "ticker": ticker,
        "price": last,
        "rsi": round(rsi_last, 2),
        "macd": round(macd_last, 4),
        "macd_signal": round(sig_last, 4),
        "macd_hist": round(hist_last, 4),
        "bb_upper": round(bb_u, 2),
        "bb_mid": round(bb_m, 2),
        "bb_lower": round(bb_l, 2),
        "pct_b": round(pct_b, 3),
        "ema50": round(e50, 2),
        "ema200": round(e200, 2),
        "mom_1m": round(mom_1m * 100, 1),
        "mom_3m": round(mom_3m * 100, 1),
        "rel_strength": round(rel * 100, 1) if rel is not None else None,
        "vol_ratio": round(vol_ratio, 2),
        "in_trend": in_trend,
        "leader_dip": leader_dip,
        "score": score,
        "reasons": reasons,
        "ok": True,
        "error": None,
        "strategy": "s3",
    }


def score_ticker_s3(ticker: str, bench_ret_3m: Optional[float] = None) -> dict:
    from raanu.market.prices import benchmark_return_3m, fetch_ohlc
    df = fetch_ohlc(ticker)
    if bench_ret_3m is None:
        bench_ret_3m = benchmark_return_3m()
    return _score_core_s3(ticker, df, bench_ret_3m)


def score_from_df_s3(ticker: str, df: pd.DataFrame,
                     bench_ret_3m: Optional[float] = None) -> dict:
    return _score_core_s3(ticker, df, bench_ret_3m)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for t in ["AAPL", "NVDA", "MSFT"]:
        r = score_ticker_s3(t)
        print(f"{r['ticker']}: {r['score']}  dip={r.get('leader_dip')}  %B={r.get('pct_b')}")
        for x in r.get("reasons", []):
            print("   -", x)
