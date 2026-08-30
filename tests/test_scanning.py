"""Scan engine and job orchestration.

The engine is tested with fake scorers rather than real price data: the
point under test is the loop, the filtering and the sharding, not whether
pandas computes an EMA correctly (which the indicator tests cover).
"""

from __future__ import annotations

import pytest

from raanu import state, strategies
from raanu.scanning import engine, job


@pytest.fixture
def fake_market(monkeypatch):
    """Deterministic prices + scorers.

    AAA passes S1, BBB passes S2, CCC passes S3, everything else fails.
    """
    # The engine reads bars through the cache now, so that is the seam.
    monkeypatch.setattr(engine, "get_bars", lambda tickers: {t: object() for t in tickers})
    monkeypatch.setattr(engine, "benchmark_return_3m", lambda: 0.05)
    monkeypatch.setattr(engine, "get_ticker_name", lambda t: f"{t} Inc")

    def s1(ticker, df, bench_ret_3m=None):
        if ticker == "AAA":
            return {"ok": True, "uptrend": True, "score": 80, "rsi": 50, "macd": 1,
                    "macd_signal": 0, "rel_strength": 5, "mom_3m": 10, "ticker": ticker}
        return {"ok": True, "uptrend": False, "score": 10, "ticker": ticker}

    def s2(ticker, df, bench_ret_3m=None):
        if ticker == "BBB":
            return {"ok": True, "stage2": True, "score": 75, "rel_strength": 3, "ticker": ticker}
        return {"ok": True, "stage2": False, "score": 5, "ticker": ticker}

    def s3(ticker, df, bench_ret_3m=None):
        if ticker == "CCC":
            return {"ok": True, "leader_dip": True, "score": 65, "ticker": ticker}
        return {"ok": True, "leader_dip": False, "score": 5, "ticker": ticker}

    for key, fn in (("s1", s1), ("s2", s2), ("s3", s3)):
        _swap_scorer(monkeypatch, key, fn)
    # Market-cap lookup is a live HTTP call; neutralise it.
    monkeypatch.setattr(engine, "_fetch_market_cap", lambda t: None)
    return ["AAA", "BBB", "CCC", "DDD", "EEE"]


def _swap_scorer(monkeypatch, key, fn):
    """Replace a strategy's scorer for the duration of a test.

    Strategy is frozen — deliberately, a strategy definition is not
    runtime-mutable state — so this swaps the whole registry entry for a
    real Strategy carrying the fake scorer, rather than poking the field.
    """
    import dataclasses
    monkeypatch.setitem(
        strategies.REGISTRY, key,
        dataclasses.replace(strategies.REGISTRY[key], score=fn),
    )


class TestScanBatch:
    def test_finds_one_hit_per_matching_strategy(self, fake_market):
        hits = engine.scan_batch(fake_market, bench=0.05)
        assert sorted(h["strategy"] for h in hits) == ["s1", "s2", "s3"]
        assert {h["ticker"] for h in hits} == {"AAA", "BBB", "CCC"}

    def test_tags_hits_with_strategy_and_name(self, fake_market):
        hits = engine.scan_batch(fake_market, bench=0.05)
        s1_hit = next(h for h in hits if h["strategy"] == "s1")
        assert s1_hit["ticker"] == "AAA"
        assert s1_hit["name"] == "AAA Inc"

    def test_can_restrict_to_one_strategy(self, fake_market):
        hits = engine.scan_batch(fake_market, bench=0.05, keys=["s2"])
        assert [h["ticker"] for h in hits] == ["BBB"]

    def test_empty_input(self, fake_market):
        assert engine.scan_batch([], bench=0.05) == []

    def test_a_raising_scorer_does_not_abort_the_batch(self, fake_market, monkeypatch):
        def explode(ticker, df, bench_ret_3m=None):
            raise ValueError("bad frame")
        _swap_scorer(monkeypatch, "s1", explode)
        # S2 and S3 must still produce their hits.
        hits = engine.scan_batch(fake_market, bench=0.05)
        assert sorted(h["strategy"] for h in hits) == ["s2", "s3"]

    def test_tradable_predicate_is_looser_than_surfaces(self, fake_market, monkeypatch):
        # score 62 clears `tradable` (>=60) but not S1's `surfaces` bar (>=70).
        _swap_scorer(monkeypatch, "s1", lambda t, df, bench_ret_3m=None: {
            "ok": True, "uptrend": True, "score": 62, "rsi": 50, "macd": 1,
            "macd_signal": 0, "rel_strength": 5, "mom_3m": 10, "ticker": t})
        assert engine.scan_batch(["AAA"], 0.05, ["s1"], predicate="surfaces") == []
        assert len(engine.scan_batch(["AAA"], 0.05, ["s1"], predicate="tradable")) == 1


