"""raanu.api.routes.orders"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from raanu.api.routes.account import _annotate_names, _strategy_resolver
from raanu.market.rest import alpaca_delete, alpaca_get, alpaca_post
from raanu.trading.trader import get_trader


class OrderRequest(BaseModel):
    ticker: str
    usd: float | None = None
    qty: float | None = None

log = logging.getLogger("raanu.api.routes.orders")

router = APIRouter()


@router.get("/api/orders")
async def orders():
    """Open/pending orders."""
    return await _annotate_names(
        await alpaca_get("/orders", params={"status": "open", "limit": 100})
    )


@router.get("/api/history/orders")
async def history_orders(limit: int = 50):
    """Closed orders (filled, cancelled, expired), tagged with strategy."""
    orders = await alpaca_get("/orders", params={"status": "closed", "limit": min(limit, 500)})
    resolve_strat = _strategy_resolver()
    for o in orders:
        # Attributed as of the order's own time — see _strategy_resolver().
        o["strategy"] = resolve_strat(o.get("symbol"),
                                      o.get("filled_at") or o.get("created_at"))
    return await _annotate_names(orders)


@router.post("/api/orders/buy")
async def place_buy(order: OrderRequest):
    body: dict = {
        "symbol":        order.ticker.upper(),
        "side":          "buy",
        "type":          "market",
        "time_in_force": "day",
    }
    if order.notional:
        body["notional"] = str(round(order.notional, 2))
    else:
        body["qty"] = str(order.quantity)
    result = await alpaca_post("/orders", body)

    # Record the BUY, or the position is permanently unattributable. This
    # endpoint backs the Live Signals "Execute" button and never wrote to the
    # trade log, so every hand-placed buy showed as Untagged forever, was
    # invisible to strategy stats, and never reached Kelly's sample.
    #
    # Manual buys are tagged "manual", NOT s1/s2/s3, deliberately: the weekly
    # limit counts BUY entries per strategy, so tagging a hand-placed order as
    # s1 would silently consume the auto-trader's budget for the week. Pass an
    # explicit `strategy` to override.
    try:
        if isinstance(result, dict) and result.get("id"):
            get_trader().tradelog.record({
                "action":   "BUY",
                "ticker":   order.ticker.upper(),
                "strategy": (order.strategy or "manual").lower(),
                "usd":      round(order.notional, 2) if order.notional else None,
                "qty":      order.quantity,
                "source":   "manual-api",
                "order_id": result.get("id"),
            })
    except Exception as e:
        log.error(f"Order placed but trade log write failed for {order.ticker}: {e}")

    return result


@router.post("/api/orders/sell")
async def place_sell(order: OrderRequest):
    body: dict = {
        "symbol":        order.ticker.upper(),
        "side":          "sell",
        "type":          "market",
        "time_in_force": "day",
    }
    if order.notional:
        body["notional"] = str(round(order.notional, 2))
    else:
        body["qty"] = str(abs(order.quantity))
    return await alpaca_post("/orders", body)


@router.delete("/api/orders/{order_id}")
async def cancel_order(order_id: str):
    return await alpaca_delete(f"/orders/{order_id}")
