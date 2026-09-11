"""
raanu.trading.schedule — what runs at 03:30, 09:35 and 11:00 ET
================================================================
The scheduled trading day: the pre-market scan (alert only), the two
execution slots, the per-strategy cash budgeting they share, and the picks
caches the dashboard reads.

Deliberately free of any HTTP or Lambda concept. The worker Lambda calls
these directly, and so does the local development loop — neither needs to
know how the other was triggered.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime

from raanu import config, state, trace
from raanu.clock import BERLIN
from raanu.market.rest import alpaca_get
from raanu.scanning.engine import top_picks
from raanu.state import keys
from raanu.trading.trader import get_trader

log = logging.getLogger("raanu.trading.schedule")

# Set once at startup by seed_tradelog_from_env(); surfaced by /api/health.
_seed_result: dict = {"seeded": 0, "reason": "startup not run"}


def seed_result() -> dict:
    return _seed_result


def set_seed_result(value: dict) -> None:
    global _seed_result
    _seed_result = value


def _strategy_resolver():
    from raanu.api.routes.account import _strategy_resolver as resolver
    return resolver


PICKS_KEY = "last_picks"


PICKS_KEY_S2 = "last_picks_s2"


PICKS_KEY_S3 = "last_picks_s3"


def _save_picks(picks: list):
    data = {
        "picks":      picks,
        "scanned_at": datetime.now(BERLIN).isoformat(),
    }
    state.put(keys.CACHE, keys.cache_sk(PICKS_KEY), data)
    # Outcome tracking. Idempotent per day+strategy, and deliberately
    # inside try/except: a research logger must never break a scan.
    try:
        from raanu.trading import picks_log
        picks_log.record("s1", picks)
    except Exception as e:
        log.warning(f"[picks] record skipped: {e}")


def _load_picks() -> dict | None:
    return state.get(keys.CACHE, keys.cache_sk(PICKS_KEY))


def _save_picks_s2(picks: list):
    data = {"picks": picks, "scanned_at": datetime.now(BERLIN).isoformat()}
    state.put(keys.CACHE, keys.cache_sk(PICKS_KEY_S2), data)
    # Outcome tracking. Idempotent per day+strategy, and deliberately
    # inside try/except: a research logger must never break a scan.
    try:
        from raanu.trading import picks_log
        picks_log.record("s2", picks)
    except Exception as e:
        log.warning(f"[picks] record skipped: {e}")


def _load_picks_s2() -> dict | None:
    return state.get(keys.CACHE, keys.cache_sk(PICKS_KEY_S2))


def _save_picks_s3(picks: list):
    data = {"picks": picks, "scanned_at": datetime.now(BERLIN).isoformat()}
    state.put(keys.CACHE, keys.cache_sk(PICKS_KEY_S3), data)
    # Outcome tracking. Idempotent per day+strategy, and deliberately
    # inside try/except: a research logger must never break a scan.
    try:
        from raanu.trading import picks_log
        picks_log.record("s3", picks)
    except Exception as e:
        log.warning(f"[picks] record skipped: {e}")


def _load_picks_s3() -> dict | None:
    return state.get(keys.CACHE, keys.cache_sk(PICKS_KEY_S3))


async def _run_scan_and_cache(alert: bool = True) -> list:
    """Run S1 scanner in a thread pool (non-blocking), cache results.

    alert=False suppresses the Telegram/WhatsApp push. The startup scan
    passes it: a restart is not a scheduled event, and during development
    every code change sent a fresh round of buy alerts.
    """
    log.info("[S1] Running momentum scan...")
    loop  = asyncio.get_event_loop()
    picks = await loop.run_in_executor(None, lambda: top_picks('s1', limit=3))
    _save_picks(picks)
    log.info(f"[S1] Scan done — {len(picks)} picks cached")

    if alert:
        _send_confident_buy_alerts(picks, strategy="s1")

    try:
        # execute=False: this is a scan-and-cache path (startup, rest day,
        # market closed, alert/preview endpoints) — none of them may order.
        await get_trader().run_one_cycle(picks=picks, strategy="s1", execute=False)
    except Exception as e:
        log.exception(f"[S1] Trader cycle error: {e}")
        get_trader().event("error", f"[S1] Trader cycle crashed: {e}")

    return picks


async def _run_scan_and_cache_s2(alert: bool = True) -> list:
    """Run S2 scanner in a thread pool (non-blocking), cache results."""
    log.info("[S2] Running VCP breakout scan...")
    loop  = asyncio.get_event_loop()
    picks = await loop.run_in_executor(None, lambda: top_picks('s2', limit=3))
    _save_picks_s2(picks)
    log.info(f"[S2] Scan done — {len(picks)} picks cached")

    if alert:
        _send_confident_buy_alerts(picks, strategy="s2")

    try:
        # execute=False: this is a scan-and-cache path (startup, rest day,
        # market closed, alert/preview endpoints) — none of them may order.
        await get_trader().run_one_cycle(picks=picks, strategy="s2", execute=False)
    except Exception as e:
        log.exception(f"[S2] Trader cycle error: {e}")
        get_trader().event("error", f"[S2] Trader cycle crashed: {e}")

    return picks


# Score >= 75 in a confirmed uptrend = high-conviction entry
_CONFIDENT_BUY_THRESHOLD = 75


def _send_confident_buy_alerts(picks: list, strategy: str = "s1", slot: str = ""):
    """Send Telegram + web-push alerts for high-conviction picks.

    Traces what each alert actually REACHED, which is a different question
    from whether it was sent. On 10 Sep 2026 the S3 scan surfaced QLYS at 90 —
    the highest score this project has recorded — and the alert went to
    `0 web` subscribers with Telegram unconfigured, i.e. to nobody. The only
    evidence was two lines in CloudWatch. CLAUDE.md already recorded that S3
    had produced 90s and 84s "that were never reported"; it happened again,
    on the same strategy, for the same reason, and nothing surfaced it.

    ``delivered`` counts channels that actually took the message, so a zero
    is legible on the trace page rather than inferred from its absence.
    """
    from raanu.notify.telegram import send_telegram
    gate_key = {"s2": "stage2", "s3": "leader_dip"}.get(strategy, "uptrend")
    confident = [p for p in picks if p.get("score", 0) >= _CONFIDENT_BUY_THRESHOLD and p.get(gate_key)]
    if not confident:
        return

    for p in confident:
        ticker = p.get("ticker", "?")
        name = p.get("name", ticker)
        score = p.get("score", 0)
        # ONE definition of this alert, in push.format_signal(), used by both
        # channels. Telegram and push used to build their own text from the same
        # pick, which is how two descriptions of one event drift until you have
        # to read both to trust either.
        from raanu.notify import push
        title, body = push.format_signal(p, strategy)
        tg = bool(send_telegram(f"*{title}* ({name})\n{body}", strategy=strategy))
        web = 0
        try:
            web = int((push.notify_signal(p, strategy) or {}).get("sent", 0))
        except Exception as e:
            log.warning(f"[push] signal notify skipped: {e}")
        log.info(f"[{strategy.upper()}] Confident buy alert sent: {ticker} score {score} "
                 f"(telegram={tg}, web={web})")
        trace.emit("notify.signal", slot=slot, strategy=strategy,
                   ticker=ticker, score=score,
                   telegram=tg, web_subscribers=web,
                   delivered=(1 if tg else 0) + web)


async def _run_scan_and_cache_s3(alert: bool = True) -> list:
    """Run S3 scanner in a thread pool (non-blocking), cache results."""
    log.info("[S3] Running leader-dip scan...")
    loop  = asyncio.get_event_loop()
    picks = await loop.run_in_executor(None, lambda: top_picks('s3', limit=3))
    _save_picks_s3(picks)
    log.info(f"[S3] Scan done — {len(picks)} picks cached")

    if alert:
        _send_confident_buy_alerts(picks, strategy="s3")

    try:
        # execute=False: this is a scan-and-cache path (startup, rest day,
        # market closed, alert/preview endpoints) — none of them may order.
        await get_trader().run_one_cycle(picks=picks, strategy="s3", execute=False)
    except Exception as e:
        log.exception(f"[S3] Trader cycle error: {e}")
        get_trader().event("error", f"[S3] Trader cycle crashed: {e}")

    return picks


# The structural flag each strategy must set for a pick to be tradable. The
# score alone is not enough: a high score on a stock that is not in the
# strategy's own setup is a scoring artefact, not a signal.
_GATE_KEYS = {"s1": "uptrend", "s2": "stage2", "s3": "leader_dip"}


def _pick_saver(strategy: str):
    return {"s2": _save_picks_s2, "s3": _save_picks_s3}.get(strategy, _save_picks)


def _scan_and_cache_for(strategy: str):
    return {"s2": _run_scan_and_cache_s2,
            "s3": _run_scan_and_cache_s3}.get(strategy, _run_scan_and_cache)


def _scan_actionable(strategy: str, n_orders: int, label: str = "") -> list[dict]:
    """Scan one strategy and return the picks that clear the trading gate.

    Extracted so the slot orchestrator can gather every strategy's candidates
    *before* any of them executes — the advisor has to see the whole day at
    once to rank across strategies. ``_execute_scheduled_trades`` still calls
    it itself when no picks are handed in, so there is exactly one definition
    of "actionable".

    Traces what it dropped and why: "why did NVDA not trade today" is
    otherwise a question only a log dig can answer.
    """
    started = time.monotonic()
    picks = top_picks(strategy, limit=n_orders + 3)
    _pick_saver(strategy)(picks)

    gate_key = _GATE_KEYS.get(strategy, "uptrend")
    bar = config.min_signal_score()

    actionable, dropped = [], []
    for p in picks:
        ticker = p.get("ticker")
        if not ticker:
            continue
        if p.get("score", 0) < bar:
            dropped.append({"ticker": ticker, "score": p.get("score"),
                            "why": f"score below {bar}"})
        elif not p.get(gate_key):
            dropped.append({"ticker": ticker, "score": p.get("score"),
                            "why": f"{gate_key} not set"})
        else:
            actionable.append(p)

    trace.emit("scan.done", slot=label, strategy=strategy,
               # `scanned` is the SHORTLIST the scan returned (n_orders + 3),
               # not the ~470-ticker universe it walked to build it.
               scanned=len(picks), actionable=len(actionable),
               elapsed_sec=round(time.monotonic() - started, 1),
               top_scores=[p.get("score") for p in picks[:5]],
               # The exact input the advisor is about to be given. Without
               # this the trace names only what was DROPPED, so on a slot
               # where the LLM call fails there is no record of what it was
               # asked about — which is precisely the slot you want to read.
               actionable_picks=[{"ticker": p.get("ticker"),
                                  "score": p.get("score")} for p in actionable])
    if dropped:
        trace.emit("filter.actionable", slot=label, strategy=strategy,
                   threshold=bar, gate=gate_key,
                   kept=len(actionable), dropped=dropped)
    return actionable


async def _refresh_or_alert(strategy: str, picks: list[dict] | None,
                            label: str = "") -> None:
    """Keep the dashboard cache fresh after a gate stopped the orders.

    ⚠️ Only RESCANS when this call has no picks of its own.

    The gate returns used to call ``_run_scan_and_cache_*()`` unconditionally,
    which was right before the advisor existed — back then
    ``_execute_scheduled_trades`` scanned for itself, so an early return
    genuinely had nothing cached and the dashboard would have shown days-old
    picks. Under ``run_slot`` that is no longer true: it has already scanned,
    and ``_scan_actionable`` already wrote the cache through the SAME
    ``_pick_saver`` these functions use.

    Measured on the 10 Sep 09:35 slot: three strategies, all weekly-limited,
    each rescanning all 470 tickers a second time — ~28s of a 110s invocation
    spent recomputing what was cached 30 seconds earlier.

    The alert is NOT redundant and is still sent: a strategy that cannot
    trade has still found something, and on that slot one of those somethings
    was QLYS at 90.
    """
    if picks is None:
        await _scan_and_cache_for(strategy)()
        return
    _send_confident_buy_alerts(picks, strategy=strategy, slot=label)


def _pool_blocked(label: str) -> str:
    """Why this slot cannot trade at all, or "" if it can.

    The pooled counterpart to the old per-strategy weekly check. There is one
    budget now, so exhaustion is a property of the SLOT rather than of each
    strategy — a capped strategy can no longer sit out while another trades,
    because there are no longer per-strategy caps to hit.

    Runs BEFORE the advisor call. On 10 Sep 2026 the slot scanned, called the
    model, got a good verdict — 13 decisions, 12 approvals, 30s, ~$0.085 —
    for a slot where no order was possible under any answer.

    ⚠️ Fails CLOSED. An unreadable budget means we do not know what we are
    allowed to spend, and the only safe reading of that is zero.
    """
    from raanu.ai.context import budget as budget_ctx
    try:
        pool = budget_ctx.state()
    except Exception as e:
        log.warning(f"[{label}] weekly pool unreadable: {e}")
        return f"weekly budget unreadable ({type(e).__name__}) — standing down"
    if pool["exhausted"]:
        return (f"weekly pool spent — {pool['trades_used']}/{pool['trades_max']} trades "
                f"and ${pool['usd_used']:,.0f}/${pool['usd_max']:,.0f}; "
                f"oldest frees {pool.get('oldest_frees_at') or 'unknown'}")
    return ""


def _seed_position_plan(ticker: str, entry_px: float, atr: float | None,
                        exit_plan: dict) -> None:
    """Write the position's exit record at BUY time.

    The exit monitor creates this record lazily on its first pass, recomputing
    ATR from whatever the market has done since. Seeding it here pins the
    entry ATR and the chosen plan to the moment the order was actually sized,
    which is what keeps sizing and exiting describing the same trade.

    Best-effort by design: a failure here must never turn a filled order into
    an exception. The position simply falls back to the strategy defaults,
    which is the behaviour that existed before exit plans did.
    """
    try:
        record = {"peak": float(entry_px)}
        if atr:
            record["atr"] = float(atr)
        if exit_plan:
            record["plan"] = exit_plan
        state.put(keys.PEAK, keys.peak_sk(ticker), record)
    except Exception as e:
        log.warning(f"[exits] could not seed plan for {ticker}: {e}")


async def _execute_scheduled_trades(n_orders: int, label: str, strategy: str = "s1",
                                    picks: list[dict] | None = None,
                                    verdict=None):
    """
    Scan and place up to n_orders market buys for a scheduled slot.
    Respects score threshold, position sizing, and already-held check.
    Sends Telegram alerts before and after each order, tagged by strategy.

    ``picks`` supplies a pre-scanned, already-approved candidate list (from
    ``run_slot``); when it is None this function scans for itself and behaves
    exactly as it did before the advisor existed. ``verdict`` carries the
    advisor's budget split. Both default to None so every existing caller —
    and the tests guarding the auto-trader switch — are unaffected.
    """
    # The per-trade cap is resolved PER PICK inside the loop — the queue is
    # pooled, so consecutive orders can belong to different strategies.
    from raanu.notify.telegram import (
        _strat_tag,
        format_pre_trade_alert,
        format_trade_confirm,
        send_whatsapp,
    )
    from raanu.trading.trader import (
        alpaca_buy_notional,
        get_free_cash,
        get_held_symbols,
        market_is_open,
        per_trade_max_for,
    )

    stag = _strat_tag(strategy)
    log.info(f"[{label}][{strategy.upper()}] Scheduled run — targeting {n_orders} order(s)")

    # ── Gate: the auto-trader is switched on ──────────────────────────────
    # First gate, deliberately: it is the one a human sets, so nothing below
    # it should run when the answer is "off".
    #
    # This check did not exist until 8 Sep 2026. run_one_cycle() honoured the
    # flag, but the scheduled slots — the path that actually trades on AWS —
    # never consulted it, so the dashboard's ENABLE/DISABLE toggle governed
    # /api/auto/scan-now and nothing else. The only thing preventing
    # autonomous trading was the EventBridge rule shipping disabled. Turning
    # the bot "off" in the UI would not have stopped 09:35 and 11:00.
    if not get_trader().enabled:
        log.info(f"[{label}][{strategy.upper()}] auto-trader is OFF — scanning only, no orders")
        trace.emit("gate.blocked", slot=label, strategy=strategy,
                   gate="auto_trader_enabled", reason="auto-trader is off")
        await _refresh_or_alert(strategy, picks, label)
        return

    # ── Gate: market hours ────────────────────────────────────────────────
    # Market orders submitted while closed sit in `accepted` until the next
    # session and fill at an unknown price — never place them blind.
    is_open, clock_msg = await market_is_open()
    if not is_open:
        log.info(f"[{label}][{strategy.upper()}] {clock_msg} — scanning only, no orders")
        trace.emit("gate.blocked", slot=label, strategy=strategy,
                   gate="market_closed", reason=clock_msg)
        await _refresh_or_alert(strategy, picks, label)
        return

    # ── Gate: rolling weekly trade limit (per strategy) ───────────────────
    ok, why = get_trader().tradelog.can_trade_now(strategy=strategy)
    if not ok:
        log.info(f"[{label}][{strategy.upper()}] {why} — no orders")
        # This was log-only until 11 Sep 2026, which made it invisible to the
        # trace — and it is the single most likely reason a day trades
        # nothing. Reading 10 Sep back, the journal showed the advisor
        # approving 12 of 13 and then simply stopped; every non-trade looked
        # like a veto when the weekly budget had made the decision.
        trace.emit("gate.blocked", slot=label, strategy=strategy,
                   gate="weekly_limit", reason=why)
        send_whatsapp(f"📊 *RaanuBot — {label}*\n{stag}\n{why}", strategy=strategy)
        await _refresh_or_alert(strategy, picks, label)
        return

    # `picks` supplied ⇒ run_slot already scanned, filtered and had the
    # advisor approve these. Scanning again here would both waste a 472-ticker
    # pass and discard the approval.
    scanned_here = picks is None
    actionable = _scan_actionable(strategy, n_orders, label) if scanned_here else list(picks)

    if not actionable:
        if scanned_here:
            msg = (
                f"📊 *RaanuBot — {label}*\n"
                f"{stag}\n"
                f"No stocks above score {config.min_signal_score()} today.\n"
                f"_No trades placed._"
            )
            send_whatsapp(msg, strategy=strategy)
        log.info(f"[{label}][{strategy.upper()}] 0 actionable picks — skipping")
        return

    held      = await get_held_symbols()
    free_cash = await get_free_cash()

    # Fail closed. An unreadable holdings list used to arrive as an empty set,
    # which reads as "hold nothing" and disables the duplicate guard for the
    # whole slot — one skipped scan is far cheaper than a duplicate position.
    if held is None:
        log.error(f"[{label}][{strategy.upper()}] Could not verify existing holdings — aborting")
        return

    if free_cash is None:
        log.error(f"[{label}][{strategy.upper()}] Could not fetch account balance — aborting")
        return

    # ── The weekly pool: a trade COUNT and a dollar CEILING, shared ─────────
    #
    # Replaces both the per-strategy weekly limit and the per-strategy cash
    # share. Those two together meant a capped strategy could not lend its
    # allowance to an uncapped one, and that execution ORDER decided
    # allocation — on 13 Aug 2026 S1 and S2 consumed the whole account and S3,
    # holding candidates scoring 90, 84 and 73, reached an empty one.
    #
    # ⚠️ CLAUDE.md used to say "do not go back to a single shared pot". That
    # rule is deliberately reversed here, and what makes it safe is the pair
    # of things that did not exist then: an absolute weekly dollar ceiling
    # (nothing bounded the total at all in August), and an explicit
    # cross-strategy ranking from the advisor, so order is now a decision
    # rather than an artefact of a for-loop.
    from raanu.ai.context import budget as budget_ctx
    pool = budget_ctx.state()
    min_trade = config.weekly_min_trade_usd()

    n_orders = min(n_orders, pool["trades_left"])
    # Money that is not in the account is a harder limit than any policy.
    budget_left = min(pool["usd_left"], free_cash)

    # The equity reserve is OFF by default now (the weekly ceiling replaced
    # it) but composes as one more floor when re-armed — see cash_reserve_pct.
    reserve_pct = config.cash_reserve_pct()
    if reserve_pct > 0:
        try:
            equity_now = float((await alpaca_get("/account")).get("equity", free_cash))
        except Exception:
            equity_now = free_cash
        budget_left = max(0.0, min(budget_left, free_cash - equity_now * reserve_pct / 100.0))

    log.info(f"[{label}] weekly pool: {pool['trades_used']}/{pool['trades_max']} trades, "
             f"${pool['usd_used']:,.0f}/${pool['usd_max']:,.0f} used "
             f"-> {n_orders} order(s), ${budget_left:,.0f} spendable")

    if n_orders <= 0 or budget_left < min_trade:
        why = (f"weekly pool exhausted — {pool['trades_left']} trade(s) and "
               f"${budget_left:,.0f} left (min ${min_trade:,.0f})")
        log.info(f"[{label}] {why}")
        trace.emit("gate.blocked", slot=label, gate="weekly_budget", reason=why,
                   trades_left=pool["trades_left"], usd_left=pool["usd_left"])
        send_whatsapp(f"📊 *RaanuBot — {label}*\n{why}")
        return

    # The advisor may pace WITHIN the pool — spend less today, keep the rest
    # for a better tape. It can never enlarge it: budget_left came from the
    # trade log, not from the verdict.
    if verdict is not None and config.llm_budget_enabled():
        asked = verdict.slot_budget(weekly_usd_left=budget_left)
        if asked < budget_left:
            log.info(f"[{label}] advisor paced this slot to ${asked:,.0f} "
                     f"of ${budget_left:,.0f}: {verdict.pacing_note or 'no reason given'}")
        budget_left = asked
        if budget_left < min_trade:
            trace.emit("gate.blocked", slot=label, gate="llm_pacing",
                       reason=verdict.pacing_note or "advisor deployed nothing this slot")
            return

    # ── Position sizing: Kelly-scaled risk budget ────────────────────────────
    # Equal-dollar sizing is incoherent once stops are ATR-scaled — a wide-stop
    # name would risk many times what a quiet one does. Instead, size so the
    # loss AT THE STOP is a fixed share of equity, with that share set by
    # Quarter Kelly on this strategy's own realized history.
    #
    # Resolved PER PICK now rather than once per call: the queue is pooled, so
    # consecutive orders can belong to different strategies and Kelly reads
    # each strategy's own realized history. Cached so a queue of five S1 names
    # still walks the trade log once.
    from raanu.trading.exits import _get_atr, effective_stop_pct
    from raanu.trading.sizing import from_trade_log, shares_for

    _kelly: dict[str, object] = {}

    def kelly_for(strat: str):
        if strat not in _kelly:
            k = from_trade_log(strategy=strat)
            log.info(f"[{label}][{strat.upper()}] sizing: {k.reason}")
            _kelly[strat] = k
        return _kelly[strat]

    try:
        equity = float((await alpaca_get("/account")).get("equity", free_cash))
    except Exception:
        equity = free_cash

    # Optional per-strategy ceiling on the pooled count. Defaults to the whole
    # pool, i.e. inert — see config.weekly_max_per_strategy for when to set it.
    per_strategy_cap = config.weekly_max_per_strategy()
    taken: dict[str, int] = dict(pool.get("trades_by_strategy_this_week") or {})

    placed = 0
    # Why an APPROVED candidate did not become an order. These were log-only,
    # which made the trace read as if the advisor were the whole story: a slot
    # could approve eleven names, place one, and leave no record of what
    # stopped the other ten. Reading that back, every non-trade looks like a
    # veto — so the prompt gets "fixed" for a decision the mechanical gates
    # actually made.
    def _skipped(ticker: str, gate: str, reason: str, strat: str = "") -> None:
        trace.emit("gate.blocked", slot=label, strategy=strat or strategy,
                   gate=gate, ticker=ticker, reason=reason)

    for pick in actionable:
        ticker = pick["ticker"].upper()
        # The queue is pooled, so the strategy is a property of the PICK, not
        # of the call. `strategy` remains the default for the legacy callers
        # (run_one_cycle, scan-now) that still pass one strategy's picks.
        strat = (pick.get("_strategy") or strategy).lower()

        if placed >= n_orders:
            _skipped(ticker, "n_orders",
                     f"weekly trade count spent ({placed} placed this slot)", strat)
            break
        if budget_left < min_trade:
            log.info(f"[{label}] weekly dollars spent after {placed} order(s)")
            _skipped(ticker, "weekly_budget",
                     f"${budget_left:,.0f} left, below the ${min_trade:,.0f} minimum", strat)
            break
        if taken.get(strat, 0) >= per_strategy_cap:
            _skipped(ticker, "per_strategy_cap",
                     f"{strat} already has {taken.get(strat, 0)} of {per_strategy_cap} "
                     f"allowed this week", strat)
            continue
        if ticker in held:
            log.info(f"[{label}][{strat.upper()}] {ticker} held or already on order — skipping")
            _skipped(ticker, "already_held", "position open or buy order queued", strat)
            continue

        k = kelly_for(strat)
        if not k.tradeable:
            # Negative f* means stand aside, not size down.
            _skipped(ticker, "kelly_stand_aside", k.reason, strat)
            continue
        per_trade_cap = per_trade_max_for(strat)

        entry_px = float(pick.get("price") or 0)
        atr = await _get_atr(ticker) if config.exit_config().stop_mode == "atr" else None
        if entry_px <= 0:
            log.info(f"[{label}][{strat.upper()}] {ticker} has no price — skipping")
            _skipped(ticker, "no_price", "scan returned no usable price", strat)
            continue

        # ── The stop that SIZES the trade must be the stop that EXITS it ─────
        # shares_for() computes qty = risk_budget / (entry - stop), so this
        # stop IS the risk model. If the advisor's plan widened the stop but
        # sizing still used the strategy default, the loss at the stop would
        # silently exceed the intended share of equity — a 5.0x ATR plan
        # against a 2.5x default doubles real risk while every log line still
        # reports the configured risk_pct. So the plan's multiple is resolved
        # HERE, once, and the same number is stored with the position below.
        exit_plan = dict(pick.get("_llm_exit_plan") or {}) if config.llm_exits_enabled() else {}

        if atr and atr > 0:
            # effective_stop_pct() is the SAME function the exit monitor calls,
            # so the stop that sizes this order cannot drift from the stop that
            # will close it. See its docstring for why that matters.
            stop_pct = effective_stop_pct(strat, atr / entry_px * 100, exit_plan)
        else:
            # No ATR available — fall back to the fixed stop so sizing stays
            # consistent with whatever the exit engine will actually use.
            stop_pct = float(os.getenv("STOP_LOSS_PCT", "3.0"))
            log.warning(f"[{label}][{strat.upper()}] {ticker}: no ATR, sizing off {stop_pct}% stop")
            # An ATR-based plan cannot be honoured without an ATR; drop it
            # rather than let the exit engine apply a stop sizing never saw.
            exit_plan.pop("stop_atr_mult", None)

        try:
            max_pos_pct = float(os.getenv("MAX_POSITION_PCT", "10.0"))
        except ValueError:
            max_pos_pct = 10.0
        qty = shares_for(equity, k.risk_pct, entry_px,
                         entry_px * (1 - stop_pct / 100),
                         max_position_pct=max_pos_pct)
        risk_sized = qty * entry_px
        size_mult = float(pick.get("_llm_size_mult", 1.0) or 1.0)
        # The weekly pot is one more ceiling in the same min() chain, so a
        # trade is trimmed to what the week can still afford rather than
        # skipped for being slightly too large.
        notional = round(min(risk_sized, per_trade_cap, budget_left) * size_mult, 2)
        if notional < min_trade:
            # A $12 position cannot move the P&L, occupies a slot in the
            # book, and dilutes the per-trade sample Kelly reads.
            log.info(
                f"[{label}][{strat.upper()}] {ticker} sized to ${notional} "
                f"(risk {k.risk_pct}%, stop {stop_pct:.1f}%) — below the "
                f"${min_trade:,.0f} minimum, skipping"
            )
            _skipped(ticker, "below_min_trade", f"sized to ${notional}", strat)
            continue

        # If the per-strategy cap binds, sizing is flat again and the ATR stop
        # no longer equalises risk across names — worth saying out loud.
        if risk_sized > per_trade_cap * 1.05:
            log.warning(
                f"[{label}][{strat.upper()}] {ticker}: risk sizing wanted "
                f"${risk_sized:,.0f} but PER_TRADE_MAX_USD_{strat.upper()} caps at "
                f"${per_trade_cap:,.0f} — per-trade risk is NOT equalised while this cap binds"
            )

        log.info(
            f"[{label}][{strat.upper()}] {ticker} @ ${entry_px:.2f} stop {stop_pct:.1f}% "
            f"risk {k.risk_pct}% -> ${notional}"
        )

        # Every input to the notional, so "why was this $412?" is answerable
        # from the trace alone rather than by re-deriving it from logs.
        trace.emit("order.sized", slot=label, strategy=strat, ticker=ticker,
                   entry_px=entry_px, atr_pct=round(atr / entry_px * 100, 2) if atr else None,
                   stop_pct=round(stop_pct, 2), risk_pct=k.risk_pct,
                   risk_sized=round(risk_sized, 2), per_trade_cap=per_trade_cap,
                   weekly_usd_left=round(budget_left, 2), size_mult=size_mult,
                   notional=notional, exit_plan=exit_plan or None,
                   llm_rationale=pick.get("_llm_rationale") or None)

        try:
            send_whatsapp(format_pre_trade_alert(
                ticker, pick.get("ticker", ticker), notional,
                pick["score"], free_cash, pick.get("reasons", []),
                strategy=strat,
                llm_rationale=pick.get("_llm_rationale", ""),
            ), strategy=strat)
            await asyncio.sleep(2)

            result = await alpaca_buy_notional(ticker, notional)
            get_trader().tradelog.record({
                "action":       "BUY",
                "ticker":       ticker,
                "notional_usd": notional,
                "score":        pick["score"],
                "reasons":      pick.get("reasons", []),
                "strategy":     strat,
                "scheduled":    label,
                "entry_price":  entry_px,
                "stop_pct":     round(stop_pct, 2),
                "risk_pct":     k.risk_pct,
                "atr_pct":      round(atr / entry_px * 100, 2) if atr else None,
                "exit_plan":    exit_plan or None,
                "alpaca_response": result,
            })

            # Seed the exit engine's per-position record now, while the entry
            # ATR and the chosen plan are both known. The monitor's first pass
            # then finds them already there instead of recomputing an ATR that
            # has since moved — and the stop it enforces is the same one that
            # sized this order.
            _seed_position_plan(ticker, entry_px, atr, exit_plan)

            trace.emit("order.placed", slot=label, strategy=strat, ticker=ticker,
                       notional=notional, score=pick.get("score"),
                       status=result.get("status") if isinstance(result, dict) else None,
                       client_order_id=(result or {}).get("client_order_id")
                       if isinstance(result, dict) else None)
            get_trader().event("buy", f"[{label}][{strat.upper()}] BUY ${notional} of {ticker} score {pick['score']}")
            # Push is best-effort and must never break an order that already filled.
            try:
                from raanu.notify import push
                push.notify_buy(ticker, notional, strat,
                                             pick.get("score"), pick)
            except Exception as e:
                log.warning(f"[push] buy notify skipped: {e}")
            send_whatsapp(format_trade_confirm("BUY", ticker, notional, result.get("status", "submitted"), strategy=strat), strategy=strat)

            held.add(ticker)
            free_cash -= notional
            # Draw down the SHARED pot, so the next pick — whatever strategy
            # it belongs to — sees what this one actually took.
            budget_left -= notional
            taken[strat] = taken.get(strat, 0) + 1
            placed += 1
        except Exception as e:
            log.error(f"[{label}][{strat.upper()}] Order failed for {ticker}: {e}")
            trace.emit("order.failed", slot=label, strategy=strat, ticker=ticker,
                       notional=notional, error_type=type(e).__name__, error=str(e))
            get_trader().event("error", f"[{label}][{strat.upper()}] {ticker} failed: {e}")

    if placed == 0:
        send_whatsapp(
            f"📊 *RaanuBot — {label}*\n"
            f"{stag}\n"
            f"Top picks already held. No new positions opened.",
            strategy=strategy,
        )
    log.info(f"[{label}] Done — placed {placed}/{n_orders} order(s), "
             f"${budget_left:,.0f} of the weekly pot still unspent")


# ── The execution slot ───────────────────────────────────────────────────────
# S3 first: the only strategy profitable in both halves of the backtest, so any
# rounding edge falls its way rather than against it.
_SLOT_ORDER = ("s3", "s1", "s2")


def _verdict_cache_key(label: str) -> str:
    day = datetime.now(BERLIN).date().isoformat()
    return f"llm_verdict#{day}#{label}"


async def run_slot(n_orders: int, label: str) -> None:
    """Run one execution slot across every strategy.

    Replaces the old ``for strat in (...): _execute_scheduled_trades(...)``
    loop in both callers. The loop had to move here because the advisor needs
    to see the whole day's candidates at once — it ranks across strategies and
    decides whether the day is worth trading at all, and neither question can
    be answered one strategy at a time.

    Structure is two phases around a single LLM call:

      1. scan every strategy, collecting actionable candidates
      2. one advisory review
      3. execute per strategy, with the approved and ranked picks

    With the advisor disabled this is behaviourally identical to the old loop —
    each strategy scans and executes exactly as before.
    """
    # No candidates anywhere, the trader switched off, or the advisor disabled
    # all short-circuit BEFORE the paid call. Most days cost nothing.
    if not config.llm_advisor_enabled():
        for strategy in _SLOT_ORDER:
            try:
                await _execute_scheduled_trades(n_orders, label, strategy=strategy)
            except Exception as e:
                log.exception(f"[{label}][{strategy.upper()}] slot failed: {e}")
        return

    if not get_trader().enabled:
        log.info(f"[{label}] auto-trader is OFF — scanning only, no advisor call")
        trace.emit("gate.blocked", slot=label, gate="auto_trader_enabled",
                   reason="auto-trader is off")
        for strategy in _SLOT_ORDER:
            try:
                await _scan_and_cache_for(strategy)()
            except Exception as e:
                log.exception(f"[{label}][{strategy.upper()}] scan failed: {e}")
        return

    # ── Phase 1: gather every strategy's candidates ──────────────────────────
    candidates: dict[str, list[dict]] = {}
    for strategy in _SLOT_ORDER:
        try:
            candidates[strategy] = _scan_actionable(strategy, n_orders, label)
        except Exception as e:
            log.exception(f"[{label}][{strategy.upper()}] scan failed: {e}")
            candidates[strategy] = []

    total = sum(len(v) for v in candidates.values())
    if not total:
        from raanu.notify.telegram import send_whatsapp
        log.info(f"[{label}] no actionable candidates in any strategy — no advisor call")
        trace.emit("gate.blocked", slot=label, gate="no_candidates",
                   reason=f"nothing cleared score {config.min_signal_score()}")
        send_whatsapp(f"📊 *RaanuBot — {label}*\n"
                      f"No stocks above score {config.min_signal_score()} today.\n"
                      f"_No trades placed._")
        return

    # ── Phase 1b: decide the unconditional gates BEFORE paying for advice ───
    #
    # 🔴 These are the gates no verdict can lift. Asking a paid model to rank
    # candidates the weekly pool has already excluded is spending money to
    # decide something already decided — measured at ~$0.085 a slot, twice a
    # day, for five days straight on 10 Sep 2026.
    from raanu.trading.trader import market_is_open
    is_open, clock_msg = await market_is_open()
    if not is_open:
        log.info(f"[{label}] {clock_msg} — no advisor call, scanning only")
        trace.emit("gate.blocked", slot=label, gate="market_closed",
                   reason=clock_msg, candidates=total)
        for strategy, picks in candidates.items():
            if picks:
                _send_confident_buy_alerts(picks, strategy=strategy, slot=label)
        return

    pool_reason = _pool_blocked(label)
    if pool_reason:
        log.info(f"[{label}] {pool_reason} — no advisor call")
        trace.emit("gate.blocked", slot=label, gate="weekly_budget",
                   reason=pool_reason,
                   # The names that WOULD have gone to the advisor. A blocked
                   # slot still surfaced candidates, and on 10 Sep one of them
                   # was the highest score this project has recorded (QLYS 90).
                   would_have_reviewed=[
                       {"ticker": p.get("ticker"), "score": p.get("score"),
                        "strategy": s}
                       for s, picks in candidates.items() for p in picks])
        # The pool stops the ORDER; it is not a reason to stop telling the
        # owner what the scan found.
        for strategy, picks in candidates.items():
            if picks:
                _send_confident_buy_alerts(picks, strategy=strategy, slot=label)
        return

    # ── Phase 2: one advisory review for the whole slot ──────────────────────
    from raanu.ai import context as ai_context
    from raanu.ai import market_context
    from raanu.ai.advisor import review_slot
    from raanu.notify.telegram import send_whatsapp

    # Pre-assembled providers only — market and budget. The history/picks/
    # positions services are declared to the model as tools instead, so a slot
    # with an obvious answer does not pay to research one it did not need.
    #
    # Raises if `budget` cannot answer: not knowing what we are allowed to
    # spend is the one context failure with no safe default.
    try:
        context = await ai_context.snapshot()
    except ai_context.ProviderError as e:
        log.warning(f"[{label}] required context unavailable: {e} — no orders")
        trace.emit("gate.blocked", slot=label, gate="context_unavailable",
                   reason=str(e))
        return
    log.info(f"[{label}] market: {market_context.headline(context.get('market') or {})}")
    trace.emit("context.snapshot", slot=label, **context)

    verdict = await review_slot(candidates, context, label)

    if verdict is None:
        # Fail closed. An advisor that cannot be consulted must not be assumed
        # to approve — the same reasoning that makes an unreadable auto-trader
        # flag read as OFF. Entries stop; open positions are untouched, because
        # the exit monitor never consults this path.
        log.warning(f"[{label}] advisor unavailable — no orders this slot")
        send_whatsapp(f"📊 *RaanuBot — {label}*\n"
                      f"Advisor unavailable — no trades placed.\n"
                      f"_{total} quant candidate(s) still logged._")
        return

    try:
        from raanu.trading import picks_log
        picks_log.attach_llm_verdict(verdict, candidates)
    except Exception as e:
        log.warning(f"[picks] llm verdict attach skipped: {e}")

    shadow = config.llm_shadow_mode()
    summary = f"{verdict.regime.replace('_', ' ')} — {verdict.market_summary}"

    if not verdict.trade_today and not shadow:
        log.info(f"[{label}] advisor stood the slot down: {verdict.market_summary}")
        trace.emit("gate.blocked", slot=label, gate="llm_trade_today",
                   reason=verdict.market_summary, regime=verdict.regime)
        send_whatsapp(f"📊 *RaanuBot — {label}*\n🛑 Standing down today.\n{summary}")
        return

    if shadow:
        # The verdict is recorded and reported but not acted on. This is how
        # "did its vetoes actually correlate with worse outcomes" gets answered
        # before any capital depends on the answer.
        log.info(f"[{label}] SHADOW — advisor said trade_today={verdict.trade_today}, "
                 f"regime={verdict.regime}; executing the quant's picks unchanged")

    # ── Phase 3: execute ONE pooled queue, in the advisor's rank order ──────
    #
    # Was `for strategy in _SLOT_ORDER: execute(strategy)`. That loop existed
    # because the budget was per strategy; with one pot there is one queue,
    # and the cross-strategy ranking the advisor produces is what decides who
    # gets funded. The old order (s3 first) was a tie-break hedge for exactly
    # the allocation problem this removes.
    if shadow:
        # Shadow runs the quant's picks unchanged, so it keeps the old
        # per-strategy shape — there is no advisor ranking to pool by.
        for strategy in _SLOT_ORDER:
            picks = candidates.get(strategy) or []
            if not picks:
                continue
            try:
                await _execute_scheduled_trades(n_orders, label, strategy=strategy,
                                                picks=picks, verdict=None)
            except Exception as e:
                log.exception(f"[{label}][{strategy.upper()}] slot failed: {e}")
        return

    queue = verdict.approved_ranked(
        candidates, apply_exits=config.llm_exits_enabled())
    if not queue:
        log.info(f"[{label}] every candidate vetoed")
        trace.emit("gate.blocked", slot=label, gate="llm_veto",
                   reason="every candidate vetoed")
        return

    log.info(f"[{label}] queue: " + ", ".join(
        f"{p['_strategy']}/{p['ticker']}#{p.get('_llm_rank')}" for p in queue))
    try:
        await _execute_scheduled_trades(n_orders, label,
                                        strategy=queue[0]["_strategy"],
                                        picks=queue, verdict=verdict)
    except Exception as e:
        log.exception(f"[{label}] slot failed: {e}")


# ── Pre-market scan (3:30 AM ET = 30 min before pre-market open) ────────────
async def _premarket_scan_and_notify():
    """Scan every strategy and send a separate Telegram alert per strategy chat.

    S3 was missing here from the day it was added: this function predates it,
    scanned only S1 and S2, and nobody noticed because the absence of an alert
    looks exactly like "no signals today". S3 has produced picks scoring 90 and
    84 that were never reported.
    """
    from raanu.notify.telegram import send_telegram
    log.info("[Pre-market] Running dual-strategy scan...")
    picks_s1 = await _run_scan_and_cache()
    picks_s2 = await _run_scan_and_cache_s2()
    picks_s3 = await _run_scan_and_cache_s3()

    # S1 alert → S1 chat
    s1_lines = ["📡 *RaanuBot — Pre-market Scan*", "📊 *S1 Pullback*", ""]
    if picks_s1:
        medals = ["🏆", "🥈", "🥉"]
        s1_lines.append(f"{len(picks_s1)} signal(s) found:\n")
        for i, p in enumerate(picks_s1):
            score = p.get("score", 0)
            heat = "🔥" if score >= 75 else "📈"
            ticker = p.get("ticker", "?")
            name = p.get("name", ticker)
            gp = " | 🎯 GP" if p.get("in_golden_pocket") else ""
            s1_lines.append(
                f"{medals[i] if i < 3 else '  '} *{ticker}* ({name}) {heat} {score}/100{gp}"
            )
    else:
        s1_lines.append("⚠️ No strong pullback signals today.")
    s1_lines.append("\n_Auto-trader will execute at market open if enabled._")
    send_telegram("\n".join(s1_lines), strategy="s1")

    # S2 alert → S2 chat
    s2_lines = ["📡 *RaanuBot — Pre-market Scan*", "🚀 *S2 Breakout*", ""]
    if picks_s2:
        medals = ["🏆", "🥈", "🥉"]
        s2_lines.append(f"{len(picks_s2)} signal(s) found:\n")
        for i, p in enumerate(picks_s2):
            score = p.get("score", 0)
            heat = "🔥" if score >= 75 else "📈"
            ticker = p.get("ticker", "?")
            name = p.get("name", ticker)
            s2_lines.append(
                f"{medals[i] if i < 3 else '  '} *{ticker}* ({name}) {heat} {score}/100"
            )
    else:
        s2_lines.append("⚠️ No strong breakout signals today.")
    s2_lines.append("\n_Auto-trader will execute at market open if enabled._")
    send_telegram("\n".join(s2_lines), strategy="s2")

    # S3 alert → S3 chat. Listed last here but it is the strategy with the best
    # evidence: the only one profitable in both halves of the backtest.
    s3_lines = ["📡 *RaanuBot — Pre-market Scan*", "💧 *S3 Leader Dip*", ""]
    if picks_s3:
        medals = ["🏆", "🥈", "🥉"]
        s3_lines.append(f"{len(picks_s3)} signal(s) found:\n")
        for i, p in enumerate(picks_s3):
            score = p.get("score", 0)
            heat = "🔥" if score >= 75 else "📈"
            ticker = p.get("ticker", "?")
            name = p.get("name", ticker)
            s3_lines.append(
                f"{medals[i] if i < 3 else '  '} *{ticker}* ({name}) {heat} {score}/100"
            )
    else:
        s3_lines.append("⚠️ No leader dips today.")
    s3_lines.append("\n_Auto-trader will execute at market open if enabled._")
    send_telegram("\n".join(s3_lines), strategy="s3")

    log.info(f"[Pre-market] Telegram sent — S1: {len(picks_s1)}, "
             f"S2: {len(picks_s2)}, S3: {len(picks_s3)}")

    # One push digest for the whole scan. Wrapped, like every other push hook:
    # a notification failure must never affect a scan.
    try:
        from raanu.notify import push
        push.notify_scan({"s1": picks_s1, "s2": picks_s2, "s3": picks_s3})
    except Exception as e:
        log.warning(f"[push] scan digest skipped: {e}")


# Trade slots run in US/Eastern — the same clock the market keeps.
#
# They used to be Berlin times (07:00 and 14:30), which are 01:00 and 08:30 ET:
# BOTH sat outside the 09:30–16:00 session, so _execute_scheduled_trades() hit
# its market-hours gate every time and fell through to scan-only. That is why
# picks were cached daily and S3 never placed a single order.
#
# 09:35 is the primary slot because the backtest fills signals at the NEXT day's
# OPEN — trading five minutes after the bell is the only entry timing its
# results describe. The five-minute delay avoids the opening auction's spread
# without meaningfully departing from that assumption. 11:00 is a second chance
# for days when the first slot is blocked (all picks already held, Kelly
# standing aside), still well inside the session.
#
# Expressed in ET rather than Berlin on purpose: Europe and the US switch DST on
# different dates, so a Berlin-anchored slot drifts by an hour twice a year
# against the only clock that matters here.
#
# The per-slot order count is deliberately larger than the old 2/1. With the
# weekly limits raised so they no longer bind, the slot count would have become
# the new hidden throttle — the same mistake the alternating-day rule made.
# Free cash and MAX_POSITION_PCT are meant to be what stops the bot, so the
# slot allows more orders than either will ever permit in one sitting.
# The third number is the slot's order cap. It tracks WEEKLY_TRADE_LIMIT so
# nothing silently caps a slot below the pool: the pooled budget is the real
# bound, and pacing across the two daily slots is the advisor's decision
# (usd_to_deploy), not a hardcoded rail. It was 5, which quietly made "seven
# trades a week, the advisor decides" untrue in any single slot.
_ET_SLOTS = [
    (9,  35, config.weekly_trade_limit(), "Open-9:35"),
    (11, 0,  config.weekly_trade_limit(), "Midday-11am"),
]


# Pre-market slot runs in US/Eastern time
_PREMARKET_ET = (3, 30)  # 3:30 AM ET = 30 min before 4:00 AM pre-market
