"""raanu.api.routes.exits"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

log = logging.getLogger("raanu.api.routes.exits")

router = APIRouter()


@router.get("/api/exit-config")
def get_exit_cfg():
    from raanu.trading.exits import get_exit_config
    return get_exit_config()


@router.post("/api/exit-config")
async def save_exit_cfg(request: Request):
    from raanu.trading.exits import update_exit_config
    body = await request.json()
    updated = update_exit_config(body)
    return {"ok": True, "config": updated}
