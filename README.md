# RaanuTradingBot — Project Context for Claude
> Paste this file at the start of every new Claude chat to restore full context.
> Last updated: May 2026

---

## 📁 Project Overview
**Name:** RaanuTradingBot  
**Goal:** Algorithmic trading bot connected to Trade 212 paper (demo) account  
**Target:** +4–5% monthly returns, max 5% stop loss  
**Owner:** Archana Arjunraj (GitHub: Prakash Rajamani)  
**GitHub:** https://github.com/raanutradingbot/raanutradingbot  
**Local URL:** http://localhost:8000  
**Public URL:**https://raanutradingbot.vercel.app
---

## 🗂 File Structure
```
C:\Users\Archana Arjunraj\OneDrive\Desktop\Algo Trading\
├── server.py                  ← FastAPI backend (uvicorn, port 8000)
├── auto_trader.py             ← Auto-trading engine (scan loop, order logic)
├── strategy.py                ← Indicator engine (RSI, MACD, BB, EMA)
├── RaanuTradingBot.html       ← Main dashboard (served at localhost:8000)
├── trade212-algo-dashboard.html ← Same dashboard (served at localhost:8000/algo)
├── START.bat                  ← Double-click to start server + ngrok together
├── SETUP_AND_START.bat        ← One-time setup script
├── cloudflared.exe            ← Cloudflare tunnel (installed as Windows service)
├── .env                       ← API keys (NOT in GitHub)
├── .gitignore                 ← Excludes .env, __pycache__, *.pyc
└── CLAUDE.md                  ← This file
```

---

## ⚙️ Tech Stack
| Layer | Technology |
|-------|-----------|
| Backend | Python, FastAPI, uvicorn |
| Frontend | Vanilla HTML/CSS/JS, Chart.js 4.4.1 |
| Trading API | Trade 212 REST API v0 |
| Price Data | Yahoo Finance via allorigins CORS proxy |
| Fonts | Inter + IBM Plex Mono (Google Fonts) |
| Version Control | Git + GitHub |
| Public Access | ngrok (free plan) |
| Tunnel Service | Cloudflare Tunnel (installed as Windows service) |

---

## 🚀 How to Start
**Double-click `START.bat`** — starts both server and ngrok automatically.

Or manually:

**Window 1 — Server:**
```
cd "C:\Users\Archana Arjunraj\OneDrive\Desktop\Algo Trading"
python server.py
```

**Window 2 — ngrok:**
```
ngrok http 8000
```

Then open: **http://localhost:8000**  
Public URL shown in ngrok window (changes each restart on free plan)

---

## 🔌 Server — server.py
- **Framework:** FastAPI + uvicorn (NOT Flask)
- **Port:** 8000
- **Host:** 0.0.0.0 (accessible via ngrok/Cloudflare)
- **Dashboard route:** `GET /` → serves `RaanuTradingBot.html`
- **AlgoDash route:** `GET /algo` → serves `trade212-algo-dashboard.html`
- **CORS:** Enabled for all origins
- **Auto-loads API key:** `/api/config` endpoint sends key to frontend on load

### Key API Endpoints
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/config` | Returns API key + mode to frontend (auto-connect) |
| GET | `/api/health` | Server health + mode |
| GET | `/api/account/cash` | Cash, free funds, total |
| GET | `/api/account/info` | Account info |
| GET | `/api/portfolio` | Open positions + PPL |
| GET | `/api/orders` | Pending orders |
| GET | `/api/history/orders?limit=20` | Order history |
| POST | `/api/orders/buy` | Place market buy |
| POST | `/api/orders/sell` | Place market sell |
| DELETE | `/api/orders/{id}` | Cancel order |
| GET | `/api/auto/status` | Auto-trader status |
| POST | `/api/auto/start` | Enable auto-trader |
| POST | `/api/auto/stop` | Disable auto-trader |
| POST | `/api/auto/scan-now` | Force immediate scan |
| GET | `/api/auto/scan-preview` | Score watchlist (no trades) |

---

## 🤖 Auto Trader — auto_trader.py
- **Scan interval:** 1800s (30 min)
- **Starts:** DISABLED — must click ENABLE in dashboard
- **Weekly trade limit:** 2 trades per 7 days
- **Per trade max:** €500
- **Min signal score:** 60/100
- **Watchlist:** AAPL, MSFT, NVDA, GOOGL, AMZN, META, AMD, TSLA

---

## 📊 Strategy Engine — strategy.py
Indicators computed locally from Yahoo Finance price data:

| Indicator | Params | Signal Range |
|-----------|--------|-------------|
| RSI | 14 periods | <25 Strong Buy → >75 Strong Sell |
| MACD | 12, 26, 9 | Bullish/Bearish crossover + histogram |
| Bollinger Bands | 20 periods, 2σ | At lower band (Buy) → At upper (Sell) |
| EMA | 50 + 200 | Golden cross → Death cross |
| ATR | 14 periods | Volatility range only |
| Volume Ratio | vs 20d avg | Spike detection |

### Composite Score Weights (configurable in dashboard)
| Indicator | Default Weight |
|-----------|---------------|
| RSI | 25% |
| MACD | 25% |
| Bollinger Bands | 20% |
| EMA 50/200 | 15% |
| News Sentiment | 10% |
| Fundamentals | 5% |

**Min score to execute trade:** 70/100 (dashboard) / 60/100 (auto_trader)

---

## 🎨 Dashboard — RaanuTradingBot.html
Single-file HTML dashboard. No build step required.

### Sections
1. **Overview** — 5 metric cards + 4 secondary cards + equity chart + composite signal ring + recent trades
2. **Portfolio** — Account summary + monthly progress bar + open positions table
3. **Orders** — Full order history with filter
4. **Live Signals** — Watchlist scanner with all indicator columns + AI reasoning cards
5. **Indicators** — Single symbol deep analysis with price+EMA chart + MACD histogram
6. **Strategy** — Weight sliders + risk parameters + execution toggles + watchlist manager
7. **Manual Trade** — Order form + preview + quick-close positions
8. **Engine Logs** — Filterable log (All/Trades/Warnings/Errors/API)

### Metric Cards (all from T212 API — auto-loads, no manual config)
| Card | Source | Color |
|------|--------|-------|
| Portfolio Value | `/api/account/cash` → totalValue | Blue |
| Open P&L | Sum of ppl across positions | Teal |
| Win Rate | Derived from order history pairs | Green |
| Avg Trade P&L | Mean P&L across closed trades | Amber |
| Stop Loss Hits | Loss-side sells this month | Red |

### Design System
- **Background:** `#0d0f12` (darkest) → `#1a1e26` (cards)
- **Accent:** `#00c896` (teal-green)
- **Green:** `#22c55e` | **Red:** `#f43f5e` | **Warn:** `#f59e0b`
- **Fonts:** Inter (UI) + IBM Plex Mono (data/labels)
- **Border:** `rgba(255,255,255,0.06)`

