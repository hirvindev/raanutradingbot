"""The session cookie — keeping the passphrase out of the browser.

The dashboard used to hold the raw ``API_READ_TOKEN`` in ``localStorage``
and replay it as a bearer token forever. These tests pin the replacement:
the passphrase is exchanged once for an HttpOnly cookie, and everything
that made the old scheme safe (the trade PIN on writes, the lockout, bearer
auth for the phone and curl) still holds.
"""

from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient

from raanu.api import auth
from raanu.api.app import create_app

READ = "a-memorable-passphrase"
PIN = "AB123456"

DASHBOARD = Path(__file__).resolve().parent.parent / "RaanuTradingBot.html"


@pytest.fixture
def secured(monkeypatch):
    monkeypatch.setenv("API_READ_TOKEN", READ)
    monkeypatch.setenv("TRADE_PIN", PIN)
    auth._AUTH_FAILS.clear()
    auth.reset_session_key()
    yield TestClient(create_app(), raise_server_exceptions=False)
    auth._AUTH_FAILS.clear()
    auth.reset_session_key()


def unlock(client, passphrase=READ):
    return client.post("/api/auth/session", json={"passphrase": passphrase})


class TestJwt:
    """The token itself. A JWT invites a specific family of attacks that an
    opaque HMAC string does not, so those get tested by name."""

    def test_a_minted_token_validates(self, secured):
        assert auth.valid_session(auth.mint_token()) is True

    def test_it_is_a_real_jwt_with_the_claims_we_expect(self, secured):
        claims = auth.verify_token(auth.mint_token())
        assert claims["iss"] == "raanu" and claims["sub"] == "owner"
        assert claims["exp"] - claims["iat"] == auth.SESSION_TTL_SEC
        assert jwt.get_unverified_header(auth.mint_token())["alg"] == "HS256"

    def test_garbage_is_rejected(self, secured):
        for bad in ("", "nonsense", "a.b.c", "...", "eyJhbGciOiJIUzI1NiJ9..",
                    "v1.9999999999.deadbeef"):
            assert auth.valid_session(bad) is False, bad

    def test_alg_none_is_rejected(self, secured):
        """The canonical JWT forgery: re-encode the claims with "alg":"none"
        and no signature. A verifier that trusts the token's own header
        accepts it. Ours pins algorithms=["HS256"], which is the only reason
        this fails."""
        claims = auth.verify_token(auth.mint_token())
        forged = jwt.encode(claims, key="", algorithm="none")
        assert auth.valid_session(forged) is False

    def test_a_token_signed_with_another_key_is_rejected(self, secured):
        claims = auth.verify_token(auth.mint_token())
        assert auth.valid_session(jwt.encode(claims, "not-our-key",
                                             algorithm="HS256")) is False

    def test_a_tampered_payload_is_rejected(self, secured):
        header, payload, sig = auth.mint_token().split(".")
        claims = auth.verify_token(auth.mint_token())
        longer = jwt.encode({**claims, "exp": claims["exp"] + 86400},
                            "not-our-key", algorithm="HS256").split(".")[1]
        assert auth.valid_session(f"{header}.{longer}.{sig}") is False

    def test_an_expired_token_is_rejected(self, secured):
        assert auth.valid_session(auth.mint_token(ttl_sec=-1)) is False

    def test_a_foreign_issuer_is_rejected(self, secured):
        now = int(time.time())
        other = jwt.encode({"iss": "somebody-else", "sub": "owner", "iat": now,
                            "exp": now + 3600},
                           auth._session_key(), algorithm="HS256")
        assert auth.valid_session(other) is False

    def test_a_token_without_an_expiry_is_rejected(self, secured):
        """require=["exp"] matters: PyJWT does not reject a token that simply
        omits exp, it just has nothing to check — which would mint forever."""
        forever = jwt.encode({"iss": "raanu", "sub": "owner"},
                             auth._session_key(), algorithm="HS256")
        assert auth.valid_session(forever) is False

    def test_the_token_never_contains_the_passphrase(self, secured):
        # A JWT payload is base64, NOT encryption — anyone can read the
        # claims. Nothing secret may go in it.
        token = auth.mint_token()
        assert READ not in token
        assert READ not in json.dumps(auth.verify_token(token))
        assert READ.encode() not in base64.urlsafe_b64decode(
            token.split(".")[1] + "==")

    def test_rotating_the_passphrase_invalidates_live_tokens(self, secured, monkeypatch):
        """The revocation story, and the reason the signing key is derived
        from the passphrase: `./aws/seed-secrets.sh API_READ_TOKEN` is all it
        takes to sign every client out."""
        token = auth.mint_token()
        assert auth.valid_session(token) is True
        monkeypatch.setenv("API_READ_TOKEN", "a-different-passphrase")
        assert auth.valid_session(token) is False

    def test_no_passphrase_configured_means_no_valid_tokens(self, secured, monkeypatch):
        token = auth.mint_token()
        monkeypatch.delenv("API_READ_TOKEN")
        assert auth.valid_session(token) is False


