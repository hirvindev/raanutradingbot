"""The state layer: composite keys, queries, size guard, backend parity.

Most of this runs against BOTH backends via the ``any_backend`` fixture. That
is deliberate — the file backend stores plain JSON while the Dynamo one runs
the float<->Decimal converter, so "they agree" is a property that can silently
break, and it is the property every caller relies on.
"""

from __future__ import annotations

import pytest

from raanu import state
from raanu.state import keys
from raanu.state.backends import MAX_ITEM_BYTES, StateItemTooLarge, estimate_size

TRADE = keys.TRADE


class TestRoundTrip:
    def test_put_then_get(self, any_backend):
        state.put(TRADE, "a", {"ticker": "NVDA", "notional_usd": 1234.56})
        assert state.get(TRADE, "a") == {"ticker": "NVDA", "notional_usd": 1234.56}

    def test_a_missing_record_returns_the_default(self, any_backend):
        assert state.get(TRADE, "nope") is None
        assert state.get(TRADE, "nope", default={"x": 1}) == {"x": 1}

    def test_floats_survive_on_both_backends(self, any_backend):
        """The gap the old suite had: every stored value was an int or string,
        so nothing ever exercised the number path."""
        payload = {"price": 90.27, "atr": 5.33, "fwd": {"d1": -2.05},
                   "score": 78, "uptrend": True, "name": None}
        state.put(TRADE, "f", payload)
        assert state.get(TRADE, "f") == payload

    def test_put_overwrites_rather_than_merging(self, any_backend):
        state.put(TRADE, "a", {"one": 1, "two": 2})
        state.put(TRADE, "a", {"one": 9})
        assert state.get(TRADE, "a") == {"one": 9}

    def test_entities_are_isolated_from_each_other(self, any_backend):
        state.put(TRADE, "x", {"n": 1})
        state.put(keys.PICK, "x", {"n": 2})
        assert state.get(TRADE, "x") == {"n": 1}
        assert state.get(keys.PICK, "x") == {"n": 2}

    def test_sort_keys_with_awkward_characters(self, any_backend):
        """Real sort keys carry ':' from ISO timestamps and '#' as a
        separator. The file backend turns these into filenames."""
        sk = "2026-09-08T15:52:19.736936+00:00#a1b2c3"
        state.put(TRADE, sk, {"ok": True})
        assert state.get(TRADE, sk) == {"ok": True}
        assert state.query(TRADE)[0].sk == sk


class TestQuery:
    @pytest.fixture(autouse=True)
    def _seed(self, any_backend):
        for sk, data in [
            ("2026-09-01#a", {"action": "BUY", "strategy": "s1", "ticker": "AAA"}),
            ("2026-09-05#b", {"action": "SELL", "strategy": "s1", "ticker": "AAA"}),
            ("2026-09-08#c", {"action": "BUY", "strategy": "s3", "ticker": "BBB"}),
            ("2026-09-09#d", {"action": "BUY", "strategy": "s3", "ticker": "CCC"}),
        ]:
            state.put(TRADE, sk, data)

    def test_returns_everything_in_sort_order(self):
        assert [r.sk for r in state.query(TRADE)] == [
            "2026-09-01#a", "2026-09-05#b", "2026-09-08#c", "2026-09-09#d"]

    def test_descending(self):
        assert state.query(TRADE, descending=True)[0].sk == "2026-09-09#d"

    def test_sk_lower_bound_is_the_weekly_window(self):
        """The access pattern this whole model exists for: a key range, not a
        full walk with timestamp parsing."""
        rows = state.query(TRADE, sk_gte="2026-09-06")
        assert [r.sk for r in rows] == ["2026-09-08#c", "2026-09-09#d"]

    def test_sk_upper_bound(self):
        assert len(state.query(TRADE, sk_lte="2026-09-05#b")) == 2

    def test_sk_between(self):
        rows = state.query(TRADE, sk_gte="2026-09-04", sk_lte="2026-09-08#c")
        assert [r.sk for r in rows] == ["2026-09-05#b", "2026-09-08#c"]

    def test_prefix(self):
        assert len(state.query(TRADE, sk_prefix="2026-09-0")) == 4
        assert len(state.query(TRADE, sk_prefix="2026-09-08")) == 1

    def test_equality_filter_on_a_nested_field(self):
        rows = state.query(TRADE, filters={"strategy": "s3"})
        assert {r.data["ticker"] for r in rows} == {"BBB", "CCC"}

    def test_filters_combine(self):
        rows = state.query(TRADE, filters={"strategy": "s1", "action": "SELL"})
        assert len(rows) == 1 and rows[0].data["ticker"] == "AAA"

    def test_limit(self):
        assert len(state.query(TRADE, limit=2)) == 2

    def test_limit_counts_rows_that_survived_the_filter(self):
        """DynamoDB applies Limit BEFORE FilterExpression, so a naive
        implementation silently under-returns. Three BUYs exist; asking for
        three must return three, not "three read, then filtered down"."""
        assert len(state.query(TRADE, filters={"action": "BUY"}, limit=3)) == 3

    def test_projection_narrows_the_payload(self):
        rows = state.query(TRADE, project=["ticker"], limit=1)
        assert rows[0].data == {"ticker": "AAA"}

    def test_an_empty_entity_queries_clean(self):
        assert state.query(keys.NOTIF) == []


