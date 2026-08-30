"""
raanu.state — persistent JSON state, backend-agnostic
======================================================
Callers use ``load``/``save``/``load_many``/``delete`` and never learn which
backend is active. ``STATE_BACKEND=dynamodb`` selects DynamoDB (AWS);
anything else uses local files.

The backend is resolved per call rather than captured once, so a test can
switch backends with ``monkeypatch.setenv`` without reimporting anything.
"""

from __future__ import annotations

from raanu import config
from raanu.state.backends import (
    DynamoBackend,
    FileBackend,
    reset_data_dir_cache,
    resolve_data_dir,
)

_backend = None
_backend_kind: tuple[str, str] | None = None


def _active():
    """Return the backend for the current environment, rebuilding it only
    when the relevant env actually changed."""
    global _backend, _backend_kind
    kind = (config.state_backend(), config.state_table())
    if _backend is None or _backend_kind != kind:
        _backend = DynamoBackend(kind[1]) if kind[0] == "dynamodb" else FileBackend(resolve_data_dir())
        _backend_kind = kind
    return _backend


def load(key: str, default=None):
    return _active().load(key, default)


def load_many(keys) -> dict:
    """Read many keys at once. On DynamoDB this is a single BatchGetItem."""
    return _active().load_many(keys)


def save(key: str, obj, ttl_seconds: int | None = None) -> None:
    _active().save(key, obj, ttl_seconds=ttl_seconds)


def delete(key: str) -> None:
    _active().delete(key)


def reset() -> None:
    """Drop cached backend + data-dir resolution. Tests only."""
    global _backend, _backend_kind
    _backend = None
    _backend_kind = None
    reset_data_dir_cache()


__all__ = ["load", "load_many", "save", "delete", "reset", "resolve_data_dir"]
