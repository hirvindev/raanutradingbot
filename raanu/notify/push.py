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
from datetime import datetime, timezone
from typing import Optional

from raanu import config, state

log = logging.getLogger("raanu.notify.push")

SUBS_KEY = "push_subs.json"


def _load() -> list:
    return state.load(SUBS_KEY, default={}).get("subs", [])


def _save(subs: list):
    state.save(SUBS_KEY, {"subs": subs})


def public_key() -> str:
    return config.vapid_public_key()


def configured() -> bool:
    return bool(public_key() and config.vapid_private_key())


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


def send(title: str, body: str, tag: str = "raanu", url: str = "/",
         sticky: bool = False) -> dict:
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

    payload = json.dumps({"title": title, "body": body, "tag": tag, "url": url,
                          "requireInteraction": sticky})
    claims = {"sub": config.vapid_claim_email()}
    sent, dead = 0, []

    for s in subs:
        try:
            webpush(subscription_info={"endpoint": s["endpoint"], "keys": s["keys"]},
                    data=payload,
                    vapid_private_key=config.vapid_private_key(),
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
NATIVE_KEY = "push_native.json"


def _load_native() -> list:
    return state.load(NATIVE_KEY, default={}).get("tokens", [])


def _save_native(toks: list):
    state.save(NATIVE_KEY, {"tokens": toks})


def register_native(token: str, platform: str = "android") -> dict:
    if not token:
        return {"ok": False, "error": "no token"}
    if len(token) < 100:
        # A real FCM registration token is ~140+ chars. A short string is a
        # test artefact, and once stored it fails on every future send.
        return {"ok": False, "error": "that does not look like an FCM token"}
    toks = [t for t in _load_native() if t.get("token") != token]
    toks.append({"token": token, "platform": platform,
                 "added": datetime.now(timezone.utc).isoformat()})
    _save_native(toks)
    log.info(f"[push] native device registered ({len(toks)} total)")
    return {"ok": True, "devices": len(toks)}


def _fcm_info():
    """Parse FCM_SERVICE_ACCOUNT_JSON, accepting base64 as well as raw JSON.

    Raw JSON loses to the paste box: the service-account private_key is a
    multi-line PEM, and pasting it into a dashboard variable field mangles the
    newlines often enough that the value ends up unparseable. That is exactly
    how this was broken — the variable was set, looked right in the UI, and was
    not JSON at all.

    Base64 is one line and cannot be mangled, so it is the recommended form.
    Raw JSON still works when it survives the trip.
    """
    raw = config.fcm_service_account_json()
    if not raw:
        return None, "FCM_SERVICE_ACCOUNT_JSON is not set"
    try:
        return json.loads(raw), None
    except Exception:
        pass
    try:
        import base64
        return json.loads(base64.b64decode(raw + "===")), None
    except Exception:
        pass
    # Say what it actually looks like WITHOUT leaking it — this is a private key.
    return None, (f"not valid JSON and not valid base64 "
                  f"(length {len(raw)}, starts with {raw[:1]!r}). "
                  f"Re-add it base64-encoded, on one line.")


def status() -> dict:
    """What is actually registered, and can the server actually send?

    Written while native push had been 'not working' for days with no way to
    tell WHICH half was broken — the phone never registering, or the server
    unable to deliver. Those need opposite fixes, and guessing between them
    wasted more time than this endpoint took to write.

    It mints a real FCM access token rather than just checking the variable is
    set: a service-account key from the wrong project, or with the Cloud
    Messaging API disabled, is present-but-useless and looks identical from the
    outside.
    """
    out = {
        "web": {"configured": configured(), "subs": len(_load())},
        "native": {"devices": [], "fcm": {}},
    }
    for t in _load_native():
        tok = t.get("token", "")
        out["native"]["devices"].append({          # never echo a whole token
            "platform": t.get("platform"),
            "added": t.get("added"),
            "token_tail": tok[-8:] if tok else None,
            "token_len": len(tok),
        })
    info, err = _fcm_info()
    if info is None:
        out["native"]["fcm"] = {"configured": bool(config.fcm_service_account_json()),
                                "credentials_valid": False, "detail": err}
        return out
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request as GRequest
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/firebase.messaging"])
        creds.refresh(GRequest())                  # the real test
        out["native"]["fcm"] = {"configured": True, "credentials_valid": True,
                                "project_id": info.get("project_id"),
                                "client_email": info.get("client_email")}
    except Exception as e:
        out["native"]["fcm"] = {"configured": True, "credentials_valid": False,
                                "detail": f"{type(e).__name__}: {e}"[:300]}
    return out


def send_native(title: str, body: str, sticky: bool = False) -> int:
    """Deliver via FCM v1. Returns how many devices were reached.

    Requires FCM_SERVICE_ACCOUNT_JSON — the service-account key from the
    Firebase project. Without it this is a no-op that says so, rather than
    failing somewhere deep in a trade path.
    """
    toks = _load_native()
    if not toks:
        return 0
    info, err = _fcm_info()
    if info is None:
        log.warning(f"[push] native skipped — {err}")
        return 0
    try:
        import httpx
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request as GRequest

        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/firebase.messaging"])
        creds.refresh(GRequest())
        url = f"https://fcm.googleapis.com/v1/projects/{info['project_id']}/messages:send"
        headers = {"Authorization": f"Bearer {creds.token}"}
        sent, dead = 0, []
        for t in toks:
            # An iPhone registers an APNs token, not an FCM one. Firing it at
            # FCM returns 404, which this loop reads as "unregistered" and
            # prunes — so an iOS device would silently unregister itself on the
            # first send and the owner would only notice by never being
            # notified. Skip it loudly instead.
            #
            # Real iOS push needs an APNs auth key, and Apple only issues those
            # to paid Developer Program members. It is gated behind the $99/yr
            # either way, so there is nothing to implement here until that is
            # a decision that has been made.
            if (t.get("platform") or "android").lower() != "android":
                log.warning(f"[push] skipping {t.get('platform')} token — "
                            f"FCM cannot deliver to APNs; iOS needs an APNs key")
                continue
            # sticky=True: pops up as a heads-up banner AND stays in the shade
            # until dismissed by hand, instead of vanishing on tap. A signal you
            # glanced at on a lock screen and lost is a signal you did not read.
            # The 'trades' channel is registered IMPORTANCE_HIGH by the app,
            # which is what permits the heads-up at all.
            android_note = {"channel_id": "trades"}
            if sticky:
                android_note["sticky"] = True
                android_note["notification_priority"] = "PRIORITY_MAX"
                android_note["default_vibrate_timings"] = True
            payload = {"message": {"token": t["token"],
                                   "notification": {"title": title, "body": body},
                                   "android": {"priority": "high",
                                               "notification": android_note}}}
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
NOTIF_KEY = "notifications.json"
# Read per call via config.notif_retain_hours() — this used to be a
# module-level constant frozen at import, before SSM secrets had loaded.


def _record(title: str, body: str, tag: str):
    """Keep every alert for config.notif_retain_hours() so it survives being dismissed.

    Tapping a notification opens the app and Android drops it, which is fine for
    a reminder and not fine for a trade signal — the alert carried the entry,
    stop and reasoning, and that was the only copy. This is the copy.

    Deliberately bounded, not a permanent archive: after two days a signal is
    history the Signals tab and picks_log already cover better. A log that only
    grows is one more thing to prune later.
    """
    try:
        now = datetime.now(timezone.utc)
        items = state.load(NOTIF_KEY, default={}).get("items", [])
        items.insert(0, {"ts": now.isoformat(), "title": title,
                         "body": body, "tag": tag})
        cutoff = now.timestamp() - config.notif_retain_hours() * 3600
        items = [i for i in items
                 if datetime.fromisoformat(i["ts"]).timestamp() >= cutoff][:200]
        state.save(NOTIF_KEY, {"items": items})
    except Exception as e:
        log.warning(f"[push] could not record notification: {e}")


def history() -> list:
    """Alerts from the retention window, newest first."""
    try:
        cutoff = datetime.now(timezone.utc).timestamp() - config.notif_retain_hours() * 3600
        items = state.load(NOTIF_KEY, default={}).get("items", [])
        return [i for i in items
                if datetime.fromisoformat(i["ts"]).timestamp() >= cutoff]
    except Exception:
        return []


def _fanout(title: str, body: str, tag: str, sticky: bool = False):
    """One call, every registered client — browser and native alike."""
    _record(title, body, tag)          # before sending: a delivery failure
                                       # must not also lose the record of it
    web = send(title, body, tag=tag, sticky=sticky)
    nat = send_native(title, body, sticky=sticky)
    log.info(f"[push] {tag}: {web.get('sent', 0)} web, {nat} native")


def _strat_name(strategy: str) -> str:
    return {"s1": "Trend Pullback", "s2": "Breakout",
            "s3": "Leader Dip"}.get((strategy or "").lower(), (strategy or "?").upper())


def _tech_lines(p: dict) -> list:
    """The technical snapshot, as lines — shared by the signal and buy pushes.

    Deliberately the same fields the Telegram alert leads with, in the same
    order. Two channels describing the same event in different shapes makes you
    re-read both to check they agree; this way a glance at either is enough.

    Markdown is NOT used: Telegram renders *bold*, a push notification shows
    the asterisks.
    """
    out = []
    price, rsi = p.get("price", 0), p.get("rsi", 0)
    macd, atr = p.get("macd", 0), p.get("atr_pct", 0)
    bits = [f"${price:,.2f}"]
    if rsi:  bits.append(f"RSI {rsi:.0f}")
    if macd: bits.append(f"MACD {macd:+.2f}")
    if atr:  bits.append(f"ATR {atr:.1f}%")
    out.append(" · ".join(bits))

    mom, rel = p.get("mom_3m", 0), p.get("rel_strength", 0)
    if mom or rel:
        line = f"3M {mom:+.1f}%"
        if rel:
            line += f" · vs SPY {rel:+.1f}%"
        if p.get("in_golden_pocket"):
            line += " · 🎯 Golden Pocket"
        out.append(line)
    return out


def _exit_levels(entry: float, atr_pct: float, strategy: str) -> Optional[dict]:
    """Where this trade would actually be closed, from the LIVE exit config.

    Read from profit_monitor rather than restated here, so an alert can never
    quote a stop the exit engine is not using. Floors included — the trail has
    them for the same reason the stop does (a 0.10%-ATR ETF would otherwise arm
    at +0.20% and exit on noise).
    """
    if not entry or not atr_pct:
        return None
    # Reads the shared config directly rather than reaching into the exit
    # engine. That import used to point at profit_monitor._exit_config while
    # profit_monitor imported push back — a genuine cycle that only stayed
    # unbroken because both sides imported lazily, inside functions.
    cfg = config.exit_config()
    mult = cfg.stop_atr_mult_for(strategy)
    stop_pct = max(cfg.stop_min_pct, min(cfg.stop_max_pct, atr_pct * mult))
    arm_pct = max(cfg.trail_activate_min_pct, cfg.trail_activate_atr * atr_pct)
    trail_pct = max(cfg.trail_min_pct, cfg.trail_atr_mult * atr_pct)
    return {"stop_pct": stop_pct, "stop_price": entry * (1 - stop_pct / 100),
            "arm_pct": arm_pct, "arm_price": entry * (1 + arm_pct / 100),
            "trail_pct": trail_pct}


def format_signal(p: dict, strategy: str) -> tuple:
    """(title, body) for a high-conviction pick — ONE definition, both channels.

    Telegram and push previously built their own text from the same pick, which
    is how two descriptions of one event drift apart until you have to read both
    to trust either. Plain text, no markdown: Telegram renders *bold*, a push
    notification just shows the asterisks.
    """
    t     = p.get("ticker", "?")
    entry = p.get("price") or 0
    atr   = p.get("atr_pct")
    rsi   = p.get("rsi") or 0
    macd  = p.get("macd") or 0
    ema20 = p.get("ema20") or 0

    rsi_note  = "oversold" if rsi < 35 else "overbought" if rsi > 70 else "neutral"
    macd_note = "bullish" if macd > 0 else "bearish"

    L = [f"Strategy: {_strat_name(strategy)} | Score: {p.get('score', 0)}/100", ""]

    L.append(f"💵 Entry: ${entry:,.2f}")
    lv = _exit_levels(entry, atr, strategy)
    if lv:
        L.append(f"🎯 Target: Trailing exit (ATR-scaled) — arms at "
                 f"+{lv['arm_pct']:.1f}% (${lv['arm_price']:,.2f}), "
                 f"trails {lv['trail_pct']:.1f}% from peak")
        L.append("   (dynamic — not a fixed broker order)")
        L.append(f"🛑 Stop-loss: ${lv['stop_price']:,.2f} "
                 f"(-{lv['stop_pct']:.2f}%, ATR-scaled)")
    L.append("")

    L.append("📊 Technical Snapshot")
    L.append(f"   RSI {rsi:.0f} ({rsi_note}) | MACD {macd:+.2f} ({macd_note})")
    if ema20:
        L.append(f"   EMA20 ${ema20:,.2f} (price {(entry - ema20) / ema20 * 100:+.1f}% away)")
    mom, rel = p.get("mom_3m"), p.get("rel_strength")
    if mom is not None:
        line = f"   3M momentum {mom:+.1f}%"
        if rel is not None:
            line += f" | Rel. strength vs SPY {rel:+.1f}%"
        L.append(line)
    if atr:
        L.append(f"   ATR {atr:.1f}%")
    if p.get("in_golden_pocket"):
        L.append("   🎯 In Golden Pocket (0.618–0.786 fib)")
    L.append("")

    reasons = p.get("reasons") or []
    if reasons:
        L.append("📝 Signal:")
        L += [f"   • {r}" for r in reasons[:3]]
        L.append("")

    L.append("Score ≥ 75 = high conviction. Review and act.")
    return f"🔥 CONFIDENT BUY — {t}", "\n".join(L)


def notify_signal(p: dict, strategy: str):
    """High-conviction pick — the push twin of the Telegram CONFIDENT BUY.

    This is the notification with the most reason to interrupt someone: it is
    the one that may need a decision, and it arrives before any order exists.
    """
    title, body = format_signal(p, strategy)
    _fanout(title, body, f"signal-{p.get('ticker', '?')}", sticky=True)


def notify_buy(ticker: str, usd: float, strategy: str, score=None,
               pick: Optional[dict] = None):
    """An order was actually placed. Carries the exit plan, not just the entry.

    The stop is the number worth knowing at 3am, and it was missing before —
    the push said what was bought but never what would close it.
    """
    head = f"✅ Bought {ticker} · ${usd:,.0f}"
    body = [f"{_strat_name(strategy)}" + (f" · Score {score}/100" if score else "")]
    if pick:
        body += _tech_lines(pick)
        entry = pick.get("price", 0)
        atr_pct = pick.get("atr_pct", 0)
        if entry and atr_pct:
            mult = {"s2": 3.0, "s3": 3.0}.get((strategy or "").lower(), 2.5)
            stop_pct = max(1.5, min(25.0, atr_pct * mult))
            body.append(f"Stop ~${entry * (1 - stop_pct / 100):,.2f} ({-stop_pct:.1f}%, ATR-scaled)")
    _fanout(head, "\n".join(body), f"buy-{ticker}", sticky=True)


def notify_exit(ticker: str, pnl: float, pct: float, reason: str):
    sign = "+" if pnl >= 0 else ""
    _fanout(f"Exited {ticker} {sign}{pct:.1f}%",
            f"{sign}${pnl:,.2f} · {reason}", f"exit-{ticker}", sticky=True)


def notify_scan(per_strategy: dict):
    """Daily scan summary — ONE notification, not one per strategy.

    Scans were deliberately excluded at first: a channel that fires on
    everything gets dismissed by reflex, and then the stop-out is dismissed with
    it. Included now by request, but as a single digest rather than three
    separate pushes, and only for scans that actually found something. A
    notification that says "nothing today" three times a day is precisely the
    kind that teaches you to swipe without reading.

    Set PUSH_SCANS=0 to turn these off again without a code change.
    """
    if not config.push_scans_enabled():
        return
    parts, total = [], 0
    for strat in ("s3", "s1", "s2"):          # best evidence first
        picks = per_strategy.get(strat) or []
        total += len(picks)
        if picks:
            top = picks[0]
            parts.append(f"{strat.upper()} {top.get('ticker','?')} {top.get('score','')}")
    if not total:
        return                                 # quiet days stay quiet
    _fanout("Pre-market scan",
            f"{total} signal(s) · " + " · ".join(parts),
            "scan", sticky=True)


def notify_error(what: str):
    _fanout("RaanuBot problem", what[:180], "error")
