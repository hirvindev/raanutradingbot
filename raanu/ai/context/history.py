"""
raanu.ai.context.history — how recent trades have actually gone
===============================================================
Closed round-trips over a window, aggregated the way this project has learned
to read them.

Exposed as a TOOL rather than pre-assembled: it is only sometimes decisive
("has S2 actually been working, or am I about to fund a losing streak"), and
on a slot where every candidate is obvious it is pure token cost.

⚠️ Reports **expectancy**, not just win rate, and says so in the payload. The
single most expensive misreading available here is that a higher win rate is
better: the highest-win-rate configuration ever tested in this project
(68.0%) LOST money, and the profit ladder raises S3's win rate from 59.4% to
68.7% while collapsing payoff 0.93 -> 0.58. A model handed a bare win rate
will reach for it; handed expectancy alongside, it has the number that
actually decides profitability.

⚠️ Reads the TRADE log only, never Alpaca's full fill history — the same rule
``kelly.py`` follows. Older round-trips were taken under the broken 3% stop
and describe a different P&L distribution entirely.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

log = logging.getLogger("raanu.ai.context.history")

name = "history"
required = False

DEFAULT_DAYS = 30


def _agg(trips: list[dict]) -> dict:
    """Win rate, payoff and expectancy for one bucket of round-trips."""
    if not trips:
        return {"n": 0}
    pnls = [float(t.get("pnl") or 0) for t in trips]
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p < 0]
    n = len(pnls)
    win_rate = len(wins) / n if n else 0.0
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    # p*avgWin - q*avgLoss. The number that decides profitability, and the
    # reason win_rate is never reported on its own.
    expectancy = win_rate * avg_win - (1 - win_rate) * avg_loss
    return {
        "n": n,
        "win_rate_pct": round(win_rate * 100, 1),
        "avg_win_usd": round(avg_win, 2),
        "avg_loss_usd": round(avg_loss, 2),
        "payoff_ratio": round(avg_win / avg_loss, 2) if avg_loss else None,
        "expectancy_usd": round(expectancy, 2),
        "total_pnl_usd": round(sum(pnls), 2),
    }


def _collect(days: int) -> dict:
    from raanu.trading.trader import get_trader
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    trades = [t for t in get_trader().tradelog.all_trades()
              if (t.get("timestamp") or "") >= cutoff]

    sells = [t for t in trades if (t.get("action") or "").upper() == "SELL"]
    buys = [t for t in trades if (t.get("action") or "BUY").upper() == "BUY"]

    by_strategy = {}
    for strategy in ("s1", "s2", "s3", "unknown"):
        sub = [t for t in sells if (t.get("strategy") or "unknown").lower() == strategy]
        if sub:
            by_strategy[strategy] = _agg(sub)

    exits: dict[str, int] = {}
    for t in sells:
        reason = (t.get("reason") or t.get("exit_reason") or "unknown")
        exits[reason] = exits.get(reason, 0) + 1

    return {
        "window_days": days,
        "buys": len(buys),
        "closed_round_trips": len(sells),
        "overall": _agg(sells),
        "by_strategy": by_strategy,
        "exit_reasons": exits,
        "note": ("expectancy_usd (p*avgWin - q*avgLoss) decides profitability, "
                 "not win_rate_pct. The highest-win-rate configuration ever "
                 "tested in this project lost money."),
        # A handful of trades cannot separate edge from noise, and this
        # project has been burned by concluding otherwise before.
        "sample_is_meaningful": len(sells) >= 30,
    }


async def fetch(days: int = DEFAULT_DAYS) -> dict | None:
    try:
        return await asyncio.to_thread(_collect, days)
    except Exception as e:
        log.warning(f"[history] unavailable: {e}")
        return None
