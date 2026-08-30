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
from datetime import datetime

from raanu import config, state
from raanu.clock import BERLIN
from raanu.market.rest import alpaca_get
from raanu.scanning.engine import top_picks
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


PICKS_KEY = "last_picks.json"


PICKS_KEY_S2 = "last_picks_s2.json"


PICKS_KEY_S3 = "last_picks_s3.json"


def _save_picks(picks: list):
    data = {
        "picks":      picks,
        "scanned_at": datetime.now(BERLIN).isoformat(),
    }
    state.save(PICKS_KEY, data)
    # Outcome tracking. Idempotent per day+strategy, and deliberately
    # inside try/except: a research logger must never break a scan.
    try:
        from raanu.trading import picks_log
        picks_log.record("s1", picks)
    except Exception as e:
        log.warning(f"[picks] record skipped: {e}")


def _load_picks() -> dict | None:
    return state.load(PICKS_KEY)


def _save_picks_s2(picks: list):
    data = {"picks": picks, "scanned_at": datetime.now(BERLIN).isoformat()}
    state.save(PICKS_KEY_S2, data)
    # Outcome tracking. Idempotent per day+strategy, and deliberately
    # inside try/except: a research logger must never break a scan.
    try:
        from raanu.trading import picks_log
        picks_log.record("s2", picks)
    except Exception as e:
        log.warning(f"[picks] record skipped: {e}")


def _load_picks_s2() -> dict | None:
    return state.load(PICKS_KEY_S2)


def _save_picks_s3(picks: list):
    data = {"picks": picks, "scanned_at": datetime.now(BERLIN).isoformat()}
    state.save(PICKS_KEY_S3, data)
    # Outcome tracking. Idempotent per day+strategy, and deliberately
    # inside try/except: a research logger must never break a scan.
    try:
        from raanu.trading import picks_log
        picks_log.record("s3", picks)
    except Exception as e:
        log.warning(f"[picks] record skipped: {e}")


def _load_picks_s3() -> dict | None:
    return state.load(PICKS_KEY_S3)


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


def _send_confident_buy_alerts(picks: list, strategy: str = "s1"):
    """Send Telegram alert for high-conviction picks, tagged by strategy."""
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
        send_telegram(f"*{title}* ({name})\n{body}", strategy=strategy)
        try:
            push.notify_signal(p, strategy)
        except Exception as e:
            log.warning(f"[push] signal notify skipped: {e}")
        log.info(f"[{strategy.upper()}] Confident buy alert sent: {ticker} score {score}")


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


