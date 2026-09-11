"""
raanu.settings — trading limits you can change without a deploy
===============================================================
The handful of numbers that bound what the bot may spend, stored in DynamoDB
so a UI can edit them later, with the environment and the code defaults behind
them.

    stored value  ->  environment variable  ->  code default

**Why not just env vars.** Everything else in ``raanu.config`` is a deploy-time
decision. These three are not: the weekly allowance is the thing an owner
actually wants to turn down on a bad week or up on a good one, and making that
a CDK change plus a three-minute deploy means it does not happen.

Four properties, each of which exists because the alternative is dangerous:

  * **Every setting has BOUNDS, enforced on write AND on read.** A UI field
    that accepts ``20000000`` is a foot-gun, and a corrupted row must not be
    able to authorise spending that no human chose. Reading clamps and logs
    rather than trusting what is in the table.
  * **Reads are cached** (``_TTL`` seconds). ``weekly_budget_usd()`` is called
    from the status endpoint the dashboard polls; a DynamoDB read per call
    would put the table on a hot path for a value that changes monthly.
  * **A read failure falls back to the LAST KNOWN GOOD value**, not to the
    default. If the owner has turned the budget down and the table blips, the
    default is the *higher* number — reverting to it on an error would quietly
    undo a deliberate restriction.
  * **Unknown names are rejected.** A typo in a UI form must not create a
    setting nothing reads, which looks like it worked and does nothing.

⚠️ The cache is per process, so on Lambda two containers can disagree for up
to ``_TTL`` after a change. That is acceptable for limits that bound a weekly
budget; it would not be for anything decided per order.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("raanu.settings")

# Long enough to keep the table off the dashboard's poll path, short enough
# that a change made in a UI shows up while the person is still looking at it.
_TTL = 60.0


@dataclass(frozen=True)
class Spec:
    """One editable setting: how to read it, and what it may not exceed."""

    kind: type            # int or float
    default: Any
    env: str              # the variable that still works, for deploy-time use
    low: float
    high: float
    help: str

    def coerce(self, value: Any) -> Any:
        """Parse and CLAMP. Out of range is clamped, not rejected, on read —
        a bad row must not take the bot down, but it must not be obeyed."""
        parsed = self.kind(float(value))
        if parsed < self.low or parsed > self.high:
            clamped = self.kind(min(max(parsed, self.low), self.high))
            log.warning(f"[settings] {self.env}={parsed} outside "
                        f"[{self.low}, {self.high}] — using {clamped}")
            return clamped
        return parsed


SPECS: dict[str, Spec] = {
    "weekly_budget_usd": Spec(
        float, 20000.0, "WEEKLY_BUDGET_USD", 0.0, 1_000_000.0,
        "Dollars of new BUY notional per rolling 7 days, pooled."),
    "weekly_trade_limit": Spec(
        int, 10, "WEEKLY_TRADE_LIMIT", 0, 100,
        "Trades per rolling 7 days, pooled across every strategy."),
    "per_trade_max_usd": Spec(
        float, 2000.0, "PER_TRADE_MAX_USD", 0.0, 1_000_000.0,
        "Hard ceiling on a single order, before Kelly sizing."),
}

# name -> (value, fetched_at). Survives for the life of the container.
_cache: dict[str, tuple[Any, float]] = {}


def reset() -> None:
    """Drop the cache. Called by the tests and after a write."""
    _cache.clear()


def _from_store(name: str):
    from raanu import state
    from raanu.state import keys
    row = state.get(keys.SETTING, keys.setting_sk(name), default=None)
    if not isinstance(row, dict):
        return None
    return row.get("value")


def get(name: str):
    """The live value of one setting.

    Never raises: a setting that cannot be read is worth a warning, not a
    dead slot. What it falls back TO is the careful part — see the module
    docstring on why the last known good value beats the default.
    """
    spec = SPECS.get(name)
    if spec is None:
        raise KeyError(f"unknown setting {name!r}")

    cached = _cache.get(name)
    if cached is not None and time.monotonic() - cached[1] < _TTL:
        return cached[0]

    try:
        raw = _from_store(name)
    except Exception as e:
        if cached is not None:
            # Last known good, NOT the default: the default may be the more
            # permissive number, and an error must not undo a restriction.
            log.warning(f"[settings] {name} unreadable ({e}) — keeping {cached[0]}")
            return cached[0]
        log.warning(f"[settings] {name} unreadable ({e}) — falling back to env/default")
        raw = None

    if raw is None:
        raw = os.getenv(spec.env)
    if raw is None or raw == "":
        value = spec.default
    else:
        try:
            value = spec.coerce(raw)
        except (TypeError, ValueError):
            log.warning(f"[settings] {name}={raw!r} is not a {spec.kind.__name__} "
                        f"— using {spec.default}")
            value = spec.default

    _cache[name] = (value, time.monotonic())
    return value


def put(name: str, value: Any) -> Any:
    """Store one setting. Returns what was actually stored, after clamping.

    Writes one item per name so a concurrent change to another setting cannot
    be clobbered — on Lambda the API and the worker are separate functions and
    a single settings blob would lose updates the same way the trade log did.
    """
    spec = SPECS.get(name)
    if spec is None:
        raise KeyError(f"unknown setting {name!r}")
    stored = spec.coerce(value)

    from raanu import state
    from raanu.state import keys
    state.put(keys.SETTING, keys.setting_sk(name),
              {"value": stored, "updated_at": _now()})
    _cache[name] = (stored, time.monotonic())
    log.info(f"[settings] {name} = {stored}")
    return stored


def clear(name: str) -> None:
    """Remove the stored override so env/default applies again."""
    if name not in SPECS:
        raise KeyError(f"unknown setting {name!r}")
    from raanu import state
    from raanu.state import keys
    state.delete(keys.SETTING, keys.setting_sk(name))
    _cache.pop(name, None)


def snapshot() -> dict:
    """Every setting, its live value, and where that value came from.

    ``source`` is the point: "why is the budget 20,000" should be answerable
    without guessing whether a UI change actually landed.
    """
    from raanu import state
    from raanu.state import keys

    out = {}
    for name, spec in SPECS.items():
        try:
            stored = state.get(keys.SETTING, keys.setting_sk(name), default=None)
        except Exception:
            stored = None
        if isinstance(stored, dict) and stored.get("value") is not None:
            source, updated = "store", stored.get("updated_at")
        elif os.getenv(spec.env):
            source, updated = "env", None
        else:
            source, updated = "default", None
        out[name] = {
            "value": get(name), "source": source, "updated_at": updated,
            "default": spec.default, "min": spec.low, "max": spec.high,
            "env": spec.env, "help": spec.help,
        }
    return out


def _now() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat()
