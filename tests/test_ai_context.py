"""The advisor's information layer.

Five services behind one protocol. What matters here is not that they fetch
correctly — that is the source's job — but that they FAIL correctly: four of
them degrade to a smaller picture, and exactly one of them must stop the slot.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from raanu import state
from raanu.ai import context
from raanu.ai.context import base, budget
from raanu.state import keys


def _buy(ticker: str, usd: float, strategy: str = "s1", days_ago: float = 1.0,
         action: str = "BUY") -> None:
    stamp = keys.stamp(datetime.now(UTC) - timedelta(days=days_ago))
    state.put(keys.TRADE, keys.trade_sk(stamp),
              {"action": action, "ticker": ticker, "notional_usd": usd,
               "strategy": strategy, "timestamp": stamp})


class TestTheWeeklyPool:
    """One allowance, two limits, rolling seven days."""

    def test_an_empty_week_has_the_whole_pool(self):
        pool = budget.state()
        assert pool["trades_left"] == 7
        assert pool["usd_left"] == 7000.0
        assert pool["exhausted"] is False

    def test_dollars_and_count_are_both_tracked(self):
        _buy("AAA", 1000.0)
        _buy("BBB", 2500.0, strategy="s3")
        pool = budget.state()
        assert pool["trades_used"] == 2
        assert pool["usd_used"] == 3500.0
        assert pool["usd_left"] == 3500.0
        assert pool["trades_left"] == 5

    def test_the_count_can_exhaust_before_the_dollars(self):
        # Seven $100 trades: $6,300 still unspent, but the week is over.
        for i in range(7):
            _buy(f"T{i}", 100.0)
        pool = budget.state()
        assert pool["trades_left"] == 0
        assert pool["usd_left"] == 6300.0
        assert pool["exhausted"] is True

    def test_the_dollars_can_exhaust_before_the_count(self):
        # Two big trades: five slots free, nothing to fund them with. This is
        # why the count alone is not a risk limit.
        _buy("AAA", 3500.0)
        _buy("BBB", 3500.0)
        pool = budget.state()
        assert pool["trades_left"] == 5
        assert pool["usd_left"] == 0.0
        assert pool["exhausted"] is True

    def test_a_remainder_below_the_minimum_counts_as_exhausted(self):
        # $40 left cannot buy anything worth having, so it is not capacity.
        _buy("AAA", 6960.0)
        assert budget.state()["exhausted"] is True

    def test_exits_do_not_consume_the_budget(self):
        # A SELL is logged with the strategy it was opened under. Counting it
        # once locked a strategy out of a whole week on a single close.
        _buy("AAA", 1000.0)
        _buy("AAA", 900.0, action="SELL")
        pool = budget.state()
        assert pool["trades_used"] == 1
        assert pool["usd_used"] == 1000.0

    def test_trades_outside_the_window_have_aged_out(self):
        _buy("OLD", 5000.0, days_ago=8)
        _buy("NEW", 1000.0, days_ago=1)
        pool = budget.state()
        assert pool["trades_used"] == 1
        assert pool["usd_used"] == 1000.0

    def test_the_pool_is_shared_not_split_by_strategy(self):
        # The whole point: S1 filling it genuinely does stop S3. The split is
        # still REPORTED, because "where did the week go" is worth answering.
        for i in range(7):
            _buy(f"T{i}", 100.0, strategy="s1")
        pool = budget.state()
        assert pool["trades_left"] == 0
        assert pool["trades_by_strategy_this_week"] == {"s1": 7}

    def test_it_reports_when_capacity_returns(self):
        # "One trade left and three more on Tuesday" is a different decision
        # from "one trade left and nothing for six days".
        _buy("AAA", 100.0, days_ago=2)
        frees = budget.state()["oldest_frees_at"]
        assert frees is not None
        assert datetime.fromisoformat(frees) > datetime.now(UTC)

    def test_a_buy_with_no_notional_does_not_crash_the_read(self):
        # Older rows predate notional_usd. A missing value must read as 0
        # rather than take down the gate that reads this.
        stamp = keys.stamp(datetime.now(UTC))
        state.put(keys.TRADE, keys.trade_sk(stamp),
                  {"action": "BUY", "ticker": "X", "strategy": "s1",
                   "timestamp": stamp})
        assert budget.state()["trades_used"] == 1

    def test_qty_times_price_is_used_when_notional_is_absent(self):
        stamp = keys.stamp(datetime.now(UTC))
        state.put(keys.TRADE, keys.trade_sk(stamp),
                  {"action": "BUY", "ticker": "X", "strategy": "s1",
                   "qty": 10, "entry_price": 25.0, "timestamp": stamp})
        assert budget.state()["usd_used"] == 250.0


class TestFailureIsNotUniform:
    """Four providers degrade; one stops the slot."""

    def _provider(self, name, *, required=False, boom=False, data=None):
        class P:
            pass
        P.name = name
        P.required = required

        async def fetch():
            if boom:
                raise RuntimeError("source down")
            return data
        P.fetch = staticmethod(fetch)
        return P

    def test_an_optional_failure_is_recorded_not_raised(self):
        out = asyncio.run(base.assemble([
            self._provider("market", data={"spy": 1}),
            self._provider("picks", boom=True)]))
        assert out["market"] == {"spy": 1}
        assert "picks" in out["partial"]

    def test_a_required_failure_stops_everything(self):
        # Not knowing what we are allowed to spend has no safe default.
        with pytest.raises(base.ProviderError):
            asyncio.run(base.assemble([
                self._provider("budget", required=True, boom=True)]))

    def test_returning_none_is_a_miss_not_a_crash(self):
        out = asyncio.run(base.assemble([self._provider("picks", data=None)]))
        assert out["partial"] == ["picks"]
        assert "picks" not in out

    def test_budget_is_the_only_required_provider(self):
        required = [p.name for p in context.EAGER if getattr(p, "required", False)]
        assert required == ["budget"]


class TestTheSplit:
    """What is pre-assembled vs what the model must ask for."""

    def test_market_and_budget_are_always_fetched(self):
        assert [p.name for p in context.EAGER] == ["market", "budget"]

    def test_the_research_services_are_tools(self):
        assert set(context.TOOLS) == {
            "get_trade_history", "get_pick_outcomes", "get_open_positions"}

    def test_every_tool_resolves_to_a_real_provider(self):
        for name, provider in context.TOOLS.items():
            assert callable(getattr(provider, "fetch", None)), name
