"""raanu.api.routes.notify"""

from __future__ import annotations

import logging

from fastapi import APIRouter

log = logging.getLogger("raanu.api.routes.notify")

router = APIRouter()


@router.post("/api/telegram/test")
def telegram_test():
    from raanu.notify.telegram import _chat_id_for, is_configured, send_telegram
    if not is_configured():
        return {"ok": False, "error": "Telegram not configured — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env"}
    ok1 = send_telegram("🧪 *RaanuTradingBot — Test*\n📊 S1 Pullback channel working.", strategy="s1")
    ok2 = send_telegram("🧪 *RaanuTradingBot — Test*\n🚀 S2 Breakout channel working.", strategy="s2")
    return {
        "ok": ok1 and ok2,
        "s1_chat": _chat_id_for("s1")[-4:] if _chat_id_for("s1") else "not set",
        "s2_chat": _chat_id_for("s2")[-4:] if _chat_id_for("s2") else "not set",
        "error": None if (ok1 and ok2) else "One or both sends failed — check chat IDs",
    }


# GET /api/test/twilio was removed here, deliberately. It echoed
# `sid[:8]` and `token[:6]` of the live Twilio credentials straight back in
# the response body — credential material handed out by an endpoint that
# only needed the read passphrase, and logged wherever the response went.
#
# It was also dead: Twilio is no longer a dependency of this project. The
# WhatsApp path was replaced by Telegram and `send_whatsapp()` is now a
# one-line alias for `send_telegram()` (see raanu/notify/telegram.py); no
# code reads TWILIO_ACCOUNT_SID, and `twilio` is not in requirements.txt.
#
# On top of that it SENT a real message on a GET, so anything that prefetches
# links — a browser, a crawler, a chat client unfurling a URL — could fire it.
