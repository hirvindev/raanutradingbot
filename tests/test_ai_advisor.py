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


class TestSlotBudget:
    """The pooled weekly pot replaced the per-strategy cash shares.

    The advisor may pace WITHIN what the week has left; it may never enlarge
    it. weekly_usd_left is computed from the trade log, so this clamps rather
    than trusts.
    """

    def test_it_may_spend_less_than_the_week_has_left(self):
        # Pacing is a real decision: "two trades today, hold five" is an
        # answer a numeric rule cannot give.
        v = _verdict(usd_to_deploy=2000)
        assert v.slot_budget(weekly_usd_left=7000) == 2000

    def test_it_may_never_spend_more(self):
        v = _verdict(usd_to_deploy=50000)
        assert v.slot_budget(weekly_usd_left=7000) == 7000

    def test_an_absent_value_uses_what_is_left_not_zero(self):
        # A missing field must not silently stand the slot down.
        assert _verdict().slot_budget(weekly_usd_left=7000) == 7000

    def test_a_negative_value_falls_back_rather_than_inverting(self):
        v = _verdict(usd_to_deploy=0)
        assert v.slot_budget(weekly_usd_left=7000) == 0

    def test_an_exhausted_week_yields_nothing_however_keen_the_advisor(self):
        v = _verdict(usd_to_deploy=5000)
        assert v.slot_budget(weekly_usd_left=0) == 0

    def test_the_schema_rejects_a_negative_request(self):
        with pytest.raises(ValidationError):
            SlotVerdict(trade_today=True, regime="neutral",
                        market_summary="x", usd_to_deploy=-1)


class TestApprovedRanked:
    """One pot, one queue. The advisor's cross-strategy rank is what decides
    who gets funded — not the order a for-loop happened to iterate in, which
    is what silently allocated capital on 13 Aug 2026."""

    CANDS = {"s1": [{"ticker": "AMD", "score": 74}],
             "s2": [{"ticker": "STT", "score": 70}],
             "s3": [{"ticker": "NVDA", "score": 88}]}

    def test_ordering_is_global_not_per_strategy(self):
        v = _verdict(decisions=[
            _decision("STT", strategy="s2", rank=1),
            _decision("NVDA", strategy="s3", rank=2),
            _decision("AMD", strategy="s1", rank=3)])
        out = v.approved_ranked(self.CANDS)
        assert [p["ticker"] for p in out] == ["STT", "NVDA", "AMD"]

    def test_each_pick_still_knows_its_strategy(self):
        # The budget is pooled; per-trade caps, exit defaults and attribution
        # are still per strategy, so the label has to survive the merge.
        v = _verdict(decisions=[_decision("NVDA", strategy="s3", rank=1)])
        assert v.approved_ranked(self.CANDS)[0]["_strategy"] == "s3"

    def test_vetoed_picks_do_not_reach_the_queue(self):
        v = _verdict(decisions=[
            _decision("NVDA", strategy="s3", rank=1, approve=False),
            _decision("AMD", strategy="s1", rank=2)])
        assert [p["ticker"] for p in v.approved_ranked(self.CANDS)] == ["AMD"]

    def test_an_invented_ticker_is_still_dropped(self):
        # The one power the advisor never gets, now across the pooled queue.
        v = _verdict(decisions=[_decision("TSLA", strategy="s3", rank=1)])
        assert v.approved_ranked(self.CANDS) == []

    def test_no_decisions_means_an_empty_queue(self):
        assert _verdict(decisions=[]).approved_ranked(self.CANDS) == []


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


