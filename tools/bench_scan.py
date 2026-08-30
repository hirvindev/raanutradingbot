"""
tools/bench_scan.py — measure the scan, and what sharding buys
===============================================================
Answers one question with real numbers instead of a model: how long does a
batch of tickers actually take to download and score, and does splitting the
universe across concurrent workers actually help — or does Yahoo Finance
throttle the concurrency away?

Worth running before changing SCAN_SHARDS or SCAN_BATCH_SIZE. It hits the
live Yahoo endpoints, so it is deliberately not part of the test suite.

    python -m tools.bench_scan --tickers 96 --shards 8
"""

from __future__ import annotations

import argparse
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from raanu.market.universe import scannable_universe
from raanu.scanning.engine import scan_universe


def _run(label: str, fn) -> tuple[float, int]:
    start = time.perf_counter()
    hits = fn()
    elapsed = time.perf_counter() - start
    print(f"  {label:<34} {elapsed:7.1f}s   {len(hits):3d} hits")
    return elapsed, len(hits)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", type=int, default=96,
                    help="how many of the curated universe to use")
    ap.add_argument("--shards", type=int, default=8)
    ap.add_argument("--batch-size", type=int, default=20)
    args = ap.parse_args()

    logging.basicConfig(level=logging.ERROR)  # keep yfinance noise out of the numbers
    universe = scannable_universe()[:args.tickers]
    print(f"\nBenchmark: {len(universe)} tickers, {args.shards} shards, "
          f"batch size {args.batch_size}\n")

    seq, seq_hits = _run("sequential (cheap mode)",
                         lambda: scan_universe(universe, batch_size=args.batch_size))

    from raanu.scanning.job import plan_shards
    shards = plan_shards(universe, args.shards)

    def sharded():
        # Threads stand in for concurrent Lambda invocations. The work is
        # network-bound, so this models the real fan-out closely enough to
        # tell whether Yahoo throttles concurrent callers — which is the
        # thing that could invalidate the whole design.
        with ThreadPoolExecutor(max_workers=len(shards)) as pool:
            results = pool.map(
                lambda s: scan_universe(s, batch_size=args.batch_size), shards)
            return [h for r in results for h in r]

    par, par_hits = _run(f"sharded x{len(shards)} (fast mode)", sharded)

    print()
    if par > 0:
        print(f"  speedup                            {seq / par:.1f}x")
    print(f"  per-shard slice                    {len(shards[0])} tickers")
    if seq_hits != par_hits:
        # Not necessarily a bug — a flaky Yahoo response changes what scores
        # — but it means these two numbers are not directly comparable.
        print(f"  NOTE: hit counts differ ({seq_hits} vs {par_hits}); "
              f"live data moved between runs")
    print()


if __name__ == "__main__":
    main()
