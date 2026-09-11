"""raanu.api.routes.health"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from raanu import config
from raanu.paths import DOTENV
from raanu.state import resolve_data_dir
from raanu.trading.schedule import seed_result
from raanu.trading.trader import get_trader

log = logging.getLogger("raanu.api.routes.health")

router = APIRouter()


# ---------- API ENDPOINTS ----------
@router.get("/api/config")
def get_config():
    """Tells the dashboard the broker mode — never exposes keys."""
    return {
        "broker":         "alpaca",
        "mode":           config.alpaca_mode(),
        "key_configured": bool(config.alpaca_key()),
    }


def _state_health() -> dict:
    backend = config.state_backend()
    if backend == "dynamodb":
        return {"backend": "dynamodb", "table": config.state_table(), "persistent": True}
    return {
        "backend": "file",
        "data_dir": str(resolve_data_dir()),
        # A non-persistent state dir silently breaks strategy attribution,
        # the weekly trade limit and Kelly's minimum sample.
        "persistent": bool(config.data_dir_override()) or DOTENV.exists(),
    }


@router.get("/api/health")
def health():
    from raanu.notify.telegram import is_configured as tg_configured
    from raanu.trading.trader import per_trade_max_for as _per_trade_max_for
    # Surfaced because a non-persistent state dir silently breaks strategy
    # attribution, the weekly trade limit and Kelly's sample.

    # Also the cutover check: after migrating to the composite-key table this
    # must match the pre-migration count, and a silent zero here is exactly the
    # state that re-arms the weekly trade limit.
    try:
        _trade_count = len(get_trader().tradelog.all_trades(project=["action"]))
    except Exception:
        _trade_count = None

    _exit = config.exit_config()
    return {
        "status":         "ok",
        "broker":         "alpaca",
        "mode":           config.alpaca_mode(),
        "key_configured": bool(config.alpaca_key()),
        "telegram_configured": tg_configured(),
        "tradelog_seed": seed_result(),
        # Operational visibility only — true once WORKER_FUNCTION_NAME is
        # wired up (the AWS deployment), which is the same check
        # /api/scan/job itself makes before firing an invoke.
        "async_scan": bool(config.env_str("WORKER_FUNCTION_NAME", "").strip()),
        # Report the backend actually in use. This used to always show a
        # filesystem path, so on Lambda — where state lives in DynamoDB and
        # that path is never touched — health displayed "/tmp" and
        # "persistent: false", which reads as "your trade log is being
        # thrown away" when it is not.
        # trade_count was computed here and then never returned — dead since
        # the route split. It is worth surfacing: a silent zero is exactly the
        # state that re-arms the weekly trade limit, and it is the check that
        # confirms a state migration actually copied the log.
        "state": {**_state_health(), "trade_count": _trade_count},
        # Read through the config accessors, NOT re-derived from raw env
        # with defaults repeated here. Repeating them is exactly how this
        # endpoint came to report min_signal_score 60 while the auto-trader
        # gated at 70 — health must report the number actually enforced.
        "config": {
            "stop_loss_pct":       _exit.stop_loss_pct,
            "trail_activate_pct":  _exit.trail_activate_pct,
            "trail_pct":           _exit.trail_pct,
            "hard_take_profit_pct": _exit.hard_take_profit_pct,
            "min_signal_score":    config.min_signal_score(),
            "weekly_trade_limit":  config.weekly_trade_limit(),
            "per_trade_max_usd":   config.per_trade_max_usd(),
            "per_trade_max_by_strategy": {
                s: _per_trade_max_for(s) for s in ("s1", "s2", "s3")
            },
            # Pooled since 11 Sep 2026: one weekly allowance across every
            # strategy, in BOTH trades and dollars. Reported together because
            # the count alone does not bound risk — seven $5,000 trades and
            # seven $200 trades are the same number.
            "weekly_budget_usd":   config.weekly_budget_usd(),
            "weekly_min_trade_usd": config.weekly_min_trade_usd(),
            "profit_check_sec":    _exit.check_interval,
        },
    }
