"""raanu.api.routes.webhooks"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import PlainTextResponse

from raanu import config
from raanu.market.rest import alpaca_headers
from raanu.scanning.engine import top_picks
from raanu.trading.schedule import _load_picks, _load_picks_s2, _save_picks, _save_picks_s2
from raanu.trading.trader import get_trader

log = logging.getLogger("raanu.api.routes.webhooks")

router = APIRouter()


async def _handle_whatsapp_command(cmd: str):
    """
    Runs in a background task — called after TwiML is already returned to Twilio.
    This avoids Twilio's 15-second webhook timeout killing long-running commands.
    """
    from raanu.notify.telegram import (
        format_daily_alert,
        format_portfolio_status,
        format_trade_confirm,
        send_whatsapp,
    )

    try:
        if cmd == "STATUS":
            try:
                from raanu.trading.exits import get_positions_for_status
                positions, account = await get_positions_for_status()
                reply = format_portfolio_status(positions, account)
            except Exception as e:
                reply = f"❌ Could not fetch portfolio: {e}"
            send_whatsapp(reply)

        elif cmd in ("PICKS", "SCAN"):
            send_whatsapp("🔍 Fetching latest picks for both strategies...")
            cached_s1 = _load_picks()
            if cached_s1 and cached_s1.get("picks"):
                picks_s1 = cached_s1["picks"]
            else:
                loop = asyncio.get_event_loop()
                picks_s1 = await loop.run_in_executor(None, lambda: top_picks('s1', limit=3))
                _save_picks(picks_s1)
            send_whatsapp(format_daily_alert(picks_s1, strategy="s1"), strategy="s1")

            cached_s2 = _load_picks_s2()
            if cached_s2 and cached_s2.get("picks"):
                picks_s2 = cached_s2["picks"]
            else:
                loop = asyncio.get_event_loop()
                picks_s2 = await loop.run_in_executor(None, lambda: top_picks('s2', limit=3))
                _save_picks_s2(picks_s2)
            send_whatsapp(format_daily_alert(picks_s2, strategy="s2"), strategy="s2")

        elif cmd.startswith("BUY "):
            parts  = cmd.split()
            ticker = parts[1] if len(parts) > 1 else ""
            usd    = float(parts[2]) if len(parts) > 2 else float(os.getenv("PER_TRADE_MAX_USD", "500"))
            if not ticker:
                send_whatsapp("❌ Usage: BUY TICKER or BUY TICKER 200")
            else:
                try:
                    body = {"symbol": ticker.upper(), "notional": str(usd), "side": "buy", "type": "market", "time_in_force": "day"}
                    async with httpx.AsyncClient(timeout=20) as c:
                        r = await c.post(f"{config.broker_base()}/orders", headers=alpaca_headers(), json=body)
                    if r.status_code >= 400:
                        send_whatsapp(f"❌ Order rejected: {r.text[:200]}")
                    else:
                        resp = r.json()
                        # Record it, or the position is unattributable forever.
                        # This path posts straight to Alpaca and used to write
                        # nothing to the trade log, so a chat-placed BUY showed
                        # as Untagged, was missing from strategy stats, and
                        # never reached Kelly's sample. Tagged "manual" for the
                        # same reason as /api/orders/buy: the weekly limit
                        # counts BUY entries per strategy, and a chat order
                        # must not silently spend the auto-trader's budget.
                        try:
                            get_trader().tradelog.record({
                                "action":   "BUY",
                                "ticker":   ticker.upper(),
                                "strategy": "manual",
                                "usd":      usd,
                                "source":   "telegram-cmd",
                                "order_id": resp.get("id"),
                            })
                        except Exception as e:
                            log.error(f"Chat BUY placed but trade log write failed for {ticker}: {e}")
                        send_whatsapp(format_trade_confirm("BUY", ticker, usd, resp.get("status", "submitted")))
                except Exception as e:
                    send_whatsapp(f"❌ Buy failed: {e}")

        elif cmd.startswith("SELL "):
            parts  = cmd.split()
            ticker = parts[1] if len(parts) > 1 else ""
            if not ticker:
                send_whatsapp("❌ Usage: SELL TICKER")
            else:
                try:
                    async with httpx.AsyncClient(timeout=20) as c:
                        r = await c.delete(f"{config.broker_base()}/positions/{ticker.upper()}", headers=alpaca_headers())
                    if r.status_code == 404:
                        send_whatsapp(f"⚠️ No open position in {ticker}")
                    elif r.status_code >= 400:
                        send_whatsapp(f"❌ Sell failed: {r.text[:200]}")
                    else:
                        result = r.json()
                        qty    = float(result.get("qty", 0))
                        price  = float(result.get("avg_entry_price", 0))
                        send_whatsapp(format_trade_confirm("SELL", ticker, qty * price, result.get("status", "submitted")))
                except Exception as e:
                    send_whatsapp(f"❌ Sell failed: {e}")

        else:
            send_whatsapp(
                "🤖 *RaanuTradingBot commands:*\n"
                "  *PICKS* — today's top 3 picks\n"
                "  *BUY AAPL* — buy $500 of AAPL\n"
                "  *BUY AAPL 200* — buy $200 of AAPL\n"
                "  *SELL AAPL* — close position\n"
                "  *STATUS* — portfolio summary"
            )

    except Exception as e:
        log.error(f"WhatsApp command handler error for '{cmd}': {e}")
        try:
            from raanu.notify.telegram import send_whatsapp
            send_whatsapp(f"❌ Internal error: {e}")
        except Exception:
            pass


@router.post("/webhook/whatsapp")
async def whatsapp_webhook(
    request: Request,
    Body: str = Form(default=""),
    From: str = Form(default=""),
):
    """
    Twilio sends incoming WhatsApp messages here.
    Returns TwiML immediately so we stay well within Twilio's 15s timeout.
    Command handling runs in a background task.

    ⚠️ This route is OUTSIDE `/api/`, so `api_auth_gate` never sees it: no
    read passphrase, no trade PIN. The sender check below is the only thing
    standing in front of `BUY`/`SELL`, which place real orders.

    It used to fail OPEN, twice over:

      * `if From and From != expected` — omitting the form field entirely
        made the condition false and skipped the check. A bare
        `curl -d 'Body=BUY NVDA 5000' .../webhook/whatsapp`, with no
        credentials of any kind, reached the order path. Verified against
        this code, not inferred.
      * the expected number was a hardcoded default in the source, so the
        check "passed" against a value published in the repository.

    Now it fails CLOSED: unset `USER_WHATSAPP` rejects everything. That is
    the safe default precisely because Twilio is retired — there is no
    legitimate sender left, so the correct behaviour for an unconfigured
    deployment is to accept nothing.
    """
    expected_from = config.user_whatsapp()
    if not expected_from or not From or From != expected_from:
        # Deliberately identical response in every rejected case: telling a
        # caller *why* it was refused tells them how to look like the owner.
        log.warning(
            "Rejected /webhook/whatsapp: "
            f"{'USER_WHATSAPP not configured' if not expected_from else 'sender not recognised'}"
        )
        return PlainTextResponse("<?xml version='1.0'?><Response/>", media_type="text/xml")

    cmd = Body.strip().upper()
    log.info(f"WhatsApp command: '{cmd}' from {From}")

    # Fire-and-forget — respond to Twilio immediately to avoid timeout
    asyncio.create_task(_handle_whatsapp_command(cmd))

    return PlainTextResponse("<?xml version='1.0'?><Response/>", media_type="text/xml")
