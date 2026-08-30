"""
raanu.api.auth — the two-token gate
====================================
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
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from raanu import config

log = logging.getLogger("raanu.api.auth")

_AUTH_FAILS: dict[str, list] = {}

router = APIRouter()


_MAX_FAILS = 8


_LOCKOUT_SEC = 900          # 15 minutes


def _client_ip(request: Request) -> str:
    # Railway terminates TLS upstream, so request.client is the proxy. The first
    # X-Forwarded-For entry is the caller.
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


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


# Registered by create_app() rather than decorated onto a module-level app,
# so importing this module builds nothing.
async def api_auth_gate(request: Request, call_next):
    path = request.url.path
    method = request.method.upper()

    # Only /api/** is gated. GET / serves the HTML shell, which holds no data,
    # and /webhook/whatsapp must stay open for Twilio to reach it.
    if not path.startswith("/api/") or method == "OPTIONS":
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

    if not secrets.compare_digest(_presented_token(request), config.api_read_token()):
        _record_fail(ip, _presented_token(request))
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    # Anything that is not a read needs the second secret. Deny-by-method rather
    # than by a path list: a new POST route is then protected the day it is
    # written, instead of the day someone remembers to add it here.
    # Push registration and its self-test move no money and change no bot
    # behaviour; gating them behind the trade PIN made "is push working?"
    # unanswerable without placing a trade.
    _READ_TOKEN_POSTS = {"/api/push/subscribe", "/api/push/unsubscribe",
                         "/api/push/test", "/api/push/native/register",
                         "/api/push/clear-web"}

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


@router.post("/api/auth/pin")
async def verify_pin(request: Request):
    """Verify trade PIN. Format: 2 letters + 6 digits (e.g. AB123456)."""
    body     = await request.json()
    entered  = body.get("pin", "").strip().upper()
    expected = os.getenv("TRADE_PIN", "").strip().upper()
    if not expected:
        return {"ok": True, "reason": "no_pin_configured"}
    return {"ok": entered == expected}
