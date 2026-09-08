"""
raanu.state.keys — the entity key scheme
=========================================
Every item in the table is ``pk`` + ``sk`` + ``data`` (+ optional ``ttl``).
``pk`` names the entity; ``sk`` identifies the record within it.

**Sort keys are lexicographically chronological where order matters.** Several
consumers depend on that — ``exits.py`` walks trades in reverse to find the
opening BUY, ``picks_log.summary()`` reads ``rows[0]`` and ``rows[-1]`` for the
date span. Timestamps therefore use a fixed-width format: the default
``datetime.isoformat()`` *drops* the microseconds when they happen to be zero,
which changes the string width and breaks the ordering. ``timespec=
"microseconds"`` pins it.

The ``#{uid}`` suffix on time-keyed entities makes same-microsecond collisions
impossible — two writes in the same microsecond would otherwise silently
overwrite one another, which is exactly the class of data loss this whole model
exists to remove.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

# ── entities (pk values) ─────────────────────────────────────────────────────
TRADE = "TRADE"       # one item per trade — unbounded, was trades_log.json
PICK = "PICK"         # one item per pick  — unbounded, was picks_log.json
NOTIF = "NOTIF"       # one item per alert — was notifications.json, TTL'd
PEAK = "PEAK"         # one per open position — was position_peaks.json
PUSHSUB = "PUSHSUB"   # one per browser — was push_subs.json
CACHE = "CACHE"       # overwrite-in-place caches — was last_picks*.json
FLAG = "FLAG"         # small switches — was auto_trader.json / scheduler_marks.json
SCAN = "SCAN"         # scan manifest + shards, TTL'd
BARS = "BARS"         # daily bars cache, TTL'd

ALL_ENTITIES = (TRADE, PICK, NOTIF, PEAK, PUSHSUB, CACHE, FLAG, SCAN, BARS)

# Entities holding one item per record, i.e. the ones that used to be a single
# growing blob. Analysis tooling iterates these; the rest are point lookups.
COLLECTIONS = (TRADE, PICK, NOTIF)


def now_stamp() -> str:
    """Fixed-width UTC timestamp, sortable as a string."""
    return datetime.now(UTC).isoformat(timespec="microseconds")


def stamp(dt: datetime) -> str:
    """Fixed-width UTC timestamp for an existing datetime (used by migration)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def _uid() -> str:
    return uuid.uuid4().hex[:6]


# ── sort-key builders ────────────────────────────────────────────────────────
def trade_sk(ts: str | None = None, uid: str | None = None) -> str:
    return f"{ts or now_stamp()}#{uid or _uid()}"


def pick_sk(date: str, strategy: str, ticker: str) -> str:
    # Natural key, not a timestamp: recording is idempotent per
    # (date, strategy, ticker), so a re-run overwrites rather than duplicating.
    return f"{date}#{strategy}#{ticker.upper()}"


def notif_sk(ts: str | None = None, uid: str | None = None) -> str:
    return f"{ts or now_stamp()}#{uid or _uid()}"


def peak_sk(symbol: str) -> str:
    return symbol.upper()


def pushsub_sk(endpoint: str) -> str:
    # Endpoints run to ~500 chars and DynamoDB caps a sort key at 1024 bytes;
    # hashing keeps it bounded and still deduplicates by endpoint.
    return hashlib.sha256(endpoint.encode()).hexdigest()[:16]


def cache_sk(name: str) -> str:
    return name


def flag_sk(name: str) -> str:
    return name


def scan_manifest_sk() -> str:
    return "current"


def scan_shard_sk(run_id: str, index: int) -> str:
    return f"{run_id}#shard#{index}"


def scan_run_prefix(run_id: str) -> str:
    """Prefix matching every shard of one run — lets the aggregator query by
    run instead of guessing how many shards to BatchGet."""
    return f"{run_id}#shard#"


def bars_sk(day: str, ticker: str) -> str:
    return f"{day}#{ticker.upper()}"
