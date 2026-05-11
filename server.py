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
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

BERLIN = ZoneInfo("Europe/Berlin")

# ---------- CONFIG ----------
HERE = Path(__file__).parent
load_dotenv(HERE / ".env", override=False)  # no-op on Railway; env vars come from dashboard
_DATA_DIR = Path("/tmp") if Path("/tmp").exists() and not (HERE / ".env").exists() else HERE
PICKS_CACHE = _DATA_DIR / "last_picks.json"

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


# ---------- BACKGROUND SCAN ----------

async def _run_scan_and_cache() -> list:
    """
    Run the XETRA scanner, cache results, and hand picks to the trader.
    This is the single place scanning happens — no duplicate yfinance calls.
    """
    from scanner import find_top_picks
    log.info("Running scheduled XETRA/GETTEX scan...")
    picks = find_top_picks(3)
    _save_picks(picks)
    log.info(f"Scan done — {len(picks)} picks cached to {PICKS_CACHE.name}")

    # Pass picks to trader so it doesn't re-scan; it only trades if enabled.
    try:
        await trader.run_one_cycle(picks=picks)
    except Exception as e:
        log.exception(f"Trader cycle error: {e}")
        trader.event("error", f"Trader cycle crashed: {e}")

    return picks


async def _scheduled_scan_loop():
    """
    Scans run twice daily regardless of the auto-execute toggle:
      07:00 Berlin — pre-market refresh (WhatsApp alert follows at 07:30)
      18:00 Berlin — after XETRA close (full-day signal data)
    """
    FIRE_TIMES = [(7, 0), (18, 0)]
    log.info("Scheduled scan loop started — fires at 07:00 and 18:00 Berlin time")

    # Kick off an immediate scan on startup so the dashboard has data right away.
    asyncio.create_task(_run_scan_and_cache())

    while True:
        now     = datetime.now(BERLIN)
        targets = []
        for h, m in FIRE_TIMES:
            t = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if now >= t:
                t += timedelta(days=1)
            targets.append(t)
        next_fire = min(targets)
        sleep_sec = (next_fire - now).total_seconds()
        log.info(f"Next scheduled scan at {next_fire.strftime('%Y-%m-%d %H:%M %Z')} (in {sleep_sec/3600:.1f}h)")
        await asyncio.sleep(sleep_sec)

        try:
            await _run_scan_and_cache()
        except Exception as e:
            log.exception(f"Scheduled scan error: {e}")


