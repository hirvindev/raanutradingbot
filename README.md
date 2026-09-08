# RaanuTradingBot

Algorithmic trading bot on Alpaca paper trading, scoring a curated ~470-ticker
US equity universe against three strategies and (optionally) auto-trading the
best signals under a set of hard risk gates.

**This is a personal project.** Full context — architecture, environment
variables, the reasoning behind every non-obvious decision, and the backtest
results that shaped the current config — lives in [CLAUDE.md](CLAUDE.md).
Read that first; this file is just the map.

- **Production:** AWS Lambda + CloudFront + DynamoDB —
  https://d2c2x91kx43y5d.cloudfront.net
- **Local dev:** `python -m raanu.api` → http://localhost:8000
- **Deploying:** see [DEPLOY.md](DEPLOY.md) — `git push` deploys nothing,
  deploys are a manual `gh workflow run`
- **AWS internals:** see [aws/README.md](aws/README.md)

## Quick start (local dev)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # fill in ALPACA_API_KEY / ALPACA_SECRET_KEY at minimum
python -m raanu.api
```

Open http://localhost:8000. `python -m raanu.api` loads `.env`, runs a
one-off startup scan, and starts the background schedule loop (03:30 / 09:35
/ 11:00 ET) — none of it places live-money orders; the account is Alpaca
paper trading.

```bash
pytest                                       # 191 tests, no network/AWS
ruff check raanu/ handlers/ tests/ tools/    # lint
```

## Layout

```
raanu/          the application package — config, market data, strategies,
                scanning engine, trading/exits/sizing, notifications, API
handlers/       thin Lambda entrypoints (api.py, worker.py)
aws/            CDK app — the whole AWS stack
tests/          191 tests
tools/          backtest.py, bench_scan.py
RaanuTradingBot.html, sw.js, manifest.webmanifest, icons/
                the dashboard — a single-file PWA, no build step
```

See CLAUDE.md's File Structure section for the full breakdown of what's
inside `raanu/`.

## Strategies

Three independent signal engines, each scoring 0–100 against the same
uptrend-filtered universe:

| | Idea | Status |
|---|---|---|
| S1 — pullback | buy a dip to the rising 20-EMA in an uptrend | live |
| S2 — breakout | Minervini stage-2 / VCP breakout to new highs | live |
| S3 — leader dip | mean reversion to the lower Bollinger Band in a name beating SPY | live, best-validated — the only one profitable in both halves of the 3-year backtest |

**No strategy has beaten SPY buy-and-hold in any backtest.** This stays on
paper trading. See CLAUDE.md for the full numbers and why that matters.

## Safety by default

- Auto-trader starts **disabled** — nothing trades until explicitly turned on
- Two-secret API auth: a read passphrase and a separate trade PIN, so looking
  at the dashboard never requires the credential that can move money
- Weekly per-strategy trade limits, per-trade dollar caps, and a cash reserve
  that's never fully deployed
- The AWS EventBridge schedule ships **disabled** — nothing runs
  autonomously until someone flips it on in the console
