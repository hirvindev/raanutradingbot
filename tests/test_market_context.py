"""The market picture the advisor reasons from.

The maths is small; what matters is that a missing symbol degrades the picture
instead of failing the slot, and that the advisor is told what is missing
rather than silently reasoning from a hole.
"""

from __future__ import annotations

import pytest

from raanu.ai import market_context
from raanu.ai.market_context import _read, headline, snapshot


def _snap(open_px, last_px, prev_close):
    return {"dailyBar": {"o": open_px, "c": last_px}, "prevDailyBar": {"c": prev_close}}


class TestThreeReadings:
    def test_gap_day_and_since_open_are_distinct_questions(self):
        # Gapped down 2%, then bought back to -0.5% on the day. Direction alone
        # would call this red; since_open says it is being accumulated.
        r = _read(_snap(98.0, 99.5, 100.0))
        assert r["gap_pct"] == -2.0
        assert r["day_pct"] == -0.5
        assert r["since_open_pct"] == pytest.approx(1.53, abs=0.01)

    def test_a_gap_that_keeps_sliding(self):
        r = _read(_snap(98.0, 96.0, 100.0))
        assert r["gap_pct"] == -2.0
        assert r["since_open_pct"] == pytest.approx(-2.04, abs=0.01)

    @pytest.mark.parametrize("payload", [
        {}, None, "nonsense",
        {"dailyBar": {"o": 0, "c": 0}, "prevDailyBar": {"c": 0}},
        {"dailyBar": {"o": 100, "c": 101}},                       # no prev close
        {"dailyBar": {"o": "x", "c": "y"}, "prevDailyBar": {"c": 1}},
    ])
    def test_unusable_payloads_return_none_rather_than_raising(self, payload):
        assert _read(payload) is None


class TestBreadth:
    def _with(self, monkeypatch, greens):
        data = {}
        for i, sym in enumerate(market_context.SECTORS):
            last = 101.0 if i < greens else 99.0
            data[sym] = _snap(100.0, last, 100.0)
        for sym in market_context.BROAD:
            data[sym] = _snap(100.0, 100.5, 100.0)
        monkeypatch.setattr(market_context, "get_snapshots", lambda t: data, raising=False)
        monkeypatch.setattr("raanu.market.broker.get_snapshots", lambda t: data)
        monkeypatch.setattr(market_context, "_vix", lambda: {"level": 15.0, "mean_20d": 14.0,
                                                             "vs_mean_pct": 7.1})
        return snapshot()

    def test_counts_green_and_red_sectors(self, monkeypatch):
        ctx = self._with(monkeypatch, greens=3)
        assert ctx["breadth"] == {"sectors_green": 3, "sectors_red": 8,
                                  "sectors_reporting": 11}

    def test_a_unanimous_tape_is_visible(self, monkeypatch):
        # 11/11 red is the kind of reading that should give a dip-buyer pause;
        # 6/11 is an ordinary day. The advisor needs to tell them apart.
        ctx = self._with(monkeypatch, greens=0)
        assert ctx["breadth"]["sectors_red"] == 11


class TestDegradesRatherThanFails:
    def test_no_broker_yields_an_empty_but_valid_picture(self, monkeypatch):
        monkeypatch.setattr("raanu.market.broker.get_snapshots", lambda t: {})
        monkeypatch.setattr(market_context, "_vix", lambda: None)
        ctx = snapshot()
        assert ctx["broad"] == {} and ctx["sectors"] == {}
        assert ctx["breadth"]["sectors_reporting"] == 0
        assert "no broker snapshots" in ctx["partial"]

    def test_a_broker_exception_does_not_propagate(self, monkeypatch):
        def boom(_):
            raise RuntimeError("alpaca down")

        monkeypatch.setattr("raanu.market.broker.get_snapshots", boom)
        monkeypatch.setattr(market_context, "_vix", lambda: None)
        assert snapshot()["partial"]

    def test_missing_symbols_are_named_in_partial(self, monkeypatch):
        monkeypatch.setattr("raanu.market.broker.get_snapshots",
                            lambda t: {"SPY": _snap(100.0, 101.0, 100.0)})
        monkeypatch.setattr(market_context, "_vix", lambda: None)
        ctx = snapshot()
        # The advisor is told what it could not see, so it can weigh its own
        # confidence instead of treating a hole as calm.
        assert "SPY" not in ctx["partial"]
        assert "QQQ" in ctx["partial"] and "VIX" in ctx["partial"]

    def test_a_broken_vix_does_not_fail_the_snapshot(self, monkeypatch):
        monkeypatch.setattr("raanu.market.broker.get_snapshots", lambda t: {})
        monkeypatch.setattr("raanu.market.prices.fetch_ohlc",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("yf down")))
        assert snapshot()["vix"] is None


class TestHeadline:
    def test_reads_as_a_sentence(self):
        ctx = {"broad": {"SPY": {"day_pct": -0.49, "gap_pct": -1.96}},
               "breadth": {"sectors_green": 2, "sectors_red": 9, "sectors_reporting": 11},
               "vix": {"level": 24.1, "vs_mean_pct": 38.0}}
        line = headline(ctx)
        assert "SPY -0.49%" in line and "2/11 green" in line and "VIX 24.1" in line

    def test_says_so_when_there_is_nothing(self):
        assert headline({}) == "no market data"
