"""
profit_monitor.py — Auto exit with a trailing stop
===================================================
Polls open Alpaca positions every CHECK_INTERVAL seconds and closes a
position when any of these fire (checked in order):

  1. HARD STOP-LOSS  — price fell to -STOP_LOSS_PCT from entry. Cut losers fast.
  2. HARD TAKE-PROFIT — optional ceiling backstop (disabled when 0).
  3. TRAILING STOP   — once a position has run up to +TRAIL_ACTIVATE_PCT, we
     track its peak and exit if it gives back TRAIL_PCT from that peak.
  4. DAILY CRASH      — stock dropped DAILY_CRASH_PCT% from previous close in
     a single session. Catches flash crashes / gap-downs even when the position
     is still above its entry price.

Per-position peak prices are persisted so the trail survives restarts.
Closing a position also sends a notification.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from raanu import config, state

log = logging.getLogger("raanu.profit")

def parse_ladder(spec: str) -> list[tuple[float, float]]:
    """Parse "peak:lock,peak:lock" into rungs sorted by peak threshold."""
    rungs: list[tuple[float, float]] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            peak, lock = part.split(":")
            rungs.append((float(peak), float(lock)))
        except ValueError:
            log.warning(f"Ignoring malformed profit-ladder rung: {part!r}")
    return sorted(rungs)


def locked_floor(peak_pct: float, rungs: list[tuple[float, float]]) -> Optional[float]:
    """Highest profit level locked in, given how far the position has run."""
    floor = None
    for threshold, lock in rungs:
        if peak_pct >= threshold:
            floor = lock
    return floor

def get_exit_config() -> dict:
    """Current exit rules as a plain dict, for GET /api/exit-config."""
    return config.exit_config().as_dict()


def update_exit_config(updates: dict) -> dict:
    """Runtime override from PATCH /api/exit-config.

    The old version mutated a module-level dict and then called
    _refresh_globals() to copy all 18 values into module globals so this
    file could read them as bare names. Both the dict and the globals are
    gone: there is one ExitConfig object and everything reads it directly,
    so the two copies can no longer drift apart.
    """
    updated = config.exit_config().apply(updates)
    log.info(f"Exit config updated: {updated}")
    return updated


_PEAKS_KEY = "position_peaks.json"


def _load_peaks() -> dict:
    """
    Per-position state: {symbol: {"peak": float, "atr": float}}.

    Older files stored a bare float per symbol — upgrade those in place so the
    trailing stop keeps its high-water mark across the format change.
    """
    raw = state.load(_PEAKS_KEY, default={})
    out = {}
    for sym, val in raw.items():
        if isinstance(val, dict):
            out[sym] = val
        else:
            out[sym] = {"peak": float(val), "atr": None}
    return out


def _save_peaks(peaks: dict) -> None:
    state.save(_PEAKS_KEY, peaks)


async def _get_atr(symbol: str, period: int = 14) -> Optional[float]:
    """
    Wilder's ATR in price units, from daily Alpaca bars.

    Captured once when a position is first seen and then frozen, so the stop
    distance stays fixed for the life of the trade rather than drifting as
    volatility changes underneath it.
    """
    bars = await _fetch_daily_bars(symbol, days=period * 8)
    if len(bars) < period + 1:
        log.warning(f"ATR for {symbol}: only {len(bars)} bars, need {period + 1}")
        return None

    atr = None
    for i in range(1, len(bars)):
        prev_close = float(bars[i - 1]["c"])
        tr = max(
            float(bars[i]["h"]) - float(bars[i]["l"]),
            abs(float(bars[i]["h"]) - prev_close),
            abs(float(bars[i]["l"]) - prev_close),
        )
        atr = tr if atr is None else (atr * (period - 1) + tr) / period
    return atr


def _headers() -> dict:
    return {
        "APCA-API-KEY-ID":     config.alpaca_key(),
        "APCA-API-SECRET-KEY": config.alpaca_secret(),
    }


def _base() -> str:
    mode = config.alpaca_mode()
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


async def _market_is_open() -> bool:
    """
    Exit rules must not be evaluated against a stale close price.

    When the market is shut, `current_price` is the last close, so a trailing
    stop can 'fire' on a move that already happened days ago — and the resulting
    order queues rather than filling, leaving the position open so the next poll
    fires again. Five-minute polling would submit a close order all weekend.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{_base()}/clock", headers=_headers())
        return bool(r.json().get("is_open")) if r.status_code == 200 else False
    except Exception as e:
        log.warning(f"Clock check failed, assuming market closed: {e}")
        return False


