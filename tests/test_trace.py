"""The decision journal.

Three properties matter more than the contents: it can never break a trade, it
expires itself, and reading a week back is a bounded query rather than a scan.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from raanu import state, trace
from raanu.state import keys


class TestNeverBreaksATrade:
    """A journal that can take down the thing it observes is worse than none."""

    def test_a_broken_backend_does_not_raise(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("state store unreachable")

        monkeypatch.setattr(state, "put", boom)
        trace.emit("order.placed", ticker="NVDA")   # must not raise

    def test_an_unserialisable_value_does_not_raise(self):
        trace.emit("scan.done", weird=object(), fn=lambda x: x)

    def test_reads_survive_a_broken_backend(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("state store unreachable")

        monkeypatch.setattr(state, "query", boom)
        assert trace.window(7) == []
        assert trace.for_day("2026-09-08") == []


class TestRetention:
    def test_every_row_carries_a_ttl(self, monkeypatch):
        seen = {}

        def capture(pk, sk, data, *, ttl_seconds=None):
            seen["ttl"] = ttl_seconds

        monkeypatch.setattr(state, "put", capture)
        trace.emit("scan.done")
        # Nothing else prunes traces; without a TTL the table grows forever.
        assert seen["ttl"] == 90 * 86400

    def test_retention_is_configurable(self, monkeypatch):
        seen = {}
        monkeypatch.setenv("TRACE_RETAIN_DAYS", "7")
        monkeypatch.setattr(state, "put",
                            lambda pk, sk, d, *, ttl_seconds=None: seen.update(ttl=ttl_seconds))
        trace.emit("scan.done")
        assert seen["ttl"] == 7 * 86400


class TestDisabling:
    def test_nothing_is_written_when_disabled(self, monkeypatch):
        monkeypatch.setenv("TRACE_ENABLED", "0")
        monkeypatch.setattr(state, "put",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("wrote")))
        trace.emit("scan.done")

    def test_enabled_by_default(self):
        # The one flag in this feature that defaults on: everything else here
        # is undebuggable without it.
        from raanu import config
        assert config.trace_enabled() is True


class TestReadBack:
    def test_round_trips_the_fields(self):
        trace.emit("order.sized", slot="Open-9:35", strategy="s3",
                   ticker="NVDA", notional=412.5)
        rows = trace.window(1)
        assert len(rows) == 1
        assert rows[0]["event"] == "order.sized"
        assert rows[0]["ticker"] == "NVDA"
        assert rows[0]["notional"] == 412.5
        assert rows[0]["strategy"] == "s3"

    def test_window_is_a_bounded_range_query_not_a_scan(self, monkeypatch):
        """The whole storage design is that a week is a key range. A scan
        would work today and fall over once there is a year of history."""
        seen = {}

        def capture(pk, **kw):
            seen.update(kw)
            return []

        monkeypatch.setattr(state, "query", capture)
        trace.window(7)
        assert seen.get("sk_gte") and seen.get("sk_lte"), "window() is not key-bounded"
        assert seen.get("sk_prefix") is None

        expected = (datetime.now(UTC).date() - timedelta(days=6)).isoformat()
        assert seen["sk_gte"].startswith(expected)

    def test_a_day_outside_the_window_is_excluded(self):
        old = (datetime.now(UTC).date() - timedelta(days=30)).isoformat()
        state.put(keys.TRACE, keys.trace_sk(old, f"{old}T00:00:00.000000+00:00", "scan.done"),
                  {"event": "scan.done", "day": old, "ticker": "OLD"})
        trace.emit("scan.done", ticker="NEW")

        tickers = [r.get("ticker") for r in trace.window(7)]
        assert "NEW" in tickers and "OLD" not in tickers
        # ...but it is still there when asked for directly.
        assert [r.get("ticker") for r in trace.for_day(old)] == ["OLD"]

    def test_summarise_counts_by_event(self):
        trace.emit("scan.done", strategy="s1")
        trace.emit("scan.done", strategy="s3")
        trace.emit("order.placed", ticker="NVDA")
        summary = trace.summarise(1)
        assert summary["total"] == 3
        assert summary["by_event"]["scan.done"] == 2
        assert summary["by_event"]["order.placed"] == 1


class TestBoundedRows:
    def test_a_huge_string_is_truncated(self):
        trace.emit("llm.response", market_summary="x" * 50_000)
        stored = trace.window(1)[0]["market_summary"]
        assert len(stored) < 5_000
        assert "chars]" in stored

    def test_a_huge_list_is_truncated(self):
        trace.emit("filter.actionable", dropped=[{"t": i} for i in range(5_000)])
        assert len(trace.window(1)[0]["dropped"]) == 100
