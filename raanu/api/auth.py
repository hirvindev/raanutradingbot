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

So the passphrase is now exchanged, once, for a JWT:

    POST /api/auth/session {"passphrase": ...}
      -> {"access_token": "eyJhbGci...", "token_type": "Bearer",
          "expires_in": 43200}
      -> Set-Cookie: raanu_session=eyJhbGci...
                     HttpOnly; Secure; SameSite=Strict

**The same token, delivered two ways, and the client picks.** The browser
uses the cookie and ignores the body; scripts read ``access_token`` and send
``Authorization: Bearer``. That split is the point — a browser must not hold
the token where script can reach it, and a script has no cookie jar worth
relying on.

Handing a script a JWT also fixes something the cookie alone did not: before
this, curl authenticated with ``Bearer <the raw passphrase>``, so every
script carried the permanent secret. Now it carries a 12-hour one.

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
import logging
import os
import secrets
import time

import jwt
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

# HS256, symmetric: there is one issuer and one verifier, both this process.
# RS256 exists to let a third party verify without being able to mint, which
# is not a distinction anything here needs.
_JWT_ALG = "HS256"
_JWT_ISSUER = "raanu"

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


def mint_token(ttl_sec: int = SESSION_TTL_SEC) -> str:
    """Issue a signed JWT for the owner.

    HS256 with the derived key. The previous format was a hand-rolled
    ``v1.<expiry>.<hmac>``, which did the same job — this is the standard
    spelling of it, so the token is inspectable with ordinary tooling and
    carries real claims instead of one bare number.
    """
    now = int(time.time())
    return jwt.encode(
        {"iss": _JWT_ISSUER, "sub": "owner", "iat": now, "exp": now + ttl_sec},
        _session_key(),
        algorithm=_JWT_ALG,
    )


def verify_token(token: str) -> dict | None:
    """Decoded claims if this is a live token we minted, else None.

    ``algorithms=[HS256]`` is the load-bearing argument, not a formality: it
    is what makes PyJWT reject ``alg: none`` and algorithm-confusion attacks,
    where an attacker re-signs the payload under a scheme the verifier did
    not intend. Never widen it, and never pass the token's own header
    algorithm here.

    Signature is checked before any claim is read — an unverified payload is
    attacker-controlled and not worth parsing, let alone trusting. PyJWT does
    that ordering for us, and validates ``exp`` itself.
    """
    if not token or not config.api_read_token():
        return None
    try:
        return jwt.decode(
            token,
            _session_key(),
            algorithms=[_JWT_ALG],
            issuer=_JWT_ISSUER,
            options={"require": ["exp", "iat", "iss"]},
        )
    except jwt.InvalidTokenError:
        # One except for expired, tampered, malformed, wrong-issuer and
        # missing-claim alike: the caller gets in or does not, and telling it
        # apart out loud only helps whoever is probing.
        return None


def valid_session(token: str) -> bool:
    return verify_token(token) is not None


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
    """Three ways in, in the order a caller is likely to use them.

    1. ``Authorization: Bearer <jwt>`` — what a script should use. Expires,
       so a leaked one stops working, and it can be re-minted at will.
    2. The session cookie, which holds the same JWT. What the browser uses;
       HttpOnly, so page scripts cannot read it.
    3. ``Authorization: Bearer <passphrase>`` — the root credential, still
       accepted so there is a way in before any token has been minted (and
       so existing scripts did not break the day JWTs landed).

    (3) is the weakest of the three: it never expires and it is the same
    secret SSM holds. Prefer (1) — POST the passphrase once to
    /api/auth/session and carry the JWT.
    """
    expected = config.api_read_token()
    if not expected:
        return True                      # gate disabled; everything is open

    presented = _presented_token(request)
    if presented:
        if verify_token(presented):
            return True
        if secrets.compare_digest(presented, expected):
            return True
    return valid_session(request.cookies.get(SESSION_COOKIE, ""))


# The login endpoint cannot itself require a credential, and logging out or
# asking "am I signed in?" must work from the locked screen. All three are
# POST-or-GET safe: they move no money and change no bot behaviour, and
# /api/auth/session does its own lockout accounting.
_PUBLIC_API_PATHS = {"/api/auth/session", "/api/auth/logout", "/api/auth/status"}


# ── what the trade PIN actually protects ─────────────────────────────────────
#
# This used to be "every non-GET needs the PIN", with a small exempt list. The
# rule was chosen for one good reason — a new POST route is protected the day
# it is written, not the day someone remembers to add it here — but it also
# meant **running a scan demanded the credential that can place trades**.
# Scanning is read-only research; requiring the money credential for it
# defeats the whole point of having two secrets, which is that you can look
# without being able to spend.
#
# So routes are now classified by what they can actually do. The safety
# property is kept by making the DEFAULT "needs the PIN": a route in neither
# set below is treated as money-moving and logged as unclassified. Adding a
# route without classifying it therefore fails safe (an unexpected PIN
# prompt), never open. `tests/test_auth_session.py` asserts the two sets
# together cover every mounted non-GET route, so drift is a test failure
# rather than a surprise.

# Moves money, or changes what the bot will do with money on its own.
MONEY_MOVING = {
    "/api/auto/start",        # enables autonomous trading
    "/api/auto/stop",         # and disables it — this really does stop the
                              # live bot; it got switched off by accident once
    "/api/auto/scan-now",     # ⚠️ NOT a scan. Calls run_one_cycle(), which
                              # places orders. Its /s2 sibling does not — the
                              # names are one character apart and the risk is
                              # not. Read the handler before reclassifying.
    "/api/exit-config",       # sets the stop and trail, i.e. decides when
                              # every open position gets sold
}