async def _symbols_with_pending_sell() -> set[str]:
    """Positions already being closed — never submit a second exit order."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{_base()}/orders", headers=_headers(),
                            params={"status": "open", "limit": 500})
        if r.status_code != 200:
            return set()
        return {
            (o.get("symbol") or "").upper()
            for o in r.json() if o.get("side") == "sell"
        }
    except Exception:
        return set()


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


async def _fetch_daily_bars(symbol: str, days: int) -> list[dict]:
    """
    Daily bars from the Alpaca data API.

    An explicit `start` is REQUIRED — without a date range the endpoint returns
    {"bars": null} rather than an error, which silently disabled every rule
    that depended on historical bars.
    """
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
                headers=_headers(),
                params={
                    "timeframe": "1Day",
                    "start": start,
                    "limit": "1000",
                    "feed": config.alpaca_data_feed(),
                },
            )
        if r.status_code != 200:
            log.warning(f"Bars fetch for {symbol} returned {r.status_code}")
            return []
        return r.json().get("bars") or []
    except Exception as e:
        log.warning(f"Bars fetch failed for {symbol}: {e}")
        return []


async def _get_prev_close(symbol: str) -> Optional[float]:
    """Previous trading-day close."""
    bars = await _fetch_daily_bars(symbol, days=10)
    if len(bars) >= 2:
        return float(bars[-2]["c"])
    return None


def strategy_for(symbol: str) -> str:
    """
    Which strategy opened this position, from the most recent BUY in the trade
    log. Returns "unknown" when there is no BUY on record.

    It used to return "s1" for those, which was a guess presented as a fact.
    The dashboard said UNTAGGED for the same position while the exit engine
    treated it as S1, and worse, `_record_exit` then wrote "s1" into the trade
    log when it closed — so an unattributable trade's P&L permanently joined
    S1's track record, which is what kelly.py sizes future positions from.

    "unknown" costs nothing here: no `stop_atr_mult_unknown` or
    `profit_ladder_unknown` key exists, so both fall through to the shared
    defaults, which are identical to the S1 values. Same exits, honest label.
    """
    try:
        from raanu.trading.trader import get_trader
        for t in reversed(get_trader().tradelog.data.get("trades", [])):
            if t.get("action") == "BUY" and (t.get("ticker") or "").upper() == symbol.upper():
                return t.get("strategy") or "unknown"
    except Exception:
        pass
    return "unknown"


def stop_atr_mult_for(strategy: str) -> float:
    """Per-strategy ATR multiple, falling back to the shared default."""
    return config.exit_config().stop_atr_mult_for(strategy)


def ladder_for(strategy: str) -> list[tuple[float, float]]:
    """
    Per-strategy profit ladder. An explicitly empty setting means "no ladder"
    and must NOT fall through to the shared default — S3 is deliberately off.
    """
    return parse_ladder(config.exit_config().ladder_for(strategy))


def _record_exit(symbol: str, entry: float, exit_price: float,
                 qty: float, pnl: float, pct: float, reason: str) -> None:
    """
    Append the closed trade to the shared trade log.

    Without this the log is buy-only, so realized P&L and win rate have
    nothing to compute from. The strategy tag is inherited from the BUY that
    opened the position.
    """
    try:
        from raanu.trading.trader import get_trader

        # "unknown", never "s1" — see strategy_for(). Mislabelling an
        # unattributable exit pollutes the win rate and payoff ratio that
        # position sizing is computed from.
        strategy = "unknown"
        for t in reversed(get_trader().tradelog.data.get("trades", [])):
            if t.get("action") == "BUY" and (t.get("ticker") or "").upper() == symbol.upper():
                strategy = t.get("strategy") or "unknown"
                break

        # Exits are the events most worth interrupting someone for — a stop or
        # trail firing is news. Wrapped: the position is already closed.
        try:
            from raanu.notify import push
            push.notify_exit(symbol.upper(), pnl, pct, reason)
        except Exception as e:
            log.warning(f"[push] exit notify skipped: {e}")

        get_trader().tradelog.record({
            "action":       "SELL",
            "ticker":       symbol.upper(),
            "strategy":     strategy,
            "qty":          qty,
            "entry_price":  entry,
            "exit_price":   exit_price,
            "realized_pnl": round(pnl, 2),
            "return_pct":   round(pct, 2),
            "exit_reason":  reason,
        })
    except Exception as e:
        log.error(f"Could not record exit for {symbol}: {e}")


async def monitor_loop():
    """
    Continuous loop — re-checks on the configured interval.
    Closes positions that hit take-profit or stop-loss.
    """
    cfg = config.exit_config()
    hard_tp = f" | hard TP +{cfg.hard_take_profit_pct}%" if cfg.hard_take_profit_pct > 0 else ""
    crash = f" | daily crash -{cfg.daily_crash_pct}%" if cfg.daily_crash_pct > 0 else ""
    stop_d = (f"{cfg.stop_atr_mult}xATR (cap {cfg.stop_max_pct}%)"
              if cfg.stop_mode == "atr" else f"-{cfg.stop_loss_pct}%")
    trail_d = (f"{cfg.trail_atr_mult}xATR, arms at +{cfg.trail_activate_atr}xATR"
               if cfg.trail_mode == "atr" else f"{cfg.trail_pct}%, arms at +{cfg.trail_activate_pct}%")
    log.info(
        f"Profit monitor: stop {stop_d} | trail {trail_d}"
        f"{hard_tp}{crash} | check every {cfg.check_interval}s"
    )

    while True:
        await asyncio.sleep(cfg.check_interval)
        await run_monitor_once()


async def run_monitor_once():
    """One exit-check pass — checks every open position once and closes any
    that hit a stop/trail/ladder/crash rule. The worker Lambda calls this
    once per invocation; `monitor_loop` wraps it in a sleep loop for local
    development, where there is a persistent process to sleep inside."""
    cfg = config.exit_config()
    from raanu.notify.telegram import send_whatsapp, format_profit_alert

    if not await _market_is_open():
        log.debug("Profit monitor: market closed — skipping exit checks")
        return

    try:
        positions = await _get_positions()
    except Exception as e:
        log.warning(f"Profit monitor: failed to fetch positions: {e}")
        return

    pending_sells = await _symbols_with_pending_sell()
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

        # A close order is already working — the position stays open until
        # it fills, so re-evaluating would stack duplicate exit orders.
        if symbol.upper() in pending_sells:
            log.info(f"{symbol}: close order already pending — skipping")
            continue
        pct = (current - entry) / entry * 100
        pnl = (current - entry) * qty

        strategy = strategy_for(symbol)
        state = peaks.get(symbol) or {}
        # Capture ATR once, the first time we see the position, and freeze
        # it — the stop distance must not drift with changing volatility.
        atr = state.get("atr")
        if atr is None and cfg.stop_mode == "atr":
            atr = await _get_atr(symbol)
        peak = max(float(state.get("peak") or entry), current)
        peaks[symbol] = {"peak": peak, "atr": atr}

        peak_pct = (peak - entry) / entry * 100
        drop_from_peak = (peak - current) / peak * 100 if peak > 0 else 0.0
        atr_pct = (atr / entry * 100) if atr else None

        # ── stop distance ────────────────────────────────────────────────
        if cfg.stop_mode == "atr" and atr_pct:
            mult = stop_atr_mult_for(strategy)
            stop_pct = min(max(mult * atr_pct, cfg.stop_min_pct), cfg.stop_max_pct)
            stop_desc = f"{mult}xATR ({stop_pct:.1f}%)"
        else:
            stop_pct = cfg.stop_loss_pct
            stop_desc = f"{cfg.stop_loss_pct}%"

        reason = None
        if pct <= -stop_pct:
            reason = f"Stop-loss {pct:.2f}% ≤ -{stop_desc}"
        elif cfg.hard_take_profit_pct > 0 and pct >= cfg.hard_take_profit_pct:
            reason = f"Take-profit +{pct:.2f}% ≥ +{cfg.hard_take_profit_pct}%"
        else:
            # ── trailing stop, also ATR-scaled ───────────────────────────
            if cfg.trail_mode == "atr" and atr_pct:
                # Floors matter as much here as on the stop: without them a
                # 0.10%-ATR instrument arms at +0.20% and exits on a 0.15%
                # give-back, closing on noise for a rounding-error gain.
                arm_pct  = max(cfg.trail_activate_atr * atr_pct, cfg.trail_activate_min_pct)
                give_pct = max(cfg.trail_atr_mult * atr_pct, cfg.trail_min_pct)
                trail_desc = f"{cfg.trail_atr_mult}xATR ({give_pct:.1f}%)"
            else:
                arm_pct   = cfg.trail_activate_pct
                give_pct  = cfg.trail_pct
                trail_desc = f"{cfg.trail_pct}%"
            if peak_pct >= arm_pct and drop_from_peak >= give_pct:
                reason = (
                    f"Trailing stop {trail_desc} — locked +{pct:.2f}% "
                    f"(peak +{peak_pct:.2f}%, gave back {drop_from_peak:.2f}%)"
                )

            # Profit ladder — books progressively more the higher it ran.
            if not reason:
                floor = locked_floor(peak_pct, ladder_for(strategy))
                if floor is not None and pct <= floor:
                    reason = (
                        f"Profit ladder — banking +{pct:.2f}% "
                        f"(peak +{peak_pct:.2f}% locked in +{floor:.1f}%)"
                    )

        if not reason and cfg.daily_crash_pct > 0:
            prev_close = await _get_prev_close(symbol)
            if prev_close and prev_close > 0:
                day_drop = (prev_close - current) / prev_close * 100
                if day_drop >= cfg.daily_crash_pct:
                    reason = (
                        f"Daily crash -{day_drop:.2f}% "
                        f"(prev close ${prev_close:.2f} → ${current:.2f}, "
                        f"threshold -{cfg.daily_crash_pct}%)"
                    )

        if not reason:
            continue

        log.info(f"Closing {symbol}: {reason} | P&L ${pnl:+.2f}")
        try:
            await _close_position(symbol)
            peaks.pop(symbol, None)
            open_symbols.discard(symbol)
            _record_exit(symbol, entry, current, qty, pnl, pct, reason)
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
