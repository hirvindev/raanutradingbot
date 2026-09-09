"""Config must be read lazily, per call.

The bug this guards against is the one the flat codebase actually had: env
vars snapshotted into module globals at import, so anything that set the
environment afterwards (SSM secrets on Lambda, monkeypatch in a test) was
silently ignored.
"""

from __future__ import annotations

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


class TestPerStrategy:
    def test_defaults_follow_conviction(self):
        # S3 is the only strategy profitable in both halves of the backtest,
        # so it gets the most attempts and the most capital.
        assert config.weekly_trade_limit("s3") == 3
        assert config.weekly_trade_limit("s2") == 1
        assert config.per_trade_max_usd("s3") == 5000.0
        assert config.per_trade_max_usd("s2") == 100.0
        assert config.cash_share("s3") == 50.0

    def test_env_overrides_per_strategy(self, monkeypatch):
        monkeypatch.setenv("WEEKLY_TRADE_LIMIT_S3", "7")
        assert config.weekly_trade_limit("s3") == 7
        assert config.weekly_trade_limit("s1") == 2

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

    def test_budget_share_cap_bounds_concentration(self, monkeypatch):
        assert config.llm_max_budget_share() == 60.0
        monkeypatch.setenv("LLM_MAX_BUDGET_SHARE", "40")
        assert config.llm_max_budget_share() == 40.0

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
