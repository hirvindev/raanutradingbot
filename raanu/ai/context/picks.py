"""
raanu.ai.context.picks — does a high score actually pay?
========================================================
The score-band table from :mod:`raanu.trading.picks_log`: every pick the
scheduled scans produced, and what it did 1, 5 and 20 trading days later
measured against SPY over the identical window.

Exposed as a TOOL. It answers one question, but it is the question the whole
ranking rests on, and the backtest says the answer is uncomfortable: raising
the score bar made results WORSE (alpha +3.20% at bar 60, +2.55% at 70,
-4.65% at 80). This is the live check on whether that still holds.

⚠️ ``verdict`` refuses to conclude below ~30 matured picks, and that refusal
is passed through rather than smoothed over. Handing a model a score-band
table built on nine picks invites exactly the over-reading this project keeps
having to walk back.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("raanu.ai.context.picks")

name = "picks"
required = False


def _collect() -> dict:
    from raanu.trading import picks_log
    data = picks_log.summary()
    return {
        "total_picks": data.get("total_picks"),
        "matured_5d": data.get("matured_5d"),
        "by_score_band": data.get("by_score_band"),
        "by_strategy": data.get("by_strategy"),
        "verdict": data.get("verdict"),
        "note": ("Returns are excess over SPY across the identical window, "
                 "measured from the pick day's close and never revised. "
                 "Backtest found the score does NOT rank: high-conviction "
                 "names underperformed marginal ones."),
    }


async def fetch() -> dict | None:
    try:
        return await asyncio.to_thread(_collect)
    except Exception as e:
        log.warning(f"[picks] unavailable: {e}")
        return None
