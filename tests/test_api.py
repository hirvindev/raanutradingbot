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

    @pytest.mark.parametrize("path", ["/api/orders/buy", "/api/orders/sell",
                                      "/api/auto/start", "/api/auto/stop",
                                      "/api/auto/scan-now", "/api/exit-config"])
    def test_money_moving_routes_need_the_trade_pin(self, secured, path):
        assert secured.post(path, headers=bearer(), json={}).status_code == 403

    @pytest.mark.parametrize("path", ["/api/scan/job", "/api/auto/scan-now/s2",
                                      "/api/picks/backfill"])
    def test_research_routes_do_not(self, secured, path):
        # This used to be the opposite. Gating by HTTP method meant running a
        # SCAN demanded the credential that can place trades, which defeats
        # the point of having a second secret at all.
        assert secured.post(path, headers=bearer(), json={}).status_code != 403

    def test_an_unclassified_write_route_still_fails_closed(self, secured):
        # The property deny-by-method gave us, kept: a route nobody
        # classified requires the PIN rather than skipping it. Full coverage
        # of the real route table is in test_auth_session.py.
        from raanu.api import auth
        assert auth.needs_trade_pin("/api/newly/invented") is True

    def test_the_html_shell_stays_public(self, secured):
        # It holds no data, and gating it would show a login prompt instead
        # of the page that knows how to ask for the token.
        assert secured.get("/").status_code == 200

    def test_twilio_webhook_stays_reachable(self, secured):
        # Outside /api/ so Twilio can reach it without credentials.
        assert secured.post("/webhook/whatsapp", data={"Body": "x", "From": "y"}).status_code != 401


class TestPublicRoutes:
    @pytest.mark.parametrize("path", ["/", "/legacy", "/sw.js",
                                      "/manifest.webmanifest"])
    def test_static_assets_resolve(self, client, path):
        # These are served relative to PROJECT_ROOT; the package move would
        # have broken every one of them silently.
        assert client.get(path).status_code == 200

    def test_kite_alias_redirects_to_the_dashboard(self, client):
        assert client.get("/kite", follow_redirects=False).status_code == 307

    def test_the_pwa_survived_the_android_cleanup(self, client):
        """sw.js and the manifest are the WEB dashboard's, not an app's — they
        are what makes it installable and what receives web push. Removing the
        Android apps must not have taken them along."""
        assert client.get("/sw.js").status_code == 200
        assert client.get("/manifest.webmanifest").status_code == 200
        assert client.get("/api/push/key").status_code == 200

    @pytest.mark.parametrize("path", ["/privacy", "/.well-known/assetlinks.json"])
    def test_android_only_routes_are_gone(self, client, path):
        # privacy.html served the Play Console Data safety declaration and
        # assetlinks.json was the TWA's Digital Asset Links proof. Both went
        # with the Android apps on 31 Aug 2026.
        assert client.get(path).status_code == 404


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

    def test_already_running_identifies_the_run_so_a_caller_can_attach(self, client, monkeypatch):
        # Without run_id here the dashboard polled for a run it never knew
        # the id of, spun 30s and reported "Failed to start" — while the
        # scan itself was running perfectly.
        monkeypatch.setenv("WORKER_FUNCTION_NAME", "raanu-worker")

        class FakeLambda:
            def invoke(self, **kw):
                return {"StatusCode": 202}

        import boto3
        monkeypatch.setattr(boto3, "client", lambda *a, **k: FakeLambda())

        first = client.post("/api/scan/job").json()
        second = client.post("/api/scan/job").json()
        assert second["status"] == "already_running"
        assert second["run_id"] == first["run_id"]
        assert second["total"] == first["total"]

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


class TestLockoutResponseShape:
    """The dashboard distinguishes an auth lockout from an infrastructure
    throttle by the response BODY, so the body has to stay distinguishable.

    It previously keyed off the 429 status alone, which meant an AWS Lambda
    ConcurrentInvocationLimitExceeded surfaced as "Too many failed attempts,
    try again in 15 minutes" — on a deployment with no passphrase set at all.
    """

    def test_auth_lockout_is_self_identifying(self, secured):
        from raanu.api import auth
        auth._AUTH_FAILS.clear()
        # Lockout counts DISTINCT wrong secrets, not attempts, so it takes
        # different guesses to trip it.
        last = None
        for i in range(auth._MAX_FAILS + 2):
            last = secured.get("/api/health", headers=bearer(f"wrong-{i}"))
        assert last.status_code == 429
        body = last.json()
        assert body["error"] == "too_many_attempts"
        assert isinstance(body["retry_after_sec"], int)
        auth._AUTH_FAILS.clear()

    def test_retyping_the_same_wrong_passphrase_does_not_lock_you_out(self, secured):
        # Deliberate: fat-fingering one passphrase repeatedly is a user, not
        # an attacker. Only many DIFFERENT guesses trip the lockout.
        from raanu.api import auth
        auth._AUTH_FAILS.clear()
        for _ in range(auth._MAX_FAILS * 3):
            assert secured.get("/api/health", headers=bearer("same")).status_code == 401
        auth._AUTH_FAILS.clear()

    def test_an_unset_passphrase_never_produces_a_lockout(self, client):
        # With API_READ_TOKEN unset the gate is skipped, so no amount of
        # requests should ever ask the user for a credential.
        for _ in range(30):
            assert client.get("/api/health").status_code == 200


class TestNoRouteReturns500:
    """Every GET route must respond without an unhandled exception.

    Added after /api/notifications shipped returning 500: it referenced
    push.NOTIF_RETAIN_HOURS, a module constant removed when push.py moved to
    lazy config. Ruff's F821 catches undefined *names* but not stale
    *attribute* access on a module, and no test hit that route — so it
    reached production and only surfaced in the browser's network tab.

    This walks the OpenAPI schema, so a new route is covered the day it is
    written rather than the day someone remembers to add it here.
    """

    @staticmethod
    def _gettable(app):
        for path, ops in app.openapi()["paths"].items():
            if "get" not in ops or "{" in path:      # skip path-param routes
                continue
            yield path

    def test_every_get_route_responds_without_a_server_error(self, client):
        failures = []
        for path in self._gettable(client.app):
            status = client.get(path).status_code
            # 4xx is legitimate here (no Alpaca key). A bare 500 is never
            # legitimate — it means the handler raised.
            if status == 500:
                failures.append(f"{path} -> {status}")
        assert not failures, "routes raising: " + ", ".join(failures)

    def test_the_walk_actually_covers_the_api(self, client):
        # Guards the guard: a broken _gettable would make the test above
        # pass vacuously.
        covered = list(self._gettable(client.app))
        assert len(covered) >= 20
        assert "/api/notifications" in covered
