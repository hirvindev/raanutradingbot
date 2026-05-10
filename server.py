"""
RaanuTradingBot — Trade 212 backend
====================================
A small local server that connects the RaanuTradingBot dashboard
to your Trade 212 account safely.

How it works:
  - Reads your T212 API key from the .env file (never embedded in HTML)
  - Exposes /api/* endpoints the dashboard can call
  - Serves the dashboard at http://localhost:8000
  - Serves the AlgoTrader dashboard at http://localhost:8000/algo

Run with:  python server.py
"""

import os
import sys
import json
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

# ---------- CONFIG ----------
HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

T212_API_KEY = os.getenv("T212_API_KEY", "").strip()
T212_MODE = os.getenv("T212_MODE", "demo").strip().lower()

if T212_MODE not in ("demo", "live"):
    print(f"⚠️  Invalid T212_MODE='{T212_MODE}'. Must be 'demo' or 'live'. Defaulting to demo.")
    T212_MODE = "demo"

BASE_URL = (
    "https://demo.trading212.com/api/v0"
    if T212_MODE == "demo"
    else "https://live.trading212.com/api/v0"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("raanu")

# ---------- AUTO-TRADER (imported AFTER load_dotenv so env is read) ----------
from auto_trader import trader, T212, SCAN_INTERVAL_SEC


_t212_client: Optional[T212] = None
def get_t212() -> T212:
    global _t212_client
    if _t212_client is None:
        _t212_client = T212(api_key=T212_API_KEY, mode=T212_MODE)
    return _t212_client


async def _scan_loop():
    """Runs in background. Cycles every SCAN_INTERVAL_SEC."""
    log.info(f"Auto-trader scan loop started (interval {SCAN_INTERVAL_SEC}s)")
    while True:
        try:
            if trader.enabled:
                await trader.run_one_cycle(get_t212())
        except Exception as e:
            log.exception(f"Scan loop error: {e}")
            trader.event("error", f"Scan loop crashed: {e}")
        await asyncio.sleep(SCAN_INTERVAL_SEC)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_scan_loop())
    yield
    task.cancel()


# ---------- APP ----------
app = FastAPI(title="RaanuTradingBot Backend", version="1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- T212 CLIENT ----------
def t212_headers():
    if not T212_API_KEY:
        raise HTTPException(
            status_code=400,
            detail="T212_API_KEY is not set. Edit the .env file and add your key.",
        )
    return {"Authorization": T212_API_KEY, "Content-Type": "application/json"}


async def t212_get(path: str):
    url = f"{BASE_URL}{path}"
    log.info(f"GET  {url}")
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.get(url, headers=t212_headers())
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Network error: {e}")
    if r.status_code == 401:
        raise HTTPException(status_code=401, detail="Trade 212 rejected the API key. Check it in .env and confirm API access is enabled in your T212 settings.")
    if r.status_code == 429:
        raise HTTPException(status_code=429, detail="Trade 212 rate limit hit. Slow down requests.")
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=f"T212 error: {r.text}")
    return r.json()


async def t212_post(path: str, body: dict):
    url = f"{BASE_URL}{path}"
    log.info(f"POST {url}  body={body}")
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.post(url, headers=t212_headers(), json=body)
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Network error: {e}")
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=f"T212 error: {r.text}")
    return r.json()


# ---------- API ENDPOINTS ----------
@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "mode": T212_MODE,
        "key_configured": bool(T212_API_KEY),
        "base_url": BASE_URL,
    }


@app.get("/api/account/cash")
async def account_cash():
    """Cash, free funds, blocked, total. The portfolio overview number."""
    return await t212_get("/equity/account/cash")


@app.get("/api/account/info")
async def account_info():
    return await t212_get("/equity/account/info")


@app.get("/api/portfolio")
async def portfolio():
    """List of open positions with quantity, avg price, current PPL."""
    return await t212_get("/equity/portfolio")


@app.get("/api/orders")
async def orders():
    """Pending orders."""
    return await t212_get("/equity/orders")


@app.get("/api/history/orders")
async def history_orders(limit: int = 20):
    return await t212_get(f"/equity/history/orders?limit={limit}")


@app.get("/api/instruments")
async def instruments():
    """Full tradable instrument list — useful for ticker search."""
    return await t212_get("/equity/metadata/instruments")


class OrderRequest(BaseModel):
    ticker: str
    quantity: float  # positive for buy, sign is normalized below


