"""
raanu.api.app — the FastAPI application
========================================
An app *factory*, not a module-level singleton. Building the app inside a
function means importing this module has no side effects, so tests can
construct an app against whatever config they set up rather than inheriting
whatever the environment happened to hold at import time.

Background loops (pre-market scan, trade slots, exit monitor, monthly
report) run **only when explicitly asked for**, via ``create_app(
with_loops=True)``. That is for local development, where a persistent
process exists to run them in. On Lambda the worker handles all four on its
own schedule, and Mangum runs with ``lifespan="off"`` so nothing here starts
at all.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from raanu import config
from raanu.api import auth
from raanu.api.routes import (
    account,
    auto,
    exits,
    health,
    notify,
    orders,
    picks,
    push,
    reports,
    scan,
    static,
    stocks,
    strategy,
    webhooks,
)
from raanu.paths import DOTENV

log = logging.getLogger("raanu.api")

# static must be last: it owns "/" and the PWA asset routes, and registering
# it before the /api routers would let its catch-alls shadow them.
_ROUTERS = (
    auth.router, health.router, account.router, orders.router, auto.router, scan.router,
    push.router, picks.router, strategy.router, reports.router, notify.router,
    exits.router, webhooks.router, stocks.router, static.router,
)


def create_app(with_loops: bool = False) -> FastAPI:
    app = FastAPI(title="RaanuTradingBot", version="2.0",
                  lifespan=_lifespan if with_loops else None)

    # A wildcard origin with tokens in play would let any page the user
    # visits read their account.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.allowed_origins(),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.middleware("http")(auth.api_auth_gate)

    for router in _ROUTERS:
        app.include_router(router)
    return app


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Local-development only — see the module docstring."""
    from raanu.trading.schedule import set_seed_result
    from raanu.trading.trader import seed_tradelog_from_env

    # Reconcile seeded history BEFORE the loops start, so the weekly limit
    # and Kelly see the full trade log on the very first scan.
    try:
        set_seed_result(seed_tradelog_from_env())
    except Exception as e:
        log.exception(f"Trade log seeding failed: {e}")
        set_seed_result({"error": str(e)})

    from raanu.trading.exits import monitor_loop
    tasks = [
        asyncio.create_task(_premarket_loop()),
        asyncio.create_task(_scheduled_trade_loop()),
        asyncio.create_task(monitor_loop()),
        asyncio.create_task(_monthly_report_loop()),
    ]
    yield
    for task in tasks:
        task.cancel()


async def _premarket_loop():
    """Sleep until 03:30 ET, scan, repeat. Local development only."""
    from datetime import datetime, timedelta

    from raanu.clock import US_EAST
    from raanu.trading.schedule import _PREMARKET_ET, _premarket_scan_and_notify

    while True:
        now = datetime.now(US_EAST)
        target = now.replace(hour=_PREMARKET_ET[0], minute=_PREMARKET_ET[1],
                             second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        while target.weekday() >= 5:      # Sat/Sun — the market is shut
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            await _premarket_scan_and_notify()
        except Exception as e:
            log.exception(f"Pre-market scan error: {e}")
        try:
            from raanu.trading import picks_log
            log.info(f"[picks] outcomes: {await asyncio.to_thread(picks_log.fill_forward_returns)}")
        except Exception as e:
            log.warning(f"[picks] backfill skipped: {e}")


async def _scheduled_trade_loop():
    """Sleep until the next execution slot, trade, repeat. Local dev only."""
    from datetime import datetime, timedelta

    from raanu.clock import US_EAST
    from raanu.trading.schedule import _ET_SLOTS, _execute_scheduled_trades

    while True:
        now = datetime.now(US_EAST)
        targets = []
        for hour, minute, orders_allowed, label in _ET_SLOTS:
            slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if now >= slot:
                slot += timedelta(days=1)
            while slot.weekday() >= 5:
                slot += timedelta(days=1)
            targets.append((slot, orders_allowed, label))
        targets.sort(key=lambda t: t[0])
        slot, orders_allowed, label = targets[0]
        await asyncio.sleep((slot - now).total_seconds())
        try:
            # S3 first: it is the only strategy profitable in both halves of
            # the backtest, so any rounding edge falls its way.
            for strat in ("s3", "s1", "s2"):
                await _execute_scheduled_trades(orders_allowed, label, strategy=strat)
        except Exception as e:
            log.exception(f"Scheduled slot error [{label}]: {e}")


async def _monthly_report_loop():
    """09:00 Berlin on the 1st. Local development only."""
    from datetime import datetime, timedelta

    from raanu.clock import BERLIN
    from raanu.trading.reports import build_monthly_report, format_monthly_report

    while True:
        now = datetime.now(BERLIN)
        target = now.replace(day=1, hour=9, minute=0, second=0, microsecond=0)
        if now >= target:
            target = (target + timedelta(days=32)).replace(day=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            from raanu.notify.telegram import send_telegram
            send_telegram(format_monthly_report(await build_monthly_report()))
        except Exception as e:
            log.exception(f"Monthly report failed: {e}")


def _load_dotenv_for_local_dev() -> None:
    """Read ``.env`` into the environment. Local dev only.

    This did not exist until 31 Aug 2026, and its absence was invisible in the
    worst way: ``.env`` was *documented* as where local config lives, and the
    file was already being read — but only ``.exists()``, as a marker for "am
    I running locally" in state/backends.py and health.py. Nothing ever loaded
    the contents. So a developer who put API_READ_TOKEN in ``.env`` got an
    UNAUTHENTICATED local server, because an unset token disables the gate.
    Configured-looking and wide open is the one combination worth ruling out.

    Deliberately called from ``main()`` and nowhere else:

      * not at import, because raanu.config exists precisely so that nothing
        reads the environment at import time;
      * not in ``create_app()``, because the tests call that, and conftest
        clears the environment specifically so a developer's real
        ALPACA_API_KEY cannot leak into a test run;
      * not on Lambda, which has no ``.env`` — SSM populates os.environ there
        (raanu/secrets.py).

    ``override=False`` matches the SSM loader: a variable already exported in
    the shell wins over the file, so a one-off `API_READ_TOKEN=x python -m
    raanu.api` behaves the way anyone would expect.
    """
    if not DOTENV.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        log.warning("%s exists but python-dotenv is not installed — ignoring it", DOTENV)
        return
    load_dotenv(DOTENV, override=False)
    log.info("Loaded local config from %s", DOTENV)


def main() -> None:
    """Local development entrypoint: ``python -m raanu.api``."""
    import uvicorn

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    _load_dotenv_for_local_dev()
    if not config.api_read_token():
        # The gate is skipped when this is unset. Fine on a laptop, but it
        # should be a thing you know, not a thing you discover.
        log.warning("API_READ_TOKEN is not set — this server is UNAUTHENTICATED")
    uvicorn.run(create_app(with_loops=True), host="0.0.0.0",
                port=config.env_int("PORT", 8000))
