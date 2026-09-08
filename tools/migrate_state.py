"""
tools/migrate_state.py — move state onto the composite-key table
=================================================================
Reads the old single-``state_key`` table and writes the new ``pk``/``sk`` one,
exploding the growing collections into one item per record.

Only nine items actually need moving. The other ~6,800 rows in the old table
are TTL'd caches (``bars/*``, ``scan/*``) that rebuild themselves on the next
scan, so copying them would be pure cost.

    python -m tools.migrate_state --dry-run          # default; changes nothing
    python -m tools.migrate_state --apply
    python -m tools.migrate_state --verify

**Order matters at cutover.** Run this while the old code is still live and
before deploying the new code:

  1. deploy the new table (CDK) — nothing uses it yet
  2. ``--dry-run``, then ``--apply``, then ``--verify``
  3. deploy the code that reads the new table
  4. confirm ``/api/health`` reports the same ``trade_count`` as before

An empty trade log reads as "no trades this week", which re-arms the weekly
trade limit — so a migration that silently copies nothing is the one failure
worth being loud about. ``--verify`` exists for exactly that.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

import boto3

from raanu.state import keys
from raanu.trading.trader import _trim_alpaca

REGION = "eu-central-1"


def _legacy_items(table_name: str) -> dict:
    """Every non-cache item from the old table, as {state_key: parsed value}."""
    client = boto3.client("dynamodb", region_name=REGION)
    out, kwargs = {}, {"TableName": table_name}
    while True:
        page = client.scan(**kwargs)
        for item in page.get("Items", []):
            key = item["state_key"]["S"]
            if key.startswith(("bars/", "scan/")):
                continue                      # TTL'd cache, rebuilds itself
            try:
                out[key] = json.loads(item["data"]["S"])
            except Exception as e:
                print(f"  ! {key}: unreadable ({e})")
        if "LastEvaluatedKey" not in page:
            return out
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _parse_ts(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except Exception:
        return datetime.fromtimestamp(0, UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def plan(legacy: dict) -> list[tuple[str, str, dict, int | None]]:
    """Map old keys onto (pk, sk, data, ttl_seconds). Pure — no writes."""
    out: list[tuple[str, str, dict, int | None]] = []

    for trade in legacy.get("trades_log.json", {}).get("trades", []):
        stamp = keys.stamp(_parse_ts(trade.get("timestamp", "")))
        row = dict(trade, timestamp=stamp)
        if "alpaca_response" in row:
            row["alpaca_response"] = _trim_alpaca(row["alpaca_response"])
        out.append((keys.TRADE, keys.trade_sk(stamp), row, None))

    for pick in legacy.get("picks_log.json", {}).get("picks", []):
        row = dict(pick)
        row.setdefault("spy", {})
        # Anything already carrying every forward window is done maturing.
        row["matured"] = all(f"d{d}" in (row.get("fwd") or {})
                             for d in (1, 5, 20))
        out.append((keys.PICK,
                    keys.pick_sk(row["date"], row["strategy"], row["ticker"]),
                    row, None))

    for item in legacy.get("notifications.json", {}).get("items", []):
        stamp = keys.stamp(_parse_ts(item.get("ts", "")))
        out.append((keys.NOTIF, keys.notif_sk(stamp), dict(item, ts=stamp),
                    48 * 3600))

    for symbol, val in (legacy.get("position_peaks.json") or {}).items():
        data = val if isinstance(val, dict) else {"peak": float(val), "atr": None}
        out.append((keys.PEAK, keys.peak_sk(symbol), data, None))

    for sub in (legacy.get("push_subs.json") or {}).get("subs", []):
        if sub.get("endpoint"):
            out.append((keys.PUSHSUB, keys.pushsub_sk(sub["endpoint"]), sub, None))

    for legacy_key, name in (("last_picks.json", "last_picks"),
                             ("last_picks_s2.json", "last_picks_s2"),
                             ("last_picks_s3.json", "last_picks_s3")):
        if legacy_key in legacy:
            out.append((keys.CACHE, keys.cache_sk(name), legacy[legacy_key], None))

    for legacy_key, name in (("auto_trader.json", "auto_trader.json"),
                             ("scheduler_marks.json", "scheduler_marks")):
        if legacy_key in legacy:
            out.append((keys.FLAG, keys.flag_sk(name), legacy[legacy_key], None))

    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from-table", default="RaanuAwsSkeleton-StateTable9728C7E5-OOZ1KI3HJWKE")
    ap.add_argument("--to-table", required=True)
    ap.add_argument("--apply", action="store_true", help="actually write")
    ap.add_argument("--verify", action="store_true", help="compare counts only")
    args = ap.parse_args()

    legacy = _legacy_items(args.from_table)
    print(f"read {len(legacy)} legacy items from {args.from_table}")
    for key in sorted(legacy):
        print(f"  - {key}")

    records = plan(legacy)
    counts: dict[str, int] = {}
    for pk, _sk, _data, _ttl in records:
        counts[pk] = counts.get(pk, 0) + 1
    print(f"\nplanned {len(records)} items on {args.to_table}:")
    for pk in sorted(counts):
        print(f"  {pk:10s} {counts[pk]:4d}")

    if legacy.get("scan_job.json") is not None:
        print("\n  (dropping scan_job.json — orphan, referenced by no code)")

    if args.verify:
        table = boto3.resource("dynamodb", region_name=REGION).Table(args.to_table)
        print("\nverifying:")
        ok = True
        for pk in sorted(counts):
            got = table.query(
                KeyConditionExpression=boto3.dynamodb.conditions.Key("pk").eq(pk),
                Select="COUNT")["Count"]
            mark = "OK " if got == counts[pk] else "MISMATCH"
            ok &= got == counts[pk]
            print(f"  {mark} {pk:10s} expected {counts[pk]:4d}  found {got:4d}")
        return 0 if ok else 1

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    table = boto3.resource("dynamodb", region_name=REGION).Table(args.to_table)
    import time as _time

    from raanu.state.coerce import to_dynamo
    written = 0
    for pk, sk, data, ttl in records:
        item = {"pk": pk, "sk": sk, "data": to_dynamo(data)}
        if ttl:
            item["ttl"] = int(_time.time()) + ttl
        table.put_item(Item=item)
        written += 1
    print(f"\nwrote {written} items. Now run --verify.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