# The whole /api/orders/ family: buy, sell, and DELETE /api/orders/{id} to
# cancel. A prefix rather than three literals because the cancel route is
# templated — the middleware sees "/api/orders/abc123", which would never
# match the OpenAPI spelling "/api/orders/{order_id}".
MONEY_MOVING_PREFIXES = ("/api/orders/",)

# Changes nothing about money: research, notifications, device registration.
# The passphrase alone is enough. Must be literal paths — a templated entry
# would silently never match and the route would demand a PIN forever.
SAFE_WRITES = {
    "/api/scan/job",           # the Live Signals scan. The reason for all this
    "/api/scan/alert-now",     # re-sends the morning alert; no trading
    "/api/auto/scan-now/s2",   # scans and caches only — no run_one_cycle()
    "/api/picks/backfill",     # fills forward returns on logged picks
    "/api/push/subscribe",     # "is push working?" was unanswerable without
    "/api/push/unsubscribe",   # placing a trade, which is absurd
    "/api/push/test",
    "/api/report/monthly/send",
    "/api/telegram/test",
}


def needs_trade_pin(path: str) -> bool:
    """Whether this non-GET path requires the second secret.

    Note this only ever applies to non-GET. A money-moving GET would bypass
    the PIN entirely — there are none today, and adding one would be a
    mistake this function cannot catch.
    """
    if path in SAFE_WRITES:
        return False
    if path in MONEY_MOVING or path.startswith(MONEY_MOVING_PREFIXES):
        return True
    log.warning(
        "Unclassified non-GET route %s — requiring the trade PIN. Add it to "
        "MONEY_MOVING or SAFE_WRITES in raanu/api/auth.py", path,
    )
    return True


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

    # The second secret, on the routes that can actually cost money — see
    # needs_trade_pin() for the classification and why it is not simply
    # "every non-GET" any more.
    #
    # This is also what makes the session cookie safe against CSRF where it
    # matters: a cross-site request cannot set X-Trade-Token, so no amount of
    # ambient cookie authority moves money. SameSite=Strict covers the rest.
    if method not in ("GET", "HEAD") and needs_trade_pin(path):
        expected = os.getenv("TRADE_PIN", "").strip()
        presented = (request.headers.get("x-trade-token") or "").strip()
        if not expected or not secrets.compare_digest(presented, expected):
            # Counted too: the trade PIN is the shorter of the two secrets and
            # the one worth guessing, so it needs the throttle more, not less.
            _record_fail(ip, presented)
            detail = ("this action needs the trade PIN"
                      if expected else
                      "TRADE_PIN is not configured on the server, so no value "
                      "can be accepted — seed it with ./aws/seed-secrets.sh")
            return JSONResponse({"error": "forbidden", "detail": detail},
                                status_code=403)

    # A good secret clears the slate, so a few fat-fingered attempts before a
    # correct one never accumulate into a lockout.
    _AUTH_FAILS.pop(ip, None)

    return await call_next(request)


@router.post("/api/auth/session")
async def open_session(request: Request):
    """Exchange the passphrase for a JWT. The one request that carries the
    passphrase; everything after it carries the token.

    The same token goes out **two ways**, and which one a client uses is the
    whole design:

      * ``Set-Cookie: raanu_session=<jwt>; HttpOnly`` — for the browser.
        HttpOnly means page scripts cannot read it, so an XSS has nothing to
        steal. The dashboard uses this and ignores the body.
      * ``access_token`` in the JSON body — for curl, scripts and any future
        non-browser client, which have no cookie jar worth relying on and
        would otherwise have to carry the permanent passphrase instead.

        curl -sX POST $URL/api/auth/session \\
             -H 'Content-Type: application/json' \\
             -d '{"passphrase":"..."}' | jq -r .access_token

    A browser client should NOT read access_token out of this response and
    store it — that puts the credential back in JavaScript's reach and undoes
    the reason the cookie is HttpOnly.
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
    token = mint_token()
    # access_token / token_type / expires_in are the OAuth2 bearer response
    # field names (RFC 6749 §5.1) — worth matching exactly, because every HTTP
    # client library already knows how to read them.
    response = JSONResponse({
        "ok": True,
        "protected": True,
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": SESSION_TTL_SEC,
    })
    response.set_cookie(
        SESSION_COOKIE,
        token,
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
    """Booleans and one timestamp — never any part of a secret, and never the
    token itself (that would hand a page script the thing HttpOnly exists to
    keep away from it).

    Lets the dashboard decide between the unlock screen and loading data
    without firing a request it expects to 401, which would otherwise put a
    failed attempt on the lockout counter at every page load.
    """
    out = {
        "protected": bool(config.api_read_token()),
        "authenticated": _authenticated(request),
        "trade_pin_configured": bool(os.getenv("TRADE_PIN", "").strip()),
        "session_ttl_sec": SESSION_TTL_SEC,
    }
    # Seconds left, so a client can renew before a request fails rather than
    # after. Derived from the token's own exp claim, not from when the page
    # happened to load.
    claims = verify_token(_presented_token(request)
                          or request.cookies.get(SESSION_COOKIE, ""))
    if claims:
        out["expires_in"] = max(0, int(claims["exp"] - time.time()))
    return out
