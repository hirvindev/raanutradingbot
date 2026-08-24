"""
datadir.py — one place that decides where persistent state lives
================================================================
`trades_log.json`, `position_peaks.json` and the picks caches are not scratch
files. Three things depend on the trade log surviving a restart:

  * strategy attribution — round-trips are tagged by looking up the ticker's
    BUY entry, so an empty log makes every closed trade report as "s1"
  * WEEKLY_TRADE_LIMIT   — a wiped log re-arms the bot to trade again
  * kelly.py MIN_SAMPLE  — the 30-trade gate never graduates off the fallback
    risk if the sample keeps resetting

This used to be computed independently in server.py, auto_trader.py and
profit_monitor.py, all falling back to /tmp on the cloud. Railway wipes /tmp on
every redeploy, so all three broke silently on each deploy. Resolution order:

  1. $DATA_DIR            — an explicitly mounted persistent volume
  2. the project dir      — local dev, identified by a .env file
  3. /tmp                 — last resort, logs a warning that it is ephemeral
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("raanu.datadir")

_PROJECT_DIR = Path(__file__).parent
_resolved: Path | None = None


def data_dir() -> Path:
    """Directory for state that must survive a restart. Cached per process."""
    global _resolved
    if _resolved is not None:
        return _resolved

    env_dir = os.getenv("DATA_DIR", "").strip()
    if env_dir:
        p = Path(env_dir)
        try:
            p.mkdir(parents=True, exist_ok=True)
            probe = p / ".write-test"
            probe.write_text("ok")
            probe.unlink()
            log.info(f"State directory: {p} (persistent volume)")
            _resolved = p
            return _resolved
        except Exception as e:
            log.error(f"DATA_DIR={env_dir} is not writable ({e}) — falling back")

    if (_PROJECT_DIR / ".env").exists():
        log.info(f"State directory: {_PROJECT_DIR} (local project dir)")
        _resolved = _PROJECT_DIR
        return _resolved

    log.warning(
        "State directory: /tmp — EPHEMERAL. The trade log will be lost on "
        "restart, which breaks strategy attribution, the weekly trade limit "
        "and Kelly's sample. Mount a volume and set DATA_DIR to it."
    )
    _resolved = Path("/tmp")
    return _resolved


def state_path(filename: str) -> Path:
    """Absolute path for one piece of persistent state."""
    return data_dir() / filename


# ---------- state_load / state_save ----------
# Every module that persists a JSON blob (trades_log.json, position_peaks.json,
# picks_log.json, push_subs.json, push_native.json, notifications.json,
# last_picks*.json) goes through these two functions instead of touching
# state_path()/read_text()/write_text() directly. STATE_BACKEND defaults to
# "file" — Railway and local dev never set it, so their behavior is exactly
# what it was before. It is set to "dynamodb" only in the Lambda environment,
# where the local filesystem does not persist between invocations.
def state_load(filename: str, default=None):
    """Load one piece of persistent JSON state, whichever backend is active."""
    if os.getenv("STATE_BACKEND", "file") == "dynamodb":
        return _dynamo_load(filename, default)
    p = state_path(filename)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            log.warning(f"State file {filename} unreadable — using default")
    return default


def state_save(filename: str, obj) -> None:
    """Persist one piece of JSON state, whichever backend is active."""
    if os.getenv("STATE_BACKEND", "file") == "dynamodb":
        _dynamo_save(filename, obj)
        return
    try:
        state_path(filename).write_text(json.dumps(obj, indent=2, default=str))
    except Exception as e:
        log.error(f"Failed to write state {filename}: {e}")


_dynamo_table = None


def _dynamo_table_resource():
    """Lazily create the boto3 DynamoDB table resource. boto3 is only ever
    imported here, so Railway/local dev never needs it installed — it's
    provided by the Lambda base image, the only place STATE_BACKEND=dynamodb
    is ever set."""
    global _dynamo_table
    if _dynamo_table is None:
        import boto3
        _dynamo_table = boto3.resource("dynamodb").Table(os.environ["STATE_TABLE"])
    return _dynamo_table


def _dynamo_load(filename: str, default):
    try:
        item = _dynamo_table_resource().get_item(Key={"state_key": filename}).get("Item")
        if item:
            return json.loads(item["data"])
    except Exception as e:
        log.warning(f"DynamoDB state {filename} unreadable — using default: {e}")
    return default


def _dynamo_save(filename: str, obj) -> None:
    try:
        _dynamo_table_resource().put_item(Item={
            "state_key": filename,
            "data": json.dumps(obj, indent=2, default=str),
        })
    except Exception as e:
        log.error(f"Failed to write DynamoDB state {filename}: {e}")
