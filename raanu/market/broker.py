"""
alpaca_data.py — Alpaca market data layer
==========================================
Provides stock bars, asset search, most-active, and market movers
via the Alpaca REST API (no third-party SDK — uses httpx directly).

Set ALPACA_API_KEY and ALPACA_SECRET_KEY in your .env to enable.
Falls back gracefully when keys are absent.
"""

import logging
import os
from datetime import UTC, datetime, timedelta

import httpx
import pandas as pd

log = logging.getLogger("raanu.alpaca")

DATA_URL   = "https://data.alpaca.markets/v2"
BROKER_URL = "https://paper-api.alpaca.markets/v2"


def _headers() -> dict:
    return {
        "APCA-API-KEY-ID":     os.getenv("ALPACA_API_KEY", "").strip(),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", "").strip(),
    }


def is_configured() -> bool:
    return bool(os.getenv("ALPACA_API_KEY", "").strip())


# ---------- HISTORICAL BARS ----------

def fetch_bars(ticker: str, days: int = 120) -> pd.DataFrame | None:
    """
    Fetch daily OHLCV bars from Alpaca Data API.
    Returns a DataFrame with columns Open/High/Low/Close/Volume, or None on failure.
    """
    if not is_configured():
        return None

    # Alpaca IEX/SIP feeds only cover US-listed symbols; skip foreign tickers
    # (e.g. SAP.DE, SIE.DE) so strategy.py falls back to yfinance immediately.
    if "." in ticker:
        return None

    start = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        resp = httpx.get(
            f"{DATA_URL}/stocks/bars",
            params={
                "symbols":   ticker.upper(),
                "timeframe": "1Day",
                "start":     start,
                "limit":     days,
                "feed":      "iex",
            },
            headers=_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        bars = resp.json().get("bars", {}).get(ticker.upper(), [])
        if not bars:
            return None

        df = pd.DataFrame(bars)
        df["t"] = pd.to_datetime(df["t"], utc=True)
        df = df.set_index("t").rename(columns={
            "o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume",
        })
        return df[["Open", "High", "Low", "Close", "Volume"]].sort_index()

    except Exception as e:
        log.warning(f"Alpaca bars error for {ticker}: {e}")
        return None


# ---------- ASSET SEARCH ----------

def search_assets(query: str, limit: int = 15) -> list[dict]:
    """
    Search tradable US equity assets by symbol or name.
    Returns list of {symbol, name, exchange}.
    """
    if not is_configured():
        return []

    try:
        resp = httpx.get(
            f"{BROKER_URL}/assets",
            params={"status": "active", "asset_class": "us_equity"},
            headers=_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        assets = resp.json()

        q = query.strip().upper()
        matches = [
            a for a in assets
            if q in a.get("symbol", "").upper() or q in a.get("name", "").upper()
        ][:limit]

        return [
            {
                "symbol":   a["symbol"],
                "name":     a.get("name", ""),
                "exchange": a.get("exchange", ""),
                "tradable": a.get("tradable", False),
            }
            for a in matches
        ]

    except Exception as e:
        log.warning(f"Alpaca asset search error: {e}")
        return []


# ---------- MOST ACTIVE ----------

def get_most_active(top: int = 20) -> list[dict]:
    """
    Returns top stocks by trading volume today.
    Each item: {symbol, volume, trade_count, vwap}.
    """
    if not is_configured():
        return []

    try:
        resp = httpx.get(
            f"{DATA_URL}/stocks/most-actives",
            params={"by": "volume", "top": top},
            headers=_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("most_actives", [])

    except Exception as e:
        log.warning(f"Alpaca most-active error: {e}")
        return []


# ---------- MARKET MOVERS ----------

def get_market_movers(top: int = 10) -> dict:
    """
    Returns top gainers and losers for the day.
    Shape: {gainers: [...], losers: [...]}.
    """
    if not is_configured():
        return {"gainers": [], "losers": []}

    try:
        resp = httpx.get(
            f"{DATA_URL}/stocks/market-movers",
            params={"top": top},
            headers=_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    except Exception as e:
        log.warning(f"Alpaca market movers error: {e}")
        return {"gainers": [], "losers": []}


# ---------- LATEST SNAPSHOT ----------

def get_snapshot(ticker: str) -> dict | None:
    """
    Returns latest trade, quote, and bar for a symbol.
    Useful for fast price lookups without pulling full bars.
    """
    if not is_configured():
        return None

    try:
        resp = httpx.get(
            f"{DATA_URL}/stocks/snapshots",
            params={"symbols": ticker.upper(), "feed": "iex"},
            headers=_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get(ticker.upper())

    except Exception as e:
        log.warning(f"Alpaca snapshot error for {ticker}: {e}")
        return None
