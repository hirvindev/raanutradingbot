# RaanuTradingBot — Project Context for Claude
> Paste this file at the start of every new Claude chat to restore full context.
> Last updated: 30 August 2026

---

## ⛔ This is a PERSONAL project

Nothing here is connected to any employer, and no employer's tooling,
credentials, accounts or conventions may be used in it.

**The global `~/.claude/CLAUDE.md` describes a Delivery Hero internal tool
(Planning Capacity Tool: React/MUI, JIRA, Google Sheets, Slack). None of it
applies here.** It loads into every session regardless, so treat it as
inapplicable background rather than instructions — this file wins on every
point of conflict.

Concretely, in this project:

- Never touch Delivery Hero credentials, service accounts, or repos. Two
  unrelated `service-account*.json` files live in `~/Downloads` and are
  **off-limits**.
- The Google account for this project is the personal one (Firebase project
  `raanubot`, Railway). The `gcloud` CLI on this machine is authenticated as
  the **work** account and therefore cannot — and must not — administer it.
- Secrets live in `~/.secrets/` (mode 700, files 600) and in Railway
  variables. Not in `~/Downloads`, not in the repo. `.env` is gitignored.

---

## 📁 Project Overview
**Name:** RaanuTradingBot  
**Goal:** Algorithmic trading bot connected to Alpaca paper trading account  
**Target:** +4–5% monthly returns, max 5% stop loss  
**Owner:** Archana Arjunraj (dev: Prakash Rajamani)  
**Local URL:** http://localhost:8000  
**Production:** AWS — https://d2c2x91kx43y5d.cloudfront.net (CloudFront -> S3 + Lambda)  
**Retired:** Railway (https://raanu.up.railway.app). The code is AWS-only now;
Railway would 501 on scanning and has no worker to run the schedule.  
**Platform:** macOS (python3, not python)

---

## 🗂 File Structure

Restructured into a package (Aug 2026). Everything used to sit flat at the
repo root; `server.py` alone was 2218 lines and ~50 routes.

```
raanu/                      ← the application package
├── config.py               ← EVERY env read, lazily. Nothing at import time.
├── paths.py                ← PROJECT_ROOT anchor for on-disk assets
├── clock.py                ← US_EAST / BERLIN / IST
├── indicators.py           ← shared indicator math (pure, no imports)
├── state/                  ← persistent JSON state
│   ├── __init__.py         ←   load / save / load_many / delete
│   └── backends.py         ←   file (local) | dynamodb (AWS)
├── market/
│   ├── prices.py           ←   yfinance OHLCV + SPY benchmark
│   ├── cache.py            ←   DAILY BARS CACHE — the main perf mechanism
│   ├── universe.py         ←   curated 472-ticker universe + names
│   ├── rest.py             ←   Alpaca REST plumbing
│   └── broker.py           ←   Alpaca market data
├── strategies/
│   ├── __init__.py         ←   REGISTRY: surfaces() vs tradable() bars
│   ├── pullback.py         ←   S1
│   ├── breakout.py         ←   S2 (Minervini stage-2 / VCP)
│   └── leader_dip.py       ←   S3 (Bollinger + MACD mean reversion)
├── scanning/
│   ├── engine.py           ←   THE one scan implementation
│   └── job.py              ←   run manifest, sharding, aggregation, stalls
├── trading/
│   ├── trader.py           ←   order executor + 5-gate system
│   ├── exits.py            ←   ATR-scaled stop / trailing stop / ladder
│   ├── sizing.py           ←   Kelly (Quarter Kelly)
│   ├── schedule.py         ←   03:30 / 09:35 / 11:00 ET slot logic
│   ├── reports.py          ←   FIFO round-trips + monthly report
│   └── picks_log.py        ←   every pick + its 1/5/20-day return vs SPY
├── notify/{telegram,push}.py
└── api/
    ├── app.py              ←   create_app() factory; loops only for local dev
    ├── auth.py             ←   the two-token gate
    └── routes/             ←   15 routers, one per domain
handlers/{api,worker}.py    ← thin Lambda entrypoints
tests/                      ← 191 tests
tools/{backtest,bench_scan}.py
RaanuTradingBot.html        ← dashboard (single file, no build step)
sw.js, manifest.webmanifest, icons/
Dockerfile.lambda           ← NOT "Dockerfile" — see the AWS section
aws/                        ← CDK app
```

**Run it locally:** `python -m raanu.api` (was `python3 server.py`).
**Tests:** `pytest`. **Lint:** `ruff check raanu/ handlers/ tests/ tools/`.

**Not in the repo, and must stay that way:** `.env` and `~/.secrets/`.

The Android keystores are no longer relevant — `mobile/` (React Native),
`twa/` (the Play wrapper) and `deploy-mobile.sh` were **deleted on 31 Aug
2026**. The dashboard is a PWA; Add to Home Screen is the phone story now.
The keystores themselves were never committed and are still in `~/.secrets/`;
keep them only if you might ever want the Play listing back.

---

## ⚙️ Tech Stack
| Layer | Technology |
|-------|-----------|
| Backend | Python 3, FastAPI, uvicorn |
| Frontend | Vanilla HTML/CSS/JS, Chart.js 4.4.1 |
| Broker | Alpaca paper trading REST API v2 |
| Price Data | Yahoo Finance via yfinance (batch download) |
| Notifications | Telegram Bot API + Web Push (VAPID) |
| Fonts | Inter + IBM Plex Mono (Google Fonts) |
| Version Control | Git + GitHub |

---

## 🚀 How to Start (macOS)
```bash
python -m raanu.api      # local dev server, background loops enabled
pytest                   # 191 tests
ruff check raanu/ handlers/ tests/ tools/
```

Then open: **http://localhost:8000**

---

## 🔌 Server — raanu/api/
- **Framework:** FastAPI + uvicorn (NOT Flask)
- **App factory:** `create_app()`. Importing builds nothing; the background
  loops start only under `create_app(with_loops=True)`, i.e. local dev. On
  Lambda, Mangum runs with `lifespan="off"` and the worker owns the schedule.
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

## 🤖 Auto Trader — raanu/trading/trader.py
- **Scans:** 03:30 ET (alert only), 09:35 ET and 11:00 ET (execute), plus one
  silent scan at server startup. There is **no periodic scan interval** — the
  old `SCAN_INTERVAL_SEC=1800` was reported by the API but no loop consumed it,
  so the dashboard advertised a 30-minute cadence that never ran. Removed.
- **Starts:** DISABLED — must POST `/api/auto/start` or click ENABLE
- **Only the 09:35/11:00 ET slots place orders.** The startup scan and the
  pre-market scan call `run_one_cycle(execute=False)` and can never trade.
  Before this, restarting the server during market hours could fire a trade.
- **Slots run every weekday.** The alternating trade/rest day rule was removed:
  it alternated on calendar-day parity while slots only run Mon–Fri, giving 3
  trade days one week and 2 the next. That silently capped the per-strategy
  weekly budgets — `WEEKLY_TRADE_LIMIT_S3=3` could never be reached, so the
  setting meant something other than what it said. **The per-strategy weekly
  limit is now the single throttle.** Do not add a second one.
- **Weekly trade limit:** per strategy — S1 2, S2 1, S3 3 per rolling 7 days
  (configurable via .env). Counts BUYs only; exits do not consume the budget.
- **Per trade max:** $500 USD (configurable via .env)
- **Min signal score:** 70/100 — the BUY gate. This used to be documented as
  60 because `/api/health` reported 60 while `auto_trader` enforced 70; the
  variable is unset on AWS so 70 was the live value. One definition now
  (`config.min_signal_score()`), and health reports that same function.
- **Position sizing:** min($500, 5% of free cash)

### 5-Gate System (all must pass before order is placed)
1. Auto-trader is enabled
2. Market is open (Alpaca clock endpoint) — bypassed when `force=true`
3. Weekly trade limit not reached
4. Free cash available (Alpaca account endpoint)
5. Stock not already held (Alpaca positions endpoint)

### Trade Alerts (via raanu/notify/telegram.py)
- **Pre-trade alert** sent BEFORE placing order (gives time to cancel)
- **Post-trade alert** sent AFTER order confirmed
- **Telegram, not Twilio.** The function is still called `send_whatsapp()`,
  but it is a one-line alias for `send_telegram()` — the name is the only
  thing left of the WhatsApp path. See the Notifications section.

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

## 🎯 Strategy 3 — strategy3.py ("Market Leader Dip")

Mean reversion inside an uptrend: buys a pullback to the **lower Bollinger
Band** in a name **beating SPY**, confirmed by the **MACD histogram turning
up**. Where S1 buys pullbacks to a rising 20-EMA and S2 buys breakouts to new
highs, S3 buys temporary weakness in leaders.

Three conditions must all hold (`leader_dip == true`):

| Gate | Test | Why |
|------|------|-----|
| LEADER | beating SPY 3M, and price > EMA200 **and** > SMA200 | mean reversion outside an uptrend is catching a falling knife |
| STRETCHED | %B ≤ 0.20 (lower fifth of the band) | unusually large deviation from its own 20-day mean |
| TURNING | MACD histogram rising | without it, price "walks the band" all the way down |

Score components: trend integrity 20, leadership/RS 22, Bollinger stretch 26,
MACD turn 24, oversold quality 12, capitulation volume 6. A name below its
200-day trend is hard-capped at `TREND_SCORE_CAP` (45) so it can never qualify.

### Backtest (3y, 472 tickers) — the best-validated strategy in the project

```
CONTROL — SPY buy & hold: +78.82%   maxDD 18.76%

  2.5x ATR stop   n=278  win 59.4%  b=0.93  ret +33.89%  maxDD 10.28%  f*=+15.8%
  3.0x ATR stop   n=269  win 63.2%  b=0.87  ret +42.23%  maxDD 11.14%  f*=+21.1%
  3% fixed stop   n=330  win 40.3%  b=1.47  ret  -1.02%  maxDD 12.96%  f*= -0.4%
```

**S3 is the only configuration that stays profitable in BOTH halves** of the
window (+23.74% then +15.10%). S1 and S2 both collapse in the second half. Its
drawdown (11.14%) is well under the index's (18.76%), though its absolute
return is not — it has never beaten buy & hold in any test.

The 8%-fixed-stop variant still fails the second half (-5.32%), so the ATR stop
is doing the work, not the Bollinger entry.

**Do not enable the profit ladder on S3** — see the Exit Engine section.

**Deliberately NOT intraday.** Every result above is from daily bars with a
median 11-day hold. Converting S3 to intraday would not tune this result, it
would discard it: %B and MACD on 5-minute bars are different signals, yfinance
only serves ~60 days of 1-minute data (so the both-halves test is impossible),
and the 300s exit poll is far too slow for minute-scale holds.

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
| Profit ladder | **per strategy** — on for S1/S2, **off for S3** | Once PEAK gain hits X%, never give back below +Y% (`PROFIT_LADDER_S1/S2/S3`) |
| Hard take-profit | disabled (0) | Optional ceiling backstop (`HARD_TAKE_PROFIT_PCT`) |
| Daily crash | -8% | Single-session drop from previous close (`DAILY_CRASH_PCT`) |

**The trail floors are not optional.** Without them a 0.10%-ATR instrument
(ARB, a merger-arb ETF) arms its trail at +0.20% and exits on a 0.15%
give-back — it closed a live position on noise for +0.49%. Floors apply to the
trail for exactly the same reason `STOP_MIN_PCT` applies to the stop.

**Profit ladder** ratchets a floor upward as the trade runs: peak +10% locks in
at least +6%, peak +20% locks +15%, and the floor never falls. It must run
*alongside* the trailing stop — ladder-only (trail off) tested at **-9.35%**.

⚠️ **The ladder is not universally good — that is why it is per-strategy:**

| | without ladder | with ladder |
|---|---|---|
| S2 breakout | +13.26% | **+15.55%** (helps) |
| S3 leader dip | **+33.89%** | +22.34% (hurts) |

On S3 the ladder lifts win rate 59.4% → 68.7% while payoff collapses
0.93 → 0.58 — it books winners before they mature. This is the clearest example
in the project of why **win rate is a misleading target**: it is trivially
raised by taking profits earlier, and you pay for every point. Expectancy
(`p × avgWin − q × avgLoss`) is the number that decides profitability. The
highest-win-rate configuration ever tested here (68.0%, 4.0×ATR on S2) *lost
money*.

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

## 📏 Risk-Adjusted Measurement — backtest.py `risk_stats()`

Total return cannot separate skill from index exposure. Every conclusion in
this file before 16 Aug 2026 was drawn without measuring that, and the file
even carried a comment at `simulate()` saying *"the improvement is just beta"*
with no code anywhere that computed beta.

`python3 backtest.py --strategy s3 --years 3 --stop-atr 3.0` now also prints:

```
CAGR         +13.48%   vs SPY +21.38%
Volatility    13.43%   vs SPY 15.36%
Beta          0.496    correlation 0.567
ALPHA         +1.39%
Sharpe         0.72    vs SPY 1.08     Sortino 1.03
Info ratio    -0.52
Exposure       99.9%   of days holding anything
```

**S3 has genuinely positive alpha (+1.39%) at half the index's beta.** That is
the first evidence in this project of anything other than levered beta — the
strategy earns slightly more than its market exposure alone would.

**And it is still worse than buy & hold.** Sharpe 0.72 against the index's
1.08: more risk paid per unit of return. Both facts are true at once, and the
alpha alone would have been read as a win. `format_risk()` therefore states the
verdict rather than leaving it to the reader.

Two things this killed immediately:

- **"S3 is under-deployed, just size it up."** Exposure is **99.9%** — it holds
  something on essentially every trading day. There is no idle capital to lever.
- **The default backtest config was testing a stop nobody chose.** `ExitConfig()`
  defaults to 2.0×ATR while the docs and live both use 2.5–3.0. At 2.0× the
  same strategy scores **alpha −10.68%**, and a bare `--strategy s3` was
  reporting that as the result. Hence `--stop-atr`.

⚠️ `risk_free_pct` defaults to **4%** (roughly T-bills over this window).
Passing 0, the common shortcut, inflates every Sharpe and would have made this
comparison look far closer than it is.

### ⚠️ `--sweep-rank`: the score does not rank

`python3 backtest.py --strategy s3 --years 3 --stop-atr 3.0 --sweep-rank`

```
 score bar  max pos  trades     CAGR   beta    ALPHA  Sharpe   maxDD
        60        4     177   +3.46%   0.27   -4.60%   -0.03  11.86%
        60        8     352  +13.48%   0.50   +1.39%    0.72  17.81%
        60       15     517  +17.27%   0.61   +3.20%    0.85  18.40%
        70       15     485  +15.80%   0.56   +2.55%    0.79  17.51%
        80        8     285   +4.63%   0.40   -5.22%    0.11  18.68%
        80       15     330   +5.54%   0.42   -4.65%    0.18  15.48%
```

Two monotone trends across all nine cells:

1. **Raising the score bar makes things WORSE.** At 15 positions, alpha falls
   +3.20% → +2.55% → −4.65% as the bar goes 60 → 70 → 80. **The scoring model
   does not rank: its high-conviction names underperform its marginal ones.**
   Everything built on "score ≥ 75 = high conviction" — the CONFIDENT BUY
   alerts, `MIN_SIGNAL_SCORE`, the notification threshold — rests on an ordering
   that this test does not support.
2. **Diversification helps, a lot.** 4 → 8 → 15 positions improves alpha at
   every threshold (−4.60 → +1.39 → +3.20 at bar 60). Beta rises too (0.27 →
   0.61), so part is market exposure — but alpha rising alongside it is not.

⚠️ **The live config sits in the bad corner.** `MIN_SIGNAL_SCORE=70` plus
per-strategy weekly limits of 1–3 trades produce a small, high-bar book: the
regime this grid scores worst. Do not change live settings off one 3-year
window on one strategy — run `--robustness` on the winning cell first — but the
direction is consistent enough that the current defaults should not be treated
as validated.

**No cell beats buy & hold.** Best Sharpe here is 0.85 against SPY's 1.08.

### The winning cell did NOT survive the split

`--robustness --max-positions 15` on S3 (now reports alpha/Sharpe per half):

```
  3.0x ATR   full  beta 0.61  alpha  +3.20%  Sharpe 0.85  (SPY 1.08)
             1st   beta 0.79  alpha  +5.52%  Sharpe 1.35  (SPY 1.45)
             2nd   beta 0.52  alpha  -0.31%  Sharpe 0.45  (SPY 0.85)

  3% fixed   full  beta 0.39  alpha  +4.73%  Sharpe 0.74  (SPY 1.08)
             1st   beta 0.47  alpha  -0.05%  Sharpe 0.69  (SPY 1.45)
             2nd   beta 0.35  alpha  +9.73%  Sharpe 0.84  (SPY 0.85)
```

**S3's +3.20% alpha is a first-half artefact** — +5.52% then −0.31%. The
headline came from the good half, exactly as every stop setting did before it.

**And the 3% fixed stop — the one this file calls catastrophic — is the only
config with positive SECOND-half alpha (+9.73%), at the lowest beta (0.35).**
That does not overturn the wide-stop finding, which was measured on S1/S2 at 8
positions; it does mean *"wider stops are a large, robust improvement"* is not
established at 15 positions on S3, and the word **robust** was too strong.

The honest synthesis: **two configurations, opposite halves, neither consistent.**
One works in the first half, the other in the second, and SPY's own Sharpe fell
1.45 → 0.85 across them — the regime changed and the strategies did not survive
the change. That is the signature of no persistent edge, not of a setting that
needs tuning.

⚠️ **Do not retune live off this.** The reason to run the split is to stop
exactly that.

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
- `/api/scan/job` (browser Live Signals, POST to start + GET to poll) — the
  same combined S1+S2+S3 scan `run_scan_job()` in scanner.py runs, with
  progress written to the state store every 25 tickers instead of streamed.
  Replaced the old `/api/scan/stream` SSE endpoint entirely (removed, along
  with the unused `/api/scan/stream/s2`) — see the AWS Migration section for
  why: a real scan takes minutes, not seconds, and neither Lambda nor
  CloudFront can hold a request open that long.
- Both compute the SPY relative-strength benchmark once per scan.

**TEST_UNIVERSE** (used when `force=true`): AAPL, NVDA, MSFT, GOOGL, META

**Performance:** a full curated-universe scan takes **~3-4 minutes** in
practice (measured directly, not estimated) — batched yfinance calls plus
retries on delisted tickers add up across 472 names. The "a few seconds"
figure that used to be here was wrong and cost real debugging time: it's
what the whole streaming-vs-async-job design decision above turned on.
`get_universe()` (full Alpaca list) still exists but is no longer used for
scanning — kept only for reference.

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
ALPACA_API_KEY=<your-alpaca-key-id>
ALPACA_SECRET_KEY=<secret>
ALPACA_MODE=paper

# Twilio — RETIRED. Not a dependency: `twilio` is not in requirements.txt,
# nothing reads TWILIO_ACCOUNT_SID, and send_whatsapp() is an alias for
# send_telegram(). Do not seed these; do not buy credentials for them.

# API auth — see the API Authentication section. Two secrets, not one:
# a phone is the most losable device here, so what it carries must not
# be able to move money. The gate is SKIPPED entirely if API_READ_TOKEN
# is unset, logging a warning per request — a deploy must not lock the
# owner out before the variable exists, but must not go quiet either.
API_READ_TOKEN=<memorable passphrase — every /api/** request>
TRADE_PIN=<additionally required for every non-GET /api/**>
ALLOWED_ORIGINS=<comma-separated; never "*" once tokens are in play>

# Notifications
TELEGRAM_BOT_TOKEN=<secret>
TELEGRAM_CHAT_ID_S1=<per-strategy chats>
VAPID_PUBLIC_KEY=<web push>
VAPID_PRIVATE_KEY=<secret>
PUSH_SCANS=1                # 0 disables the daily scan digest on push

# Persistence — REQUIRED on Railway. Without it every module falls back
# to /tmp, which is wiped on redeploy, taking the trade log with it.
DATA_DIR=/data

# Trading parameters
# Trade budget — PER STRATEGY (capital and attempts follow conviction).
# S3 is the only strategy profitable in both halves of the backtest, so it gets
# the most of both; S2 is throttled to token size purely to keep its live
# sample growing. Blank/absent falls back to the global value below.
WEEKLY_TRADE_LIMIT=2        # global fallback for untagged paths
WEEKLY_TRADE_LIMIT_S1=2
WEEKLY_TRADE_LIMIT_S2=1
WEEKLY_TRADE_LIMIT_S3=3
PER_TRADE_MAX_USD=2500      # hard cap; overrides Kelly risk sizing when it binds
PER_TRADE_MAX_USD_S1=1000
PER_TRADE_MAX_USD_S2=100
PER_TRADE_MAX_USD_S3=5000
CASH_RESERVE_PCT=30         # keep 30% of EQUITY liquid; see below
# Per-strategy slice of the deployable budget. Without these, whichever
# strategy ran first could spend the whole account — and once did.
CASH_SHARE_S1=30
CASH_SHARE_S2=20
CASH_SHARE_S3=50            # highest conviction; the loop also runs s3 first
MIN_SIGNAL_SCORE=60
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

## 💵 Cash Reserve — CASH_RESERVE_PCT

On the first slot that could actually execute (13 Aug 2026, 09:35 ET) the bot
deployed **$99,414 of a $99,414 account and left $0.01**. Every gate passed —
per-trade cap, weekly limit, Kelly sizing — because **none of them limits the
total committed at once**. Consequences: no capacity for the 11:00 slot, none
for a better signal the next day, and the whole account in whichever strategies
happened to fire first that morning (13 buys: S1 x8, S2 x5, S3 x0 — i.e. all of
it in the two strategies that fail the second-half backtest, none in the one
that survives it).

`CASH_RESERVE_PCT` (default 30) is measured against **equity, not free cash**,
so it means "keep this share of the account liquid" rather than a share of
whatever happens to be left. When the account is already over-deployed it simply
yields nothing to spend — **it never forces a sale** to rebuild the buffer.

    reserve    = equity * CASH_RESERVE_PCT / 100
    deployable = max(0, free_cash - reserve)

Orders size against `deployable` and draw it down as they go; the slot stops
when it falls below $1. Do not size against raw `free_cash` again.

---

## 🍰 Per-Strategy Cash Share — CASH_SHARE_S1/S2/S3

The scheduled slot used to run `for strat in ("s1","s2","s3")` against a single
pot of cash, so **whichever ran first could spend everything**. On 13 Aug 2026
S1 and S2 consumed the entire account at 09:35 ET and S3 — holding candidates
scoring **90, 84 and 73** — arrived at $0.01 and bought nothing.

Execution order was silently deciding capital allocation, and it decided against
the one strategy profitable in both halves of the backtest. That is the exact
opposite of what this file says the design intends.

Each strategy now receives a slice of the deployable budget:

    deployable = max(0, free_cash - equity * CASH_RESERVE_PCT/100)
    budget(s)  = deployable * CASH_SHARE_S{n} / 100

Defaults follow conviction: **S3 50%, S1 30%, S2 20%**. The loop also runs
**s3 first**, so any rounding edge falls its way rather than against it.

⚠️ Do not go back to a single shared pot. Per-trade caps and weekly limits bound
one order and one count; neither bounds what a strategy can take from the whole.

---

## 📈 Pick Outcome Tracking — picks_log.py

Records every pick the scheduled scans produce and fills in what each name did
1, 5 and 20 trading days later. Answers the question the Signals tab raises but
cannot settle: **does a score of 90 mean anything?**

**Deliberately separate from `trades_log.json`.** That records what was BOUGHT;
most picks never are — the weekly limit, the cash share or an existing holding
stops them. Judging the scoring engines by the trade log only ever measures the
subset that survived the gates, which is a different question.

Two rules carried from the backtester:

* **A baseline is stored alongside.** "+2% in five days" is unreadable if SPY did
  +2.5%. Every pick keeps SPY's return over the identical window, and the UI
  shows the difference in percentage points.
* **Returns are measured from the pick day's close and NEVER revised.** That is
  when the signal was known; anything earlier is lookahead. Verified: re-running
  the fill leaves existing values untouched.

Scores are bucketed (60-69, 70-79, 80-89, 90+) because the useful question is
monotonicity — do higher scores earn higher returns — not what one pick did.

* Recording hooks `_save_picks*`, idempotent per (date, strategy) so the two
  daily slots cannot double-count the same signal.
* Backfill runs after the 03:30 ET pre-market scan, in a thread.
* `GET /api/picks/outcomes`, `POST /api/picks/backfill`; state in
  `picks_log.json` on the volume.
* `summary()["verdict"]` refuses to draw conclusions below ~30 matured picks.

Nothing here touches the trading path — every hook is wrapped so a research
logger can never break a scan.

---

## 🔐 API Authentication — raanu/api/auth.py

Before this the deployed API was **completely open**: `curl .../api/account/cash`
with no credentials returned the live balance, and `/api/orders/buy`,
`/api/orders/sell` and `/api/auto/start` took no auth at all. CORS was
`allow_origins=["*"]`. The only protection was that nobody knew the URL.

**Two tokens, not one.** A phone is the most losable device in this system, so
the token it carries must not be able to move money.

| Token | Header | Covers |
|-------|--------|--------|
| `API_READ_TOKEN` | `Authorization: Bearer …` | every `/api/**` request |
| `TRADE_PIN` | `X-Trade-Token: …` | **additionally** every non-GET `/api/**` |

- Non-GET is denied **by method, not by a path list** — a new POST route is
  protected the day it is written, not the day someone remembers to add it.
- `GET /` (the HTML shell) stays public; it holds no data.
- ⚠️ **`/webhook/whatsapp` is outside `/api/`, so the gate never sees it** — and
  its `BUY`/`SELL` commands place real orders. It **failed open** until
  31 Aug 2026: the check read `if From and From != expected`, so *omitting*
  the form field skipped it entirely, and the expected number was a hardcoded
  default committed to this repo. A bare
  `curl -d 'Body=BUY NVDA 5000' .../webhook/whatsapp` — no passphrase, no
  trade PIN — reached the order path. Verified against the running app, then
  fixed to fail closed: unset `USER_WHATSAPP` now rejects everything.
  **Delete this route.** Twilio is retired so nothing legitimate calls it, and
  a `From` field is not authentication — Twilio's actual protection is the
  `X-Twilio-Signature` HMAC, which this never implemented. If chat commands
  are ever wanted back, use Telegram's webhook with a secret token.
- The old `/api/scan/stream` used to accept `?token=` too, because
  **EventSource cannot set headers** — that route is gone (replaced by
  `/api/scan/job`, ordinary header auth like everything else), so there is
  no longer any place a token rides in a URL or could appear in access logs.
- **The gate is skipped entirely when `API_READ_TOKEN` is unset**, logging a
  warning on every request. A deploy must not lock the owner out before the
  variable exists, but "temporarily open" must not go quiet either.
- `ALLOWED_ORIGINS` replaces `"*"`. With tokens in play, a wildcard origin lets
  any page the user visits read their account.

### The browser never holds the passphrase

The dashboard used to keep the raw `API_READ_TOKEN` in
`localStorage['raanu.readToken']` and replay it as a bearer token. That put
**the server's own permanent secret** — the one SSM holds — in script-readable
storage on every machine the dashboard had ever been opened on. Any XSS on the
origin exfiltrates it, DevTools shows it in plain text, and it never expires.

It is now exchanged, once, for a **JWT** (HS256, 12h):

    POST /api/auth/session {"passphrase": …}
      → {"access_token": "eyJhbGci…", "token_type": "Bearer",
         "expires_in": 43200}
      → Set-Cookie: raanu_session=eyJhbGci…
                    HttpOnly; Secure; SameSite=Strict; Max-Age=43200

**The same token goes out two ways and the client picks.** The browser uses
the cookie and ignores the body; curl and scripts read `access_token` and send
`Authorization: Bearer`. That split is the design, not an accident:

- A browser must not hold the token where script can reach it. `HttpOnly` is
  the point — JavaScript cannot read it, so an injected script has nothing to
  steal. **A client that reads `access_token` out of the login response and
  stores it has undone the whole thing.** The dashboard deliberately does not.
- A script has no cookie jar worth relying on, and before JWTs it
  authenticated with `Bearer <the raw passphrase>` — so every script carried
  the *permanent* secret. Now it carries a 12-hour one.

The passphrase crosses the wire exactly once, in a TLS body, and is never
written down client-side.

⚠️ **`algorithms=["HS256"]` in `verify_token()` is load-bearing, not
boilerplate.** It is what makes PyJWT reject `alg: none` and
algorithm-confusion forgeries. Never widen it, and never pass the token's own
header algorithm. There is a test named `test_alg_none_is_rejected` that fails
the moment someone does.

- **The token is stateless** — claims verified by signature rather than
  looked up. A session table would mean a DynamoDB read on the critical path
  of every request, including the 1.5s scan poll. The cost is that a token
  cannot be revoked individually; rotating the passphrase revokes all of
  them, which is the only revocation this one-owner system needs.
- **The HMAC key is derived from `API_READ_TOKEN` itself.** No second secret
  to seed, and **rotating the passphrase signs every device out** — that is
  the revocation switch, with no infrastructure behind it. Re-run
  `./aws/seed-secrets.sh API_READ_TOKEN` if a laptop goes missing.
- **PBKDF2, not a bare hash** (200k rounds, derived once per cold start and
  cached). The JWT signing key is the derived key, never the passphrase
  itself — otherwise a stolen token is an offline guessing target against a
  deliberately *memorable* secret.
- **Nothing secret goes in the claims.** A JWT payload is base64, not
  encryption; anyone holding the token can read it. Claims are
  `iss/sub/iat/exp` and nothing else.
- **Raw-passphrase bearer still works** as the root credential, so there is a
  way in before any token has been minted. It never expires, so prefer the
  JWT for anything scripted.
- **CSRF**: `SameSite=Strict` covers reads, and writes are covered by
  construction — a cross-site request cannot set `X-Trade-Token`, so ambient
  cookie authority can never move money.
- `GET /api/auth/status` returns **booleans only** and is outside the gate, so
  the dashboard can ask "protected? signed in?" without firing a request it
  expects to 401. Probing with a real endpoint put a failed attempt on the
  lockout counter at **every page load**.
- The dashboard **actively deletes** the legacy `raanu.readToken` key on load.
  Moving where the secret lives does nothing if the old plaintext copy stays
  behind in every browser.

`action()` still prompts for the trade PIN per action and never stores it.

⚠️ **The phone app still stores the passphrase** in `AsyncStorage['raanu.pass']`
(app-private, so it needs a rooted device, and the server's deny-by-method rule
means it still cannot move money). Fixing it means a Play release and a
re-unlock every 12h on a monitor app, so it was left alone deliberately —
not overlooked.

⚠️ **`POST /api/auto/stop` really does stop the live bot.** A verification call
during this work disabled production and had to be re-enabled — test against
localhost, or use a GET, unless you mean it.

### Removed, and why

- **`GET /api/test/twilio`** echoed `sid[:8]` and `token[:6]` of the live
  Twilio credentials in its response body, and **sent a real message on a
  GET** — so anything that prefetches a URL could fire it. Dead code besides.
- **`POST /api/auth/pin`** answered "is this the right PIN?". Being a POST,
  the gate already demanded the PIN to reach it, so it could only ever confirm
  something the caller had just proved they knew. Zero callers.

---

## 🔔 Notifications — raanu/notify/{telegram,push}.py

**Two channels.** The Android apps — the React Native app, the TWA, and the
whole FCM path — were removed on 31 Aug 2026. What is left is the web
dashboard and the two ways it can reach you.

| | Telegram | Web push |
|---|---|---|
| BUY / EXIT / ERROR | ✅ | ✅ |
| Daily scan digest | ✅ | ✅ |
| Quiet days ("no signals") | ✅ | ❌ |

**Web push still works on a phone.** The dashboard remains a PWA: Add to Home
Screen on Android Chrome or iOS Safari (16.4+) and notifications arrive with
the tab closed. That was always the free path, and it is now the only one —
which also retires the dual-subscription problem that used to send two
notifications per trade, because a web push belongs to the service worker that
registered it and there were two registrars.

**Scan digests are ONE notification, never one per strategy, and empty scans
send nothing.** A channel that fires on everything gets dismissed by reflex,
and then the stop-out gets dismissed with it. `PUSH_SCANS=0` disables them.

### `GET /api/push/status` — use this first
Reports whether VAPID is configured and how many browsers are subscribed.

It exists because "push is broken" was ambiguous between *the browser never
registered* and *the server cannot deliver* — opposite fixes, and guessing
between them cost more time than the endpoint took to write.

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
python-multipart
```

Install all:
```bash
pip3 install fastapi uvicorn httpx python-dotenv pydantic numpy pandas yfinance ta python-multipart
```

---

## 🚀 Deploying — see DEPLOY.md

⚠️ **`git push` deploys NOTHING.** `.github/workflows/deploy-aws.yml` is
`workflow_dispatch`-only — deliberately, so a deploy is a decision rather than
a side effect of a commit. The old Railway habit of "pushing *is* the deploy"
does not carry over, and this doc said otherwise for a while.

```bash
gh workflow run "Deploy AWS skeleton" && gh run watch
```

One run ships everything — the `raanu/` package, both Lambda handlers, the CDK
stack and the dashboard assets. There is nothing else to release: the PWA
updates whenever the dashboard does, because it *is* the dashboard.

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
- [x] Pre-trade + post-trade alerts via Telegram
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
- [x] Token-gated API (read passphrase + separate trade PIN, deny-by-method)
- [x] Mobile-responsive layout + PWA (Add to Home Screen)
- [x] Push on two channels — Telegram and web push
- [x] Pick outcome tracking with a SPY baseline (`picks_log.py`)
- [x] Per-strategy cash shares, so execution order no longer decides allocation
- [x] Strategy stamped into `client_order_id` — attribution survives a log wipe

## 🔲 Pending / Next Steps
- [ ] **Beat the benchmark.** S3 is the first strategy to survive the
      first/second-half split, but still returns +42% against SPY's +79%.
      No strategy has yet beaten buy & hold in any test.
- [ ] Dashboard has no S3 tab — Live Signals and the strategy filter still
      only know about S1/S2. `/api/strategy/compare` also only reports s1/s2.
- [ ] `PER_TRADE_MAX_USD=2500` still binds on every candidate, so per-trade risk
      is compressed but not equalised — needs ~$10k for `MAX_POSITION_PCT` to
      become the real limit
- [ ] Dashboard sell button for open positions. The deleted native app had
      one (long-press, PIN prompted per action, never stored) — that
      interaction is worth copying, the code is in git history.
- [ ] News sentiment scoring (currently no real data source)
- [x] **Android apps removed — 31 Aug 2026.** `mobile/` (React Native),
      `twa/` (Play wrapper), `deploy-mobile.sh`, `tools/play_upload.py`,
      `privacy.html`, the assetlinks route and the whole FCM push path are
      gone. This supersedes the "Play Store: internal testing only" decision
      of 16 Aug 2026.

      The apps were a monitor for one Alpaca account behind a passphrase, and
      the PWA already delivers that: Add to Home Screen gives the full
      dashboard, an app icon, no browser bar, and web push. What the apps
      added over it was Play auto-updates — for two known users — against the
      cost of two keystores that can never be lost, a signing pipeline, a
      second notification transport, and a second copy of the passphrase in
      `AsyncStorage`. That last one is what settled it.

      To bring one back you would need the signals-only public mode that
      still does not exist, and an answer to whether a public app
      recommending stocks is regulated as investment advice (it is, in most
      jurisdictions including India). Answer that one first.

---

## 💾 Persistent State — datadir.py

`trades_log.json`, `position_peaks.json` and the picks caches must survive a
restart. Three things break silently if they don't:

- **strategy attribution** — round-trips are tagged from the ticker's BUY entry,
  so an empty log reports every closed trade as `s1`
- **`WEEKLY_TRADE_LIMIT`** — a wiped log re-arms the bot to trade again
- **`kelly.py` `MIN_SAMPLE`** — the 30-trade gate never graduates off the
  fallback risk if the sample keeps resetting

Resolution order: `$DATA_DIR` → project dir (local dev, detected by `.env`) →
`/tmp` with a warning. **On Railway a 5GB volume is mounted at `/data` with
`DATA_DIR=/data`.** Before this, all three modules independently fell back to
`/tmp`, which Railway wipes on every redeploy.

Check it with `GET /api/health` → `state.data_dir` / `state.persistent`.
If `data_dir` reads `/tmp`, the write test failed and state is ephemeral.

---

## 🏷 Strategy Attribution — the broker is the record

Every BUY stamps its strategy into Alpaca's **`client_order_id`**:

    raanu-s3-NVDA-20260816T143022123

Alpaca keeps that for the life of the order, so **the broker is the durable
record and `trades_log.json` is a cache**. Before this, the tag lived only in
the log — so when the log was still on `/tmp` and Railway wiped it, the answer
to "which strategy bought this?" was destroyed permanently. **BAC, OKTA and
ROKU are still unattributable for exactly that reason and cannot be
recovered**; Alpaca recorded the order, never the reason for it.

⚠️ **Unattributable means `"unknown"`, never `"s1"`.** `strategy_for()` used to
return `"s1"` for positions with no BUY on record — a guess presented as a
fact. The dashboard showed UNTAGGED for the same position while the exit engine
treated it as S1, and `_record_exit()` then **wrote `"s1"` into the log on
close**, so an unattributable trade's P&L permanently joined S1's track
record — which is what `kelly.py` sizes every future position from. The guess
did not stay cosmetic.

`"unknown"` is safe because no `stop_atr_mult_unknown` or
`profit_ladder_unknown` key exists, so both fall through to the shared
defaults, which are identical to the S1 values. Same 2.5×ATR stop, same
ladder — label only. Verify that equivalence before adding any per-strategy
key, or "unknown" silently starts meaning something.

---

## ☁️ AWS Migration — DONE, and the performance work behind it

Production is AWS. Railway is retired; the code no longer runs there.

**Architecture**: CloudFront serves the dashboard from S3 and routes
`/api/*` + `/webhook/*` to the API Lambda (FastAPI behind Mangum) — one
browser-visible origin, so no CORS. A worker Lambda runs everything
scheduled. All state is in one DynamoDB table. Secrets come from SSM
Parameter Store at cold start.

### The scan: 120s -> 23s cold, 6.8s warm (measured on the deployment)

Two things got it there, and **the one I expected to matter mattered less
than the one I found by measuring**:

1. **A daily bars cache.** These are daily bars — they change once a
   session — but every scan re-downloaded a year of history for all 470
   tickers, which was ~80% of the runtime. Now the first scan of a session
   pays for the download and every later one just scores. Payload is
   gzipped and rounded to 4dp (a hundredth of a cent, past any precision an
   indicator uses); Yahoo's float32 artefacts like `78.29000091552734`
   tripled the stored size for no information.
2. **Sharded fan-out**, 8 worker invocations.

⚠️ **Benchmark locally before assuming concurrency helps.** yfinance
downloads cap at **~6.2 tickers/sec from one host and adding workers does
not move it** — 1, 4, 8 and 16 all measured the same. That killed the
original "fan out for ~5x" plan before it cost a deploy. Fan-out *does*
help on Lambda (23s vs an ~87s local extrapolation) because separate
execution environments get separate source IPs, which is exactly the thing
a single-machine benchmark cannot tell you. Both facts are true and neither
is guessable from the other.

Use `python -m tools.bench_scan` before changing `SCAN_SHARDS` or
`SCAN_BATCH_SIZE`.

### Cost — measured, not modelled

Peak memory 375 MB of 1024 MB; 178 GB-s for two full scans. Projected
~11,500 GB-s/month against a 400,000 GB-s always-free tier: **~3%**.
Parallelism here is effectively free; the reason to bound it is cold-start
waste, not cost.

The heartbeat is `cron(0/5 13-21 ? * MON-FRI)` **UTC** — a deliberate
superset of the session under both EST and EDT, because EventBridge cron
has no timezone parameter and a tighter window would drift across DST. The
worker's own US/Eastern logic still decides what runs. ~288 -> ~102
invocations/day; every one removed was a no-op.

**The schedule rule ships DISABLED.** Nothing trades autonomously until it
is explicitly enabled.

### Things that will bite the next person

- **`Dockerfile.lambda`, not `Dockerfile`.** Railway auto-detects a bare
  `Dockerfile` at repo root. Named this way even now that Railway is
  retired, because renaming it buys nothing and un-naming it is a trap.
- **Container image, not a zip.** Measured: this dependency set is 266 MB
  unzipped, already past the 250 MB zip ceiling before anything is added.
- **ECR has a lifecycle policy** capping the shared bootstrap repo at 15
  images. CDK tags every build permanently and the repo's default rule only
  expires *untagged* images, so without it every `requirements.txt` change
  piles up a new ~200-300 MB layer forever. Applied by hand, account-wide,
  not reapplied by `cdk deploy` — see `aws/README.md`.
- **DynamoDB TTL is on `ttl`.** Scan shards and cached bars set it and
  expire themselves. Nothing else deletes them.
- **Two circular-dependency traps in the CDK stack**: the Lambda env must
  not reference the CloudFront domain, and neither must the Function URL's
  CORS config — the Distribution already depends on the Function URL as an
  origin. Both were hit; both cost a deploy.
- **CloudFront must not forward the viewer `Host` header** to a Function
  URL origin (use `ALL_VIEWER_EXCEPT_HOST_HEADER`). With `ALL_VIEWER` the
  origin rejects the request and you get `{"Message":null}`.

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
- macOS uses `python3` not `python`

---

## 💬 How to Use This File

**You no longer need to paste it.** Claude Code loads `CLAUDE.md` from the
project root automatically at the start of every session in this directory.

Two things to remember when reading it:

1. **The global `~/.claude/CLAUDE.md` loads too, and it is about an unrelated
   Delivery Hero tool.** This file overrides it on every point of conflict —
   see the banner at the top.
2. **Keep the evidence, not just the conclusions.** Most warnings here exist
   because something failed in production and cost real time: the scheduler
   that never fired, the 100%-deployed account, the fixed stop tighter than
   daily ATR, the strategy tag that lived in one wipeable file. A rule without
   its reason gets "simplified" away by the next person, including a future
   Claude.

The single most important line in the file: **no strategy has beaten SPY
buy-and-hold in any test.** S3 is the only one profitable in both halves.
Stay on paper.


