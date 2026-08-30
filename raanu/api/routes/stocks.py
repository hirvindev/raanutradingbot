"""raanu.api.routes.stocks"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

log = logging.getLogger("raanu.api.routes.stocks")

router = APIRouter()


# ---------- STOCK SEARCH / MARKET DATA (Alpaca) ----------
@router.get("/api/stocks/search")
async def stocks_search(q: str = ""):
    """Search tradable US equities by symbol or name."""
    from raanu.market import broker as alpaca_data
    if not q.strip():
        raise HTTPException(status_code=400, detail="Provide ?q=<symbol or name>")
    results = alpaca_data.search_assets(q.strip(), limit=15)
    return {"query": q, "results": results, "source": "alpaca"}


@router.get("/api/stocks/active")
async def stocks_most_active(top: int = 20):
    """Top stocks by volume today."""
    from raanu.market import broker as alpaca_data
    data = alpaca_data.get_most_active(top=min(top, 50))
    return {"most_active": data, "source": "alpaca"}


@router.get("/api/stocks/movers")
async def stocks_market_movers(top: int = 10):
    """Top daily gainers and losers."""
    from raanu.market import broker as alpaca_data
    data = alpaca_data.get_market_movers(top=min(top, 25))
    return {"gainers": data.get("gainers", []), "losers": data.get("losers", []), "source": "alpaca"}
