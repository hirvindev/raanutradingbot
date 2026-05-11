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
    """Run the scanner, cache results, pass picks to the ad-hoc trader."""
    from scanner import find_top_picks
    log.info("Running momentum scan...")
    picks = find_top_picks(3)
    _save_picks(picks)
    log.info(f"Scan done — {len(picks)} picks cached")

    try:
        await trader.run_one_cycle(picks=picks)
    except Exception as e:
        log.exception(f"Trader cycle error: {e}")
        trader.event("error", f"Trader cycle crashed: {e}")

    return picks


def _is_trade_day() -> bool:
    """Alternates every calendar day in Berlin time — True today means skip tomorrow."""
    return datetime.now(BERLIN).toordinal() % 2 == 0


async def _execute_scheduled_trades(n_orders: int, label: str):
    """
    Scan and place up to n_orders market buys for a scheduled slot.
    Respects score threshold, position sizing, and already-held check.
    Sends WhatsApp alerts before and after each order.
    """
    from scanner import find_top_picks
    from auto_trader import (
        get_free_cash, get_held_symbols, alpaca_buy_notional,
        MIN_SIGNAL_SCORE, PER_TRADE_MAX_USD,
    )
    from notifier import send_whatsapp, format_pre_trade_alert, format_trade_confirm

    log.info(f"[{label}] Scheduled run — targeting {n_orders} order(s)")

    picks = find_top_picks(n=n_orders + 3)
    _save_picks(picks)

    actionable = [
        p for p in picks
        if p.get("score", 0) >= MIN_SIGNAL_SCORE and p.get("us_adr")
    ]

    if not actionable:
        msg = (
            f"📊 *RaanuBot — {label}*\n"
            f"No stocks above score {MIN_SIGNAL_SCORE} today.\n"
            f"_No trades placed._"
        )
        send_whatsapp(msg)
        log.info(f"[{label}] 0 actionable picks — skipping")
        return

    held      = await get_held_symbols()
    free_cash = await get_free_cash()

    if free_cash is None:
        log.error(f"[{label}] Could not fetch account balance — aborting")
        return

    placed = 0
    for pick in actionable:
        if placed >= n_orders:
            break
        ticker = pick["us_adr"].upper()
        if ticker in held:
            log.info(f"[{label}] {ticker} already held — skipping")
            continue

        notional = min(float(PER_TRADE_MAX_USD), round(free_cash * 0.05, 2))
        if notional < 1.0:
            log.info(f"[{label}] Insufficient cash (${free_cash:.2f}) — stopping")
            break

        try:
            send_whatsapp(format_pre_trade_alert(
                ticker, pick.get("ticker", ticker), notional,
                pick["score"], free_cash, pick.get("reasons", []),
            ))
            await asyncio.sleep(2)

            result = await alpaca_buy_notional(ticker, notional)
            trader.tradelog.record({
                "action":       "BUY",
                "us_adr":       ticker,
                "notional_usd": notional,
                "score":        pick["score"],
                "reasons":      pick.get("reasons", []),
                "scheduled":    label,
                "alpaca_response": result,
            })
            trader.event("buy", f"[{label}] BUY ${notional} of {ticker} score {pick['score']}")
            send_whatsapp(format_trade_confirm("BUY", ticker, notional, result.get("status", "submitted")))

            held.add(ticker)
            free_cash -= notional
            placed += 1
        except Exception as e:
            log.error(f"[{label}] Order failed for {ticker}: {e}")
            trader.event("error", f"[{label}] {ticker} failed: {e}")

    if placed == 0:
        send_whatsapp(
            f"📊 *RaanuBot — {label}*\n"
            f"Top picks already held. No new positions opened."
        )
    log.info(f"[{label}] Done — placed {placed}/{n_orders} order(s)")


# Berlin schedule:
#   07:00 Berlin — scan + execute top 2 orders  (alternating days)
#   14:30 Berlin — scan + execute top 1 order   (alternating days)
_BERLIN_SLOTS = [
    (7,  0,  2, "Morning-7am"),
    (14, 30, 1, "Afternoon-2:30pm"),
]


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
                log.info(f"[{next_label}] Trade day ✓ — executing")
                await _execute_scheduled_trades(next_n, next_label)
            else:
                log.info(f"[{next_label}] Rest day — scanning only, no orders")
                await _run_scan_and_cache()
        except Exception as e:
            log.exception(f"Scheduled slot error [{next_label}]: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from profit_monitor import monitor_loop
    tasks = [
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
            send_whatsapp("🔍 Fetching latest picks...")
            # Use cached results first (instant); fall back to fresh scan if no cache
            cached = _load_picks()
            if cached and cached.get("picks"):
                picks = cached["picks"]
            else:
                from scanner import find_top_picks
                loop = asyncio.get_event_loop()
                picks = await loop.run_in_executor(None, lambda: find_top_picks(3))
                _save_picks(picks)
            send_whatsapp(format_daily_alert(picks))

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
    SSE endpoint — one yfinance batch download (~1-2 s), then streams each
    scored ticker to the browser immediately. The table populates in real time.
    """
    from scanner import UNIVERSE
    from strategy import score_from_df, batch_download

    async def generator():
        import json
        total = len(UNIVERSE)
        yield f"data: {json.dumps({'status': 'downloading', 'total': total})}\n\n"

        # Run blocking download in thread so the event loop stays free
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, batch_download, UNIVERSE)

        scanned = 0
        for ticker in UNIVERSE:
            df     = data.get(ticker)
            result = score_from_df(ticker, df)
            result["total"] = total
            yield f"data: {json.dumps(result)}\n\n"
            scanned += 1

        yield f"data: {json.dumps({'done': True, 'scanned': scanned, 'total': total})}\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
    print(f"  Schedule:      07:00 Berlin — top 2 orders | 14:30 Berlin — top 1 order")
    print(f"                 Alternating days (trade / rest / trade / rest...)")
    print(f"  Weekly limit:  {WEEKLY_TRADE_LIMIT} trades / ${PER_TRADE_MAX_USD} max each")
    print(f"  Min score:     {MIN_SIGNAL_SCORE}/100")
    print("=" * 60)

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False)
