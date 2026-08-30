"""
kelly.py — Kelly Criterion position sizing
===========================================
    f* = (b*p - q) / b

    p  win rate            q  1 - p
    b  payoff ratio        =  average win / average loss

f* is the fraction of capital to put at risk per trade for maximum long-run
geometric growth. Two properties matter more than the formula:

  1. **Negative f* means no edge.** The correct size is zero. This module
     returns 0.0 and the caller must stand aside rather than trade smaller.

  2. **f* is only as good as p and b.** Both are estimated from a finite trade
     history, and Full Kelly assumes they are exact. They never are, so we
     scale by KELLY_FRACTION (default 0.25 — Quarter Kelly) and refuse to size
     at all below MIN_SAMPLE closed trades.

The output is a *risk* fraction, not a position size: it is the share of equity
lost if the stop is hit. Convert to shares with:

    qty = (equity * risk_fraction) / stop_distance_per_share

That is what makes a high-ATR name automatically take a small position — its
stop distance is wide, so the same risk budget buys fewer shares.
"""

from __future__ import annotations

import logging
import os
import statistics as st
from collections.abc import Iterable
from dataclasses import dataclass

log = logging.getLogger("raanu.kelly")


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# Quarter Kelly by default: retains a large share of the growth rate at roughly
# a quarter of the drawdown, and stays sane when p and b are mis-estimated.
KELLY_FRACTION  = _float_env("KELLY_FRACTION", 0.25)
MIN_SAMPLE      = _int_env("KELLY_MIN_SAMPLE", 30)
MAX_RISK_PCT    = _float_env("KELLY_MAX_RISK_PCT", 2.0)   # hard ceiling per trade
FALLBACK_RISK_PCT = _float_env("KELLY_FALLBACK_RISK_PCT", 0.5)


@dataclass
class KellyResult:
    risk_pct: float          # % of equity to risk on the next trade
    full_kelly_pct: float    # unscaled f*, for reporting
    win_rate: float
    payoff_b: float
    sample: int
    reason: str

    @property
    def tradeable(self) -> bool:
        return self.risk_pct > 0


def kelly_f(win_rate: float, payoff_b: float) -> float:
    """Raw Kelly fraction. Negative result means the edge is negative."""
    if payoff_b <= 0:
        return 0.0
    return (payoff_b * win_rate - (1.0 - win_rate)) / payoff_b


def from_pnls(pnls: Iterable[float], fraction: float | None = None) -> KellyResult:
    """
    Compute a sized risk budget from a sequence of realized trade P&Ls.

    Returns risk_pct = 0 whenever we should not be trading: too few samples to
    estimate from, no losses (so b is undefined), or a negative edge.
    """
    pnls = [float(p) for p in pnls]
    frac = KELLY_FRACTION if fraction is None else fraction

    if len(pnls) < MIN_SAMPLE:
        return KellyResult(
            risk_pct=FALLBACK_RISK_PCT, full_kelly_pct=0.0, win_rate=0.0,
            payoff_b=0.0, sample=len(pnls),
            reason=f"only {len(pnls)} closed trades (<{MIN_SAMPLE}) — using fallback {FALLBACK_RISK_PCT}% risk",
        )

    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    if not wins or not losses:
        return KellyResult(
            risk_pct=FALLBACK_RISK_PCT, full_kelly_pct=0.0,
            win_rate=len(wins) / len(pnls), payoff_b=0.0, sample=len(pnls),
            reason="no wins or no losses yet — payoff ratio undefined",
        )

    p = len(wins) / len(pnls)
    b = st.mean(wins) / abs(st.mean(losses))
    f = kelly_f(p, b)

    if f <= 0:
        return KellyResult(
            risk_pct=0.0, full_kelly_pct=f * 100, win_rate=p, payoff_b=b,
            sample=len(pnls),
            reason=f"negative edge (f*={f*100:.1f}%) — stand aside, do not size down",
        )

    risk = min(f * frac * 100, MAX_RISK_PCT)
    return KellyResult(
        risk_pct=round(risk, 3), full_kelly_pct=round(f * 100, 2),
        win_rate=round(p, 4), payoff_b=round(b, 3), sample=len(pnls),
        reason=f"f*={f*100:.1f}% x {frac:g} = {f*frac*100:.2f}% risk"
               + (f" (capped at {MAX_RISK_PCT}%)" if f * frac * 100 > MAX_RISK_PCT else ""),
    )


def from_trade_log(strategy: str | None = None,
                   fraction: float | None = None) -> KellyResult:
    """
    Compute Kelly from the live trade log's realized SELL records.

    Deliberately reads ONLY the trade log, not the full Alpaca fill history.
    Those older round-trips were taken under a fixed 3% stop, which produced a
    different P&L distribution entirely (median hold 2.1 days, 70% of exits at
    the stop). Estimating p and b from them would size the new ATR-stop
    configuration off the statistics of the old broken one. The sample rebuilds
    from scratch, and MIN_SAMPLE holds sizing at FALLBACK_RISK_PCT until there
    are enough trades under the current rules to estimate from.
    """
    from raanu.trading.trader import get_trader

    pnls = [
        float(t["realized_pnl"])
        for t in get_trader().tradelog.data.get("trades", [])
        if t.get("action") == "SELL" and t.get("realized_pnl") is not None
        and (strategy is None or t.get("strategy") == strategy)
    ]
    return from_pnls(pnls, fraction)


def shares_for(equity: float, risk_pct: float, entry_price: float,
               stop_price: float, max_position_pct: float = 20.0) -> float:
    """
    Convert a risk budget into a share count.

        risk_budget = equity * risk_pct / 100
        qty         = risk_budget / (entry - stop)

    Capped so a very tight stop cannot swallow the account.
    """
    stop_distance = entry_price - stop_price
    if stop_distance <= 0 or entry_price <= 0 or risk_pct <= 0:
        return 0.0
    qty = (equity * risk_pct / 100) / stop_distance
    max_qty = (equity * max_position_pct / 100) / entry_price
    return max(0.0, min(qty, max_qty))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Worked example from the Kelly literature: p=0.55, b=1.5 -> f*=25%
    print(f"p=0.55 b=1.5  ->  f* = {kelly_f(0.55, 1.5)*100:.1f}%  (expect 25.0%)")
    print(f"p=0.50 b=2.0  ->  f* = {kelly_f(0.50, 2.0)*100:.1f}%  (expect 25.0%)")
    # Edge flips sign at b = q/p, which is 1.222 when p=0.45 — NOT at b=1.5.
    print(f"p=0.45 b=1.5  ->  f* = {kelly_f(0.45, 1.5)*100:.1f}%  (expect +8.3%, still positive)")
    print(f"p=0.45 b=1.222 ->  f* = {kelly_f(0.45, 1.222)*100:.1f}%  (expect ~0% — break-even)")
    print(f"p=0.45 b=0.97 ->  f* = {kelly_f(0.45, 0.97)*100:.1f}%  (expect -11.7%)")

    import random
    random.seed(7)
    good = [random.choice([150.0] * 55 + [-100.0] * 45) for _ in range(400)]
    bad  = [random.choice([100.0] * 30 + [-100.0] * 70) for _ in range(400)]
    for name, series in [("edge +", good), ("edge -", bad)]:
        r = from_pnls(series)
        print(f"\n{name}: risk {r.risk_pct}%  f*={r.full_kelly_pct}%  "
              f"p={r.win_rate}  b={r.payoff_b}  n={r.sample}\n  {r.reason}")
