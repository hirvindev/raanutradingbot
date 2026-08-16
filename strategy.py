"""
strategy.py — Trend + momentum scoring engine (pullback-in-uptrend)
===================================================================
Pulls daily OHLCV from Yahoo Finance (free, no API key) and scores each
ticker on a 0-100 scale. A score >= 60 is an actionable BUY signal.

Philosophy — only surface stocks that can actually make money:
  1. HARD UPTREND GATE. A stock must be in a confirmed uptrend
     (price above a rising EMA50, EMA50 above EMA200) or it is capped
     below the actionable threshold and can never be a pick. This kills
     "falling knives" (oversold stocks in a downtrend).
  2. Among uptrending stocks, favour a healthy PULLBACK entry — price
     that has dipped toward its rising 20-EMA with RSI in the 40-60 zone
     — over extended, overbought names that are chasing new highs.
  3. Reward relative strength vs the market (SPY) and volume confirmation.

The score is the engine's only opinion. Whether the bot trades on it is
decided by auto_trader.py against the weekly / per-trade limits.
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

log = logging.getLogger("raanu.strategy")

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


# ---------- STRUCTURE / FIBONACCI GOLDEN POCKET ----------
# Original implementation of well-known Smart-Money / Fibonacci concepts
# (swing structure + the 0.618-0.786 "golden pocket" pullback zone). Used to
# reward a price that has retraced into the optimal entry zone of its last
# up-impulse while the broader trend is still up.

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


# ---------- DATA ----------
def fetch_ohlc(ticker: str, period: str = "1y") -> Optional[pd.DataFrame]:
    """Daily candles via yfinance (fast, free, no API key needed)."""
    try:
        df = yf.download(
            ticker,
            period=period,
            interval="1d",
            progress=False,
            auto_adjust=True,
            threads=False,
        )
    except Exception as e:
        log.warning(f"yfinance error for {ticker}: {e}")
        return None
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def benchmark_return_3m(period: str = "1y") -> Optional[float]:
    """3-month return of SPY, used for relative-strength scoring. None on failure."""
    try:
        df = fetch_ohlc("SPY", period=period)
        if df is None or "Close" not in df:
            return None
        close = df["Close"].dropna()
        if len(close) < 64:
            return None
        return roc(close, 63)
    except Exception:
        return None


# ---------- SCORING ----------
def _empty(ticker: str, reason: str, err: str) -> dict:
    return {"ticker": ticker, "ok": False, "score": 0, "reasons": [reason], "error": err}


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


def _score_core(ticker: str, df: Optional[pd.DataFrame], bench_ret_3m: Optional[float] = None) -> dict:
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


def score_ticker(ticker: str, bench_ret_3m: Optional[float] = None) -> dict:
    """Download daily OHLCV and score one ticker."""
    df = fetch_ohlc(ticker)
    if bench_ret_3m is None:
        bench_ret_3m = benchmark_return_3m()
    return _score_core(ticker, df, bench_ret_3m)


def score_from_df(ticker: str, df: pd.DataFrame, bench_ret_3m: Optional[float] = None) -> dict:
    """Score a ticker from a pre-fetched OHLCV DataFrame (skip download)."""
    return _score_core(ticker, df, bench_ret_3m)


def batch_download(tickers: list[str], period: str = "1y") -> dict[str, pd.DataFrame]:
    """Download OHLCV for all tickers in a single yfinance call. Returns {ticker: df}.
    Delisted or unavailable tickers are silently skipped — they produce empty rows."""
    if not tickers:
        return {}
    try:
        raw = yf.download(
            tickers,
            period=period,
            interval="1d",
            progress=False,
            auto_adjust=True,
            threads=True,
            group_by="ticker",
            timeout=8,
        )
    except Exception as e:
        log.warning(f"Batch yfinance download failed: {e}")
        return {}

    result = {}
    if len(tickers) == 1:
        t = tickers[0]
        if not raw.empty:
            df = raw.copy()
            if isinstance(df.columns, pd.MultiIndex):
                # With group_by="ticker" the ticker is level 0 and the OHLCV
                # field is the last level — flattening to level 0 would name
                # every column after the ticker and lose "Close" entirely.
                levels = [
                    i for i in range(df.columns.nlevels)
                    if "Close" in df.columns.get_level_values(i)
                ]
                df.columns = df.columns.get_level_values(levels[0] if levels else -1)
            result[t] = df
        return result

    for t in tickers:
        try:
            df = raw[t].dropna(how="all")
            if not df.empty:
                result[t] = df
        except Exception:
            pass
    return result


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


if __name__ == "__main__":
    # smoke test
    logging.basicConfig(level=logging.INFO)
    for r in scan(["AAPL", "MSFT", "NVDA"]):
        print(f"{r['ticker']}: {r.get('score')}  up={r.get('uptrend')}  reasons={r.get('reasons')}")
