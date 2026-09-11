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


def worker_schedule_rule_name() -> str:
    """The EventBridge rule that fires the worker's ET time slots.

    Set only on AWS. Empty locally, where there is no schedule to toggle —
    the dev-server loops in raanu/api/app.py run instead."""
    return env_str("WORKER_SCHEDULE_RULE_NAME")


def data_dir_override() -> str:
    return env_str("DATA_DIR")


# ── scanning ─────────────────────────────────────────────────────────────────


def scan_shards() -> int:
    """Fan-out width for a fast (interactive) scan.

    Bounded by **account Lambda concurrency, not cost**. This AWS account
    has a limit of 10 concurrent executions (the reduced quota AWS applies
    to new accounts, not the 1,000 default), and every shard is one. Fan out
    to 8 and a scan consumes almost the whole account: the API Lambda then
    gets `ConcurrentInvocationLimitExceeded` and the dashboard dies with a
    429 mid-scan, which is exactly what happened.

    4, not 6. The dashboard is itself a concurrency consumer: a refresh
    fires several reads and the scan poll runs every 1.5s, each one an API
    Lambda execution. At 6 shards a refresh mid-scan still pushed the
    account over 10, the overflow came back 429, and the scan poll lost its
    status feed. 4 shards + the browser's capped 3 concurrent reads + a poll
    leaves genuine headroom.

    Throttled *async* shard invokes are retried by AWS rather than lost, so
    over-fanning degrades the dashboard rather than the scan — which is
    exactly why it took three rounds to spot.

    Reserved concurrency on the API function would be the sturdier fix, but
    AWS refuses it unless unreserved stays >= 100, which a 10-limit account
    cannot satisfy. Raise the quota (Service Quotas -> Lambda -> Concurrent
    executions; free) and this can go up with it.
    """
    return max(1, env_int("SCAN_SHARDS", 4))


def scan_max_shards() -> int:
    """Ceiling when the planner scales shards up for a large universe.

    Same concurrency budget as above. Also: 8 shards measured only a 3.2x
    speedup over one host, not 8x — partly Yahoo throttling, and partly
    that some of those shards were queueing behind this very limit.
    """
    return max(1, env_int("SCAN_MAX_SHARDS", 4))


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


def _setting(name: str):
    """One editable setting, store-backed. Falls back to env then default.

    Routed through raanu.settings rather than read here so the value can be
    changed without a deploy — see that module for the bounds, the cache and
    why a read failure keeps the last known value instead of reverting to the
    (more permissive) default."""
    from raanu import settings
    return settings.get(name)


def weekly_trade_limit() -> int:
    """Trades per rolling 7 days, POOLED across every strategy.

    Was per strategy (s1 2, s2 1, s3 3 = 6, unusable as a pool because a
    capped strategy could not lend to an uncapped one). One number now, and
    the advisor decides which strategies spend it.

    ⚠️ Takes no ``strategy`` argument on purpose. The old signature accepted
    one and silently returned a different number per strategy; leaving it in
    place would let a caller keep asking a question that no longer has a
    per-strategy answer.

    Editable at runtime — stored in DynamoDB, see raanu.settings."""
    return int(_setting("weekly_trade_limit"))


def weekly_budget_usd() -> float:
    """Dollars of NEW BUY notional per rolling 7 days, pooled.

    The count limit alone does not bound risk: seven $5,000 trades and seven
    $200 trades are the same number and a 25x difference in exposure. This is
    the constraint that actually caps how much capital the week can commit.

    Counts BUY notional only — exits do not refund it, because the budget
    limits how much NEW exposure is opened per week, not net position.

    Editable at runtime — stored in DynamoDB, see raanu.settings."""
    return float(_setting("weekly_budget_usd"))


def weekly_min_trade_usd() -> float:
    """Smallest trade worth placing from what is left of the weekly pot.

    Without a floor the last few dollars of the budget become a $12 position:
    it pays commission-free but it cannot move the P&L, it occupies a slot in
    the book, and it dilutes the per-trade sample that Kelly reads."""
    return env_float("WEEKLY_MIN_TRADE_USD", 100.0)


def weekly_max_per_strategy() -> int:
    """Optional ceiling on how many of the weekly trades one strategy may take.

    Defaults to the full pool, i.e. OFF — the pooled budget is deliberately
    unrestricted by strategy, which is the whole point of pooling it.

    It exists as a dial because the evidence cuts both ways: S1 and S2 both
    collapse in the second half of the backtest while S3 survives, so an
    advisor that puts all seven trades into S1 is concentrating into the two
    strategies with the weaker record. If that shows up in the retro, set
    this rather than going back to per-strategy quotas."""
    return env_int("WEEKLY_MAX_PER_STRATEGY", weekly_trade_limit())


