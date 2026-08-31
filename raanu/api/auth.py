"""
raanu.api.auth — the two-token gate, and the session that keeps the
passphrase out of the browser
====================================================================
Two secrets, not one: a phone is the most losable device in this system, so
the token it carries must not be able to move money.

    API_READ_TOKEN   Authorization: Bearer ...   every /api/** request
    TRADE_PIN        X-Trade-Token: ...          additionally every non-GET

Non-GET is denied **by method, not by a path list** — a new POST route is
protected the day it is written, not the day someone remembers to add it
here.

The gate is skipped entirely when API_READ_TOKEN is unset, logging a warning
on every request: a deploy must not lock the owner out before the variable
exists, but "temporarily open" must not go quiet either.

Sessions: why the browser stops holding the secret
--------------------------------------------------
The dashboard used to keep the raw passphrase in ``localStorage`` and replay
it as a bearer token on every request. That put the *server's own shared
secret* — the one that never expires and that SSM holds — into
script-readable storage on every machine the dashboard was ever opened on.
Any XSS on the origin exfiltrates it permanently, and DevTools shows it in
plain text.

So the passphrase is now exchanged, once, for a session cookie:

    POST /api/auth/session {"passphrase": ...}
      -> Set-Cookie: raanu_session=v1.<expiry>.<hmac>
                     HttpOnly; Secure; SameSite=Strict

``HttpOnly`` puts it out of reach of JavaScript entirely, so there is no
longer anything for an injected script to read. The passphrase itself
crosses the wire exactly once, inside a TLS body, and is never written down
client-side.

The cookie is **stateless** — an HMAC over its own expiry, verified rather
than looked up. That matters here specifically: a session table would mean a
DynamoDB read on the critical path of every single request, including the
1.5s scan poll.

The HMAC key is derived from ``API_READ_TOKEN`` itself, which buys two
things and costs one:

  * no second secret to seed — the thing this whole change exists to avoid;
  * rotating the passphrase in SSM invalidates every outstanding session,
    which is the revocation story, with no infrastructure behind it;
  * but a stolen cookie would otherwise be an offline guessing target
    against a *memorable* passphrase. Hence PBKDF2 rather than a bare hash —
    derived once per cold start and cached, so the ~30ms is paid by the
    container, not by each request.

Bearer auth still works, unchanged. The React Native app, curl and the CLI
have no cookie jar worth relying on, and breaking them to fix the browser
would be trading one problem for another.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from raanu import config

log = logging.getLogger("raanu.api.auth")

_AUTH_FAILS: dict[str, dict] = {}

router = APIRouter()


_MAX_FAILS = 8


_LOCKOUT_SEC = 900          # 15 minutes


# ── session cookie ───────────────────────────────────────────────────────────

SESSION_COOKIE = "raanu_session"

# Long enough to work through a trading day without re-entering the
# passphrase, short enough that a walked-away-from laptop is not a permanent
# credential. Rotating API_READ_TOKEN cuts it short at any time.
SESSION_TTL_SEC = 12 * 3600

# Fixed rather than stored: there is exactly one secret here, so a per-user
# salt has nothing to separate. The salt's job is to stop a precomputed table
# from being reusable against other deployments, and a constant unique to
# this application does that.
_KDF_SALT = b"raanu-session-v1"
_KDF_ROUNDS = 200_000

_derived_key: tuple[str, bytes] | None = None


def _session_key() -> bytes:
    """PBKDF2 of the passphrase, cached for the life of the process.

    Keyed on a fingerprint of the passphrase so that rotating it in SSM
    re-derives on the next call instead of silently validating sessions
    minted under the old secret.
    """
    global _derived_key
    token = config.api_read_token()
    fingerprint = hashlib.sha256(token.encode()).hexdigest()
    if _derived_key is None or _derived_key[0] != fingerprint:
        _derived_key = (
            fingerprint,
            hashlib.pbkdf2_hmac("sha256", token.encode(), _KDF_SALT, _KDF_ROUNDS),
        )
    return _derived_key[1]


def _sign(payload: str) -> str:
    return hmac.new(_session_key(), payload.encode(), hashlib.sha256).hexdigest()


def mint_session(ttl_sec: int = SESSION_TTL_SEC) -> str:
    payload = f"v1.{int(time.time()) + ttl_sec}"
    return f"{payload}.{_sign(payload)}"


def valid_session(cookie: str) -> bool:
    """True if this cookie was minted by us and has not expired."""
    if not cookie or not config.api_read_token():
        return False
    version, _, rest = cookie.partition(".")
    expiry, _, mac = rest.partition(".")
    if version != "v1" or not expiry or not mac:
        return False
    # Signature first: an unsigned cookie's expiry is attacker-controlled and
    # not worth parsing, let alone trusting.
    if not hmac.compare_digest(_sign(f"{version}.{expiry}"), mac):
        return False
    try:
        return time.time() < int(expiry)
    except ValueError:
        return False


def reset_session_key() -> None:
    """Drop the derived key. Tests change API_READ_TOKEN between cases; the
    fingerprint check already handles that, so this exists for clarity."""
    global _derived_key
    _derived_key = None


def _client_ip(request: Request) -> str:
    # CloudFront terminates TLS upstream, so request.client is the proxy. The
    # first X-Forwarded-For entry is the caller.
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _is_https(request: Request) -> bool:
    """Whether to mark the cookie Secure.

    Not unconditional: a Secure cookie is silently dropped by the browser
    over plain http, which would make local dev on http://localhost:8000
    look like a broken login with nothing in any log to say why.
    """
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return (proto or request.url.scheme) == "https"


def _locked_out(ip: str) -> int:
    """Seconds remaining on a lockout, or 0."""
    now = time.time()
    seen = {h: t for h, t in _AUTH_FAILS.get(ip, {}).items() if now - t < _LOCKOUT_SEC}
    _AUTH_FAILS[ip] = seen
    if len(seen) >= _MAX_FAILS:
        return int(_LOCKOUT_SEC - (now - min(seen.values())))
    return 0


def _record_fail(ip: str, presented: str):
    # Hashed, so a mistyped secret is not sitting in memory in the clear.
    h = hashlib.sha256(presented.encode("utf-8", "replace")).hexdigest()
    _AUTH_FAILS.setdefault(ip, {})[h] = time.time()
    n = len(_AUTH_FAILS[ip])
    if n >= _MAX_FAILS:
        log.warning(f"Auth lockout: {ip} after {n} distinct wrong secrets")


def _presented_token(request: Request) -> str:
    """Read the caller's token from the header.

    Used to accept ?token= too, for /api/scan/stream's EventSource
    connection — EventSource can't set request headers. That route is gone
    (replaced by the async job + polling endpoints, /api/scan/job, which
    use ordinary header auth like everything else), so this no longer
    needs a query-string fallback — one less place a token could end up in
    access logs or browser history.
    """
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return (request.headers.get("x-api-token") or "").strip()


def _authenticated(request: Request) -> bool:
    """A valid bearer token OR a valid session cookie."""
    expected = config.api_read_token()
    if not expected:
        return True                      # gate disabled; everything is open
    token = _presented_token(request)
    if token and secrets.compare_digest(token, expected):
        return True
    return valid_session(request.cookies.get(SESSION_COOKIE, ""))


# The login endpoint cannot itself require a credential, and logging out or
# asking "am I signed in?" must work from the locked screen. All three are
# POST-or-GET safe: they move no money and change no bot behaviour, and
# /api/auth/session does its own lockout accounting.
_PUBLIC_API_PATHS = {"/api/auth/session", "/api/auth/logout", "/api/auth/status"}

# Push registration and its self-test move no money and change no bot
# behaviour; gating them behind the trade PIN made "is push working?"
# unanswerable without placing a trade.
_READ_TOKEN_POSTS = {"/api/push/subscribe", "/api/push/unsubscribe",
                     "/api/push/test"}


# Registered by create_app() rather than decorated onto a module-level app,
# so importing this module builds nothing.
async def api_auth_gate(request: Request, call_next):
    path = request.url.path
    method = request.method.upper()

    # Only /api/** is gated. GET / serves the HTML shell, which holds no data,
    # and /webhook/whatsapp must stay open for Twilio to reach it.
    if not path.startswith("/api/") or method == "OPTIONS":
        return await call_next(request)

    if path in _PUBLIC_API_PATHS:
        return await call_next(request)

    # Unset token = gate disabled, so a deploy cannot lock the owner out before
    # the variable is in place. Loud on every request rather than silent, or
    # "temporarily open" quietly becomes permanent.
    if not config.api_read_token():
        log.warning("API_READ_TOKEN is not set — the API is OPEN to anyone with the URL")
        return await call_next(request)

    ip = _client_ip(request)
    wait = _locked_out(ip)
    if wait:
        return JSONResponse(
            {"error": "too_many_attempts", "retry_after_sec": wait},
            status_code=429, headers={"Retry-After": str(wait)},
        )

    if not _authenticated(request):
        _record_fail(ip, _presented_token(request))
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    # Anything that is not a read needs the second secret. Deny-by-method rather
    # than by a path list: a new POST route is then protected the day it is
    # written, instead of the day someone remembers to add it here.
    #
    # This is also what makes the session cookie safe against CSRF for
    # everything that matters: a cross-site request cannot set X-Trade-Token,
    # so no amount of ambient cookie authority moves money. SameSite=Strict
    # covers the reads on top of that.
    if method not in ("GET", "HEAD") and path not in _READ_TOKEN_POSTS:
        expected = os.getenv("TRADE_PIN", "").strip()
        presented = (request.headers.get("x-trade-token") or "").strip()
        if not expected or not secrets.compare_digest(presented, expected):
            # Counted too: the trade PIN is the shorter of the two secrets and
            # the one worth guessing, so it needs the throttle more, not less.
            _record_fail(ip, presented)
            return JSONResponse(
                {"error": "forbidden", "detail": "this action needs the trade PIN"},
                status_code=403,
            )

    # A good secret clears the slate, so a few fat-fingered attempts before a
    # correct one never accumulate into a lockout.
    _AUTH_FAILS.pop(ip, None)

    return await call_next(request)


@router.post("/api/auth/session")
async def open_session(request: Request):
    """Exchange the passphrase for an HttpOnly session cookie.

    The one request in the system that carries the passphrase. Everything
    after it rides the cookie, so no page ever has to hold the secret.
    """
    expected = config.api_read_token()
    if not expected:
        # Nothing to authenticate against. Say so plainly rather than handing
        # out a cookie that means nothing, or a 401 the UI would read as a
        # wrong passphrase.
        return JSONResponse({"ok": True, "protected": False,
                             "reason": "no_passphrase_configured"})

    ip = _client_ip(request)
    wait = _locked_out(ip)
    if wait:
        return JSONResponse(
            {"error": "too_many_attempts", "retry_after_sec": wait},
            status_code=429, headers={"Retry-After": str(wait)},
        )

    try:
        body = await request.json()
    except Exception:
        body = {}
    presented = str((body or {}).get("passphrase") or "").strip()

    if not secrets.compare_digest(presented, expected):
        _record_fail(ip, presented)
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    _AUTH_FAILS.pop(ip, None)
    response = JSONResponse({"ok": True, "protected": True,
                             "expires_in_sec": SESSION_TTL_SEC})
    response.set_cookie(
        SESSION_COOKIE,
        mint_session(),
        max_age=SESSION_TTL_SEC,
        httponly=True,                   # the whole point: JS cannot read it
        secure=_is_https(request),
        samesite="strict",
        path="/",
    )
    return response


@router.post("/api/auth/logout")
async def close_session():
    """Drop the session cookie. Public by design — clearing your own
    credential should never require presenting it."""
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/api/auth/status")
async def auth_status(request: Request):
    """Booleans only — never any part of a secret.

    Lets the dashboard decide between the unlock screen and loading data
    without firing a request it expects to 401, which would otherwise put a
    failed attempt on the lockout counter at every page load.
    """
    return {
        "protected": bool(config.api_read_token()),
        "authenticated": _authenticated(request),
        "trade_pin_configured": bool(os.getenv("TRADE_PIN", "").strip()),
        "session_ttl_sec": SESSION_TTL_SEC,
    }
