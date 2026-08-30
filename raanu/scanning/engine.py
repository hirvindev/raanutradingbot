"""
raanu.scanning.engine — the one scan implementation
====================================================
Replaces four near-identical loops (``find_top_picks``,
``find_top_picks_s2``, ``find_top_picks_s3``, and the SSE route's inline
copy). Every scan in the system — interactive, scheduled, sharded or
not — now runs the same scoring code, so the Live Signals screen and the
auto-trader can no longer disagree about what a ticker scored.

``scan_batch`` is deliberately pure: no state I/O, no env reads, no
progress reporting. Everything that varies between a fast sharded scan and
a cheap sequential one lives in ``raanu.scanning.job``, which composes this.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor

from raanu import strategies
from raanu.market.cache import get_bars
from raanu.market.prices import benchmark_return_3m
from raanu.market.universe import get_ticker_name, scannable_universe

log = logging.getLogger("raanu.scanning.engine")

# Market-cap lookups are one HTTP call per ticker and only decorate the UI,
# so they run in a small pool after scoring rather than inline. Serially,
# inside the scoring loop, they were adding a round trip per qualifying hit.
_CAP_FETCH_WORKERS = 8


def _cap_label(market_cap: float | None) -> str:
    if not market_cap:
        return "—"
    if market_cap >= 200e9:
        return "Mega"
    if market_cap >= 10e9:
        return "Large"
    if market_cap >= 2e9:
        return "Mid"
    if market_cap >= 300e6:
        return "Small"
    return "Micro"


def _fetch_market_cap(ticker: str) -> float | None:
    try:
        import yfinance as yf
        return yf.Ticker(ticker).fast_info.market_cap
    except Exception:
        return None


def enrich_market_caps(hits: list[dict]) -> None:
    """Attach cap_label to each hit, in parallel, in place.

    One lookup per distinct ticker even when a ticker qualifies under
    several strategies — the same name showing up as both an S1 pullback and
    an S3 dip is two setups, not two companies.
    """
    if not hits:
        return
    tickers = sorted({h["ticker"] for h in hits if h.get("ticker")})
    if not tickers:
        return
    try:
        with ThreadPoolExecutor(max_workers=min(_CAP_FETCH_WORKERS, len(tickers))) as pool:
            caps = dict(zip(tickers, pool.map(_fetch_market_cap, tickers)))
    except Exception as e:
        log.warning(f"Market-cap enrichment failed: {e}")
        caps = {}
    for hit in hits:
        hit["cap_label"] = _cap_label(caps.get(hit.get("ticker")))


def scan_batch(
    tickers: list[str],
    bench: float | None,
    keys: Iterable[str] = strategies.ALL_KEYS,
    *,
    predicate: str = "surfaces",
) -> list[dict]:
    """Score one batch of tickers against the given strategies.

    Pure: downloads prices, scores, filters. No state, no config, no
    progress side effects — which is what makes it directly unit-testable
    and safe to run concurrently in a shard.

    A ticker may appear more than once in the result if it passes several
    strategies. Those are different setups, not duplicates.
    """
    if not tickers:
        return []

    # Cache-aware: reads today's cached bars and downloads only what is
    # missing. Downloads are throttled at ~6.2 tickers/s server-side and
    # concurrency does not help, so not re-fetching is the only real win.
    data = get_bars(tickers)
    hits: list[dict] = []

    for ticker in tickers:
        frame = data.get(ticker)
        for key in keys:
            strategy = strategies.get(key)
            try:
                result = strategy.score(ticker, frame, bench_ret_3m=bench)
            except Exception as e:
                log.debug(f"Score error {ticker}/{key}: {e}")
                continue
            if not getattr(strategy, predicate)(result):
                continue
            result["strategy"] = key
            result["name"] = get_ticker_name(ticker)
            hits.append(result)

    return hits


def scan_universe(
    tickers: list[str],
    keys: Iterable[str] = strategies.ALL_KEYS,
    *,
    batch_size: int = 50,
    predicate: str = "surfaces",
    on_progress: Callable[[int, list[dict]], None] | None = None,
) -> list[dict]:
    """Scan a list of tickers in batches, reporting progress as it goes.

    ``on_progress(scanned_so_far, hits_so_far)`` is called after **every**
    batch. That cadence is the point: the previous scanner downloaded 250
    tickers at a time and only reported once the download returned, so the
    UI sat at 0% for ~53s and then jumped. Smaller batches mean the bar
    actually moves.
    """
    bench = benchmark_return_3m()
    hits: list[dict] = []
    scanned = 0

    for start in range(0, len(tickers), batch_size):
        batch = tickers[start:start + batch_size]
        hits.extend(scan_batch(batch, bench, keys, predicate=predicate))
        scanned += len(batch)
        if on_progress:
            on_progress(scanned, hits)

    return hits


def top_picks(strategy: str = "s1", limit: int = 3,
              tickers: list[str] | None = None) -> list[dict]:
    """Highest-scoring tradable candidates for one strategy.

    The auto-trader's entry point, replacing the three ``find_top_picks*``
    functions. Uses the ``tradable`` bar, not ``surfaces`` — the trader
    applies its own MIN_SIGNAL_SCORE gate on top of this.
    """
    universe = tickers if tickers is not None else scannable_universe()
    log.info(f"[{strategy}] scanning {len(universe)} tickers")
    hits = scan_universe(universe, [strategy], predicate="tradable")
    hits.sort(key=lambda h: h.get("score", 0), reverse=True)
    log.info(f"[{strategy}] {len(hits)} candidates above threshold, returning top {limit}")
    return hits[:limit]