### Auto-Connect Feature
Dashboard calls `/api/config` on load → gets API key from backend → connects automatically.
No manual API key entry needed ever again.

---

## 🔑 Environment Variables — .env
```
T212_API_KEY=your_key_here
T212_MODE=demo
```
- `T212_MODE` must be `demo` or `live` (NOT `practice`)
- `.env` is excluded from GitHub via `.gitignore`

---

## 🌐 Trade 212 API
| Mode | Base URL |
|------|----------|
| Demo/Paper | `https://demo.trading212.com/api/v0` |
| Live | `https://live.trading212.com/api/v0` |

**Auth:** `Authorization: <api_key>` header  
**Get API key:** T212 app → Settings → API (Beta)

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
```

Install all:
```
pip install fastapi uvicorn httpx python-dotenv pydantic numpy pandas yfinance ta
```

---

## 🗃 Git Workflow
```bash
# Save and push changes
git add .
git commit -m "describe change"
git push

# Pull latest (other machine)
git pull
```
**Branch:** main  
**Remote:** https://github.com/raanutradingbot/raanutradingbot.git

---

## ✅ What's Working
- [x] FastAPI server running on port 8000
- [x] Trade 212 demo API connected
- [x] Dashboard served at localhost:8000
- [x] AlgoDash served at localhost:8000/algo
- [x] Indicator engine (RSI, MACD, BB, EMA, ATR)
- [x] Composite signal scoring (weighted)
- [x] Auto-trader scan loop (disabled by default)
- [x] Real T212 metrics (portfolio value, cash, positions, orders)
- [x] Win rate + avg trade derived from order history
- [x] Stop loss auto-set after every buy
- [x] Watchlist scanner with signal table
- [x] Manual trade form
- [x] GitHub repo connected (main branch)
- [x] Auto-load API key from backend (no manual config)
- [x] ngrok public URL working
- [x] Cloudflare tunnel installed as Windows service
- [x] START.bat — double-click to launch everything

## 🔲 Pending / Next Steps
- [ ] Claude AI integration for news sentiment scoring
- [ ] NewsAPI integration for live headlines
- [ ] Backtesting module
- [ ] Real fundamentals data (P/E, EPS)
- [ ] Mobile responsive layout
- [ ] Push notifications / alerts
- [ ] Monthly P&L tracking with real account history
- [ ] Fix T212_MODE warning (change .env from 'practice' to 'demo')
- [ ] Permanent ngrok URL (upgrade to paid or use static domain)

---

## 🐛 Known Issues
- `T212_MODE=practice` in .env causes warning — change to `demo`
- ngrok URL changes on every restart (free plan limitation)
- Yahoo Finance CORS proxy occasionally slow — falls back to simulated prices
- News sentiment is random placeholder — needs real NewsAPI integration
- Fundamentals score is placeholder — needs real data source
- ngrok shows "Visit Site" warning on first open (normal for free plan)

---

## 💬 How to Use This File
At the start of a new Claude chat, paste this file and say:
> "Here is my CLAUDE.md project context. Let's continue building RaanuTradingBot."

Claude will have full context of the codebase, stack, goals and progress.