def per_trade_max_usd(strategy: str = "") -> float:
    """Hard ceiling on a single order, before Kelly sizing.

    One number for every strategy now ($2,000), editable at runtime. The old
    per-strategy defaults (s1 $1,000, s2 $100, s3 $5,000) predate the pooled
    weekly budget and fought with it: $5,000 against a $7,000 week meant two
    S3 trades exhausted the dollars while five trade slots sat unused.

    $2,000 x 10 trades = the $20,000 week exactly, so the count and the
    dollars run out together instead of one silently making the other
    unreachable.

    ``PER_TRADE_MAX_USD_S1/S2/S3`` still override per strategy for anyone who
    wants that back; there is simply no longer a different DEFAULT per
    strategy."""
    base = float(_setting("per_trade_max_usd"))
    try:
        return float(_per_strategy("PER_TRADE_MAX_USD", strategy, base))
    except (TypeError, ValueError):
        return base


def cash_reserve_pct() -> float:
    """Share of EQUITY held back from entries. Now 0 by default — OFF.

    ⚠️ Read the history before re-arming this. On 13 Aug 2026 the bot deployed
    $99,414 of a $99,414 account and left $0.01, because every gate bounded
    ONE order and nothing bounded the total committed at once. This reserve
    was the answer to that.

    ``weekly_budget_usd()`` is the answer now, and it is a tighter one: the
    reserve was a percentage of a moving equity figure that said nothing about
    pace, while $7,000 per rolling week is an absolute ceiling on new exposure
    regardless of account size. On a ~$100k account that is ~7% a week, so the
    13 Aug failure mode is bounded by roughly 14x more headroom than the
    reserve gave it.

    Kept as a dial rather than deleted: set CASH_RESERVE_PCT to re-arm it and
    it composes with the weekly budget as another floor on free cash."""
    return env_float("CASH_RESERVE_PCT", 0.0)


def max_position_pct() -> float:
    return env_float("MAX_POSITION_PCT", 10.0)


def auto_trade_enabled() -> bool:
    return env_bool("AUTO_TRADE_ENABLED", False)


def watchlist() -> list[str]:
    return [t.upper() for t in env_list("WATCHLIST", "AAPL,MSFT,NVDA,GOOGL,AMZN")]


# ── llm advisor ──────────────────────────────────────────────────────────────
# The LLM reviews the quant's candidates once per execution slot: whether the
# day is worth trading, which picks to take, how to split the budget, and how
# to exit each one. It can never invent a candidate the quant did not surface.
#
# Every switch here defaults OFF except tracing. The powers are separate flags
# on purpose — the two highest-risk ones (budget, exits) can be enabled later
# and independently of the gate itself, each with its own observation window.


def llm_advisor_enabled() -> bool:
    return env_bool("LLM_ADVISOR_ENABLED", False)


def llm_shadow_mode() -> bool:
    """Run the advisor and record its verdict, but do not act on it.

    The whole rollout hinges on this: it is how "did the LLM's vetoes actually
    correlate with worse outcomes" gets answered before any capital rides on
    the answer. Every conclusion in this project that skipped its equivalent
    turned out to be a first-half artefact."""
    return env_bool("LLM_ADVISOR_SHADOW", False)


def llm_exits_enabled() -> bool:
    """Let the advisor set per-trade stop / trail / ladder overrides."""
    return env_bool("LLM_EXITS_ENABLED", False)


def llm_budget_enabled() -> bool:
    """Let the advisor redistribute the per-strategy cash shares."""
    return env_bool("LLM_BUDGET_ENABLED", False)


def llm_provider() -> str:
    return env_str("LLM_PROVIDER", "anthropic").lower()


def llm_model() -> str:
    """Sonnet 5 ($3/$15 per MTok) rather than Opus 5 ($5/$25).

    At ~2 calls a day the absolute difference is a few dollars a month, so
    this is a deliberate choice rather than a forced one: revisit it if the
    shadow-mode data shows the advisor reasoning poorly about the
    counter-intuitive evidence it is given (the score does not rank; win rate
    is a misleading target). Set LLM_MODEL to override without a deploy.

    Thinking runs adaptively when the request omits `thinking`, and those
    tokens count against max_tokens — which is why the caller asks for 16000
    rather than something sized only to the JSON verdict."""
    return env_str("LLM_MODEL", "claude-sonnet-5")


def llm_api_key() -> str:
    return env_str("LLM_API_KEY")


def llm_timeout_sec() -> float:
    """150s per attempt. Read this together with llm_max_retries().

    60s was the original value and it failed closed on the advisor's very
    first live slot (9 Sep 2026, Open-9:35): three attempts, each hitting
    exactly 60s, 185s of wall clock, eleven actionable candidates, no orders.
    Nothing was wrong with the request — a call carrying adaptive thinking
    plus web search simply does not finish in 60s, and the old non-streaming
    call put no bytes on the socket while it worked, so a healthy request was
    indistinguishable from a dead connection.

    ⚠️ The real ceiling is ``timeout x (max_retries + 1)`` and the worker
    Lambda is killed at 600s. 150 x 2 = 300s leaves room for the exit-monitor
    pass that runs in the same invocation immediately after the slot — a hard
    Lambda kill would skip it AND lose the llm.failed trace row. Raise either
    of these two numbers and you must check the other."""
    return env_float("LLM_TIMEOUT_SEC", 150.0)


