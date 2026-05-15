"""
strategy.py — Real indicator-based scoring engine
==================================================
Pulls daily OHLC from Yahoo Finance (free, no API key) and scores each
ticker on a 0-100 scale combining RSI, MACD, EMA trend, and Bollinger
bands. A score >= 60 is considered an actionable BUY signal.

The score is the engine's only opinion. Whether the bot actually trades
on that opinion is decided by auto_trader.py against the weekly /
per-trade limits.
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

import alpaca_data

log = logging.getLogger("raanu.strategy")


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


# ---------- SCORING ----------
def score_ticker(ticker: str) -> dict:
    """
    Returns a dict like:
      {ticker, price, rsi, macd, macd_signal, ema50, ema200,
       score (0-100), reasons[str], ok (bool), error (str|None)}
    """
    df = fetch_ohlc(ticker)
    if df is None or len(df) < 30:
        return {
            "ticker": ticker,
            "ok": False,
            "score": 0,
            "reasons": ["No market data"],
            "error": "fetch failed",
        }

    close = df["Close"].dropna()
    if len(close) < 30:
        return {"ticker": ticker, "ok": False, "score": 0, "reasons": ["Too little data"], "error": "short series"}

    last = float(close.iloc[-1])
    r_series = rsi(close)
    r_last = float(r_series.iloc[-1]) if not np.isnan(r_series.iloc[-1]) else 50.0

    macd_line, signal_line, hist = macd(close)
    macd_last = float(macd_line.iloc[-1])
    sig_last = float(signal_line.iloc[-1])
    hist_last = float(hist.iloc[-1])
    hist_prev = float(hist.iloc[-2]) if len(hist) >= 2 else hist_last

    e50 = float(ema(close, 50).iloc[-1])
    long_period = 200 if len(close) >= 200 else max(50, len(close) // 2)
    e200 = float(ema(close, long_period).iloc[-1])

    bb_lo_s, bb_mid_s, bb_hi_s = bollinger(close)
    bb_lo = float(bb_lo_s.iloc[-1])
    bb_mid = float(bb_mid_s.iloc[-1])

    # ---------- score components ----------
    score = 0
    reasons = []

    # RSI
    if r_last < 30:
        score += 30
        reasons.append(f"RSI {r_last:.1f} — oversold")
    elif r_last < 40:
        score += 18
        reasons.append(f"RSI {r_last:.1f} — mildly oversold")
    elif r_last > 70:
        score -= 25
        reasons.append(f"RSI {r_last:.1f} — overbought (avoid)")
    elif 45 <= r_last <= 60:
        score += 5
        reasons.append(f"RSI {r_last:.1f} — neutral")

    # MACD
    if macd_last > sig_last and hist_last > 0:
        score += 25
        reasons.append(f"MACD bullish ({macd_last:.2f} > signal {sig_last:.2f})")
        # bonus for fresh crossover (histogram flipped positive)
        if hist_prev <= 0 and hist_last > 0:
            score += 10
            reasons.append("MACD fresh bullish crossover")
    elif macd_last < sig_last:
        score -= 10
        reasons.append("MACD bearish")

    # EMA trend
    if e50 > e200:
        score += 20
        reasons.append(f"EMA50 €{e50:.2f} > EMA{long_period} €{e200:.2f} — uptrend")
    else:
        score -= 15
        reasons.append("EMA50 below long EMA — downtrend")

    # Bollinger
    if last <= bb_lo * 1.01:
        score += 15
        reasons.append(f"Price €{last:.2f} touching lower BB €{bb_lo:.2f}")
    elif last >= bb_mid * 1.05:
        score -= 5
        reasons.append("Price stretched above mid-band")

    score = max(0, min(100, score))

    return {
        "ticker": ticker,
        "price": last,
        "rsi": round(r_last, 2),
        "macd": round(macd_last, 4),
        "macd_signal": round(sig_last, 4),
        "ema50": round(e50, 2),
        "ema_long": round(e200, 2),
        "bb_low": round(bb_lo, 2),
        "score": int(score),
        "reasons": reasons,
        "ok": True,
        "error": None,
    }


def score_from_df(ticker: str, df: pd.DataFrame) -> dict:
    """Score a ticker from a pre-fetched OHLCV DataFrame (skip download)."""
    if df is None or len(df) < 30:
        return {"ticker": ticker, "ok": False, "score": 0, "reasons": ["No market data"], "error": "fetch failed"}
    close = df["Close"].dropna()
    if len(close) < 30:
        return {"ticker": ticker, "ok": False, "score": 0, "reasons": ["Too little data"], "error": "short series"}

    last = float(close.iloc[-1])
    r_series = rsi(close)
    r_last = float(r_series.iloc[-1]) if not np.isnan(r_series.iloc[-1]) else 50.0
    macd_line, signal_line, hist = macd(close)
    macd_last = float(macd_line.iloc[-1])
    sig_last  = float(signal_line.iloc[-1])
    hist_last = float(hist.iloc[-1])
    hist_prev = float(hist.iloc[-2]) if len(hist) >= 2 else hist_last
    e50 = float(ema(close, 50).iloc[-1])
    long_period = 200 if len(close) >= 200 else max(50, len(close) // 2)
    e200 = float(ema(close, long_period).iloc[-1])
    bb_lo_s, bb_mid_s, _ = bollinger(close)
    bb_lo  = float(bb_lo_s.iloc[-1])
    bb_mid = float(bb_mid_s.iloc[-1])

    score = 0; reasons = []
    if r_last < 30:   score += 30; reasons.append(f"RSI {r_last:.1f} — oversold")
    elif r_last < 40: score += 18; reasons.append(f"RSI {r_last:.1f} — mildly oversold")
    elif r_last > 70: score -= 25; reasons.append(f"RSI {r_last:.1f} — overbought")
    elif 45 <= r_last <= 60: score += 5; reasons.append(f"RSI {r_last:.1f} — neutral")
    if macd_last > sig_last and hist_last > 0:
        score += 25; reasons.append(f"MACD bullish ({macd_last:.2f} > {sig_last:.2f})")
        if hist_prev <= 0 and hist_last > 0: score += 10; reasons.append("MACD fresh bullish crossover")
    elif macd_last < sig_last: score -= 10; reasons.append("MACD bearish")
    if e50 > e200: score += 20; reasons.append(f"EMA50 {e50:.2f} > EMA{long_period} {e200:.2f} — uptrend")
    else:          score -= 15; reasons.append("EMA50 below long EMA — downtrend")
    if last <= bb_lo * 1.01:    score += 15; reasons.append(f"Price {last:.2f} touching lower BB {bb_lo:.2f}")
    elif last >= bb_mid * 1.05: score -= 5;  reasons.append("Price stretched above mid-band")

    return {
        "ticker": ticker, "price": last, "rsi": round(r_last, 2),
        "macd": round(macd_last, 4), "macd_signal": round(sig_last, 4),
        "ema50": round(e50, 2), "ema_long": round(e200, 2),
        "bb_low": round(bb_lo, 2), "score": int(max(0, min(100, score))),
        "reasons": reasons, "ok": True, "error": None,
    }


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
                df.columns = df.columns.get_level_values(0)
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
    out = []
    for t in tickers:
        try:
            out.append(score_ticker(t.strip().upper()))
        except Exception as e:
            log.exception(f"Scan error for {t}: {e}")
            out.append({"ticker": t, "ok": False, "score": 0, "reasons": [str(e)], "error": str(e)})
    out.sort(key=lambda x: x.get("score", 0), reverse=True)
    return out


if __name__ == "__main__":
    # smoke test
    logging.basicConfig(level=logging.INFO)
    for r in scan(["AAPL", "MSFT", "NVDA"]):
        print(f"{r['ticker']}: {r.get('score')}  reasons={r.get('reasons')}")
