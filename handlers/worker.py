"""
handlers.worker — Lambda entrypoint for everything on a schedule
=================================================================
Three jobs, dispatched by event shape:

  * ``{"task": "scan_shard", ...}``  one slice of a fan-out scan
  * ``{"task": "scan", ...}``        a whole scan in this invocation
  * anything else                    the periodic heartbeat: run whatever
                                     ET time-slot work is now due

Time-based dispatch rather than one EventBridge rule per slot: classic
EventBridge cron is UTC-only, so a hardcoded expression silently drifts an
hour across DST twice a year. The ET logic the app already has stays the
single source of truth for *when* things run; the schedule only has to fire
often enough.
"""

from __future__ import annotations

import asyncio
import logging

from raanu.secrets import load_ssm_secrets

load_ssm_secrets()

from raanu import state  # noqa: E402
from raanu.clock import US_EAST  # noqa: E402

log = logging.getLogger("raanu.handlers.worker")
logging.getLogger().setLevel(logging.INFO)

MARKS_KEY = "scheduler_marks.json"

_seeded = False


def _seed_once() -> None:
    """Reconcile seeded trade history before any job runs, so the weekly
    limit and Kelly see the full log from this container's first
    invocation."""
    global _seeded
    if _seeded:
        return
    from raanu.trading.schedule import set_seed_result
    from raanu.trading.trader import seed_tradelog_from_env
    try:
        set_seed_result(seed_tradelog_from_env())
    except Exception as e:
        log.exception(f"Trade log seeding failed: {e}")
    _seeded = True


async def _run_due_jobs() -> None:
    from datetime import datetime

    from raanu.trading import schedule
    from raanu.trading.exits import run_monitor_once

    now = datetime.now(US_EAST)
    today = now.strftime("%Y-%m-%d")
    is_weekday = now.weekday() < 5
    marks = state.load(MARKS_KEY, default={})
    changed = False

    # Pre-market: 03:30 ET, alert only, never orders.
    hour, minute = schedule._PREMARKET_ET
    if (is_weekday and now >= now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            and marks.get("premarket") != today):
        log.info("[worker] pre-market scan")
        try:
            await schedule._premarket_scan_and_notify()
            from raanu.trading import picks_log
            await asyncio.to_thread(picks_log.fill_forward_returns)
        except Exception as e:
            log.exception(f"[worker] pre-market scan failed: {e}")
        marks["premarket"] = today
        changed = True

    # Execution slots: 09:35 and 11:00 ET.
    for hour, minute, orders_allowed, label in schedule._ET_SLOTS:
        key = f"trade_{hour:02d}{minute:02d}"
        if not (is_weekday
                and now >= now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                and marks.get(key) != today):
            continue
        log.info(f"[worker] trade slot {label}")
        # S3 first: the only strategy profitable in both halves of the
        # backtest, so any rounding edge falls its way.
        for strat in ("s3", "s1", "s2"):
            try:
                await schedule._execute_scheduled_trades(orders_allowed, label, strategy=strat)
            except Exception as e:
                log.exception(f"[worker] slot {label}/{strat} failed: {e}")
        marks[key] = today
        changed = True

    if changed:
        state.save(MARKS_KEY, marks)

    # Exit checks run every invocation — already self-gated on market-open.
    try:
        await run_monitor_once()
    except Exception as e:
        log.exception(f"[worker] exit monitor failed: {e}")


def handler(event, context):
    from raanu.scanning import job

    event = event or {}
    task = event.get("task")

    # One shard of a fan-out scan. Checked first and returned immediately:
    # a shard must never also run the time-based work.
    if task == "scan_shard":
        job.run_shard(event["run_id"], event["index"], event["tickers"])
        return {"ok": True, "shard": event["index"]}

    _seed_once()

    if task == "scan":
        job.run_inline(job.start_run(mode=event.get("mode", "cheap")))
        return {"ok": True}

    asyncio.run(_run_due_jobs())
    return {"ok": True}