@app.post("/api/orders/buy")
async def place_buy(order: OrderRequest):
    body = {"quantity": abs(order.quantity), "ticker": order.ticker}
    return await t212_post("/equity/orders/market", body)


@app.post("/api/orders/sell")
async def place_sell(order: OrderRequest):
    body = {"quantity": -abs(order.quantity), "ticker": order.ticker}
    return await t212_post("/equity/orders/market", body)


@app.delete("/api/orders/{order_id}")
async def cancel_order(order_id: int):
    url = f"{BASE_URL}/equity/orders/{order_id}"
    log.info(f"DELETE {url}")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.delete(url, headers=t212_headers())
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return {"cancelled": True, "id": order_id}


# ---------- AUTO-TRADER CONTROL ----------
@app.get("/api/auto/status")
def auto_status():
    return trader.status()


@app.post("/api/auto/start")
def auto_start():
    if not T212_API_KEY:
        raise HTTPException(status_code=400, detail="T212 API key not configured. Set it in .env first.")
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
    """Trigger an immediate scan-and-maybe-trade cycle."""
    if not trader.enabled:
        raise HTTPException(status_code=400, detail="Auto-trader is disabled. Start it first.")
    await trader.run_one_cycle(get_t212())
    return trader.status()


@app.get("/api/auto/scan-preview")
async def auto_scan_preview():
    """Score the watchlist WITHOUT placing any order. Safe to call anytime."""
    from strategy import scan as strat_scan
    from auto_trader import WATCHLIST, MIN_SIGNAL_SCORE
    results = strat_scan(WATCHLIST)
    return {
        "watchlist": WATCHLIST,
        "min_score": MIN_SIGNAL_SCORE,
        "results": results,
        "actionable": [r for r in results if r.get("ok") and r.get("score", 0) >= MIN_SIGNAL_SCORE],
    }


# ---------- STATIC / DASHBOARD ----------

@app.get("/")
def root():
    """Serve the original RaanuTradingBot dashboard."""
    html_path = HERE / "RaanuTradingBot.html"
    if not html_path.exists():
        return JSONResponse(
            {"error": "RaanuTradingBot.html not found in this folder."},
            status_code=404,
        )
    return FileResponse(html_path)


@app.get("/algo")
def algo_dashboard():
    """
    Serve the new AlgoTrader dashboard with real-time T212 metrics.
    Place trade212-algo-dashboard.html in the same folder as server.py.
    Then open: http://localhost:8000/algo
    """
    html_path = HERE / "trade212-algo-dashboard.html"
    if not html_path.exists():
        return JSONResponse(
            {
                "error": "trade212-algo-dashboard.html not found.",
                "fix": "Download trade212-algo-dashboard.html and place it in: " + str(HERE),
            },
            status_code=404,
        )
    return FileResponse(html_path)


# ---------- MAIN ----------
if __name__ == "__main__":
    import uvicorn

    from auto_trader import WEEKLY_TRADE_LIMIT, PER_TRADE_MAX_EUR, MIN_SIGNAL_SCORE, WATCHLIST

    algo_html_present = (HERE / "trade212-algo-dashboard.html").exists()

    print("=" * 60)
    print("  RaanuTradingBot — Trade 212 Backend")
    print("=" * 60)
    print(f"  Mode:        {T212_MODE.upper()}")
    print(f"  T212 URL:    {BASE_URL}")
    print(f"  API key:     {'configured ✓' if T212_API_KEY else 'NOT SET — edit .env'}")
    print(f"  Dashboard:   http://localhost:8000")
    print(f"  AlgoDash:    http://localhost:8000/algo  {'✓' if algo_html_present else '⚠ trade212-algo-dashboard.html not found'}")
    print(f"  Tailscale:   http://<your-tailscale-ip>:8000  (auto-enabled)")
    print()
    print("  Auto-trader limits:")
    print(f"    - Max {WEEKLY_TRADE_LIMIT} trades / 7 days")
    print(f"    - Each trade <= EUR {PER_TRADE_MAX_EUR:.0f}")
    print(f"    - Min score to fire: {MIN_SIGNAL_SCORE}")
    print(f"    - Watchlist: {','.join(WATCHLIST)}")
    print(f"    - Auto-trader starts DISABLED. Click ENABLE in dashboard.")
    print("=" * 60)
    print()
    if not T212_API_KEY:
        print("⚠️  WARNING: T212_API_KEY is not set in .env")
        print("   The dashboard will load but API calls will fail.")
        print()

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")