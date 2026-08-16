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

import os
import json
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger("raanu.profit")

_exit_config = {
    # "atr" scales the stop to how much the stock actually moves in a day.
    # A fixed percentage stop sits inside the daily range of a volatile name,
    # so it exits on noise rather than on the thesis failing.
    "stop_mode":           os.getenv("STOP_MODE", "atr").strip().lower(),
    # Per-strategy multiples: the backtest preferred 2.5x for S1 pullbacks and
    # 3.0x for S2 breakouts (breakouts need more room to hold through the
    # retest). stop_atr_mult is the fallback for untagged positions.
    "stop_atr_mult":       float(os.getenv("STOP_ATR_MULT", "2.5")),
    "stop_atr_mult_s1":    float(os.getenv("STOP_ATR_MULT_S1", "2.5")),
    "stop_atr_mult_s2":    float(os.getenv("STOP_ATR_MULT_S2", "3.0")),
    "stop_atr_mult_s3":    float(os.getenv("STOP_ATR_MULT_S3", "3.0")),
    "stop_loss_pct":       float(os.getenv("STOP_LOSS_PCT", "3.0")),   # used when stop_mode="pct"
    "stop_max_pct":        float(os.getenv("STOP_MAX_PCT", "25.0")),   # ceiling on an ATR stop
    # Floor: very quiet instruments (min-vol ETFs, merger-arb funds) compute an
    # ATR stop tighter than the bid-ask spread, which would exit on a tick.
    "stop_min_pct":        float(os.getenv("STOP_MIN_PCT", "1.5")),
    "trail_mode":          os.getenv("TRAIL_MODE", "atr").strip().lower(),
    "trail_activate_atr":  float(os.getenv("TRAIL_ACTIVATE_ATR", "2.0")),
    "trail_atr_mult":      float(os.getenv("TRAIL_ATR_MULT", "1.5")),
    # Floors, for the same reason the stop has one. On a very quiet instrument
    # (ARB: 0.10% ATR) an unfloored trail arms at +0.20% and exits on a 0.15%
    # give-back — closing a position on noise for a rounding-error gain.
    "trail_min_pct":          float(os.getenv("TRAIL_MIN_PCT", "3.0")),
    "trail_activate_min_pct": float(os.getenv("TRAIL_ACTIVATE_MIN_PCT", "2.5")),
    "trail_activate_pct":  float(os.getenv("TRAIL_ACTIVATE_PCT", os.getenv("TAKE_PROFIT_PCT", "5.0"))),
    "trail_pct":           float(os.getenv("TRAIL_PCT", "2.5")),
    "hard_take_profit_pct": float(os.getenv("HARD_TAKE_PROFIT_PCT", "0")),
    "daily_crash_pct":     float(os.getenv("DAILY_CRASH_PCT", "8.0")),
    "check_interval":      int(os.getenv("PROFIT_CHECK_SEC", "300")),
}

_STR_KEYS = {"stop_mode", "trail_mode",
             "profit_ladder", "profit_ladder_s1", "profit_ladder_s2", "profit_ladder_s3"}

# ── Profit ladder (ratchet), per strategy ────────────────────────────────────
# "book progressively more as the trade goes higher": each rung says once the
# position's PEAK gain reaches X%, never give back below a locked Y% profit.
# The floor only ever ratchets up — it cannot fall as the price falls.
#
#   "5:2,10:6,15:11,20:15,30:24"  ->  peak +10% locks in at least +6%
#
# It runs alongside the trailing stop; whichever triggers first exits.
#
# The ladder is NOT universally good, which is why it is per-strategy:
#   S2 breakouts  — helped:  +13.26% -> +15.55%
#   S3 leader dip — HURT:    +33.89% -> +22.34%, because it lifts win rate
#                            59.4% -> 68.7% while payoff collapses 0.93 -> 0.58.
#                            It books winners before they mature.
# An empty string disables it for that strategy.
_DEFAULT_LADDER = "5:2,10:6,15:11,20:15,30:24"
_exit_config["profit_ladder"]    = os.getenv("PROFIT_LADDER", _DEFAULT_LADDER)
_exit_config["profit_ladder_s1"] = os.getenv("PROFIT_LADDER_S1", _DEFAULT_LADDER)
_exit_config["profit_ladder_s2"] = os.getenv("PROFIT_LADDER_S2", _DEFAULT_LADDER)
_exit_config["profit_ladder_s3"] = os.getenv("PROFIT_LADDER_S3", "")  # off — backtest says it costs return


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

STOP_MODE           = _exit_config["stop_mode"]
STOP_ATR_MULT       = _exit_config["stop_atr_mult"]
STOP_LOSS_PCT       = _exit_config["stop_loss_pct"]
STOP_MAX_PCT        = _exit_config["stop_max_pct"]
STOP_MIN_PCT        = _exit_config["stop_min_pct"]
TRAIL_MODE          = _exit_config["trail_mode"]
TRAIL_ACTIVATE_ATR  = _exit_config["trail_activate_atr"]
TRAIL_ATR_MULT      = _exit_config["trail_atr_mult"]
TRAIL_MIN_PCT       = _exit_config["trail_min_pct"]
TRAIL_ACTIVATE_MIN_PCT = _exit_config["trail_activate_min_pct"]
TRAIL_ACTIVATE_PCT  = _exit_config["trail_activate_pct"]
TRAIL_PCT           = _exit_config["trail_pct"]
HARD_TAKE_PROFIT_PCT = _exit_config["hard_take_profit_pct"]
DAILY_CRASH_PCT     = _exit_config["daily_crash_pct"]
CHECK_INTERVAL      = _exit_config["check_interval"]
PROFIT_LADDER       = parse_ladder(_exit_config["profit_ladder"])


