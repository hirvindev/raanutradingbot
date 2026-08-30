"""raanu.api.routes.auto"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from raanu import config
from raanu.market.universe import TEST_UNIVERSE
from raanu.scanning.engine import top_picks
from raanu.trading.schedule import (
    _load_picks,
    _run_scan_and_cache,
    _run_scan_and_cache_s2,
    _save_picks,
    _save_picks_s2,
)
from raanu.trading.trader import get_trader

log = logging.getLogger("raanu.api.routes.auto")

router = APIRouter()


# ---------- AUTO-TRADER CONTROL ----------
@router.get("/api/auto/status")
def auto_status():
    return get_trader().status()


@router.post("/api/auto/start")
def auto_start():
    if not config.alpaca_key():
        raise HTTPException(status_code=400, detail="ALPACA_API_KEY is not configured.")
    get_trader().enabled = True
    get_trader().event("control", "Auto-trader ENABLED")
    return {"enabled": True}


@router.post("/api/auto/stop")
def auto_stop():
    get_trader().enabled = False
    get_trader().event("control", "Auto-trader DISABLED")
    return {"enabled": False}


@router.post("/api/auto/scan-now")
async def auto_scan_now(force: bool = False):
    """Force an immediate scan + cache refresh.
    ?force=true  — bypass market-hours gate and limit to 5 stocks (test mode)."""
    if force:
        log.info("TEST MODE — scanning 5 stocks only, market hours bypassed")
        picks = top_picks('s1', limit=3, tickers=TEST_UNIVERSE[:5])
        _save_picks(picks)
    else:
        picks = await _run_scan_and_cache()
    await get_trader().run_one_cycle(picks=picks, force_market_open=force)
    return {"picks": len(picks), "forced": force, **get_trader().status()}


@router.get("/api/auto/scan-preview")
async def auto_scan_preview():
    """Return cached picks without running a new scan."""
    cached = _load_picks()
    if not cached:
        return {"message": "No scan results yet — scan runs at 07:00 and 18:00 Berlin time", "picks": []}
    return {
        "scanned_at": cached["scanned_at"],
        "min_score":  config.min_signal_score(),
        "picks":      cached["picks"],
        "actionable": [p for p in cached["picks"] if p.get("score", 0) >= config.min_signal_score() and p.get("uptrend") and p.get("ticker")],
    }


@router.post("/api/auto/scan-now/s2")
async def auto_scan_now_s2(force: bool = False):
    """Force an immediate S2 scan."""
    if force:
        log.info("[S2] TEST MODE — scanning 5 stocks only")
        picks = top_picks('s2', limit=3, tickers=TEST_UNIVERSE[:5])
        _save_picks_s2(picks)
    else:
        picks = await _run_scan_and_cache_s2()
    return {"picks": len(picks), "strategy": "s2", "forced": force}