def llm_max_retries() -> int:
    """1 retry, not the Anthropic SDK's default of 2.

    The SDK retries timeouts, so leaving this unset silently multiplies the
    per-attempt timeout by three — which is what turned a 60s timeout into a
    185s stall on the first live slot. See llm_timeout_sec() for the
    arithmetic this has to satisfy."""
    return env_int("LLM_MAX_RETRIES", 1)


def llm_effort() -> str:
    """Thinking depth — the single biggest lever on this call's token bill.

    `medium` rather than the API's default `high`. The advisor is not solving
    an open problem: the quant has already selected the candidates, the
    prompt states the decision rubric and the measured evidence, and the
    answer is a bounded JSON verdict over at most ~11 names. Thinking tokens
    are billed as output ($15/MTok on Sonnet 5) and are the bulk of what this
    call spends.

    ⚠️ This is the one setting here that trades decision quality for cost, and
    it is one env var to revert (LLM_EFFORT=high). Whether `medium` actually
    costs anything in decision quality is unmeasured — check it against the
    picks_log/verdict pairing before treating it as settled, the same way
    every other claim in this project is supposed to be."""
    return env_str("LLM_EFFORT", "medium").lower()


def llm_web_search() -> bool:
    """Macro context the numbers cannot show — a war, a Fed decision, crude.
    No model knows this morning's news from training data."""
    return env_bool("LLM_WEB_SEARCH", True)


def llm_tools_enabled() -> bool:
    """Let the advisor pull trade history / pick outcomes / positions itself.

    The hybrid split: what is always needed and must be known BEFORE the call
    (the market picture, and the weekly budget the fail-closed gate depends
    on) is pre-assembled into the prompt. What is only sometimes decisive —
    "has S2 actually been working", "do 90s outperform 75s" — is a tool the
    model pulls when a call is close, so the common slot does not pay for it."""
    return env_bool("LLM_TOOLS_ENABLED", True)


def llm_max_tool_iterations() -> int:
    """How many tool round trips one review may take.

    Each iteration is a whole extra inference pass over a growing transcript,
    so this multiplies both latency and tokens. Three is enough for "check
    history, check picks, decide"; more usually means the model is browsing."""
    return env_int("LLM_MAX_TOOL_ITERATIONS", 3)


def llm_total_budget_sec() -> float:
    """Wall-clock ceiling on the WHOLE review, tool loop included.

    🔴 Load-bearing. llm_timeout_sec() bounds one HTTP request; with a tool
    loop the review is now several of them, so the per-request timeout no
    longer bounds the review. Unbounded, 3 iterations x 2 attempts x 150s =
    900s against a 600s Lambda — the function would be killed mid-slot,
    skipping the exit-monitor pass that shares the invocation AND losing the
    llm.failed row, so the failure would be both worse and invisible.

    300s leaves 300s of Lambda for everything else. The deadline is enforced
    by shrinking each request's timeout to the time actually left, so an
    overrun fails closed through the normal path instead of being killed."""
    return env_float("LLM_TOTAL_BUDGET_SEC", 300.0)


def llm_search_max_uses() -> int:
    """2 searches, down from 3.

    Search results are the largest *input* component of the call by a wide
    margin — whole pages are injected into context and then re-read on every
    subsequent inference pass in the same request, so each extra search costs
    more than the one before it. Two is enough to establish "what happened
    this morning", which is all the prompt asks for; the prompt explicitly
    forbids shopping for opinions, and that is the use a third search would
    serve."""
    return env_int("LLM_SEARCH_MAX_USES", 2)


def llm_retro_enabled() -> bool:
    return env_bool("LLM_RETRO_ENABLED", False)


# ── tracing ──────────────────────────────────────────────────────────────────


def trace_enabled() -> bool:
    """Defaults True, unlike every LLM flag above.

    Tracing is the one part that is safe on by default, and it has standalone
    value with the advisor entirely off: `gate.blocked` and `order.sized`
    answer "why did nothing trade on Tuesday" without a log dig."""
    return env_bool("TRACE_ENABLED", True)


def trace_retain_days() -> int:
    return env_int("TRACE_RETAIN_DAYS", 90)


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
    """Sole gate on /webhook/whatsapp, which can place orders. Unset means
    reject everything — see raanu/api/routes/webhooks.py."""
    return env_str("USER_WHATSAPP")

# FCM_SERVICE_ACCOUNT_JSON, TWA_SHA256_FINGERPRINT and TWA_PACKAGE_NAME were
# removed on 31 Aug 2026 with the Android apps. Web push (VAPID) stays — the
# dashboard is still a PWA and that is the desktop notification channel.