class TestLogin:
    def test_the_right_passphrase_returns_a_cookie(self, secured):
        r = unlock(secured)
        assert r.status_code == 200
        assert auth.SESSION_COOKIE in r.cookies

    def test_the_cookie_is_httponly_samesite_strict(self, secured):
        """HttpOnly is the entire point — it is what puts the credential out
        of reach of any script on the page. SameSite=Strict is what stops
        another site borrowing it."""
        header = unlock(secured).headers["set-cookie"].lower()
        assert "httponly" in header
        assert "samesite=strict" in header
        assert re.search(r"\bpath=/", header)

    def test_the_wrong_passphrase_is_401_and_sets_nothing(self, secured):
        r = unlock(secured, "wrong")
        assert r.status_code == 401
        assert auth.SESSION_COOKIE not in r.cookies

    def test_a_malformed_body_is_401_not_500(self, secured):
        r = secured.post("/api/auth/session", content=b"not json")
        assert r.status_code == 401

    def test_login_needs_no_credentials_of_its_own(self, secured):
        # Otherwise there would be no way in.
        assert unlock(secured).status_code == 200

    def test_failed_logins_count_toward_the_lockout(self, secured):
        for _ in range(auth._MAX_FAILS):
            unlock(secured, f"wrong-{time.time_ns()}")
        r = unlock(secured)
        assert r.status_code == 429
        assert r.json()["error"] == "too_many_attempts"

    def test_an_unprotected_server_says_so_rather_than_401ing(self, monkeypatch):
        """With no passphrase configured the gate is open. Returning 401 here
        would make the dashboard demand a secret that does not exist."""
        monkeypatch.delenv("API_READ_TOKEN", raising=False)
        client = TestClient(create_app(), raise_server_exceptions=False)
        body = client.post("/api/auth/session", json={"passphrase": ""}).json()
        assert body["protected"] is False


class TestCookieAuthenticates:
    def test_reads_work_after_unlocking(self, secured):
        assert secured.get("/api/health").status_code == 401
        unlock(secured)                       # TestClient keeps the cookie jar
        assert secured.get("/api/health").status_code == 200

    def test_logout_ends_it(self, secured):
        unlock(secured)
        assert secured.get("/api/health").status_code == 200
        secured.post("/api/auth/logout")
        assert secured.get("/api/health").status_code == 401

    def test_a_forged_cookie_does_not_authenticate(self, secured):
        secured.cookies.set(auth.SESSION_COOKIE, "v1.99999999999.deadbeef")
        assert secured.get("/api/health").status_code == 401

    def test_the_cookie_does_NOT_substitute_for_the_trade_pin(self, secured):
        """The whole two-secret design: a session proves you may look, never
        that you may spend. Also what makes the cookie CSRF-safe for writes —
        a cross-site request cannot set X-Trade-Token."""
        unlock(secured)
        assert secured.post("/api/auto/start").status_code == 403
        r = secured.post("/api/auto/start", headers={"X-Trade-Token": PIN})
        assert r.status_code != 403


