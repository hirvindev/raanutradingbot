# RaanuTradingBot — Project Context for Claude
> Paste this file at the start of every new Claude chat to restore full context.
> Last updated: May 2026

---

## 📁 Project Overview
**Name:** RaanuTradingBot  
**Goal:** Algorithmic trading bot connected to Alpaca paper trading account  
**Target:** +4–5% monthly returns, max 5% stop loss  
**Owner:** Archana Arjunraj (dev: Prakash Rajamani)  
**Local URL:** http://localhost:8000  
**Platform:** macOS (python3, not python)

---

## 🗂 File Structure
```
/Users/prakash.rajamani/raanutradingbot/
├── server.py              ← FastAPI backend (uvicorn, port 8000)
├── auto_trader.py         ← Auto-trading engine (5-gate scan loop + order logic)
├── strategy.py            ← Indicator engine (RSI, MACD, BB, EMA) + batch_download
├── scanner.py             ← Momentum scanner (42 US-listed tickers, batch yfinance)
├── alpaca_data.py         ← Alpaca market data helper (skips non-US tickers)
├── notifier.py            ← Twilio WhatsApp alerts (pre-trade + post-trade)
├── profit_monitor.py      ← Take-profit / stop-loss monitor
├── RaanuTradingBot.html   ← Main dashboard (single-file, served at localhost:8000)
├── start.sh               ← Start server on Mac
├── setup.sh               ← One-time Mac setup
├── requirements.txt       ← Python dependencies
├── .env                   ← API keys (NOT in GitHub)
├── .gitignore             ← Excludes .env, __pycache__, *.pyc
└── CLAUDE.md              ← This file
```

---

## ⚙️ Tech Stack
| Layer | Technology |
|-------|-----------|
| Backend | Python 3, FastAPI, uvicorn |
| Frontend | Vanilla HTML/CSS/JS, Chart.js 4.4.1 |
| Broker | Alpaca paper trading REST API v2 |
| Price Data | Yahoo Finance via yfinance (batch download) |
| Notifications | Twilio WhatsApp API |
| Fonts | Inter + IBM Plex Mono (Google Fonts) |
| Version Control | Git + GitHub |

---

## 🚀 How to Start (macOS)
```bash
python3 server.py
```
Or: `./start.sh`

Then open: **http://localhost:8000**

---

## 🔌 Server — server.py
- **Framework:** FastAPI + uvicorn (NOT Flask)
- **Port:** 8000
- **Host:** 0.0.0.0
- **Dashboard route:** `GET /` → serves `RaanuTradingBot.html`
- **CORS:** Enabled for all origins

### Key API Endpoints
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Server health + mode |
| GET | `/api/account/cash` | Cash + buying power |
| GET | `/api/portfolio` | Open positions + P&L |
| GET | `/api/orders` | Pending orders |
| POST | `/api/orders/buy` | Place market buy (notional) |
| POST | `/api/orders/sell` | Close a position |
| GET | `/api/auto/status` | Auto-trader status |
| POST | `/api/auto/start` | Enable auto-trader |
| POST | `/api/auto/stop` | Disable auto-trader |
| POST | `/api/auto/scan-now?force=true` | Force scan (5 stocks, bypass market hours) |
| POST | `/api/auto/scan-now` | Normal scan + trade if enabled |
| GET | `/api/auto/picks` | Cached last scan results |

---

## 🤖 Auto Trader — auto_trader.py
- **Scan interval:** 1800s (30 min)
- **Starts:** DISABLED — must POST `/api/auto/start` or click ENABLE
- **Weekly trade limit:** 2 trades per 7 days (configurable via .env)
- **Per trade max:** $500 USD (configurable via .env)
- **Min signal score:** 60/100
- **Position sizing:** min($500, 5% of free cash)

### 5-Gate System (all must pass before order is placed)
1. Auto-trader is enabled
2. Market is open (Alpaca clock endpoint) — bypassed when `force=true`
3. Weekly trade limit not reached
4. Free cash available (Alpaca account endpoint)
5. Stock not already held (Alpaca positions endpoint)

### WhatsApp Alerts (via notifier.py)
- **Pre-trade alert** sent BEFORE placing order (gives time to cancel)
- **Post-trade alert** sent AFTER order confirmed
- Uses Twilio Sandbox WhatsApp

---

## 📊 Strategy Engine — strategy.py
**Trend + momentum model (pullback-in-uptrend).** Indicators computed locally
from Yahoo Finance daily OHLCV. The engine only surfaces stocks that can
actually make money — it does NOT buy falling knives.

**HARD UPTREND GATE:** a stock must be in a confirmed uptrend
(`price > EMA200` AND `EMA50 > EMA200` AND `EMA50 rising`) or its score is
capped at `UPTREND_SCORE_CAP` (45) so it can never reach the actionable
threshold. Only `uptrend == true` stocks become picks.

Among uptrending stocks, scoring favours a **healthy pullback entry** (price
dipped toward the rising 20-EMA, RSI 40–60) over extended/overbought names.

