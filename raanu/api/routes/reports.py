"""raanu.api.routes.reports"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from raanu.trading.reports import build_monthly_report, format_monthly_report

log = logging.getLogger("raanu.api.routes.reports")

router = APIRouter()


@router.get("/api/report/monthly")
async def monthly_report(year: int | None = None, month: int | None = None):
    """Monthly per-strategy comparison as JSON (used by the dashboard)."""
    return await build_monthly_report(year, month)


@router.post("/api/report/monthly/send")
async def monthly_report_send(year: int | None = None, month: int | None = None):
    """Build and push the monthly report to Telegram now."""
    from raanu.notify.telegram import send_telegram
    rep = await build_monthly_report(year, month)
    ok = send_telegram(format_monthly_report(rep), strategy="s1")
    return {"sent": ok, "period": rep["period"], "trades": rep["total_trades"]}
