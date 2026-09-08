"""`run_slot` — the orchestrator around the single advisory call.

What matters here is not that the advisor is clever but that its failure modes
are boring: no verdict means no orders, a stand-down means no orders, and the
paid call never happens when it could not change the outcome anyway.
"""

from __future__ import annotations

import asyncio

import pytest

from raanu.ai.schema import CandidateDecision, SlotVerdict
from raanu.trading import schedule, trader

PICKS = {"s3": [{"ticker": "NVDA", "score": 88, "leader_dip": True}],
         "s1": [{"ticker": "AMD", "score": 74, "uptrend": True}],
         "s2": []}


@pytest.fixture
def slot(monkeypatch):
    """A slot wired up so nothing leaves the process.

    Returns a recorder of what each layer was asked to do.
    """
    seen = {"executed": [], "scanned": [], "advisor_calls": 0, "cached": []}

    monkeypatch.setattr(schedule, "_scan_actionable",
                        lambda strategy, n, label="": list(PICKS.get(strategy, [])))

    async def fake_execute(n_orders, label, strategy="s1", picks=None, verdict=None):
        seen["executed"].append((strategy, [p["ticker"] for p in (picks or [])], verdict))

    monkeypatch.setattr(schedule, "_execute_scheduled_trades", fake_execute)

    async def fake_cache(*a, **k):
        seen["cached"].append(True)
        return []

    monkeypatch.setattr(schedule, "_scan_and_cache_for", lambda strategy: fake_cache)

    from raanu.ai import market_context
    monkeypatch.setattr(market_context, "snapshot", lambda: {"broad": {}, "breadth": {}})

    def set_verdict(verdict):
        from raanu.ai import advisor

        async def fake_review(candidates, context, label):
            seen["advisor_calls"] += 1
            return verdict

        monkeypatch.setattr(advisor, "review_slot", fake_review)

    seen["set_verdict"] = set_verdict
    monkeypatch.setenv("LLM_ADVISOR_ENABLED", "1")
    trader.AutoTrader().enabled = True
    return seen


def _verdict(**kw) -> SlotVerdict:
    base = dict(trade_today=True, regime="neutral", market_summary="calm")
    base.update(kw)
    return SlotVerdict(**base)


def _approve(ticker, strategy, **kw) -> CandidateDecision:
    base = dict(ticker=ticker, strategy=strategy, approve=True, rank=1, confidence=0.8)
    base.update(kw)
    return CandidateDecision(**base)


class TestFailsClosed:
    def test_no_verdict_means_no_orders(self, slot):
        slot["set_verdict"](None)
        asyncio.run(schedule.run_slot(5, "test-slot"))
        assert slot["executed"] == [], (
            "the advisor could not be reached and the slot traded anyway — "
            "an unreachable advisor must never read as approval")

    def test_stand_down_means_no_orders(self, slot):
        slot["set_verdict"](_verdict(trade_today=False,
                                     market_summary="index gapped -2.4%, 11/11 sectors red"))
        asyncio.run(schedule.run_slot(5, "test-slot"))
        assert slot["executed"] == []

    def test_all_vetoed_means_no_orders_for_that_strategy(self, slot):
        slot["set_verdict"](_verdict(decisions=[]))    # nothing approved
        asyncio.run(schedule.run_slot(5, "test-slot"))
        assert slot["executed"] == []


class TestTheCallIsSkippedWhenItCannotMatter:
    """Most days should cost nothing."""

    def test_no_call_when_the_trader_is_off(self, slot):
        slot["set_verdict"](_verdict())
        trader.AutoTrader().enabled = False
        asyncio.run(schedule.run_slot(5, "test-slot"))
        assert slot["advisor_calls"] == 0
        assert slot["executed"] == []
        assert slot["cached"], "picks should still refresh so the dashboard is not stale"

    def test_no_call_when_nothing_is_actionable(self, slot, monkeypatch):
        slot["set_verdict"](_verdict())
        monkeypatch.setattr(schedule, "_scan_actionable", lambda *a, **k: [])
        asyncio.run(schedule.run_slot(5, "test-slot"))
        assert slot["advisor_calls"] == 0
        assert slot["executed"] == []

    def test_no_call_when_the_advisor_is_disabled(self, slot, monkeypatch):
        slot["set_verdict"](_verdict())
        monkeypatch.setenv("LLM_ADVISOR_ENABLED", "0")
        asyncio.run(schedule.run_slot(5, "test-slot"))
        assert slot["advisor_calls"] == 0
        # ...and the old per-strategy behaviour still runs unchanged.
        assert [s for s, _, _ in slot["executed"]] == ["s3", "s1", "s2"]


class TestApprovedPicksReachTheExecutor:
    def test_only_approved_tickers_are_passed_on(self, slot):
        # S3's NVDA is approved; S1's AMD is not mentioned, so S1 must not run.
        verdict = _verdict(decisions=[_approve("NVDA", "s3")])
        slot["set_verdict"](verdict)
        asyncio.run(schedule.run_slot(5, "test-slot"))

        assert [(s, t) for s, t, _ in slot["executed"]] == [("s3", ["NVDA"])]

    def test_the_verdict_is_handed_to_the_executor_for_budgeting(self, slot):
        verdict = _verdict(decisions=[_approve("NVDA", "s3")])
        slot["set_verdict"](verdict)
        asyncio.run(schedule.run_slot(5, "test-slot"))

        assert slot["executed"][0][2] is verdict

    def test_s3_runs_first(self, slot):
        slot["set_verdict"](_verdict(decisions=[_approve("NVDA", "s3"),
                                                _approve("AMD", "s1")]))
        asyncio.run(schedule.run_slot(5, "test-slot"))
        # S3 is the only strategy profitable in both halves of the backtest,
        # so any rounding edge falls its way.
        assert [s for s, _, _ in slot["executed"]] == ["s3", "s1"]


class TestShadowMode:
    """The verdict is recorded but not acted on — how the advisor gets judged
    before any capital depends on it."""

    def test_shadow_trades_the_quants_picks_despite_a_stand_down(self, slot, monkeypatch):
        monkeypatch.setenv("LLM_ADVISOR_SHADOW", "1")
        slot["set_verdict"](_verdict(trade_today=False, market_summary="would stand down"))
        asyncio.run(schedule.run_slot(5, "test-slot"))
        assert [s for s, _, _ in slot["executed"]] == ["s3", "s1"]

    def test_shadow_ignores_vetoes(self, slot, monkeypatch):
        monkeypatch.setenv("LLM_ADVISOR_SHADOW", "1")
        slot["set_verdict"](_verdict(decisions=[]))     # everything vetoed
        asyncio.run(schedule.run_slot(5, "test-slot"))
        assert [tickers for _, tickers, _ in slot["executed"]] == [["NVDA"], ["AMD"]]

    def test_shadow_still_calls_the_advisor(self, slot, monkeypatch):
        monkeypatch.setenv("LLM_ADVISOR_SHADOW", "1")
        slot["set_verdict"](_verdict())
        asyncio.run(schedule.run_slot(5, "test-slot"))
        assert slot["advisor_calls"] == 1

    def test_shadow_passes_no_verdict_to_the_executor(self, slot, monkeypatch):
        # Otherwise the budget split would take effect while "not acting on it".
        monkeypatch.setenv("LLM_ADVISOR_SHADOW", "1")
        monkeypatch.setenv("LLM_BUDGET_ENABLED", "1")
        slot["set_verdict"](_verdict(budget_pct={"s3": 100.0}))
        asyncio.run(schedule.run_slot(5, "test-slot"))
        assert all(v is None for _, _, v in slot["executed"])
