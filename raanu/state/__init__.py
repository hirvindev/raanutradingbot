"""
raanu.state — persistent state, backend-agnostic
=================================================
Callers use ``get``/``put``/``get_many``/``query``/``delete`` and never learn
which backend is active. ``STATE_BACKEND=dynamodb`` selects DynamoDB (AWS);
anything else uses local files.

Every item is ``pk`` (entity) + ``sk`` (record) + ``data``. See
``raanu/state/keys.py`` for the key scheme and ``raanu/state/backends.py`` for
why ``data`` is a native map rather than an escaped JSON string.

``query`` is the addition that made the rest of this worth doing. Before it,
answering "which trades happened in the last 7 days" meant loading the entire
trade log and walking it — on every ``/api/auto/status`` call, which the
dashboard polls. It is now a key-range read.

The backend is resolved per call rather than captured once, so a test can
switch backends with ``monkeypatch.setenv`` without reimporting anything.
"""

from __future__ import annotations

from dataclasses import dataclass

from raanu import config
from raanu.state.backends import (
    DynamoBackend,
    FileBackend,
    StateItemTooLarge,
    estimate_size,
    reset_data_dir_cache,
    resolve_data_dir,
)

_backend = None
_backend_kind: tuple[str, str] | None = None


@dataclass(frozen=True)
class Record:
    """One stored record. ``sk`` is carried alongside because it is the
    record's identity — callers need it to update or delete."""
    sk: str
    data: dict


def _active():
    """Return the backend for the current environment, rebuilding it only
    when the relevant env actually changed."""
    global _backend, _backend_kind
    kind = (config.state_backend(), config.state_table())
    if _backend is None or _backend_kind != kind:
        _backend = DynamoBackend(kind[1]) if kind[0] == "dynamodb" else FileBackend(resolve_data_dir())
        _backend_kind = kind
    return _backend


def get(pk: str, sk: str, default=None):
    return _active().get(pk, sk, default)


def get_many(pairs) -> dict:
    """Read many (pk, sk) pairs at once. One BatchGetItem on DynamoDB."""
    return _active().get_many(pairs)


def put(pk: str, sk: str, data, *, ttl_seconds: int | None = None) -> None:
    _active().put(pk, sk, data, ttl_seconds=ttl_seconds)


def query(pk: str, *, sk_prefix: str | None = None, sk_gte: str | None = None,
          sk_lte: str | None = None, descending: bool = False,
          limit: int | None = None, filters: dict | None = None,
          project: list[str] | None = None,
          strict: bool = False) -> list[Record]:
    """Records under one entity, in sort-key order.

    ``filters`` are equality tests against dotted paths inside ``data``
    (e.g. ``{"action": "SELL"}``). They narrow what is *returned*, not what is
    read — use ``sk_gte``/``sk_prefix`` for that, since those are key
    conditions the database can seek on.

    ``project`` limits which fields of ``data`` come back, which is worth using
    when scanning all of history for two fields.

    🔴 ``strict`` re-raises a backend failure instead of returning what was
    read so far. Use it wherever an EMPTY RESULT WOULD GRANT PERMISSION: a
    swallowed read is indistinguishable from "there is nothing there", so an
    unreadable trade log otherwise reads as "no trades this week" — the full
    weekly allowance, on a week that may already be spent.
    """
    rows = _active().query(
        pk, sk_prefix=sk_prefix, sk_gte=sk_gte, sk_lte=sk_lte,
        descending=descending, limit=limit, filters=filters, project=project,
        strict=strict)
    return [Record(sk=sk, data=data) for sk, data in rows]


def delete(pk: str, sk: str) -> None:
    _active().delete(pk, sk)


def reset() -> None:
    """Drop cached backend + data-dir resolution. Tests only."""
    global _backend, _backend_kind
    _backend = None
    _backend_kind = None
    reset_data_dir_cache()


__all__ = [
    "Record", "StateItemTooLarge", "delete", "estimate_size", "get",
    "get_many", "put", "query", "reset", "resolve_data_dir",
]
