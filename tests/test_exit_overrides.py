"""Per-trade exit plans, and the invariant that makes them safe.

The advisor may set a stop, a trail and a ladder per position instead of per
strategy. Two things have to hold for that to be anything other than a way to
lose money quietly:

  1. the stop that SIZES a trade is the stop that EXITS it, and
  2. the safety floors still clamp whatever the advisor chose.

Both are tested here, first.
"""

from __future__ import annotations

import pytest

from raanu import config
from raanu.trading.exits import effective_stop_pct, stop_atr_mult_for


class TestSizingAndExitAgree:
    """The risk-doubling defect this design is most exposed to.

    `sizing.shares_for()` computes qty = risk_budget / (entry - stop), so the
    stop used at entry IS the risk model. If a plan widened the exit stop but
    sizing kept using the strategy default, the loss at the stop would quietly
    exceed the intended share of equity while every log line still reported the
    configured risk_pct.
    """

    def test_entry_and_exit_resolve_the_identical_stop(self):
        # Both call sites go through one function, so this is structural
        # rather than a coincidence two implementations happen to share.
        from raanu.trading import exits, schedule

        assert "effective_stop_pct" in exits.__dict__
        src = __import__("inspect").getsource(schedule._execute_scheduled_trades)
        assert "effective_stop_pct(" in src, (
            "the entry sizer no longer uses the shared stop helper — sizing "
            "and exiting can now disagree, which silently changes real risk")

    def test_a_wide_plan_changes_the_sized_stop_too(self):
        atr_pct = 4.0
        default = effective_stop_pct("s1", atr_pct)
        widened = effective_stop_pct("s1", atr_pct, {"stop_atr_mult": 5.0})

        assert widened > default
        # 5.0 x 4.0% = 20%, inside STOP_MAX_PCT (25) so not clamped.
        assert widened == pytest.approx(20.0)

    def test_risk_at_the_stop_stays_the_intended_share_of_equity(self):
        """The consequence, stated in money rather than percentages."""
        from raanu.trading.sizing import shares_for

        equity, risk_pct, entry, atr_pct = 100_000.0, 1.0, 100.0, 4.0
        plan = {"stop_atr_mult": 5.0}

        stop_pct = effective_stop_pct("s1", atr_pct, plan)
        qty = shares_for(equity, risk_pct, entry, entry * (1 - stop_pct / 100),
                         max_position_pct=100.0)

        # The exit engine will enforce this same stop, so the realised loss is
        # the risk budget — not double it.
        loss_at_stop = qty * entry * stop_pct / 100
        assert loss_at_stop == pytest.approx(equity * risk_pct / 100, rel=1e-6)

    def test_sizing_off_the_default_while_exiting_on_a_plan_would_double_risk(self):
        """Demonstrates the bug the shared helper prevents, so the reason for
        the helper survives someone 'simplifying' it later."""
        from raanu.trading.sizing import shares_for

        equity, risk_pct, entry, atr_pct = 100_000.0, 1.0, 100.0, 4.0

        sized_on_default = effective_stop_pct("s1", atr_pct)                    # 2.5x -> 10%
        exited_on_plan = effective_stop_pct("s1", atr_pct, {"stop_atr_mult": 5.0})  # 20%
        qty = shares_for(equity, risk_pct, entry, entry * (1 - sized_on_default / 100),
                         max_position_pct=100.0)

        intended = equity * risk_pct / 100
        actual = qty * entry * exited_on_plan / 100
        assert actual == pytest.approx(intended * 2, rel=1e-6)


class TestFloorsStillClamp:
    """CLAUDE.md: the floors are not optional. An unfloored trail closed a live
    ARB position on a 0.15% wiggle for +0.49%."""

    def test_a_tiny_plan_stop_is_raised_to_the_floor(self, monkeypatch):
        monkeypatch.setenv("STOP_MIN_PCT", "1.5")
        config.reset_exit_config()
        # 1.5 x 0.1% = 0.15%, far inside the spread — must be floored.
        assert effective_stop_pct("s1", 0.1, {"stop_atr_mult": 1.5}) == 1.5

    def test_a_huge_plan_stop_is_capped_at_the_ceiling(self, monkeypatch):
        monkeypatch.setenv("STOP_MAX_PCT", "25.0")
        config.reset_exit_config()
        assert effective_stop_pct("s1", 12.0, {"stop_atr_mult": 5.0}) == 25.0


class TestNoPlanIsUnchangedBehaviour:
    def test_absent_plan_uses_the_strategy_default(self):
        for strategy in ("s1", "s2", "s3"):
            expected = stop_atr_mult_for(strategy) * 3.0
            assert effective_stop_pct(strategy, 3.0) == pytest.approx(expected)

    def test_empty_plan_is_the_same_as_no_plan(self):
        assert effective_stop_pct("s3", 3.0, {}) == effective_stop_pct("s3", 3.0)

    def test_plan_without_a_stop_key_leaves_the_stop_alone(self):
        plan = {"ladder": "off", "note": "let it run"}
        assert effective_stop_pct("s3", 3.0, plan) == effective_stop_pct("s3", 3.0)


class TestPlanSurvivesAMonitorPass:
    """The monitor rewrites the position record on every pass. A wholesale
    replace would drop the plan on the first tick and every trade would
    silently revert to strategy defaults with nothing in the logs."""

    def test_the_record_write_merges_rather_than_replaces(self):
        import inspect

        from raanu.trading import exits
        src = inspect.getsource(exits.run_monitor_once)
        assert "**pstate" in src, (
            "run_monitor_once replaces the position record instead of merging "
            "it — the exit plan seeded at buy time is dropped on the first pass")

    def test_the_shadowed_state_local_is_gone(self):
        """`state = peaks.get(symbol)` shadowed the raanu.state module import
        for the whole function, so any state.get/put added inside it broke."""
        import inspect

        from raanu.trading import exits
        src = inspect.getsource(exits.run_monitor_once)
        assert "\n        state = peaks.get(" not in src
        assert "pstate = peaks.get(" in src