class TestTheCallIsStreamed:
    """9 Sep 2026: the advisor's first live slot timed out three times at 60s
    each and placed no orders. The request was non-streaming, so nothing
    reached the socket while the model thought and searched, and a healthy
    call was indistinguishable from a dead connection. These pin the fix.
    """

    def _fake_client(self, monkeypatch, *, stop_reason="end_turn"):
        """A stand-in SDK client that records how it was called."""
        from raanu.ai import advisor

        seen: dict = {}

        class FakeStream:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): return False

            async def get_final_message(self):
                class Msg:
                    pass
                msg = Msg()
                msg.stop_reason = stop_reason
                msg.content = []
                msg.parsed_output = _verdict(decisions=[_decision("NVDA")])
                msg.usage = type("U", (), {
                    "input_tokens": 1500, "output_tokens": 900,
                    "cache_read_input_tokens": 1200,
                    "cache_creation_input_tokens": 0})()
                return msg

        class FakeMessages:
            def stream(self, **kwargs):
                seen.update(kwargs)
                return FakeStream()

            def parse(self, **kwargs):  # pragma: no cover - must never be hit
                raise AssertionError("the advisor must stream, not parse")

        class FakeClient:
            messages = FakeMessages()

        monkeypatch.setattr(advisor, "_client", lambda: FakeClient())
        return seen

    def test_it_streams_rather_than_blocking_on_a_single_response(self, monkeypatch):
        seen = self._fake_client(monkeypatch)
        from raanu.ai import advisor
        verdict = asyncio.run(advisor._call_anthropic('{"slot":"t"}'))
        assert verdict.trade_today is True
        assert seen, "messages.stream() was never called"

    def test_the_structured_output_contract_survives_streaming(self, monkeypatch):
        # The whole reason parse() was used. stream() takes the same
        # output_format and returns a ParsedMessage, so this must not drift.
        seen = self._fake_client(monkeypatch)
        from raanu.ai import advisor
        asyncio.run(advisor._call_anthropic('{}'))
        assert seen["output_format"] is SlotVerdict

    def test_effort_is_set(self, monkeypatch):
        seen = self._fake_client(monkeypatch)
        from raanu.ai import advisor
        asyncio.run(advisor._call_anthropic('{}'))
        assert seen["output_config"]["effort"] == "medium"

    def test_the_prompt_cache_is_NOT_declared(self, monkeypatch):
        # Measured, not assumed: the cached prefix is ~12,700 tokens, a write
        # costs 1.25x and a read 0.1x, and nothing ever reads across slots
        # (85 minutes apart, 1h max TTL) — every observed slot logged
        # cache_read_input_tokens: 0. The only reader is a retry, and the
        # streaming fix is what made retries rare. Break-even is a ~28% retry
        # rate; the observed rate is 0. Re-add it if that changes.
        seen = self._fake_client(monkeypatch)
        from raanu.ai import advisor
        asyncio.run(advisor._call_anthropic('{}'))
        assert "cache_control" not in seen

    def test_usage_records_the_cache_lines_not_just_the_totals(self, monkeypatch):
        # An unmeasured token-reduction claim is the thing this project keeps
        # having to walk back. cache_read stuck at zero means the prefix cache
        # is not engaging and cache_control is dead weight.
        self._fake_client(monkeypatch)
        from raanu.ai import advisor
        verdict = asyncio.run(advisor._call_anthropic('{}'))
        assert verdict._usage["cache_read_input_tokens"] == 1200

    def test_a_refusal_still_raises_rather_than_returning_a_verdict(self, monkeypatch):
        self._fake_client(monkeypatch, stop_reason="refusal")
        from raanu.ai import advisor
        with pytest.raises(RuntimeError):
            asyncio.run(advisor._call_anthropic('{}'))


class TestRetryBudgetFitsTheLambda:
    """The SDK retries timeouts, so the worst case is timeout x (retries + 1).

    The worker Lambda is killed at 600s and runs the exit-monitor pass in the
    same invocation immediately after the slot — a hard kill would skip it and
    lose the llm.failed trace row too. This is the arithmetic that has to hold.
    """

    def test_worst_case_wall_clock_stays_inside_the_worker_lambda(self):
        from raanu import config
        worst = config.llm_timeout_sec() * (config.llm_max_retries() + 1)
        assert worst <= 450, (
            f"{worst}s of retries against a 600s Lambda leaves no room for "
            "the exit-monitor pass that follows the slot")

    def test_max_retries_is_set_explicitly_not_left_to_the_sdk(self, monkeypatch):
        # Leaving it unset means the SDK's default of 2, i.e. three attempts.
        # That is what turned a 60s timeout into a 185s stall on 9 Sep 2026.
        from raanu import config
        assert config.llm_max_retries() < 2

    def test_the_timeout_is_long_enough_for_thinking_plus_search(self):
        from raanu import config
        assert config.llm_timeout_sec() >= 120


