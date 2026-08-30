"""Per-exchange symbol lists and universe selection."""

from __future__ import annotations

import json

import pytest

from raanu.market import exchanges
from raanu.market.universe import scannable_universe
from raanu.scanning import job


class TestFile:
    def test_parses_and_has_the_expected_venues(self):
        data = json.loads(exchanges.EXCHANGES_FILE.read_text())["exchanges"]
        assert {"nasdaq", "nyse", "nyse_arca", "nyse_american"} <= set(data)

    def test_every_exchange_has_stocks_etfs_and_a_label(self):
        for key, entry in json.loads(exchanges.EXCHANGES_FILE.read_text())["exchanges"].items():
            assert entry["label"], key
            assert isinstance(entry["stocks"], list) and isinstance(entry["etfs"], list)

    def test_symbols_are_resolvable_by_the_price_feed(self):
        # Dots and dollar signs mark preferred shares, warrants and units,
        # which yfinance cannot resolve — each one would be pure retry cost
        # inside its download batch.
        for entry in json.loads(exchanges.EXCHANGES_FILE.read_text())["exchanges"].values():
            for symbol in entry["stocks"] + entry["etfs"]:
                assert symbol == symbol.upper() and not set(symbol) & set(".$ "), symbol

    def test_no_duplicates_within_an_exchange(self):
        for key, entry in json.loads(exchanges.EXCHANGES_FILE.read_text())["exchanges"].items():
            combined = entry["stocks"] + entry["etfs"]
            assert len(combined) == len(set(combined)), key

    def test_stocks_and_etfs_are_disjoint(self):
        for key, entry in json.loads(exchanges.EXCHANGES_FILE.read_text())["exchanges"].items():
            assert not (set(entry["stocks"]) & set(entry["etfs"])), key


class TestCatalog:
    def test_curated_is_first_and_is_the_default(self):
        catalog = exchanges.catalog()
        assert catalog[0]["key"] == exchanges.CURATED
        assert catalog[0]["total"] == len(scannable_universe())

    def test_totals_add_up(self):
        for entry in exchanges.catalog():
            assert entry["total"] == entry["stocks"] + entry["etfs"]

    def test_arca_is_almost_entirely_etfs(self):
        # Documents a fact worth knowing before choosing it: these are
        # equity strategies, so an ETF venue mostly buys download time.
        arca = next(e for e in exchanges.catalog() if e["key"] == "nyse_arca")
        assert arca["etfs"] > arca["stocks"] * 50


class TestTickersFor:
    def test_curated_is_the_scannable_universe(self):
        assert exchanges.tickers_for("curated") == scannable_universe()

    def test_default_is_curated(self):
        assert exchanges.tickers_for() == scannable_universe()

    def test_an_exchange_returns_stocks_plus_etfs(self):
        nyse = exchanges.tickers_for("nyse")
        catalog = next(e for e in exchanges.catalog() if e["key"] == "nyse")
        assert len(nyse) == catalog["total"]

    def test_etfs_can_be_excluded(self):
        catalog = next(e for e in exchanges.catalog() if e["key"] == "nyse")
        assert len(exchanges.tickers_for("nyse", include_etfs=False)) == catalog["stocks"]

    def test_all_spans_every_exchange_without_duplicates(self):
        every = exchanges.tickers_for("all")
        assert len(every) == len(set(every))
        assert set(exchanges.tickers_for("nyse")) <= set(every)

    def test_unknown_key_falls_back_to_curated_rather_than_raising(self):
        # A stale dashboard asking for a removed exchange should scan
        # something sensible, not 500.
        assert exchanges.tickers_for("mars_stock_exchange") == scannable_universe()

    @pytest.mark.parametrize("key", ["NASDAQ", " nasdaq ", "NySe"])
    def test_key_matching_is_forgiving(self, key):
        assert len(exchanges.tickers_for(key)) > 100


class TestShardScaling:
    def test_small_universes_use_the_configured_width(self):
        from raanu import config
        assert job.shard_count_for(470) == config.scan_shards()

    def test_large_universes_get_more_shards_not_longer_ones(self, monkeypatch):
        # Otherwise a full-exchange shard would creep toward the Lambda
        # timeout instead of the fan-out absorbing the extra work.
        monkeypatch.setenv("SCAN_SHARDS", "2")
        monkeypatch.setenv("SCAN_MAX_SHARDS", "12")
        assert job.shard_count_for(5581) > 2

    def test_shard_count_is_capped_by_the_concurrency_budget(self, monkeypatch):
        # The binding constraint is this account's Lambda concurrency limit
        # of 10, NOT cost. Fanning past it starves the API Lambda and 429s
        # the dashboard mid-scan.
        monkeypatch.setenv("SCAN_MAX_SHARDS", "6")
        assert job.shard_count_for(100_000) == 6

    def test_default_fanout_leaves_headroom_under_a_10_concurrency_account(self):
        from raanu import config
        assert config.scan_shards() <= 7, "must leave slots for the API Lambda"
        assert config.scan_max_shards() <= 7

    def test_every_ticker_lands_in_exactly_one_shard(self):
        tickers = exchanges.tickers_for("nyse")
        shards = job.plan_shards(tickers, job.shard_count_for(len(tickers)))
        assert [t for s in shards for t in s] == tickers


class TestResultCapping:
    def test_a_shard_caps_its_stored_hits_but_reports_the_true_count(self, monkeypatch):
        # A full-exchange scan projects to ~880 hits at ~640 bytes each —
        # half a megabyte in every 1.5s poll. Capping bounds that; reporting
        # total_hits keeps the truncation visible instead of silent.
        many = [{"ticker": f"T{i}", "score": i} for i in range(200)]
        monkeypatch.setattr(job, "scan_universe", lambda *a, **k: list(many))
        monkeypatch.setattr(job, "enrich_market_caps", lambda hits: None)

        job.run_shard("run-x", 0, ["A"])
        from raanu import state
        shard = state.load(job._shard_key("run-x", 0))
        assert len(shard["hits"]) == job._MAX_HITS_PER_SHARD
        assert shard["total_hits"] == 200
        # And it keeps the BEST ones, not the first ones it happened to see.
        assert shard["hits"][0]["score"] == 199

    def test_status_surfaces_the_uncapped_total(self, monkeypatch):
        many = [{"ticker": f"T{i}", "score": i} for i in range(100)]
        monkeypatch.setattr(job, "scan_universe", lambda *a, **k: list(many))
        monkeypatch.setattr(job, "enrich_market_caps", lambda hits: None)
        job.run_inline(job.start_run(mode="cheap", tickers=["A", "B"]))
        snapshot = job.status()
        assert snapshot["total_hits"] == 100
        assert len(snapshot["results"]) == job._MAX_HITS_PER_SHARD
