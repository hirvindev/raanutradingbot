"""raanu.api.routes.trace"""

from __future__ import annotations

import logging

from fastapi import APIRouter

log = logging.getLogger("raanu.api.routes.trace")

router = APIRouter()


@router.get("/api/trace")
async def read_trace(days: int = 7, event: str = "", strategy: str = ""):
    """The decision journal — why the bot did what it did.

    Answers questions the logs make expensive: "why did nothing trade on
    Tuesday", "what did the advisor actually see", "what were every one of the
    inputs to that $412 notional". ``picks_log`` records whether a pick was
    *good*; this records why it was *acted on*.

    GET only, so the trade PIN never applies — the read passphrase is enough,
    and nothing here can move money.
    """
    from raanu import trace

    rows = trace.window(max(1, min(days, 90)))
    if event:
        rows = [r for r in rows if str(r.get("event", "")).startswith(event)]
    if strategy:
        rows = [r for r in rows if r.get("strategy") == strategy]
    return {
        "days": days,
        "count": len(rows),
        "summary": trace.summarise(max(1, min(days, 90))),
        "events": rows,
    }
