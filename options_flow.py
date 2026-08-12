"""
options_flow.py — weekly-expiry call flow from Alpaca's option snapshots
========================================================================
Data layer for S4. Given a list of underlyings it returns, for the nearest
weekly expiry, the total call volume, total put volume, and the call share of
the two.

Feed reality (measured, not assumed)
------------------------------------
  * `opra`       -> 403 "OPRA agreement is not signed". Sign it free in the
                    Alpaca dashboard; until then real trade prints are not
                    available.
  * `indicative` -> 200. Trades are DELAYED and quotes are MODIFIED. Contract
                    daily volume is present and usable for ranking, but it is
                    not the authoritative tape.
  * open interest is NOT returned by either feed's snapshot endpoint, on any
    plan available here.

That last point is the important one, and it constrains what S4 can be. The
standard way to separate *new positioning* from *existing* is volume / open
interest; without OI, raw volume mostly ranks liquidity — the same mega caps
every day. S4 therefore never ranks on raw volume alone; see CALL_SHARE in
strategy4.py, which uses the call share of total option volume so that a small
name with lopsided call activity can outrank a big name with balanced flow.

Volume is also directionless: every contract traded has a buyer and a seller,
so high call volume includes covered-call writers and market-maker hedges, not
just bulls. S4 treats it as ONE input alongside price confirmation, never as a
standalone buy signal.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Optional

import httpx

log = logging.getLogger("raanu.oflow")

DATA_BASE = "https://data.alpaca.markets/v1beta1/options"

# Snapshot pages cap at 1000 contracts. A single weekly expiry for one
# underlying is far below that, so one page per ticker is enough.
PAGE_LIMIT = 1000

# Concurrency against the snapshots endpoint. The universe is ~472 names and
# one sequential pass took minutes; 12 in flight brings a full scan under a
# minute without tripping rate limits.
MAX_CONCURRENT = 12
TIMEOUT_SEC = 15


# S4 is a SINGLE-STOCK signal. Index and sector ETF options are a different
# instrument with different flow: they are bought as hedges and as broad market
# expressions, so heavy call volume in QQQ or XLK says something about
# positioning, not about a company. Measured on 2026-08-12, QQQ ranked 6th in
# the universe by raw call volume while its flow was actually put-heavy (44.0%
# call share) — precisely the kind of false read this excludes.
#
# Verified against Alpaca asset names across the 472-name universe: these 17
# are every fund in it, with no false positives.
ETF_SYMBOLS = frozenset({
    "SPY", "QQQ", "IWM", "GLD", "SLV", "ARKK",
    "XLE", "XLF", "XLV", "XLI", "XLK", "XLC", "XLY", "XLP", "XLRE", "XLB", "XLU",
})

# Safety net for names added later. Deliberately does NOT match "Shares" or
# "Ordinary" — those appear in ordinary-share and ADR names (Eaton, Linde,
# Shopify, ARM, NIO) and an earlier version wrongly excluded eight real
# companies because of it.
_FUND_NAME_RE = re.compile(
    r"(\bETF\b|\bSPDR\b|\biShares\b|Select Sector|\bTrust\b|\bIndex Fund\b|\bETN\b)",
    re.I,
)


def is_fund(ticker: str, name: str = "") -> bool:
    """True for index/sector ETFs and other funds — excluded from S4."""
    return ticker.upper() in ETF_SYMBOLS or bool(name and _FUND_NAME_RE.search(name))


def stock_only(tickers: list[str]) -> list[str]:
    """Drop funds, keeping single stocks. Names come from the scanner's cache."""
    try:
        from scanner import get_ticker_name
        return [t for t in tickers if not is_fund(t, get_ticker_name(t))]
    except Exception:
        return [t for t in tickers if not is_fund(t)]


def _headers() -> dict:
    return {
        "APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", "").strip(),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", "").strip(),
    }


def _feed() -> str:
    """opra when the agreement is signed, indicative otherwise."""
    return os.getenv("ALPACA_OPTIONS_FEED", "indicative").strip().lower()


