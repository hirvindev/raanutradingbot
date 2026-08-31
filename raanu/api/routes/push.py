"""raanu.api.routes.push"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from raanu import config

log = logging.getLogger("raanu.api.routes.push")

router = APIRouter()


@router.get("/api/push/key")
async def push_key():
    """Public VAPID key. Safe to hand out — it only lets a browser subscribe."""
    from raanu.notify import push
    return {"key": push.public_key(), "configured": push.configured()}


@router.post("/api/push/subscribe")
async def push_subscribe(request: Request):
    from raanu.notify import push
    return push.subscribe(await request.json())


@router.post("/api/push/unsubscribe")
async def push_unsubscribe(request: Request):
    from raanu.notify import push
    return push.unsubscribe((await request.json()).get("endpoint", ""))


# POST /api/push/clear-web and POST /api/push/native/register were removed on
# 31 Aug 2026 with the Android apps.
#
# clear-web existed only to resolve a conflict between two of them: the TWA and
# the native app both subscribed to the same events, so every trade notified
# twice and tapping either one opened the TWA — a web push belongs to the
# service worker that registered it, not to whichever app you prefer. With one
# client left there is nothing to disambiguate. Use unsubscribe.
#
# native/register took an FCM token from the React Native app, which no longer
# exists.


@router.get("/api/notifications")
async def notifications():
    """Alerts from the retention window (default 48h), newest first.

    Exists because a tapped notification is gone, and a trade signal is the
    wrong thing to lose — it carried the entry, stop and reasoning.
    """
    from raanu.notify import push
    items = push.history()
    return {"items": items, "count": len(items),
            "retain_hours": config.notif_retain_hours()}


@router.get("/api/push/status")
async def push_status():
    """Which half of push is broken: registration, or delivery?"""
    from raanu.notify import push
    return push.status()


@router.post("/api/push/test")
async def push_test():
    """Fire a test notification. Deliberately a READ-token action.

    Confirming your own phone receives notifications is not a money-moving
    operation, and requiring the trade PIN for it meant the only way to find out
    whether push worked was to wait for a real trade — which is why it looked
    broken rather than untested.
    """
    from raanu.notify import push
    sample = {"ticker": "HUM", "score": 87, "price": 385.88, "rsi": 52.0,
              "macd": 0.93, "atr_pct": 4.2, "ema20": 380.65, "mom_3m": 28.4,
              "rel_strength": 24.3,
              "reasons": ["Confirmed uptrend — price > EMA200, EMA50 rising",
                          "Price $385.88 above EMA50 $365.40",
                          "3M momentum +28.4%"]}
    title, body = push.format_signal(sample, "s1")
    title += " (sample)"
    # Through _fanout, not send() directly: a self-test that skips part of the
    # delivery path proves less than it appears to. It was bypassing _record(),
    # so tests never showed up in Alerts — exactly the thing being tested.
    push._fanout(title, body, "test", sticky=True)
    st = push.status()
    return {"sent": st["web"]["subs"], "web_subs": st["web"]["subs"],
            "recorded": True}
