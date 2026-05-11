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
from scanner import find_top_picks

log = logging.getLogger("raanu.auto")

HERE = Path(__file__).parent
# Use /tmp on cloud (Railway) so writes always succeed; fall back to local dir
_DATA_DIR = Path("/tmp") if Path("/tmp").exists() and not (HERE / ".env").exists() else HERE
LOG_PATH = _DATA_DIR / "trades_log.json"


# ---------- LIMITS (configurable via .env) ----------
def _int_env(name, default):
    try: return int(os.getenv(name, str(default)))
    except: return default

def _float_env(name, default):
    try: return float(os.getenv(name, str(default)))
    except: return default


WEEKLY_TRADE_LIMIT = _int_env("WEEKLY_TRADE_LIMIT", 2)
PER_TRADE_MAX_USD  = _float_env("PER_TRADE_MAX_USD", 500.0)
MIN_SIGNAL_SCORE   = _int_env("MIN_SIGNAL_SCORE", 60)
SCAN_INTERVAL_SEC  = _int_env("SCAN_INTERVAL_SEC", 1800)  # 30 min default
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


# ---------- ALPACA ORDER PLACER ----------
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


async def alpaca_buy_notional(symbol: str, notional: float) -> dict:
    """Place a market buy order for a notional USD amount."""
    body = {
        "symbol":        symbol.upper(),
        "notional":      str(round(notional, 2)),
        "side":          "buy",
        "type":          "market",
        "time_in_force": "day",
    }
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
class AutoTrader:
    def __init__(self):
        self.enabled = False
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
        recent = self.tradelog.trades_in_last_7_days()
        return {
            "enabled": self.enabled,
            "config": {
                "weekly_limit":       WEEKLY_TRADE_LIMIT,
                "per_trade_max_usd":  PER_TRADE_MAX_USD,
                "min_score":          MIN_SIGNAL_SCORE,
                "scan_interval_sec":  SCAN_INTERVAL_SEC,
                "watchlist":          WATCHLIST,
            },
            "trades_this_week":           len(recent),
            "trades_remaining_this_week": max(0, WEEKLY_TRADE_LIMIT - len(recent)),
            "recent_trades":   recent[-5:],
            "last_scan":       self.last_scan,
            "last_decision":   self.last_decision,
            "recent_events":   self.events[-30:],
        }

    async def run_one_cycle(self, picks: Optional[list] = None):
        """
        Check signals and maybe place a trade.
        Pass pre-computed picks to avoid a redundant scan (scheduled scan
        already ran). If picks=None, runs a fresh scan.
        Scanning always happens; trading only when self.enabled=True.
        """
        if picks is None:
            self.event("scan", "Running XETRA/GETTEX momentum scan...")
            picks = find_top_picks(3)
        else:
            self.event("scan", f"Using pre-scanned picks ({len(picks)} candidates)")

        self.last_scan = {
            "ts":      datetime.now(timezone.utc).isoformat(),
            "results": picks,
        }

        if not self.enabled:
            self.last_decision = {"action": "idle", "reason": "Auto-execute is off — picks cached, no order placed"}
            return

        # Limit gate
        ok, why = self.tradelog.can_trade_now()
        if not ok:
            self.event("limit", why)
            self.last_decision = {"action": "skip", "reason": why}
            return

        # Best pick above threshold that has a US ADR for Alpaca execution
        best = next(
            (p for p in picks
             if p.get("score", 0) >= MIN_SIGNAL_SCORE and p.get("us_adr")),
            None,
        )

        if not best:
            top = picks[0] if picks else {"ticker": "?", "score": 0}
            msg = f"No executable signal (best: {top.get('ticker')} score {top.get('score',0)}, need >={MIN_SIGNAL_SCORE} with US ADR)"
            self.event("hold", msg)
            self.last_decision = {"action": "hold", "reason": msg}
            return

        adr      = best["us_adr"]   # US-listed ADR to execute via Alpaca
        notional = float(PER_TRADE_MAX_USD)

        self.event(
            "buy",
            f"BUY ${notional} of {adr} (XETRA: {best['ticker']}) "
            f"score {best['score']} — {' | '.join(best['reasons'][:2])}",
            {"score": best["score"], "xetra": best["ticker"], "adr": adr, "usd": notional},
        )

        try:
            result = await alpaca_buy_notional(adr, notional)
        except Exception as e:
            self.event("error", f"Alpaca rejected order: {e}")
            self.last_decision = {"action": "error", "reason": str(e)}
            return

        self.tradelog.record({
            "action":          "BUY",
            "xetra_ticker":    best["ticker"],
            "us_adr":          adr,
            "notional_usd":    notional,
            "score":           best["score"],
            "reasons":         best["reasons"],
            "alpaca_response": result,
        })

        # Notify via WhatsApp
        try:
            from notifier import send_whatsapp, format_trade_confirm
            send_whatsapp(format_trade_confirm("BUY", adr, notional, result.get("status", "submitted")))
        except Exception:
            pass

        self.event("filled", f"Order submitted — {adr} id {result.get('id', '?')}")
        self.last_decision = {
            "action":  "buy",
            "xetra":   best["ticker"],
            "adr":     adr,
            "usd":     notional,
            "score":   best["score"],
        }


# Module-level singleton
trader = AutoTrader()
