"""
raanu.market.universe — the curated tradable universe
======================================================
The list itself lives in ``universe.json`` beside this file; this module
only loads it and answers questions about it. It used to be ~290 lines of
literal Python, which meant every edit to the ticker list produced a code
diff and every code change risked touching the data.

**Strategy-driven, not brute-force, and not an exchange listing.** This is a
hand-maintained set of liquid quality names, not everything NASDAQ or NYSE
lists — it spans both (roughly 56% NYSE, 40% Nasdaq, plus 15 NYSE Arca
ETFs) because it was assembled by picking companies. Scanning the full
Alpaca universe would mean thousands of illiquid names the strategies would
never trade, and downloads are throttled at ~6.2 tickers/sec per host, so a
bigger universe is a real cost rather than a free option.

⚠️ The 77 high-beta/momentum additions (crypto miners, AI/quantum, space,
nuclear) run a median daily ATR of ~6.5% vs ~4.7% for the original list, and
53 of the 77 exceed 5%. They are only viable alongside ATR-scaled stops and
risk-based sizing. In backtest the expansion helped under a 2.5xATR stop
(+6.38% vs -0.04%) and hurt under the old fixed 3% stop (-32.10% vs
-22.19%). Do not revert to fixed-percentage stops while these are in.

``get_universe()`` — a live fetch of every Alpaca asset — used to live here.
Nothing had called it since scanning moved to this curated list, so it was
removed rather than left as a second, untested definition of "the universe".
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

import httpx

from raanu import config

log = logging.getLogger("raanu.market.universe")

UNIVERSE_FILE = Path(__file__).parent / "universe.json"

_asset_names: dict[str, str] = {}


@lru_cache(maxsize=1)
def _data() -> dict:
    """Parse universe.json once per process."""
    with UNIVERSE_FILE.open() as f:
        return json.load(f)


# Ordered. Scan shards are contiguous slices of this list, so the order
# decides which ticker lands in which shard.
FALLBACK_UNIVERSE: list[str] = _data()["universe"]

TICKER_NAMES: dict[str, str] = _data()["names"]

# Used when force=true, for a fast smoke-test scan.
TEST_UNIVERSE: list[str] = _data()["test_universe"]

# Tickers Yahoo Finance returns no data for. They score nothing either way,
# but they are not free: yfinance retries each one individually inside the
# batch call, and CloudWatch showed those retries burning ~20s per download
# on every run. Under the sharded scan that is worse, since wall-clock is
# the SLOWEST shard.
#
# Held here rather than deleted from the universe on purpose: these are
# real, liquid companies that belong in it and may start resolving again.
# Re-check with:
#   python -c "import yfinance; print(yfinance.download('BK CTRA', period='5d'))"
# and remove the entry from universe.json if data comes back.
KNOWN_NO_DATA: frozenset[str] = frozenset(_data()["known_no_data"])


def scannable_universe() -> list[str]:
    """The universe a scan should actually download — the curated list minus
    tickers currently returning no data."""
    return [t for t in FALLBACK_UNIVERSE if t not in KNOWN_NO_DATA]


def _load_asset_names() -> dict[str, str]:
    """Bulk-fetch symbol -> company name from Alpaca, cached for the process.

    Covers the tickers with no hand-written name in universe.json. Needs
    ALPACA_API_KEY; without it those tickers render as bare symbols.
    """
    global _asset_names
    if _asset_names:
        return _asset_names

    if not (config.alpaca_key() and config.alpaca_secret()):
        return {}

    base = config.broker_base().removesuffix("/v2")
    try:
        resp = httpx.get(
            f"{base}/v2/assets",
            headers={"APCA-API-KEY-ID": config.alpaca_key(),
                     "APCA-API-SECRET-KEY": config.alpaca_secret()},
            params={"asset_class": "us_equity", "status": "active"},
            timeout=30,
        )
        resp.raise_for_status()
        _asset_names = {a["symbol"]: a.get("name") or a["symbol"] for a in resp.json()}
        log.info(f"Loaded {len(_asset_names)} asset names from Alpaca")
    except Exception as e:
        log.warning(f"Could not load asset names: {e}")
        return {}
    return _asset_names


def get_ticker_name(ticker: str) -> str:
    """Company name for a ticker, or the ticker itself if unknown."""
    return TICKER_NAMES.get(ticker) or _load_asset_names().get(ticker) or ticker


def reset_name_cache() -> None:
    global _asset_names
    _asset_names = {}


def get_universe_summary() -> dict:
    return {"exchange": "US", "total_stocks": len(scannable_universe())}