class TestScanUniverse:
    def test_progress_is_reported_per_batch_not_just_at_the_end(self, fake_market):
        # The whole reason for small batches: the old scanner downloaded 250
        # at a time and reported nothing for ~53s.
        seen = []
        engine.scan_universe(fake_market, batch_size=2,
                             on_progress=lambda scanned, hits: seen.append(scanned))
        assert seen == [2, 4, 5]

    def test_progress_carries_hits_found_so_far(self, fake_market):
        snapshots = []
        engine.scan_universe(fake_market, batch_size=2,
                             on_progress=lambda scanned, hits: snapshots.append(len(hits)))
        assert snapshots[-1] == 3
        assert snapshots == sorted(snapshots), "hit count must be monotonic"

    def test_batching_does_not_change_the_result(self, fake_market):
        one = engine.scan_universe(fake_market, batch_size=100)
        many = engine.scan_universe(fake_market, batch_size=1)
        assert {(h["ticker"], h["strategy"]) for h in one} == \
               {(h["ticker"], h["strategy"]) for h in many}


class TestTopPicks:
    def test_returns_highest_scoring_first_and_respects_limit(self, fake_market, monkeypatch):
        scores = {"AAA": 90, "BBB": 70, "CCC": 80, "DDD": 60, "EEE": 65}
        _swap_scorer(monkeypatch, "s1", lambda t, df, bench_ret_3m=None: {
            "ok": True, "uptrend": True, "score": scores[t], "ticker": t})
        picks = engine.top_picks("s1", limit=3, tickers=fake_market)
        assert [p["ticker"] for p in picks] == ["AAA", "CCC", "BBB"]


class TestPlanShards:
    def test_splits_evenly_and_loses_nothing(self):
        shards = job.plan_shards([f"T{i}" for i in range(472)], 8)
        assert len(shards) == 8
        assert sum(len(s) for s in shards) == 472
        assert [t for s in shards for t in s] == [f"T{i}" for i in range(472)]

    def test_balanced_to_within_one_ticker(self):
        # Wall-clock for a fan-out is the slowest shard, so an uneven split
        # wastes exactly as much time as its largest slice is oversized.
        sizes = {len(s) for s in job.plan_shards([f"T{i}" for i in range(472)], 8)}
        assert max(sizes) - min(sizes) <= 1

    def test_more_shards_than_tickers_does_not_produce_empty_shards(self):
        shards = job.plan_shards(["A", "B"], 8)
        assert shards == [["A"], ["B"]]

    def test_single_shard_is_the_whole_universe(self):
        assert job.plan_shards(["A", "B", "C"], 1) == [["A", "B", "C"]]

    def test_empty_universe(self):
        assert job.plan_shards([], 4) == [[]]


