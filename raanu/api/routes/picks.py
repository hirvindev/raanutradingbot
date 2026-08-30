"""raanu.api.routes.picks"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter

log = logging.getLogger("raanu.api.routes.picks")

router = APIRouter()


@router.get("/api/picks/outcomes")
async def picks_outcomes(limit: int = 40):
    """What the bot picked, and what those names actually did afterwards.

    Separate from the trade log on purpose: most picks are never bought, so
    judging the scoring engines by trades alone only ever measures the subset
    that survived the weekly limit, the cash share and the already-held check.
    """
    from raanu.trading import picks_log
    return {"summary": picks_log.summary(), "recent": picks_log.recent(limit)}


@router.post("/api/picks/backfill")
async def picks_backfill():
    """Force the forward-return fill instead of waiting for 03:30 ET."""
    from raanu.trading import picks_log
    return await asyncio.to_thread(picks_log.fill_forward_returns)
