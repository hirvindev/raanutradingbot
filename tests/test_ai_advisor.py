"""The advisory layer: what it may decide, and what it may never decide.

The advisor sits between the quant's candidates and the order path. These
tests pin the boundaries — it can veto, rank, trim and set exits; it cannot
invent a ticker, inflate a position, or enlarge the budget. And when it cannot
be reached at all, the slot places no orders.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from raanu.ai.schema import CandidateDecision, ExitPlan, SlotVerdict


def _verdict(**kw) -> SlotVerdict:
    base = dict(trade_today=True, regime="neutral", market_summary="calm tape")
    base.update(kw)
    return SlotVerdict(**base)


def _decision(ticker, **kw) -> CandidateDecision:
    base = dict(ticker=ticker, strategy="s3", approve=True, rank=1, confidence=0.7)
    base.update(kw)
    return CandidateDecision(**base)


PICKS = [{"ticker": "NVDA", "score": 88}, {"ticker": "AMD", "score": 74},
         {"ticker": "MSFT", "score": 71}]


class TestCannotInventABuy:
    """The one power the advisor never gets, enforced in code rather than by
    asking the prompt nicely."""

    def test_a_ticker_the_quant_never_surfaced_is_dropped(self):
        v = _verdict(decisions=[_decision("TSLA"), _decision("NVDA", rank=2)])
        out = v.approved_for("s3", PICKS)
        assert [p["ticker"] for p in out] == ["NVDA"]

    def test_a_decision_for_another_strategy_does_not_leak_across(self):
        v = _verdict(decisions=[_decision("NVDA", strategy="s1")])
        assert v.approved_for("s3", PICKS) == []

    def test_no_decision_at_all_means_not_traded(self):
        # Silence is not consent: an unmentioned candidate is not approved.
        assert _verdict(decisions=[]).approved_for("s3", PICKS) == []


class TestVetoAndRanking:
    def test_vetoed_candidates_are_removed(self):
        v = _verdict(decisions=[_decision("NVDA", approve=False),
                                _decision("AMD", rank=2)])
        assert [p["ticker"] for p in v.approved_for("s3", PICKS)] == ["AMD"]

    def test_output_is_in_the_advisors_rank_order_not_score_order(self):
        # MSFT scores lowest and is ranked first — the ordering must follow the
        # advisor, since the project's own backtest found the score does not rank.
        v = _verdict(decisions=[_decision("NVDA", rank=3), _decision("AMD", rank=2),
                                _decision("MSFT", rank=1)])
        assert [p["ticker"] for p in v.approved_for("s3", PICKS)] == ["MSFT", "AMD", "NVDA"]

    def test_the_original_pick_is_not_mutated(self):
        v = _verdict(decisions=[_decision("NVDA", size_mult=0.5)])
        v.approved_for("s3", PICKS)
        assert "_llm_size_mult" not in PICKS[0]


class TestSizeMultOnlyShrinks:
    def test_schema_rejects_a_multiplier_above_one(self):
        with pytest.raises(ValidationError):
            CandidateDecision(ticker="NVDA", strategy="s3", approve=True,
                              confidence=0.9, size_mult=1.5)

    def test_a_trim_is_carried_through(self):
        v = _verdict(decisions=[_decision("NVDA", size_mult=0.25)])
        assert v.approved_for("s3", PICKS)[0]["_llm_size_mult"] == 0.25

    def test_default_is_full_size(self):
        v = _verdict(decisions=[_decision("NVDA")])
        assert v.approved_for("s3", PICKS)[0]["_llm_size_mult"] == 1.0


class TestExitPlans:
    def test_plan_is_attached_when_exits_are_enabled(self):
        v = _verdict(decisions=[_decision("NVDA",
                                          exit_plan=ExitPlan(stop_atr_mult=3.5, note="wide"))])
        plan = v.approved_for("s3", PICKS, apply_exits=True)[0]["_llm_exit_plan"]
        assert plan == {"stop_atr_mult": 3.5, "note": "wide"}

    def test_plan_is_withheld_when_exits_are_disabled(self):
        v = _verdict(decisions=[_decision("NVDA", exit_plan=ExitPlan(stop_atr_mult=3.5))])
        assert v.approved_for("s3", PICKS, apply_exits=False)[0]["_llm_exit_plan"] == {}

    def test_untouched_plan_stores_nothing(self):
        # "strategy_default" must not be persisted as a choice, or every
        # position would carry a plan that means "no plan".
        assert ExitPlan().as_stored() == {}
        assert ExitPlan().is_empty()

    @pytest.mark.parametrize("field,value", [
        ("stop_atr_mult", 0.5), ("stop_atr_mult", 9.0),
        ("trail_activate_atr", 0.1), ("trail_atr_mult", 8.0),
    ])
    def test_out_of_range_exit_values_are_rejected(self, field, value):
        with pytest.raises(ValidationError):
            ExitPlan(**{field: value})


class TestBudgetShare:
    CAP, FALLBACK = 60.0, 50.0

    def _share(self, verdict, strategy="s3"):
        return verdict.budget_share(strategy, fallback=self.FALLBACK, max_share=self.CAP)

    def test_a_valid_split_is_used(self):
        v = _verdict(budget_pct={"s1": 30, "s2": 20, "s3": 50})
        assert self._share(v) == 50

    def test_concentration_is_capped(self):
        # Alpha improved at 4 -> 8 -> 15 positions, so the advisor may tilt
        # toward conviction but not pour everything into one strategy.
        v = _verdict(budget_pct={"s1": 0, "s2": 0, "s3": 100})
        assert self._share(v) == self.CAP

    def test_a_split_summing_over_100_falls_back(self):
        v = _verdict(budget_pct={"s1": 80, "s2": 80, "s3": 80})
        assert self._share(v) == self.FALLBACK

    def test_a_negative_weight_falls_back(self):
        v = _verdict(budget_pct={"s1": -10, "s2": 50, "s3": 50})
        assert self._share(v) == self.FALLBACK

    def test_absent_split_falls_back(self):
        assert self._share(_verdict()) == self.FALLBACK

    def test_a_strategy_missing_from_the_split_falls_back(self):
        v = _verdict(budget_pct={"s1": 50, "s2": 20})
        assert self._share(v) == self.FALLBACK

    def test_zero_is_honoured_rather_than_treated_as_missing(self):
        # "spend nothing on S2 today" is a real instruction, not a gap.
        v = _verdict(budget_pct={"s1": 50, "s2": 0, "s3": 50})
        assert self._share(v, "s2") == 0


class TestFailsClosed:
    """Any failure means no orders. One choke point, so this cannot rot."""

    def _review(self, monkeypatch, boom):
        from raanu.ai import advisor
        monkeypatch.setattr(advisor, "_call", boom)
        return asyncio.run(advisor.review_slot({"s3": PICKS}, {}, "test-slot"))

    def test_timeout_returns_none(self, monkeypatch):
        async def boom(_): raise TimeoutError("too slow")
        assert self._review(monkeypatch, boom) is None

    def test_network_error_returns_none(self, monkeypatch):
        async def boom(_): raise ConnectionError("no route to host")
        assert self._review(monkeypatch, boom) is None

    def test_malformed_response_returns_none(self, monkeypatch):
        async def boom(_): raise ValueError("no parsed_output on response")
        assert self._review(monkeypatch, boom) is None

    def test_unset_provider_returns_none_rather_than_raising(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "nonesuch")
        from raanu.ai import advisor
        assert asyncio.run(advisor.review_slot({"s3": PICKS}, {}, "slot")) is None

    def test_a_good_response_comes_back(self, monkeypatch):
        from raanu.ai import advisor
        expected = _verdict(decisions=[_decision("NVDA")])

        async def ok(_): return expected
        monkeypatch.setattr(advisor, "_call", ok)
        got = asyncio.run(advisor.review_slot({"s3": PICKS}, {}, "slot"))
        assert got is expected
