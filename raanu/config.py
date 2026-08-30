"""
raanu.config — every environment read in the application, in one place
=======================================================================
**Everything here is lazy.** Nothing reads ``os.environ`` at import time.

That is the whole point of this module. The flat codebase snapshotted ~40
env vars into module-level constants at import — ``profit_monitor`` alone
froze 18 of them, plus more in ``server``, ``auto_trader`` and ``kelly``.
That only worked because ``lambda_secrets.load_ssm_secrets()`` happened to
run before the first import; any change to import order would have left
those constants holding defaults while the real values sat unused in
``os.environ``, silently, with no error. Lazy accessors make the ordering
irrelevant and let tests set config with ``monkeypatch.setenv`` instead of
reimporting modules.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# ── primitives ───────────────────────────────────────────────────────────────


def env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def env_int(name: str, default: int) -> int:
    try:
        return int(env_str(name) or default)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(env_str(name) or default)
    except ValueError:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    raw = env_str(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def env_list(name: str, default: str = "") -> list[str]:
    return [p.strip() for p in env_str(name, default).split(",") if p.strip()]


def _per_strategy(prefix: str, strategy: str, fallback):
    """Read ``{prefix}_{S1|S2|S3}``, falling back to ``{prefix}``.

    Capital and attempts follow conviction in this project, so almost every
    trading limit is per strategy with a global default behind it.
    """
    raw = env_str(f"{prefix}_{(strategy or 's1').upper()}")
    return raw if raw else fallback


# ── broker ───────────────────────────────────────────────────────────────────


def alpaca_key() -> str:
    return env_str("ALPACA_API_KEY")


def alpaca_secret() -> str:
    return env_str("ALPACA_SECRET_KEY")


def alpaca_mode() -> str:
    mode = env_str("ALPACA_MODE", "paper").lower()
    return mode if mode in ("paper", "live") else "paper"


def broker_base() -> str:
    return ("https://api.alpaca.markets/v2" if alpaca_mode() == "live"
            else "https://paper-api.alpaca.markets/v2")


def alpaca_data_feed() -> str:
    return env_str("ALPACA_DATA_FEED", "iex").lower()


# ── api auth ─────────────────────────────────────────────────────────────────


def api_read_token() -> str:
    return env_str("API_READ_TOKEN")


def trade_pin() -> str:
    return env_str("TRADE_PIN")


def allowed_origins() -> list[str]:
    return env_list("ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000")


# ── state / infrastructure ───────────────────────────────────────────────────


def state_backend() -> str:
    return env_str("STATE_BACKEND", "file").lower()


def state_table() -> str:
    return env_str("STATE_TABLE")


def worker_function_name() -> str:
    """Set only where a worker Lambda exists to invoke — i.e. on AWS."""
    return env_str("WORKER_FUNCTION_NAME")


def data_dir_override() -> str:
    return env_str("DATA_DIR")


# ── scanning ─────────────────────────────────────────────────────────────────


def scan_shards() -> int:
    """Fan-out width for a fast (interactive) scan.

    8 is a balance, not a limit: more shards cut wall-clock further but each
    pays its own cold start, and the whole fleet is far inside the Lambda
    free tier either way, so there is nothing to save by going narrower.
    """
    return max(1, env_int("SCAN_SHARDS", 8))


def scan_batch_size() -> int:
    """Tickers per yfinance call *within* a shard.

    Deliberately small. The old scanner used 250, which meant no progress was
    reported for the ~53s that download took — the UI sat at 0% and then
    jumped. Smaller batches report progress mid-shard.
    """
    return max(1, env_int("SCAN_BATCH_SIZE", 20))


def min_signal_score() -> int:
    """The auto-trader's BUY gate — a pick must clear this to be ordered.

    Defaults to 70, not 60. The flat codebase had both numbers for the same
    variable: ``auto_trader`` gated at 70 while ``/api/health`` reported 60.
    Since the variable is unset on AWS the default was live, so the dashboard
    was displaying a threshold the bot was not enforcing. 70 is the value
    that was actually in force, and lowering it here would quietly loosen
    the buy gate — so 70 it is, from one definition.

    Distinct from the per-strategy *surfacing* thresholds in the scan engine,
    which decide what appears in Live Signals rather than what gets bought.
    """
    return env_int("MIN_SIGNAL_SCORE", 70)


# ── trading limits ───────────────────────────────────────────────────────────


def weekly_trade_limit(strategy: str = "") -> int:
    defaults = {"s1": 2, "s2": 1, "s3": 3}
    fallback = defaults.get((strategy or "").lower(), env_int("WEEKLY_TRADE_LIMIT", 2))
    try:
        return int(_per_strategy("WEEKLY_TRADE_LIMIT", strategy, fallback))
    except (TypeError, ValueError):
        return int(fallback)


def per_trade_max_usd(strategy: str = "") -> float:
    defaults = {"s1": 1000.0, "s2": 100.0, "s3": 5000.0}
    fallback = defaults.get((strategy or "").lower(), env_float("PER_TRADE_MAX_USD", 1000.0))
    try:
        return float(_per_strategy("PER_TRADE_MAX_USD", strategy, fallback))
    except (TypeError, ValueError):
        return float(fallback)


def cash_reserve_pct() -> float:
    """Share of EQUITY (not free cash) kept liquid. Measured against equity so
    it means "keep this much of the account uninvested" rather than "a share
    of whatever happens to be left" — on 13 Aug 2026 the bot deployed
    $99,414 of a $99,414 account because nothing bounded the total."""
    return env_float("CASH_RESERVE_PCT", 30.0)


def cash_share(strategy: str) -> float:
    """Per-strategy slice of the deployable budget. Without these, whichever
    strategy ran first could spend the whole account — and once did."""
    defaults = {"s1": 30.0, "s2": 20.0, "s3": 50.0}
    return env_float(f"CASH_SHARE_{strategy.upper()}", defaults.get(strategy.lower(), 0.0))


def max_position_pct() -> float:
    return env_float("MAX_POSITION_PCT", 10.0)


def auto_trade_enabled() -> bool:
    return env_bool("AUTO_TRADE_ENABLED", False)


def watchlist() -> list[str]:
    return [t.upper() for t in env_list("WATCHLIST", "AAPL,MSFT,NVDA,GOOGL,AMZN")]


# ── position sizing (kelly) ──────────────────────────────────────────────────


def kelly_fraction() -> float:
    return env_float("KELLY_FRACTION", 0.25)


def kelly_min_sample() -> int:
    return env_int("KELLY_MIN_SAMPLE", 30)


def kelly_max_risk_pct() -> float:
    return env_float("KELLY_MAX_RISK_PCT", 2.0)


def kelly_fallback_risk_pct() -> float:
    return env_float("KELLY_FALLBACK_RISK_PCT", 0.5)


# ── exits ────────────────────────────────────────────────────────────────────

# "book progressively more as the trade goes higher": each rung says once the
# position's PEAK gain reaches X%, never give back below a locked Y% profit.
# The floor only ever ratchets up. Runs alongside the trailing stop; whichever
# triggers first exits (ladder-only, with the trail off, tested at -9.35%).
_LADDER_DEFAULT = "5:2,10:6,15:11,20:15,30:24"


@dataclass
class ExitConfig:
    """Exit rules: env-seeded defaults, mutable at runtime via
    ``PATCH /api/exit-config``.

    Replaces the old ``_exit_config`` dict plus ``_refresh_globals()``, which
    mirrored all 18 values into module globals so the exit loop could read
    them as bare names. Attribute access on one object does the same job
    without the second copy that could drift out of sync.
    """

    stop_mode: str = ""
    stop_atr_mult: float = 0.0
    stop_atr_mult_s1: float = 0.0
    stop_atr_mult_s2: float = 0.0
    stop_atr_mult_s3: float = 0.0
    stop_loss_pct: float = 0.0
    stop_max_pct: float = 0.0
    stop_min_pct: float = 0.0
    trail_mode: str = ""
    trail_activate_atr: float = 0.0
    trail_atr_mult: float = 0.0
    trail_min_pct: float = 0.0
    trail_activate_min_pct: float = 0.0
    trail_activate_pct: float = 0.0
    trail_pct: float = 0.0
    hard_take_profit_pct: float = 0.0
    daily_crash_pct: float = 0.0
    check_interval: int = 300
    profit_ladder: str = ""
    profit_ladder_s1: str = ""
    profit_ladder_s2: str = ""
    profit_ladder_s3: str = ""
    _str_fields: frozenset = field(
        default_factory=lambda: frozenset(
            {"stop_mode", "trail_mode", "profit_ladder",
             "profit_ladder_s1", "profit_ladder_s2", "profit_ladder_s3"}
        ),
        repr=False, compare=False,
    )

    @classmethod
    def from_env(cls) -> ExitConfig:
        return cls(
            # "atr" scales the stop to how much the stock actually moves in a
            # day. A fixed percentage stop sits inside the daily range of a
            # volatile name, so it exits on noise rather than on the thesis
            # failing — measured live: 70% of exits were stop-outs and 0 of 50
            # losing trades ever reached +5%.
            stop_mode=env_str("STOP_MODE", "atr").lower(),
            # Breakouts (S2) need more room to hold a retest than pullbacks.
            stop_atr_mult=env_float("STOP_ATR_MULT", 2.5),
            stop_atr_mult_s1=env_float("STOP_ATR_MULT_S1", 2.5),
            stop_atr_mult_s2=env_float("STOP_ATR_MULT_S2", 3.0),
            stop_atr_mult_s3=env_float("STOP_ATR_MULT_S3", 3.0),
            stop_loss_pct=env_float("STOP_LOSS_PCT", 3.0),
            stop_max_pct=env_float("STOP_MAX_PCT", 25.0),
            # Floor: very quiet instruments compute an ATR stop tighter than
            # the bid-ask spread, which would exit on a tick.
            stop_min_pct=env_float("STOP_MIN_PCT", 1.5),
            trail_mode=env_str("TRAIL_MODE", "atr").lower(),
            trail_activate_atr=env_float("TRAIL_ACTIVATE_ATR", 2.0),
            trail_atr_mult=env_float("TRAIL_ATR_MULT", 1.5),
            # Floors, for the same reason the stop has one. On a 0.10%-ATR
            # instrument an unfloored trail arms at +0.20% and exits on a
            # 0.15% give-back — closing on noise for a rounding-error gain.
            trail_min_pct=env_float("TRAIL_MIN_PCT", 3.0),
            trail_activate_min_pct=env_float("TRAIL_ACTIVATE_MIN_PCT", 2.5),
            trail_activate_pct=env_float("TRAIL_ACTIVATE_PCT", env_float("TAKE_PROFIT_PCT", 5.0)),
            trail_pct=env_float("TRAIL_PCT", 2.5),
            hard_take_profit_pct=env_float("HARD_TAKE_PROFIT_PCT", 0.0),
            daily_crash_pct=env_float("DAILY_CRASH_PCT", 8.0),
            check_interval=env_int("PROFIT_CHECK_SEC", 300),
            # Per strategy, because the ladder is NOT universally good: it
            # helps S2 (+13.26% -> +15.55%) and hurts S3 (+33.89% -> +22.34%),
            # booking winners before they mature.
            profit_ladder=env_str("PROFIT_LADDER", _LADDER_DEFAULT),
            profit_ladder_s1=env_str("PROFIT_LADDER_S1", _LADDER_DEFAULT),
            profit_ladder_s2=env_str("PROFIT_LADDER_S2", _LADDER_DEFAULT),
            profit_ladder_s3=env_str("PROFIT_LADDER_S3", ""),
        )

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

    def apply(self, updates: dict) -> dict:
        """Runtime override from the API. Unknown keys are ignored rather
        than raising, so a stale dashboard cannot 500 the endpoint."""
        for key, value in (updates or {}).items():
            if key not in self.as_dict():
                continue
            if key in self._str_fields:
                setattr(self, key, str(value).strip().lower())
            elif key == "check_interval":
                setattr(self, key, int(value))
            else:
                setattr(self, key, float(value))
        return self.as_dict()

    def stop_atr_mult_for(self, strategy: str) -> float:
        """Untagged positions ("unknown") deliberately fall through to the
        shared default, which is identical to the S1 value — a label, not a
        different rule. Verify that equivalence before adding any new
        per-strategy key, or "unknown" silently starts meaning something."""
        return getattr(self, f"stop_atr_mult_{(strategy or '').lower()}", self.stop_atr_mult)

    def ladder_for(self, strategy: str) -> str:
        return getattr(self, f"profit_ladder_{(strategy or '').lower()}", self.profit_ladder)


_exit_config: ExitConfig | None = None


def exit_config() -> ExitConfig:
    """Process-wide exit config. Built on first use, not at import, so SSM
    secrets loaded during Lambda init are already in place."""
    global _exit_config
    if _exit_config is None:
        _exit_config = ExitConfig.from_env()
    return _exit_config


def reset_exit_config() -> None:
    """Drop the cached config so the next call re-reads the environment.
    Used by tests; there is no runtime caller."""
    global _exit_config
    _exit_config = None


# ── notifications ────────────────────────────────────────────────────────────


def telegram_token() -> str:
    return env_str("TELEGRAM_BOT_TOKEN")


def telegram_chat_id(strategy: str = "") -> str:
    if strategy:
        per = env_str(f"TELEGRAM_CHAT_ID_{strategy.upper()}")
        if per:
            return per
    return env_str("TELEGRAM_CHAT_ID")


def vapid_public_key() -> str:
    return env_str("VAPID_PUBLIC_KEY")


def vapid_private_key() -> str:
    return env_str("VAPID_PRIVATE_KEY")


def vapid_claim_email() -> str:
    return env_str("VAPID_CLAIM_EMAIL", "mailto:admin@example.com")


def fcm_service_account_json() -> str:
    return env_str("FCM_SERVICE_ACCOUNT_JSON")


def push_scans_enabled() -> bool:
    return env_bool("PUSH_SCANS", True)


def notif_retain_hours() -> float:
    return env_float("NOTIF_RETAIN_HOURS", 48.0)


def twilio_sid() -> str:
    return env_str("TWILIO_ACCOUNT_SID")


def twilio_token() -> str:
    return env_str("TWILIO_AUTH_TOKEN")


def twilio_whatsapp_from() -> str:
    return env_str("TWILIO_WHATSAPP_FROM")


def user_whatsapp() -> str:
    return env_str("USER_WHATSAPP")


# ── android / TWA ────────────────────────────────────────────────────────────


def twa_fingerprint() -> str:
    return env_str("TWA_SHA256_FINGERPRINT")


def twa_package_name() -> str:
    return env_str("TWA_PACKAGE_NAME", "app.raanu.mobile")
