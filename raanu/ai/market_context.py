"""
raanu.ai.market_context — what the broad market is doing right now
===================================================================
The hard numbers the advisor reasons from, before it goes looking for the
narrative. One Alpaca snapshots call covers the indices and all eleven sector
SPDRs; VIX comes from yfinance because Alpaca carries no index data.

Three readings per symbol, because they answer different questions:

  * **gap %**        open vs previous close — the overnight/pre-market move
  * **day %**        current vs previous close — where it stands now
  * **since-open %** current vs today's open — is the gap being bought or sold

The third is the one people forget, and it is often the most informative: a
-1.5% gap being steadily bought back is a very different tape from a -1.5% gap
still sliding, and they have opposite implications for a dip-buying strategy.

⚠️ This must NOT be sourced from ``raanu.market.cache``. Those bars freeze at
the first fetch of the ET day (4-day TTL), so an intraday regime read from
them would be stale by construction — warmed pre-open it has no bar for today
at all.
"""

from __future__ import annotations

import logging

log = logging.getLogger("raanu.ai.context")

# Broad market. IWM is included because small caps lead risk appetite in both
# directions and often diverge from the mega-cap indices.
BROAD = ("SPY", "QQQ", "IWM")

# All eleven sector SPDRs. Breadth across them is a better dislocation signal
# than any single index: SPY can be dragged by two mega-caps while the other
# nine sectors are fine, and that is emphatically not a market-wide selloff.
SECTORS = ("XLE", "XLF", "XLK", "XLV", "XLI",
           "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC")

VIX_TICKER = "^VIX"


def _pct(numerator: float, denominator: float) -> float | None:
    if not denominator:
        return None
    return round((numerator / denominator - 1) * 100, 2)


def _read(snapshot: dict) -> dict | None:
    """Reduce one Alpaca snapshot to the three percentages."""
    if not isinstance(snapshot, dict):
        return None
    daily = snapshot.get("dailyBar") or {}
    prev = snapshot.get("prevDailyBar") or {}
    try:
        open_px = float(daily.get("o") or 0)
        last_px = float(daily.get("c") or 0)
        prev_close = float(prev.get("c") or 0)
    except (TypeError, ValueError):
        return None
    if not (open_px and last_px and prev_close):
        return None
    return {
        "gap_pct": _pct(open_px, prev_close),
        "day_pct": _pct(last_px, prev_close),
        "since_open_pct": _pct(last_px, open_px),
        "price": round(last_px, 2),
    }


def _vix() -> dict | None:
    """VIX level against its own 20-day mean.

    The level alone says little — 18 is calm in one regime and elevated in
    another. The ratio to its recent average is what identifies a volatility
    *event*, which is the thing that should give a dip-buyer pause.
    """
    try:
        from raanu.market.prices import fetch_ohlc
        frame = fetch_ohlc(VIX_TICKER, period="3mo")
        if frame is None or frame.empty or "Close" not in frame:
            return None
        closes = frame["Close"].astype(float).dropna()
        if len(closes) < 21:
            return None
        level = float(closes.iloc[-1])
        mean20 = float(closes.iloc[-21:-1].mean())
        if mean20 <= 0:
            return None
        return {
            "level": round(level, 2),
            "mean_20d": round(mean20, 2),
            "vs_mean_pct": round((level / mean20 - 1) * 100, 1),
        }
    except Exception as e:
        log.warning(f"[context] VIX unavailable: {e}")
        return None


def snapshot() -> dict:
    """The market picture for one slot.

    Degrades rather than raises: a missing symbol, an unconfigured broker or a
    yfinance outage each remove part of the picture and leave the rest. The
    advisor is told what is missing via ``partial`` so it can weigh its own
    confidence instead of silently reasoning from a hole.
    """
    context: dict = {"broad": {}, "sectors": {}, "vix": None, "partial": []}

    try:
        from raanu.market.broker import get_snapshots
        raw = get_snapshots(list(BROAD) + list(SECTORS))
    except Exception as e:
        log.warning(f"[context] snapshots unavailable: {e}")
        raw = {}

    if not raw:
        context["partial"].append("no broker snapshots")

    for symbol in BROAD:
        reading = _read(raw.get(symbol) or {})
        if reading:
            context["broad"][symbol] = reading
        else:
            context["partial"].append(symbol)

    green = red = 0
    for symbol in SECTORS:
        reading = _read(raw.get(symbol) or {})
        if not reading:
            context["partial"].append(symbol)
            continue
        context["sectors"][symbol] = reading
        day = reading.get("day_pct")
        if day is None:
            continue
        if day >= 0:
            green += 1
        else:
            red += 1

    context["breadth"] = {
        "sectors_green": green,
        "sectors_red": red,
        "sectors_reporting": green + red,
    }

    vix = _vix()
    if vix:
        context["vix"] = vix
    else:
        context["partial"].append("VIX")

    return context


def headline(context: dict) -> str:
    """One line for logs and Telegram — the picture at a glance."""
    spy = (context.get("broad") or {}).get("SPY") or {}
    breadth = context.get("breadth") or {}
    vix = context.get("vix") or {}
    bits = []
    if spy.get("day_pct") is not None:
        bits.append(f"SPY {spy['day_pct']:+.2f}%")
    if spy.get("gap_pct") is not None:
        bits.append(f"gap {spy['gap_pct']:+.2f}%")
    if breadth.get("sectors_reporting"):
        bits.append(f"breadth {breadth['sectors_green']}/{breadth['sectors_reporting']} green")
    if vix.get("level") is not None:
        bits.append(f"VIX {vix['level']} ({vix['vs_mean_pct']:+.0f}% vs 20d)")
    return " | ".join(bits) or "no market data"
