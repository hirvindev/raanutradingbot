"""Config must be read lazily, per call.

The bug this guards against is the one the flat codebase actually had: env
vars snapshotted into module globals at import, so anything that set the
environment afterwards (SSM secrets on Lambda, monkeypatch in a test) was
silently ignored.
"""

from __future__ import annotations

import pytest

from raanu import config


class TestLaziness:
    def test_env_change_is_visible_without_reimport(self, monkeypatch):
        monkeypatch.setenv("MIN_SIGNAL_SCORE", "42")
        assert config.min_signal_score() == 42
        monkeypatch.setenv("MIN_SIGNAL_SCORE", "77")
        assert config.min_signal_score() == 77

    def test_exit_config_is_built_on_first_use_not_at_import(self, monkeypatch):
        monkeypatch.setenv("STOP_ATR_MULT_S1", "9.5")
        config.reset_exit_config()
        assert config.exit_config().stop_atr_mult_s1 == 9.5


class TestCoercion:
    def test_malformed_values_fall_back_instead_of_raising(self, monkeypatch):
        # A typo'd env var must not take the whole Lambda down on cold start.
        monkeypatch.setenv("MIN_SIGNAL_SCORE", "not-a-number")
        monkeypatch.setenv("KELLY_FRACTION", "")
        assert config.min_signal_score() == 70
        assert config.kelly_fraction() == 0.25

    def test_min_signal_score_defaults_to_the_gate_actually_enforced(self):
        # The flat codebase had two defaults for this one variable: the
        # auto-trader gated at 70 while /api/health reported 60. The variable
        # is unset on AWS, so 70 was live and the dashboard was displaying a
        # threshold nothing enforced. 70 is the number that was real.
        assert config.min_signal_score() == 70

    def test_bool_parsing(self, monkeypatch):
        for raw, expected in [("1", True), ("true", True), ("YES", True),
                              ("on", True), ("0", False), ("no", False)]:
            monkeypatch.setenv("AUTO_TRADE_ENABLED", raw)
            assert config.auto_trade_enabled() is expected

    def test_auto_trade_defaults_off(self):
        # The bot must never boot into a trading state by accident.
        assert config.auto_trade_enabled() is False

    def test_alpaca_mode_rejects_garbage_and_defaults_to_paper(self, monkeypatch):
        monkeypatch.setenv("ALPACA_MODE", "nonsense")
        assert config.alpaca_mode() == "paper"
        assert "paper-api" in config.broker_base()

    def test_live_mode_selects_live_broker(self, monkeypatch):
        monkeypatch.setenv("ALPACA_MODE", "live")
        assert config.broker_base() == "https://api.alpaca.markets/v2"


class TestWeeklyPool:
    """One allowance across every strategy, in trades AND dollars.

    The per-strategy quotas are gone: they meant a capped strategy could not
    lend its allowance to an uncapped one, and the split (2/1/3 = 6) was not
    usable as a pool. The advisor allocates the pool now.
    """

    def test_the_pool_is_ten_trades_and_twenty_thousand_dollars(self):
        # $2,000 x 10 = $20,000 exactly, so the count and the dollars run out
        # together rather than one making the other unreachable.
        assert config.weekly_trade_limit() == 10
        assert config.weekly_budget_usd() == 20000.0
        assert config.per_trade_max_usd() == 2000.0

    def test_both_limits_are_configurable(self, monkeypatch):
        monkeypatch.setenv("WEEKLY_TRADE_LIMIT", "10")
        monkeypatch.setenv("WEEKLY_BUDGET_USD", "12000")
        assert config.weekly_trade_limit() == 10
        assert config.weekly_budget_usd() == 12000.0

    def test_it_takes_no_strategy_argument(self):
        # The old signature accepted one and returned a different number per
        # strategy. Leaving it would let callers keep asking a question that
        # no longer has a per-strategy answer.
        with pytest.raises(TypeError):
            config.weekly_trade_limit("s3")

    def test_the_per_strategy_ceiling_is_inert_by_default(self):
        # A dial, not a quota: defaults to the whole pool.
        assert config.weekly_max_per_strategy() == config.weekly_trade_limit()

    def test_the_minimum_trade_stops_dust_positions(self):
        assert config.weekly_min_trade_usd() == 100.0

    def test_the_equity_reserve_is_off_by_default(self):
        # The weekly ceiling replaced it — an absolute cap on new exposure
        # rather than a percentage of a moving equity figure.
        assert config.cash_reserve_pct() == 0.0

    def test_the_per_trade_cap_is_one_number_unless_overridden(self, monkeypatch):
        # The old per-strategy DEFAULTS (s1 1000, s2 100, s3 5000) predate the
        # pooled budget and fought with it. The override mechanism survives.
        assert config.per_trade_max_usd("s3") == 2000.0
        monkeypatch.setenv("PER_TRADE_MAX_USD_S2", "250")
        assert config.per_trade_max_usd("s2") == 250.0

    def test_unknown_strategy_falls_through_to_shared_default(self):
        # "unknown" is what an unattributable position gets. It must resolve
        # to the shared default, never guess a strategy.
        cfg = config.exit_config()
        assert cfg.stop_atr_mult_for("unknown") == cfg.stop_atr_mult
        assert cfg.stop_atr_mult_for("s2") == 3.0


