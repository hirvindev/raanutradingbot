"""
picks_log.py — did the bot's picks actually work?
==================================================
Records every pick the scheduled scans produce and, on later runs, fills in what
each name did 1, 5 and 20 trading days on. Answers the question the Signals tab
raises but cannot settle: **does a score of 90 mean anything?**

This is deliberately separate from `trades_log.json`, which records what was
BOUGHT. Most picks are never bought — the weekly limit, the cash share, or a
duplicate holding stops them — so judging the engines by the trade log only ever
shows the subset that survived the gates. A signal's quality and a portfolio's
outcome are different questions and want different logs.

Two disciplines carried over from earlier work in this project:

  * **A baseline is recorded alongside.** "+2% in five days" is unreadable if
    SPY did +2.5% over the same window. Every run stores SPY's forward returns
    from the same date, so the comparison is like for like.
  * **Returns are measured from the pick day's CLOSE and never revised.** That
    is when the signal was known; measuring from an earlier price would be
    lookahead, the same rule backtest.py follows.

Scores are bucketed (60-69, 70-79, 80-89, 90+) because the useful question is
monotonicity — do higher scores earn higher forward returns — not what any
single pick did.

Nothing here touches the trading path. It is read-only research that happens to
run on live data.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from datadir import state_load, state_save

log = logging.getLogger("raanu.picks")

STATE_KEY = "picks_log.json"

FORWARD_DAYS = (1, 5, 20)
MAX_PER_SCAN = 5          # the top few are what a person would actually act on
BANDS = ((90, 200, "90+"), (80, 90, "80-89"), (70, 80, "70-79"), (0, 70, "60-69"))


def _load() -> dict:
    return state_load(STATE_KEY, default={"picks": []})


def _save(data: dict):
    state_save(STATE_KEY, data)


def record(strategy: str, picks: list) -> int:
    """Store today's picks for one strategy. Idempotent per (date, strategy).

    Re-recording the same day is a no-op rather than a duplicate: the scheduler
    runs two slots a day and both cache picks, and counting the same signal
    twice would quietly double its weight in every aggregate.
    """
    if not picks:
        return 0
    try:
        data = _load()
        day = datetime.now(timezone.utc).date().isoformat()
        have = {(p["date"], p["strategy"]) for p in data["picks"]}
        if (day, strategy) in have:
            return 0
        n = 0
        for p in picks[:MAX_PER_SCAN]:
            if not p.get("ticker") or not p.get("score"):
                continue
            data["picks"].append({
                "date": day,
                "ts": datetime.now(timezone.utc).isoformat(),
                "strategy": strategy,
                "ticker": p["ticker"],
                "name": p.get("name"),
                "score": p["score"],
                "price_at_pick": p.get("price"),
                "reasons": p.get("reasons", [])[:6],
                "fwd": {},
            })
            n += 1
        _save(data)
        log.info(f"[picks] recorded {n} {strategy.upper()} picks for {day}")
        return n
    except Exception as e:
        log.error(f"[picks] record failed: {e}")
        return 0


def fill_forward_returns() -> dict:
    """Backfill forward returns for anything old enough. Never revises a value."""
    from strategy import batch_download

    data = _load()
    rows = data.get("picks", [])
    pending = [r for r in rows if any(f"d{d}" not in (r.get("fwd") or {}) for d in FORWARD_DAYS)]
    if not pending:
        return {"filled": 0, "pending": 0}

    tickers = sorted({r["ticker"] for r in pending} | {"SPY"})
    frames = batch_download(tickers, period="6mo")
    closes = {t: df["Close"].astype(float)
              for t, df in frames.items() if df is not None and not df.empty}

    filled = 0
    for r in rows:
        s = closes.get(r["ticker"])
        spy = closes.get("SPY")
        if s is None or s.empty:
            continue
        idx = s.index[s.index.astype(str).str[:10] <= r["date"]]
        if len(idx) == 0:
            continue
        i0 = s.index.get_loc(idx[-1])
        base = float(s.iloc[i0])
        if base <= 0:
            continue
        fwd = r.setdefault("fwd", {})
        bwd = r.setdefault("spy", {})
        for d in FORWARD_DAYS:
            k = f"d{d}"
            if fwd.get(k) is not None:
                continue                       # written once, never revised
            if i0 + d < len(s):
                fwd[k] = round((float(s.iloc[i0 + d]) / base - 1) * 100, 2)
                filled += 1
                # Same window on SPY, so a cohort number can be read against it.
                if spy is not None and not spy.empty:
                    sidx = spy.index[spy.index.astype(str).str[:10] <= r["date"]]
                    if len(sidx):
                        j0 = spy.index.get_loc(sidx[-1])
                        if j0 + d < len(spy) and float(spy.iloc[j0]) > 0:
                            bwd[k] = round((float(spy.iloc[j0 + d]) / float(spy.iloc[j0]) - 1) * 100, 2)

    _save(data)
    still = sum(1 for r in rows if any(f"d{d}" not in (r.get("fwd") or {}) for d in FORWARD_DAYS))
    log.info(f"[picks] filled {filled} forward returns, {still} still maturing")
    return {"filled": filled, "pending": still}


def _agg(rows: list, day: str) -> Optional[dict]:
    vals = [r["fwd"][day] for r in rows if (r.get("fwd") or {}).get(day) is not None]
    spy = [r["spy"][day] for r in rows if (r.get("spy") or {}).get(day) is not None]
    if not vals:
        return None
    out = {"n": len(vals),
           "avg": round(sum(vals) / len(vals), 2),
           "win_rate": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1)}
    if spy:
        out["spy"] = round(sum(spy) / len(spy), 2)
        out["edge"] = round(out["avg"] - out["spy"], 2)
    return out


def summary() -> dict:
    data = _load()
    rows = data.get("picks", [])

    by_strategy, by_band = {}, {}
    for s in ("s1", "s2", "s3"):
        sub = [r for r in rows if r["strategy"] == s]
        if sub:
            by_strategy[s] = {f"d{d}": _agg(sub, f"d{d}") for d in FORWARD_DAYS}
    for lo, hi, label in BANDS:
        sub = [r for r in rows if lo <= r["score"] < hi]
        if sub:
            by_band[label] = {f"d{d}": _agg(sub, f"d{d}") for d in FORWARD_DAYS}

    matured = sum(1 for r in rows if (r.get("fwd") or {}).get("d5") is not None)
    return {
        "total_picks": len(rows),
        "matured_5d": matured,
        "first": rows[0]["date"] if rows else None,
        "last": rows[-1]["date"] if rows else None,
        "by_strategy": by_strategy,
        "by_score_band": by_band,
        # Said out loud rather than left to be inferred — a handful of picks
        # cannot separate an edge from noise, and reading one into them is
        # exactly what this log exists to prevent.
        "verdict": (f"Not enough data — {matured} picks have a 5-day result. "
                    "Needs ~30 before the score bands mean anything."
                    if matured < 30 else
                    "Compare each band's avg against its SPY column; a real "
                    "signal shows higher scores earning a bigger edge."),
    }


def recent(limit: int = 40) -> list:
    return _load().get("picks", [])[-limit:][::-1]
