"""
worker_handler.py — AWS Lambda entrypoint for scheduled trading logic
========================================================================
Invoked every 5 minutes by a single EventBridge rule (see
aws/stacks/skeleton_stack.py — created DISABLED until explicitly enabled).
Replaces server.py's three infinite asyncio loops (_premarket_loop,
_scheduled_trade_loop, profit_monitor.monitor_loop): Lambda has no
persistent process to sleep inside between invocations, so this checks
"has each target ET time passed today, and did I already run it" using
small idempotency markers in the state store, then does whichever of the
three jobs are due this invocation.

Deliberately time-based, not event-payload-based: classic EventBridge Rule
cron is UTC-only with no timezone parameter, so a hardcoded cron would
silently drift an hour across DST twice a year. Reusing server.py's own
already-correct US_EAST/ZoneInfo logic here avoids that instead of pushing
DST math into infra config.
"""

import asyncio
import logging
from datetime import datetime

from lambda_secrets import load_ssm_secrets

load_ssm_secrets()

from datadir import state_load, state_save

log = logging.getLogger("raanu.worker")

MARKS_KEY = "scheduler_marks.json"


def _already_ran(marks: dict, slot: str, today: str) -> bool:
    return marks.get(slot) == today


async def _run_due_jobs():
    import server
    from profit_monitor import run_monitor_once

    now = datetime.now(server.US_EAST)
    today = now.strftime("%Y-%m-%d")
    is_weekday = now.weekday() < 5  # Sat=5, Sun=6 — market is shut
    marks = state_load(MARKS_KEY, default={})
    changed = False

    # Pre-market slot — 03:30 ET, weekdays, alert-only (no orders).
    h, m = server._PREMARKET_ET
    premarket_t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if is_weekday and now >= premarket_t and not _already_ran(marks, "premarket", today):
        log.info("[worker] running pre-market scan")
        try:
            await server._premarket_scan_and_notify()
            import picks_log
            try:
                await asyncio.to_thread(picks_log.fill_forward_returns)
            except Exception as e:
                log.warning(f"[picks] backfill skipped: {e}")
        except Exception as e:
            log.exception(f"[worker] pre-market scan error: {e}")
        marks["premarket"] = today
        changed = True

    # Scheduled trade slots — 09:35 and 11:00 ET, weekdays.
    for h, m, n_orders, label in server._ET_SLOTS:
        slot_key = f"trade_{h:02d}{m:02d}"
        slot_t = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if is_weekday and now >= slot_t and not _already_ran(marks, slot_key, today):
            log.info(f"[worker] running trade slot {label}")
            # S3 first: it is the only strategy profitable in both halves of
            # the backtest, so any leftover edge should fall its way.
            for strat in ("s3", "s1", "s2"):
                try:
                    await server._execute_scheduled_trades(n_orders, label, strategy=strat)
                except Exception as e:
                    log.exception(f"[worker] scheduled slot error [{label}/{strat}]: {e}")
            marks[slot_key] = today
            changed = True

    if changed:
        state_save(MARKS_KEY, marks)

    # Profit monitor — every invocation; already self-gated on market-open.
    try:
        await run_monitor_once()
    except Exception as e:
        log.exception(f"[worker] profit monitor error: {e}")


_seeded = False


def handler(event, context):
    global _seeded
    if not _seeded:
        # Reconcile any seeded history before the first job runs, so the
        # weekly limit and Kelly see the full trade log from this
        # container's very first invocation — mirrors server.py's lifespan.
        from auto_trader import seed_tradelog_from_env
        try:
            seed_tradelog_from_env()
        except Exception as e:
            log.exception(f"[worker] trade log seeding failed: {e}")
        _seeded = True

    asyncio.run(_run_due_jobs())
    return {"ok": True}