class TestGetMany:
    def test_reads_a_batch(self, any_backend):
        for i in range(12):
            state.put(keys.BARS, f"2026-09-08#T{i}", {"n": i})
        pairs = [(keys.BARS, f"2026-09-08#T{i}") for i in range(12)]
        assert len(state.get_many(pairs)) == 12

    def test_pages_past_the_100_key_batch_limit(self, any_backend):
        for i in range(150):
            state.put(keys.BARS, f"d#{i:03d}", {"i": i})
        got = state.get_many([(keys.BARS, f"d#{i:03d}") for i in range(150)])
        assert len(got) == 150, "BatchGetItem caps at 100 keys — must paginate"

    def test_missing_keys_are_omitted_not_errors(self, any_backend):
        state.put(keys.BARS, "here", {"n": 1})
        got = state.get_many([(keys.BARS, "here"), (keys.BARS, "gone")])
        assert got == {(keys.BARS, "here"): {"n": 1}}

    def test_empty_input(self, any_backend):
        assert state.get_many([]) == {}


class TestDelete:
    def test_delete_removes(self, any_backend):
        state.put(TRADE, "a", {"n": 1})
        state.delete(TRADE, "a")
        assert state.get(TRADE, "a") is None

    def test_delete_is_idempotent(self, any_backend):
        state.delete(TRADE, "never-existed")


class TestSizeGuard:
    """The old failure was silent: an oversized put_item raised inside a bare
    except that only logged, so the trade log would simply stop recording —
    which re-arms the weekly trade limit and resets Kelly's sample."""

    def test_a_huge_item_raises_instead_of_being_swallowed(self, any_backend):
        with pytest.raises(StateItemTooLarge):
            state.put(TRADE, "big", {"blob": "x" * (MAX_ITEM_BYTES + 1)})

    def test_the_guard_sits_below_dynamodbs_hard_limit(self):
        assert MAX_ITEM_BYTES < 400 * 1024

    def test_a_realistic_record_is_nowhere_near_the_guard(self, any_backend):
        trade = {"action": "BUY", "ticker": "PANW", "notional_usd": 3129.48,
                 "score": 84, "strategy": "s3", "entry_price": 334.16,
                 "reasons": ["a reason that is reasonably long"] * 6,
                 "alpaca_response": {"id": "x" * 36, "status": "filled"}}
        assert estimate_size(TRADE, keys.trade_sk(), trade) < MAX_ITEM_BYTES / 100

    def test_item_per_record_keeps_a_long_history_safe(self, any_backend):
        """400 trades used to be past the 400KB ceiling as one item. As 400
        items it is a non-event — this is the whole point of the remodel."""
        for i in range(400):
            state.put(TRADE, f"2026-09-08T00:00:00.{i:06d}+00:00#x",
                      {"action": "BUY", "ticker": "NVDA", "notional_usd": 1234.56,
                       "reasons": ["a plausible-length reason string"] * 5})
        rows = state.query(TRADE)
        assert len(rows) == 400
        assert max(estimate_size(TRADE, r.sk, r.data) for r in rows) < MAX_ITEM_BYTES


class TestConcurrentWriters:
    def test_two_writers_do_not_lose_each_others_records(self, any_backend):
        """The live bug this fixes. Both Lambdas held a full-object snapshot
        and rewrote it whole, so a BUY from the worker and a SELL from the API
        discarded one another. With one item per record there is nothing to
        overwrite."""
        state.put(TRADE, keys.trade_sk("2026-09-08T10:00:00.000000+00:00", "aaa"),
                  {"action": "BUY", "ticker": "NVDA"})
        state.put(TRADE, keys.trade_sk("2026-09-08T10:00:00.000000+00:00", "bbb"),
                  {"action": "SELL", "ticker": "AAPL"})
        assert len(state.query(TRADE)) == 2

    def test_same_microsecond_writes_both_survive(self):
        a = keys.trade_sk("2026-09-08T10:00:00.000000+00:00")
        b = keys.trade_sk("2026-09-08T10:00:00.000000+00:00")
        assert a != b, "the uid suffix is what stops a same-instant collision"


