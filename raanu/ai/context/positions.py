"""
raanu.ai.context.positions — what the book already holds
=========================================================
Exposed as a TOOL rather than pre-assembled. The bare list of held symbols is
capacity information and rides along with the budget (a name already held
cannot be bought again, so ranking it wastes a slot); this is the detail —
size, unrealised P&L, concentration — which only matters when the advisor is
weighing whether to add correlated exposure.
"""

from __future__ import annotations

import logging

log = logging.getLogger("raanu.ai.context.positions")

name = "positions"
required = False


async def fetch() -> dict | None:
    from raanu.market.rest import alpaca_get
    try:
        rows = await alpaca_get("/positions")
    except Exception as e:
        log.warning(f"[positions] unavailable: {e}")
        return None
    if not isinstance(rows, list):
        return None

    out, total = [], 0.0
    for r in rows:
        try:
            value = float(r.get("market_value") or 0)
        except (TypeError, ValueError):
            value = 0.0
        total += abs(value)
        out.append({
            "ticker": r.get("symbol"),
            "market_value": round(value, 2),
            "unrealized_pl_pct": _pct(r.get("unrealized_plpc")),
        })
    out.sort(key=lambda p: -(p["market_value"] or 0))

    # Share of the book in its largest single name. The one diversification
    # result this project has actually measured says alpha improved from 4 to
    # 8 to 15 positions, so concentration is the number worth surfacing.
    top = (out[0]["market_value"] / total * 100) if out and total else 0.0
    return {"count": len(out), "total_value": round(total, 2),
            "largest_position_pct": round(top, 1), "positions": out}


def _pct(raw) -> float | None:
    try:
        return round(float(raw) * 100, 2)
    except (TypeError, ValueError):
        return None
