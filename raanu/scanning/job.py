"""
raanu.scanning.job — scan runs: sharding, fan-out, aggregation
===============================================================
A full scan of the curated universe takes ~120s on Lambda (measured, three
consecutive runs: 122.5s / 114.4s / 117.0s), and the downloads are ~80% of
that. Nothing can hold an HTTP request open that long here: CloudFront's
origin timeout for a Function URL tops out at 60s without an AWS quota
increase, and Mangum buffers the whole response anyway. So a scan is a
*job*: started asynchronously, polled for progress.

Two modes, because the two callers want opposite things:

  * **fast** — a human clicked "Scan market" and is watching a progress bar.
    Fan out across N worker invocations so wall-clock is one shard's work
    (~20-25s) rather than the whole universe's (~120s).
  * **cheap** — the scheduled 03:30 / 09:35 / 11:00 ET slots, where nobody
    is watching. One invocation, sequential, one cold start instead of N.

Both run the identical engine. Only the execution shape differs.

Storage uses the existing single-partition-key table, no schema change:

    scan/current              -> the manifest (run id, shard count, mode)
    scan/{run_id}/shard/{i}   -> one shard's progress and hits

Per-shard items also fix a latent problem in the previous design, which
rewrote the entire growing result list to ONE item every 25 tickers — ~19
writes of a payload climbing toward DynamoDB's 400KB item ceiling.
"""

from __future__ import annotations

import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from raanu import config, state
from raanu.market import exchanges
from raanu.scanning.engine import enrich_market_caps, scan_universe

log = logging.getLogger("raanu.scanning.job")

MANIFEST_KEY = "scan/current"

# Scan state is transient by nature — the next run supersedes it. A day is
# long enough to inspect a finished run and short enough that per-run shard
# items never accumulate.
_RUN_TTL_SECONDS = 24 * 3600

# Tickers per shard the planner aims for when sizing a fan-out. A curated
# 470-ticker scan stays at SCAN_SHARDS; a full-exchange scan gets more
# shards rather than longer ones, so no single shard approaches the Lambda
# timeout.
_TARGET_PER_SHARD = 600

# Concurrency ceiling. 8 shards measured ~20 tickers/s aggregate against a
# ~6.2/s single-host rate — a 3.2x gain, not 8x, which says Yahoo is already
# throttling partially. Going much wider is untested and may trip a harder
# limit rather than going faster, so this stays conservative. Raise it only
# with a measurement in hand.
_MAX_SHARDS = 12

# Hits each shard keeps, highest score first. A full-exchange scan projects
# to ~880 hits at ~640 bytes each — over half a megabyte in every poll
# response, at 1.5s intervals. Capping bounds both that and the DynamoDB
# item. The true count is still reported as `total_hits`, so truncation is
# visible rather than silent.
#
# Note this keeps each shard's OWN top N, which is not exactly the global
# top N: a shard finding 40 strong names loses its weakest while an empty
# shard contributes nothing. The very best names survive regardless, which
# is what the screen is for.
_MAX_HITS_PER_SHARD = 25

# How long a run may go without completing before the aggregate reports it
# as stalled. A shard that dies (OOM, timeout, a Lambda-level error) would
# otherwise leave the UI polling a run that can never finish.
_STALL_AFTER_SECONDS = 15 * 60


def _shard_key(run_id: str, index: int) -> str:
    return f"scan/{run_id}/shard/{index}"


def shard_count_for(ticker_count: int) -> int:
    """How many shards to fan a scan of this size across."""
    import math
    wanted = math.ceil(ticker_count / _TARGET_PER_SHARD) if ticker_count else 1
    return max(config.scan_shards(), min(wanted, _MAX_SHARDS))


def plan_shards(tickers: list[str], shard_count: int) -> list[list[str]]:
    """Split tickers into ``shard_count`` contiguous, near-equal slices.

    Balanced to within one ticker, because wall-clock for a fan-out is the
    SLOWEST shard — an uneven split wastes exactly as much time as its
    largest slice is oversized.
    """
    shard_count = max(1, min(shard_count, len(tickers))) if tickers else 1
    base, extra = divmod(len(tickers), shard_count)
    shards, start = [], 0
    for i in range(shard_count):
        size = base + (1 if i < extra else 0)
        shards.append(tickers[start:start + size])
        start += size
    return shards


def start_run(mode: str = "fast", tickers: list[str] | None = None,
              universe_key: str = exchanges.CURATED) -> dict:
    """Create a run manifest and return it. Does not execute anything."""
    universe = tickers if tickers is not None else exchanges.tickers_for(universe_key)
    shard_count = shard_count_for(len(universe)) if mode == "fast" else 1
    shards = plan_shards(universe, shard_count)

    manifest = {
        "run_id": uuid.uuid4().hex[:12],
        "mode": mode,
        "universe": universe_key,
        "universe_label": exchanges.label_for(universe_key),
        "shards": len(shards),
        "total": len(universe),
        "started_at": time.time(),
    }
    state.save(MANIFEST_KEY, manifest, ttl_seconds=_RUN_TTL_SECONDS)
    # Seed every shard as pending, so a poll arriving before the workers have
    # cold-started reports "0 of N done" rather than an empty, ambiguous run.
    for index, shard in enumerate(shards):
        state.save(_shard_key(manifest["run_id"], index),
                   {"status": "pending", "scanned": 0, "total": len(shard), "hits": []},
                   ttl_seconds=_RUN_TTL_SECONDS)
    return {**manifest, "_shards": shards}


