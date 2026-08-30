"""
raanu.state.backends — where persistent JSON state physically lives
====================================================================
Two backends behind one interface:

  * ``FileBackend``   — local development. Resolution order is
    ``$DATA_DIR`` -> project dir (when a ``.env`` is present) -> ``/tmp``
    with a warning, carried over from the original ``datadir.py``.
  * ``DynamoBackend`` — AWS. Lambda's filesystem does not survive between
    invocations, and three things break silently when state is lost:
    strategy attribution (round-trips are tagged from the ticker's BUY
    entry), the weekly trade limit (a wiped log re-arms the bot), and
    kelly's 30-trade minimum sample (it never graduates off fallback risk).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from raanu import config
from raanu.paths import DOTENV, PROJECT_ROOT

log = logging.getLogger("raanu.state")

# DynamoDB caps BatchGetItem at 100 keys per request.
_BATCH_GET_LIMIT = 100


class FileBackend:
    def __init__(self, directory: Path):
        self.dir = directory

    def _path(self, key: str) -> Path:
        # Keys are namespaced with "/" (e.g. "scan/<run>/shard/3"), which is a
        # flat partition key on DynamoDB but a directory tree on a filesystem.
        path = self.dir / key
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def load(self, key, default=None):
        path = self._path(key)
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text())
        except Exception:
            log.warning(f"State {key} unreadable — using default")
            return default

    def load_many(self, keys):
        return {k: v for k in keys if (v := self.load(k)) is not None}

    def save(self, key, obj, ttl_seconds=None):
        # ttl_seconds is meaningful only to DynamoDB. Locally these are real
        # files a developer can inspect, and silently deleting them would be
        # more surprising than leaving them.
        try:
            self._path(key).write_text(json.dumps(obj, indent=2, default=str))
        except Exception as e:
            log.error(f"Failed to write state {key}: {e}")

    def delete(self, key):
        try:
            self._path(key).unlink(missing_ok=True)
        except Exception as e:
            log.warning(f"Failed to delete state {key}: {e}")


class DynamoBackend:
    def __init__(self, table_name: str):
        self.table_name = table_name
        self._table = None
        self._resource = None

    @property
    def table(self):
        # boto3 is imported here, not at module scope, so local development
        # and the test suite never need it installed.
        if self._table is None:
            import boto3
            self._resource = boto3.resource("dynamodb")
            self._table = self._resource.Table(self.table_name)
        return self._table

    def load(self, key, default=None):
        try:
            item = self.table.get_item(Key={"state_key": key}).get("Item")
            return json.loads(item["data"]) if item else default
        except Exception as e:
            log.warning(f"DynamoDB state {key} unreadable — using default: {e}")
            return default

    def load_many(self, keys):
        """One round trip for up to 100 keys, instead of N GetItems.

        This is what makes polling a sharded scan cheap: the aggregate view
        reads every shard's progress in a single call.
        """
        keys = list(keys)
        if not keys:
            return {}
        out: dict[str, object] = {}
        try:
            import boto3
            client = boto3.resource("dynamodb")
            for start in range(0, len(keys), _BATCH_GET_LIMIT):
                request = {
                    self.table_name: {
                        "Keys": [{"state_key": k} for k in keys[start:start + _BATCH_GET_LIMIT]]
                    }
                }
                # DynamoDB may return UnprocessedKeys under throttling; it is
                # the caller's job to retry them, not the service's.
                for _ in range(4):
                    response = client.batch_get_item(RequestItems=request)
                    for item in response.get("Responses", {}).get(self.table_name, []):
                        try:
                            out[item["state_key"]] = json.loads(item["data"])
                        except Exception:
                            log.warning(f"Unparseable state item {item.get('state_key')}")
                    request = response.get("UnprocessedKeys") or {}
                    if not request:
                        break
        except Exception as e:
            log.warning(f"DynamoDB batch read failed: {e}")
        return out

    def save(self, key, obj, ttl_seconds=None):
        try:
            item = {"state_key": key, "data": json.dumps(obj, default=str)}
            if ttl_seconds:
                # Per-run scan shards would otherwise accumulate forever.
                item["ttl"] = int(time.time()) + int(ttl_seconds)
            self.table.put_item(Item=item)
        except Exception as e:
            log.error(f"Failed to write DynamoDB state {key}: {e}")

    def delete(self, key):
        try:
            self.table.delete_item(Key={"state_key": key})
        except Exception as e:
            log.warning(f"Failed to delete DynamoDB state {key}: {e}")


_resolved_dir: Path | None = None


def resolve_data_dir() -> Path:
    """Directory for state that must survive a restart. Cached per process."""
    global _resolved_dir
    if _resolved_dir is not None:
        return _resolved_dir

    override = config.data_dir_override()
    if override:
        path = Path(override)
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write-test"
            probe.write_text("ok")
            probe.unlink()
            log.info(f"State directory: {path} (configured volume)")
            _resolved_dir = path
            return _resolved_dir
        except Exception as e:
            log.error(f"DATA_DIR={override} is not writable ({e}) — falling back")

    # A .env beside the project marks a developer's checkout.
    if DOTENV.exists():
        log.info(f"State directory: {PROJECT_ROOT} (local project dir)")
        _resolved_dir = PROJECT_ROOT
        return _resolved_dir

    log.warning(
        "State directory: /tmp — EPHEMERAL. The trade log will be lost on "
        "restart, which breaks strategy attribution, the weekly trade limit "
        "and Kelly's sample. Set DATA_DIR, or STATE_BACKEND=dynamodb."
    )
    _resolved_dir = Path("/tmp")
    return _resolved_dir


def reset_data_dir_cache() -> None:
    """Tests point DATA_DIR at a tmp_path per test; without this the first
    resolution would stick for the whole session."""
    global _resolved_dir
    _resolved_dir = None