class TestPayloadIsTrimmed:
    """The prompt-side token bill is the half this code controls."""

    def _payload(self, **over):
        from raanu.ai.advisor import _payload
        pick = {"ticker": "NVDA", "score": 88, "price": 123.45000000000002,
                "reasons": [f"reason {i} — with an em-dash" for i in range(9)]}
        pick.update(over)
        return _payload({"s3": [pick]}, {}, "slot")

    def test_float_noise_is_rounded_away(self):
        # 0.15000000000000002 is eighteen tokens of nothing. Same lesson the
        # daily bars cache learned about Yahoo's float32 artefacts.
        assert "123.45000000000002" not in self._payload()
        assert "123.45" in self._payload()

    def test_the_reason_list_is_capped(self):
        import json
        rows = json.loads(self._payload())["candidates"]["s3"]
        assert len(rows[0]["reasons"]) == 4

    def test_the_leading_reasons_are_kept_not_the_tail(self):
        # The first few carry the structural facts with no numeric column
        # (52-week-high distance, base tightness, volume ratio).
        import json
        rows = json.loads(self._payload())["candidates"]["s3"]
        assert rows[0]["reasons"][0].startswith("reason 0")

    def test_em_dashes_are_not_escaped(self):
        # Escaped, one character becomes six. The state layer already pays
        # this lesson: a native map measured 24% smaller than escaped JSON.
        assert "\\u2014" not in self._payload()

    def test_booleans_survive_the_rounding_pass(self):
        import json
        rows = json.loads(self._payload(uptrend=True))["candidates"]["s3"]
        assert rows[0]["uptrend"] is True


class TestIncoherentVerdictsFailLoudly:
    """`trade_today=True` with no decisions parses fine and trades nothing.

    Measured on 9 Sep 2026: 1 live call in 8 came back this way, at BOTH
    medium and high effort, on input the other 7 answered with six decisions.
    Left alone it is a silent no-trade logged as a successful llm.response —
    the exact failure this module exists to make loud.
    """

    def _review(self, monkeypatch, verdict, candidates=PICKS):
        from raanu.ai import advisor

        async def ok(_): return verdict
        monkeypatch.setattr(advisor, "_call", ok)
        return asyncio.run(advisor.review_slot({"s3": candidates}, {}, "slot"))

    def test_trade_today_with_no_decisions_is_rejected(self, monkeypatch):
        assert self._review(monkeypatch, _verdict(decisions=[])) is None

    def test_a_stand_down_with_no_decisions_is_perfectly_normal(self, monkeypatch):
        # trade_today=False and an empty list is coherent: nothing to decide.
        v = _verdict(trade_today=False, decisions=[])
        assert self._review(monkeypatch, v) is v

    def test_a_verdict_with_decisions_is_untouched(self, monkeypatch):
        v = _verdict(decisions=[_decision("NVDA")])
        assert self._review(monkeypatch, v) is v

    def test_it_lands_on_the_failed_path_not_the_response_path(self, monkeypatch):
        # The outcome is the same either way — no orders. What changes is that
        # it becomes an llm.failed row naming the reason.
        seen = []
        from raanu.ai import advisor
        monkeypatch.setattr(advisor.trace, "emit",
                            lambda event, **kw: seen.append(event))
        self._review(monkeypatch, _verdict(decisions=[]))
        assert "llm.failed" in seen and "llm.response" not in seen


