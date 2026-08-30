"""raanu.api.routes.scan"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from raanu import config
from raanu.clock import BERLIN
from raanu.market import exchanges
from raanu.scanning import job
from raanu.scanning.engine import top_picks
from raanu.trading.schedule import _load_picks, _load_picks_s2, _save_picks, _save_picks_s2

log = logging.getLogger("raanu.api.routes.scan")

router = APIRouter()


# ---------- XETRA/GETTEX SCANNER ----------
@router.get("/api/scan/top3")
async def scan_top3():
    """
    Return the latest XETRA/GETTEX picks instantly from cache.
    Cache is refreshed automatically at 07:00 and 18:00 Berlin time,
    and immediately on server startup.
    """
    from raanu.market.universe import get_universe_summary
    cached = _load_picks()
    if cached:
        return {
            "picks":       cached["picks"],
            "scanned_at":  cached["scanned_at"],
            "from_cache":  True,
            "universe":    get_universe_summary(),
            "count":       len(cached["picks"]),
        }
    # First startup before cache exists — run live (happens once)
    picks = top_picks('s1', limit=3)
    _save_picks(picks)
    return {
        "picks":      picks,
        "scanned_at": datetime.now(BERLIN).isoformat(),
        "from_cache": False,
        "universe":   get_universe_summary(),
        "count":      len(picks),
    }


@router.post("/api/scan/alert-now")
async def scan_alert_now():
    """Trigger morning alerts for both strategies immediately (for testing)."""
    from raanu.notify.telegram import format_daily_alert, send_whatsapp

    cached_s1 = _load_picks()
    picks_s1  = cached_s1["picks"] if cached_s1 else []
    if not picks_s1:
        picks_s1 = top_picks('s1', limit=3)
        _save_picks(picks_s1)

    cached_s2 = _load_picks_s2()
    picks_s2  = cached_s2["picks"] if cached_s2 else []
    if not picks_s2:
        picks_s2 = top_picks('s2', limit=3)
        _save_picks_s2(picks_s2)

    msg_s1 = format_daily_alert(picks_s1, strategy="s1")
    msg_s2 = format_daily_alert(picks_s2, strategy="s2")
    ok1 = send_whatsapp(msg_s1, strategy="s1")
    ok2 = send_whatsapp(msg_s2, strategy="s2")
    return {"sent_s1": ok1, "sent_s2": ok2, "picks_s1": len(picks_s1), "picks_s2": len(picks_s2)}


# ---------- SCAN JOB ----------
# A full scan takes ~87s cold. Nothing can hold an HTTP request open that
# long here (CloudFront's Function URL origin timeout caps at 60s without an
# AWS quota increase, and Mangum buffers the whole response anyway), so a
# scan is a job: started asynchronously, polled for progress.
@router.get("/api/scan/universes")
async def scan_universes():
    """What the dashboard's universe dropdown offers.

    Curated first — it is the default. The exchange-wide entries are the
    deliberate exception, and their counts are shown so it is obvious that
    picking "Nasdaq" means 5,581 tickers rather than 470.
    """
    return {"universes": exchanges.catalog(), "default": exchanges.CURATED}


@router.post("/api/scan/job")
async def scan_job_start(mode: str = "fast", universe: str = exchanges.CURATED):
    """Start a scan.

    ``universe`` selects what to scan: ``curated`` (the default 470-name
    list), an exchange key from /api/scan/universes, or ``all``.

    ``fast`` fans out across worker invocations for someone watching a
    progress bar; ``cheap`` runs it in one invocation for the scheduled
    slots, where nobody is. Both run the identical engine — and with the
    daily bars cache warm, either finishes in seconds.
    """
    if not config.worker_function_name():
        return JSONResponse(
            {"error": "WORKER_FUNCTION_NAME not set — this endpoint requires the worker Lambda"},
            status_code=501,
        )
    if job.status().get("status") == "running":
        # Re-entrancy guard: a double-click must not fan out twice.
        return {"status": "already_running"}

    manifest = job.start_run(mode="cheap" if mode == "cheap" else "fast",
                             universe_key=universe)
    job.dispatch(manifest)
    return {"status": "started", "run_id": manifest["run_id"],
            "shards": manifest["shards"], "mode": manifest["mode"],
            "universe": manifest["universe"], "total": manifest["total"]}


@router.get("/api/scan/job")
async def scan_job_status():
    """Merged progress across every shard. Poll target for the dashboard."""
    return job.status()
