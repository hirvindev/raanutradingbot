"""raanu.trading.schedule_rule — the EventBridge switch behind SCHEDULE ON/OFF.

Exercised against a real (moto) EventBridge rule rather than a mocked
boto3 client, so a wrong action name or parameter would actually fail these
tests instead of the mock silently agreeing with it.
"""

from __future__ import annotations

import pytest

RULE_NAME = "test-worker-schedule"


@pytest.fixture
def events_rule(monkeypatch):
    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")

    with moto.mock_aws():
        monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        monkeypatch.setenv("WORKER_SCHEDULE_RULE_NAME", RULE_NAME)
        client = boto3.client("events", region_name="eu-central-1")
        client.put_rule(Name=RULE_NAME, ScheduleExpression="rate(5 minutes)", State="DISABLED")
        yield client


class TestNotConfigured:
    """No WORKER_SCHEDULE_RULE_NAME — local dev, or a stack predating this
    feature. Must report itself as unavailable, never crash or claim ON."""

    def test_status_reports_unconfigured(self, monkeypatch):
        from raanu.trading import schedule_rule
        monkeypatch.delenv("WORKER_SCHEDULE_RULE_NAME", raising=False)
        assert schedule_rule.status() == {"configured": False, "enabled": False}

    def test_set_enabled_raises_rather_than_silently_no_op(self, monkeypatch):
        from raanu.trading import schedule_rule
        monkeypatch.delenv("WORKER_SCHEDULE_RULE_NAME", raising=False)
        with pytest.raises(RuntimeError):
            schedule_rule.set_enabled(True)


class TestConfigured:
    def test_status_reflects_the_real_rule_state(self, events_rule):
        from raanu.trading import schedule_rule
        assert schedule_rule.status() == {"configured": True, "enabled": False}

    def test_enable_flips_the_real_rule(self, events_rule):
        from raanu.trading import schedule_rule
        result = schedule_rule.set_enabled(True)
        assert result == {"configured": True, "enabled": True}
        assert events_rule.describe_rule(Name=RULE_NAME)["State"] == "ENABLED"

    def test_disable_flips_the_real_rule(self, events_rule):
        from raanu.trading import schedule_rule
        schedule_rule.set_enabled(True)
        result = schedule_rule.set_enabled(False)
        assert result == {"configured": True, "enabled": False}
        assert events_rule.describe_rule(Name=RULE_NAME)["State"] == "DISABLED"

    def test_a_describe_failure_reads_as_disabled_not_a_crash(self, events_rule, monkeypatch):
        """Fail closed, matching the auto-trader's own enabled-flag property:
        a state-read blip must never be read as ON."""
        from raanu.trading import schedule_rule
        monkeypatch.setenv("WORKER_SCHEDULE_RULE_NAME", "does-not-exist")
        result = schedule_rule.status()
        assert result["configured"] is True
        assert result["enabled"] is False
