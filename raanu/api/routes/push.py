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


@router.post("/api/push/clear-web")
async def push_clear_web():
    """Drop every browser/TWA push subscription.

    Two apps subscribing to the same events meant two notifications per trade,
    and tapping either one opened the TWA — because a web push belongs to the
    service worker that registered it, not to whichever app you prefer. With the
    native app in place the web channel is redundant, and one owner is the only
    stable arrangement.
    """
    from raanu.notify import push
    n = len(push._load())
    push._save([])
    return {"ok": True, "cleared": n}


@router.post("/api/push/native/register")
async def push_native_register(request: Request):
    """Register a native (FCM) device token. Read-token only, like web push —
    registering a phone for notifications moves no money."""
    from raanu.notify import push
    b = await request.json()
    return push.register_native(b.get("token", ""), b.get("platform", "android"))


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
    # Through _fanout, not send()/send_native() directly: a self-test that
    # skips half the delivery path proves less than it appears to. It was
    # bypassing _record(), so tests never showed up in Alerts — exactly the
    # thing the test is meant to demonstrate.
    push._fanout(title, body, "test", sticky=True)
    st = push.status()
    return {"sent": len(st["native"]["devices"]) + st["web"]["subs"],
            "native_sent": len(st["native"]["devices"]),
            "web_subs": st["web"]["subs"], "recorded": True}
