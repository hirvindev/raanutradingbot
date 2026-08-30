"""raanu.api.routes.strategy"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from raanu.api.routes.account import _annotate_names, _strategy_resolver
from raanu.market.rest import alpaca_get
from raanu.trading.reports import _booked_totals, match_closed_trades
from raanu.trading.schedule import _load_picks, _load_picks_s2, _load_picks_s3
from raanu.trading.trader import get_trader

log = logging.getLogger("raanu.api.routes.strategy")

router = APIRouter()


@router.get("/api/strategy/compare")
async def strategy_compare():
    """Return trade performance split by strategy for the dashboard Strategy tab."""
    all_trades = get_trader().tradelog.data.get("trades", [])

    # Real P&L comes from Alpaca fills, not from our own buy log.
    try:
        closed_orders = await alpaca_get("/orders", params={
            "status": "closed", "limit": "500", "direction": "desc"
        })
    except Exception:
        closed_orders = []

    # Tag each order with the strategy that opened the position, as of the
    # order's own time — see _strategy_resolver().
    resolve_strat = _strategy_resolver()
    for o in closed_orders:
        o["strategy"] = resolve_strat(o.get("symbol"),
                                      o.get("filled_at") or o.get("created_at"))

    round_trips = await _annotate_names(match_closed_trades(closed_orders))

    def _strategy_stats(strat: str) -> dict:
        trades = [t for t in all_trades if t.get("strategy") == strat]
        rts    = [r for r in round_trips if r["strategy"] == strat]

        wins    = [r for r in rts if r["pnl"] > 0]
        losses  = [r for r in rts if r["pnl"] <= 0]
        net_pnl = sum(r["pnl"] for r in rts)

        return {
            "strategy": strat,
            "label": {"s1": "S1 Pullback", "s2": "S2 Breakout", "s3": "S3 Leader Dip"}[strat],
            "total_trades":   len(trades),
            "closed_trades":  len(rts),
            "profitable":     len(wins),
            "loss_making":    len(losses),
            "win_rate":       round(len(wins) / len(rts) * 100, 1) if rts else 0,
            "net_pnl":        round(net_pnl, 2),
            "avg_return_pct": round(sum(r["pct"] for r in rts) / len(rts), 2) if rts else 0,
            "trades":         trades[-50:],
            "closed":         rts[-50:],
        }

    # Picks caches
    s1_picks = _load_picks()
    s2_picks = _load_picks_s2()

    s3_picks = _load_picks_s3()
    return {
        "s1": _strategy_stats("s1"),
        "s2": _strategy_stats("s2"),
        "s3": _strategy_stats("s3"),
        "s1_picks": s1_picks.get("picks", []) if s1_picks else [],
        "s2_picks": s2_picks.get("picks", []) if s2_picks else [],
        "s3_picks": s3_picks.get("picks", []) if s3_picks else [],
        "s1_scanned_at": s1_picks.get("scanned_at") if s1_picks else None,
        "s2_scanned_at": s2_picks.get("scanned_at") if s2_picks else None,
        "s3_scanned_at": s3_picks.get("scanned_at") if s3_picks else None,
        # Booked profit and loss as separate figures, over EVERY round-trip and
        # not just the attributed ones. Summing the per-strategy blocks would
        # silently drop the untagged history — most of this account's realized
        # P&L — and report a total that disagrees with Alpaca.
        "totals": _booked_totals(round_trips),
    }