async def _execute_scheduled_trades(n_orders: int, label: str, strategy: str = "s1"):
    """
    Scan and place up to n_orders market buys for a scheduled slot.
    Respects score threshold, position sizing, and already-held check.
    Sends Telegram alerts before and after each order, tagged by strategy.
    """
    from raanu.trading.trader import (
        alpaca_buy_notional,
        get_free_cash,
        get_held_symbols,
        market_is_open,
        per_trade_max_for,
    )
    # The cap is per strategy — see auto_trader.per_trade_max_for().
    per_trade_cap = per_trade_max_for(strategy)
    from raanu.notify.telegram import (
        _strat_tag,
        format_pre_trade_alert,
        format_trade_confirm,
        send_whatsapp,
    )

    stag = _strat_tag(strategy)
    log.info(f"[{label}][{strategy.upper()}] Scheduled run — targeting {n_orders} order(s)")

    # ── Gate: market hours ────────────────────────────────────────────────
    # Market orders submitted while closed sit in `accepted` until the next
    # session and fill at an unknown price — never place them blind.
    is_open, clock_msg = await market_is_open()
    if not is_open:
        log.info(f"[{label}][{strategy.upper()}] {clock_msg} — scanning only, no orders")
        await {"s2": _run_scan_and_cache_s2, "s3": _run_scan_and_cache_s3}.get(strategy, _run_scan_and_cache)()
        return

    # ── Gate: rolling weekly trade limit (per strategy) ───────────────────
    ok, why = get_trader().tradelog.can_trade_now(strategy=strategy)
    if not ok:
        log.info(f"[{label}][{strategy.upper()}] {why} — no orders")
        send_whatsapp(f"📊 *RaanuBot — {label}*\n{stag}\n{why}", strategy=strategy)
        # Still refresh the cache. The weekly budget is now the ONLY throttle, so
        # a strategy that has spent it sits out the rest of the week — and with
        # the alternating rest-day scan gone, returning bare here would leave the
        # dashboard showing days-old picks for exactly those strategies.
        await {"s2": _run_scan_and_cache_s2, "s3": _run_scan_and_cache_s3}.get(strategy, _run_scan_and_cache)()
        return

    if strategy == "s3":
        picks = top_picks('s3', limit=n_orders + 3)
        _save_picks_s3(picks)
        gate_key = "leader_dip"
    elif strategy == "s2":
        picks = top_picks('s2', limit=n_orders + 3)
        _save_picks_s2(picks)
        gate_key = "stage2"
    else:
        picks = top_picks('s1', limit=n_orders + 3)
        _save_picks(picks)
        gate_key = "uptrend"

    actionable = [
        p for p in picks
        if p.get("score", 0) >= config.min_signal_score() and p.get(gate_key) and p.get("ticker")
    ]

    if not actionable:
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

    # Never place more orders than the weekly budget still allows — the budget
    # is per strategy, so this must not read the global WEEKLY_TRADE_LIMIT.
    # BUYs only — exits are logged with the same strategy tag and must not eat
    # the opening budget (see TradeLog.trades_in_last_7_days).
    from raanu.trading.trader import weekly_limit_for
    remaining = weekly_limit_for(strategy) - len(
        get_trader().tradelog.trades_in_last_7_days(strategy=strategy, action="BUY")
    )
    n_orders  = min(n_orders, max(0, remaining))

    # ── Position sizing: Kelly-scaled risk budget ────────────────────────────
    # Equal-dollar sizing is incoherent once stops are ATR-scaled — a wide-stop
    # name would risk many times what a quiet one does. Instead, size so the
    # loss AT THE STOP is a fixed share of equity, with that share set by
    # Quarter Kelly on this strategy's own realized history.
    from raanu.trading.exits import _get_atr, stop_atr_mult_for
    from raanu.trading.sizing import from_trade_log, shares_for

    k = from_trade_log(strategy=strategy)
    log.info(f"[{label}][{strategy.upper()}] sizing: {k.reason}")
    if not k.tradeable:
        send_whatsapp(
            f"📊 *RaanuBot — {label}*\n{stag}\n"
            f"No orders — {k.reason}\n"
            f"_{k.sample} closed trades, win rate {k.win_rate*100:.0f}%, payoff {k.payoff_b:.2f}_",
            strategy=strategy,
        )
        log.info(f"[{label}][{strategy.upper()}] Kelly says stand aside — no orders")
        return

    try:
        equity = float((await alpaca_get("/account")).get("equity", free_cash))
    except Exception:
        equity = free_cash

    # ── Cash reserve ─────────────────────────────────────────────────────────
    # On the first slot that could actually execute, the bot deployed $99,414 of
    # a $99,414 account and left $0.01. Every gate passed — per-trade cap, weekly
    # limit, Kelly sizing — because none of them limits the TOTAL committed at
    # once. The result: no capacity for the 11:00 slot, none for a better signal
    # tomorrow, and the whole account in whichever strategies happened to fire
    # first that morning.
    #
    # Measured against EQUITY, not free cash, so the reserve means "keep this
    # share of the account liquid" rather than a share of whatever is left. When
    # the account is already over-deployed this simply yields nothing to spend —
    # it never forces a sale to rebuild the buffer.
    reserve_pct = config.cash_reserve_pct()
    reserve = equity * reserve_pct / 100.0
    deployable = max(0.0, free_cash - reserve)

    # ── Per-strategy share ───────────────────────────────────────────────────
    # The slot runs s1, s2, s3 in that order against ONE pot, so whichever runs
    # first can spend everything. On 13 Aug S1 and S2 consumed the whole account
    # and S3 — holding candidates scoring 90, 84 and 73 — reached an empty one.
    # Execution order was silently deciding allocation, and it decided against
    # the only strategy profitable in both halves of the backtest.
    #
    # Each strategy now gets a slice of the deployable budget. Weights follow
    # conviction, which is what CLAUDE.md always said capital should do.
    share = config.cash_share(strategy) or 33.0
    deployable = deployable * share / 100.0
    log.info(f"[{label}][{strategy.upper()}] share {share:.0f}% of deployable")
    log.info(f"[{label}][{strategy.upper()}] cash {free_cash:,.0f} | "
             f"reserve {reserve_pct:.0f}% = {reserve:,.0f} | deployable {deployable:,.0f}")
    if deployable < 1.0:
        msg = (f"Cash reserve reached — {free_cash:,.0f} free vs a "
               f"{reserve_pct:.0f}% reserve of {reserve:,.0f}. No new positions.")
        log.info(f"[{label}][{strategy.upper()}] {msg}")
        send_whatsapp(f"📊 *RaanuBot — {label}*\n{stag}\n{msg}", strategy=strategy)
        return

    placed = 0
    for pick in actionable:
        if placed >= n_orders:
            break
        if deployable < 1.0:
            log.info(f"[{label}][{strategy.upper()}] reserve reached after {placed} order(s)")
            break
        ticker = pick["ticker"].upper()
        if ticker in held:
            log.info(f"[{label}][{strategy.upper()}] {ticker} held or already on order — skipping")
            continue

        entry_px = float(pick.get("price") or 0)
        atr = await _get_atr(ticker) if config.exit_config().stop_mode == "atr" else None
        if entry_px <= 0:
            log.info(f"[{label}][{strategy.upper()}] {ticker} has no price — skipping")
            continue

        if atr and atr > 0:
            mult = stop_atr_mult_for(strategy)
            stop_pct = min(max(mult * atr / entry_px * 100, config.exit_config().stop_min_pct), config.exit_config().stop_max_pct)
        else:
            # No ATR available — fall back to the fixed stop so sizing stays
            # consistent with whatever the exit engine will actually use.
            stop_pct = float(os.getenv("STOP_LOSS_PCT", "3.0"))
            log.warning(f"[{label}][{strategy.upper()}] {ticker}: no ATR, sizing off {stop_pct}% stop")

        try:
            max_pos_pct = float(os.getenv("MAX_POSITION_PCT", "10.0"))
        except ValueError:
            max_pos_pct = 10.0
        qty = shares_for(equity, k.risk_pct, entry_px,
                         entry_px * (1 - stop_pct / 100),
                         max_position_pct=max_pos_pct)
        risk_sized = qty * entry_px
        notional = round(min(risk_sized, per_trade_cap, deployable), 2)
        if notional < 1.0:
            log.info(
                f"[{label}][{strategy.upper()}] {ticker} sized to ${notional} "
                f"(risk {k.risk_pct}%, stop {stop_pct:.1f}%) — skipping"
            )
            continue

        # If the per-strategy cap binds, sizing is flat again and the ATR stop
        # no longer equalises risk across names — worth saying out loud.
        if risk_sized > per_trade_cap * 1.05:
            log.warning(
                f"[{label}][{strategy.upper()}] {ticker}: risk sizing wanted "
                f"${risk_sized:,.0f} but PER_TRADE_MAX_USD_{strategy.upper()} caps at "
                f"${per_trade_cap:,.0f} — per-trade risk is NOT equalised while this cap binds"
            )

        log.info(
            f"[{label}][{strategy.upper()}] {ticker} @ ${entry_px:.2f} stop {stop_pct:.1f}% "
            f"risk {k.risk_pct}% -> ${notional}"
        )

        try:
            send_whatsapp(format_pre_trade_alert(
                ticker, pick.get("ticker", ticker), notional,
                pick["score"], free_cash, pick.get("reasons", []),
                strategy=strategy,
            ), strategy=strategy)
            await asyncio.sleep(2)

            result = await alpaca_buy_notional(ticker, notional)
            get_trader().tradelog.record({
                "action":       "BUY",
                "ticker":       ticker,
                "notional_usd": notional,
                "score":        pick["score"],
                "reasons":      pick.get("reasons", []),
                "strategy":     strategy,
                "scheduled":    label,
                "entry_price":  entry_px,
                "stop_pct":     round(stop_pct, 2),
                "risk_pct":     k.risk_pct,
                "atr_pct":      round(atr / entry_px * 100, 2) if atr else None,
                "alpaca_response": result,
            })
            get_trader().event("buy", f"[{label}][{strategy.upper()}] BUY ${notional} of {ticker} score {pick['score']}")
            # Push is best-effort and must never break an order that already filled.
            try:
                from raanu.notify import push
                push.notify_buy(ticker, notional, strategy,
                                             pick.get("score"), pick)
            except Exception as e:
                log.warning(f"[push] buy notify skipped: {e}")
            send_whatsapp(format_trade_confirm("BUY", ticker, notional, result.get("status", "submitted"), strategy=strategy), strategy=strategy)

            held.add(ticker)
            free_cash -= notional
            deployable -= notional
            placed += 1
        except Exception as e:
            log.error(f"[{label}][{strategy.upper()}] Order failed for {ticker}: {e}")
            get_trader().event("error", f"[{label}][{strategy.upper()}] {ticker} failed: {e}")

    if placed == 0:
        send_whatsapp(
            f"📊 *RaanuBot — {label}*\n"
            f"{stag}\n"
            f"Top picks already held. No new positions opened.",
            strategy=strategy,
        )
    log.info(f"[{label}][{strategy.upper()}] Done — placed {placed}/{n_orders} order(s)")


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
_ET_SLOTS = [
    (9,  35, 5, "Open-9:35"),
    (11, 0,  5, "Midday-11am"),
]


# Pre-market slot runs in US/Eastern time
_PREMARKET_ET = (3, 30)  # 3:30 AM ET = 30 min before 4:00 AM pre-market