class TestRunLifecycle:
    def test_cheap_mode_is_always_one_shard(self, fake_market, monkeypatch):
        monkeypatch.setenv("SCAN_SHARDS", "8")
        manifest = job.start_run(mode="cheap", tickers=fake_market)
        assert manifest["shards"] == 1
        assert manifest["mode"] == "cheap"

    def test_fast_mode_uses_the_configured_shard_count(self, fake_market, monkeypatch):
        monkeypatch.setenv("SCAN_SHARDS", "3")
        assert job.start_run(mode="fast", tickers=fake_market)["shards"] == 3

    def test_status_is_idle_before_any_run(self):
        assert job.status()["status"] == "idle"

    def test_shards_start_pending_so_an_early_poll_is_not_ambiguous(self, fake_market, monkeypatch):
        monkeypatch.setenv("SCAN_SHARDS", "3")
        job.start_run(mode="fast", tickers=fake_market)
        snapshot = job.status()
        assert snapshot["status"] == "running"
        assert snapshot["shards"] == 3 and snapshot["shards_done"] == 0

    def test_inline_run_completes_and_aggregates_every_shard(self, fake_market, monkeypatch):
        monkeypatch.setenv("SCAN_SHARDS", "3")
        manifest = job.start_run(mode="fast", tickers=fake_market)
        job.run_inline(manifest)

        snapshot = job.status()
        assert snapshot["status"] == "done"
        assert snapshot["scanned"] == 5
        assert snapshot["shards_done"] == 3
        assert sorted(h["strategy"] for h in snapshot["results"]) == ["s1", "s2", "s3"]

    def test_results_are_sorted_by_score_across_shards(self, fake_market):
        manifest = job.start_run(mode="cheap", tickers=fake_market)
        job.run_inline(manifest)
        scores = [h["score"] for h in job.status()["results"]]
        assert scores == sorted(scores, reverse=True)

    def test_a_failed_shard_is_reported_but_others_still_return_results(
            self, fake_market, monkeypatch):
        monkeypatch.setenv("SCAN_SHARDS", "2")
        manifest = job.start_run(mode="fast", tickers=fake_market)
        job.run_shard(manifest["run_id"], 0, manifest["_shards"][0])

        def explode(*a, **k):
            raise RuntimeError("shard blew up")
        monkeypatch.setattr(job, "scan_universe", explode)
        job.run_shard(manifest["run_id"], 1, manifest["_shards"][1])

        snapshot = job.status()
        assert snapshot["status"] == "done"       # partial success, not total failure
        assert snapshot["failed_shards"] == 1
        assert "shard blew up" in snapshot["error"]
        assert snapshot["results"], "surviving shard's hits must still be returned"

    def test_all_shards_failing_is_an_error(self, fake_market, monkeypatch):
        monkeypatch.setattr(job, "scan_universe", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        job.run_inline(job.start_run(mode="cheap", tickers=fake_market))
        assert job.status()["status"] == "error"

    def test_a_run_that_never_finishes_is_reported_as_stalled(self, fake_market, monkeypatch):
        # Guards the UI against polling forever when a shard dies at the
        # Lambda level (OOM/timeout) and never writes a terminal state.
        job.start_run(mode="cheap", tickers=fake_market)
        stale = dict(state.load(job.MANIFEST_KEY))
        stale["started_at"] -= job._STALL_AFTER_SECONDS + 1
        state.save(job.MANIFEST_KEY, stale)
        assert job.status()["status"] == "stalled"


class TestDispatch:
    def test_refuses_to_dispatch_without_a_worker_configured(self, fake_market):
        manifest = job.start_run(mode="fast", tickers=fake_market)
        with pytest.raises(RuntimeError, match="WORKER_FUNCTION_NAME"):
            job.dispatch(manifest)

    def test_invokes_the_worker_once_per_shard_asynchronously(self, fake_market, monkeypatch):
        import json

        monkeypatch.setenv("WORKER_FUNCTION_NAME", "raanu-worker")
        monkeypatch.setenv("SCAN_SHARDS", "3")
        calls = []

        class FakeLambda:
            def invoke(self, **kw):
                calls.append(kw)
                return {"StatusCode": 202}

        import boto3
        monkeypatch.setattr(boto3, "client", lambda name, *a, **k: FakeLambda())

        manifest = job.start_run(mode="fast", tickers=fake_market)
        job.dispatch(manifest)

        assert len(calls) == 3
        assert {c["FunctionName"] for c in calls} == {"raanu-worker"}
        # Event = fire-and-forget. RequestResponse would block the API Lambda
        # for the whole scan, which is the bug this design exists to avoid.
        assert {c["InvocationType"] for c in calls} == {"Event"}
        payloads = [json.loads(c["Payload"]) for c in calls]
        assert {p["task"] for p in payloads} == {"scan_shard"}
        assert sorted(p["index"] for p in payloads) == [0, 1, 2]
        # Every ticker must be dispatched exactly once.
        assert sorted(t for p in payloads for t in p["tickers"]) == sorted(fake_market)