class TestSortKeyOrdering:
    """Several consumers rely on append order being chronological."""

    def test_timestamps_are_fixed_width(self):
        """isoformat() drops microseconds when they are zero, which changes
        the string width and breaks lexicographic ordering."""
        from datetime import UTC, datetime
        exact = datetime(2026, 9, 8, 10, 0, 0, 0, tzinfo=UTC)
        assert keys.stamp(exact) == "2026-09-08T10:00:00.000000+00:00"
        assert len(keys.stamp(exact)) == len(keys.now_stamp())

    def test_lexicographic_order_matches_chronological_order(self):
        from datetime import UTC, datetime
        stamps = [keys.stamp(datetime(2026, 9, d, h, 0, 0, us, tzinfo=UTC))
                  for d, h, us in [(1, 0, 0), (1, 0, 5), (1, 3, 0), (10, 0, 0)]]
        assert stamps == sorted(stamps)

    def test_naive_timestamps_are_treated_as_utc(self):
        from datetime import datetime
        assert keys.stamp(datetime(2026, 9, 8, 10, 0)).endswith("+00:00")


class TestTtl:
    def test_ttl_is_written_as_an_attribute(self, dynamo_table):
        import boto3
        state.put(keys.SCAN, "current", {"run": 1}, ttl_seconds=3600)
        item = boto3.resource("dynamodb", region_name="eu-central-1") \
            .Table(dynamo_table).get_item(Key={"pk": keys.SCAN, "sk": "current"})["Item"]
        assert "ttl" in item

    def test_no_ttl_by_default(self, dynamo_table):
        import boto3
        state.put(TRADE, "a", {"n": 1})
        item = boto3.resource("dynamodb", region_name="eu-central-1") \
            .Table(dynamo_table).get_item(Key={"pk": TRADE, "sk": "a"})["Item"]
        assert "ttl" not in item

    def test_the_file_backend_hides_expired_records(self, monkeypatch, tmp_path):
        """DynamoDB reclaims lazily but never serves an expired item. The file
        backend does not sweep, so it must at least not hand one back — or the
        two backends disagree."""
        monkeypatch.setenv("STATE_BACKEND", "file")
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        state.reset()
        state.put(keys.SCAN, "old", {"n": 1}, ttl_seconds=-1)
        assert state.get(keys.SCAN, "old") is None
        assert state.query(keys.SCAN) == []


class TestNativeStorage:
    def test_data_is_a_map_not_a_string(self, dynamo_table):
        """The change this all rests on. `data` used to be one escaped JSON
        string, which made the payload opaque to filters, projections and
        anyone reading the console."""
        import boto3
        state.put(TRADE, "a", {"ticker": "NVDA", "price": 90.27})
        raw = boto3.client("dynamodb", region_name="eu-central-1").get_item(
            TableName=dynamo_table, Key={"pk": {"S": TRADE}, "sk": {"S": "a"}})["Item"]
        assert "M" in raw["data"], "data must be a native map"
        assert raw["data"]["M"]["price"]["N"] == "90.27"


class TestBackendSelection:
    def test_dynamodb_when_configured(self, monkeypatch):
        from raanu.state.backends import DynamoBackend
        monkeypatch.setenv("STATE_BACKEND", "dynamodb")
        monkeypatch.setenv("STATE_TABLE", "t")
        state.reset()
        assert isinstance(state._active(), DynamoBackend)

    def test_file_otherwise(self, monkeypatch):
        from raanu.state.backends import FileBackend
        monkeypatch.setenv("STATE_BACKEND", "file")
        state.reset()
        assert isinstance(state._active(), FileBackend)


class TestDataDirResolution:
    def test_data_dir_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        state.reset()
        assert state.resolve_data_dir() == tmp_path

    def test_unwritable_data_dir_falls_back(self, monkeypatch):
        monkeypatch.setenv("DATA_DIR", "/proc/nope/nowhere")
        state.reset()
        assert state.resolve_data_dir() != "/proc/nope/nowhere"
