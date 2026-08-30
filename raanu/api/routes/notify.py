"""raanu.api.routes.notify"""

from __future__ import annotations

import logging
import os

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


@router.get("/api/test/twilio")
async def test_twilio():
    """Debug endpoint — shows what Twilio credentials Railway sees and tests them."""
    import httpx
    sid   = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    frm   = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886").strip()
    to    = os.getenv("USER_WHATSAPP", "whatsapp:+919176911755").strip()

    if not sid or not token:
        return {"error": "missing_creds", "sid_set": bool(sid), "token_set": bool(token)}

    try:
        resp = httpx.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            auth=(sid, token),
            data={"From": frm, "To": to, "Body": "RaanuBot test message ✅"},
            timeout=15,
        )
        return {
            "status_code": resp.status_code,
            "sid_prefix":  sid[:8] + "...",
            "token_prefix": token[:6] + "...",
            "from": frm,
            "to":   to,
            "twilio_response": resp.json(),
        }
    except Exception as e:
        return {"error": str(e)}
