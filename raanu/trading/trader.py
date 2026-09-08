"""
auto_trader.py — Limit-respecting automatic order executor (Alpaca)
====================================================================
Hard rules (enforced before every order):
  1. At most WEEKLY_TRADE_LIMIT orders in any rolling 7-day window
  2. Each order's notional value is at most PER_TRADE_MAX_USD
  3. Score must clear MIN_SIGNAL_SCORE
  4. Bot must be ENABLED via /api/auto/start

State is persisted in trades_log.json so limits survive restart.
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Optional

import httpx

from raanu import config, state
from raanu.state import keys

STRATEGY_LABELS = {"s1": "S1 Pullback", "s2": "S2 Breakout", "s3": "S3 Leader Dip"}

log = logging.getLogger("raanu.trading.trader")


# ---------- LIMITS ----------
# All of these now come from raanu.config, read per call. They used to be
# module-level constants computed from os.getenv at import time, which meant
# the values were frozen before SSM secrets had loaded on Lambda.

# The per-trade cap is PER STRATEGY. Capital follows conviction: S3 is the only
# strategy that has ever stayed profitable across both halves of the backtest
# window, S1 and S2 both collapse in the second half. S2 is kept alive at token
# size purely to keep collecting a live sample — a $0 cap would stop the data.
# A blank/absent value falls back to the global PER_TRADE_MAX_USD.
def per_trade_max_for(strategy: str) -> float:
    """Per-trade USD cap for this strategy, falling back to the global cap."""
    return config.per_trade_max_usd(strategy or "s1")


# The weekly trade limit is PER STRATEGY too, and for the same reason as the
# per-trade cap: S3 is the only strategy profitable in both halves of the
# backtest, so it gets the most attempts; S2 gets one, purely to keep its live
# sample growing. A blank/absent value falls back to the global limit.
def weekly_limit_for(strategy: str) -> int:
    """Weekly trade limit for this strategy, falling back to the global limit."""
    return config.weekly_trade_limit(strategy or "s1")


# NOTE: there is deliberately no periodic scan interval. Scans are driven by
# the schedule in server.py (_premarket_loop and _scheduled_trade_loop). A
# SCAN_INTERVAL_SEC setting used to be defined here and reported by the API,
# but no loop ever consumed it — the dashboard displayed "30 min" for a scan
# cadence that did not exist.
WATCHLIST = config.watchlist()


# ---------- TRADE LOG ----------
# Fields kept from Alpaca's order response. The full body is ~840 bytes of
# mostly nulls (replaced_by, hwm, subtag, legs, position_intent...) and was 65%
# of the entire trade log. Dropping the rest is safe because the broker is the
# record of record — the full order is always re-fetchable by id.
_ALPACA_KEEP = ("id", "client_order_id", "status", "filled_avg_price",
                "filled_qty", "filled_at", "created_at")


def _trim_alpaca(response):
    if not isinstance(response, dict):
        return response
    return {k: v for k, v in response.items() if k in _ALPACA_KEEP}


def _parse_ts(value: str) -> datetime:
    """Parse a stored timestamp, assuming UTC when it carries no zone.

    Seeded and historical entries were written with a bare ``isoformat()``, so
    both offset-aware and naive strings exist. Anything unparseable sorts to
    the epoch rather than raising — a malformed row must not break a merge.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except Exception:
        return datetime.fromtimestamp(0, UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class TradeLog:
    """One DynamoDB item per trade, keyed by timestamp.

    It used to be a single item holding every trade ever, loaded once into
    ``self.data`` and rewritten whole on each append. That had two failure
    modes, and item-per-record removes both:

    * **A hard ceiling.** At ~1.4 KB/trade the 400 KB item limit landed at
      ~285 trades — roughly six months at the weekly-limit pace. An oversized
      put was swallowed by a bare ``except``, so the log would simply stop
      recording, which re-arms the weekly trade limit and resets Kelly's
      sample.
    * **Lost updates.** ``self.data`` was a snapshot cached for the life of a
      Lambda container. The API and the worker each held their own, so a BUY
      written by one and a SELL written by the other discarded each other.
      There is deliberately no cached copy here now — every read is a query.
    """

    def trades_in_last_7_days(self, strategy: str | None = None,
                              action: str | None = None):
        """Trades inside the rolling 7-day window.

        A key-range read, not a full-log walk. This runs four times per
        ``/api/auto/status`` call and the dashboard polls that, so it used to
        parse every timestamp in the entire history on every refresh.

        `action` filters by BUY/SELL. The weekly limit budgets *new orders*, so
        every limit check must pass action="BUY": exits land in the same log,
        tagged with the strategy they were opened under, and without this filter
        a closed round-trip consumed the opening budget. With
        WEEKLY_TRADE_LIMIT_S2=1 a single exit locked S2 out for a whole week.
        """
        cutoff = keys.stamp(datetime.now(UTC) - timedelta(days=7))
        filters = {}
        if strategy:
            filters["strategy"] = strategy
        rows = state.query(keys.TRADE, sk_gte=cutoff, filters=filters or None)
        out = [r.data for r in rows]
        if action:
            out = [t for t in out if (t.get("action") or "BUY").upper() == action.upper()]
        return out

    def all_trades(self, **kwargs):
        """Every trade, oldest first. Used by Kelly and by attribution, both of
        which genuinely need full history rather than a window."""
        return [r.data for r in state.query(keys.TRADE, **kwargs)]

    def can_trade_now(self, strategy: str = "s1") -> tuple[bool, str]:
        recent = self.trades_in_last_7_days(strategy=strategy, action="BUY")
        label = STRATEGY_LABELS.get(strategy, "S1 Pullback")
        limit = weekly_limit_for(strategy)   # per strategy, not the global cap
        if len(recent) >= limit:
            oldest = min(recent, key=lambda x: x["timestamp"])
            return False, f"[{label}] Weekly limit reached ({len(recent)}/{limit}). Oldest expires {oldest['timestamp']}"
        return True, f"[{label}] OK ({len(recent)}/{limit} this week)"

    def record(self, payload: dict):
        """Write one trade as its own item. No read, no merge, no rewrite —
        which is what makes concurrent writers safe."""
        stamp = keys.now_stamp()
        payload["timestamp"] = stamp
        if "alpaca_response" in payload:
            payload["alpaca_response"] = _trim_alpaca(payload["alpaca_response"])
        state.put(keys.TRADE, keys.trade_sk(stamp), payload)
        return payload


# ---------- ALPACA HELPERS ----------
def _alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID":     config.alpaca_key(),
        "APCA-API-SECRET-KEY": config.alpaca_secret(),
        "Content-Type":        "application/json",
    }