class TestExitConfigOverrides:
    def test_runtime_update_sticks_and_coerces_types(self):
        cfg = config.exit_config()
        cfg.apply({"stop_atr_mult": "4.0", "stop_mode": " PCT ", "check_interval": "120"})
        assert cfg.stop_atr_mult == 4.0
        assert cfg.stop_mode == "pct"
        assert cfg.check_interval == 120

    def test_unknown_keys_are_ignored_not_fatal(self):
        # A stale dashboard posting a removed field must not 500 the endpoint.
        cfg = config.exit_config()
        assert cfg.apply({"no_such_setting": 1})["stop_mode"] == "atr"

    def test_ladder_is_off_for_s3_by_default(self):
        # The ladder lifts S3's win rate 59.4% -> 68.7% while payoff collapses
        # 0.93 -> 0.58: it books winners before they mature. Off for S3.
        cfg = config.exit_config()
        assert cfg.ladder_for("s3") == ""
        assert cfg.ladder_for("s2") == "5:2,10:6,15:11,20:15,30:24"


class TestLLMAdvisorDefaults:
    """Every advisory power ships off. The bot must never gain a new way to
    spend money as a side effect of a deploy."""

    def test_all_advisory_switches_default_off(self):
        assert config.llm_advisor_enabled() is False
        assert config.llm_shadow_mode() is False
        assert config.llm_exits_enabled() is False
        assert config.llm_budget_enabled() is False
        assert config.llm_retro_enabled() is False

    def test_tracing_defaults_ON(self):
        # The exception, deliberately: tracing is safe, and turning it off is
        # what makes everything else undebuggable.
        assert config.trace_enabled() is True

    def test_switches_are_read_per_call(self, monkeypatch):
        monkeypatch.setenv("LLM_ADVISOR_ENABLED", "1")
        assert config.llm_advisor_enabled() is True
        monkeypatch.setenv("LLM_ADVISOR_ENABLED", "0")
        assert config.llm_advisor_enabled() is False

    def test_model_default_is_pinned(self):
        # A silent model change would alter every trading decision the advisor
        # makes, so this is config, never an implicit default.
        assert config.llm_model() == "claude-sonnet-5"

    def test_timeout_is_generous_enough_for_web_search(self, monkeypatch):
        # A premature timeout reads as fail-closed, i.e. a day with no trades.
        # 30 was the original floor and it was not enough: the advisor's first
        # live slot (9 Sep 2026) timed out three times at 60s each and traded
        # nothing, so the floor moved with the default.
        assert config.llm_timeout_sec() >= 120
        monkeypatch.setenv("LLM_TIMEOUT_SEC", "90")
        assert config.llm_timeout_sec() == 90.0

    def test_the_retry_budget_is_bounded_rather_than_left_to_the_sdk(self, monkeypatch):
        # The SDK retries timeouts and defaults to 2 retries, so an unset
        # value silently triples the wall clock — 60s became 185s live.
        assert config.llm_max_retries() == 1
        monkeypatch.setenv("LLM_MAX_RETRIES", "0")
        assert config.llm_max_retries() == 0

    def test_the_per_strategy_ceiling_can_bound_concentration(self, monkeypatch):
        # Replaces LLM_MAX_BUDGET_SHARE, which capped a strategy's slice of a
        # split that no longer exists. Off by default — the pool is meant to
        # be unrestricted by strategy — but available if the retro shows the
        # advisor piling into the two strategies with the weaker record.
        monkeypatch.setenv("WEEKLY_MAX_PER_STRATEGY", "4")
        assert config.weekly_max_per_strategy() == 4

    def test_trace_retention_is_configurable(self, monkeypatch):
        assert config.trace_retain_days() == 90
        monkeypatch.setenv("TRACE_RETAIN_DAYS", "30")
        assert config.trace_retain_days() == 30

    def test_malformed_llm_values_fall_back(self, monkeypatch):
        monkeypatch.setenv("LLM_TIMEOUT_SEC", "soon")
        monkeypatch.setenv("TRACE_RETAIN_DAYS", "forever")
        monkeypatch.setenv("LLM_SEARCH_MAX_USES", "lots")
        assert config.llm_timeout_sec() == 150.0
        assert config.trace_retain_days() == 90
        assert config.llm_search_max_uses() == 2
