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
    (``usd_to_deploy``) and that lever is clamped to what the weekly budget
    actually has left, computed from the trade log rather than from anything
    the model said.
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
    usd_to_deploy: float | None = Field(
        default=None, ge=0,
        description=(
            "Dollars of the REMAINING weekly budget to commit in this slot. "
            "Omit to let the approved trades size themselves against what is "
            "left. Use it to pace: approving two trades today out of seven "
            "for the week is a legitimate answer, and so is holding the whole "
            "budget back for a better tape."))
    pacing_note: str = Field(
        default="", max_length=300,
        description=("Why this many trades now rather than more or fewer. "
                     "Required reading when fewer are approved than the "
                     "weekly budget allows."))
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

    def slot_budget(self, *, weekly_usd_left: float) -> float:
        """Dollars this slot may commit.

        The advisor may spend LESS than what the week has left — pacing is a
        real decision, and "hold the budget for a better tape" is one of the
        few things a model can contribute that a numeric rule cannot. It may
        never spend MORE: ``weekly_usd_left`` is computed from the trade log,
        not from anything the model said, so this clamps rather than trusts.

        An absent or unusable value means "use what is left", not "zero" — a
        malformed field must not silently stand the slot down.
        """
        try:
            asked = float(self.usd_to_deploy) if self.usd_to_deploy is not None else None
        except (TypeError, ValueError):
            asked = None
        if asked is None or asked < 0:
            return max(0.0, weekly_usd_left)
        return max(0.0, min(asked, weekly_usd_left))

    def approved_ranked(self, candidates: dict[str, list[dict]], *,
                        apply_exits: bool = True) -> list[dict]:
        """Every approved pick across ALL strategies, in the advisor's order.

        The pooled budget's counterpart to ``approved_for``. Execution is no
        longer per strategy — there is one pot and one queue — so the ranking
        the advisor produces across strategies is what actually decides who
        gets funded, rather than the order a for-loop happened to iterate in.

        Each survivor carries ``_strategy`` so the executor still knows what
        it is placing: per-trade caps, exit defaults and attribution are all
        still per strategy even though the budget is not.
        """
        out: list[tuple[int, dict]] = []
        for strategy, picks in (candidates or {}).items():
            for pick in self.approved_for(strategy, picks, apply_exits=apply_exits):
                annotated = dict(pick)
                annotated["_strategy"] = strategy
                out.append((annotated.get("_llm_rank", 99), annotated))
        out.sort(key=lambda pair: pair[0])
        return [pick for _, pick in out]