def _broker_base() -> str:
    mode = config.alpaca_mode()
    return (
        "https://paper-api.alpaca.markets/v2"
        if mode != "live"
        else "https://api.alpaca.markets/v2"
    )


async def market_is_open() -> tuple[bool, str]:
    """Return (is_open, reason). Uses Alpaca clock endpoint."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{_broker_base()}/clock", headers=_alpaca_headers())
        if r.status_code != 200:
            return False, f"Clock endpoint error {r.status_code}"
        data = r.json()
        if data.get("is_open"):
            return True, "Market open"
        next_open = data.get("next_open", "unknown")
        return False, f"Market closed — next open {next_open}"
    except Exception as e:
        return False, f"Clock check failed: {e}"


async def get_open_orders() -> list[dict] | None:
    """Orders submitted but not yet filled. None (not []) when the call fails,
    so callers can tell "no open orders" from "could not check"."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{_broker_base()}/orders",
                headers=_alpaca_headers(),
                params={"status": "open", "limit": 500},
            )
        if r.status_code != 200:
            log.error(f"get_open_orders: Alpaca returned {r.status_code}")
            return None
        return r.json()
    except Exception as e:
        log.error(f"get_open_orders: {e}")
        return None


def _order_cash_committed(order: dict) -> float:
    """Dollar value an unfilled buy order will consume when it fills."""
    notional = order.get("notional")
    if notional:
        return float(notional)
    qty   = float(order.get("qty") or 0) - float(order.get("filled_qty") or 0)
    price = float(order.get("limit_price") or 0)
    return qty * price