| Component | Max | Notes |
|-----------|----:|-------|
| Trend structure | 20 | confirmed uptrend + price above EMA50 |
| Momentum (3M ROC) | 15 | scaled by 3-month return |
| Relative strength vs SPY | 12 | only rewards stocks **beating the market** |
| MACD regime | 10 | bullish + fresh crossover bonus |
| Pullback quality (RSI zone) | 15 | RSI 40–55 ideal; >72 penalised (chasing) |
| Proximity to 20-EMA | 10 | pulled-back-to-support bonus; >12% above = chasing penalty |
| Volume / structure | 10 | up-day on above-avg volume, holding above weekly low |
| Fib golden pocket (SMC) | 12 | price retraced into 0.618–0.786 zone of last up-impulse, turning up |

**Score range:** 0–100. Score ≥ 60 **and** `uptrend == true` = actionable BUY.

**Golden-pocket layer (SMC structure):** `golden_pocket()` finds the last
up-impulse (fractal swing low → higher swing high) and the Fibonacci
retracement the price sits in. Inside an uptrend, price sitting in the
0.618–0.786 "golden pocket" and turning up is the highest-conviction pullback
entry — an original implementation of public Fibonacci/Smart-Money concepts
(NOT a copy of any invite-only indicator). Only applied when `uptrend == true`.

### Key Functions
- `score_ticker(ticker, bench_ret_3m=None)` — download + score one ticker
- `score_from_df(ticker, df, bench_ret_3m=None)` — score from pre-fetched DataFrame (fast path)
- `benchmark_return_3m()` — SPY 3-month return, the relative-strength benchmark (fetched once per scan)
- `golden_pocket(high, low, close)` — Fib retracement zone of the last swing impulse
- `_score_core(...)` — shared scoring logic behind both entry points
- `batch_download(tickers)` — one yfinance call for all tickers, returns `{ticker: df}`
  - Uses `timeout=8` so a stalled ticker fails fast
  - Delisted/unavailable tickers return empty DataFrames and are silently skipped

Each result dict now also carries: `uptrend` (bool), `mom_1m`, `mom_3m`,
`rel_strength`, `ema20`, `in_golden_pocket` (bool), `swing_high`, `swing_low`, `fib_retrace`.

---

## 🚪 Exit Engine — profit_monitor.py

Polls open Alpaca positions every `PROFIT_CHECK_SEC` (default 300s) and closes
on the first rule that fires. A **trailing stop** replaced the old fixed +5%
take-profit so winners can ride the trend:

| Rule | Default | Behaviour |
|------|---------|-----------|
| Hard stop-loss | -3% | Cut losers fast (`STOP_LOSS_PCT`) |
| Trailing stop | arms at +5%, trails 2.5% | Track peak once up `TRAIL_ACTIVATE_PCT`; exit if price gives back `TRAIL_PCT` from peak |
| Hard take-profit | disabled (0) | Optional ceiling backstop (`HARD_TAKE_PROFIT_PCT`) |

Per-position peak prices persist to `position_peaks.json` so the trail keeps
its high-water mark across restarts. Wired into the FastAPI lifespan
(`server.py`) — runs continuously alongside the scheduled-trade loop.

---

## 🔭 Scanner — scanner.py
**Strategy-driven, not brute-force.** The scanner screens a single curated,
liquid **quality universe** (`FALLBACK_UNIVERSE`, ~400 large/mid-cap US
equities + sector ETFs) and only surfaces stocks that pass our strategy — those
in a **confirmed uptrend**. It does NOT scan the entire Alpaca universe
(thousands of illiquid names the strategy would never trade); a big scan is
unnecessary when only uptrend pullbacks are ever bought.

- `find_top_picks(n)` — auto-trader path. Batch-scores the curated universe,
  keeps only `uptrend == true AND score >= 60`, returns the top `n`.
- `/api/scan/stream` (browser Live Signals) — screens the **same** curated
  universe and streams **only confirmed-uptrend candidates** (emits a lightweight
  `progress` tick per 25 tickers so the bar advances; non-uptrend names are
  scored but never emitted).
- Both compute the SPY relative-strength benchmark once per scan.

**TEST_UNIVERSE** (used when `force=true`): AAPL, NVDA, MSFT, GOOGL, META

**Performance:** curated universe scans in a few seconds (batched yfinance,
`CHUNK_SIZE=250`). `get_universe()` (full Alpaca list) still exists but is no
longer used for scanning — kept only for reference.

**Note:** All tickers are US-listed and directly executable on Alpaca.

---

## 🎨 Dashboard — RaanuTradingBot.html
Single-file HTML dashboard. No build step required.

### Active Sections
1. **Overview** — Account cards (portfolio value, P&L, win rate) + equity chart + recent trades
2. **Portfolio** — Open positions table
3. **Orders** — Order history with filter
4. **Live Signals** — Batch scanner with score table + Execute button per row
5. **Auto Trader** — Enable/disable, scan-now, status, event log
6. **Engine Logs** — Filterable log

