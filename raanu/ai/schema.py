"""
raanu.ai.schema — the advisor's response contract
==================================================
Pydantic models, so the same declaration is both the JSON schema handed to the
model and the validator of what comes back. ``pydantic`` is already a
dependency, so this costs nothing and removes a hand-written schema that would
otherwise drift from the code that reads it.

**The ``Field`` bounds are the enforcement.** The prompt asks the model to stay
inside them; these make it true. Two in particular are load-bearing:

  * ``size_mult`` is capped at 1.0 — the advisor may shrink a position or
    remove it, never inflate one. Increasing exposure has exactly one lever
    (``budget_pct``) and that lever is bounded by the cash reserve.
  * every ``ExitPlan`` bound sits inside the range the backtester actually
    explored, so a plan can pick a different stop but not an untested one.

And :meth:`SlotVerdict.approved_for` drops any ticker that was not in the
candidate list it was given. That drop — not the system prompt — is what makes
"the advisor cannot invent a buy" a property of the code.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# The three strategies the advisor allocates between. Kept here rather than
# imported from raanu.strategies so the response contract does not depend on
# the scanning package.
STRATEGIES = ("s1", "s2", "s3")


class ExitPlan(BaseModel):
    """Per-trade exit rules, replacing the per-strategy defaults.

    ``None`` means "use the strategy default" — an absent field is not the
    same as a chosen value, and the exit engine must be able to tell them
    apart. The bounds bracket the backtested range (stops were swept at
    2.5-3.0x ATR; 1.5-5.0 allows a real choice without leaving the map).
    """

    stop_atr_mult: float | None = Field(
        default=None, ge=1.5, le=5.0,
        description="Stop distance as a multiple of ATR(14) at entry.")
    trail_activate_atr: float | None = Field(
        default=None, ge=1.0, le=4.0,
        description="Arm the trailing stop once up this many ATR.")
    trail_atr_mult: float | None = Field(
        default=None, ge=1.0, le=3.0,
        description="Exit on giving back this many ATR from the peak.")
    ladder: Literal["strategy_default", "off", "standard"] = Field(
        default="strategy_default",
        description=(
            "Profit ladder. NOT universally good: it helped S2 "
            "(+13.26%->+15.55%) and hurt S3 (+33.89%->+22.34%) by booking "
            "winners before they matured. Prefer strategy_default."))
    note: str = Field(default="", max_length=200,
                      description="Why these exits for this trade.")

    def is_empty(self) -> bool:
        """True when nothing was chosen — the strategy defaults apply whole."""
        return (self.stop_atr_mult is None
                and self.trail_activate_atr is None
                and self.trail_atr_mult is None
                and self.ladder == "strategy_default")

    def as_stored(self) -> dict:
        """The compact form persisted with the position. Omits unset fields so
        a stored plan reads as exactly what was decided."""
        out = {}
        for key in ("stop_atr_mult", "trail_activate_atr", "trail_atr_mult"):
            value = getattr(self, key)
            if value is not None:
                out[key] = float(value)
        if self.ladder != "strategy_default":
            out["ladder"] = self.ladder
        if self.note:
            out["note"] = self.note
        return out


class CandidateDecision(BaseModel):
    """One verdict on one quant-surfaced candidate."""

    ticker: str
    strategy: str
    approve: bool
    rank: int = Field(
        default=99, ge=1,
        description="1 = best of the day, ranked across ALL strategies.")
    confidence: float = Field(ge=0, le=1)
    size_mult: float = Field(
        default=1.0, ge=0, le=1,
        description="Multiplier on the sized position. Trim only; 1.0 = full.")
    exit_plan: ExitPlan = Field(default_factory=ExitPlan)
    rationale: str = Field(default="", max_length=240)


class SlotVerdict(BaseModel):
    """The advisor's whole answer for one execution slot."""

    trade_today: bool = Field(
        description=("False stands the whole slot down. Reserve it for a "
                     "genuine dislocation, not ordinary weakness — S1 and S3 "
                     "are dip-buyers and ordinary red is their entry."))
    regime: Literal["risk_on", "neutral", "risk_off"]
    market_summary: str = Field(
        max_length=600,
        description="What the tape and the news say. Names the reason when "
                    "trade_today is false.")
    budget_pct: dict[str, float] | None = Field(
        default=None,
        description="Per-strategy share of the deployable budget, e.g. "
                    '{"s1": 30, "s2": 20, "s3": 50}. Must sum to <= 100.')
    decisions: list[CandidateDecision] = Field(default_factory=list)

    # ── consumption helpers ──────────────────────────────────────────────────

    def decision_for(self, strategy: str, ticker: str) -> CandidateDecision | None:
        want = (strategy.lower(), ticker.upper())
        for d in self.decisions:
            if (d.strategy.lower(), d.ticker.upper()) == want:
                return d
        return None

    def approved_for(self, strategy: str, picks: list[dict],
                     *, apply_exits: bool = True) -> list[dict]:
        """The approved subset of ``picks``, in the advisor's rank order.

        Annotates each survivor with ``_llm_*`` fields for the executor. A
        ticker with no decision, or a decision that was not approved, is
        dropped — and a decision naming a ticker that is not in ``picks`` is
        ignored entirely, which is where "cannot invent a buy" is enforced.
        """
        out: list[tuple[int, dict]] = []
        for pick in picks:
            ticker = str(pick.get("ticker") or "")
            if not ticker:
                continue
            decision = self.decision_for(strategy, ticker)
            if decision is None or not decision.approve:
                continue
            annotated = dict(pick)
            annotated["_llm_size_mult"] = max(0.0, min(1.0, decision.size_mult))
            annotated["_llm_rationale"] = decision.rationale
            annotated["_llm_confidence"] = decision.confidence
            annotated["_llm_rank"] = decision.rank
            annotated["_llm_exit_plan"] = (
                decision.exit_plan.as_stored() if apply_exits else {})
            out.append((decision.rank, annotated))
        out.sort(key=lambda pair: pair[0])
        return [pick for _, pick in out]

    def budget_share(self, strategy: str, *, fallback: float,
                     max_share: float) -> float:
        """This strategy's percentage of the deployable budget.

        Falls back to the configured ``CASH_SHARE_*`` whenever the advisor did
        not supply a usable split — a malformed allocation must not fail the
        slot, and it must not silently mean "zero".
        """
        weights = self.budget_pct or {}
        try:
            values = {k.lower(): float(v) for k, v in weights.items()}
        except (TypeError, ValueError):
            return fallback
        if not values or any(v < 0 for v in values.values()):
            return fallback
        if sum(values.values()) > 100.0001:
            return fallback
        share = values.get(strategy.lower())
        if share is None:
            return fallback
        # Capped even when the advisor's own numbers are internally valid:
        # concentration is bounded by policy, not by the model's restraint.
        return min(share, max_share)
