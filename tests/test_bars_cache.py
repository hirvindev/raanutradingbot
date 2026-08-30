"""Daily OHLCV cache.

The cache is the main performance mechanism in the scan path, so the
round-trip fidelity matters: a frame that comes back subtly different from
the one that went in would silently change every score computed from it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from raanu.market import cache


def make_frame(rows: int = 260, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2025-01-01", periods=rows)
    close = 100 + np.cumsum(rng.normal(0, 1, rows))
    return pd.DataFrame(
        {
            "Open": close + rng.normal(0, 0.5, rows),
            "High": close + abs(rng.normal(1, 0.5, rows)),
            "Low": close - abs(rng.normal(1, 0.5, rows)),
            "Close": close,
            "Volume": rng.integers(1e6, 5e6, rows).astype(float),
        },
        index=idx,
    )


class TestRoundTrip:
    def test_frame_survives_encode_decode_intact(self):
        # Lossy by design at 4dp — see _PRECISION. Values must match to well
        # inside that, and dtypes must match exactly.
        original = make_frame()
        restored = cache._decode(cache._encode(original))
        pd.testing.assert_frame_equal(original, restored, check_freq=False,
                                      check_exact=False, atol=1e-4)

    def test_dtypes_are_preserved_exactly(self):
        # A float Volume column silently returning as int64 would change the
        # dtype of data every score is computed from.
        original = make_frame()
        restored = cache._decode(cache._encode(original))
        assert dict(restored.dtypes) == dict(original.dtypes)

    def test_precision_loss_is_below_a_hundredth_of_a_cent(self):
        original = make_frame()
        restored = cache._decode(cache._encode(original))
        assert (original["Close"] - restored["Close"]).abs().max() < 1e-4

    def test_index_comes_back_as_datetimes(self):
        # The indicators slice by date; a string index would break them in a
        # way that produces wrong numbers rather than an obvious error.
        restored = cache._decode(cache._encode(make_frame()))
        assert isinstance(restored.index, pd.DatetimeIndex)

    def test_compression_is_worth_having(self):
        # Uncompressed, 472 tickers/day of writes costs real money on
        # DynamoDB's per-KB pricing. This is the claim that makes it free.
        original = make_frame()
        raw = len(original.to_json(orient="split", date_format="iso"))
        assert len(cache._encode(original)) < raw / 3, "expected >3x vs raw JSON"


class TestStoreAndLoad:
    def test_round_trip_through_state(self):
        frames = {"AAA": make_frame(seed=1), "BBB": make_frame(seed=2)}
        assert cache.store(frames) == 2
        loaded = cache.load(["AAA", "BBB"])
        assert set(loaded) == {"AAA", "BBB"}
        pd.testing.assert_frame_equal(loaded["AAA"], frames["AAA"], check_freq=False,
                                      check_exact=False, atol=1e-4)

    def test_only_returns_what_is_cached(self):
        cache.store({"AAA": make_frame()})
        assert set(cache.load(["AAA", "MISSING"])) == {"AAA"}

    def test_empty_frames_are_not_cached(self):
        # Caching a delisted ticker's empty result just serves emptiness faster.
        assert cache.store({"DEAD": pd.DataFrame()}) == 0
        assert cache.load(["DEAD"]) == {}

    def test_a_different_session_date_is_a_different_generation(self):
        cache.store({"AAA": make_frame()}, day="2026-01-01")
        assert cache.load(["AAA"], day="2026-01-01")
        assert cache.load(["AAA"], day="2026-01-02") == {}

    def test_corrupt_entry_is_skipped_not_fatal(self):
        from raanu import state
        state.save(cache._key("AAA", cache.session_date()), {"bars": "not-gzip"})
        assert cache.load(["AAA"]) == {}

    def test_disabled_cache_stores_and_loads_nothing(self, monkeypatch):
        monkeypatch.setenv("BARS_CACHE", "0")
        assert cache.store({"AAA": make_frame()}) == 0
        assert cache.load(["AAA"]) == {}


class TestGetBars:
    def test_cold_cache_downloads_everything_then_caches_it(self):
        calls = []

        def downloader(tickers):
            calls.append(list(tickers))
            return {t: make_frame(seed=hash(t) % 100) for t in tickers}

        got = cache.get_bars(["AAA", "BBB"], downloader=downloader)
        assert set(got) == {"AAA", "BBB"}
        assert calls == [["AAA", "BBB"]]
        # Second call must be served entirely from cache.
        calls.clear()
        again = cache.get_bars(["AAA", "BBB"], downloader=downloader)
        assert set(again) == {"AAA", "BBB"}
        assert calls == [], "warm cache must not download"

    def test_partial_hit_downloads_only_the_misses(self):
        # This is the whole point: the scheduled scan warms most of the
        # universe, and a later scan pays only for what changed.
        cache.store({"AAA": make_frame(seed=1)})
        asked = []

        def downloader(tickers):
            asked.extend(tickers)
            return {t: make_frame(seed=9) for t in tickers}

        got = cache.get_bars(["AAA", "BBB", "CCC"], downloader=downloader)
        assert asked == ["BBB", "CCC"], "cached ticker must not be re-downloaded"
        assert set(got) == {"AAA", "BBB", "CCC"}

    def test_empty_input(self):
        assert cache.get_bars([]) == {}

    def test_download_failure_does_not_lose_the_cache_hits(self):
        cache.store({"AAA": make_frame()})

        def downloader(tickers):
            return {}

        got = cache.get_bars(["AAA", "BBB"], downloader=downloader)
        assert set(got) == {"AAA"}


class TestSessionDate:
    def test_keyed_on_eastern_not_utc(self, monkeypatch):
        # A UTC key would split one ET trading session across two cache
        # generations for anything running after 20:00 ET.
        from datetime import datetime
        from zoneinfo import ZoneInfo

        class FakeDT(datetime):
            @classmethod
            def now(cls, tz=None):
                # 00:30 UTC on the 2nd == 19:30 ET on the 1st.
                return datetime(2026, 1, 2, 0, 30, tzinfo=ZoneInfo("UTC")).astimezone(tz)

        monkeypatch.setattr(cache, "datetime", FakeDT)
        assert cache.session_date() == "2026-01-01"
