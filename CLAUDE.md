# RaanuTradingBot — Project Context for Claude
> Paste this file at the start of every new Claude chat to restore full context.
> Last updated: August 2026

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
├── profit_monitor.py      ← Exit engine (ATR-scaled stop / trailing stop)
├── strategy2.py           ← S2 breakout engine (Minervini stage-2 / VCP)
├── backtest.py            ← Walk-forward backtester (signal cache + fast sim)
├── kelly.py               ← Kelly Criterion position sizing (Quarter Kelly)
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
on the first rule that fires.

**Stops are ATR-scaled, not fixed percentages.** A post-mortem of 107 live
round-trips found the old fixed -3% stop was *smaller than the median daily
true range* of the stocks being traded (3.39%). Consequences measured live:
70% of all exits were stop-outs, median hold was 2.1 days on a multi-week swing
strategy, and **0 of 50 losing trades ever reached +5%** — the trailing stop had
never once armed. A stop inside one day's normal range exits on noise rather
than on the thesis failing.

| Rule | Default | Behaviour |
|------|---------|-----------|
| Stop-loss | `STOP_ATR_MULT_S1=2.5`, `STOP_ATR_MULT_S2=3.0` | Distance = multiple × ATR(14) at entry, frozen for the life of the trade |
| Stop floor / ceiling | 1.5% / 25% | `STOP_MIN_PCT` stops quiet ETFs exiting inside the spread; `STOP_MAX_PCT` caps extreme-ATR names |
| Trailing stop | arms at +2.0×ATR, trails 1.5×ATR | `TRAIL_ACTIVATE_ATR` / `TRAIL_ATR_MULT` |
| Trail floors | give-back ≥3%, arm ≥2.5% | `TRAIL_MIN_PCT` / `TRAIL_ACTIVATE_MIN_PCT` — **required**, see below |
| Profit ladder | `5:2,10:6,15:11,20:15,30:24` | Once PEAK gain hits X%, never give back below +Y% (`PROFIT_LADDER`) |
| Hard take-profit | disabled (0) | Optional ceiling backstop (`HARD_TAKE_PROFIT_PCT`) |
| Daily crash | -8% | Single-session drop from previous close (`DAILY_CRASH_PCT`) |

**The trail floors are not optional.** Without them a 0.10%-ATR instrument
(ARB, a merger-arb ETF) arms its trail at +0.20% and exits on a 0.15%
give-back — it closed a live position on noise for +0.49%. Floors apply to the
trail for exactly the same reason `STOP_MIN_PCT` applies to the stop.

**Profit ladder** ratchets a floor upward as the trade runs: peak +10% locks in
at least +6%, peak +20% locks +15%, and the floor never falls. Backtested on S2
it improved return +13.26% → **+15.55%** and win rate 62.8% → 65.7%. It must run
*alongside* the trailing stop — ladder-only (trail off) tested at **-9.35%**.

**Exits only run while the market is open.** When closed, `current_price` is the
last close, so a trail can fire on a move that already happened; the resulting
order queues instead of filling, leaving the position open so the next poll
fires again. Five-minute polling would submit close orders all weekend.
`_symbols_with_pending_sell()` is a second guard against stacking exit orders.

Set `STOP_MODE=pct` / `TRAIL_MODE=pct` to restore fixed-percentage behaviour.
The multiple is per-strategy — breakouts (S2) need more room to hold a retest
than pullbacks (S1).

Per-position state persists to `position_peaks.json` as
`{symbol: {peak, atr}}` — old bare-float files are upgraded on load. Exits are
now recorded to `trades_log.json` as `SELL` entries with realized P&L, which is
what feeds `kelly.py`. Wired into the FastAPI lifespan (`server.py`).

---

## 📐 Position Sizing — kelly.py

    f* = (b·p − q) / b        p = win rate, q = 1−p, b = avg win / avg loss

Sizing is **risk-based**, not equal-dollar: position size is set so the loss
*at the stop* equals a fixed share of equity. This is what makes ATR stops
coherent — a wide-stop volatile name automatically gets a small position.

    qty = (equity × risk_pct / 100) / (entry − stop)

- `KELLY_FRACTION=0.25` — Quarter Kelly. Full Kelly assumes `p` and `b` are
  exact; they never are.