class TestTheToolLoop:
    """The hybrid split: market and budget are in the prompt; history, picks
    and positions are tools the model pulls only when a call is close.

    🔴 The loop is what makes the per-request timeout stop bounding the
    review. llm_timeout_sec() bounds ONE request; three iterations of two
    attempts at 150s is 900s against a 600s Lambda — and being killed there is
    worse than failing, because the exit-monitor pass shares the invocation
    and the llm.failed row never gets written.
    """

    def _client(self, monkeypatch, script):
        """A fake client that replays `script`, one entry per request."""
        from raanu.ai import advisor
        calls = {"n": 0, "messages": [], "timeouts": []}

        class Block:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        class FakeStream:
            def __init__(self, resp): self._resp = resp
            async def __aenter__(self): return self
            async def __aexit__(self, *_): return False
            async def get_final_message(self): return self._resp

        class FakeMessages:
            def stream(self, **kwargs):
                i = calls["n"]
                calls["n"] += 1
                calls["messages"].append(kwargs["messages"])
                calls["timeouts"].append(kwargs.get("timeout"))
                spec = script[min(i, len(script) - 1)]
                resp = Block(**spec)
                return FakeStream(resp)

        class FakeClient:
            messages = FakeMessages()

        monkeypatch.setattr(advisor, "_client", lambda: FakeClient())
        monkeypatch.setenv("LLM_TOOLS_ENABLED", "1")
        calls["Block"] = Block
        return calls

    def _final(self):
        return {"stop_reason": "end_turn", "content": [],
                "parsed_output": _verdict(decisions=[_decision("NVDA")]),
                "usage": None}

    def test_a_tool_call_is_executed_and_fed_back(self, monkeypatch):
        from raanu.ai import advisor

        class Use:
            type = "tool_use"
            id = "tu_1"
            name = "get_pick_outcomes"
            input = {}

        calls = self._client(monkeypatch, [
            {"stop_reason": "tool_use", "content": [Use()]},
            self._final(),
        ])
        monkeypatch.setattr(advisor, "_run_tool",
                            lambda n, a: _async({"bands": "ok"}))
        verdict = asyncio.run(advisor._call_anthropic('{}'))
        assert verdict.trade_today is True
        assert calls["n"] == 2, "the tool result was never sent back"
        # Results ride in a single user message, or the model learns to stop
        # calling tools in parallel.
        last = calls["messages"][-1][-1]
        assert last["role"] == "user"
        assert last["content"][0]["type"] == "tool_result"

    def test_the_deadline_shrinks_each_request_timeout(self, monkeypatch):
        monkeypatch.setenv("LLM_TOTAL_BUDGET_SEC", "40")
        monkeypatch.setenv("LLM_TIMEOUT_SEC", "150")
        calls = self._client(monkeypatch, [self._final()])
        from raanu.ai import advisor
        asyncio.run(advisor._call_anthropic('{}'))
        # Never longer than what is left of the review's whole budget.
        assert calls["timeouts"][0] <= 40

    def test_an_exhausted_deadline_fails_closed(self, monkeypatch):
        from raanu.ai import advisor

        class Use:
            type, id, name, input = "tool_use", "tu_1", "get_pick_outcomes", {}

        self._client(monkeypatch, [{"stop_reason": "tool_use", "content": [Use()]}])
        monkeypatch.setenv("LLM_TOTAL_BUDGET_SEC", "0")
        # Raises rather than returning a half-formed verdict; review_slot's
        # single except turns it into "no orders".
        with pytest.raises((TimeoutError, RuntimeError)):
            asyncio.run(advisor._call_anthropic('{}'))

    def test_endless_tool_calling_is_stopped(self, monkeypatch):
        from raanu.ai import advisor

        class Use:
            type, id, name, input = "tool_use", "tu_1", "get_pick_outcomes", {}

        monkeypatch.setenv("LLM_MAX_TOOL_ITERATIONS", "2")
        calls = self._client(monkeypatch, [{"stop_reason": "tool_use", "content": [Use()]}])
        monkeypatch.setattr(advisor, "_run_tool", lambda n, a: _async({"x": 1}))
        with pytest.raises(RuntimeError):
            asyncio.run(advisor._call_anthropic('{}'))
        assert calls["n"] <= 3

    def test_a_failing_tool_does_not_fail_the_slot(self, monkeypatch):
        # The model asked an optional question. "That lookup did not work" is
        # something it can reason around; raising would turn a degraded
        # picture into no trades at all.
        from raanu.ai import advisor
        out = asyncio.run(advisor._run_tool("get_trade_history", {"days": "oops"}))
        assert "error" in out

    def test_an_unknown_tool_is_an_error_result_not_a_crash(self, monkeypatch):
        from raanu.ai import advisor
        out = asyncio.run(advisor._run_tool("get_nuclear_codes", {}))
        assert "error" in out

    def test_tools_can_be_switched_off_entirely(self, monkeypatch):
        monkeypatch.setenv("LLM_TOOLS_ENABLED", "0")
        monkeypatch.setenv("LLM_WEB_SEARCH", "0")
        from raanu.ai import advisor
        assert advisor._tools() == []

    def test_web_search_and_context_tools_coexist(self, monkeypatch):
        monkeypatch.setenv("LLM_TOOLS_ENABLED", "1")
        monkeypatch.setenv("LLM_WEB_SEARCH", "1")
        from raanu.ai import advisor
        names = [t["name"] for t in advisor._tools()]
        assert "web_search" in names and "get_trade_history" in names


async def _async(value):
    return value