### Removed Sections (intentionally)
- Indicators tab (single-symbol deep analysis) — removed as unused
- Manual Trade tab — removed as unused

### Design System
- **Background:** `#0d0f12` (darkest) → `#1a1e26` (cards)
- **Accent:** `#00c896` (teal-green)
- **Green:** `#22c55e` | **Red:** `#f43f5e` | **Warn:** `#f59e0b`
- **Fonts:** Inter (UI) + IBM Plex Mono (data/labels)

---

## 🔑 Environment Variables — .env
```
# Alpaca paper trading
ALPACA_API_KEY=PK5V3WKKLEQUUPQQ6YBZHTCYMM
ALPACA_SECRET_KEY=<secret>
ALPACA_MODE=paper

# Twilio WhatsApp alerts
TWILIO_ACCOUNT_SID=<your-twilio-account-sid>
TWILIO_AUTH_TOKEN=<secret>
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
USER_WHATSAPP=whatsapp:+919176911755

# Trading parameters
WEEKLY_TRADE_LIMIT=2
PER_TRADE_MAX_USD=500
MIN_SIGNAL_SCORE=60
SCAN_INTERVAL_SEC=1800
STOP_LOSS_PCT=3.0            # Hard stop-loss (cut losers)
TRAIL_ACTIVATE_PCT=5.0      # Arm the trailing stop once up this % (defaults to TAKE_PROFIT_PCT)
TRAIL_PCT=2.5               # Exit if price gives back this % from its peak
HARD_TAKE_PROFIT_PCT=0      # Optional hard TP ceiling; 0 = disabled (let the trail ride)
TAKE_PROFIT_PCT=5.0         # Legacy — now only the fallback for TRAIL_ACTIVATE_PCT
PROFIT_CHECK_SEC=300
```

---

## 🌐 Alpaca API
| Mode | Broker Base URL |
|------|----------------|
| Paper | `https://paper-api.alpaca.markets/v2` |
| Live | `https://api.alpaca.markets/v2` |

**Auth:** `APCA-API-KEY-ID` + `APCA-API-SECRET-KEY` headers  
**Data:** `https://data.alpaca.markets/v2` (IEX feed, US-only — foreign tickers return 401 and are skipped in alpaca_data.py)  
**Get API key:** alpaca.markets → Paper Trading dashboard → API Keys

---

## 📦 Python Dependencies
```
fastapi
uvicorn
httpx
python-dotenv
pydantic
numpy
pandas
yfinance
ta
twilio
python-multipart
```

Install all:
```bash
pip3 install fastapi uvicorn httpx python-dotenv pydantic numpy pandas yfinance ta twilio python-multipart
```

---

## 🗃 Git Workflow
```bash
git add .
git commit -m "describe change"
git push
```
**Branch:** main

---

## ✅ What's Working
- [x] FastAPI server on port 8000 (macOS, python3)
- [x] Alpaca paper trading connected
- [x] Dashboard at localhost:8000 (RaanuTradingBot.html)
- [x] Indicator engine (RSI, MACD, Bollinger, EMA)
- [x] Composite signal scoring (0–100, threshold 60)
- [x] Batch yfinance download (42 US tickers in ~1–3 seconds)
- [x] Auto-trader scan loop (disabled by default)
- [x] 5-gate system before every order
- [x] WhatsApp pre-trade + post-trade alerts via Twilio
- [x] Avoid re-buying already-held stocks
- [x] Position sizing (min of $500 cap and 5% of free cash)
- [x] Market hours gate (skip trades when market closed)
- [x] Force/test mode: `scan-now?force=true` → 5-stock scan, bypass market hours
- [x] Delisted/stalled tickers auto-skipped (empty DataFrame + 8s timeout)
- [x] Live Signals Execute button (calls `/api/orders/buy` with notional)
- [x] Trade log persisted to `trades_log.json` (survives restart)

## 🔲 Pending / Next Steps
- [ ] Sell/exit logic (profit monitor currently separate in profit_monitor.py)
- [ ] Dashboard sell button for open positions
- [ ] Backtesting module
- [ ] News sentiment scoring (currently no real data source)
- [ ] Mobile responsive layout
- [ ] Expand universe if market conditions keep producing 0 picks (scores < 60)

---

## 🐛 Known Issues / Gotchas
- `MBGYY` (Mercedes-Benz ADR) was timing out yfinance — removed from universe
- `QIAGF`, `BMWYY`, `DPSGY` delisted — removed from universe
- Alpaca IEX feed is US-only — alpaca_data.py returns None for any ticker with `.` in name
- When no stocks score ≥ 60, picks = 0 and no trade fires (correct behavior — don't force bad trades)
- Twilio WhatsApp sandbox requires user to opt-in first (send "join <sandbox-name>" to the number)
- macOS uses `python3` not `python`

---

## 💬 How to Use This File
At the start of a new Claude chat, paste this file and say:
> "Here is my CLAUDE.md project context. Let's continue building RaanuTradingBot."
