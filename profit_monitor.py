"""
profit_monitor.py — Auto exit with a trailing stop
===================================================
Polls open Alpaca positions every CHECK_INTERVAL seconds and closes a
position when any of these fire (checked in order):

  1. HARD STOP-LOSS  — price fell to -STOP_LOSS_PCT from entry. Cut losers fast.
  2. TRAILING STOP   — once a position has run up to +TRAIL_ACTIVATE_PCT, we
     track its peak and exit if it gives back TRAIL_PCT from that peak. This
     lets winners run well past the old fixed +5% cap while locking in gains —
     the natural exit for a trend/momentum strategy.
  3. HARD TAKE-PROFIT — optional ceiling backstop (disabled by default).

Per-position peak prices are persisted so the trail survives restarts.
Closing a position also sends a notification.
"""

import os
import json
import asyncio
import logging
from pathlib import Path

import httpx

log = logging.getLogger("raanu.profit")

_exit_config = {
    "stop_loss_pct":       float(os.getenv("STOP_LOSS_PCT", "3.0")),
    "trail_activate_pct":  float(os.getenv("TRAIL_ACTIVATE_PCT", os.getenv("TAKE_PROFIT_PCT", "5.0"))),
    "trail_pct":           float(os.getenv("TRAIL_PCT", "2.5")),
    "hard_take_profit_pct": float(os.getenv("HARD_TAKE_PROFIT_PCT", "0")),
    "check_interval":      int(os.getenv("PROFIT_CHECK_SEC", "300")),
}

STOP_LOSS_PCT       = _exit_config["stop_loss_pct"]
TRAIL_ACTIVATE_PCT  = _exit_config["trail_activate_pct"]
TRAIL_PCT           = _exit_config["trail_pct"]
HARD_TAKE_PROFIT_PCT = _exit_config["hard_take_profit_pct"]
CHECK_INTERVAL      = _exit_config["check_interval"]


def get_exit_config() -> dict:
    return dict(_exit_config)


def update_exit_config(updates: dict) -> dict:
    global STOP_LOSS_PCT, TRAIL_ACTIVATE_PCT, TRAIL_PCT, HARD_TAKE_PROFIT_PCT, CHECK_INTERVAL
    for k, v in updates.items():
        if k in _exit_config:
            _exit_config[k] = float(v) if k != "check_interval" else int(v)
    STOP_LOSS_PCT       = _exit_config["stop_loss_pct"]
    TRAIL_ACTIVATE_PCT  = _exit_config["trail_activate_pct"]
    TRAIL_PCT           = _exit_config["trail_pct"]
    HARD_TAKE_PROFIT_PCT = _exit_config["hard_take_profit_pct"]
    CHECK_INTERVAL      = _exit_config["check_interval"]
    log.info(f"Exit config updated: {_exit_config}")
    return dict(_exit_config)

# Peak-price store — survives restarts so the trailing stop keeps its high-water
# mark. Uses /tmp on Railway (no local .env), else the project dir.
_HERE = Path(__file__).parent
_DATA_DIR = Path("/tmp") if Path("/tmp").exists() and not (_HERE / ".env").exists() else _HERE
_PEAKS_FILE = _DATA_DIR / "position_peaks.json"


def _load_peaks() -> dict:
    try:
        return json.loads(_PEAKS_FILE.read_text())
    except Exception:
        return {}


def _save_peaks(peaks: dict) -> None:
    try:
        _PEAKS_FILE.write_text(json.dumps(peaks))
    except Exception as e:
        log.warning(f"Could not persist position peaks: {e}")


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

    hard_tp = f" | hard TP +{HARD_TAKE_PROFIT_PCT}%" if HARD_TAKE_PROFIT_PCT > 0 else ""
    log.info(
        f"Profit monitor: SL -{STOP_LOSS_PCT}% | trailing {TRAIL_PCT}% "
        f"(arms at +{TRAIL_ACTIVATE_PCT}%){hard_tp} | check every {CHECK_INTERVAL}s"
    )

    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            positions = await _get_positions()
        except Exception as e:
            log.warning(f"Profit monitor: failed to fetch positions: {e}")
            continue

        peaks = _load_peaks()
        open_symbols = set()

        for pos in positions:
            symbol  = pos.get("symbol", "")
            entry   = float(pos.get("avg_entry_price", 0))
            current = float(pos.get("current_price", 0))
            qty     = float(pos.get("qty", 0))
            side    = pos.get("side", "long")

            if entry <= 0 or current <= 0 or side != "long":
                continue

            open_symbols.add(symbol)
            pct = (current - entry) / entry * 100
            pnl = (current - entry) * qty

            # Update the high-water mark for this position.
            peak = max(peaks.get(symbol, entry), current)
            peaks[symbol] = peak
            peak_pct = (peak - entry) / entry * 100
            drop_from_peak = (peak - current) / peak * 100 if peak > 0 else 0.0

            reason = None
            if pct <= -STOP_LOSS_PCT:
                reason = f"Stop-loss {pct:.2f}% ≤ -{STOP_LOSS_PCT}%"
            elif HARD_TAKE_PROFIT_PCT > 0 and pct >= HARD_TAKE_PROFIT_PCT:
                reason = f"Take-profit +{pct:.2f}% ≥ +{HARD_TAKE_PROFIT_PCT}%"
            elif peak_pct >= TRAIL_ACTIVATE_PCT and drop_from_peak >= TRAIL_PCT:
                reason = (
                    f"Trailing stop — locked +{pct:.2f}% "
                    f"(peak +{peak_pct:.2f}%, gave back {drop_from_peak:.2f}%)"
                )

            if not reason:
                continue

            log.info(f"Closing {symbol}: {reason} | P&L ${pnl:+.2f}")
            try:
                await _close_position(symbol)
                peaks.pop(symbol, None)
                open_symbols.discard(symbol)
                send_whatsapp(
                    format_profit_alert(symbol, entry, current, pnl, pct, reason)
                )
            except Exception as e:
                log.error(f"Failed to close {symbol}: {e}")

        # Prune peaks for positions that are no longer open.
        peaks = {s: p for s, p in peaks.items() if s in open_symbols}
        _save_peaks(peaks)


async def get_positions_for_status() -> tuple[list[dict], dict]:
    """Helper for WhatsApp STATUS command."""
    positions = await _get_positions()
    account   = await _get_account()
    return positions, account
