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

    # The slot now checks the gates no verdict can lift BEFORE paying for
    # advice, so a test that wants the advisor reached has to look tradeable.
    # Both default to "yes"; the tests below flip them deliberately.
    seen["market_open"] = True
    seen["blockers"] = {}

    async def fake_clock():
        return (seen["market_open"], "market closed for the test")

    monkeypatch.setattr(trader, "market_is_open", fake_clock)
    monkeypatch.setattr(schedule, "_unconditional_blockers",
                        lambda label, strategies: dict(seen["blockers"]))
    monkeypatch.setattr(schedule, "_send_confident_buy_alerts",
                        lambda picks, strategy="s1", slot="": None)

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


class TestUnconditionalGatesRunBeforeTheAdvisor:
    """The paid call must not happen when no verdict could change the outcome.

    Measured on 10 Sep 2026: all three strategies were at their weekly cap,
    and the slot still scanned, still called the advisor, and still got a
    good verdict — 13 decisions, 12 approvals, 30s, ~$0.085 — for a slot in
    which no order was possible under ANY answer. Twice a day, until the
    oldest trade aged out five days later.
    """

    def test_a_fully_weekly_limited_slot_never_calls_the_advisor(self, slot):
        slot["blockers"] = {"s1": "weekly limit (2/2)", "s2": "weekly limit (1/1)",
                            "s3": "weekly limit (3/3)"}
        slot["set_verdict"](_verdict(decisions=[_approve("NVDA", "s3")]))
        asyncio.run(schedule.run_slot(5, "test-slot"))
        assert slot["advisor_calls"] == 0, "paid for advice on a slot that could not trade"
        assert slot["executed"] == []

    def test_a_partially_blocked_slot_still_reviews_what_is_left(self, slot):
        # S3 is capped; S1 is not. The advisor should be asked about S1 only.
        slot["blockers"] = {"s3": "weekly limit (3/3)"}
        slot["set_verdict"](_verdict(decisions=[_approve("AMD", "s1")]))
        asyncio.run(schedule.run_slot(5, "test-slot"))
        assert slot["advisor_calls"] == 1
        assert [s for s, _, _ in slot["executed"]] == ["s1"]

    def test_a_blocked_strategy_is_not_shown_to_the_advisor(self, slot, monkeypatch):
        seen_candidates = {}
        from raanu.ai import advisor

        async def capture(candidates, context, label):
            seen_candidates.update({k: len(v) for k, v in candidates.items()})
            return _verdict(decisions=[_approve("AMD", "s1")])

        monkeypatch.setattr(advisor, "review_slot", capture)
        slot["blockers"] = {"s3": "weekly limit (3/3)"}
        asyncio.run(schedule.run_slot(5, "test-slot"))
        assert "s3" not in seen_candidates
        assert seen_candidates.get("s1") == 1

    def test_a_closed_market_never_calls_the_advisor(self, slot):
        slot["market_open"] = False
        slot["set_verdict"](_verdict(decisions=[_approve("NVDA", "s3")]))
        asyncio.run(schedule.run_slot(5, "test-slot"))
        assert slot["advisor_calls"] == 0
        assert slot["executed"] == []

    def test_an_open_market_with_budget_still_calls_the_advisor(self, slot):
        # The guard must not become a reason the slot never trades.
        slot["set_verdict"](_verdict(decisions=[_approve("NVDA", "s3")]))
        asyncio.run(schedule.run_slot(5, "test-slot"))
        assert slot["advisor_calls"] == 1


class TestRefreshOrAlert:
    """A gate stopped the orders — what still has to happen?"""

    def test_supplied_picks_are_not_rescanned(self, monkeypatch):
        # run_slot already scanned and _scan_actionable already wrote the
        # cache through the same _pick_saver. Rescanning 470 tickers a second
        # time cost ~28s of a 110s invocation on 10 Sep.
        rescans, alerts = [], []

        async def boom(*a, **k):
            rescans.append(True)
            return []

        monkeypatch.setattr(schedule, "_scan_and_cache_for", lambda s: boom)
        monkeypatch.setattr(schedule, "_send_confident_buy_alerts",
                            lambda picks, strategy="s1", slot="": alerts.append(len(picks)))
        asyncio.run(schedule._refresh_or_alert("s3", [{"ticker": "QLYS", "score": 90}], "slot"))
        assert rescans == [], "rescanned picks it had already been handed"
        assert alerts == [1], "a capped strategy still found something worth saying"

    def test_no_picks_means_the_cache_is_refreshed(self, monkeypatch):
        # The pre-advisor callers (run_one_cycle, scan-now) genuinely have
        # nothing cached, so the rescan must survive for them.
        rescans = []

        async def scan(*a, **k):
            rescans.append(True)
            return []

        monkeypatch.setattr(schedule, "_scan_and_cache_for", lambda s: scan)
        asyncio.run(schedule._refresh_or_alert("s3", None, "slot"))
        assert rescans == [True]
