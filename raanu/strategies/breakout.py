"""
strategy2.py — VCP Breakout scoring engine
===========================================
Volatility Contraction Pattern (VCP) breakout strategy inspired by
Mark Minervini's SEPA methodology. Buys stocks breaking out of a
tight consolidation near 52-week highs on above-average volume.

Key differences from Strategy 1 (pullback-in-uptrend):
  S1 enters on WEAKNESS  — price dips to rising EMA20, RSI 40-60
  S2 enters on STRENGTH  — price breaks above consolidation on volume

Score components (0-100):
  1. Stage 2 trend template    — max 20
  2. Proximity to 52-week high — max 15
  3. Consolidation tightness   — max 20
  4. Volume pattern            — max 15
  5. Relative strength vs SPY  — max 15
  6. MACD regime               — max  8
  7. Breakout confirmation     — max  7

Score >= 60 AND stage2 == true  →  actionable breakout BUY.
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger("raanu.strategy2")

STAGE2_SCORE_CAP = 45


def _sma(close: pd.Series, period: int) -> pd.Series:
    return close.rolling(period).mean()


def _ema(close: pd.Series, period: int) -> pd.Series:
    return close.ewm(span=period, adjust=False).mean()


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _macd(close: pd.Series, fast=12, slow=26, signal=9):
    macd_line = _ema(close, fast) - _ema(close, slow)
    signal_line = _ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def _roc(close: pd.Series, n: int) -> float:
    if len(close) <= n:
        return 0.0
    prev = float(close.iloc[-1 - n])
    if prev == 0:
        return 0.0
    return float(close.iloc[-1]) / prev - 1.0


def _empty(ticker: str, reason: str, err: str) -> dict:
    return {"ticker": ticker, "ok": False, "score": 0, "reasons": [reason],
            "error": err, "strategy": "s2"}


def _score_core_s2(ticker: str, df: Optional[pd.DataFrame],
                   bench_ret_3m: Optional[float] = None) -> dict:
    if df is None or len(df) < 60:
        return _empty(ticker, "Insufficient history (<60 bars)", "short series")

    close = df["Close"].dropna()
    if len(close) < 60:
        return _empty(ticker, "Insufficient history (<60 bars)", "short series")
    vol = df["Volume"].dropna() if "Volume" in df.columns else None
    high = df["High"].dropna() if "High" in df.columns else close
    low = df["Low"].dropna() if "Low" in df.columns else close

    last = float(close.iloc[-1])

    # Indicators
    r_series = _rsi(close)
    r_last = float(r_series.iloc[-1]) if not np.isnan(r_series.iloc[-1]) else 50.0

    macd_line, signal_line, hist = _macd(close)
    macd_last = float(macd_line.iloc[-1])
    sig_last = float(signal_line.iloc[-1])
    hist_last = float(hist.iloc[-1])
    hist_prev = float(hist.iloc[-2]) if len(hist) >= 2 else hist_last

    sma150 = float(_sma(close, min(150, len(close))).iloc[-1])
    sma200 = float(_sma(close, min(200, len(close))).iloc[-1])
    sma200_prev = float(_sma(close, min(200, len(close))).iloc[-min(40, len(close) - 1)])

    e20 = float(_ema(close, 20).iloc[-1])

    mom_3m = _roc(close, 63)
    rel = (mom_3m - bench_ret_3m) if bench_ret_3m is not None else None

    # 52-week high/low
    lookback_52w = min(252, len(high))
    high_52w = float(high.iloc[-lookback_52w:].max())
    low_52w = float(low.iloc[-lookback_52w:].min())
    dist_from_high = (high_52w - last) / high_52w if high_52w > 0 else 1.0

    # Consolidation: 20-day price range as % of price
    recent_high_20 = float(high.iloc[-20:].max())
    recent_low_20 = float(low.iloc[-20:].min())
    consolidation_range = (recent_high_20 - recent_low_20) / last if last > 0 else 1.0

    # Volume analysis
    vol_today = float(vol.iloc[-1]) if vol is not None and len(vol) >= 1 else 0
    vol_5d_avg = float(vol.iloc[-5:].mean()) if vol is not None and len(vol) >= 5 else 0
    vol_50d_avg = float(vol.iloc[-50:].mean()) if vol is not None and len(vol) >= 50 else (
        float(vol.mean()) if vol is not None and len(vol) >= 10 else 0
    )

    # Breakout: price vs recent resistance (20-day high, 10-day high)
    high_10d = float(high.iloc[-11:-1].max()) if len(high) >= 11 else float(high.iloc[:-1].max())
    high_20d = float(high.iloc[-21:-1].max()) if len(high) >= 21 else float(high.iloc[:-1].max())

    # STAGE 2 TREND TEMPLATE
    stage2 = (last > sma150 > sma200) and (sma200 > sma200_prev)

    score = 0
    reasons: list[str] = []

    # 1) Stage 2 trend template — max 20
    if last > sma200:
        score += 5
    if last > sma150:
        score += 5
    if sma150 > sma200:
        score += 5
    if sma200 > sma200_prev:
        score += 5
    if stage2:
        reasons.append("Stage 2 uptrend confirmed (Minervini template)")
    else:
        reasons.append("Not in Stage 2 uptrend")

    # 2) Proximity to 52-week high — max 15
    if dist_from_high <= 0.05:
        score += 15
        reasons.append(f"Within {dist_from_high*100:.1f}% of 52-week high ${high_52w:.2f}")
    elif dist_from_high <= 0.10:
        score += 12
        reasons.append(f"{dist_from_high*100:.1f}% from 52-week high")
    elif dist_from_high <= 0.15:
        score += 8
        reasons.append(f"{dist_from_high*100:.1f}% from 52-week high")
    elif dist_from_high <= 0.25:
        score += 4
    else:
        score -= 5
        reasons.append(f"Far from 52-week high ({dist_from_high*100:.0f}% away)")

    # 3) Consolidation tightness (VCP core) — max 20
    if consolidation_range < 0.08:
        score += 20
        reasons.append(f"Very tight base ({consolidation_range*100:.1f}% range) — VCP setup")
    elif consolidation_range < 0.12:
        score += 15
        reasons.append(f"Tight consolidation ({consolidation_range*100:.1f}% range)")
    elif consolidation_range < 0.18:
        score += 10
        reasons.append(f"Moderate base ({consolidation_range*100:.1f}% range)")
    elif consolidation_range < 0.25:
        score += 5
    else:
        score -= 5
        reasons.append(f"Wide/choppy range ({consolidation_range*100:.0f}%) — no VCP")

    # 4) Volume pattern — max 15
    vol_score = 0
    if vol_50d_avg > 0:
        vol_ratio_recent = vol_5d_avg / vol_50d_avg if vol_50d_avg > 0 else 1.0
        vol_ratio_today = vol_today / vol_50d_avg if vol_50d_avg > 0 else 1.0

        # Volume dry-up in base (sellers exhausted)
        if vol_ratio_recent < 0.70:
            vol_score += 8
            reasons.append("Volume dried up in base — sellers exhausted")
        elif vol_ratio_recent < 0.85:
            vol_score += 4

        # Breakout volume surge
        up_day = last > float(close.iloc[-2]) if len(close) >= 2 else False
        if up_day and vol_ratio_today > 1.5:
            vol_score += 7
            reasons.append(f"Breakout volume {vol_ratio_today:.1f}x average")
        elif up_day and vol_ratio_today > 1.0:
            vol_score += 3
    score += min(15, vol_score)

    # 5) Relative strength vs SPY — max 15
    if rel is not None:
        if rel > 0.15:
            score += 15
            reasons.append(f"Strong RS — beating SPY by {rel*100:.1f}% (3M)")
        elif rel > 0.10:
            score += 12
            reasons.append(f"RS +{rel*100:.1f}% vs SPY (3M)")
        elif rel > 0.05:
            score += 8
        elif rel > 0:
            score += 4
        elif rel < -0.05:
            score -= 5
            reasons.append(f"Lagging SPY by {abs(rel)*100:.1f}%")

    # 6) MACD regime — max 8
    if macd_last > sig_last and hist_last > 0:
        score += 5
        reasons.append("MACD bullish")
        if hist_prev <= 0 and hist_last > 0:
            score += 3
            reasons.append("MACD fresh crossover")
    elif macd_last < sig_last:
        score -= 4
        reasons.append("MACD bearish")

    # 7) Breakout confirmation — max 7
    if last > high_20d:
        score += 7
        reasons.append(f"Breaking above 20-day resistance ${high_20d:.2f}")
    elif last > high_10d:
        score += 4
        reasons.append(f"Breaking above 10-day high ${high_10d:.2f}")

    score = int(max(0, min(100, score)))

    if not stage2:
        score = min(score, STAGE2_SCORE_CAP)

    return {
        "ticker": ticker,
        "price": last,
        "rsi": round(r_last, 2),
        "macd": round(macd_last, 4),
        "macd_signal": round(sig_last, 4),
        "ema20": round(e20, 2),
        "sma150": round(sma150, 2),
        "sma200": round(sma200, 2),
        "high_52w": round(high_52w, 2),
        "low_52w": round(low_52w, 2),
        "dist_from_high_pct": round(dist_from_high * 100, 1),
        "consolidation_range_pct": round(consolidation_range * 100, 1),
        "mom_3m": round(mom_3m * 100, 1),
        "rel_strength": round(rel * 100, 1) if rel is not None else None,
        "stage2": stage2,
        "score": score,
        "reasons": reasons,
        "ok": True,
        "error": None,
        "strategy": "s2",
    }


def score_ticker_s2(ticker: str, bench_ret_3m: Optional[float] = None) -> dict:
    from raanu.market.prices import benchmark_return_3m, fetch_ohlc
    df = fetch_ohlc(ticker)
    if bench_ret_3m is None:
        bench_ret_3m = benchmark_return_3m()
    return _score_core_s2(ticker, df, bench_ret_3m)


def score_from_df_s2(ticker: str, df: pd.DataFrame,
                     bench_ret_3m: Optional[float] = None) -> dict:
    return _score_core_s2(ticker, df, bench_ret_3m)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for r in [score_ticker_s2(t) for t in ["AAPL", "MSFT", "NVDA"]]:
        print(f"{r['ticker']}: {r.get('score')}  stage2={r.get('stage2')}  reasons={r.get('reasons')}")
