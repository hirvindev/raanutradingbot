"""
raanu.ai.context.budget — what the week has left to spend
==========================================================
The pooled weekly allowance, and the single source of truth for whether a
trade may be placed at all.

**Two limits, not one.** Seven trades and $7,000 per rolling 7 days. The count
alone does not bound risk — seven $5,000 trades and seven $200 trades are the
same number and a 25x difference in exposure — and the dollars alone do not
bound how thinly the book is spread. Whichever runs out first stops the week.

**Rolling, not calendar.** The window is the last 7 days from now, so budget
drips back one trade at a time as each ages out, rather than arriving in a
lump every Monday and inviting the whole allowance to be spent by lunchtime.
This also matches what ``trades_in_last_7_days`` already did.

🔴 **This provider is ``required``.** Every other one degrades to a smaller
picture; this one degrades to "we do not know what we are allowed to spend",
and the only safe reading of that is zero. It raises rather than returning a
default, which routes it into the advisor's single ``except`` and out as "no
orders this slot" — the same fail-closed path as a timeout.

⚠️ Counts **BUY notional only**. Exits do not refund the budget: it limits how
much NEW exposure a week opens, not the net position. The same reasoning
already applies to the trade count — ``trades_in_last_7_days(action="BUY")``
exists because a closed round-trip was once consuming the opening budget, and
a single exit locked a strategy out for a week.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from raanu import config

log = logging.getLogger("raanu.ai.context.budget")

name = "budget"
required = True


def _buys_this_week() -> list[dict]:
    """The week's BUYs, or an exception.

    🔴 ``strict=True`` is the whole fail-closed contract. The state layer
    normally swallows a read failure and returns what it got, which for a
    query means ``[]`` — indistinguishable from "nothing was bought this
    week", i.e. the FULL weekly allowance available. On an already-spent week
    that is the most dangerous answer available, and a DynamoDB blip would be
    enough to produce it.

    Caught live on 11 Sep 2026: a failing query reported a clean 0/7 trades
    and $0/$7,000 used against a week that had six trades and $15,229 in it.
    """
    from raanu.trading.trader import get_trader
    return get_trader().tradelog.trades_in_last_7_days(action="BUY", strict=True)


def _notional_of(trade: dict) -> float:
    """What this BUY committed, in dollars.

    ``notional_usd`` is what the order path records. Falls back to qty x entry
    for older rows and for any path that recorded a share count instead — a
    missing value must read as 0 rather than crash the budget read, but it
    should be rare enough to notice, hence the log line.
    """
    value = trade.get("notional_usd")
    if value is not None:
        try:
            return abs(float(value))
        except (TypeError, ValueError):
            pass
    try:
        qty = float(trade.get("qty") or 0)
        price = float(trade.get("entry_price") or 0)
        if qty and price:
            return abs(qty * price)
    except (TypeError, ValueError):
        pass
    log.warning(f"[budget] BUY with no notional: {trade.get('ticker')} "
                f"{trade.get('timestamp')}")
    return 0.0


def state() -> dict:
    """The pool, as numbers. Used by the gate AND shown to the advisor.

    One function for both so the model is never told it has capacity the
    executor will then refuse — the class of drift that made the stop used at
    entry differ from the stop used at exit.
    """
    buys = _buys_this_week()
    max_trades = config.weekly_trade_limit()
    max_usd = config.weekly_budget_usd()

    used_usd = sum(_notional_of(t) for t in buys)
    by_strategy: dict[str, int] = {}
    for trade in buys:
        key = (trade.get("strategy") or "unknown").lower()
        by_strategy[key] = by_strategy.get(key, 0) + 1

    trades_left = max(0, max_trades - len(buys))
    usd_left = max(0.0, max_usd - used_usd)

    # When the oldest BUY ages out, that slot and its dollars come back. The
    # advisor uses this to decide between spending now and waiting: "one trade
    # left and three more return on Tuesday" is a different situation from
    # "one trade left and nothing returns for six days".
    frees_at = None
    if buys:
        oldest = min(buys, key=lambda t: t.get("timestamp") or "")
        stamp = oldest.get("timestamp")
        if stamp:
            try:
                frees_at = (datetime.fromisoformat(stamp) + timedelta(days=7)).isoformat()
            except ValueError:
                frees_at = None

    return {
        "window": "rolling 7 days",
        "trades_used": len(buys),
        "trades_max": max_trades,
        "trades_left": trades_left,
        "usd_used": round(used_usd, 2),
        "usd_max": round(max_usd, 2),
        "usd_left": round(usd_left, 2),
        "min_trade_usd": config.weekly_min_trade_usd(),
        "trades_by_strategy_this_week": by_strategy,
        "oldest_frees_at": frees_at,
        "exhausted": trades_left <= 0 or usd_left < config.weekly_min_trade_usd(),
    }


async def fetch() -> dict:
    """Pool state plus the account figures that bound it in practice.

    The account read is best-effort: without it the advisor still knows the
    weekly pool, which is the binding constraint by design. ``held`` is the
    one position detail carried here rather than left to the positions tool,
    because it is capacity information — a name already held cannot be bought
    again, so ranking it wastes a slot.
    """
    out = await asyncio.to_thread(state)
    try:
        from raanu.trading.trader import get_free_cash, get_held_symbols
        free_cash, held = await asyncio.gather(get_free_cash(), get_held_symbols())
        if free_cash is not None:
            out["free_cash"] = round(float(free_cash), 2)
            # Money that is not there is a harder limit than any policy.
            out["spendable_now"] = round(min(out["usd_left"], float(free_cash)), 2)
        if held is not None:
            out["already_held"] = sorted(held)
    except Exception as e:
        log.warning(f"[budget] account detail unavailable: {e}")
    return out
