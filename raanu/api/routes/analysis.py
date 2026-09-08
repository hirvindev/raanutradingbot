"""raanu.api.routes.analysis — read-only queries over stored state.

These exist because the composite-key model makes them cheap. Before it, "the
trades I made in August" meant loading every trade ever recorded and filtering
in Python; it is now a key-range read, which is what makes it reasonable to
expose over HTTP at all.

All GET, all read-only, no money moves — the read passphrase is enough, no
trade PIN. ``tools/query_state.py`` is the same set of questions from a shell.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from fastapi import APIRouter, Query

from raanu import state
from raanu.state import keys

log = logging.getLogger("raanu.api.routes.analysis")

router = APIRouter()

# A page cap, so a wide-open range cannot pull the whole history into one
# response. The true count is reported alongside, so truncation is visible
# rather than silent — the same rule the scan shards follow.
_MAX_ROWS = 500


@router.get("/api/analysis/entities")
def analysis_entities():
    """What is stored, how many items, and how close any of it is to the
    400KB per-item ceiling that the old single-blob model kept running into."""
    from raanu.state.backends import MAX_ITEM_BYTES

    out = []
    for pk in keys.ALL_ENTITIES:
        records = state.query(pk)
        if not records:
            continue
        sizes = [state.estimate_size(pk, r.sk, r.data) for r in records]
        out.append({
            "entity": pk,
            "items": len(records),
            "total_bytes": sum(sizes),
            "largest_item_bytes": max(sizes),
            "pct_of_guard": round(100 * max(sizes) / MAX_ITEM_BYTES, 3),
        })
    return {"entities": out, "guard_bytes": MAX_ITEM_BYTES, "hard_limit_bytes": 409_600}


@router.get("/api/analysis/trades")
def analysis_trades(
    since: str | None = Query(None, description="inclusive sort-key lower bound, e.g. 2026-08-01"),
    until: str | None = None,
    strategy: str | None = None,
    action: str | None = Query(None, description="BUY or SELL"),
    limit: int = Query(_MAX_ROWS, le=_MAX_ROWS),
):
    """The trade log, filtered. ``since``/``until`` are key conditions, so they
    narrow what is read rather than what is returned."""
    filters = {}
    if strategy:
        filters["strategy"] = strategy
    if action:
        filters["action"] = action.upper()
    rows = state.query(keys.TRADE, sk_gte=since, sk_lte=until,
                       filters=filters or None, limit=limit)
    return {"count": len(rows), "truncated": len(rows) >= limit,
            "trades": [r.data for r in rows]}


@router.get("/api/analysis/pnl")
def analysis_pnl(since: str | None = None, until: str | None = None):
    """Realized P&L by strategy — the same numbers Kelly sizes from.

    Reports expectancy alongside win rate deliberately. Win rate on its own is
    misleading: it is trivially raised by taking profits earlier, and the
    highest-win-rate configuration ever backtested here lost money.
    """
    rows = state.query(keys.TRADE, sk_gte=since, sk_lte=until,
                       filters={"action": "SELL"})
    by = defaultdict(list)
    for r in rows:
        if r.data.get("realized_pnl") is not None:
            by[r.data.get("strategy") or "unknown"].append(float(r.data["realized_pnl"]))

    out = {}
    for strat, pnls in by.items():
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
        win_rate = len(wins) / len(pnls)
        out[strat] = {
            "closed_trades": len(pnls),
            "win_rate": round(100 * win_rate, 1),
            "net_pnl": round(sum(pnls), 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(-avg_loss, 2),
            "payoff": round(avg_win / avg_loss, 2) if avg_loss else None,
            # p*avgWin - q*avgLoss. This is the number that decides
            # profitability, not the win rate above it.
            "expectancy": round(win_rate * avg_win - (1 - win_rate) * avg_loss, 2),
        }
    return {"by_strategy": out, "closed_trades": sum(len(v) for v in by.values())}


@router.get("/api/analysis/scores")
def analysis_scores(since: str | None = None):
    """Forward returns by score band — does a higher score earn more?

    The useful question is monotonicity across bands, not what any single pick
    did. Note the backtest's ``--sweep-rank`` found the score does NOT rank on
    S3, so treat a flat or inverted result here as consistent with that rather
    than as a bug.
    """
    rows = [r.data for r in state.query(keys.PICK, sk_gte=since)]
    bands = ((90, 200, "90+"), (80, 90, "80-89"), (70, 80, "70-79"), (0, 70, "60-69"))

    out = {}
    for lo, hi, label in bands:
        sub = [r for r in rows if lo <= (r.get("score") or 0) < hi]
        if not sub:
            continue
        band = {"n": len(sub)}
        for d in (1, 5, 20):
            vals = [v for v in ((r.get("fwd") or {}).get(f"d{d}") for r in sub) if v is not None]
            spy = [v for v in ((r.get("spy") or {}).get(f"d{d}") for r in sub) if v is not None]
            band[f"d{d}"] = ({"n": len(vals), "avg": round(sum(vals) / len(vals), 2)}
                             if vals else None)
            if vals and spy:
                band[f"d{d}"]["spy"] = round(sum(spy) / len(spy), 2)
                band[f"d{d}"]["edge"] = round(sum(vals) / len(vals) - sum(spy) / len(spy), 2)
        out[label] = band

    matured = sum(1 for r in rows if (r.get("fwd") or {}).get("d5") is not None)
    return {
        "by_score_band": out,
        "total_picks": len(rows),
        "matured_5d": matured,
        "verdict": (f"Not enough data — {matured} picks have a 5-day result. "
                    "Needs ~30 before the bands mean anything."
                    if matured < 30 else
                    "Compare each band's avg against its spy column; a real "
                    "signal shows higher scores earning a bigger edge."),
    }
