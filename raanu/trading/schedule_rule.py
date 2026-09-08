"""
raanu.trading.schedule_rule — the EventBridge switch, from the dashboard
=========================================================================
The AUTO ON toggle (raanu.trading.trader.AutoTrader.enabled) only decides
whether a *running* scheduled slot is allowed to place orders. It says
nothing about whether the slot ever runs at all — that is a separate AWS
EventBridge rule which invokes the worker Lambda on the 03:30/09:35/11:00 ET
cron. The rule ships disabled and, before this module existed, could only be
flipped from the AWS console — which is also where a click could silently
fail to register, with nothing in the app to say so either way.

This wraps the three EventBridge calls the dashboard needs: read the rule's
current state, and flip it. boto3 is imported lazily, matching
raanu.state.backends and raanu.scanning.job — local dev has no rule to talk
to, and importing boto3 for a feature that no-ops locally would be dead
weight on every request.
"""

from __future__ import annotations

import logging

from raanu import config

log = logging.getLogger("raanu.trading.schedule_rule")


def _client():
    import boto3
    return boto3.client("events")


def status() -> dict:
    """Whether the schedule is wired up at all, and if so, its state.

    ``configured: False`` means this is local dev or the CDK stack predates
    this feature — there is no rule to check. Distinct from ``enabled: False``,
    which means the rule exists and is off.
    """
    rule_name = config.worker_schedule_rule_name()
    if not rule_name:
        return {"configured": False, "enabled": False}
    try:
        resp = _client().describe_rule(Name=rule_name)
    except Exception as e:
        log.warning(f"[schedule] could not read rule state ({e}) — reporting disabled")
        return {"configured": True, "enabled": False, "error": str(e)}
    return {"configured": True, "enabled": resp.get("State") == "ENABLED"}


def set_enabled(enabled: bool) -> dict:
    """Flip the rule. Raises if there is no rule configured — the caller
    (the API route) turns that into a 501, matching /api/scan/job's own
    WORKER_FUNCTION_NAME guard."""
    rule_name = config.worker_schedule_rule_name()
    if not rule_name:
        raise RuntimeError("WORKER_SCHEDULE_RULE_NAME not set — no schedule to toggle")
    client = _client()
    if enabled:
        client.enable_rule(Name=rule_name)
    else:
        client.disable_rule(Name=rule_name)
    log.info(f"[schedule] {rule_name} -> {'ENABLED' if enabled else 'DISABLED'}")
    return status()
