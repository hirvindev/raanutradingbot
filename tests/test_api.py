"""API surface and the two-token auth gate.

The gate is the highest-stakes code in the app — it is the only thing
between the open internet and an endpoint that places orders — so it gets
tested by behaviour (what a request with these headers actually gets back)
rather than by inspecting the middleware.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from raanu.api.app import create_app

READ = "test-read-token"
PIN = "1234"


@pytest.fixture
def client():
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture
def secured(monkeypatch):
    monkeypatch.setenv("API_READ_TOKEN", READ)
    monkeypatch.setenv("TRADE_PIN", PIN)
    return TestClient(create_app(), raise_server_exceptions=False)


def bearer(token=READ):
    return {"Authorization": f"Bearer {token}"}


class TestRouteInventory:
    def test_every_expected_domain_is_mounted(self, client):
        paths = set(client.app.openapi()["paths"])
        for path in ("/api/health", "/api/portfolio", "/api/orders/buy",
                     "/api/auto/status", "/api/scan/job", "/api/push/key",
                     "/api/picks/outcomes", "/api/strategy/compare",
                     "/webhook/whatsapp", "/"):
            assert path in paths, f"{path} missing after the route split"

    def test_no_duplicate_operations(self, client):
        # Two routers accidentally claiming the same path would make which
        # handler wins depend on registration order.
        spec = client.app.openapi()["paths"]
        seen = [f"{m} {p}" for p, ops in spec.items() for m in ops]
        assert len(seen) == len(set(seen))


class TestGateDisabled:
    """With API_READ_TOKEN unset the gate is skipped, so a deploy cannot lock
    the owner out before the variable exists."""

    def test_api_is_reachable_without_credentials(self, client):
        assert client.get("/api/health").status_code == 200

    def test_health_reports_the_api_is_unsecured(self, client):
        assert client.get("/api/health").json()["key_configured"] is False


class TestGateEnabled:
    def test_read_requires_the_token(self, secured):
        assert secured.get("/api/health").status_code == 401

    def test_read_succeeds_with_the_token(self, secured):
        assert secured.get("/api/health", headers=bearer()).status_code == 200

    def test_wrong_token_is_rejected(self, secured):
        assert secured.get("/api/health", headers=bearer("nope")).status_code == 401

    def test_x_api_token_header_also_works(self, secured):
        assert secured.get("/api/health", headers={"X-Api-Token": READ}).status_code == 200

    def test_query_token_is_no_longer_accepted(self, secured):
        # ?token= existed only for EventSource, which cannot set headers.
        # The SSE route is gone, so this must not remain as a way to put a
        # credential into access logs and browser history.
        assert secured.get(f"/api/health?token={READ}").status_code == 401

    def test_writes_need_the_trade_pin_on_top_of_the_read_token(self, secured):
        assert secured.post("/api/auto/start", headers=bearer()).status_code == 403

    def test_writes_succeed_with_both_secrets(self, secured, monkeypatch):
        monkeypatch.setenv("ALPACA_API_KEY", "key")
        r = secured.post("/api/auto/start",
                         headers={**bearer(), "X-Trade-Token": PIN})
        assert r.status_code == 200

    def test_both_secrets_but_no_broker_key_is_a_400_not_a_403(self, secured):
        # The gate must have passed; the failure is the app's own missing
        # config, which is a different problem with a different fix.
        r = secured.post("/api/auto/start",
                         headers={**bearer(), "X-Trade-Token": PIN})
        assert r.status_code == 400

    def test_wrong_pin_is_rejected(self, secured):
        r = secured.post("/api/auto/start",
                         headers={**bearer(), "X-Trade-Token": "0000"})
        assert r.status_code == 403

    @pytest.mark.parametrize("path", ["/api/orders/buy", "/api/scan/job",
                                      "/api/auto/stop", "/api/exit-config"])
    def test_every_write_route_is_gated_by_method_not_a_path_list(self, secured, path):
        # Deny-by-method is the point: a new POST route is protected the day
        # it is written, not the day someone remembers to add it to a list.
        assert secured.post(path, headers=bearer(), json={}).status_code == 403

    def test_the_html_shell_stays_public(self, secured):
        # It holds no data, and gating it would show a login prompt instead
        # of the page that knows how to ask for the token.
        assert secured.get("/").status_code == 200

    def test_twilio_webhook_stays_reachable(self, secured):
        # Outside /api/ so Twilio can reach it without credentials.
        assert secured.post("/webhook/whatsapp", data={"Body": "x", "From": "y"}).status_code != 401


class TestPublicRoutes:
    @pytest.mark.parametrize("path", ["/", "/legacy", "/privacy", "/sw.js",
                                      "/manifest.webmanifest"])
    def test_static_assets_resolve(self, client, path):
        # These are served relative to PROJECT_ROOT; the package move would
        # have broken every one of them silently.
        assert client.get(path).status_code == 200

    def test_kite_alias_redirects_to_the_dashboard(self, client):
        assert client.get("/kite", follow_redirects=False).status_code == 307

    def test_assetlinks_refuses_to_serve_an_empty_list(self, client):
        # Deliberate: an empty [] looks like a valid answer to Chrome and
        # fails TWA verification silently, so unconfigured must be an error.
        assert client.get("/.well-known/assetlinks.json").status_code == 503

    def test_assetlinks_serves_the_fingerprints_when_configured(self, client, monkeypatch):
        monkeypatch.setenv("TWA_SHA256_FINGERPRINT", "AA:BB")
        body = client.get("/.well-known/assetlinks.json").json()
        assert body[0]["target"]["sha256_cert_fingerprints"] == ["AA:BB"]


class TestScanJobRoutes:
    def test_start_is_501_without_a_worker(self, client):
        r = client.post("/api/scan/job")
        assert r.status_code == 501
        assert "WORKER_FUNCTION_NAME" in r.json()["error"]

    def test_status_is_idle_before_any_run(self, client):
        assert client.get("/api/scan/job").json()["status"] == "idle"

    def test_start_dispatches_and_reports_the_run(self, client, monkeypatch):
        monkeypatch.setenv("WORKER_FUNCTION_NAME", "raanu-worker")
        monkeypatch.setenv("SCAN_SHARDS", "4")
        sent = []

        class FakeLambda:
            def invoke(self, **kw):
                sent.append(kw)
                return {"StatusCode": 202}

        import boto3
        monkeypatch.setattr(boto3, "client", lambda *a, **k: FakeLambda())

        body = client.post("/api/scan/job").json()
        assert body["status"] == "started"
        assert body["shards"] == 4 and body["mode"] == "fast"
        assert len(sent) == 4

    def test_cheap_mode_uses_a_single_shard(self, client, monkeypatch):
        monkeypatch.setenv("WORKER_FUNCTION_NAME", "raanu-worker")
        monkeypatch.setenv("SCAN_SHARDS", "8")

        class FakeLambda:
            def invoke(self, **kw):
                return {"StatusCode": 202}

        import boto3
        monkeypatch.setattr(boto3, "client", lambda *a, **k: FakeLambda())

        assert client.post("/api/scan/job?mode=cheap").json()["shards"] == 1

    def test_universes_endpoint_lists_curated_first(self, client):
        body = client.get("/api/scan/universes").json()
        assert body["default"] == "curated"
        assert body["universes"][0]["key"] == "curated"
        assert {"nasdaq", "nyse", "nyse_arca"} <= {u["key"] for u in body["universes"]}

    def test_universe_param_selects_what_gets_scanned(self, client, monkeypatch):
        monkeypatch.setenv("WORKER_FUNCTION_NAME", "raanu-worker")
        sent = []

        class FakeLambda:
            def invoke(self, **kw):
                sent.append(kw)
                return {"StatusCode": 202}

        import boto3
        monkeypatch.setattr(boto3, "client", lambda *a, **k: FakeLambda())

        body = client.post("/api/scan/job?universe=nyse_american").json()
        assert body["universe"] == "nyse_american"
        assert body["total"] == 274          # NYSE American, not the curated 470
        import json as _json
        dispatched = [t for c in sent for t in _json.loads(c["Payload"])["tickers"]]
        assert len(dispatched) == 274

    def test_a_second_start_while_running_does_not_fan_out_twice(self, client, monkeypatch):
        monkeypatch.setenv("WORKER_FUNCTION_NAME", "raanu-worker")
        monkeypatch.setenv("SCAN_SHARDS", "2")
        sent = []

        class FakeLambda:
            def invoke(self, **kw):
                sent.append(kw)
                return {"StatusCode": 202}

        import boto3
        monkeypatch.setattr(boto3, "client", lambda *a, **k: FakeLambda())

        client.post("/api/scan/job")
        assert client.post("/api/scan/job").json()["status"] == "already_running"
        assert len(sent) == 2, "a double-click must not dispatch a second fan-out"


class TestHealth:
    def test_reports_state_and_config(self, client):
        body = client.get("/api/health").json()
        assert body["status"] == "ok"
        assert body["mode"] == "paper"
        assert body["state"]["backend"] == "file"
        assert "data_dir" in body["state"]

    def test_reports_dynamodb_rather_than_a_filesystem_path_on_lambda(
            self, client, monkeypatch):
        # Showing "/tmp" and persistent:false while state actually lives in
        # DynamoDB reads as "your trade log is being thrown away".
        monkeypatch.setenv("STATE_BACKEND", "dynamodb")
        monkeypatch.setenv("STATE_TABLE", "raanu-state")
        state = client.get("/api/health").json()["state"]
        assert state == {"backend": "dynamodb", "table": "raanu-state", "persistent": True}

    def test_reports_the_min_score_actually_enforced(self, client):
        # This used to report 60 while the auto-trader gated at 70.
        from raanu import config
        assert int(client.get("/api/health").json()["config"]["min_signal_score"]) \
               == config.min_signal_score()
