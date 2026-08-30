"""
raanu.indicators — the shared indicator math
========================================================
Pure functions over price series: no I/O, no config, no logging. All three
strategies build on these, which is why they live apart from any one of
them. Extracted from the old flat ``strategy.py``.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


# Actionable threshold — a non-uptrend stock is capped just below this so it
# can never become a pick. Keep in sync with MIN_SIGNAL_SCORE (default 60).
UPTREND_SCORE_CAP = 45


# ---------- INDICATORS ----------
def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def ema(close: pd.Series, period: int) -> pd.Series:
    return close.ewm(span=period, adjust=False).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def bollinger(close: pd.Series, period: int = 20, num_std: float = 2.0):
    mid = close.rolling(period).mean()
    sd = close.rolling(period).std()
    return mid - num_std * sd, mid, mid + num_std * sd


def roc(close: pd.Series, n: int) -> float:
    """Rate of change over n bars (fractional, e.g. 0.12 = +12%)."""
    if len(close) <= n:
        return 0.0
    prev = float(close.iloc[-1 - n])
    if prev == 0:
        return 0.0
    return float(close.iloc[-1]) / prev - 1.0


def _fractal_swings(high: np.ndarray, low: np.ndarray, k: int = 5):
    """Return (swing_high_idxs, swing_low_idxs) using k-bar fractal pivots.
    A bar is a swing high if it is the highest high in the +/-k window (and
    symmetrically for lows). The final k bars can't be confirmed pivots."""
    n = len(high)
    hi_idx, lo_idx = [], []
    for i in range(k, n - k):
        if high[i] == high[i - k:i + k + 1].max():
            hi_idx.append(i)
        if low[i] == low[i - k:i + k + 1].min():
            lo_idx.append(i)
    return hi_idx, lo_idx


def golden_pocket(high: pd.Series, low: pd.Series, close: pd.Series,
                  lookback: int = 80, k: int = 5) -> Optional[dict]:
    """
    Identify the last up-impulse (swing low -> higher swing high) and the
    Fibonacci retracement zone the current price sits in.

    Returns dict with: swing_low, swing_high, gp_lo, gp_hi (0.786/0.618),
    ote_hi (0.5), in_golden_pocket, in_ote, retrace (fraction). None if no
    valid recent up-impulse is found.
    """
    if len(close) < lookback:
        lookback = len(close)
    if lookback < 2 * k + 5:
        return None

    h = high.iloc[-lookback:].to_numpy(dtype=float)
    l = low.iloc[-lookback:].to_numpy(dtype=float)
    hi_idx, lo_idx = _fractal_swings(h, l, k)
    if not hi_idx or not lo_idx:
        return None

    # Most recent confirmed swing high, and the lowest swing low before it.
    idx_h = hi_idx[-1]
    price_h = float(h[idx_h])
    prior_lows = [i for i in lo_idx if i < idx_h]
    if not prior_lows:
        return None
    idx_l = min(prior_lows, key=lambda i: l[i])
    price_l = float(l[idx_l])

    impulse = price_h - price_l
    if impulse <= 0:
        return None

    last = float(close.iloc[-1])
    # Retracement fraction from the swing high back down toward the swing low.
    retrace = (price_h - last) / impulse

    gp_hi = price_h - 0.618 * impulse   # shallow edge of golden pocket
    gp_lo = price_h - 0.786 * impulse   # deep edge
    ote_hi = price_h - 0.5 * impulse    # wider optimal-trade-entry edge

    return {
        "swing_low": round(price_l, 2),
        "swing_high": round(price_h, 2),
        "gp_lo": round(gp_lo, 2),
        "gp_hi": round(gp_hi, 2),
        "ote_hi": round(ote_hi, 2),
        "retrace": round(retrace, 3),
        "in_golden_pocket": gp_lo <= last <= gp_hi,
        "in_ote": gp_lo <= last <= ote_hi,
    }


def atr_pct_from_df(df, period: int = 14):
    """Wilder's ATR(14) as a percentage of the last close.

    Same maths as backtest.atr_series() — deliberately, so a signal alert and a
    backtest describe the same instrument the same way.
    """
    try:
        high, low, close = df["High"], df["Low"], df["Close"]
        prev = close.shift(1)
        import pandas as _pd
        tr = _pd.concat([high - low, (high - prev).abs(), (low - prev).abs()],
                        axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
        last = float(close.iloc[-1])
        if not last or atr != atr:          # NaN guard
            return None
        return round(float(atr) / last * 100, 2)
    except Exception:
        return None
