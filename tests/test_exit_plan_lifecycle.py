"""The exit plan's whole life: written at buy, survives the monitor, enforced.

The unit tests around it check the pieces. This drives the real
`run_monitor_once()` against the real state store, because the failure this
guards against is invisible in pieces: the monitor rewrites each position's
record on every pass, so a plan can be silently dropped on the first tick and
every trade quietly reverts to strategy defaults, with nothing in the logs and
no exception anywhere.
"""

from __future__ import annotations

import asyncio

import pytest

from raanu import state
from raanu.state import keys
from raanu.trading import exits

ENTRY = 100.0
ATR = 4.0            # 4% of entry
PLAN = {"stop_atr_mult": 3.5, "note": "high ATR name"}


@pytest.fixture
def live_position(monkeypatch):
    """One open position, market open, nothing pending — no network."""
    async def positions():
        return [{"symbol": "NVDA", "avg_entry_price": ENTRY,
                 "current_price": ENTRY, "qty": 10.0, "side": "long"}]

    async def market_open():
        return True

    async def no_pending():
        return set()

    async def never_called(*a, **k):
        raise AssertionError("should not have closed the position")

    monkeypatch.setattr(exits, "_get_positions", positions)
    monkeypatch.setattr(exits, "_market_is_open", market_open)
    monkeypatch.setattr(exits, "_symbols_with_pending_sell", no_pending)
    monkeypatch.setattr(exits, "_close_position", never_called)
    # Freeze the ATR so no bar download is attempted, and so the seeded value
    # is provably the one used rather than a coincidentally similar refetch.
    monkeypatch.setattr(exits, "_get_atr", never_called)


def _seed_plan(plan=PLAN):
    """What schedule.py writes immediately after a fill."""
    record = {"peak": ENTRY, "atr": ATR}
    if plan:
        record["plan"] = plan
    state.put(keys.PEAK, keys.peak_sk("NVDA"), record)


def _stored() -> dict:
    return state.get(keys.PEAK, keys.peak_sk("NVDA")) or {}


class TestPlanSurvivesTheMonitor:
    def test_plan_is_still_there_after_repeated_passes(self, live_position):
        _seed_plan()
        for _ in range(3):
            asyncio.run(exits.run_monitor_once())

        stored = _stored()
        assert stored.get("plan") == PLAN, (
            "the exit plan was dropped by the monitor — every trade would "
            "silently revert to the strategy default stop")
        # The frozen entry ATR must survive too; the stop distance must not
        # drift with changing volatility over the life of the trade.
        assert stored.get("atr") == ATR

    def test_the_peak_still_updates_alongside_the_plan(self, live_position, monkeypatch):
        _seed_plan()
        asyncio.run(exits.run_monitor_once())

        async def higher():
            return [{"symbol": "NVDA", "avg_entry_price": ENTRY,
                     "current_price": 130.0, "qty": 10.0, "side": "long"}]

        monkeypatch.setattr(exits, "_get_positions", higher)
        asyncio.run(exits.run_monitor_once())

        stored = _stored()
        assert stored["peak"] == 130.0, "merging must not freeze the peak"
        assert stored["plan"] == PLAN, "updating the peak must not drop the plan"


class TestTheEnforcedStopMatchesThePlan:
    def test_plan_widens_the_stop_the_monitor_uses(self, live_position):
        _seed_plan()
        asyncio.run(exits.run_monitor_once())

        atr_pct = ATR / ENTRY * 100
        planned = exits.effective_stop_pct("unknown", atr_pct, _stored()["plan"])
        default = exits.effective_stop_pct("unknown", atr_pct)

        assert planned == pytest.approx(14.0)   # 3.5 x 4%
        assert default == pytest.approx(10.0)   # 2.5 x 4%, the shared default
        assert planned > default

    def test_a_position_with_no_plan_is_unchanged(self, live_position):
        _seed_plan(plan=None)
        asyncio.run(exits.run_monitor_once())

        stored = _stored()
        assert "plan" not in stored
        atr_pct = ATR / ENTRY * 100
        assert exits.effective_stop_pct("unknown", atr_pct, stored.get("plan")) == \
            exits.effective_stop_pct("unknown", atr_pct)


class TestPlanCannotDefeatTheFloors:
    def test_an_absurdly_tight_plan_is_floored(self, live_position, monkeypatch):
        # CLAUDE.md: an unfloored trail closed a live ARB position on a 0.15%
        # wiggle for +0.49%. The floors apply to a model-chosen stop too.
        monkeypatch.setenv("STOP_MIN_PCT", "1.5")
        from raanu import config
        config.reset_exit_config()

        _seed_plan({"stop_atr_mult": 1.5})
        asyncio.run(exits.run_monitor_once())

        # 1.5 x 0.1% ATR would be 0.15% — inside the spread.
        assert exits.effective_stop_pct("unknown", 0.1, _stored()["plan"]) == 1.5
