"""Runtime-editable trading limits.

These are the numbers that bound what the bot may spend, and they can now be
changed without a deploy — so the interesting cases are not "does it store a
number" but what happens when the store is wrong, unreachable, or asked for
something absurd.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from raanu import config, settings, state
from raanu.api.app import create_app
from raanu.state import keys


@pytest.fixture
def client():
    # Unsecured: the gate is off when API_READ_TOKEN is unset, which conftest
    # guarantees. The PIN classification is asserted directly instead.
    return TestClient(create_app(), raise_server_exceptions=False)


class TestResolutionOrder:
    """stored -> env -> default, and you can tell which one answered."""

    def test_the_default_is_the_new_pooled_shape(self):
        # $2,000 x 10 trades = the $20,000 week exactly, so the count and the
        # dollars run out together instead of one making the other
        # unreachable — the mismatch the old $5,000 S3 cap created.
        assert settings.get("weekly_budget_usd") == 20000.0
        assert settings.get("weekly_trade_limit") == 10
        assert settings.get("per_trade_max_usd") == 2000.0

    def test_env_overrides_the_default(self, monkeypatch):
        monkeypatch.setenv("WEEKLY_BUDGET_USD", "5000")
        settings.reset()
        assert settings.get("weekly_budget_usd") == 5000.0

    def test_the_store_overrides_the_env(self, monkeypatch):
        monkeypatch.setenv("WEEKLY_BUDGET_USD", "5000")
        settings.reset()
        settings.put("weekly_budget_usd", 9000)
        assert settings.get("weekly_budget_usd") == 9000.0

    def test_clearing_falls_back_to_env(self, monkeypatch):
        monkeypatch.setenv("WEEKLY_BUDGET_USD", "5000")
        settings.put("weekly_budget_usd", 9000)
        settings.clear("weekly_budget_usd")
        assert settings.get("weekly_budget_usd") == 5000.0

    def test_the_snapshot_says_where_each_value_came_from(self, monkeypatch):
        # "Why is the budget 20,000" must be answerable without guessing
        # whether a UI change actually landed.
        settings.put("weekly_trade_limit", 4)
        snap = settings.snapshot()
        assert snap["weekly_trade_limit"]["source"] == "store"
        assert snap["weekly_budget_usd"]["source"] == "default"
        monkeypatch.setenv("PER_TRADE_MAX_USD", "750")
        settings.reset()
        assert settings.snapshot()["per_trade_max_usd"]["source"] == "env"


class TestBoundsAreEnforcedBothWays:
    """A UI field that accepts 20000000 is a foot-gun, and a corrupted row
    must not authorise spending no human chose."""

    def test_a_write_above_the_ceiling_is_clamped(self):
        stored = settings.put("weekly_budget_usd", 50_000_000)
        assert stored == 1_000_000.0
        assert settings.get("weekly_budget_usd") == 1_000_000.0

    def test_a_negative_write_is_clamped_to_zero(self):
        assert settings.put("weekly_trade_limit", -5) == 0

    def test_a_row_that_is_already_out_of_range_is_clamped_on_READ(self):
        # Written around the API — a corrupted or hand-edited row.
        state.put(keys.SETTING, keys.setting_sk("weekly_trade_limit"),
                  {"value": 9999})
        settings.reset()
        assert settings.get("weekly_trade_limit") == 100

    def test_an_unparseable_row_falls_back_rather_than_crashing(self):
        state.put(keys.SETTING, keys.setting_sk("weekly_budget_usd"),
                  {"value": "twenty thousand"})
        settings.reset()
        assert settings.get("weekly_budget_usd") == 20000.0

    def test_an_unknown_name_is_rejected_not_ignored(self):
        # A typo in a UI form must not create a setting nothing reads, which
        # looks like it worked and does nothing.
        with pytest.raises(KeyError):
            settings.put("weekly_budget_usdd", 5)
        with pytest.raises(KeyError):
            settings.get("nonsense")


class TestFailureKeepsTheSaferNumber:
    """🔴 A read failure must not quietly undo a deliberate restriction."""

    def test_it_keeps_the_last_known_value_not_the_default(self, monkeypatch):
        # The owner turned the budget DOWN to 3,000. The default is 20,000.
        # Reverting to the default on a blip would re-arm the larger number.
        settings.put("weekly_budget_usd", 3000)
        assert settings.get("weekly_budget_usd") == 3000.0

        monkeypatch.setattr(settings, "_TTL", -1)   # force a refetch
        monkeypatch.setattr(settings, "_from_store",
                            lambda name: (_ for _ in ()).throw(RuntimeError("down")))
        assert settings.get("weekly_budget_usd") == 3000.0

    def test_with_no_cache_at_all_it_falls_back_rather_than_raising(self, monkeypatch):
        # Cold container plus an outage: a limit that cannot be read is worth
        # a warning, not a dead slot — the weekly budget's own fail-closed
        # gate is what stops trading, and it reads the trade log, not this.
        monkeypatch.setattr(settings, "_from_store",
                            lambda name: (_ for _ in ()).throw(RuntimeError("down")))
        settings.reset()
        assert settings.get("weekly_budget_usd") == 20000.0


class TestConfigReadsThroughIt:
    def test_config_sees_a_stored_change(self):
        settings.put("weekly_budget_usd", 12345)
        assert config.weekly_budget_usd() == 12345.0

    def test_the_trade_limit_is_store_backed(self):
        settings.put("weekly_trade_limit", 3)
        assert config.weekly_trade_limit() == 3

    def test_the_per_trade_cap_is_one_number_for_every_strategy(self):
        # The old per-strategy defaults (s1 1000, s2 100, s3 5000) predate the
        # pooled budget and fought with it.
        for strategy in ("s1", "s2", "s3", ""):
            assert config.per_trade_max_usd(strategy) == 2000.0

    def test_a_per_strategy_env_override_still_wins(self, monkeypatch):
        monkeypatch.setenv("PER_TRADE_MAX_USD_S2", "250")
        assert config.per_trade_max_usd("s2") == 250.0
        assert config.per_trade_max_usd("s3") == 2000.0


class TestTheApi:
    def test_read_reports_every_setting(self, client):
        body = client.get("/api/settings").json()["settings"]
        assert set(body) == set(settings.SPECS)
        assert body["weekly_budget_usd"]["value"] == 20000.0

    def test_a_write_applies_and_echoes_what_took_effect(self, client):
        r = client.put("/api/settings", json={"values": {"weekly_trade_limit": 5}})
        assert r.status_code == 200
        assert r.json()["applied"]["weekly_trade_limit"] == 5
        assert config.weekly_trade_limit() == 5

    def test_the_response_is_the_authority_on_what_was_stored(self, client):
        # Clamped, so a UI must render what comes back, not what it sent.
        r = client.put("/api/settings", json={"values": {"weekly_budget_usd": 99_000_000}})
        assert r.json()["applied"]["weekly_budget_usd"] == 1_000_000.0

    def test_an_unknown_name_is_a_400(self, client):
        r = client.put("/api/settings", json={"values": {"nope": 1}})
        assert r.status_code == 400

    def test_writes_are_classified_as_money_moving(self):
        # Places no order itself, but raising the weekly budget is exactly how
        # an order gets placed. The PIN guards money, not HTTP verbs.
        from raanu.api import auth
        assert auth.needs_trade_pin("/api/settings")
        assert auth.needs_trade_pin("/api/settings/weekly_budget_usd")
