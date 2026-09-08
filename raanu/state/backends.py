"""
raanu.state.backends — where persistent state physically lives
===============================================================
Two backends behind one interface:

  * ``FileBackend``   — local development. Resolution order is
    ``$DATA_DIR`` -> project dir (when a ``.env`` is present) -> ``/tmp``
    with a warning, carried over from the original ``datadir.py``.
  * ``DynamoBackend`` — AWS. Lambda's filesystem does not survive between
    invocations, and three things break silently when state is lost:
    strategy attribution (round-trips are tagged from the ticker's BUY
    entry), the weekly trade limit (a wiped log re-arms the bot), and
    kelly's 30-trade minimum sample (it never graduates off fallback risk).

Every item is ``pk`` + ``sk`` + ``data`` (+ optional ``ttl``). ``data`` is a
**native map**, not an escaped JSON string — see ``raanu/state/coerce.py`` for
why that needs a float<->Decimal pass and why that pass lives at this boundary.

Item-per-record is the point
----------------------------
Each collection used to be ONE item holding a growing list, which put a hard
400 KB ceiling on the trade log (~285 trades) and the picks log (~989 picks),
and made every append a whole-object read-modify-write — so the API Lambda and
the worker Lambda silently discarded each other's writes. Both problems are
properties of the item shape, not of the data volume.

The size guard below exists because the old failure was **silent**: an
oversized ``put_item`` raised inside a bare ``except`` that only logged. With
item-per-record nothing should come close to the limit, so tripping the guard
means something is genuinely wrong and it should be loud.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from urllib.parse import quote, unquote

from raanu import config
from raanu.paths import DOTENV, PROJECT_ROOT
from raanu.state.coerce import from_dynamo, to_dynamo

log = logging.getLogger("raanu.state")

# DynamoDB caps BatchGetItem at 100 keys per request.
_BATCH_GET_LIMIT = 100

# DynamoDB's hard item ceiling is 400 KB. Guard well below it: with
# item-per-record, anything approaching this is a bug worth surfacing, not a
# capacity problem worth silently tolerating.
MAX_ITEM_BYTES = 300_000


class StateItemTooLarge(Exception):
    """Raised instead of writing an item that would approach the 400 KB cap.

    Deliberately loud. The bug this replaces was a swallowed write: the trade
    log would simply stop recording, which per CLAUDE.md re-arms the weekly
    trade limit and resets Kelly's sample — the bot starts over-trading and
    nothing says so.
    """


def estimate_size(pk: str, sk: str, data) -> int:
    """Approximate DynamoDB item size: attribute names + UTF-8 value bytes."""
    def walk(o, name: str = "") -> int:
        n = len(name.encode())
        if o is None or isinstance(o, bool):
            return n + 1
        if isinstance(o, (int, float)):
            return n + 21           # DynamoDB numbers cap at 21 bytes
        if isinstance(o, str):
            return n + len(o.encode())
        if isinstance(o, dict):
            return n + 3 + sum(walk(v, k) for k, v in o.items())
        if isinstance(o, (list, tuple)):
            return n + 3 + sum(walk(v) for v in o)
        return n + len(str(o).encode())

    return len("pk") + len(pk.encode()) + len("sk") + len(sk.encode()) + walk(data, "data")


def _check_size(pk: str, sk: str, data) -> None:
    size = estimate_size(pk, sk, data)
    if size > MAX_ITEM_BYTES:
        log.error(
            "Refusing to write %s/%s — %d bytes exceeds the %d guard "
            "(DynamoDB's hard limit is 400KB). This entity should be split "
            "into one item per record.", pk, sk, size, MAX_ITEM_BYTES,
        )
        raise StateItemTooLarge(f"{pk}/{sk} is {size} bytes")


def _matches(data: dict, filters: dict | None) -> bool:
    """Equality filter over dotted paths, e.g. {'action': 'SELL'}.

    Mirrors what DynamoDB's FilterExpression does server-side, so the file
    backend answers the same question the Dynamo one does.
    """
    if not filters:
        return True
    for path, expected in filters.items():
        cur = data
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return False
            cur = cur[part]
        if cur != expected:
            return False
    return True


def _project(data: dict, project: list[str] | None) -> dict:
    """Keep only the requested dotted paths, mirroring ProjectionExpression.

    The file backend has already read the whole record by this point, so this
    saves nothing locally — it exists so both backends return the same shape.
    A caller that projects and then reads an unprojected field must fail the
    same way on a laptop as it does on Lambda.
    """
    if not project:
        return data
    out: dict = {}
    for path in project:
        parts = path.split(".")
        src, dst = data, out
        for part in parts[:-1]:
            if not isinstance(src, dict) or part not in src:
                break
            src = src[part]
            dst = dst.setdefault(part, {})
        else:
            if isinstance(src, dict) and parts[-1] in src:
                dst[parts[-1]] = src[parts[-1]]
    return out


class FileBackend:
    """Local mirror of the Dynamo layout: <dir>/<pk>/<url-quoted sk>.json

    Sort keys contain ':' and '#', so filenames are percent-encoded — losslessly
    reversible, and safe on any filesystem.
    """

    def __init__(self, directory: Path):
        self.dir = directory

    def _dir(self, pk: str) -> Path:
        path = self.dir / "state" / pk
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _path(self, pk: str, sk: str) -> Path:
        return self._dir(pk) / (quote(sk, safe="") + ".json")

    def _read(self, pk: str, sk: str) -> dict | None:
        path = self._path(pk, sk)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text())
        except Exception:
            log.warning(f"State {pk}/{sk} unreadable — treating as absent")
            return None
        # Honour TTL on read. The file backend does not sweep expired items
        # (deleting a developer's files behind their back is worse than leaving
        # them), but it must not hand back something DynamoDB would have
        # already reclaimed, or the two backends disagree.
        ttl = raw.get("ttl")
        if ttl and ttl < time.time():
            return None
        return raw

    def get(self, pk, sk, default=None):
        raw = self._read(pk, sk)
        return raw["data"] if raw else default

    def get_many(self, pairs):
        out = {}
        for pk, sk in pairs:
            raw = self._read(pk, sk)
            if raw is not None:
                out[(pk, sk)] = raw["data"]
        return out

    def put(self, pk, sk, data, ttl_seconds=None):
        _check_size(pk, sk, data)
        item = {"pk": pk, "sk": sk, "data": data}
        if ttl_seconds:
            item["ttl"] = int(time.time()) + int(ttl_seconds)
        try:
            self._path(pk, sk).write_text(json.dumps(item, indent=2, default=str))
        except Exception as e:
            log.error(f"Failed to write state {pk}/{sk}: {e}")

    def query(self, pk, *, sk_prefix=None, sk_gte=None, sk_lte=None,
              descending=False, limit=None, filters=None, project=None):
        directory = self._dir(pk)
        rows = []
        for path in directory.iterdir():
            if path.suffix != ".json":
                continue
            sk = unquote(path.stem)
            if sk_prefix and not sk.startswith(sk_prefix):
                continue
            if sk_gte and sk < sk_gte:
                continue
            if sk_lte and sk > sk_lte:
                continue
            raw = self._read(pk, sk)
            if raw is None or not _matches(raw["data"], filters):
                continue
            rows.append((sk, _project(raw["data"], project)))
        rows.sort(key=lambda r: r[0], reverse=descending)
        if limit:
            rows = rows[:limit]
        return rows

    def delete(self, pk, sk):
        try:
            self._path(pk, sk).unlink(missing_ok=True)
        except Exception as e:
            log.warning(f"Failed to delete state {pk}/{sk}: {e}")


class DynamoBackend:
    def __init__(self, table_name: str):
        self.table_name = table_name
        self._table = None

    @property
    def table(self):
        # boto3 is imported here, not at module scope, so local development
        # and the test suite never need it installed.
        if self._table is None:
            import boto3
            self._table = boto3.resource("dynamodb").Table(self.table_name)
        return self._table

    def get(self, pk, sk, default=None):
        try:
            item = self.table.get_item(Key={"pk": pk, "sk": sk}).get("Item")
            return from_dynamo(item["data"]) if item else default
        except Exception as e:
            log.warning(f"DynamoDB {pk}/{sk} unreadable — using default: {e}")
            return default

    def get_many(self, pairs):
        """One round trip per 100 keys instead of N GetItems.

        This is what keeps polling a sharded scan cheap, and what makes the
        bars cache a single read for a whole batch of tickers.
        """
        pairs = list(pairs)
        if not pairs:
            return {}
        out = {}
        try:
            import boto3
            client = boto3.resource("dynamodb")
            for start in range(0, len(pairs), _BATCH_GET_LIMIT):
                chunk = pairs[start:start + _BATCH_GET_LIMIT]
                request = {self.table_name: {
                    "Keys": [{"pk": pk, "sk": sk} for pk, sk in chunk]}}
                # DynamoDB may return UnprocessedKeys under throttling; it is
                # the caller's job to retry them, not the service's.
                for _ in range(4):
                    response = client.batch_get_item(RequestItems=request)
                    for item in response.get("Responses", {}).get(self.table_name, []):
                        out[(item["pk"], item["sk"])] = from_dynamo(item["data"])
                    request = response.get("UnprocessedKeys") or {}
                    if not request:
                        break
        except Exception as e:
            log.warning(f"DynamoDB batch read failed: {e}")
        return out

    def put(self, pk, sk, data, ttl_seconds=None):
        _check_size(pk, sk, data)
        item = {"pk": pk, "sk": sk, "data": to_dynamo(data)}
        if ttl_seconds:
            item["ttl"] = int(time.time()) + int(ttl_seconds)
        try:
            self.table.put_item(Item=item)
        except Exception as e:
            log.error(f"Failed to write DynamoDB state {pk}/{sk}: {e}")

    def query(self, pk, *, sk_prefix=None, sk_gte=None, sk_lte=None,
              descending=False, limit=None, filters=None, project=None):
        from boto3.dynamodb.conditions import Attr, Key

        cond = Key("pk").eq(pk)
        if sk_prefix:
            cond = cond & Key("sk").begins_with(sk_prefix)
        elif sk_gte and sk_lte:
            cond = cond & Key("sk").between(sk_gte, sk_lte)
        elif sk_gte:
            cond = cond & Key("sk").gte(sk_gte)
        elif sk_lte:
            cond = cond & Key("sk").lte(sk_lte)

        kwargs = {"KeyConditionExpression": cond, "ScanIndexForward": not descending}
        if filters:
            expr = None
            for path, expected in filters.items():
                # Attr handles dotted paths and reserved words ("data" IS a
                # DynamoDB reserved word) via generated name placeholders.
                clause = Attr(f"data.{path}").eq(expected)
                expr = clause if expr is None else expr & clause
            kwargs["FilterExpression"] = expr
        if project:
            # Every path segment gets a name placeholder: "data" is a DynamoDB
            # reserved word, and a projected field could be one too.
            names = {"#sk": "sk", "#data": "data"}
            parts = ["#sk"]
            for i, path in enumerate(project):
                aliases = []
                for j, seg in enumerate(path.split(".")):
                    alias = f"#p{i}_{j}"
                    names[alias] = seg
                    aliases.append(alias)
                parts.append("#data." + ".".join(aliases))
            kwargs["ProjectionExpression"] = ", ".join(parts)
            kwargs["ExpressionAttributeNames"] = names

        rows = []
        try:
            while True:
                # A FilterExpression is applied AFTER Limit, so passing Limit
                # through would silently under-return. Page until we have
                # enough post-filter rows instead.
                response = self.table.query(**kwargs)
                for item in response.get("Items", []):
                    rows.append((item["sk"], from_dynamo(item.get("data", {}))))
                    if limit and len(rows) >= limit:
                        return rows
                nxt = response.get("LastEvaluatedKey")
                if not nxt:
                    break
                kwargs["ExclusiveStartKey"] = nxt
        except Exception as e:
            log.warning(f"DynamoDB query {pk} failed: {e}")
        return rows

    def delete(self, pk, sk):
        try:
            self.table.delete_item(Key={"pk": pk, "sk": sk})
        except Exception as e:
            log.warning(f"Failed to delete DynamoDB state {pk}/{sk}: {e}")


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
