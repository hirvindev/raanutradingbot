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

import logging
from datetime import UTC, date, datetime

from raanu import state
from raanu.state import keys

log = logging.getLogger("raanu.picks")

FORWARD_DAYS = (1, 5, 20)
MAX_PER_SCAN = 5          # the top few are what a person would actually act on
BANDS = ((90, 200, "90+"), (80, 90, "80-89"), (70, 80, "70-79"), (0, 70, "60-69"))

# A pick stops being backfilled once every forward window has had a chance to
# close. Without this, a row whose price history no longer covers its pick date
# stays "pending" forever and its ticker is re-downloaded on every daily
# backfill — the pending set only ever grew.
MATURE_AFTER_DAYS = 40


def _load_rows() -> list:
    """Every pick, oldest first."""
    return [r.data for r in state.query(keys.PICK)]


def _put(row: dict) -> None:
    state.put(keys.PICK, keys.pick_sk(row["date"], row["strategy"], row["ticker"]), row)


def record(strategy: str, picks: list) -> int:
    """Store today's picks for one strategy. Idempotent per (date, strategy).

    Re-recording the same day is a no-op rather than a duplicate: the scheduler
    runs two slots a day and both cache picks, and counting the same signal
    twice would quietly double its weight in every aggregate.
    """
    if not picks:
        return 0
    try:
        day = datetime.now(UTC).date().isoformat()
        # Idempotence is now a property of the key, not of a scan: the sort key
        # is (date, strategy, ticker), so re-recording the same day overwrites
        # in place instead of appending a duplicate. Still short-circuit, to
        # keep the "already recorded" case free of writes.
        if state.query(keys.PICK, sk_prefix=f"{day}#{strategy}#", limit=1):
            return 0
        n = 0
        for p in picks[:MAX_PER_SCAN]:
            if not p.get("ticker") or not p.get("score"):
                continue
            _put({
                "date": day,
                "ts": datetime.now(UTC).isoformat(),
                "strategy": strategy,
                "ticker": p["ticker"],
                "name": p.get("name"),
                "score": p["score"],
                "price_at_pick": p.get("price"),
                "reasons": p.get("reasons", [])[:6],
                "fwd": {},
                "spy": {},
                "matured": False,
            })
            n += 1
        log.info(f"[picks] recorded {n} {strategy.upper()} picks for {day}")
        return n
    except Exception as e:
        log.error(f"[picks] record failed: {e}")
        return 0


def attach_llm_verdict(verdict, candidates: dict) -> int:
    """Merge the advisor's verdict onto today's already-recorded pick rows.

    Rows are keyed ``(date, strategy, ticker)``, so this is an idempotent
    merge — re-running a slot overwrites in place rather than duplicating.

    This is the point of recording it at all. ``fill_forward_returns()`` will
    later attach what each pick actually did over 1/5/20 days against SPY, so
    the advisor becomes answerable to the same question the scores are:
    **did the picks it vetoed underperform the ones it approved, and were the
    days it stood down actually bad days?** Without this merge there is no way
    to find out, and an unfalsifiable gate is exactly what this log exists to
    prevent.

    Best-effort like everything else here: research bookkeeping must never
    break a trading slot.
    """
    try:
        day = datetime.now(UTC).date().isoformat()
        shared = {
            "llm_trade_today": bool(verdict.trade_today),
            "llm_regime": verdict.regime,
            "llm_market_summary": verdict.market_summary,
        }
        n = 0
        for strategy, picks in (candidates or {}).items():
            for pick in picks:
                ticker = pick.get("ticker")
                if not ticker:
                    continue
                row = state.get(keys.PICK, keys.pick_sk(day, strategy, ticker))
                if not row:
                    continue          # never recorded (below MAX_PER_SCAN)
                row.update(shared)
                decision = verdict.decision_for(strategy, ticker)
                if decision is not None:
                    row.update({
                        "llm_approve": decision.approve,
                        "llm_rank": decision.rank,
                        "llm_confidence": decision.confidence,
                        "llm_size_mult": decision.size_mult,
                        "llm_rationale": decision.rationale,
                        "llm_exit_plan": decision.exit_plan.as_stored() or None,
                    })
                _put(row)
                n += 1
        log.info(f"[picks] attached advisor verdict to {n} rows for {day}")
        return n
    except Exception as e:
        log.error(f"[picks] attach_llm_verdict failed: {e}")
        return 0


def fill_forward_returns() -> dict:
    """Backfill forward returns for anything old enough. Never revises a value."""
    from raanu.market.prices import batch_download

    # Only unmatured rows are candidates — a bounded query rather than a scan
    # of every pick ever recorded.
    rows = [r.data for r in state.query(keys.PICK, filters={"matured": False})]
    pending = [r for r in rows if any(f"d{d}" not in (r.get("fwd") or {}) for d in FORWARD_DAYS)]
    if not pending:
        return {"filled": 0, "pending": 0}

    tickers = sorted({r["ticker"] for r in pending} | {"SPY"})
    frames = batch_download(tickers, period="6mo")
    closes = {t: df["Close"].astype(float)
              for t, df in frames.items() if df is not None and not df.empty}

    filled = 0
    today = datetime.now(UTC).date()
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

        # Retire the row once every window has had time to close, whether or
        # not the data ever arrived. Otherwise a pick whose history no longer
        # reaches its own date is re-downloaded daily, forever.
        age = (today - date.fromisoformat(r["date"])).days
        complete = all(f"d{d}" in (r.get("fwd") or {}) for d in FORWARD_DAYS)
        if complete or age > MATURE_AFTER_DAYS:
            r["matured"] = True
        _put(r)

    still = sum(1 for r in rows if not r.get("matured"))
    log.info(f"[picks] filled {filled} forward returns, {still} still maturing")
    return {"filled": filled, "pending": still}


def _agg(rows: list, day: str) -> dict | None:
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
    rows = _load_rows()

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
    """Newest first. A bounded query — this used to load every pick ever
    recorded in order to return the last forty."""
    return [r.data for r in state.query(keys.PICK, descending=True, limit=limit)]
