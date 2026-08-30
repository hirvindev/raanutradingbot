"""
raanu.market.cache — daily OHLCV cache
=======================================
Why this exists, in one measurement: yfinance downloads cap out at about
**6.2 tickers/second and concurrency does not move that number** — 1, 4, 8
and 16 parallel workers all benchmarked the same. It is a server-side
throttle, so no amount of Lambda fan-out beats it. 472 tickers is therefore
~76s of unavoidable download, which was ~80% of the ~120s scan.

So don't parallelize the work — stop repeating it. These are **daily** bars:
they change once per session, but every scan re-downloaded a full year of
history for all 472 tickers. The first scan of a session pays for the
download; every later one that day reads the cache and only scores, which
is ~8s of CPU.

That maps directly onto how the two callers differ:

  * the scheduled 03:30 ET scan warms the cache (cheap, nobody waiting)
  * the "Scan market" button then hits a warm cache (fast, someone waiting)

Payload is gzipped. Uncompressed, 472 tickers x ~35KB/day of writes would
be real money on DynamoDB's per-KB write pricing; compressed it is ~5KB a
ticker and rounds to nothing.
"""

from __future__ import annotations

import base64
import gzip
import logging
from datetime import datetime
from io import StringIO
from zoneinfo import ZoneInfo

import pandas as pd

from raanu import config, state

log = logging.getLogger("raanu.market.cache")

US_EAST = ZoneInfo("US/Eastern")

# Keep a few sessions so a scan just after midnight ET, or a backfill, still
# finds recent data. Long enough to be useful, short enough that the cache
# never becomes storage worth thinking about.
_TTL_SECONDS = 4 * 24 * 3600


def session_date() -> str:
    """Cache generation, keyed on the US/Eastern date.

    ET rather than UTC because the bars are US market data: every scan
    during one trading session must agree on which day's cache it wants,
    and a UTC key would split a single ET session across two generations
    for anything running after 20:00 ET.
    """
    return datetime.now(US_EAST).strftime("%Y-%m-%d")


def _key(ticker: str, day: str) -> str:
    return f"bars/{day}/{ticker}"


def enabled() -> bool:
    return config.env_bool("BARS_CACHE", True)


# Prices are stored to 4 decimal places — a hundredth of a cent, well past
# any precision an indicator can use, and far past the ~2dp the exchange
# actually quotes. Yahoo hands back float32 artefacts like 78.29000091552734;
# keeping those digits triples the stored size for no information. Rounding
# is 3.2x smaller than raw JSON versus 2.0x for gzip alone, which is the
# difference between a cache that costs cents and one that costs nothing.
_PRECISION = 4


def _encode(df: pd.DataFrame) -> str:
    payload = {
        # Column dtypes do NOT survive a JSON round trip: an all-integral
        # float Volume column comes back as int64, silently changing the
        # dtype of data every score is computed from.
        "dtypes": {str(c): str(t) for c, t in df.dtypes.items()},
        "frame": df.round(_PRECISION).to_json(
            orient="split", date_format="iso", double_precision=_PRECISION),
    }
    import json
    raw = json.dumps(payload).encode()
    return base64.b64encode(gzip.compress(raw, compresslevel=6)).decode()


def _decode(blob: str) -> pd.DataFrame:
    import json
    payload = json.loads(gzip.decompress(base64.b64decode(blob)).decode())
    df = pd.read_json(StringIO(payload["frame"]), orient="split")
    df.index = pd.to_datetime(df.index)
    for column, dtype in (payload.get("dtypes") or {}).items():
        if column in df.columns:
            try:
                df[column] = df[column].astype(dtype)
            except (TypeError, ValueError):
                pass
    return df


def load(tickers: list[str], day: str | None = None) -> dict[str, pd.DataFrame]:
    """Return cached frames for whichever tickers are present. Never raises —
    a cache miss and a broken cache must both just mean "download it"."""
    if not tickers or not enabled():
        return {}
    day = day or session_date()
    out: dict[str, pd.DataFrame] = {}
    try:
        raw = state.load_many([_key(t, day) for t in tickers])
    except Exception as e:
        log.warning(f"Bars cache read failed: {e}")
        return {}
    for ticker in tickers:
        entry = raw.get(_key(ticker, day))
        if not entry:
            continue
        try:
            out[ticker] = _decode(entry["bars"])
        except Exception as e:
            log.debug(f"Discarding unreadable cached bars for {ticker}: {e}")
    return out


def store(frames: dict[str, pd.DataFrame], day: str | None = None) -> int:
    """Cache freshly-downloaded frames. Empty frames are skipped — caching a
    delisted ticker's empty result would just serve the emptiness faster."""
    if not enabled():
        return 0
    day = day or session_date()
    written = 0
    for ticker, df in (frames or {}).items():
        if df is None or getattr(df, "empty", True):
            continue
        try:
            state.save(_key(ticker, day), {"bars": _encode(df)}, ttl_seconds=_TTL_SECONDS)
            written += 1
        except Exception as e:
            log.debug(f"Could not cache bars for {ticker}: {e}")
    return written


def get_bars(tickers: list[str], downloader=None) -> dict[str, pd.DataFrame]:
    """Frames for `tickers`, from cache where possible and the network for
    the rest. The result is indistinguishable from a plain download, which
    is what lets the engine stay unaware that a cache exists at all.
    """
    if not tickers:
        return {}
    if downloader is None:
        from raanu.market.prices import batch_download
        downloader = batch_download

    cached = load(tickers)
    missing = [t for t in tickers if t not in cached]
    if not missing:
        log.info(f"Bars cache: {len(cached)}/{len(tickers)} hit, no download needed")
        return cached

    fetched = downloader(missing) or {}
    stored = store(fetched)
    if cached:
        log.info(f"Bars cache: {len(cached)} hit, {len(missing)} downloaded ({stored} cached)")
    return {**cached, **fetched}