def weekly_expiry(today: Optional[date] = None) -> date:
    """The nearest standard weekly expiry — the coming Friday.

    On Friday itself the same day is returned: contracts still trade until the
    close, and same-day flow is the freshest signal there is. On Saturday and
    Sunday the next Friday is returned, so a weekend scan looks forward rather
    than at a chain that has already settled.
    """
    d = today or datetime.now().date()
    days_ahead = (4 - d.weekday()) % 7          # 4 = Friday
    return d + timedelta(days=days_ahead)


def _classify(symbol: str) -> Optional[str]:
    """'call' / 'put' from an OCC symbol: ROOT + YYMMDD + C|P + strike."""
    for i, ch in enumerate(symbol):
        if ch.isdigit():
            # date starts here; the type letter follows the 6 date digits
            t = symbol[i + 6:i + 7].upper()
            return {"C": "call", "P": "put"}.get(t)
    return None


async def _flow_one(client: httpx.AsyncClient, sem: asyncio.Semaphore,
                    ticker: str, expiry: date) -> Optional[dict]:
    """Total call/put volume for one underlying at one expiry."""
    async with sem:
        try:
            r = await client.get(
                f"{DATA_BASE}/snapshots/{ticker}",
                headers=_headers(),
                params={
                    "feed": _feed(),
                    "limit": str(PAGE_LIMIT),
                    "expiration_date": expiry.isoformat(),
                },
                timeout=TIMEOUT_SEC,
            )
        except Exception as e:
            log.debug(f"[S4] {ticker} snapshot failed: {e}")
            return None

    if r.status_code == 403:
        # Surfaced loudly once by scan_call_flow(); a silent skip here would
        # look like "no options activity anywhere" rather than "wrong feed".
        return {"ticker": ticker, "error": "forbidden"}
    if r.status_code != 200:
        return None

    snaps = (r.json() or {}).get("snapshots") or {}
    call_vol = put_vol = 0
    top_strike_vol = 0
    top_strike = None

    for sym, snap in snaps.items():
        kind = _classify(sym)
        if not kind:
            continue
        v = int((snap.get("dailyBar") or {}).get("v") or 0)
        if kind == "call":
            call_vol += v
            if v > top_strike_vol:
                top_strike_vol, top_strike = v, sym
        else:
            put_vol += v

    total = call_vol + put_vol
    if total <= 0:
        return None

    return {
        "ticker": ticker,
        "expiry": expiry.isoformat(),
        "call_vol": call_vol,
        "put_vol": put_vol,
        "total_vol": total,
        "call_share": round(call_vol / total, 4),
        "call_put_ratio": round(call_vol / put_vol, 2) if put_vol else None,
        "contracts": len(snaps),
        "top_call_contract": top_strike,
        "top_call_vol": top_strike_vol,
        "feed": _feed(),
    }


async def scan_call_flow_async(tickers: list[str],
                               expiry: Optional[date] = None) -> dict[str, dict]:
    exp = expiry or weekly_expiry()
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *(_flow_one(client, sem, t, exp) for t in tickers),
            return_exceptions=True,
        )

    out: dict[str, dict] = {}
    forbidden = 0
    for r in results:
        if isinstance(r, dict):
            if r.get("error") == "forbidden":
                forbidden += 1
                continue
            out[r["ticker"]] = r

    if forbidden:
        log.warning(
            f"[S4] {forbidden} tickers returned 403 on feed '{_feed()}' — "
            "OPRA agreement likely unsigned. Set ALPACA_OPTIONS_FEED=indicative."
        )
    log.info(f"[S4] Option flow: {len(out)}/{len(tickers)} underlyings with "
             f"{exp.isoformat()} activity (feed={_feed()})")
    return out


def scan_call_flow(tickers: list[str], expiry: Optional[date] = None) -> dict[str, dict]:
    """Blocking wrapper — the scanner runs in a thread pool executor."""
    return asyncio.run(scan_call_flow_async(tickers, expiry))