- **Negative f\* returns 0 risk** — stand aside, do not size down.
- Below `KELLY_MIN_SAMPLE=30` closed trades, falls back to
  `KELLY_FALLBACK_RISK_PCT=0.5`.
- Reads **only** the trade log, never the full Alpaca fill history — older
  round-trips were taken under the broken 3% stop and describe a different
  P&L distribution entirely.
- `MAX_POSITION_PCT=10` caps any single position; `PER_TRADE_MAX_USD` is a
  further hard cap and **will override risk sizing if set too low** (the server
  logs a warning whenever it binds).

---

## 🧪 Backtester — backtest.py

Two phases, because entry signals don't depend on exit rules:

1. `build_signals()` — walks the calendar scoring every ticker with the **same**
   functions the live bot uses, on history slices ending that day. Cached to
   `backtest_reports/`.
2. `simulate()` — replays cached signals through a portfolio under one exit
   config. Cheap, so parameter sweeps take seconds.

No lookahead: signals from bars up to day *i* fill at day *i+1*'s **open**.
Stops check the day's Low, trailing peaks use the High, and a stop wins ties.

```bash
python3 backtest.py --strategy s2 --years 3 --sweep-atr     # compare stop rules
python3 backtest.py --strategy s2 --years 3 --robustness    # first vs second half
```

### ⚠️ Backtest findings (3y, 472 tickers, Aug 2023 – Aug 2026)

**Control: SPY buy & hold returned +78.82% (CAGR +21.38%, maxDD 18.76%).**

| Stop rule | S1 | S2 |
|-----------|----|----|
| 3% fixed (the old live config) | **-32.10%** | +5.86% |
| 8% fixed | +10.51% | +39.46% |
| 2.5× ATR | +6.38% | +7.54% |
| 3.0× ATR | +0.72% | +13.92% |

Two conclusions that must not be lost:

1. **Wider stops are a large, robust improvement** — the direction holds across
   both strategies, both universes, fixed and ATR. This is the trustworthy part.
2. **No configuration beat holding the index, and the edge is not stable.** The
   `--robustness` split shows *every* stop setting profitable in the first half
   and negative in the second — while SPY rose +37% then +30%. S2's headline
   +39% came almost entirely from the first 18 months. The ATR work makes the
   machinery sound; it does **not** establish that either strategy has an edge.

---

## 🔭 Scanner — scanner.py
**Strategy-driven, not brute-force.** The scanner screens a single curated,
liquid **quality universe** (`FALLBACK_UNIVERSE`, **472** tickers) and only
surfaces stocks that pass our strategy — those
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

### Universe composition (472)
- ~395 large/mid-cap quality names + sector ETFs (the original list)
- **+77 high-beta / momentum names** — crypto miners (HIVE, MARA, RIOT, CLSK,
  CIFR, WULF, IREN, CORZ, BTDR, MSTR), AI/quantum (BBAI, IONQ, RGTI, QBTS,
  SOUN, TEM, NBIS, CRWV), space/eVTOL (RKLB, ASTS, LUNR, ACHR, JOBY), nuclear
  (OKLO, SMR, VST), semis (SMCI, ARM, ALAB, CRDO), plus top-100 gaps (INTU,
  TMO, DHR, PGR, TJX, UNP, CB, ANET, APH).
- Every addition was verified `tradable` **and** `fractionable` on Alpaca.

⚠️ These run a median daily ATR of ~6.5% vs ~4.7% for the original list, and 53
of the 77 exceed 5%. They are only viable with ATR-scaled stops and risk-based
sizing. In backtest the expansion **helped** under a 2.5×ATR stop (+6.38% vs
-0.04%) and **hurt** under the old 3% stop (-32.10% vs -22.19%). Do not revert
to fixed-percentage stops while these are in the universe.

**Ticker names:** `get_ticker_name()` bulk-loads symbol → company name from
Alpaca `/v2/assets` (cached). It previously read `_universe_names`, which was
only populated as a side effect of `get_universe()` — no longer called — so
every ticker outside the hand-written `TICKER_NAMES` dict rendered as a bare
symbol.

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
PER_TRADE_MAX_USD=2500      # hard cap; overrides Kelly risk sizing when it binds
MIN_SIGNAL_SCORE=60
SCAN_INTERVAL_SEC=1800
PROFIT_CHECK_SEC=300

