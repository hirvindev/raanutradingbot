"""
raanu.market.prices — OHLCV and benchmark data from Yahoo Finance
==================================================================
Market data, deliberately separated from the strategies that consume it:
these functions know nothing about scoring, and the strategies know nothing
about where bars come from. Extracted from the old flat ``strategy.py``.
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd
import yfinance as yf

from raanu.indicators import roc

log = logging.getLogger("raanu.market.prices")


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
