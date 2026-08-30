"""State persistence, run against both backends.

The DynamoDB cases use moto (a real in-process DynamoDB implementation)
rather than a mocked boto3 client, so a wrong key schema or a bad item shape
fails here instead of on deploy.
"""

from __future__ import annotations

from raanu import state


class TestFileBackend:
    def test_round_trip(self):
        state.save("trades_log.json", {"trades": [{"symbol": "NVDA"}]})
        assert state.load("trades_log.json") == {"trades": [{"symbol": "NVDA"}]}

    def test_missing_key_returns_default(self):
        assert state.load("nope.json", default={"trades": []}) == {"trades": []}
        assert state.load("nope.json") is None

    def test_overwrite_replaces_rather_than_merges(self):
        state.save("k", {"a": 1, "b": 2})
        state.save("k", {"a": 9})
        assert state.load("k") == {"a": 9}

    def test_namespaced_keys_do_not_collide(self):
        # Scan shards use "scan/<run>/shard/<i>" keys, which are a flat
        # partition key on DynamoDB but nested directories on a filesystem.
        state.save("scan/run-a/shard/0", {"hits": 1})
        state.save("scan/run-a/shard/1", {"hits": 2})
        state.save("scan/run-b/shard/0", {"hits": 3})
        assert state.load("scan/run-a/shard/0") == {"hits": 1}
        assert state.load("scan/run-a/shard/1") == {"hits": 2}
        assert state.load("scan/run-b/shard/0") == {"hits": 3}

    def test_corrupt_payload_returns_default_instead_of_raising(self, tmp_path, monkeypatch):
        state.save("broken.json", {"ok": True})
        target = state.resolve_data_dir() / "broken.json"
        target.write_text("{ this is not json")
        assert state.load("broken.json", default={"fallback": True}) == {"fallback": True}

    def test_load_many(self):
        state.save("a", {"n": 1})
        state.save("b", {"n": 2})
        got = state.load_many(["a", "b", "missing"])
        assert got == {"a": {"n": 1}, "b": {"n": 2}}

    def test_delete(self):
        state.save("gone", {"x": 1})
        state.delete("gone")
        assert state.load("gone") is None
        state.delete("gone")  # idempotent


class TestDynamoBackend:
    def test_round_trip(self, dynamo_table):
        state.save("trades_log.json", {"trades": [{"symbol": "MU"}]})
        assert state.load("trades_log.json") == {"trades": [{"symbol": "MU"}]}

    def test_missing_key_returns_default(self, dynamo_table):
        assert state.load("absent", default={"d": 1}) == {"d": 1}

    def test_load_many_batches(self, dynamo_table):
        for i in range(12):
            state.save(f"scan/r1/shard/{i}", {"shard": i})
        got = state.load_many([f"scan/r1/shard/{i}" for i in range(12)])
        assert len(got) == 12
        assert got["scan/r1/shard/7"] == {"shard": 7}

    def test_load_many_over_the_100_key_batch_limit(self, dynamo_table):
        # BatchGetItem caps at 100 keys; the backend must page, not truncate.
        for i in range(150):
            state.save(f"k{i}", {"i": i})
        got = state.load_many([f"k{i}" for i in range(150)])
        assert len(got) == 150

    def test_load_many_empty_is_a_noop(self, dynamo_table):
        assert state.load_many([]) == {}

    def test_ttl_attribute_written_only_when_requested(self, dynamo_table):
        import boto3

        state.save("with_ttl", {"a": 1}, ttl_seconds=3600)
        state.save("no_ttl", {"a": 1})
        table = boto3.resource("dynamodb", region_name="eu-central-1").Table(dynamo_table)
        assert "ttl" in table.get_item(Key={"state_key": "with_ttl"})["Item"]
        assert "ttl" not in table.get_item(Key={"state_key": "no_ttl"})["Item"]

    def test_delete(self, dynamo_table):
        state.save("bye", {"x": 1})
        state.delete("bye")
        assert state.load("bye") is None


class TestBackendSelection:
    def test_defaults_to_file_backend(self):
        from raanu.state.backends import FileBackend
        assert isinstance(state._active(), FileBackend)

    def test_switches_when_env_changes(self, monkeypatch):
        from raanu.state.backends import DynamoBackend, FileBackend
        assert isinstance(state._active(), FileBackend)
        monkeypatch.setenv("STATE_BACKEND", "dynamodb")
        monkeypatch.setenv("STATE_TABLE", "some-table")
        assert isinstance(state._active(), DynamoBackend)


class TestDataDirResolution:
    def test_explicit_data_dir_wins(self, tmp_path, monkeypatch):
        target = tmp_path / "chosen"
        monkeypatch.setenv("DATA_DIR", str(target))
        state.reset()
        assert state.resolve_data_dir() == target

    def test_unwritable_data_dir_falls_back_rather_than_crashing(self, monkeypatch):
        # A bad DATA_DIR must degrade, not take the process down on boot.
        monkeypatch.setenv("DATA_DIR", "/proc/cannot-create-here")
        state.reset()
        assert state.resolve_data_dir() != "/proc/cannot-create-here"
