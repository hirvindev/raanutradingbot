"""raanu.api.routes.orders"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import AliasChoices, BaseModel, Field

from raanu.api.routes.account import _annotate_names, _strategy_resolver
from raanu.market.rest import alpaca_delete, alpaca_get, alpaca_post
from raanu.trading.trader import get_trader


class OrderRequest(BaseModel):
    """A manual order.

    The handlers used to read ``order.notional``, ``order.quantity`` and
    ``order.strategy`` — none of which this model has ever declared. Pydantic
    raises AttributeError for a missing field, so **both /api/orders/buy and
    /api/orders/sell returned 500 on every single request**, including the
    dashboard's sell button. Nothing caught it because the route-walk test
    only exercises GETs.

    The aliases exist because three spellings are already in the wild: the
    dashboard posts ``quantity``, this model says ``qty``, and Alpaca's own
    field is ``notional``. Accepting all of them is cheaper than a coordinated
    rename across a client that ships separately from the API.
    """

    ticker: str
    usd: float | None = Field(
        default=None, validation_alias=AliasChoices("usd", "notional"))
    qty: float | None = Field(
        default=None, validation_alias=AliasChoices("qty", "quantity"))
    # Manual orders are tagged "manual", never s1/s2/s3 — the weekly limit
    # counts BUY entries per strategy, so mislabelling one silently spends the
    # auto-trader's budget for the week.
    strategy: str | None = None


def _order_body(order: OrderRequest, side: str) -> dict:
    body = {
        "symbol":        order.ticker.upper(),
        "side":          side,
        "type":          "market",
        "time_in_force": "day",
    }
    if order.usd:
        body["notional"] = str(round(order.usd, 2))
    elif order.qty:
        # abs(): a short position reports a negative qty, and Alpaca wants the
        # size with the direction in `side`.
        body["qty"] = str(abs(order.qty))
    else:
        # Previously this fell through and posted the string "None" as qty,
        # which Alpaca rejects with a message about nothing in particular.
        raise HTTPException(
            status_code=400,
            detail="give either usd (notional) or qty",
        )
    return body

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
    result = await alpaca_post("/orders", _order_body(order, "buy"))

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
                "usd":      round(order.usd, 2) if order.usd else None,
                "qty":      order.qty,
                "source":   "manual-api",
                "order_id": result.get("id"),
            })
    except Exception as e:
        log.error(f"Order placed but trade log write failed for {order.ticker}: {e}")

    return result


@router.post("/api/orders/sell")
async def place_sell(order: OrderRequest):
    return await alpaca_post("/orders", _order_body(order, "sell"))


@router.delete("/api/orders/{order_id}")
async def cancel_order(order_id: str):
    return await alpaca_delete(f"/orders/{order_id}")
