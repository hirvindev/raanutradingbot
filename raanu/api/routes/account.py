"""raanu.api.routes.account"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter

from raanu import config
from raanu.market.rest import alpaca_get, alpaca_headers
from raanu.trading.trader import get_trader

_asset_name_cache: dict[str, str] = {}

log = logging.getLogger("raanu.api.routes.account")

router = APIRouter()


@router.get("/api/account/cash")
async def account_cash():
    """Account balances — mapped to the shape the dashboard expects."""
    acct = await alpaca_get("/account")
    total = float(acct.get("portfolio_value", 0))

    # Alpaca's /account has NO unrealized_pl field — it only exists per position.
    # Sum it across open positions, otherwise the dashboard shows a flat $0.00.
    open_pnl = 0.0
    try:
        for p in await alpaca_get("/positions"):
            open_pnl += float(p.get("unrealized_pl", 0) or 0)
    except Exception:
        log.warning("Could not fetch positions for open P&L")

    # Use portfolio history for daily P&L — includes realized gains from sells today.
    # last_equity comparison is unreliable on paper accounts (often returns 0).
    daily_ppl = 0.0
    try:
        hist = await alpaca_get(
            "/account/portfolio/history",
            params={"period": "1D", "timeframe": "15Min", "extended_hours": "true"},
        )
        pl_arr = hist.get("profit_loss", [])
        if pl_arr:
            daily_ppl = float(pl_arr[-1] or 0)
    except Exception:
        last_eq   = float(acct.get("last_equity", total))
        daily_ppl = total - last_eq

    # Cash still sitting in unfilled buy orders is spoken for, not free.
    from raanu.trading.trader import get_free_cash
    cash      = float(acct.get("cash", 0))
    free      = await get_free_cash()
    if free is None:
        free = cash
    committed = max(0.0, cash - free)

    return {
        "total":     total,
        "free":      free,
        "invested":  total - cash,
        "ppl":       open_pnl,
        "daily_ppl": daily_ppl,
        "blocked":   committed,
        "currency":  acct.get("currency", "USD"),
        "_raw":      acct,
    }


@router.get("/api/account/info")
async def account_info():
    return await alpaca_get("/account")


_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(?:\.(\d+))?(.*)$")


def _parse_ts(v) -> datetime | None:
    """
    Parse an ISO timestamp to an aware UTC datetime, or None.

    Python 3.9's fromisoformat accepts fractional seconds only at exactly 3 or
    6 digits, and rejects a trailing 'Z'. Alpaca sends both other widths (e.g.
    '...T13:32:53.92223Z', 5 digits) and 'Z'. Parsing those raised, this
    returned None, and the caller then silently fell back to "latest BUY" —
    which is precisely the mis-attribution this function exists to prevent.
    Normalise the fraction to 6 digits before parsing.
    """
    if not v:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    m = _TS_RE.match(str(v).strip())
    if not m:
        return None
    head, frac, tail = m.group(1), m.group(2) or "0", m.group(3) or ""
    tail = tail.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        ts = datetime.fromisoformat(f"{head}.{frac[:6].ljust(6, '0')}{tail}")
    except Exception:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _strategy_resolver():
    """
    Return `resolve(symbol, when=None) -> strategy`.

    Attribution used to be a plain ticker -> strategy dict built by looping the
    trade log, so the LAST BUY for a ticker overwrote every earlier one. A
    ticker traded by two strategies at different times had its whole history —
    including the older strategy's closed round-trips — relabelled with the
    newer one. JAZZ is a live example: bought by S2 on 2026-07-23 and by S1 on
    2026-07-29, so S2's round-trip was being counted as an S1 result.

    Now each BUY keeps its own timestamp and an order is attributed to the most
    recent BUY at or before it. `when=None` (an open position) still means "the
    latest BUY", which is correct for the lot currently held.
    """
    buys: dict[str, list[tuple[datetime | None, str]]] = {}
    for t in get_trader().tradelog.data.get("trades", []):
        if t.get("action") == "BUY" and t.get("ticker"):
            buys.setdefault(t["ticker"].upper(), []).append(
                (_parse_ts(t.get("timestamp")), t.get("strategy", "s1"))
            )
    for sym in buys:
        buys[sym].sort(key=lambda x: (x[0] is None, x[0]))

    def resolve(symbol: str, when=None) -> str:
        entries = buys.get((symbol or "").upper())
        if not entries:
            return ""            # unattributed — NOT s1
        when = _parse_ts(when) if not isinstance(when, datetime) else when
        if when is None:
            return entries[-1][1]
        prior = [s for ts, s in entries if ts is None or ts <= when]
        # An order before any logged BUY belongs to the earliest known one
        # (clock skew between Alpaca's fill time and our log write).
        return prior[-1] if prior else entries[0][1]

    return resolve


@router.get("/api/portfolio")
async def portfolio():
    """Open positions, tagged with strategy and company name."""
    positions = await alpaca_get("/positions")
    resolve_strat = _strategy_resolver()

    uncached = [p.get("symbol", "").upper() for p in positions if p.get("symbol", "").upper() not in _asset_name_cache]
    if uncached:
        async with httpx.AsyncClient(timeout=10) as client:
            for sym in uncached:
                try:
                    r = await client.get(f"{config.broker_base()}/assets/{sym}", headers=alpaca_headers())
                    if r.status_code == 200:
                        _asset_name_cache[sym] = r.json().get("name", sym)
                except Exception:
                    _asset_name_cache[sym] = sym

    out = []
    for p in positions:
        sym = p.get("symbol", "").upper()
        out.append({
            "ticker":        sym,
            "name":          _asset_name_cache.get(sym, sym),
            "quantity":      float(p.get("qty", 0)),
            "averagePrice":  float(p.get("avg_entry_price", 0)),
            "currentPrice":  float(p.get("current_price", 0)),
            "ppl":           float(p.get("unrealized_pl", 0)),
            "fxPpl":         0,
            "initialFill":   p.get("asset_id"),
            # "" (not "s1") when the log has no BUY for this symbol — an
            # unattributed position is unknown, not an S1 trade.
            "strategy":      resolve_strat(sym),
            "_raw":          p,
        })
    return out


async def _annotate_names(rows: list) -> list:
    """Attach `name` to any row carrying a `symbol`.

    Alpaca's order payload has no company name, so the dashboard's Instrument
    column could only ever show a bare ticker. Names come from the same
    process-cached /v2/assets map the scanner uses, so the cost is one fetch per
    process rather than one per row — but that first fetch is blocking httpx,
    hence the thread.
    """
    def _work():
        from raanu.market.universe import get_ticker_name
        for r in rows:
            sym = r.get("symbol")
            if sym:
                r["name"] = get_ticker_name(sym)
        return rows
    try:
        return await asyncio.to_thread(_work)
    except Exception as e:
        log.warning(f"Could not attach company names: {e}")
        return rows