# Exit rules — ATR-scaled (see Exit Engine section)
STOP_MODE=atr               # atr | pct
STOP_ATR_MULT_S1=2.5        # S1 pullbacks
STOP_ATR_MULT_S2=3.0        # S2 breakouts need more room
STOP_ATR_MULT=2.5           # fallback for untagged positions
STOP_MIN_PCT=1.5            # floor — quiet ETFs would stop inside the spread
STOP_MAX_PCT=25.0           # ceiling for extreme-ATR names
STOP_LOSS_PCT=3.0           # only used when STOP_MODE=pct
TRAIL_MODE=atr              # atr | pct | off
TRAIL_ACTIVATE_ATR=2.0      # arm the trail once up this many ATR
TRAIL_ATR_MULT=1.5          # exit on giving back this many ATR from peak
TRAIL_ACTIVATE_PCT=5.0      # used when TRAIL_MODE=pct
TRAIL_PCT=2.5               # used when TRAIL_MODE=pct
HARD_TAKE_PROFIT_PCT=0      # Optional hard TP ceiling; 0 = disabled (let the trail ride)
TAKE_PROFIT_PCT=5.0         # Legacy — now only the fallback for TRAIL_ACTIVATE_PCT
DAILY_CRASH_PCT=8.0         # exit on a single-session drop this large

# Position sizing (kelly.py)
KELLY_FRACTION=0.25         # Quarter Kelly
KELLY_MIN_SAMPLE=30         # below this, use the fallback risk
KELLY_FALLBACK_RISK_PCT=0.5 # % of equity risked while the sample is thin
KELLY_MAX_RISK_PCT=2.0      # hard ceiling on per-trade risk
MAX_POSITION_PCT=10.0       # cap any single position at this % of equity
ALPACA_DATA_FEED=iex        # iex | sip
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
- [x] Avoid re-buying already-held stocks **and stocks with a queued buy order**
- [x] ATR-scaled stops + trailing stops, per strategy
- [x] Kelly risk-based position sizing (Quarter Kelly)
- [x] Market hours gate on **both** the auto-trader and the scheduled path
- [x] Weekend skip in the scheduled trade loop
- [x] Weekly trade limit enforced on the scheduled path
- [x] Force/test mode: `scan-now?force=true` → 5-stock scan, bypass market hours
- [x] Delisted/stalled tickers auto-skipped (empty DataFrame + 8s timeout)
- [x] Live Signals Execute button (calls `/api/orders/buy` with notional)
- [x] Trade log persisted to `trades_log.json` — now records SELLs with realized P&L
- [x] Walk-forward backtester with stop-rule sweeps and a both-halves stability check

## 🔲 Pending / Next Steps
- [ ] **Find an edge.** No strategy currently beats SPY buy & hold in backtest,
      and the apparent S2 edge does not survive the first/second-half split.
      Everything else is secondary to this.
- [ ] `PER_TRADE_MAX_USD=2500` still binds on every candidate, so per-trade risk
      is compressed but not equalised — needs ~$10k for `MAX_POSITION_PCT` to
      become the real limit
- [ ] Dashboard sell button for open positions
- [ ] News sentiment scoring (currently no real data source)
- [ ] Mobile responsive layout

---

## 🐛 Known Issues / Gotchas

### Bugs fixed Aug 2026 — do not reintroduce
- **`/account` has no `unrealized_pl` field.** It exists only per position.
  `acct.get("unrealized_pl", 0)` silently returned 0, so the dashboard showed a
  flat $0.00 open P&L forever. Sum it across `/positions`.
- **Alpaca `/v2/stocks/{sym}/bars` returns `{"bars": null}` without an explicit
  `start`** — no error, just empty. This silently disabled the daily-crash rule
  from the day it was written.
- **`batch_download()` with a single ticker** flattened the MultiIndex on the
  ticker level, producing five columns all named e.g. `SPY` and losing `Close`.
- **Alpaca only debits `cash` on fill**, not on order submission. Orders queued
  outside market hours sit in `accepted` for hours, so raw `cash` overstates
  what is free. `get_free_cash()` subtracts open buy orders.
- **Position-only "already held" checks cause duplicate orders.** A queued order
  creates no position, so every later scan re-bought the same ticker (TENB was
  ordered 4× in one day). `get_held_symbols()` includes open buy orders.
- The scheduled trade path had **no market-hours gate and no weekly-limit
  check** — only `run_one_cycle()` did. Both now apply to both paths.

### General
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
