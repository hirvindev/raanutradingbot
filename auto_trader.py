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

import os
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx

from strategy import scan, score_ticker
from scanner import find_top_picks, find_top_picks_s2, find_top_picks_s3

STRATEGY_LABELS = {"s1": "S1 Pullback", "s2": "S2 Breakout", "s3": "S3 Leader Dip"}

log = logging.getLogger("raanu.auto")

HERE = Path(__file__).parent
from datadir import state_path
LOG_PATH = state_path("trades_log.json")


# ---------- LIMITS (configurable via .env) ----------
def _int_env(name, default):
    try: return int(os.getenv(name, str(default)))
    except: return default

def _float_env(name, default):
    try: return float(os.getenv(name, str(default)))
    except: return default


WEEKLY_TRADE_LIMIT = _int_env("WEEKLY_TRADE_LIMIT", 2)
PER_TRADE_MAX_USD  = _float_env("PER_TRADE_MAX_USD", 1000.0)
MIN_SIGNAL_SCORE   = _int_env("MIN_SIGNAL_SCORE", 70)

# The per-trade cap is PER STRATEGY. Capital follows conviction: S3 is the only
# strategy that has ever stayed profitable across both halves of the backtest
# window, S1 and S2 both collapse in the second half. S2 is kept alive at token
# size purely to keep collecting a live sample — a $0 cap would stop the data.
# A blank/absent value falls back to the global PER_TRADE_MAX_USD.
def per_trade_max_for(strategy: str) -> float:
    """Per-trade USD cap for this strategy, falling back to the global cap."""
    raw = os.getenv(f"PER_TRADE_MAX_USD_{(strategy or 's1').upper()}", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return {"s1": 1000.0, "s2": 100.0, "s3": 5000.0}.get(
        (strategy or "s1").lower(), float(PER_TRADE_MAX_USD)
    )


# The weekly trade limit is PER STRATEGY too, and for the same reason as the
# per-trade cap: S3 is the only strategy profitable in both halves of the
# backtest, so it gets the most attempts; S2 gets one, purely to keep its live
# sample growing. A blank/absent value falls back to the global limit.
def weekly_limit_for(strategy: str) -> int:
    """Weekly trade limit for this strategy, falling back to the global limit."""
    raw = os.getenv(f"WEEKLY_TRADE_LIMIT_{(strategy or 's1').upper()}", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return {"s1": 2, "s2": 1, "s3": 3}.get(
        (strategy or "s1").lower(), int(WEEKLY_TRADE_LIMIT)
    )


# NOTE: there is deliberately no periodic scan interval. Scans are driven by
# the schedule in server.py (_premarket_loop and _scheduled_trade_loop). A
# SCAN_INTERVAL_SEC setting used to be defined here and reported by the API,
# but no loop ever consumed it — the dashboard displayed "30 min" for a scan
# cadence that did not exist.
WATCHLIST = [t.strip().upper() for t in os.getenv("WATCHLIST", "AAPL,MSFT,NVDA,GOOGL,AMZN").split(",") if t.strip()]


# ---------- TRADE LOG ----------
class TradeLog:
    def __init__(self, path: Path = LOG_PATH):
        self.path = path
        self.data = self._load()

    def _load(self):
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except Exception:
                pass
        return {"trades": []}

    def save(self):
        try:
            self.path.write_text(json.dumps(self.data, indent=2, default=str))
        except Exception as e:
            log.error(f"Failed to write trade log: {e}")

    def trades_in_last_7_days(self, strategy: Optional[str] = None,
                              action: Optional[str] = None):
        """Log entries inside the rolling 7-day window.

        `action` filters by BUY/SELL. The weekly limit budgets *new orders*, so
        every limit check must pass action="BUY": exits land in the same log,
        tagged with the strategy they were opened under, and without this filter
        a closed round-trip consumed the opening budget. With
        WEEKLY_TRADE_LIMIT_S2=1 a single exit locked S2 out for a whole week.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        out = []
        for t in self.data.get("trades", []):
            try:
                ts = datetime.fromisoformat(t["timestamp"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts > cutoff:
                    if strategy and t.get("strategy") != strategy:
                        continue
                    if action and (t.get("action") or "BUY").upper() != action.upper():
                        continue
                    out.append(t)
            except Exception:
                continue
        return out

    def can_trade_now(self, strategy: str = "s1") -> tuple[bool, str]:
        recent = self.trades_in_last_7_days(strategy=strategy, action="BUY")
        label = STRATEGY_LABELS.get(strategy, "S1 Pullback")
        limit = weekly_limit_for(strategy)   # per strategy, not the global cap
        if len(recent) >= limit:
            oldest = min(recent, key=lambda x: x["timestamp"])
            return False, f"[{label}] Weekly limit reached ({len(recent)}/{limit}). Oldest expires {oldest['timestamp']}"
        return True, f"[{label}] OK ({len(recent)}/{limit} this week)"

    def record(self, payload: dict):
        payload["timestamp"] = datetime.now(timezone.utc).isoformat()
        self.data.setdefault("trades", []).append(payload)
        self.save()


# ---------- ALPACA HELPERS ----------
def _alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID":     os.getenv("ALPACA_API_KEY", "").strip(),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", "").strip(),
        "Content-Type":        "application/json",
    }


def _broker_base() -> str:
    mode = os.getenv("ALPACA_MODE", "paper").strip().lower()
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


async def get_open_orders() -> Optional[list[dict]]:
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


async def get_free_cash() -> Optional[float]:
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


async def get_held_symbols() -> Optional[set[str]]:
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
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3]
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
def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


class AutoTrader:
    def __init__(self):
        # Off unless explicitly enabled. This used to be a hardcoded True, so
        # every `python3 server.py` silently became a live trading bot on the
        # shared Alpaca account — contradicting the documented "starts
        # DISABLED". Running a local server alongside the deployed one gave two
        # traders on one account, each with its own weekly counter (so double
        # the intended trades) and each seeing the other's fills as untagged.
        # Set AUTO_TRADE_ENABLED=true on exactly ONE deployment.
        self.enabled = _bool_env("AUTO_TRADE_ENABLED", False)
        self.tradelog = TradeLog()
        self.last_scan: Optional[dict] = None
        self.last_decision: Optional[dict] = None
        self.events: list[dict] = []

    def event(self, kind: str, msg: str, extra: Optional[dict] = None):
        ev = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, "msg": msg}
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
                "weekly_limit":       WEEKLY_TRADE_LIMIT,
                "per_trade_max_usd":  PER_TRADE_MAX_USD,
                "per_trade_max_by_strategy": {
                    s: per_trade_max_for(s) for s in ("s1", "s2", "s3")
                },
                "weekly_limit_by_strategy": {
                    s: weekly_limit_for(s) for s in ("s1", "s2", "s3")
                },
                "min_score":          MIN_SIGNAL_SCORE,
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

    async def run_one_cycle(self, picks: Optional[list] = None,
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
            picks = {"s2": find_top_picks_s2, "s3": find_top_picks_s3}.get(strategy, find_top_picks)(3)
        else:
            self.event("scan", f"[{label}] Using pre-scanned picks ({len(picks)} candidates)")

        self.last_scan = {
            "ts":      datetime.now(timezone.utc).isoformat(),
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
             if p.get("score", 0) >= MIN_SIGNAL_SCORE
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
                f"score {top.get('score', 0)} (need >={MIN_SIGNAL_SCORE}). "
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
            from notifier import send_whatsapp, format_pre_trade_alert, format_trade_confirm
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


# Module-level singleton
trader = AutoTrader()


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
    raw = os.getenv("TRADELOG_SEED", "").strip()
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

    existing = trader.tradelog.data.setdefault("trades", [])
    seen = {_key(t) for t in existing}
    added = 0
    for t in incoming:
        if not isinstance(t, dict) or not t.get("timestamp"):
            continue
        if _key(t) in seen:
            continue
        seen.add(_key(t))
        existing.append(t)
        added += 1

    if added:
        existing.sort(key=lambda t: t.get("timestamp") or "")
        trader.tradelog.save()
    log.info(f"TRADELOG_SEED: merged {added} new entries, "
             f"{len(incoming) - added} already present, total now {len(existing)}")
    return {"seeded": added, "skipped": len(incoming) - added, "total": len(existing)}
