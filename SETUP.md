# RaanuTradingBot — Local Setup

For architecture, environment variables, and design rationale, see
[CLAUDE.md](CLAUDE.md). This is just the "get it running on your machine"
guide.

## What you have

- `raanu/` — the application package (FastAPI backend)
- `handlers/` — Lambda entrypoints (not needed for local dev)
- `RaanuTradingBot.html` — the dashboard, served at `/`
- `.env` — your secrets go here (gitignored, never share this file)
- `requirements.txt` — Python dependencies

## One-time setup

### 1. Install Python 3.12+

```bash
python3 --version
```

macOS: `brew install python3` if it's missing or too old.

### 2. Get an Alpaca paper trading API key

1. Sign up at https://alpaca.markets (free)
2. Dashboard → **Paper Trading** → API Keys → generate a key
3. Copy the Key ID and Secret — the secret is shown once

Paper trading is a real Alpaca account with fake money. There is no "demo
mode" toggle to get wrong — the base URL (`paper-api.alpaca.markets`) is what
makes it paper.

### 3. Configure `.env`

```bash
cp .env.example .env
```

Fill in at minimum:

```
ALPACA_API_KEY=your-key-id
ALPACA_SECRET_KEY=your-secret
ALPACA_MODE=paper
```

Everything else in `.env.example` is optional — Telegram, web push, and the
API auth tokens each independently no-op when unset. See CLAUDE.md's
Environment Variables section for the full list and what each one gates.

### 4. Install dependencies and run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m raanu.api
```

Open **http://localhost:8000**. The server runs a silent startup scan and
starts the background schedule loop; `GET /api/health` confirms it's up and
shows whether state is persistent.

### 5. Run the tests

```bash
pytest                                       # 191 tests — no network, no AWS
ruff check raanu/ handlers/ tests/ tools/    # lint
```

## What works right now

- Real Alpaca paper account connection — live cash, positions, orders
- Three independent scoring strategies (S1 pullback, S2 breakout, S3 leader
  dip) over a curated ~470-ticker universe
- Auto-trader with a 5-gate system before any order, weekly per-strategy
  trade limits, and Kelly-based position sizing — **starts disabled**
- ATR-scaled stop-loss and trailing-stop exit engine
- Telegram + web push alerts (each optional, independently configured)
- Walk-forward backtester (`tools/backtest.py`) with stop-rule sweeps and a
  both-halves stability check
- Token-gated API: a read passphrase for `GET`s, a separate trade PIN for
  anything that moves money

## Important warnings

- **No strategy has beaten SPY buy-and-hold in any backtest.** S3 is the only
  one that stays profitable across both halves of the 3-year test. Read the
  Backtester section of CLAUDE.md before treating any score as a sure thing.
- **Stay on paper.** `ALPACA_MODE=paper` is the default and nothing in this
  repo flips it to live automatically.
- **Never share your `.env` file or anything under `~/.secrets/`.** They hold
  the keys that can place real orders on your Alpaca account.
- **The auto-trader starts disabled** even after you configure everything —
  you have to explicitly `POST /api/auto/start` or use the dashboard toggle.
