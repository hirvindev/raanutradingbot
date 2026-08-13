"""
push.py — Web Push notifications to the installed app
======================================================
Sends system notifications to the phone for the events worth interrupting
someone about. Telegram remains the full record; this is the tap on the
shoulder.

What gets pushed, and why only these
------------------------------------
  BUY   — money committed on your behalf
  EXIT  — a stop or trail fired; the thing you would want to know immediately
  ERROR — the bot tried to act and could not

Scans, "no actionable signal" and routine status deliberately do NOT push. A
channel that fires on everything trains you to swipe it away, and then the stop
-out notification gets swiped away too. Telegram already carries the full
narrative and can be read at leisure.

Delivery reality
----------------
  * Android, installed from Play or the browser: works when the app is closed.
  * iPhone: only when installed via Safari's "Add to Home Screen" (iOS 16.4+),
    and iOS drops the subscription if the app goes unopened for a while. Treat
    it as best-effort there, never as the channel a stop-loss depends on.

Subscriptions live on the persistent volume; a wiped file means silent phones
and nothing worse, so failures here never propagate to the trading path.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from datadir import state_path

log = logging.getLogger("raanu.push")

SUBS_PATH = state_path("push_subs.json")


def _load() -> list:
    if SUBS_PATH.exists():
        try:
            return json.loads(SUBS_PATH.read_text()).get("subs", [])
        except Exception:
            log.warning("[push] subscription file unreadable")
    return []


def _save(subs: list):
    try:
        SUBS_PATH.write_text(json.dumps({"subs": subs}, indent=2))
    except Exception as e:
        log.error(f"[push] could not save subscriptions: {e}")


def public_key() -> str:
    return os.getenv("VAPID_PUBLIC_KEY", "").strip()


def configured() -> bool:
    return bool(public_key() and os.getenv("VAPID_PRIVATE_KEY", "").strip())


def subscribe(sub: dict) -> dict:
    """Register a device. Keyed on endpoint, so re-subscribing is not a duplicate."""
    if not sub.get("endpoint"):
        return {"ok": False, "error": "no endpoint"}
    subs = _load()
    subs = [s for s in subs if s.get("endpoint") != sub["endpoint"]]
    subs.append({**sub, "added": datetime.now(timezone.utc).isoformat()})
    _save(subs)
    log.info(f"[push] device registered ({len(subs)} total)")
    return {"ok": True, "devices": len(subs)}


def unsubscribe(endpoint: str) -> dict:
    subs = [s for s in _load() if s.get("endpoint") != endpoint]
    _save(subs)
    return {"ok": True, "devices": len(subs)}


def send(title: str, body: str, tag: str = "raanu", url: str = "/") -> dict:
    """Push to every registered device. Never raises.

    A 404 or 410 from the push service means the browser dropped the
    subscription — the device is pruned rather than retried forever.
    """
    if not configured():
        return {"sent": 0, "skipped": "VAPID keys not configured"}
    subs = _load()
    if not subs:
        return {"sent": 0, "skipped": "no devices registered"}

    try:
        from pywebpush import webpush, WebPushException
    except Exception as e:
        log.warning(f"[push] pywebpush unavailable: {e}")
        return {"sent": 0, "error": str(e)}

    payload = json.dumps({"title": title, "body": body, "tag": tag, "url": url})
    claims = {"sub": os.getenv("VAPID_CLAIM_EMAIL", "mailto:admin@example.com")}
    sent, dead = 0, []

    for s in subs:
        try:
            webpush(subscription_info={"endpoint": s["endpoint"], "keys": s["keys"]},
                    data=payload,
                    vapid_private_key=os.getenv("VAPID_PRIVATE_KEY", "").strip(),
                    vapid_claims=dict(claims))
            sent += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):
                dead.append(s["endpoint"])       # browser dropped it; stop trying
            else:
                log.warning(f"[push] send failed ({code}): {e}")
        except Exception as e:
            log.warning(f"[push] send error: {e}")

    if dead:
        _save([s for s in subs if s["endpoint"] not in dead])
        log.info(f"[push] pruned {len(dead)} expired subscription(s)")
    return {"sent": sent, "pruned": len(dead), "devices": len(subs) - len(dead)}


# ---------- native (FCM / Expo) ----------
# The web path above uses VAPID and speaks to browser push services. A native
# Android app cannot use that: it needs FCM. Tokens therefore live in their own
# file and are sent through a different transport, but BOTH are fanned out by
# the same notify_* helpers, so a buy reaches every registered client without
# the trading code knowing which kind each one is.
NATIVE_PATH = state_path("push_native.json")


def _load_native() -> list:
    if NATIVE_PATH.exists():
        try:
            return json.loads(NATIVE_PATH.read_text()).get("tokens", [])
        except Exception:
            log.warning("[push] native token file unreadable")
    return []


def _save_native(toks: list):
    try:
        NATIVE_PATH.write_text(json.dumps({"tokens": toks}, indent=2))
    except Exception as e:
        log.error(f"[push] could not save native tokens: {e}")


def register_native(token: str, platform: str = "android") -> dict:
    if not token:
        return {"ok": False, "error": "no token"}
    toks = [t for t in _load_native() if t.get("token") != token]
    toks.append({"token": token, "platform": platform,
                 "added": datetime.now(timezone.utc).isoformat()})
    _save_native(toks)
    log.info(f"[push] native device registered ({len(toks)} total)")
    return {"ok": True, "devices": len(toks)}


def send_native(title: str, body: str) -> int:
    """Deliver via FCM v1. Returns how many devices were reached.

    Requires FCM_SERVICE_ACCOUNT_JSON — the service-account key from the
    Firebase project. Without it this is a no-op that says so, rather than
    failing somewhere deep in a trade path.
    """
    toks = _load_native()
    if not toks:
        return 0
    raw = os.getenv("FCM_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        log.info("[push] native skipped — FCM_SERVICE_ACCOUNT_JSON not set")
        return 0
    try:
        import httpx
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request as GRequest

        info = json.loads(raw)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/firebase.messaging"])
        creds.refresh(GRequest())
        url = f"https://fcm.googleapis.com/v1/projects/{info['project_id']}/messages:send"
        headers = {"Authorization": f"Bearer {creds.token}"}
        sent, dead = 0, []
        for t in toks:
            payload = {"message": {"token": t["token"],
                                   "notification": {"title": title, "body": body},
                                   "android": {"priority": "high",
                                               "notification": {"channel_id": "trades"}}}}
            r = httpx.post(url, headers=headers, json=payload, timeout=15)
            if r.status_code == 200:
                sent += 1
            elif r.status_code in (400, 404):
                dead.append(t["token"])      # unregistered; stop trying
            else:
                log.warning(f"[push] FCM {r.status_code}: {r.text[:120]}")
        if dead:
            _save_native([t for t in toks if t["token"] not in dead])
            log.info(f"[push] pruned {len(dead)} dead native token(s)")
        return sent
    except Exception as e:
        log.warning(f"[push] native send failed: {e}")
        return 0


# ---------- event helpers (call these, not send()) ----------
def _fanout(title: str, body: str, tag: str):
    """One call, every registered client — browser and native alike."""
    web = send(title, body, tag=tag)
    nat = send_native(title, body)
    log.info(f"[push] {tag}: {web.get('sent', 0)} web, {nat} native")


def notify_buy(ticker: str, usd: float, strategy: str, score=None):
    _fanout(f"Bought {ticker}",
            f"${usd:,.0f} · {strategy.upper()}" + (f" · score {score}" if score else ""),
            f"buy-{ticker}")


def notify_exit(ticker: str, pnl: float, pct: float, reason: str):
    sign = "+" if pnl >= 0 else ""
    _fanout(f"Exited {ticker} {sign}{pct:.1f}%",
            f"{sign}${pnl:,.2f} · {reason}", f"exit-{ticker}")


def notify_error(what: str):
    _fanout("RaanuBot problem", what[:180], "error")