async def get_free_cash() -> float | None:
    """
    Cash genuinely available to deploy, or None on error.

    Alpaca only debits `cash` when an order *fills*. Orders queued outside
    market hours sit in `accepted` for hours, so raw `cash` overstates what is
    actually free — subtract everything already committed to open buys.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{_broker_base()}/account", headers=_alpaca_headers())
        if r.status_code != 200:
            return None
        cash = float(r.json().get("cash", 0))
    except Exception:
        return None

    open_orders = await get_open_orders()
    if open_orders is None:
        # Without the order book, `cash` overstates what is free by the value
        # of every queued buy — the exact error that overspends.
        log.error("get_free_cash: open orders unreadable, refusing to report cash")
        return None
    committed = sum(
        _order_cash_committed(o) for o in open_orders if o.get("side") == "buy"
    )
    return max(0.0, cash - committed)


async def get_held_symbols() -> set[str] | None:
    """
    Symbols we must not buy again — open positions *plus* symbols with an
    unfilled buy order already working.

    Position-only checking was the source of duplicate orders: an order queued
    while the market is closed creates no position, so every later scan saw the
    ticker as un-held and submitted another buy for it.

    Returns None when the check could not be completed. It used to swallow
    every error and return whatever it had — usually an empty set — so a single
    timed-out Alpaca call turned the duplicate guard off entirely and the
    caller could not tell "nothing held" from "could not look". Callers must
    treat None as "do not trade this cycle": failing closed costs one skipped
    scan, failing open costs a duplicate position.
    """
    held: set[str] = set()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{_broker_base()}/positions", headers=_alpaca_headers())
        if r.status_code != 200:
            log.error(f"get_held_symbols: /positions returned {r.status_code}")
            return None
        held = {p["symbol"].upper() for p in r.json()}
    except Exception as e:
        log.error(f"get_held_symbols: /positions failed: {e}")
        return None

    open_orders = await get_open_orders()
    if open_orders is None:
        log.error("get_held_symbols: could not read open orders")
        return None
    for o in open_orders:
        if o.get("side") == "buy" and o.get("symbol"):
            held.add(o["symbol"].upper())
    return held


async def alpaca_buy_notional(symbol: str, notional: float,
                              strategy: str | None = None) -> dict:
    """Place a market buy order for a notional USD amount.

    The strategy is stamped into `client_order_id` so attribution survives
    losing the trade log. It used to live ONLY in trades_log.json, so when that
    file was on /tmp and Railway wiped it on redeploy, the answer to "which
    strategy bought this?" was destroyed permanently — BAC, OKTA and ROKU are
    still unattributable for exactly this reason. Alpaca keeps client_order_id
    for the life of the order, which makes the broker the durable record and
    the local log a cache.
    """
    body = {
        "symbol":        symbol.upper(),
        "notional":      str(round(notional, 2)),
        "side":          "buy",
        "type":          "market",
        "time_in_force": "day",
    }
    if strategy:
        # Must be unique per order or Alpaca rejects it; 128-char limit.
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")[:-3]
        body["client_order_id"] = f"raanu-{strategy.lower()}-{symbol.upper()}-{stamp}"[:128]
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{_broker_base()}/orders",
            headers=_alpaca_headers(),
            json=body,
        )
    if r.status_code >= 400:
        raise RuntimeError(f"Alpaca {r.status_code}: {r.text}")
    return r.json()


# ---------- AUTO TRADER ----------
# The auto-trader's on/off switch, in the store both Lambdas share. See the
# `enabled` property below for why it cannot live in process memory.
_AUTO_STATE_KEY = "auto_trader.json"


class AutoTrader:
    def __init__(self):
        # NOTE: `enabled` is deliberately NOT set here — it is a property
        # backed by the shared state store. Assigning it in __init__ would
        # write the env default over whatever the owner last chose, on every
        # cold start.
        self.tradelog = TradeLog()
        self.last_scan: dict | None = None
        self.last_decision: dict | None = None
        self.events: list[dict] = []

    # ── the on/off switch ────────────────────────────────────────────────
    #
    # Shared state, not process memory. It used to be a plain attribute, which
    # worked on Railway — one long-lived process owned both the API and the
    # scheduler, so `get_trader().enabled = True` was visible to the loop that
    # traded.
    #
    # On AWS that is no longer true and the attribute was quietly meaningless:
    #
    #   * POST /api/auto/start set it on ONE API Lambda container. The next
    #     request could land on another container, or a cold one, and see the
    #     old value.
    #   * The worker — which actually runs the 09:35 and 11:00 slots — is a
    #     different function entirely and never saw it at all.
    #
    # Putting it in the state store both Lambdas already share makes the
    # switch mean the same thing everywhere. Off unless explicitly turned on:
    # a hardcoded True once made every local `python -m raanu.api` a live
    # trading bot on the shared Alpaca account.

    @property
    def enabled(self) -> bool:
        try:
            saved = state.get(keys.FLAG, keys.flag_sk(_AUTO_STATE_KEY), default=None)
        except Exception as e:
            # Fail closed. An unreadable switch must never authorise trading,
            # and the dashboard showing "off" during a blip is the harmless
            # direction to be wrong in.
            log.warning(f"[auto] could not read the enable flag ({e}) — treating as OFF")
            return False
        if not isinstance(saved, dict) or "enabled" not in saved:
            # Never set. AUTO_TRADE_ENABLED seeds the initial answer so a
            # deployment can ship enabled if it means to.
            return config.auto_trade_enabled()
        return bool(saved["enabled"])

    @enabled.setter
    def enabled(self, value: bool) -> None:
        state.put(keys.FLAG, keys.flag_sk(_AUTO_STATE_KEY), {
            "enabled": bool(value),
            "changed_at": datetime.now(UTC).isoformat(),
        })

    def event(self, kind: str, msg: str, extra: dict | None = None):
        ev = {"ts": datetime.now(UTC).isoformat(), "kind": kind, "msg": msg}
        if extra:
            ev.update(extra)
        self.events.append(ev)
        if len(self.events) > 500:
            self.events = self.events[-500:]
        log.info(f"[auto:{kind}] {msg}")

    def status(self) -> dict:
        recent = self.tradelog.trades_in_last_7_days()          # display: buys + sells
        buys_by_strat = {
            s: len(self.tradelog.trades_in_last_7_days(strategy=s, action="BUY"))
            for s in ("s1", "s2", "s3")
        }
        remaining_by_strat = {
            s: max(0, weekly_limit_for(s) - n) for s, n in buys_by_strat.items()
        }
        return {
            "enabled": self.enabled,
            "config": {
                "weekly_limit":       config.weekly_trade_limit(),
                "per_trade_max_usd":  config.per_trade_max_usd(),
                "per_trade_max_by_strategy": {
                    s: per_trade_max_for(s) for s in ("s1", "s2", "s3")
                },
                "weekly_limit_by_strategy": {
                    s: weekly_limit_for(s) for s in ("s1", "s2", "s3")
                },
                "min_score":          config.min_signal_score(),
                "watchlist":          WATCHLIST,
            },
            # Budgets count BUYs only, and they are per strategy — comparing an
            # all-strategy count against the global limit reported "0 remaining"
            # while S3 still had its full allowance.
            "trades_this_week":           sum(buys_by_strat.values()),
            "trades_remaining_this_week": sum(remaining_by_strat.values()),
            "trades_this_week_by_strategy":      buys_by_strat,
            "trades_remaining_by_strategy":      remaining_by_strat,
            "recent_trades":   recent[-5:],
            "last_scan":       self.last_scan,
            "last_decision":   self.last_decision,
            "recent_events":   self.events[-30:],
        }

    async def run_one_cycle(self, picks: list | None = None,
                           force_market_open: bool = False,
                           strategy: str = "s1",
                           execute: bool = True):
        """
        Check signals and maybe place a trade.
        strategy: "s1" (pullback), "s2" (breakout) or "s3" (leader dip)
        execute:  False = scan, cache and report only, never order.

        The scan-and-cache paths pass execute=False. They used to call this
        with ordering enabled, which meant the startup scan and the "rest day"
        branch of the scheduled loop could both place trades — the rest-day
        branch logging "scanning only, no orders" while doing exactly that.
        """
        label = STRATEGY_LABELS.get(strategy, "S1 Pullback")
        uptrend_key = {"s2": "stage2", "s3": "leader_dip"}.get(strategy, "uptrend")

        if picks is None:
            self.event("scan", f"[{label}] Running scan...")
            from raanu.scanning.engine import top_picks
            picks = top_picks(strategy, limit=3)
        else:
            self.event("scan", f"[{label}] Using pre-scanned picks ({len(picks)} candidates)")

        self.last_scan = {
            "ts":      datetime.now(UTC).isoformat(),
            "results": picks,
        }

        if not execute:
            self.last_decision = {"action": "idle", "reason": f"[{label}] Scan-only run — picks cached, no order placed"}
            return

        if not self.enabled:
            self.last_decision = {"action": "idle", "reason": f"[{label}] Auto-execute is off — picks cached, no order placed"}
            return

        # ── Gate 1: market hours ──────────────────────────────────────────
        if force_market_open:
            self.event("scan", "Market hours check bypassed (force mode)")
        else:
            is_open, clock_msg = await market_is_open()
            if not is_open:
                self.event("hold", f"Skipping — {clock_msg}")
                self.last_decision = {"action": "hold", "reason": clock_msg}
                return

        # ── Gate 2: weekly trade limit (per strategy) ─────────────────────
        ok, why = self.tradelog.can_trade_now(strategy=strategy)
        if not ok:
            self.event("limit", why)
            self.last_decision = {"action": "skip", "reason": why}
            return

        # ── Gate 3: fetch live account state ─────────────────────────────
        free_cash   = await get_free_cash()
        held        = await get_held_symbols()

        if held is None:
            msg = "Could not verify existing holdings — skipping (fail closed)"
            self.event("error", msg)
            self.last_decision = {"action": "error", "reason": msg}
            return

        if free_cash is None:
            msg = "Could not fetch account balance — skipping"
            self.event("error", msg)
            self.last_decision = {"action": "error", "reason": msg}
            return

        # ── Gate 4: best signal that is executable and not already held ───
        best = next(
            (p for p in picks
             if p.get("score", 0) >= config.min_signal_score()
             and p.get(uptrend_key)
             and p.get("ticker")
             and p["ticker"].upper() not in held),
            None,
        )

        if not best:
            held_str = ", ".join(sorted(held)) if held else "none"
            top = picks[0] if picks else {"ticker": "?", "score": 0}
            msg = (
                f"No new executable signal — best: {top.get('ticker')} "
                f"score {top.get('score', 0)} (need >={config.min_signal_score()}). "
                f"Already held: {held_str}"
            )
            self.event("hold", msg)
            self.last_decision = {"action": "hold", "reason": msg}
            return

        sym = best["ticker"]

        # ── Gate 5: position sizing (min of cap and 10% of free cash) ────
        strat_cap   = per_trade_max_for(strategy)
        max_by_cash = round(free_cash * 0.10, 2)   # never risk >10% of cash
        notional    = min(strat_cap, max_by_cash)

        if notional < 1.0:
            msg = f"Insufficient free cash (${free_cash:.2f}) to open a position"
            self.event("hold", msg)
            self.last_decision = {"action": "hold", "reason": msg}
            return

        self.event(
            "buy",
            f"BUY ${notional} of {sym} "
            f"score {best['score']} — cash ${free_cash:.0f}, {label} cap ${strat_cap:.0f}, "
            f"10%-of-cash cap ${max_by_cash:.0f} — "
            f"{' | '.join(best['reasons'][:2])}",
            {"score": best["score"], "ticker": sym, "usd": notional},
        )

        # ── Notify BEFORE placing the order ──────────────────────────────
        try:
            from raanu.notify.telegram import (
                format_pre_trade_alert,
                format_trade_confirm,
                send_whatsapp,
            )
            send_whatsapp(format_pre_trade_alert(
                sym, sym, notional, best["score"],
                free_cash, best.get("reasons", []),
                strategy=strategy,
            ), strategy=strategy)
        except Exception:
            pass

        try:
            result = await alpaca_buy_notional(sym, notional, strategy)
        except Exception as e:
            self.event("error", f"Alpaca rejected order: {e}")
            self.last_decision = {"action": "error", "reason": str(e)}
            return

        self.tradelog.record({
            "action":          "BUY",
            "ticker":          sym,
            "notional_usd":    notional,
            "score":           best["score"],
            "reasons":         best["reasons"],
            "strategy":        strategy,
            "alpaca_response": result,
        })

        # Notify AFTER order confirmed
        try:
            send_whatsapp(format_trade_confirm("BUY", sym, notional, result.get("status", "submitted"), strategy=strategy), strategy=strategy)
        except Exception:
            pass

        self.event("filled", f"Order submitted — {sym} id {result.get('id', '?')}")
        self.last_decision = {
            "action":  "buy",
            "ticker":  sym,
            "usd":     notional,
            "score":   best["score"],
        }


_trader: Optional["AutoTrader"] = None


def get_trader() -> "AutoTrader":
    """Process-wide trader, built on first use.

    This used to be ``trader = AutoTrader()`` at module scope, so merely
    importing this module read the whole trade log from DynamoDB — on every
    Lambda cold start, including requests that never touch trading.
    """
    global _trader
    if _trader is None:
        _trader = AutoTrader()
    return _trader


def reset_trader() -> None:
    """Drop the cached trader. Tests only."""
    global _trader
    _trader = None


def seed_tradelog_from_env() -> dict:
    """
    One-shot reconciliation: merge TRADELOG_SEED (a JSON array of trade-log
    entries) into the persistent log, skipping anything already present.

    Exists because a local server and the deployed one traded the same Alpaca
    account while keeping separate logs, so each instance's history was
    invisible to the other — positions showed as untagged, strategy stats were
    split, and Kelly's sample never grew. Idempotent by
    (timestamp, ticker, action), so leaving the variable set is harmless; unset
    it once the merge is confirmed.
    """
    raw = config.env_str("TRADELOG_SEED")
    if not raw:
        return {"seeded": 0, "skipped": 0, "reason": "TRADELOG_SEED not set"}
    try:
        incoming = json.loads(raw)
        if not isinstance(incoming, list):
            raise ValueError("TRADELOG_SEED must be a JSON array")
    except Exception as e:
        log.error(f"TRADELOG_SEED ignored — could not parse: {e}")
        return {"seeded": 0, "skipped": 0, "error": str(e)}

    def _key(t):
        return (t.get("timestamp"), (t.get("ticker") or "").upper(), t.get("action"))

    existing = get_trader().tradelog.all_trades()
    seen = {_key(t) for t in existing}
    added = 0
    for t in incoming:
        if not isinstance(t, dict) or not t.get("timestamp"):
            continue
        if _key(t) in seen:
            continue
        seen.add(_key(t))
        # One item per seeded trade, keyed by its own timestamp — so seeded
        # history interleaves with live history in sort order rather than being
        # appended to the end of a list and re-sorted.
        state.put(keys.TRADE, keys.trade_sk(keys.stamp(_parse_ts(t["timestamp"]))), t)
        added += 1

    total = len(existing) + added
    log.info(f"TRADELOG_SEED: merged {added} new entries, "
             f"{len(incoming) - added} already present, total now {total}")
    return {"seeded": added, "skipped": len(incoming) - added, "total": total}