def run_shard(run_id: str, index: int, tickers: list[str]) -> None:
    """Execute one shard and persist its progress. Never raises.

    A shard that dies must not take the run down with it — the aggregate
    reports what the surviving shards found and flags the failure.
    """
    key = _shard_key(run_id, index)
    total = len(tickers)

    def publish(status: str, scanned: int, hits: list[dict], error: str | None = None) -> None:
        ranked = sorted(hits, key=lambda h: h.get("score", 0), reverse=True)
        payload = {
            "status": status,
            "scanned": scanned,
            "total": total,
            "hits": ranked[:_MAX_HITS_PER_SHARD],
            "total_hits": len(ranked),
        }
        if error:
            payload["error"] = error
        state.save(key, payload, ttl_seconds=_RUN_TTL_SECONDS)

    publish("running", 0, [])
    try:
        hits = scan_universe(
            tickers,
            batch_size=config.scan_batch_size(),
            on_progress=lambda scanned, found: publish("running", scanned, found),
        )
        # Only the qualifying handful need a cap lookup, so this runs once at
        # the end rather than per hit during scoring.
        enrich_market_caps(hits)
        publish("done", total, hits)
        log.info(f"[scan {run_id}] shard {index}: {total} scanned, {len(hits)} hits")
    except Exception as e:
        log.exception(f"[scan {run_id}] shard {index} failed: {e}")
        publish("error", 0, [], error=str(e))


def dispatch(manifest: dict) -> None:
    """Send each shard to the worker Lambda, concurrently.

    boto3 is synchronous, so N sequential invokes would add N round trips to
    the caller's response. A small pool keeps the whole fan-out inside a
    single round trip's worth of latency.
    """
    worker = config.worker_function_name()
    if not worker:
        raise RuntimeError("WORKER_FUNCTION_NAME is not set — cannot dispatch scan shards")

    import json

    import boto3
    client = boto3.client("lambda")
    run_id, shards = manifest["run_id"], manifest["_shards"]

    def invoke(item):
        index, tickers = item
        client.invoke(
            FunctionName=worker,
            InvocationType="Event",
            Payload=json.dumps({
                "task": "scan_shard",
                "run_id": run_id,
                "index": index,
                "tickers": tickers,
            }).encode(),
        )

    with ThreadPoolExecutor(max_workers=min(len(shards), 16)) as pool:
        list(pool.map(invoke, enumerate(shards)))
    log.info(f"[scan {run_id}] dispatched {len(shards)} shard(s) to {worker}")


def run_inline(manifest: dict) -> None:
    """Cheap mode: run every shard in this process, sequentially."""
    for index, tickers in enumerate(manifest["_shards"]):
        run_shard(manifest["run_id"], index, tickers)


def status() -> dict:
    """Merged view of the current run, for the dashboard to poll.

    One BatchGetItem for all shards rather than N reads, so polling every
    1.5s stays cheap.
    """
    manifest = state.load(MANIFEST_KEY)
    if not manifest:
        return {"status": "idle"}

    run_id = manifest["run_id"]
    shard_count = manifest.get("shards", 1)
    shards = state.load_many([_shard_key(run_id, i) for i in range(shard_count)])

    scanned = sum(s.get("scanned", 0) for s in shards.values())
    hits: list[dict] = []
    found = 0
    for shard in shards.values():
        hits.extend(shard.get("hits") or [])
        found += shard.get("total_hits", len(shard.get("hits") or []))
    hits.sort(key=lambda h: h.get("score", 0), reverse=True)

    done = sum(1 for s in shards.values() if s.get("status") == "done")
    failed = [s for s in shards.values() if s.get("status") == "error"]
    finished = (done + len(failed)) >= shard_count and shard_count > 0

    if finished:
        # A run where every shard failed is a failure; a run where some
        # succeeded still has real results worth showing.
        status_value = "error" if done == 0 and failed else "done"
    elif time.time() - manifest.get("started_at", 0) > _STALL_AFTER_SECONDS:
        status_value = "stalled"
    else:
        status_value = "running"

    out = {
        "status": status_value,
        "run_id": run_id,
        "mode": manifest.get("mode", "fast"),
        "scanned": scanned,
        "total": manifest.get("total", 0),
        "shards": shard_count,
        "shards_done": done,
        "universe": manifest.get("universe", exchanges.CURATED),
        "universe_label": manifest.get("universe_label", ""),
        "results": hits,
        # Diverges from len(results) once a shard hits _MAX_HITS_PER_SHARD.
        # Surfaced so the UI can say "showing N of M" instead of quietly
        # presenting a truncated list as the whole answer.
        "total_hits": found,
        "elapsed": round(time.time() - manifest.get("started_at", 0), 1),
    }
    if failed:
        # Surfaced rather than swallowed: partial results are useful, but the
        # user should know they are partial.
        out["failed_shards"] = len(failed)
        out["error"] = next((s.get("error") for s in failed if s.get("error")), "shard failed")
    return out


def is_running() -> bool:
    return status().get("status") == "running"
