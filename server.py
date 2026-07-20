"""
RaanuTradingBot — Alpaca backend
=================================
Connects the dashboard to your Alpaca paper/live account.

Run with:  python server.py
"""

import os
import json
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

BERLIN = ZoneInfo("Europe/Berlin")
IST    = ZoneInfo("Asia/Kolkata")
US_EAST = ZoneInfo("US/Eastern")

# ---------- CONFIG ----------
HERE = Path(__file__).parent
load_dotenv(HERE / ".env", override=False)  # no-op on Railway; env vars come from dashboard
_DATA_DIR = Path("/tmp") if Path("/tmp").exists() and not (HERE / ".env").exists() else HERE
PICKS_CACHE = _DATA_DIR / "last_picks.json"
PICKS_CACHE_S2 = _DATA_DIR / "last_picks_s2.json"

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "").strip()
ALPACA_SECRET  = os.getenv("ALPACA_SECRET_KEY", "").strip()
ALPACA_MODE    = os.getenv("ALPACA_MODE", "paper").strip().lower()  # paper | live

if ALPACA_MODE not in ("paper", "live"):
    print(f"WARNING: Invalid ALPACA_MODE='{ALPACA_MODE}'. Defaulting to paper.")
    ALPACA_MODE = "paper"

BROKER_BASE = (
    "https://paper-api.alpaca.markets/v2"
    if ALPACA_MODE != "live"
    else "https://api.alpaca.markets/v2"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("raanu")

# ---------- AUTO-TRADER (imported AFTER load_dotenv) ----------
from auto_trader import trader

# ---------- PICKS CACHE ----------
# PICKS_CACHE already set above in CONFIG section


def _save_picks(picks: list):
    data = {
        "picks":      picks,
        "scanned_at": datetime.now(BERLIN).isoformat(),
    }
    try:
        PICKS_CACHE.write_text(json.dumps(data, indent=2, default=str))
    except Exception as e:
        log.warning(f"Could not write picks cache: {e}")


def _load_picks() -> Optional[dict]:
    if PICKS_CACHE.exists():
        try:
            return json.loads(PICKS_CACHE.read_text())
        except Exception:
            pass
    return None


# ---------- S2 PICKS CACHE ----------

def _save_picks_s2(picks: list):
    data = {"picks": picks, "scanned_at": datetime.now(BERLIN).isoformat()}
    try:
        PICKS_CACHE_S2.write_text(json.dumps(data, indent=2, default=str))
    except Exception as e:
        log.warning(f"Could not write S2 picks cache: {e}")


def _load_picks_s2() -> Optional[dict]:
    if PICKS_CACHE_S2.exists():
        try:
            return json.loads(PICKS_CACHE_S2.read_text())
        except Exception:
            pass
    return None


# ---------- BACKGROUND SCAN ----------

async def _run_scan_and_cache() -> list:
    """Run S1 scanner in a thread pool (non-blocking), cache results."""
    from scanner import find_top_picks
    log.info("[S1] Running momentum scan...")
    loop  = asyncio.get_event_loop()
    picks = await loop.run_in_executor(None, lambda: find_top_picks(3))
    _save_picks(picks)
    log.info(f"[S1] Scan done — {len(picks)} picks cached")

    _send_confident_buy_alerts(picks, strategy="s1")

    try:
        await trader.run_one_cycle(picks=picks, strategy="s1")
    except Exception as e:
        log.exception(f"[S1] Trader cycle error: {e}")
        trader.event("error", f"[S1] Trader cycle crashed: {e}")

    return picks


async def _run_scan_and_cache_s2() -> list:
    """Run S2 scanner in a thread pool (non-blocking), cache results."""
    from scanner import find_top_picks_s2
    log.info("[S2] Running VCP breakout scan...")
    loop  = asyncio.get_event_loop()
    picks = await loop.run_in_executor(None, lambda: find_top_picks_s2(3))
    _save_picks_s2(picks)
    log.info(f"[S2] Scan done — {len(picks)} picks cached")

    _send_confident_buy_alerts(picks, strategy="s2")

    try:
        await trader.run_one_cycle(picks=picks, strategy="s2")
    except Exception as e:
        log.exception(f"[S2] Trader cycle error: {e}")
        trader.event("error", f"[S2] Trader cycle crashed: {e}")

    return picks


# Score >= 75 in a confirmed uptrend = high-conviction entry
_CONFIDENT_BUY_THRESHOLD = 75

def _send_confident_buy_alerts(picks: list, strategy: str = "s1"):
    """Send Telegram alert for high-conviction picks, tagged by strategy."""
    from notifier import send_telegram, _strat_tag
    gate_key = "uptrend" if strategy == "s1" else "stage2"
    confident = [p for p in picks if p.get("score", 0) >= _CONFIDENT_BUY_THRESHOLD and p.get(gate_key)]
    if not confident:
        return

    stag = _strat_tag(strategy)
    for p in confident:
        ticker = p.get("ticker", "?")
        name = p.get("name", ticker)
        score = p.get("score", 0)
        price = p.get("price", 0)
        rsi = p.get("rsi", 0)
        mom_3m = p.get("mom_3m", 0)
        rel = p.get("rel_strength", 0)
        reasons = " | ".join(p.get("reasons", [])[:3])
        gp = "\n   🎯 *In Golden Pocket* (0.618–0.786 fib)" if p.get("in_golden_pocket") else ""

        msg = (
            f"🟢 *CONFIDENT BUY — {ticker}* ({name})\n"
            f"   {stag}\n"
            f"   Score: *{score}/100* | Uptrend confirmed\n"
            f"   💵 ${price:.2f} | RSI {rsi:.0f} | 3M momentum {mom_3m:+.1f}%\n"
            f"   Rel. strength vs SPY: {rel:+.1f}%{gp}\n"
            f"   {reasons}\n\n"
            f"   _Score ≥ {_CONFIDENT_BUY_THRESHOLD} = high conviction. Review and act._"
        )
        send_telegram(msg, strategy=strategy)
        log.info(f"[{strategy.upper()}] Confident buy alert sent: {ticker} score {score}")


def _is_trade_day() -> bool:
    """Alternates every calendar day in Berlin time — True today means skip tomorrow."""
    return datetime.now(BERLIN).toordinal() % 2 == 0


async def _execute_scheduled_trades(n_orders: int, label: str, strategy: str = "s1"):
    """
    Scan and place up to n_orders market buys for a scheduled slot.
    Respects score threshold, position sizing, and already-held check.
    Sends Telegram alerts before and after each order, tagged by strategy.
    """
    from scanner import find_top_picks, find_top_picks_s2
    from auto_trader import (
        get_free_cash, get_held_symbols, alpaca_buy_notional,
        MIN_SIGNAL_SCORE, PER_TRADE_MAX_USD,
    )
    from notifier import send_whatsapp, format_pre_trade_alert, format_trade_confirm, _strat_tag

    stag = _strat_tag(strategy)
    log.info(f"[{label}][{strategy.upper()}] Scheduled run — targeting {n_orders} order(s)")

    if strategy == "s2":
        picks = find_top_picks_s2(n=n_orders + 3)
        _save_picks_s2(picks)
        gate_key = "stage2"
    else:
        picks = find_top_picks(n=n_orders + 3)
        _save_picks(picks)
        gate_key = "uptrend"

    actionable = [
        p for p in picks
        if p.get("score", 0) >= MIN_SIGNAL_SCORE and p.get(gate_key) and p.get("ticker")
    ]

    if not actionable:
        msg = (
            f"📊 *RaanuBot — {label}*\n"
            f"{stag}\n"
            f"No stocks above score {MIN_SIGNAL_SCORE} today.\n"
            f"_No trades placed._"
        )
        send_whatsapp(msg, strategy=strategy)
        log.info(f"[{label}][{strategy.upper()}] 0 actionable picks — skipping")
        return

    held      = await get_held_symbols()
    free_cash = await get_free_cash()

    if free_cash is None:
        log.error(f"[{label}][{strategy.upper()}] Could not fetch account balance — aborting")
        return

    placed = 0
    for pick in actionable:
        if placed >= n_orders:
            break
        ticker = pick["ticker"].upper()
        if ticker in held:
            log.info(f"[{label}][{strategy.upper()}] {ticker} already held — skipping")
            continue

        notional = min(float(PER_TRADE_MAX_USD), round(free_cash * 0.05, 2))
        if notional < 1.0:
            log.info(f"[{label}][{strategy.upper()}] Insufficient cash (${free_cash:.2f}) — stopping")
            break

        try:
            send_whatsapp(format_pre_trade_alert(
                ticker, pick.get("ticker", ticker), notional,
                pick["score"], free_cash, pick.get("reasons", []),
                strategy=strategy,
            ), strategy=strategy)
            await asyncio.sleep(2)

            result = await alpaca_buy_notional(ticker, notional)
            trader.tradelog.record({
                "action":       "BUY",
                "ticker":       ticker,
                "notional_usd": notional,
                "score":        pick["score"],
                "reasons":      pick.get("reasons", []),
                "strategy":     strategy,
                "scheduled":    label,
                "alpaca_response": result,
            })
            trader.event("buy", f"[{label}][{strategy.upper()}] BUY ${notional} of {ticker} score {pick['score']}")
            send_whatsapp(format_trade_confirm("BUY", ticker, notional, result.get("status", "submitted"), strategy=strategy), strategy=strategy)

            held.add(ticker)
            free_cash -= notional
            placed += 1
        except Exception as e:
            log.error(f"[{label}][{strategy.upper()}] Order failed for {ticker}: {e}")
            trader.event("error", f"[{label}][{strategy.upper()}] {ticker} failed: {e}")

    if placed == 0:
        send_whatsapp(
            f"📊 *RaanuBot — {label}*\n"
            f"{stag}\n"
            f"Top picks already held. No new positions opened.",
            strategy=strategy,
        )
    log.info(f"[{label}][{strategy.upper()}] Done — placed {placed}/{n_orders} order(s)")


# ── Pre-market scan (3:30 AM ET = 30 min before pre-market open) ────────────
async def _premarket_scan_and_notify():
    """Scan both strategies and send separate Telegram alerts to each strategy's chat."""
    from notifier import send_telegram
    log.info("[Pre-market] Running dual-strategy scan...")
    picks_s1 = await _run_scan_and_cache()
    picks_s2 = await _run_scan_and_cache_s2()

    # S1 alert → S1 chat
    s1_lines = ["📡 *RaanuBot — Pre-market Scan*", "📊 *S1 Pullback*", ""]
    if picks_s1:
        medals = ["🏆", "🥈", "🥉"]
        s1_lines.append(f"{len(picks_s1)} signal(s) found:\n")
        for i, p in enumerate(picks_s1):
            score = p.get("score", 0)
            heat = "🔥" if score >= 75 else "📈"
            ticker = p.get("ticker", "?")
            name = p.get("name", ticker)
            gp = " | 🎯 GP" if p.get("in_golden_pocket") else ""
            s1_lines.append(
                f"{medals[i] if i < 3 else '  '} *{ticker}* ({name}) {heat} {score}/100{gp}"
            )
    else:
        s1_lines.append("⚠️ No strong pullback signals today.")
    s1_lines.append("\n_Auto-trader will execute at market open if enabled._")
    send_telegram("\n".join(s1_lines), strategy="s1")

    # S2 alert → S2 chat
    s2_lines = ["📡 *RaanuBot — Pre-market Scan*", "🚀 *S2 Breakout*", ""]
    if picks_s2:
        medals = ["🏆", "🥈", "🥉"]
        s2_lines.append(f"{len(picks_s2)} signal(s) found:\n")
        for i, p in enumerate(picks_s2):
            score = p.get("score", 0)
            heat = "🔥" if score >= 75 else "📈"
            ticker = p.get("ticker", "?")
            name = p.get("name", ticker)
            s2_lines.append(
                f"{medals[i] if i < 3 else '  '} *{ticker}* ({name}) {heat} {score}/100"
            )
    else:
        s2_lines.append("⚠️ No strong breakout signals today.")
    s2_lines.append("\n_Auto-trader will execute at market open if enabled._")
    send_telegram("\n".join(s2_lines), strategy="s2")

    log.info(f"[Pre-market] Telegram sent — S1: {len(picks_s1)}, S2: {len(picks_s2)}")


# Schedule slots:
#   03:30 ET  — pre-market scan + Telegram alert (scan only, no orders)
#   07:00 Berlin — scan + execute top 2 orders  (alternating days)
#   14:30 Berlin — scan + execute top 1 order   (alternating days)

# Trade slots run in Berlin time
_BERLIN_SLOTS = [
    (7,  0,  2, "Morning-7am"),
    (14, 30, 1, "Afternoon-2:30pm"),
]

# Pre-market slot runs in US/Eastern time
_PREMARKET_ET = (3, 30)  # 3:30 AM ET = 30 min before 4:00 AM pre-market


async def _premarket_loop():
    """Fires daily at 3:30 AM ET — scan + Telegram notification, no orders."""
    log.info("Pre-market loop started — 3:30 AM ET daily (30 min before pre-market)")
    while True:
        now = datetime.now(US_EAST)
        t = now.replace(hour=_PREMARKET_ET[0], minute=_PREMARKET_ET[1], second=0, microsecond=0)
        if now >= t:
            t += timedelta(days=1)
        # Skip weekends (Sat=5, Sun=6)
        while t.weekday() >= 5:
            t += timedelta(days=1)
        sleep_sec = (t - now).total_seconds()
        log.info(
            f"Next pre-market scan: {t.strftime('%Y-%m-%d %H:%M %Z')} "
            f"(in {sleep_sec/3600:.1f}h)"
        )
        await asyncio.sleep(sleep_sec)
        try:
            await _premarket_scan_and_notify()
        except Exception as e:
            log.exception(f"Pre-market scan error: {e}")


async def _scheduled_trade_loop():
    """
    Fires at 07:00 and 14:30 Berlin time on alternating calendar days.
    Non-trade days: scans and caches picks but places no orders.
    """
    log.info("Scheduled trade loop started — 7:00 AM and 2:30 PM Berlin on alternating days")
    asyncio.create_task(_run_scan_and_cache())  # immediate startup scan

    while True:
        now     = datetime.now(BERLIN)
        targets = []
        for h, m, n_orders, label in _BERLIN_SLOTS:
            t = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if now >= t:
                t += timedelta(days=1)
            targets.append((t, n_orders, label))

        targets.sort(key=lambda x: x[0])
        next_t, next_n, next_label = targets[0]
        sleep_sec = (next_t - now).total_seconds()
        log.info(
            f"Next slot: {next_label} at {next_t.strftime('%Y-%m-%d %H:%M %Z')} "
            f"(in {sleep_sec/3600:.1f}h)"
        )
        await asyncio.sleep(sleep_sec)

        try:
            if _is_trade_day():
                log.info(f"[{next_label}] Trade day ✓ — executing both strategies")
                await _execute_scheduled_trades(next_n, next_label, strategy="s1")
                await _execute_scheduled_trades(next_n, next_label, strategy="s2")
            else:
                log.info(f"[{next_label}] Rest day — scanning only, no orders")
                await _run_scan_and_cache()
                await _run_scan_and_cache_s2()
        except Exception as e:
            log.exception(f"Scheduled slot error [{next_label}]: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from profit_monitor import monitor_loop
    tasks = [
        asyncio.create_task(_premarket_loop()),
        asyncio.create_task(_scheduled_trade_loop()),
        asyncio.create_task(monitor_loop()),
    ]
    yield
    for t in tasks:
        t.cancel()


# ---------- APP ----------
app = FastAPI(title="RaanuTradingBot", version="2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- ALPACA CLIENT ----------
def alpaca_headers() -> dict:
    if not ALPACA_API_KEY:
        raise HTTPException(
            status_code=400,
            detail="ALPACA_API_KEY is not set. Add it to your .env file.",
        )
    return {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
        "Content-Type":        "application/json",
    }


async def alpaca_get(path: str, params: Optional[dict] = None):
    url = f"{BROKER_BASE}{path}"
    log.info(f"GET  {url}")
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.get(url, headers=alpaca_headers(), params=params)
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Network error: {e}")
    if r.status_code == 401:
        raise HTTPException(status_code=401, detail="Alpaca rejected the API key. Check ALPACA_API_KEY and ALPACA_SECRET_KEY in .env.")
    if r.status_code == 429:
        raise HTTPException(status_code=429, detail="Alpaca rate limit hit.")
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=f"Alpaca error: {r.text}")
    return r.json()


async def alpaca_post(path: str, body: dict):
    url = f"{BROKER_BASE}{path}"
    log.info(f"POST {url}")
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.post(url, headers=alpaca_headers(), json=body)
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Network error: {e}")
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=f"Alpaca error: {r.text}")
    return r.json()


async def alpaca_delete(path: str):
    url = f"{BROKER_BASE}{path}"
    log.info(f"DELETE {url}")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.delete(url, headers=alpaca_headers())
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return {"cancelled": True}


# ---------- API ENDPOINTS ----------
@app.get("/api/config")
def get_config():
    """Tells the dashboard the broker mode — never exposes keys."""
    return {
        "broker":         "alpaca",
        "mode":           ALPACA_MODE,
        "key_configured": bool(ALPACA_API_KEY),
    }


@app.get("/api/health")
def health():
    from notifier import is_configured as tg_configured
    return {
        "status":         "ok",
        "broker":         "alpaca",
        "mode":           ALPACA_MODE,
        "key_configured": bool(ALPACA_API_KEY),
        "telegram_configured": tg_configured(),
        "config": {
            "stop_loss_pct":       os.getenv("STOP_LOSS_PCT", "3.0"),
            "trail_activate_pct":  os.getenv("TRAIL_ACTIVATE_PCT", os.getenv("TAKE_PROFIT_PCT", "5.0")),
            "trail_pct":           os.getenv("TRAIL_PCT", "2.5"),
            "hard_take_profit_pct": os.getenv("HARD_TAKE_PROFIT_PCT", "0"),
            "scan_interval_sec":   os.getenv("SCAN_INTERVAL_SEC", "1800"),
            "min_signal_score":    os.getenv("MIN_SIGNAL_SCORE", "60"),
            "weekly_trade_limit":  os.getenv("WEEKLY_TRADE_LIMIT", "2"),
            "per_trade_max_usd":   os.getenv("PER_TRADE_MAX_USD", "500"),
            "profit_check_sec":    os.getenv("PROFIT_CHECK_SEC", "300"),
        },
    }


@app.post("/api/telegram/test")
def telegram_test():
    from notifier import send_telegram, is_configured, _chat_id_for
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


@app.get("/api/exit-config")
def get_exit_cfg():
    from profit_monitor import get_exit_config
    return get_exit_config()


@app.post("/api/exit-config")
async def save_exit_cfg(request: Request):
    from profit_monitor import update_exit_config
    body = await request.json()
    updated = update_exit_config(body)
    return {"ok": True, "config": updated}


@app.get("/api/account/cash")
async def account_cash():
    """Account balances — mapped to the shape the dashboard expects."""
    acct = await alpaca_get("/account")
    total = float(acct.get("portfolio_value", 0))

    # Use portfolio history for daily P&L — includes realized gains from sells today.
    # last_equity comparison is unreliable on paper accounts (often returns 0).
    daily_ppl = 0.0
    try:
        hist = await alpaca_get(
            "/account/portfolio/history",
            params={"period": "1D", "timeframe": "15Min", "extended_hours": "true"},
        )
        pl_arr = hist.get("profit_loss", [])
        if pl_arr:
            daily_ppl = float(pl_arr[-1] or 0)
    except Exception:
        last_eq   = float(acct.get("last_equity", total))
        daily_ppl = total - last_eq

    return {
        "total":     total,
        "free":      float(acct.get("cash", 0)),
        "invested":  total - float(acct.get("cash", 0)),
        "ppl":       float(acct.get("unrealized_pl", 0)),
        "daily_ppl": daily_ppl,
        "blocked":   0,
        "currency":  acct.get("currency", "USD"),
        "_raw":      acct,
    }


@app.get("/api/account/info")
async def account_info():
    return await alpaca_get("/account")


_asset_name_cache: dict[str, str] = {}

@app.get("/api/portfolio")
async def portfolio():
    """Open positions, tagged with strategy and company name."""
    positions = await alpaca_get("/positions")
    strat_map = {}
    for t in trader.tradelog.data.get("trades", []):
        if t.get("action") == "BUY" and t.get("ticker"):
            strat_map[t["ticker"].upper()] = t.get("strategy", "s1")

    uncached = [p.get("symbol", "").upper() for p in positions if p.get("symbol", "").upper() not in _asset_name_cache]
    if uncached:
        async with httpx.AsyncClient(timeout=10) as client:
            for sym in uncached:
                try:
                    r = await client.get(f"{BROKER_BASE}/assets/{sym}", headers=alpaca_headers())
                    if r.status_code == 200:
                        _asset_name_cache[sym] = r.json().get("name", sym)
                except Exception:
                    _asset_name_cache[sym] = sym

    out = []
    for p in positions:
        sym = p.get("symbol", "").upper()
        out.append({
            "ticker":        sym,
            "name":          _asset_name_cache.get(sym, sym),
            "quantity":      float(p.get("qty", 0)),
            "averagePrice":  float(p.get("avg_entry_price", 0)),
            "currentPrice":  float(p.get("current_price", 0)),
            "ppl":           float(p.get("unrealized_pl", 0)),
            "fxPpl":         0,
            "initialFill":   p.get("asset_id"),
            "strategy":      strat_map.get(sym, ""),
            "_raw":          p,
        })
    return out


@app.get("/api/orders")
async def orders():
    """Open/pending orders."""
    return await alpaca_get("/orders", params={"status": "open", "limit": 100})


@app.get("/api/history/orders")
async def history_orders(limit: int = 50):
    """Closed orders (filled, cancelled, expired)."""
    return await alpaca_get("/orders", params={"status": "closed", "limit": min(limit, 500)})


class OrderRequest(BaseModel):
    ticker: str
    quantity: Optional[float] = None
    notional: Optional[float] = None  # dollar amount, alternative to qty


@app.post("/api/orders/buy")
async def place_buy(order: OrderRequest):
    body: dict = {
        "symbol":        order.ticker.upper(),
        "side":          "buy",
        "type":          "market",
        "time_in_force": "day",
    }
    if order.notional:
        body["notional"] = str(round(order.notional, 2))
    else:
        body["qty"] = str(order.quantity)
    return await alpaca_post("/orders", body)


@app.post("/api/orders/sell")
async def place_sell(order: OrderRequest):
    body: dict = {
        "symbol":        order.ticker.upper(),
        "side":          "sell",
        "type":          "market",
        "time_in_force": "day",
    }
    if order.notional:
        body["notional"] = str(round(order.notional, 2))
    else:
        body["qty"] = str(abs(order.quantity))
    return await alpaca_post("/orders", body)


@app.delete("/api/orders/{order_id}")
async def cancel_order(order_id: str):
    return await alpaca_delete(f"/orders/{order_id}")


# ---------- AUTO-TRADER CONTROL ----------
@app.get("/api/auto/status")
def auto_status():
    return trader.status()


@app.post("/api/auto/start")
def auto_start():
    if not ALPACA_API_KEY:
        raise HTTPException(status_code=400, detail="ALPACA_API_KEY not configured. Set it in .env first.")
    trader.enabled = True
    trader.event("control", "Auto-trader ENABLED")
    return {"enabled": True}


@app.post("/api/auto/stop")
def auto_stop():
    trader.enabled = False
    trader.event("control", "Auto-trader DISABLED")
    return {"enabled": False}


@app.post("/api/auto/scan-now")
async def auto_scan_now(force: bool = False):
    """Force an immediate scan + cache refresh.
    ?force=true  — bypass market-hours gate and limit to 5 stocks (test mode)."""
    from scanner import find_top_picks
    if force:
        log.info("TEST MODE — scanning 5 stocks only, market hours bypassed")
        picks = find_top_picks(n=3, max_stocks=5)
        _save_picks(picks)
    else:
        picks = await _run_scan_and_cache()
    await trader.run_one_cycle(picks=picks, force_market_open=force)
    return {"picks": len(picks), "forced": force, **trader.status()}


@app.get("/api/auto/scan-preview")
async def auto_scan_preview():
    """Return cached picks without running a new scan."""
    from auto_trader import MIN_SIGNAL_SCORE
    cached = _load_picks()
    if not cached:
        return {"message": "No scan results yet — scan runs at 07:00 and 18:00 Berlin time", "picks": []}
    return {
        "scanned_at": cached["scanned_at"],
        "min_score":  MIN_SIGNAL_SCORE,
        "picks":      cached["picks"],
        "actionable": [p for p in cached["picks"] if p.get("score", 0) >= MIN_SIGNAL_SCORE and p.get("uptrend") and p.get("ticker")],
    }


# ---------- XETRA/GETTEX SCANNER ----------
@app.get("/api/scan/top3")
async def scan_top3():
    """
    Return the latest XETRA/GETTEX picks instantly from cache.
    Cache is refreshed automatically at 07:00 and 18:00 Berlin time,
    and immediately on server startup.
    """
    from scanner import get_universe_summary
    cached = _load_picks()
    if cached:
        return {
            "picks":       cached["picks"],
            "scanned_at":  cached["scanned_at"],
            "from_cache":  True,
            "universe":    get_universe_summary(),
            "count":       len(cached["picks"]),
        }
    # First startup before cache exists — run live (happens once)
    from scanner import find_top_picks
    picks = find_top_picks(3)
    _save_picks(picks)
    return {
        "picks":      picks,
        "scanned_at": datetime.now(BERLIN).isoformat(),
        "from_cache": False,
        "universe":   get_universe_summary(),
        "count":      len(picks),
    }


@app.post("/api/auth/pin")
async def verify_pin(request: Request):
    """Verify trade PIN. Format: 2 letters + 6 digits (e.g. AB123456)."""
    body     = await request.json()
    entered  = body.get("pin", "").strip().upper()
    expected = os.getenv("TRADE_PIN", "").strip().upper()
    if not expected:
        return {"ok": True, "reason": "no_pin_configured"}
    return {"ok": entered == expected}


@app.get("/api/test/twilio")
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


@app.post("/api/scan/alert-now")
async def scan_alert_now():
    """Trigger morning alerts for both strategies immediately (for testing)."""
    from notifier import send_whatsapp, format_daily_alert

    cached_s1 = _load_picks()
    picks_s1  = cached_s1["picks"] if cached_s1 else []
    if not picks_s1:
        from scanner import find_top_picks
        picks_s1 = find_top_picks(3)
        _save_picks(picks_s1)

    cached_s2 = _load_picks_s2()
    picks_s2  = cached_s2["picks"] if cached_s2 else []
    if not picks_s2:
        from scanner import find_top_picks_s2
        picks_s2 = find_top_picks_s2(3)
        _save_picks_s2(picks_s2)

    msg_s1 = format_daily_alert(picks_s1, strategy="s1")
    msg_s2 = format_daily_alert(picks_s2, strategy="s2")
    ok1 = send_whatsapp(msg_s1, strategy="s1")
    ok2 = send_whatsapp(msg_s2, strategy="s2")
    return {"sent_s1": ok1, "sent_s2": ok2, "picks_s1": len(picks_s1), "picks_s2": len(picks_s2)}


# ---------- WHATSAPP WEBHOOK (Twilio) ----------

async def _handle_whatsapp_command(cmd: str):
    """
    Runs in a background task — called after TwiML is already returned to Twilio.
    This avoids Twilio's 15-second webhook timeout killing long-running commands.
    """
    from notifier import send_whatsapp, format_trade_confirm, format_portfolio_status, format_daily_alert

    try:
        if cmd == "STATUS":
            try:
                from profit_monitor import get_positions_for_status
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
                from scanner import find_top_picks
                loop = asyncio.get_event_loop()
                picks_s1 = await loop.run_in_executor(None, lambda: find_top_picks(3))
                _save_picks(picks_s1)
            send_whatsapp(format_daily_alert(picks_s1, strategy="s1"), strategy="s1")

            cached_s2 = _load_picks_s2()
            if cached_s2 and cached_s2.get("picks"):
                picks_s2 = cached_s2["picks"]
            else:
                from scanner import find_top_picks_s2
                loop = asyncio.get_event_loop()
                picks_s2 = await loop.run_in_executor(None, lambda: find_top_picks_s2(3))
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
                        r = await c.post(f"{BROKER_BASE}/orders", headers=alpaca_headers(), json=body)
                    if r.status_code >= 400:
                        send_whatsapp(f"❌ Order rejected: {r.text[:200]}")
                    else:
                        send_whatsapp(format_trade_confirm("BUY", ticker, usd, r.json().get("status", "submitted")))
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
                        r = await c.delete(f"{BROKER_BASE}/positions/{ticker.upper()}", headers=alpaca_headers())
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
            from notifier import send_whatsapp
            send_whatsapp(f"❌ Internal error: {e}")
        except Exception:
            pass


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(
    request: Request,
    Body: str = Form(default=""),
    From: str = Form(default=""),
):
    """
    Twilio sends incoming WhatsApp messages here.
    Returns TwiML immediately so we stay well within Twilio's 15s timeout.
    Command handling runs in a background task.
    """
    expected_from = os.getenv("USER_WHATSAPP", "whatsapp:+919176911755").strip()
    if From and From != expected_from:
        log.warning(f"WhatsApp message from unknown number: {From}")
        return PlainTextResponse("<?xml version='1.0'?><Response/>", media_type="text/xml")

    cmd = Body.strip().upper()
    log.info(f"WhatsApp command: '{cmd}' from {From}")

    # Fire-and-forget — respond to Twilio immediately to avoid timeout
    asyncio.create_task(_handle_whatsapp_command(cmd))

    return PlainTextResponse("<?xml version='1.0'?><Response/>", media_type="text/xml")


# ---------- STOCK SEARCH / MARKET DATA (Alpaca) ----------
@app.get("/api/stocks/search")
async def stocks_search(q: str = ""):
    """Search tradable US equities by symbol or name."""
    import alpaca_data
    if not q.strip():
        raise HTTPException(status_code=400, detail="Provide ?q=<symbol or name>")
    results = alpaca_data.search_assets(q.strip(), limit=15)
    return {"query": q, "results": results, "source": "alpaca"}


@app.get("/api/stocks/active")
async def stocks_most_active(top: int = 20):
    """Top stocks by volume today."""
    import alpaca_data
    data = alpaca_data.get_most_active(top=min(top, 50))
    return {"most_active": data, "source": "alpaca"}


@app.get("/api/stocks/movers")
async def stocks_market_movers(top: int = 10):
    """Top daily gainers and losers."""
    import alpaca_data
    data = alpaca_data.get_market_movers(top=min(top, 25))
    return {"gainers": data.get("gainers", []), "losers": data.get("losers", []), "source": "alpaca"}


# ---------- STREAMING SCAN ----------
@app.get("/api/scan/stream")
async def scan_stream():
    """
    SSE endpoint — scans the curated, liquid quality universe (the same one the
    auto-trader uses) and streams ONLY stocks that pass our strategy: those in a
    confirmed uptrend. Non-uptrend names are scored but not emitted — there's no
    point brute-forcing the entire market when the strategy only ever buys
    uptrend pullbacks. Batch-downloads in chunks so results stream progressively.
    """
    from scanner import FALLBACK_UNIVERSE, get_ticker_name, CHUNK_SIZE
    from strategy import score_from_df, batch_download, benchmark_return_3m

    async def generator():
        import json
        loop = asyncio.get_event_loop()

        universe = FALLBACK_UNIVERSE

        # SPY 3-month return — relative-strength benchmark (computed once).
        bench = await loop.run_in_executor(None, benchmark_return_3m)

        total = len(universe)
        yield f"data: {json.dumps({'status': 'downloading', 'total': total})}\n\n"

        scanned = 0
        emitted = 0
        chunks = [universe[i:i + CHUNK_SIZE] for i in range(0, len(universe), CHUNK_SIZE)]

        def _cap_label(mc):
            if not mc: return "—"
            if mc >= 200e9: return "Mega"
            if mc >= 10e9:  return "Large"
            if mc >= 2e9:   return "Mid"
            if mc >= 300e6: return "Small"
            return "Micro"

        def _fetch_market_cap(ticker):
            try:
                import yfinance as yf
                return yf.Ticker(ticker).fast_info.market_cap
            except Exception:
                return None

        for chunk in chunks:
            data = await loop.run_in_executor(None, batch_download, chunk)
            for ticker in chunk:
                result = score_from_df(ticker, data.get(ticker), bench_ret_3m=bench)
                scanned += 1
                # Lightweight progress tick so the bar advances even though most
                # tickers are filtered out (only uptrend candidates are emitted).
                if scanned % 25 == 0 or scanned == total:
                    yield f"data: {json.dumps({'progress': True, 'scanned': scanned, 'total': total})}\n\n"
                # Swing filter: uptrend + score >= 60 + MACD not bearish + RSI not overbought.
                if not (result.get("ok") and result.get("uptrend")):
                    continue
                if result.get("score", 0) < 60:
                    continue
                rsi_val = result.get("rsi", 50)
                macd_val = result.get("macd", 0)
                macd_sig = result.get("macd_signal", 0)
                if rsi_val > 72 or macd_val < macd_sig:
                    continue
                result["total"] = total
                result["name"] = get_ticker_name(ticker)
                mc = await loop.run_in_executor(None, _fetch_market_cap, ticker)
                result["cap_label"] = _cap_label(mc)
                yield f"data: {json.dumps(result)}\n\n"
                emitted += 1

        yield f"data: {json.dumps({'done': True, 'scanned': scanned, 'emitted': emitted, 'total': total})}\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------- STRATEGY COMPARISON ----------

@app.get("/api/strategy/compare")
async def strategy_compare():
    """Return trade performance split by strategy for the dashboard Strategy tab."""
    from auto_trader import trader as _trader
    all_trades = _trader.tradelog.data.get("trades", [])

    # Also pull closed orders from Alpaca for real P&L
    try:
        closed = await alpaca_get("/orders", params={
            "status": "closed", "limit": "200", "direction": "desc"
        })
    except Exception:
        closed = []

    # Build a map of filled orders by symbol+side for P&L lookup
    order_map = {}
    for o in closed:
        sym = o.get("symbol", "")
        side = o.get("side", "")
        key = f"{sym}_{side}_{(o.get('filled_at') or '')[:10]}"
        order_map[key] = o

    def _strategy_stats(strat: str) -> dict:
        trades = [t for t in all_trades if t.get("strategy") == strat]
        if not trades:
            return {
                "strategy": strat,
                "label": "S1 Pullback" if strat == "s1" else "S2 Breakout",
                "total_trades": 0, "profitable": 0, "loss_making": 0,
                "win_rate": 0, "net_pnl": 0, "avg_return_pct": 0,
                "trades": [],
            }

        return {
            "strategy": strat,
            "label": "S1 Pullback" if strat == "s1" else "S2 Breakout",
            "total_trades": len(trades),
            "trades": trades[-50:],
        }

    # Picks caches
    s1_picks = _load_picks()
    s2_picks = _load_picks_s2()

    return {
        "s1": _strategy_stats("s1"),
        "s2": _strategy_stats("s2"),
        "s1_picks": s1_picks.get("picks", []) if s1_picks else [],
        "s2_picks": s2_picks.get("picks", []) if s2_picks else [],
        "s1_scanned_at": s1_picks.get("scanned_at") if s1_picks else None,
        "s2_scanned_at": s2_picks.get("scanned_at") if s2_picks else None,
    }


@app.get("/api/scan/stream/s2")
async def scan_stream_s2():
    """SSE endpoint — scans the curated universe with Strategy 2 (VCP Breakout)."""
    from scanner import FALLBACK_UNIVERSE, get_ticker_name, CHUNK_SIZE
    from strategy2 import score_from_df_s2
    from strategy import batch_download, benchmark_return_3m

    async def generator():
        import json
        loop = asyncio.get_event_loop()
        universe = FALLBACK_UNIVERSE
        bench = await loop.run_in_executor(None, benchmark_return_3m)
        total = len(universe)
        yield f"data: {json.dumps({'status': 'downloading', 'total': total, 'strategy': 's2'})}\n\n"

        scanned = 0
        emitted = 0
        chunks = [universe[i:i + CHUNK_SIZE] for i in range(0, len(universe), CHUNK_SIZE)]

        for chunk in chunks:
            data = await loop.run_in_executor(None, batch_download, chunk)
            for ticker in chunk:
                result = score_from_df_s2(ticker, data.get(ticker), bench_ret_3m=bench)
                scanned += 1
                if scanned % 25 == 0 or scanned == total:
                    yield f"data: {json.dumps({'progress': True, 'scanned': scanned, 'total': total})}\n\n"
                if not (result.get("ok") and result.get("stage2")):
                    continue
                if result.get("score", 0) < 60:
                    continue
                result["total"] = total
                result["name"] = get_ticker_name(ticker)
                yield f"data: {json.dumps(result)}\n\n"
                emitted += 1

        yield f"data: {json.dumps({'done': True, 'scanned': scanned, 'emitted': emitted, 'total': total})}\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/auto/scan-now/s2")
