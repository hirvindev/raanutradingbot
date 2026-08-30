"""Shared fixtures.

Every test runs against an isolated state directory and a clean environment.
The env-clearing matters more than it looks: this codebase's config used to
be frozen into module globals at import, and the whole point of the rewrite
is that it is now readable per call — a test that inherited the developer's
real ALPACA_API_KEY would hide exactly the bug we are guarding against.
"""

from __future__ import annotations

import pytest

from raanu import config, state

# Anything the application reads that could leak in from the developer's
# shell or a .env file and change a test's outcome.
_APP_ENV_PREFIXES = (
    "ALPACA_", "TELEGRAM_", "TWILIO_", "VAPID_", "FCM_", "KELLY_", "STOP_",
    "TRAIL_", "PROFIT_", "SCAN_", "STATE_", "WEEKLY_", "PER_TRADE_", "CASH_",
    "PUSH_", "NOTIF_", "TWA_", "AUTO_TRADE_", "API_READ_TOKEN", "TRADE_PIN",
    "ALLOWED_ORIGINS", "DATA_DIR", "WORKER_FUNCTION_NAME", "MIN_SIGNAL_SCORE",
    "MAX_POSITION_PCT", "WATCHLIST", "HARD_TAKE_PROFIT_PCT", "DAILY_CRASH_PCT",
    "TAKE_PROFIT_PCT", "TRADELOG_SEED",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    import os

    for name in list(os.environ):
        if name.startswith(_APP_ENV_PREFIXES):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "state"))
    config.reset_exit_config()
    state.reset()
    yield
    config.reset_exit_config()
    state.reset()


@pytest.fixture
def dynamo_table(monkeypatch):
    """A real DynamoDB table, in-process, via moto — so the DynamoDB backend
    is exercised for real rather than mocked into always agreeing with us."""
    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")

    with moto.mock_aws():
        monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        client = boto3.client("dynamodb", region_name="eu-central-1")
        client.create_table(
            TableName="raanu-test-state",
            KeySchema=[{"AttributeName": "state_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "state_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setenv("STATE_BACKEND", "dynamodb")
        monkeypatch.setenv("STATE_TABLE", "raanu-test-state")
        state.reset()
        yield "raanu-test-state"
        state.reset()