def _refresh_globals() -> None:
    global STOP_MODE, STOP_ATR_MULT, STOP_LOSS_PCT, STOP_MAX_PCT, STOP_MIN_PCT
    global TRAIL_MODE, TRAIL_ACTIVATE_ATR, TRAIL_ATR_MULT, TRAIL_MIN_PCT, TRAIL_ACTIVATE_MIN_PCT
    global TRAIL_ACTIVATE_PCT, TRAIL_PCT, HARD_TAKE_PROFIT_PCT
    global DAILY_CRASH_PCT, CHECK_INTERVAL, PROFIT_LADDER
    STOP_MODE           = _exit_config["stop_mode"]
    STOP_ATR_MULT       = _exit_config["stop_atr_mult"]
    STOP_LOSS_PCT       = _exit_config["stop_loss_pct"]
    STOP_MAX_PCT        = _exit_config["stop_max_pct"]
    STOP_MIN_PCT        = _exit_config["stop_min_pct"]
    TRAIL_MODE          = _exit_config["trail_mode"]
    TRAIL_ACTIVATE_ATR  = _exit_config["trail_activate_atr"]
    TRAIL_ATR_MULT      = _exit_config["trail_atr_mult"]
    TRAIL_MIN_PCT       = _exit_config["trail_min_pct"]
    TRAIL_ACTIVATE_MIN_PCT = _exit_config["trail_activate_min_pct"]
    TRAIL_ACTIVATE_PCT  = _exit_config["trail_activate_pct"]
    TRAIL_PCT           = _exit_config["trail_pct"]
    HARD_TAKE_PROFIT_PCT = _exit_config["hard_take_profit_pct"]
    DAILY_CRASH_PCT     = _exit_config["daily_crash_pct"]
    CHECK_INTERVAL      = _exit_config["check_interval"]
    PROFIT_LADDER       = parse_ladder(_exit_config["profit_ladder"])


def get_exit_config() -> dict:
    return dict(_exit_config)


def update_exit_config(updates: dict) -> dict:
    for k, v in updates.items():
        if k not in _exit_config:
            continue
        if k in _STR_KEYS:
            _exit_config[k] = str(v).strip().lower()
        elif k == "check_interval":
            _exit_config[k] = int(v)
        else:
            _exit_config[k] = float(v)
    _refresh_globals()
    log.info(f"Exit config updated: {_exit_config}")
    return dict(_exit_config)

# Peak-price store — survives restarts so the trailing stop keeps its high-water
# mark. Uses /tmp on Railway (no local .env), else the project dir.
_HERE = Path(__file__).parent
from datadir import state_path
_PEAKS_FILE = state_path("position_peaks.json")


def _load_peaks() -> dict:
    """
    Per-position state: {symbol: {"peak": float, "atr": float}}.

    Older files stored a bare float per symbol — upgrade those in place so the
    trailing stop keeps its high-water mark across the format change.
    """
    try:
        raw = json.loads(_PEAKS_FILE.read_text())
    except Exception:
        return {}
    out = {}
    for sym, val in raw.items():
        if isinstance(val, dict):
            out[sym] = val
        else:
            out[sym] = {"peak": float(val), "atr": None}
    return out


def _save_peaks(peaks: dict) -> None:
    try:
        _PEAKS_FILE.write_text(json.dumps(peaks))
    except Exception as e:
        log.warning(f"Could not persist position peaks: {e}")


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
                    "feed": os.getenv("ALPACA_DATA_FEED", "iex"),
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
        from auto_trader import trader
        for t in reversed(trader.tradelog.data.get("trades", [])):
            if t.get("action") == "BUY" and (t.get("ticker") or "").upper() == symbol.upper():
                return t.get("strategy") or "unknown"
    except Exception:
        pass
    return "unknown"


def stop_atr_mult_for(strategy: str) -> float:
    """Per-strategy ATR multiple, falling back to the shared default."""
    return _exit_config.get(f"stop_atr_mult_{strategy}") or _exit_config["stop_atr_mult"]


def ladder_for(strategy: str) -> list[tuple[float, float]]:
    """
    Per-strategy profit ladder. An explicitly empty setting means "no ladder"
    and must NOT fall through to the shared default — S3 is deliberately off.
    """
    key = f"profit_ladder_{strategy}"
    spec = _exit_config[key] if key in _exit_config else _exit_config["profit_ladder"]
    return parse_ladder(spec)


