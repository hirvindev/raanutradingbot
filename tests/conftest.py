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
    "ALLOWED_ORIGINS", "DATA_DIR", "WORKER_FUNCTION_NAME", "WORKER_SCHEDULE_RULE_NAME",
    "MIN_SIGNAL_SCORE",
    # LLM_ especially: LLM_API_KEY is a real, billable credential, so a
    # developer with it exported would otherwise have the suite calling the
    # live API — the exact leak this tuple exists to prevent.
    "LLM_", "TRACE_",
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
    is exercised for real rather than mocked into always agreeing with us.

    Composite pk+sk, matching the deployed table. That matters beyond fidelity:
    boto3 raises on a float *client-side*, before moto ever sees the request,
    so this fixture catches a converter regression for real.
    """
    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")

    with moto.mock_aws():
        monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        client = boto3.client("dynamodb", region_name="eu-central-1")
        client.create_table(
            TableName="raanu-test-state",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                       {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                  {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setenv("STATE_BACKEND", "dynamodb")
        monkeypatch.setenv("STATE_TABLE", "raanu-test-state")
        state.reset()
        yield "raanu-test-state"
        state.reset()


@pytest.fixture(params=["file", "dynamodb"])
def any_backend(request, monkeypatch, tmp_path):
    """Runs a test body against BOTH backends.

    They have to answer identically, and that is easy to break now that only
    one of them runs the float<->Decimal converter — the file backend stores
    plain JSON. Parametrising here means a divergence fails a test rather than
    waiting to surface in production.
    """
    if request.param == "file":
        monkeypatch.setenv("STATE_BACKEND", "file")
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "filestate"))
        state.reset()
        yield "file"
        state.reset()
        return

    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")
    with moto.mock_aws():
        monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        boto3.client("dynamodb", region_name="eu-central-1").create_table(
            TableName="raanu-test-state",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                       {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                  {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setenv("STATE_BACKEND", "dynamodb")
        monkeypatch.setenv("STATE_TABLE", "raanu-test-state")
        state.reset()
        yield "dynamodb"
        state.reset()
