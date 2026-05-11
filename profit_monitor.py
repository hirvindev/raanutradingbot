"""
profit_monitor.py — Auto sell at take-profit / stop-loss
=========================================================
Polls open Alpaca positions every CHECK_INTERVAL seconds.
When a position hits take-profit or stop-loss threshold:
  1. Closes the position via Alpaca API
  2. Sends a WhatsApp notification
"""

import os
import asyncio
import logging
import httpx
from datetime import datetime, timezone

log = logging.getLogger("raanu.profit")

TAKE_PROFIT_PCT   = float(os.getenv("TAKE_PROFIT_PCT", "5.0"))
STOP_LOSS_PCT     = float(os.getenv("STOP_LOSS_PCT", "3.0"))
CHECK_INTERVAL    = int(os.getenv("PROFIT_CHECK_SEC", "300"))  # 5 min


def _headers() -> dict:
    return {
        "APCA-API-KEY-ID":     os.getenv("ALPACA_API_KEY", "").strip(),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", "").strip(),
    }


def _base() -> str:
    mode = os.getenv("ALPACA_MODE", "paper").strip().lower()
    return (
        "https://paper-api.alpaca.markets/v2"
        if mode != "live"
        else "https://api.alpaca.markets/v2"
    )


async def _get_positions() -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{_base()}/positions", headers=_headers())
        r.raise_for_status()
        return r.json()


async def _close_position(symbol: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.delete(f"{_base()}/positions/{symbol}", headers=_headers())
        if r.status_code == 404:
            return {"status": "not_found"}
        if r.status_code >= 400:
            raise RuntimeError(f"Close failed {r.status_code}: {r.text}")
        return r.json()


async def _get_account() -> dict:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{_base()}/account", headers=_headers())
        r.raise_for_status()
        return r.json()


async def monitor_loop():
    """
    Continuous loop — checks every CHECK_INTERVAL seconds.
    Closes positions that hit take-profit or stop-loss.
    """
    from notifier import send_whatsapp, format_profit_alert

    log.info(
        f"Profit monitor: TP +{TAKE_PROFIT_PCT}% | SL -{STOP_LOSS_PCT}% | "
        f"check every {CHECK_INTERVAL}s"
    )

    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            positions = await _get_positions()
        except Exception as e:
            log.warning(f"Profit monitor: failed to fetch positions: {e}")
            continue

        for pos in positions:
            symbol  = pos.get("symbol", "")
            entry   = float(pos.get("avg_entry_price", 0))
            current = float(pos.get("current_price", 0))
            qty     = float(pos.get("qty", 0))
            side    = pos.get("side", "long")

            if entry <= 0 or current <= 0 or side != "long":
                continue

            pct = (current - entry) / entry * 100
            pnl = (current - entry) * qty

            if pct >= TAKE_PROFIT_PCT:
                reason = f"Take-profit +{pct:.2f}% ≥ +{TAKE_PROFIT_PCT}%"
            elif pct <= -STOP_LOSS_PCT:
                reason = f"Stop-loss {pct:.2f}% ≤ -{STOP_LOSS_PCT}%"
            else:
                continue

            log.info(f"Closing {symbol}: {reason} | P&L ${pnl:+.2f}")
            try:
                await _close_position(symbol)
                send_whatsapp(
                    format_profit_alert(symbol, entry, current, pnl, pct, reason)
                )
            except Exception as e:
                log.error(f"Failed to close {symbol}: {e}")


async def get_positions_for_status() -> tuple[list[dict], dict]:
    """Helper for WhatsApp STATUS command."""
    positions = await _get_positions()
    account   = await _get_account()
    return positions, account
