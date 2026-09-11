"""The pooled weekly budget, end to end through the executor.

One pot of trades and dollars, drawn down in the advisor's cross-strategy
rank order. These are the scenarios where money actually moves, so they are
written as "what should the account look like afterwards", not as unit tests
of the helpers.

⚠️ CLAUDE.md used to say "do not go back to a single shared pot", after S1 and
S2 consumed a whole account on 13 Aug 2026 and S3 — holding candidates scoring
90, 84 and 73 — reached $0.01. That rule is deliberately reversed, and the two
things that make it safe now did not exist then: an absolute weekly dollar
ceiling, and an explicit cross-strategy ranking so execution order is a
decision rather than an artefact of a for-loop. Several tests below exist
specifically to keep both of those true.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from raanu import state
from raanu.ai.schema import SlotVerdict
from raanu.state import keys
from raanu.trading import schedule, trader


def _buy(usd: float, strategy: str = "s1", days_ago: float = 1.0):
    stamp = keys.stamp(datetime.now(UTC) - timedelta(days=days_ago))
    state.put(keys.TRADE, keys.trade_sk(stamp),
              {"action": "BUY", "ticker": "OLD", "notional_usd": usd,
               "strategy": strategy, "timestamp": stamp})


@pytest.fixture
def book(monkeypatch):
    """An executor with the network removed. Records every order placed."""
    placed = []

    async def fake_buy(ticker, notional, *a, **k):
        placed.append({"ticker": ticker, "notional": notional})
        return {"status": "accepted", "client_order_id": f"x-{ticker}"}

    async def open_market():
        return (True, "open")

    monkeypatch.setattr(trader, "alpaca_buy_notional", fake_buy)
    monkeypatch.setattr(trader, "market_is_open", open_market)
    monkeypatch.setattr(trader, "get_free_cash", lambda: _val(100_000.0))
    monkeypatch.setattr(trader, "get_held_symbols", lambda: _val(set()))
    monkeypatch.setattr(schedule, "alpaca_get", lambda p: _val({"equity": 100_000.0}))
    monkeypatch.setattr(schedule, "_send_confident_buy_alerts",
                        lambda picks, strategy="s1", slot="": None)
    monkeypatch.setattr(schedule, "_seed_position_plan", lambda *a, **k: None)

    from raanu.notify import telegram
    monkeypatch.setattr(telegram, "send_whatsapp", lambda *a, **k: True)

    # Sizing: a fixed, generous risk budget so the POOL is what binds.
    from raanu.trading import sizing

    class K:
        tradeable, risk_pct, reason, sample = True, 1.0, "test", 99
        win_rate, payoff_b = 0.6, 1.5

    monkeypatch.setattr(sizing, "from_trade_log", lambda strategy=None: K())
    # exits is imported INSIDE the executor, so the patch has to land on the
    # source module rather than on schedule's namespace.
    from raanu.trading import exits
    monkeypatch.setattr(exits, "_get_atr", lambda t: _val(None))
    monkeypatch.setenv("STOP_MODE", "pct")
    # Pinned rather than inherited so these scenarios do not drift when the
    # shipped limits are retuned. $2,000 x 10 = the $20,000 week exactly, so
    # the COUNT and the DOLLARS run out together.
    from raanu import settings
    settings.put("weekly_budget_usd", 20_000)
    settings.put("weekly_trade_limit", 10)
    settings.put("per_trade_max_usd", 2_000)
    monkeypatch.setenv("MAX_POSITION_PCT", "100")
    trader.AutoTrader().enabled = True
    return placed


async def _val(v):
    return v


def _pick(ticker, strategy, score=80, rank=1):
    return {"ticker": ticker, "score": score, "price": 100.0,
            "_strategy": strategy, "_llm_rank": rank, "_llm_size_mult": 1.0,
            "_llm_exit_plan": {}, "reasons": []}


def _run(picks, n_orders=12, verdict=None):
    # Above the pool's own count, so the POOL is what binds
    # rather than this per-slot argument.
    asyncio.run(schedule._execute_scheduled_trades(
        n_orders, "test-slot", strategy=picks[0]["_strategy"],
        picks=picks, verdict=verdict))


class TestThePotIsShared:
    def test_any_strategy_may_take_any_share(self, book):
        # The whole point of pooling: five S1 names is a legitimate week.
        _run([_pick(f"T{i}", "s1", rank=i + 1) for i in range(5)])
        assert len(book) == 5
        assert {p["ticker"] for p in book} == {"T0", "T1", "T2", "T3", "T4"}

    def test_execution_follows_the_advisors_rank_across_strategies(self, book):
        _run([_pick("AAA", "s2", rank=1), _pick("BBB", "s3", rank=2),
              _pick("CCC", "s1", rank=3)])
        assert [p["ticker"] for p in book] == ["AAA", "BBB", "CCC"]

    def test_the_trade_count_stops_the_slot(self, book):
        _buy(100.0, days_ago=1)          # 1 of 10 already used
        _run([_pick(f"T{i}", "s1", rank=i + 1) for i in range(12)])
        assert len(book) == 9, "the pooled count is not bounding the slot"

    def test_the_dollar_ceiling_stops_the_slot(self, book):
        # $19,500 already committed: one $500 trade fits, the rest must not —
        # even though nine of the ten trade slots are still free.
        _buy(19_500.0, days_ago=1)
        _run([_pick(f"T{i}", "s1", rank=i + 1) for i in range(5)])
        assert sum(p["notional"] for p in book) <= 500.01
        assert len(book) == 1

    def test_a_trade_is_trimmed_to_fit_rather_than_skipped(self, book):
        # $500 left against a $2,000 cap: take the $500 rather than stand down
        # and leave the allowance to expire unused.
        _buy(19_500.0, days_ago=1)
        _run([_pick("AAA", "s1")])
        assert book and book[0]["notional"] == pytest.approx(500.0, abs=1)

    def test_dust_is_not_worth_placing(self, book):
        # $40 left cannot move the P&L, occupies a slot in the book, and
        # dilutes the per-trade sample Kelly reads.
        _buy(19_960.0, days_ago=1)
        _run([_pick("AAA", "s1")])
        assert book == []

    def test_spending_draws_the_shared_pot_down_for_later_picks(self, book):
        # Consecutive orders belong to different strategies; the second must
        # see what the first actually took, or the pot is not shared at all.
        _buy(17_500.0, days_ago=1)       # $2,500 left, $2,000 per-trade cap
        _run([_pick("AAA", "s3", rank=1), _pick("BBB", "s1", rank=2)])
        assert [round(p["notional"]) for p in book] == [2000, 500]


class TestTheAdvisorPaces:
    def _verdict(self, **kw):
        base = dict(trade_today=True, regime="neutral", market_summary="calm")
        base.update(kw)
        return SlotVerdict(**base)

    def test_it_can_hold_budget_back(self, book, monkeypatch):
        monkeypatch.setenv("LLM_BUDGET_ENABLED", "1")
        _run([_pick("AAA", "s1", rank=1), _pick("BBB", "s1", rank=2)],
             verdict=self._verdict(usd_to_deploy=1500,
                                   pacing_note="saving for Thursday"))
        assert sum(p["notional"] for p in book) <= 1500.01

    def test_it_cannot_enlarge_the_pool(self, book, monkeypatch):
        monkeypatch.setenv("LLM_BUDGET_ENABLED", "1")
        _buy(19_000.0, days_ago=1)       # only $1,000 genuinely left
        _run([_pick("AAA", "s1")], verdict=self._verdict(usd_to_deploy=50_000))
        assert sum(p["notional"] for p in book) <= 1000.01

    def test_pacing_is_ignored_when_the_flag_is_off(self, book, monkeypatch):
        monkeypatch.setenv("LLM_BUDGET_ENABLED", "0")
        _run([_pick("AAA", "s1")], verdict=self._verdict(usd_to_deploy=0))
        assert book, "budget control was applied while LLM_BUDGET_ENABLED=0"


class TestTheOptionalRail:
    def test_a_per_strategy_ceiling_bounds_concentration_when_set(self, book, monkeypatch):
        # Off by default. Available if the retro ever shows the advisor
        # piling into the two strategies with the weaker second-half record.
        monkeypatch.setenv("WEEKLY_MAX_PER_STRATEGY", "2")
        _run([_pick(f"T{i}", "s1", rank=i + 1) for i in range(5)])
        assert len(book) == 2

    def test_it_is_inert_by_default(self, book):
        _run([_pick(f"T{i}", "s1", rank=i + 1) for i in range(5)])
        assert len(book) == 5


class TestPerStrategyThingsStayPerStrategy:
    def test_each_order_is_tagged_with_its_own_strategy(self, book):
        # The BUDGET is pooled; attribution, per-trade caps and exit defaults
        # are not. A mis-tag would send a trade's P&L to another strategy's
        # track record, which is what kelly.py sizes future positions from.
        _run([_pick("AAA", "s3", rank=1), _pick("BBB", "s2", rank=2)])
        logged = {t["ticker"]: t["strategy"]
                  for t in trader.AutoTrader().tradelog.all_trades()
                  if t.get("action") == "BUY"}
        assert logged.get("AAA") == "s3"
        assert logged.get("BBB") == "s2"

    def test_the_per_trade_cap_is_the_picks_own_strategy(self, book, monkeypatch):
        monkeypatch.setenv("PER_TRADE_MAX_USD_S2", "250")
        _run([_pick("AAA", "s2", rank=1), _pick("BBB", "s3", rank=2)])
        by = {p["ticker"]: p["notional"] for p in book}
        assert by["AAA"] == pytest.approx(250.0, abs=1)
        assert by["BBB"] > 250.0


class TestCapsAndPoolMustAgree:
    """⚠️ The per-trade cap and the weekly pot are two ceilings on the same
    money, and they have to be set consistently.

    The SHIPPED defaults now agree — $2,000 x 10 = $20,000 exactly — but the
    per-strategy override can still break that, which is what these pin.
    An override above budget/limit makes the trade count unreachable: the
    dollars run out while trade slots sit unused, which is what
    PER_TRADE_MAX_USD_S3=$5,000 did against the old $7,000 week.
    """

    def test_a_large_per_trade_cap_makes_the_count_unreachable(self, book, monkeypatch):
        monkeypatch.setenv("PER_TRADE_MAX_USD_S3", "10000")
        _run([_pick(f"T{i}", "s3", rank=i + 1) for i in range(10)])
        assert len(book) == 2, "expected the dollar ceiling to bind first"
        assert sum(p["notional"] for p in book) == pytest.approx(20000.0, abs=1)

    def test_caps_at_budget_over_limit_let_the_count_bind(self, book, monkeypatch):
        monkeypatch.setenv("PER_TRADE_MAX_USD_S3", "2000")
        _run([_pick(f"T{i}", "s3", rank=i + 1) for i in range(12)])
        assert len(book) == 10