async def _daily_alert_loop():
    """Fires WhatsApp morning alert every day at 07:30 Berlin — reads from cache."""
    from notifier import send_whatsapp, format_daily_alert

    log.info("Daily alert loop started — fires at 07:30 Berlin time")
    while True:
        now    = datetime.now(BERLIN)
        target = now.replace(hour=7, minute=30, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        sleep_sec = (target - now).total_seconds()
        log.info(f"Next morning alert in {sleep_sec/3600:.1f}h at {target.strftime('%Y-%m-%d %H:%M %Z')}")
        await asyncio.sleep(sleep_sec)

        try:
            # 07:00 scan already ran; use that cache.  Fall back to live scan if needed.
            cached = _load_picks()
            picks  = cached["picks"] if cached else []
            if not picks:
                from scanner import find_top_picks
                picks = find_top_picks(3)
                _save_picks(picks)
            msg = format_daily_alert(picks)
            ok  = send_whatsapp(msg)
            log.info(f"Morning alert {'sent' if ok else 'failed (Twilio not configured?)'} — {len(picks)} picks")
        except Exception as e:
            log.exception(f"Daily alert error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from profit_monitor import monitor_loop
    tasks = [
        asyncio.create_task(_scheduled_scan_loop()),
        asyncio.create_task(_daily_alert_loop()),
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
    return {
        "status":         "ok",
        "broker":         "alpaca",
        "mode":           ALPACA_MODE,
        "key_configured": bool(ALPACA_API_KEY),
    }


@app.get("/api/account/cash")
async def account_cash():
    """Account balances — mapped to the shape the dashboard expects."""
    acct = await alpaca_get("/account")
    return {
        "total":    float(acct.get("portfolio_value", 0)),
        "free":     float(acct.get("cash", 0)),
        "invested": float(acct.get("portfolio_value", 0)) - float(acct.get("cash", 0)),
        "ppl":      float(acct.get("unrealized_pl", 0)),
        "blocked":  0,
        "currency": acct.get("currency", "USD"),
        "_raw":     acct,
    }


@app.get("/api/account/info")
async def account_info():
    return await alpaca_get("/account")


@app.get("/api/portfolio")
async def portfolio():
    """Open positions."""
    positions = await alpaca_get("/positions")
    # Map Alpaca fields to what the dashboard renders
    out = []
    for p in positions:
        out.append({
            "ticker":        p.get("symbol"),
            "quantity":      float(p.get("qty", 0)),
            "averagePrice":  float(p.get("avg_entry_price", 0)),
            "currentPrice":  float(p.get("current_price", 0)),
            "ppl":           float(p.get("unrealized_pl", 0)),
            "fxPpl":         0,
            "initialFill":   p.get("asset_id"),
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
async def auto_scan_now():
    """Force an immediate scan + cache refresh (regardless of auto-execute state)."""
    picks = await _run_scan_and_cache()
    return {"picks": len(picks), **trader.status()}


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
        "actionable": [p for p in cached["picks"] if p.get("score", 0) >= MIN_SIGNAL_SCORE and p.get("us_adr")],
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


@app.post("/api/scan/alert-now")
async def scan_alert_now():
    """Trigger the morning WhatsApp alert immediately (for testing)."""
    from notifier import send_whatsapp, format_daily_alert
    cached = _load_picks()
    picks  = cached["picks"] if cached else []
    if not picks:
        from scanner import find_top_picks
        picks = find_top_picks(3)
        _save_picks(picks)
    msg = format_daily_alert(picks)
    ok  = send_whatsapp(msg)
    return {"sent": ok, "picks": len(picks), "message_preview": msg[:300]}


# ---------- WHATSAPP WEBHOOK (Twilio) ----------
@app.post("/webhook/whatsapp")
async def whatsapp_webhook(
    request: Request,
    Body: str = Form(default=""),
    From: str = Form(default=""),
):
    """
    Twilio sends incoming WhatsApp messages here.
    Configure this URL in Twilio console → WhatsApp Sandbox → 'When a message comes in'.
    URL: https://your-ngrok-url.ngrok.io/webhook/whatsapp
    """
    from notifier import (
        send_whatsapp, format_trade_confirm, format_portfolio_status, format_daily_alert
    )
    from profit_monitor import get_positions_for_status
    from scanner import find_top_picks

    expected_from = os.getenv("USER_WHATSAPP", "whatsapp:+919176911755").strip()
    if From and From != expected_from:
        log.warning(f"WhatsApp message from unknown number: {From}")
        return PlainTextResponse("<?xml version='1.0'?><Response/>", media_type="text/xml")

    cmd = Body.strip().upper()
    log.info(f"WhatsApp command: '{cmd}' from {From}")

    reply = ""

    if cmd == "STATUS":
        try:
            positions, account = await get_positions_for_status()
            reply = format_portfolio_status(positions, account)
        except Exception as e:
            reply = f"❌ Could not fetch portfolio: {e}"

    elif cmd == "PICKS" or cmd == "SCAN":
        reply = "🔍 Scanning XETRA/GETTEX... this takes ~30s, sending results shortly."
        send_whatsapp(reply)
        try:
            picks = find_top_picks(3)
            reply = format_daily_alert(picks)
        except Exception as e:
            reply = f"❌ Scan failed: {e}"

    elif cmd.startswith("BUY "):
        parts = cmd.split()
        ticker = parts[1] if len(parts) > 1 else ""
        usd    = float(parts[2]) if len(parts) > 2 else float(os.getenv("PER_TRADE_MAX_USD", "500"))
        if not ticker:
            reply = "❌ Usage: BUY TICKER or BUY TICKER 200"
        else:
            try:
                body  = {"symbol": ticker.upper(), "notional": str(usd), "side": "buy", "type": "market", "time_in_force": "day"}
                async with httpx.AsyncClient(timeout=20) as c:
                    r = await c.post(f"{BROKER_BASE}/orders", headers=alpaca_headers(), json=body)
                if r.status_code >= 400:
                    reply = f"❌ Order rejected: {r.text[:200]}"
                else:
                    result = r.json()
                    reply  = format_trade_confirm("BUY", ticker, usd, result.get("status", "submitted"))
            except Exception as e:
                reply = f"❌ Buy failed: {e}"

    elif cmd.startswith("SELL "):
        parts  = cmd.split()
        ticker = parts[1] if len(parts) > 1 else ""
        if not ticker:
            reply = "❌ Usage: SELL TICKER"
        else:
            try:
                async with httpx.AsyncClient(timeout=20) as c:
                    r = await c.delete(f"{BROKER_BASE}/positions/{ticker.upper()}", headers=alpaca_headers())
                if r.status_code == 404:
                    reply = f"⚠️ No open position in {ticker}"
                elif r.status_code >= 400:
                    reply = f"❌ Sell failed: {r.text[:200]}"
                else:
                    result = r.json()
                    qty    = float(result.get("qty", 0))
                    price  = float(result.get("avg_entry_price", 0))
                    reply  = format_trade_confirm("SELL", ticker, qty * price, result.get("status", "submitted"))
            except Exception as e:
                reply = f"❌ Sell failed: {e}"

    else:
        reply = (
            "🤖 *RaanuTradingBot commands:*\n"
            "  *PICKS* — today's top 3 picks\n"
            "  *BUY AAPL* — buy $500 of AAPL\n"
            "  *BUY AAPL 200* — buy $200 of AAPL\n"
            "  *SELL AAPL* — close position\n"
            "  *STATUS* — portfolio summary"
        )

    if reply:
        send_whatsapp(reply)

    # Return empty TwiML so Twilio doesn't also send a raw reply
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
    print(f"  Scans at:      07:00 and 18:00 Berlin time (always-on)")
    print(f"  Morning alert: 07:30 Berlin via WhatsApp")
    print(f"  Weekly limit:  {WEEKLY_TRADE_LIMIT} trades / ${PER_TRADE_MAX_USD} max each")
    print(f"  Min score:     {MIN_SIGNAL_SCORE}/100")
    print("=" * 60)

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False)