def _record_exit(symbol: str, entry: float, exit_price: float,
                 qty: float, pnl: float, pct: float, reason: str) -> None:
    """
    Append the closed trade to the shared trade log.

    Without this the log is buy-only, so realized P&L and win rate have
    nothing to compute from. The strategy tag is inherited from the BUY that
    opened the position.
    """
    try:
        from auto_trader import trader

        # "unknown", never "s1" — see strategy_for(). Mislabelling an
        # unattributable exit pollutes the win rate and payoff ratio that
        # position sizing is computed from.
        strategy = "unknown"
        for t in reversed(trader.tradelog.data.get("trades", [])):
            if t.get("action") == "BUY" and (t.get("ticker") or "").upper() == symbol.upper():
                strategy = t.get("strategy") or "unknown"
                break

        # Exits are the events most worth interrupting someone for — a stop or
        # trail firing is news. Wrapped: the position is already closed.
        try:
            import push
            push.notify_exit(symbol.upper(), pnl, pct, reason)
        except Exception as e:
            log.warning(f"[push] exit notify skipped: {e}")

        trader.tradelog.record({
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
    Continuous loop — checks every CHECK_INTERVAL seconds.
    Closes positions that hit take-profit or stop-loss.
    """
    from notifier import send_whatsapp, format_profit_alert

    hard_tp = f" | hard TP +{HARD_TAKE_PROFIT_PCT}%" if HARD_TAKE_PROFIT_PCT > 0 else ""
    crash = f" | daily crash -{DAILY_CRASH_PCT}%" if DAILY_CRASH_PCT > 0 else ""
    stop_d = (f"{STOP_ATR_MULT}xATR (cap {STOP_MAX_PCT}%)"
              if STOP_MODE == "atr" else f"-{STOP_LOSS_PCT}%")
    trail_d = (f"{TRAIL_ATR_MULT}xATR, arms at +{TRAIL_ACTIVATE_ATR}xATR"
               if TRAIL_MODE == "atr" else f"{TRAIL_PCT}%, arms at +{TRAIL_ACTIVATE_PCT}%")
    log.info(
        f"Profit monitor: stop {stop_d} | trail {trail_d}"
        f"{hard_tp}{crash} | check every {CHECK_INTERVAL}s"
    )

    while True:
        await asyncio.sleep(CHECK_INTERVAL)

        if not await _market_is_open():
            log.debug("Profit monitor: market closed — skipping exit checks")
            continue

        try:
            positions = await _get_positions()
        except Exception as e:
            log.warning(f"Profit monitor: failed to fetch positions: {e}")
            continue

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
            if atr is None and STOP_MODE == "atr":
                atr = await _get_atr(symbol)
            peak = max(float(state.get("peak") or entry), current)
            peaks[symbol] = {"peak": peak, "atr": atr}

            peak_pct = (peak - entry) / entry * 100
            drop_from_peak = (peak - current) / peak * 100 if peak > 0 else 0.0
            atr_pct = (atr / entry * 100) if atr else None

            # ── stop distance ────────────────────────────────────────────────
            if STOP_MODE == "atr" and atr_pct:
                mult = stop_atr_mult_for(strategy)
                stop_pct = min(max(mult * atr_pct, STOP_MIN_PCT), STOP_MAX_PCT)
                stop_desc = f"{mult}xATR ({stop_pct:.1f}%)"
            else:
                stop_pct = STOP_LOSS_PCT
                stop_desc = f"{STOP_LOSS_PCT}%"

            reason = None
            if pct <= -stop_pct:
                reason = f"Stop-loss {pct:.2f}% ≤ -{stop_desc}"
            elif HARD_TAKE_PROFIT_PCT > 0 and pct >= HARD_TAKE_PROFIT_PCT:
                reason = f"Take-profit +{pct:.2f}% ≥ +{HARD_TAKE_PROFIT_PCT}%"
            else:
                # ── trailing stop, also ATR-scaled ───────────────────────────
                if TRAIL_MODE == "atr" and atr_pct:
                    # Floors matter as much here as on the stop: without them a
                    # 0.10%-ATR instrument arms at +0.20% and exits on a 0.15%
                    # give-back, closing on noise for a rounding-error gain.
                    arm_pct  = max(TRAIL_ACTIVATE_ATR * atr_pct, TRAIL_ACTIVATE_MIN_PCT)
                    give_pct = max(TRAIL_ATR_MULT * atr_pct, TRAIL_MIN_PCT)
                    trail_desc = f"{TRAIL_ATR_MULT}xATR ({give_pct:.1f}%)"
                else:
                    arm_pct   = TRAIL_ACTIVATE_PCT
                    give_pct  = TRAIL_PCT
                    trail_desc = f"{TRAIL_PCT}%"
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

            if not reason and DAILY_CRASH_PCT > 0:
                prev_close = await _get_prev_close(symbol)
                if prev_close and prev_close > 0:
                    day_drop = (prev_close - current) / prev_close * 100
                    if day_drop >= DAILY_CRASH_PCT:
                        reason = (
                            f"Daily crash -{day_drop:.2f}% "
                            f"(prev close ${prev_close:.2f} → ${current:.2f}, "
                            f"threshold -{DAILY_CRASH_PCT}%)"
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