async def auto_scan_now_s2(force: bool = False):
    """Force an immediate S2 scan."""
    from scanner import find_top_picks_s2
    if force:
        log.info("[S2] TEST MODE — scanning 5 stocks only")
        picks = find_top_picks_s2(n=3, max_stocks=5)
        _save_picks_s2(picks)
    else:
        picks = await _run_scan_and_cache_s2()
    return {"picks": len(picks), "strategy": "s2", "forced": force}


# ---------- STATIC ----------
@app.get("/")
def root():
    html_path = HERE / "RaanuTradingBot.html"
    if not html_path.exists():
        return JSONResponse({"error": "RaanuTradingBot.html not found."}, status_code=404)
    return FileResponse(html_path)


# ---------- MAIN ----------
if __name__ == "__main__":
    import uvicorn
    from auto_trader import WEEKLY_TRADE_LIMIT, PER_TRADE_MAX_USD, MIN_SIGNAL_SCORE

    print("=" * 60)
    print("  RaanuTradingBot — Alpaca Backend")
    print("=" * 60)
    print(f"  Mode:          {ALPACA_MODE.upper()}")
    print(f"  Broker URL:    {BROKER_BASE}")
    print(f"  API key:       {'configured ✓' if ALPACA_API_KEY else 'NOT SET — edit .env'}")
    print(f"  Dashboard:     http://localhost:8000")
    print(f"  Pre-market:    03:30 ET daily — scan + Telegram alert (no orders)")
    print(f"  Trade slots:   07:00 Berlin — top 2 orders | 14:30 Berlin — top 1 order")
    print(f"                 Alternating days (trade / rest / trade / rest...)")
    print(f"  Weekly limit:  {WEEKLY_TRADE_LIMIT} trades / ${PER_TRADE_MAX_USD} max each")
    print(f"  Min score:     {MIN_SIGNAL_SCORE}/100")
    print("=" * 60)

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False)
