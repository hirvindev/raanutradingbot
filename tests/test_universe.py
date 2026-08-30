"""The curated universe, now loaded from universe.json.

Moving the list out of Python removed the compiler as a safety net: a typo
in a .py file fails at import, a typo in JSON just yields wrong data. These
tests are that net.
"""

from __future__ import annotations

import json

from raanu.market import universe


class TestFileIntegrity:
    def test_json_is_the_source_and_parses(self):
        assert universe.UNIVERSE_FILE.exists()
        json.loads(universe.UNIVERSE_FILE.read_text())

    def test_expected_size(self):
        assert len(universe.FALLBACK_UNIVERSE) == 472
        assert len(universe.scannable_universe()) == 470

    def test_no_duplicates(self):
        # A duplicate would be scored twice and could surface twice in Live
        # Signals as if it were two separate setups.
        u = universe.FALLBACK_UNIVERSE
        assert len(u) == len(set(u))

    def test_tickers_look_like_tickers(self):
        for t in universe.FALLBACK_UNIVERSE:
            assert t == t.upper() and t.isascii() and 1 <= len(t) <= 5, t
            # A dot means a foreign listing; Alpaca's IEX feed is US-only.
            assert "." not in t, t

    def test_names_are_a_subset_of_the_universe(self):
        assert set(universe.TICKER_NAMES) <= set(universe.FALLBACK_UNIVERSE)

    def test_known_no_data_entries_are_in_the_universe(self):
        # Excluding a ticker that isn't there would silently do nothing.
        assert universe.KNOWN_NO_DATA <= set(universe.FALLBACK_UNIVERSE)

    def test_every_exclusion_carries_a_reason(self):
        reasons = json.loads(universe.UNIVERSE_FILE.read_text())["known_no_data"]
        assert all(v.strip() for v in reasons.values())

    def test_test_universe_is_real_tickers_from_the_universe(self):
        assert set(universe.TEST_UNIVERSE) <= set(universe.FALLBACK_UNIVERSE)


class TestScannable:
    def test_excludes_the_no_data_tickers(self):
        scannable = universe.scannable_universe()
        assert not (set(scannable) & universe.KNOWN_NO_DATA)
        assert "AAPL" in scannable

    def test_preserves_order(self):
        # Scan shards are contiguous slices, so order decides shard
        # assignment — a reorder silently reshuffles the work.
        scannable = universe.scannable_universe()
        expected = [t for t in universe.FALLBACK_UNIVERSE if t not in universe.KNOWN_NO_DATA]
        assert scannable == expected

    def test_summary_counts_what_is_actually_scanned(self):
        assert universe.get_universe_summary()["total_stocks"] == 470


class TestNames:
    def test_known_name_resolves(self):
        assert universe.get_ticker_name("AAPL") == "Apple"

    def test_unknown_ticker_falls_back_to_the_symbol(self, monkeypatch):
        monkeypatch.setattr(universe, "_load_asset_names", dict)
        assert universe.get_ticker_name("ZZZZ") == "ZZZZ"

    def test_alpaca_lookup_is_skipped_without_credentials(self):
        # No keys must mean no network call and no exception — just bare
        # symbols in the UI.
        universe.reset_name_cache()
        assert universe._load_asset_names() == {}
