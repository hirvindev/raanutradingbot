# RaanuTradingBot — Setup Guide

## What you have

- `RaanuTradingBot.html` — the dashboard
- `server.py` — local Python backend that talks to Trade 212
- `start.bat` — one-click launcher (Windows)
- `.env` — your API key goes here (never share this file)
- `requirements.txt` — Python dependencies

## One-time setup

### 1. Install Python (if you don't have it)

Download from https://python.org/downloads (3.10 or newer).

**Important:** During install, tick the box that says *"Add Python to PATH"*.

To check it worked, open Command Prompt and run:
```
python --version
```

### 2. Get a Trade 212 API key

Trade 212 doesn't enable API access by default. You have to ask for it.

1. Open the Trade 212 mobile app or web app
2. Go to **Settings → API (Beta)**
3. If you see a "Generate API Key" button, click it. Choose **Practice** account first.
4. If the API option is missing, email **info@trading212.com** asking for API access on your Practice account. They typically approve within a day.
5. Copy the key (long string starting with random characters).

### 3. Add the key to `.env`

Open the `.env` file in Notepad. Replace the empty value:

```
T212_API_KEY=paste-your-key-here
T212_MODE=demo
```

Save and close. Keep `T212_MODE=demo` until you have tested everything.

## Running it

Double-click `start.bat`.

The first time it runs, it will install Python packages (takes a minute). After that, every launch is instant. Your default browser will open to `http://localhost:8000` automatically.

You should see in the top-right corner: **T212 DEMO LIVE** with a green dot.

If you see anything else, check the Command Prompt window that opened — error messages there explain what's wrong.

## What works right now

- ✓ Real Trade 212 connection (Practice account)
- ✓ Live account balance and free funds
- ✓ Live open positions with real P&L
- ✓ Real BUY orders via the dashboard buttons (with confirmation prompt)
- ✓ Real SELL/Close position orders
- ✓ Position list refreshes every 30 seconds

## What's still mock data (next steps)

- The equity curve chart (still shows simulated history)
- Trade history table (mock — needs to read from `/api/history/orders`)
- News sentiment and AI reasoning (no news API connected yet)
- Live signals / RSI / MACD scores (no market data feed connected yet)
- The "auto-trading" engine (it logs fake events — no real strategy is running)

## Going from manual to automated

Right now, BUY/SELL only fire when you click the button. To run autonomously, the next pieces needed are:

1. A market data source (Alpha Vantage / Polygon.io free tier) to get real OHLC candles
2. Indicator calculations on real data (Python `pandas-ta` library)
3. A strategy loop that checks signals every N minutes
4. Stop-loss watcher that auto-closes positions at -5%

I can build any of these next. Tell me which to tackle first.

## Important warnings

- **The dashboard targets +4-5% per month.** Be aware that compounded, that's 60-80% per year. Renaissance Medallion, the most successful quant fund ever, averages ~66% gross. Most retail algo traders lose money. The number is aspirational, not a guarantee.
- **Always trade in DEMO first.** Run for at least a month and verify it makes money on paper before switching `T212_MODE=live`.
- **Never share your `.env` file.** It contains your API key. Anyone with that key can place orders on your account.
- **T212 ticker format.** Tickers in T212 use codes like `AAPL_US_EQ`, not just `AAPL`. The dashboard's mock watchlist uses short tickers — when you place a real order, you may need the full T212 ticker. Use `/api/instruments` (visit `http://localhost:8000/api/instruments` after start) to find the exact ticker for the stock you want.