class TestBearerJwt:
    """The reason for JWTs at all: a script should carry an expiring token,
    not the permanent passphrase."""

    def test_the_endpoint_hands_back_a_usable_token(self, secured):
        body = unlock(secured).json()
        assert body["token_type"] == "Bearer"
        assert body["expires_in"] == auth.SESSION_TTL_SEC
        # OAuth2 bearer field names (RFC 6749 §5.1), so ordinary HTTP client
        # libraries can read the response without special-casing us.
        token = body["access_token"]

        fresh = TestClient(create_app(), raise_server_exceptions=False)
        assert fresh.get("/api/health").status_code == 401
        r = fresh.get("/api/health", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200

    def test_the_body_token_and_the_cookie_are_the_same_token(self, secured):
        r = unlock(secured)
        assert r.json()["access_token"] == r.cookies[auth.SESSION_COOKIE]

    def test_an_expired_bearer_jwt_is_refused(self, secured):
        stale = auth.mint_token(ttl_sec=-1)
        r = secured.get("/api/health", headers={"Authorization": f"Bearer {stale}"})
        assert r.status_code == 401

    def test_a_jwt_does_not_substitute_for_the_trade_pin(self, secured):
        token = unlock(secured).json()["access_token"]
        fresh = TestClient(create_app(), raise_server_exceptions=False)
        r = fresh.post("/api/auto/start",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403

    def test_status_reports_the_remaining_lifetime(self, secured):
        unlock(secured)
        left = secured.get("/api/auth/status").json()["expires_in"]
        assert 0 < left <= auth.SESSION_TTL_SEC

    def test_status_still_leaks_no_token(self, secured):
        unlock(secured)
        assert "access_token" not in secured.get("/api/auth/status").text


class TestRawPassphraseBearerStillWorks:
    """The root credential. Kept so there is a way in before any token has
    been minted — but it never expires, so the JWT is what a script should
    actually carry."""

    def test_bearer_passphrase_authenticates_without_any_cookie(self, secured):
        r = secured.get("/api/health", headers={"Authorization": f"Bearer {READ}"})
        assert r.status_code == 200

    def test_bearer_still_needs_the_pin_for_writes(self, secured):
        r = secured.post("/api/auto/start", headers={"Authorization": f"Bearer {READ}"})
        assert r.status_code == 403


class TestStatusEndpoint:
    def test_it_is_reachable_while_locked_out_of_everything_else(self, secured):
        assert secured.get("/api/auth/status").status_code == 200
        assert secured.get("/api/health").status_code == 401

    def test_it_reports_protected_but_not_authenticated_before_unlock(self, secured):
        body = secured.get("/api/auth/status").json()
        assert body["protected"] is True and body["authenticated"] is False

    def test_it_reports_authenticated_after_unlock(self, secured):
        unlock(secured)
        assert secured.get("/api/auth/status").json()["authenticated"] is True

    def test_it_leaks_no_secret_material(self, secured):
        """Booleans only. A status endpoint is the easiest place in an app to
        accidentally hand out a prefix of the thing it is reporting on."""
        raw = secured.get("/api/auth/status").text
        assert READ not in raw and PIN not in raw
        for value in secured.get("/api/auth/status").json().values():
            assert isinstance(value, (bool, int)), value

    def test_probing_it_does_not_burn_a_lockout_attempt(self, secured):
        # The dashboard calls this on every page load. If it counted, opening
        # the dashboard eight times would lock the owner out.
        for _ in range(auth._MAX_FAILS * 2):
            secured.get("/api/auth/status")
        assert unlock(secured).status_code == 200


class TestDashboardStoresNothing:
    """Regression guards on the client. The server-side fix is worthless if
    the page keeps writing the passphrase down."""

    def test_the_passphrase_is_never_put_in_localstorage(self):
        html = DASHBOARD.read_text()
        # `raanu.theme` is the only legitimate localStorage key left.
        writes = re.findall(r"localStorage\.setItem\(\s*['\"]?([\w.]+)", html)
        assert set(writes) <= {"raanu.theme"}, f"unexpected localStorage writes: {writes}"

    def test_no_authorization_header_is_built_client_side(self):
        html = DASHBOARD.read_text()
        assert "Bearer" not in html, "the dashboard should authenticate by cookie now"

    def test_the_legacy_token_is_actively_purged(self):
        # Moving where the secret lives does nothing if the old plaintext
        # copy is left sitting in every browser that loaded the old page.
        html = DASHBOARD.read_text()
        assert "localStorage.removeItem('raanu.readToken')" in html

    def test_requests_opt_in_to_sending_the_cookie(self):
        assert "credentials: 'same-origin'" in DASHBOARD.read_text()

    def test_every_api_call_sends_credentials(self):
        """A fetch that forgets withCreds() gets a silent 401 and an empty
        panel — the exact failure mode that is hardest to spot by eye. Three
        push calls were missed on the first pass of this change."""
        bare = [line.strip() for line in DASHBOARD.read_text().splitlines()
                if "fetch(API" in line and "withCreds" not in line]
        assert not bare, f"fetch() without withCreds(): {bare}"


class TestWhatsAppWebhookFailsClosed:
    """/webhook/whatsapp sits OUTSIDE /api/, so the auth gate never sees it,
    and its BUY/SELL commands place real orders. The sender check is the only
    thing in front of them — and it used to fail open when the `From` field
    was simply omitted."""

    @pytest.fixture
    def reached(self, monkeypatch, secured):
        from raanu.api.routes import webhooks
        seen = []

        async def _stub(cmd):
            seen.append(cmd)

        monkeypatch.setattr(webhooks, "_handle_whatsapp_command", _stub)
        return seen

    def test_omitting_the_sender_no_longer_reaches_the_trade_commands(self, secured, reached):
        # The regression itself: `if From and From != expected` skipped the
        # check entirely when From was absent.
        secured.post("/webhook/whatsapp", data={"Body": "BUY NVDA 5000"})
        assert reached == []

    def test_an_unconfigured_deployment_accepts_nothing(self, secured, reached, monkeypatch):
        monkeypatch.delenv("USER_WHATSAPP", raising=False)
        for data in ({"Body": "BUY NVDA 5000"},
                     {"Body": "BUY NVDA 5000", "From": "whatsapp:+10000000000"},
                     {"Body": "SELL NVDA", "From": ""}):
            secured.post("/webhook/whatsapp", data=data)
        assert reached == []

    def test_a_wrong_sender_is_rejected(self, secured, reached, monkeypatch):
        monkeypatch.setenv("USER_WHATSAPP", "whatsapp:+15550001111")
        secured.post("/webhook/whatsapp",
                     data={"Body": "BUY NVDA 5000", "From": "whatsapp:+19999999999"})
        assert reached == []

    def test_the_configured_sender_still_works(self, secured, reached, monkeypatch):
        monkeypatch.setenv("USER_WHATSAPP", "whatsapp:+15550001111")
        secured.post("/webhook/whatsapp",
                     data={"Body": "STATUS", "From": "whatsapp:+15550001111"})
        assert reached == ["STATUS"]

    def test_no_phone_number_is_hardcoded_in_the_source(self):
        """The old default put the owner's real number in the repository AND
        made it the value the check compared against."""
        src = (Path(__file__).resolve().parent.parent
               / "raanu" / "api" / "routes" / "webhooks.py").read_text()
        assert "whatsapp:+9" not in src and "whatsapp:+1" not in src

    def test_rejections_do_not_say_why(self, secured, monkeypatch):
        monkeypatch.setenv("USER_WHATSAPP", "whatsapp:+15550001111")
        unconfigured = secured.post("/webhook/whatsapp", data={"Body": "STATUS"})
        wrong = secured.post("/webhook/whatsapp",
                             data={"Body": "STATUS", "From": "whatsapp:+19999999999"})
        assert unconfigured.text == wrong.text


class TestRemovedEndpoints:
    def test_the_twilio_debug_endpoint_is_gone(self, secured):
        """It echoed sid[:8] and token[:6] of the live Twilio credentials,
        and sent a real message on a GET."""
        assert "/api/test/twilio" not in secured.app.openapi()["paths"]

    def test_the_pin_oracle_is_gone(self, secured):
        """POST /api/auth/pin answered "is this the right PIN?" — and being a
        POST, the gate already required the PIN to reach it. It could only
        ever tell you something you had just proved you knew."""
        assert "/api/auth/pin" not in secured.app.openapi()["paths"]

    @pytest.mark.parametrize("path", ["/api/push/native/register",
                                      "/api/push/clear-web",
                                      "/privacy",
                                      "/.well-known/assetlinks.json"])
    def test_android_only_routes_are_gone(self, secured, path):
        assert path not in secured.app.openapi()["paths"]

    def test_the_auth_path_lists_have_no_stale_entries(self, secured):
        """SAFE_WRITES, MONEY_MOVING and _PUBLIC_API_PATHS name paths as
        literals, so deleting a route leaves a dangling entry behind —
        harmless today, and a silent hole the day something else claims that
        path. SAFE_WRITES is the dangerous one: a stale entry there means a
        future route at that path skips the trade PIN."""
        mounted = set(secured.app.openapi()["paths"])
        for name, paths in (("SAFE_WRITES", auth.SAFE_WRITES),
                            ("MONEY_MOVING", auth.MONEY_MOVING),
                            ("_PUBLIC_API_PATHS", auth._PUBLIC_API_PATHS)):
            stale = paths - mounted
            assert not stale, f"{name} names routes that no longer exist: {sorted(stale)}"

    def test_web_push_survived(self, secured):
        """The PWA is the phone story now, so web push is load-bearing rather
        than the redundant second channel it used to be."""
        for path in ("/api/push/key", "/api/push/subscribe",
                     "/api/push/unsubscribe", "/api/push/status"):
            assert path in secured.app.openapi()["paths"]


class TestLocalDotenv:
    """``.env`` is where local config is documented to live, and for a long
    time nothing loaded it — the file was only stat'd as a "am I local?"
    marker. A passphrase put there therefore did nothing, and an unset
    passphrase DISABLES the gate, so local dev looked configured and was
    wide open."""

    def test_dotenv_is_actually_loaded(self, tmp_path, monkeypatch):
        from raanu import config
        from raanu.api import app as app_mod

        env_file = tmp_path / ".env"
        env_file.write_text("API_READ_TOKEN=from-dotenv\n")
        monkeypatch.setattr(app_mod, "DOTENV", env_file)
        monkeypatch.delenv("API_READ_TOKEN", raising=False)

        assert config.api_read_token() == ""
        app_mod._load_dotenv_for_local_dev()
        assert config.api_read_token() == "from-dotenv"

    def test_a_real_env_var_beats_the_file(self, tmp_path, monkeypatch):
        # override=False, matching the SSM loader — so a one-off
        # `API_READ_TOKEN=x python -m raanu.api` does what it looks like.
        from raanu import config
        from raanu.api import app as app_mod

        env_file = tmp_path / ".env"
        env_file.write_text("API_READ_TOKEN=from-dotenv\n")
        monkeypatch.setattr(app_mod, "DOTENV", env_file)
        monkeypatch.setenv("API_READ_TOKEN", "from-shell")

        app_mod._load_dotenv_for_local_dev()
        assert config.api_read_token() == "from-shell"

    def test_a_missing_dotenv_is_not_an_error(self, tmp_path, monkeypatch):
        from raanu.api import app as app_mod
        monkeypatch.setattr(app_mod, "DOTENV", tmp_path / "nope.env")
        app_mod._load_dotenv_for_local_dev()      # must not raise

    def test_creating_an_app_does_NOT_read_dotenv(self, tmp_path, monkeypatch):
        """Tests call create_app(), and conftest clears the environment on
        purpose so a developer's real ALPACA_API_KEY cannot leak into a run.
        Loading .env there would defeat that."""
        from raanu import config
        from raanu.api import app as app_mod

        env_file = tmp_path / ".env"
        env_file.write_text("ALPACA_API_KEY=leaked-from-dotenv\n")
        monkeypatch.setattr(app_mod, "DOTENV", env_file)
        monkeypatch.delenv("ALPACA_API_KEY", raising=False)

        app_mod.create_app()
        assert config.alpaca_key() == ""


class TestPinClassification:
    """The trade PIN guards money, not HTTP verbs.

    It used to be "every non-GET needs the PIN", which meant running a SCAN
    required the credential that can place trades — defeating the point of
    having two secrets. Routes are classified by capability now, and these
    tests exist so the classification cannot silently rot."""

    def _non_get(self, client):
        spec = client.app.openapi()["paths"]
        return {(m.upper(), p) for p, ops in spec.items()
                for m in ops if m.upper() not in ("GET", "HEAD")}

    def test_every_non_get_route_is_classified(self, secured):
        """The one that matters. A new route added without a decision shows
        up here rather than in production."""
        unclassified = []
        for _method, path in self._non_get(secured):
            if path in auth._PUBLIC_API_PATHS or not path.startswith("/api/"):
                continue
            if (path in auth.SAFE_WRITES or path in auth.MONEY_MOVING
                    or path.startswith(auth.MONEY_MOVING_PREFIXES)):
                continue
            unclassified.append(path)
        assert not unclassified, (
            "add to MONEY_MOVING or SAFE_WRITES in raanu/api/auth.py: "
            + ", ".join(sorted(unclassified)))

    def test_the_two_sets_do_not_overlap(self):
        assert not (auth.SAFE_WRITES & auth.MONEY_MOVING)
        for path in auth.SAFE_WRITES:
            assert not path.startswith(auth.MONEY_MOVING_PREFIXES), path

    def test_safe_writes_are_literal_paths(self):
        """The middleware sees a concrete URL, so a templated entry here would
        never match and the route would demand a PIN forever — safe, but
        baffling to debug."""
        for path in auth.SAFE_WRITES:
            assert "{" not in path, path

    def test_an_unclassified_route_defaults_to_requiring_the_pin(self):
        """Fail closed. This is the property the old deny-by-method rule had,
        and the one thing the refactor must not lose."""
        assert auth.needs_trade_pin("/api/something/invented") is True

    @pytest.mark.parametrize("path", [
        "/api/orders/buy", "/api/orders/sell", "/api/orders/abc123",
        "/api/auto/start", "/api/auto/stop", "/api/auto/scan-now",
        "/api/exit-config",
    ])
    def test_money_moving_routes_need_the_pin(self, path):
        assert auth.needs_trade_pin(path) is True

    @pytest.mark.parametrize("path", [
        "/api/scan/job", "/api/scan/alert-now", "/api/auto/scan-now/s2",
        "/api/picks/backfill", "/api/push/subscribe", "/api/telegram/test",
    ])
    def test_research_and_notification_routes_do_not(self, path):
        assert auth.needs_trade_pin(path) is False

    def test_scan_now_and_its_s2_sibling_differ(self):
        """One character apart, opposite risk: /api/auto/scan-now calls
        run_one_cycle() and places orders; /s2 only scans and caches."""
        assert auth.needs_trade_pin("/api/auto/scan-now") is True
        assert auth.needs_trade_pin("/api/auto/scan-now/s2") is False

    def test_scanning_works_with_only_the_passphrase(self, secured):
        """End to end: the actual complaint that started this."""
        unlock(secured)
        assert secured.post("/api/scan/job").status_code != 403

    def test_placing_an_order_still_does_not(self, secured):
        unlock(secured)
        assert secured.post("/api/orders/buy", json={}).status_code == 403

    def test_a_missing_server_pin_says_so_instead_of_just_refusing(self, secured, monkeypatch):
        """Without this the UI prompts for a PIN, you type one, and it is
        rejected — with no way to tell that NO value could have worked."""
        monkeypatch.delenv("TRADE_PIN")
        unlock(secured)
        body = secured.post("/api/auto/start").json()
        assert "not configured" in body["detail"]


class TestClientMatchesServer:
    """The dashboard decides whether to prompt; the server decides whether to
    allow. If they disagree the server wins and you get a 403 — so the lists
    must not drift."""

    def _calls(self, fn_name):
        html = DASHBOARD.read_text()
        out = set()
        for m in re.finditer(rf"\b{fn_name}\(\s*[`'\"]([^`'\"]+)", html):
            out.add(m.group(1).split("?")[0].split("${")[0])
        return out

    def test_every_action_call_targets_a_money_moving_route(self):
        for path in self._calls("action"):
            assert auth.needs_trade_pin(path), f"{path} prompts for a PIN it does not need"

    def test_every_post_call_targets_a_safe_route(self):
        for path in self._calls("post"):
            assert not auth.needs_trade_pin(path), f"{path} will 403 — use action()"

    def test_the_scan_button_does_not_prompt(self):
        assert "/api/scan/job" in self._calls("post")
        assert "/api/scan/job" not in self._calls("action")
