"""raanu.api.routes.schedule — enable/disable the EventBridge worker schedule"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from raanu.trading import schedule_rule

router = APIRouter()


@router.get("/api/schedule/status")
def schedule_status():
    """Whether the AWS schedule that fires scheduled scans/trades is
    configured at all, and if so, on or off. Separate from AUTO ON — that
    only governs whether a running slot may place orders."""
    return schedule_rule.status()


@router.post("/api/schedule/enable")
def schedule_enable():
    try:
        return schedule_rule.set_enabled(True)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=501)


@router.post("/api/schedule/disable")
def schedule_disable():
    try:
        return schedule_rule.set_enabled(False)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=501)
