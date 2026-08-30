"""
raanu.strategies — the strategy registry
=========================================
One place that answers "what strategies exist, how does each score a
ticker, and what makes a result worth showing or worth buying".

Before this, those answers were copy-pasted across four near-identical scan
loops (``find_top_picks``, ``find_top_picks_s2``, ``find_top_picks_s3`` and
the SSE route's inline loop), each with its own slightly different
thresholds. Adding a strategy meant finding and editing all four; changing a
threshold in one and not the others produced exactly the kind of silent
divergence this project has been bitten by before.

Two distinct bars, deliberately kept apart:

  * ``surfaces`` — is this worth putting on the Live Signals screen? Stricter
    for S1/S2, which surface only high-conviction setups.
  * ``tradable`` — is this a candidate the auto-trader may consider? Looser
    on score, because the trader then applies its own MIN_SIGNAL_SCORE gate
    on top; the structural flag (uptrend / stage-2 / leader-dip) is the part
    that must hold.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from raanu.strategies.breakout import score_from_df_s2
from raanu.strategies.leader_dip import score_from_df_s3
from raanu.strategies.pullback import score_from_df

# The structural precondition each strategy sets on its own result dict.
# A result failing this is not a weak signal, it is the wrong setup entirely.
_STRUCTURE_KEY = {"s1": "uptrend", "s2": "stage2", "s3": "leader_dip"}


def _s1_surfaces(r: dict) -> bool:
    # High conviction only: an uptrend alone is not enough to put a name on
    # the screen — it also has to not be overbought, have MACD confirming,
    # and actually be beating the market.
    return (r.get("score", 0) >= 70
            and r.get("rsi", 50) <= 68
            and r.get("macd", 0) >= r.get("macd_signal", 0)
            and (r.get("rel_strength") or 0) > 0
            and (r.get("mom_3m") or 0) > 0)


def _s2_surfaces(r: dict) -> bool:
    return r.get("score", 0) >= 70 and (r.get("rel_strength") or 0) > 0


def _s3_surfaces(r: dict) -> bool:
    # S3 surfaces at 60, not 70. It is the only strategy profitable in both
    # halves of the backtest, and --sweep-rank showed raising the score bar
    # makes results WORSE (alpha +3.20% -> -4.65% going 60 -> 80), so a
    # higher bar here would hide the best-validated signals in the project.
    return r.get("score", 0) >= 60


@dataclass(frozen=True)
class Strategy:
    key: str
    label: str
    score: Callable[..., dict]
    _surfaces: Callable[[dict], bool]

    @property
    def structure_key(self) -> str:
        return _STRUCTURE_KEY[self.key]

    def _structurally_ok(self, r: dict) -> bool:
        return bool(r.get("ok") and r.get(self.structure_key))

    def surfaces(self, r: dict) -> bool:
        """Worth showing on Live Signals."""
        return self._structurally_ok(r) and self._surfaces(r)

    def tradable(self, r: dict) -> bool:
        """Worth passing to the auto-trader as a candidate."""
        return self._structurally_ok(r) and r.get("score", 0) >= 60


REGISTRY: dict[str, Strategy] = {
    "s1": Strategy("s1", "S1 Pullback", score_from_df, _s1_surfaces),
    "s2": Strategy("s2", "S2 Breakout", score_from_df_s2, _s2_surfaces),
    "s3": Strategy("s3", "S3 Leader Dip", score_from_df_s3, _s3_surfaces),
}

ALL_KEYS = tuple(REGISTRY)


def get(key: str) -> Strategy:
    return REGISTRY[key]


__all__ = ["ALL_KEYS", "REGISTRY", "Strategy", "get"]
