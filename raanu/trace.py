"""
raanu.trace — the decision journal
===================================
Why did the bot do that? ``picks_log`` answers *"was the pick good?"*; this
answers *"why did we act on it?"*. Neither alone can explain a bad week — the
outcome without the reasoning is unattributable, and the reasoning without the
outcome is unfalsifiable.

Every meaningful state change in the trading pipeline emits one row: the scan
finishing, the actionable filter dropping a ticker, a gate blocking, an order
being sized and placed, an exit firing. A week later ``window(7)`` hands the
whole chain back — to a human debugging "why did nothing trade on Tuesday", or
to ``raanu.ai.retro`` asking what actually drove the week.

**Sort key is ``{day}#{ts}#{event}#{uid}``**, so a date range is a bounded
query rather than a table scan. That is the entire storage design.

Three rules this module keeps:

  * **It can never break a trade.** Every public function swallows its own
    exceptions. A journal that can take down the thing it observes is worse
    than no journal.
  * **It emits on state change, never per evaluation.** The exit monitor runs
    ~78 times a day per position; tracing every pass would bury the signal and
    cost real storage. "What changed", not "what was checked".
  * **Rows expire themselves.** ``TRACE_RETAIN_DAYS`` sets a TTL on every
    write. Nothing else prunes them, so without it the table grows forever.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from raanu import config, state
from raanu.state import keys

log = logging.getLogger("raanu.trace")

# Values longer than this are truncated before storage. DynamoDB caps an item
# at 400KB and `state.put` raises above its own limit; a trace row that fails
# to write is a trace row that is not there when you need it, so bound the
# inputs rather than discover the ceiling in production.
_MAX_FIELD_CHARS = 4000


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _truncate(value):
    """Bound anything unbounded, preserving type where it matters."""
    if isinstance(value, str) and len(value) > _MAX_FIELD_CHARS:
        return value[:_MAX_FIELD_CHARS] + f"…[+{len(value) - _MAX_FIELD_CHARS} chars]"
    if isinstance(value, (list, tuple)):
        return [_truncate(v) for v in value[:100]]
    if isinstance(value, dict):
        return {k: _truncate(v) for k, v in value.items()}
    return value


def emit(event: str, *, slot: str = "", strategy: str = "", **fields) -> None:
    """Record one pipeline event. Best-effort and silent on failure.

    ``event`` is a dotted name (``scan.done``, ``order.sized``, ``exit.fired``)
    so traces group naturally when read back.
    """
    if not config.trace_enabled():
        return
    try:
        day = _today()
        row = {
            "ts": keys.now_stamp(),
            "day": day,
            "event": event,
            "slot": slot,
            "strategy": strategy,
            **{k: _truncate(v) for k, v in fields.items()},
        }
        state.put(
            keys.TRACE,
            keys.trace_sk(day, row["ts"], event),
            row,
            ttl_seconds=config.trace_retain_days() * 86400,
        )
    except Exception as e:
        # Deliberately not re-raised and deliberately not logged at error:
        # a failed trace must not turn into noise that masks a real problem.
        log.warning(f"[trace] {event} not recorded: {e}")


def for_day(day: str) -> list[dict]:
    """Every event on one ET-agnostic UTC day, oldest first."""
    try:
        return [r.data for r in state.query(keys.TRACE,
                                            sk_prefix=keys.trace_day_prefix(day))]
    except Exception as e:
        log.warning(f"[trace] read failed for {day}: {e}")
        return []


def window(days: int = 7) -> list[dict]:
    """Every event in the last ``days`` days, oldest first.

    One range query on the sort key, not a scan — ``sk_gte``/``sk_lte`` bound
    it at the database rather than filtering after the fact.
    """
    try:
        today = datetime.now(UTC).date()
        start = (today - timedelta(days=max(0, days - 1))).isoformat()
        # "~" sorts after every character the key builder can produce, so the
        # upper bound covers all of today without needing tomorrow's date.
        return [r.data for r in state.query(
            keys.TRACE, sk_gte=f"{start}#", sk_lte=f"{today.isoformat()}#~")]
    except Exception as e:
        log.warning(f"[trace] window read failed: {e}")
        return []


def summarise(days: int = 7) -> dict:
    """Counts by event and by day — the cheap overview before reading detail."""
    rows = window(days)
    by_event: dict[str, int] = {}
    by_day: dict[str, int] = {}
    for r in rows:
        by_event[r.get("event", "?")] = by_event.get(r.get("event", "?"), 0) + 1
        by_day[r.get("day", "?")] = by_day.get(r.get("day", "?"), 0) + 1
    return {"days": days, "total": len(rows), "by_event": by_event, "by_day": by_day}
