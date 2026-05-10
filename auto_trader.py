"""
auto_trader.py — Limit-respecting automatic order executor
===========================================================
Hard rules (enforced before every order, never bypassable):
  1. At most WEEKLY_TRADE_LIMIT orders in any rolling 7-day window
  2. Each order's notional value is at most PER_TRADE_MAX_EUR
  3. Score must clear MIN_SIGNAL_SCORE
  4. Bot must be ENABLED via /api/auto/start

State is persisted in trades_log.json so limits survive restart.
"""

import os
import json
import math
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx

from strategy import scan, score_ticker

log = logging.getLogger("raanu.auto")

HERE = Path(__file__).parent
LOG_PATH = HERE / "trades_log.json"


# ---------- LIMITS (configurable via .env) ----------
def _int_env(name, default):
    try: return int(os.getenv(name, str(default)))
    except: return default

def _float_env(name, default):
    try: return float(os.getenv(name, str(default)))
    except: return default


WEEKLY_TRADE_LIMIT = _int_env("WEEKLY_TRADE_LIMIT", 2)
PER_TRADE_MAX_EUR = _float_env("PER_TRADE_MAX_EUR", 500.0)
MIN_SIGNAL_SCORE = _int_env("MIN_SIGNAL_SCORE", 60)
SCAN_INTERVAL_SEC = _int_env("SCAN_INTERVAL_SEC", 1800)  # default 30 min
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

    def trades_in_last_7_days(self):
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        out = []
        for t in self.data.get("trades", []):
            try:
                ts = datetime.fromisoformat(t["timestamp"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts > cutoff:
                    out.append(t)
            except Exception:
                continue
        return out

    def can_trade_now(self) -> tuple[bool, str]:
        recent = self.trades_in_last_7_days()
        if len(recent) >= WEEKLY_TRADE_LIMIT:
            oldest = min(recent, key=lambda x: x["timestamp"])
            return False, f"Weekly limit reached ({len(recent)}/{WEEKLY_TRADE_LIMIT}). Oldest expires {oldest['timestamp']}"
        return True, f"OK ({len(recent)}/{WEEKLY_TRADE_LIMIT} this week)"

    def record(self, payload: dict):
        payload["timestamp"] = datetime.now(timezone.utc).isoformat()
        self.data.setdefault("trades", []).append(payload)
        self.save()


# ---------- T212 CLIENT (uses same key the server has) ----------
class T212:
    def __init__(self, api_key: str, mode: str):
        self.api_key = api_key
        self.base = (
            "https://demo.trading212.com/api/v0"
            if mode == "demo"
            else "https://live.trading212.com/api/v0"
        )
        self._instruments_cache: Optional[list] = None

    def headers(self):
        return {"Authorization": self.api_key, "Content-Type": "application/json"}

    async def instruments(self) -> list:
        if self._instruments_cache:
            return self._instruments_cache
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.get(f"{self.base}/equity/metadata/instruments", headers=self.headers())
            r.raise_for_status()
            self._instruments_cache = r.json()
        return self._instruments_cache

    async def resolve_ticker(self, simple_ticker: str) -> Optional[str]:
        """Map 'AAPL' -> 'AAPL_US_EQ' by querying T212's instrument list."""
        inst = await self.instruments()
        st = simple_ticker.upper()
        # Prefer exact shortName/ticker match on US equity
        for i in inst:
            if i.get("shortName", "").upper() == st and i.get("type") == "STOCK":
                return i.get("ticker")
        # Fallback — any ticker starting with the simple name
        for i in inst:
            if i.get("ticker", "").startswith(st + "_"):
                return i.get("ticker")
        return None

    async def buy_market(self, t212_ticker: str, quantity: float):
        body = {"ticker": t212_ticker, "quantity": abs(quantity)}
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{self.base}/equity/orders/market", headers=self.headers(), json=body)
            if r.status_code >= 400:
                raise RuntimeError(f"T212 {r.status_code}: {r.text}")
            return r.json()


# ---------- AUTO TRADER ----------
class AutoTrader:
    def __init__(self):
        self.enabled = False
        self.tradelog = TradeLog()
        self.last_scan: Optional[dict] = None
        self.last_decision: Optional[dict] = None
        self.events: list[dict] = []  # in-memory event ring buffer

    def event(self, kind: str, msg: str, extra: Optional[dict] = None):
        ev = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, "msg": msg}
        if extra: ev.update(extra)
        self.events.append(ev)
        if len(self.events) > 500:
            self.events = self.events[-500:]
        log.info(f"[auto:{kind}] {msg}")

    def status(self) -> dict:
        recent = self.tradelog.trades_in_last_7_days()
        return {
            "enabled": self.enabled,
            "config": {
                "weekly_limit": WEEKLY_TRADE_LIMIT,
                "per_trade_max_eur": PER_TRADE_MAX_EUR,
                "min_score": MIN_SIGNAL_SCORE,
                "scan_interval_sec": SCAN_INTERVAL_SEC,
                "watchlist": WATCHLIST,
            },
            "trades_this_week": len(recent),
            "trades_remaining_this_week": max(0, WEEKLY_TRADE_LIMIT - len(recent)),
            "recent_trades": recent[-5:],
            "last_scan": self.last_scan,
            "last_decision": self.last_decision,
            "recent_events": self.events[-30:],
        }

    async def run_one_cycle(self, t212: T212):
        """One full scan + maybe-trade cycle."""
        if not self.enabled:
            return
        self.event("scan", f"Scanning {len(WATCHLIST)} tickers: {','.join(WATCHLIST)}")
        results = scan(WATCHLIST)
        self.last_scan = {"ts": datetime.now(timezone.utc).isoformat(), "results": results}

        # Limit gate
        ok, why = self.tradelog.can_trade_now()
        if not ok:
            self.event("limit", why)
            self.last_decision = {"action": "skip", "reason": why}
            return

        # Find best candidate
        best = None
        for r in results:
            if r.get("ok") and r.get("score", 0) >= MIN_SIGNAL_SCORE:
                best = r
                break

        if not best:
            top = results[0] if results else {"ticker": "?", "score": 0}
            msg = f"No actionable signal (best: {top.get('ticker')} score {top.get('score',0)}, threshold {MIN_SIGNAL_SCORE})"
            self.event("hold", msg)
            self.last_decision = {"action": "hold", "reason": msg}
            return

        # Skip if already holding (avoid stacking — keep simple)
        # (Future: check current positions and skip if already in portfolio)

        # Compute share quantity within €500 cap
        price = best["price"]
        if price <= 0:
            self.event("error", f"{best['ticker']} bad price {price}")
            return
        max_shares_by_eur = PER_TRADE_MAX_EUR / price
        # T212 supports fractional shares; round to 2 decimals
        qty = math.floor(max_shares_by_eur * 100) / 100
        if qty <= 0:
            msg = f"{best['ticker']}: even 0.01 share costs > €{PER_TRADE_MAX_EUR}. Skipping."
            self.event("skip", msg)
            self.last_decision = {"action": "skip", "reason": msg}
            return
        eur_amount = round(qty * price, 2)

        # Resolve T212 ticker
        try:
            t212_ticker = await t212.resolve_ticker(best["ticker"])
        except Exception as e:
            self.event("error", f"Could not load T212 instrument list: {e}")
            return
        if not t212_ticker:
            msg = f"{best['ticker']}: no matching T212 instrument found. Skipping."
            self.event("skip", msg)
            self.last_decision = {"action": "skip", "reason": msg}
            return

        # Place order
        self.event(
            "buy",
            f"BUY {qty} {best['ticker']} (~€{eur_amount}) — score {best['score']} — {' | '.join(best['reasons'][:3])}",
            {"score": best["score"], "price": price, "qty": qty, "eur": eur_amount},
        )
        try:
            result = await t212.buy_market(t212_ticker, qty)
        except Exception as e:
            self.event("error", f"T212 rejected order: {e}")
            self.last_decision = {"action": "error", "reason": str(e)}
            return

        # Record
        self.tradelog.record({
            "action": "BUY",
            "ticker": best["ticker"],
            "t212_ticker": t212_ticker,
            "quantity": qty,
            "price": price,
            "eur": eur_amount,
            "score": best["score"],
            "reasons": best["reasons"],
            "t212_response": result,
        })
        self.event("filled", f"Recorded — order id {result.get('id', '?')}")
        self.last_decision = {
            "action": "buy",
            "ticker": best["ticker"],
            "qty": qty,
            "eur": eur_amount,
            "score": best["score"],
        }


# Module-level singleton
trader = AutoTrader()
