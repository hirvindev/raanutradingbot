"""
RaanuTradingBot — Alpaca backend
=================================
Connects the dashboard to your Alpaca paper/live account.

Run with:  python server.py
"""

import os
import hashlib
import secrets
import time
import re
import json
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, StreamingResponse)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

BERLIN = ZoneInfo("Europe/Berlin")
IST    = ZoneInfo("Asia/Kolkata")
US_EAST = ZoneInfo("US/Eastern")

# ---------- CONFIG ----------
HERE = Path(__file__).parent
load_dotenv(HERE / ".env", override=False)  # no-op on Railway; env vars come from dashboard
from datadir import state_load, state_save, data_dir
_DATA_DIR = data_dir()
PICKS_KEY = "last_picks.json"
PICKS_KEY_S2 = "last_picks_s2.json"
PICKS_KEY_S3 = "last_picks_s3.json"

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "").strip()
ALPACA_SECRET  = os.getenv("ALPACA_SECRET_KEY", "").strip()
ALPACA_MODE    = os.getenv("ALPACA_MODE", "paper").strip().lower()  # paper | live

if ALPACA_MODE not in ("paper", "live"):
    print(f"WARNING: Invalid ALPACA_MODE='{ALPACA_MODE}'. Defaulting to paper.")
    ALPACA_MODE = "paper"

BROKER_BASE = (
    "https://paper-api.alpaca.markets/v2"
    if ALPACA_MODE != "live"
    else "https://api.alpaca.markets/v2"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("raanu")

# ---------- AUTO-TRADER (imported AFTER load_dotenv) ----------
from auto_trader import trader

# ---------- PICKS CACHE ----------
# PICKS_KEY already set above in CONFIG section


def _save_picks(picks: list):
    data = {
        "picks":      picks,
        "scanned_at": datetime.now(BERLIN).isoformat(),
    }
    state_save(PICKS_KEY, data)
    # Outcome tracking. Idempotent per day+strategy, and deliberately
    # inside try/except: a research logger must never break a scan.
    try:
        import picks_log; picks_log.record("s1", picks)
    except Exception as e:
        log.warning(f"[picks] record skipped: {e}")


def _load_picks() -> Optional[dict]:
    return state_load(PICKS_KEY)


# ---------- S2 PICKS CACHE ----------

def _save_picks_s2(picks: list):
    data = {"picks": picks, "scanned_at": datetime.now(BERLIN).isoformat()}
    state_save(PICKS_KEY_S2, data)
    # Outcome tracking. Idempotent per day+strategy, and deliberately
    # inside try/except: a research logger must never break a scan.
    try:
        import picks_log; picks_log.record("s2", picks)
    except Exception as e:
        log.warning(f"[picks] record skipped: {e}")


def _load_picks_s2() -> Optional[dict]:
    return state_load(PICKS_KEY_S2)


def _save_picks_s3(picks: list):
    data = {"picks": picks, "scanned_at": datetime.now(BERLIN).isoformat()}
    state_save(PICKS_KEY_S3, data)
    # Outcome tracking. Idempotent per day+strategy, and deliberately
    # inside try/except: a research logger must never break a scan.
    try:
        import picks_log; picks_log.record("s3", picks)
    except Exception as e:
        log.warning(f"[picks] record skipped: {e}")


def _load_picks_s3() -> Optional[dict]:
    return state_load(PICKS_KEY_S3)


# ---------- BACKGROUND SCAN ----------

async def _run_scan_and_cache(alert: bool = True) -> list:
    """Run S1 scanner in a thread pool (non-blocking), cache results.

    alert=False suppresses the Telegram/WhatsApp push. The startup scan
    passes it: a restart is not a scheduled event, and during development
    every code change sent a fresh round of buy alerts.
    """
    from scanner import find_top_picks
    log.info("[S1] Running momentum scan...")
    loop  = asyncio.get_event_loop()
    picks = await loop.run_in_executor(None, lambda: find_top_picks(3))
    _save_picks(picks)
    log.info(f"[S1] Scan done — {len(picks)} picks cached")

    if alert:
        _send_confident_buy_alerts(picks, strategy="s1")

    try:
        # execute=False: this is a scan-and-cache path (startup, rest day,
        # market closed, alert/preview endpoints) — none of them may order.
        await trader.run_one_cycle(picks=picks, strategy="s1", execute=False)
    except Exception as e:
        log.exception(f"[S1] Trader cycle error: {e}")
        trader.event("error", f"[S1] Trader cycle crashed: {e}")

    return picks


async def _run_scan_and_cache_s2(alert: bool = True) -> list:
    """Run S2 scanner in a thread pool (non-blocking), cache results."""
    from scanner import find_top_picks_s2
    log.info("[S2] Running VCP breakout scan...")
    loop  = asyncio.get_event_loop()
    picks = await loop.run_in_executor(None, lambda: find_top_picks_s2(3))
    _save_picks_s2(picks)
    log.info(f"[S2] Scan done — {len(picks)} picks cached")

    if alert:
        _send_confident_buy_alerts(picks, strategy="s2")

    try:
        # execute=False: this is a scan-and-cache path (startup, rest day,
        # market closed, alert/preview endpoints) — none of them may order.
        await trader.run_one_cycle(picks=picks, strategy="s2", execute=False)
    except Exception as e:
        log.exception(f"[S2] Trader cycle error: {e}")
        trader.event("error", f"[S2] Trader cycle crashed: {e}")

    return picks


# Score >= 75 in a confirmed uptrend = high-conviction entry
_CONFIDENT_BUY_THRESHOLD = 75

def _send_confident_buy_alerts(picks: list, strategy: str = "s1"):
    """Send Telegram alert for high-conviction picks, tagged by strategy."""
    from notifier import send_telegram
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
        import push
        title, body = push.format_signal(p, strategy)
        send_telegram(f"*{title}* ({name})\n{body}", strategy=strategy)
        try:
            push.notify_signal(p, strategy)
        except Exception as e:
            log.warning(f"[push] signal notify skipped: {e}")
        log.info(f"[{strategy.upper()}] Confident buy alert sent: {ticker} score {score}")


# The alternating trade/rest day rule was REMOVED — do not reintroduce it.
#
# It alternated on calendar-day parity while the slots only run Mon–Fri, so the
# weekday pattern shifted every week and produced 3 trade days one week and 2
# the next: about 2.5 per week. That silently capped the per-strategy weekly
# budgets, which are the throttle that is actually configured, documented and
# enforced at two gates. WEEKLY_TRADE_LIMIT_S3=3 could never be reached, because
# there were rarely three trade days in a rolling week — so the setting quietly
# meant something other than what it said.
#
# One throttle now: the per-strategy weekly limit. Every weekday runs the slots,
# and the budget decides whether an order follows.


async def _run_scan_and_cache_s3(alert: bool = True) -> list:
    """Run S3 scanner in a thread pool (non-blocking), cache results."""
    from scanner import find_top_picks_s3
    log.info("[S3] Running leader-dip scan...")
    loop  = asyncio.get_event_loop()
    picks = await loop.run_in_executor(None, lambda: find_top_picks_s3(3))
    _save_picks_s3(picks)
    log.info(f"[S3] Scan done — {len(picks)} picks cached")

    if alert:
        _send_confident_buy_alerts(picks, strategy="s3")

    try:
        # execute=False: this is a scan-and-cache path (startup, rest day,
        # market closed, alert/preview endpoints) — none of them may order.
        await trader.run_one_cycle(picks=picks, strategy="s3", execute=False)
    except Exception as e:
        log.exception(f"[S3] Trader cycle error: {e}")
        trader.event("error", f"[S3] Trader cycle crashed: {e}")

    return picks


async def _execute_scheduled_trades(n_orders: int, label: str, strategy: str = "s1"):
    """
    Scan and place up to n_orders market buys for a scheduled slot.
    Respects score threshold, position sizing, and already-held check.
    Sends Telegram alerts before and after each order, tagged by strategy.
    """
    from scanner import find_top_picks, find_top_picks_s2, find_top_picks_s3
    from auto_trader import (
        get_free_cash, get_held_symbols, alpaca_buy_notional, market_is_open,
        MIN_SIGNAL_SCORE, per_trade_max_for, _float_env,
    )
    # The cap is per strategy — see auto_trader.per_trade_max_for().
    per_trade_cap = per_trade_max_for(strategy)
    from notifier import send_whatsapp, format_pre_trade_alert, format_trade_confirm, _strat_tag

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
    ok, why = trader.tradelog.can_trade_now(strategy=strategy)
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
        picks = find_top_picks_s3(n=n_orders + 3)
        _save_picks_s3(picks)
        gate_key = "leader_dip"
    elif strategy == "s2":
        picks = find_top_picks_s2(n=n_orders + 3)
        _save_picks_s2(picks)
        gate_key = "stage2"
    else:
        picks = find_top_picks(n=n_orders + 3)
        _save_picks(picks)
        gate_key = "uptrend"

    actionable = [
        p for p in picks
        if p.get("score", 0) >= MIN_SIGNAL_SCORE and p.get(gate_key) and p.get("ticker")
    ]

    if not actionable:
        msg = (
            f"📊 *RaanuBot — {label}*\n"
            f"{stag}\n"
            f"No stocks above score {MIN_SIGNAL_SCORE} today.\n"
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
    from auto_trader import weekly_limit_for
    remaining = weekly_limit_for(strategy) - len(
        trader.tradelog.trades_in_last_7_days(strategy=strategy, action="BUY")
    )
    n_orders  = min(n_orders, max(0, remaining))

    # ── Position sizing: Kelly-scaled risk budget ────────────────────────────
    # Equal-dollar sizing is incoherent once stops are ATR-scaled — a wide-stop
    # name would risk many times what a quiet one does. Instead, size so the
    # loss AT THE STOP is a fixed share of equity, with that share set by
    # Quarter Kelly on this strategy's own realized history.
    from kelly import from_trade_log, shares_for
    from profit_monitor import _get_atr, stop_atr_mult_for, STOP_MODE, STOP_MIN_PCT, STOP_MAX_PCT

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
    reserve_pct = _float_env("CASH_RESERVE_PCT", 30.0)
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
    share = {"s1": _float_env("CASH_SHARE_S1", 30.0),
             "s2": _float_env("CASH_SHARE_S2", 20.0),
             "s3": _float_env("CASH_SHARE_S3", 50.0)}.get(strategy, 33.0)
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
        atr = await _get_atr(ticker) if STOP_MODE == "atr" else None
        if entry_px <= 0:
            log.info(f"[{label}][{strategy.upper()}] {ticker} has no price — skipping")
            continue

        if atr and atr > 0:
            mult = stop_atr_mult_for(strategy)
            stop_pct = min(max(mult * atr / entry_px * 100, STOP_MIN_PCT), STOP_MAX_PCT)
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
            trader.tradelog.record({
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
            trader.event("buy", f"[{label}][{strategy.upper()}] BUY ${notional} of {ticker} score {pick['score']}")
            # Push is best-effort and must never break an order that already filled.
            try:
                import push; push.notify_buy(ticker, notional, strategy,
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
            trader.event("error", f"[{label}][{strategy.upper()}] {ticker} failed: {e}")

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
    from notifier import send_telegram
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
        import push
        push.notify_scan({"s1": picks_s1, "s2": picks_s2, "s3": picks_s3})
    except Exception as e:
        log.warning(f"[push] scan digest skipped: {e}")


# Schedule slots:
#   03:30 ET  — pre-market scan + Telegram alert (scan only, no orders)
#   09:35 ET  — scan + execute top 2 orders  (every weekday)
#   11:00 ET  — scan + execute top 1 order   (every weekday)

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



async def _premarket_loop():
    """Fires daily at 3:30 AM ET — scan + Telegram notification, no orders."""
    log.info("Pre-market loop started — 3:30 AM ET daily (30 min before pre-market)")
    while True:
        now = datetime.now(US_EAST)
        t = now.replace(hour=_PREMARKET_ET[0], minute=_PREMARKET_ET[1], second=0, microsecond=0)
        if now >= t:
            t += timedelta(days=1)
        # Skip weekends (Sat=5, Sun=6)
        while t.weekday() >= 5:
            t += timedelta(days=1)
        sleep_sec = (t - now).total_seconds()
        log.info(
            f"Next pre-market scan: {t.strftime('%Y-%m-%d %H:%M %Z')} "
            f"(in {sleep_sec/3600:.1f}h)"
        )
        await asyncio.sleep(sleep_sec)
        try:
            await _premarket_scan_and_notify()
        except Exception as e:
            log.exception(f"Pre-market scan error: {e}")
        # Fill in what previous picks actually did. Blocking (yfinance), so it
        # runs in a thread and after the alert rather than delaying it.
        try:
            import picks_log
            r = await asyncio.to_thread(picks_log.fill_forward_returns)
            log.info(f"[picks] outcomes: {r}")
        except Exception as e:
            log.warning(f"[picks] backfill skipped: {e}")


async def _scheduled_trade_loop():
    """
    Fires at 09:35 and 11:00 ET every weekday.

    Whether an order actually follows is decided by the per-strategy weekly
    limit inside _execute_scheduled_trades(), not by the calendar.
    """
    log.info("Scheduled trade loop started — 9:35 AM and 11:00 AM ET, every weekday")
    # Immediate startup scan: caches picks so the dashboard is not empty, but
    # stays silent. A restart is not a scheduled event — alerting here meant
    # every code change pushed a fresh round of buy notifications.
    asyncio.create_task(_run_scan_and_cache(alert=False))

    while True:
        now     = datetime.now(US_EAST)
        targets = []
        for h, m, n_orders, label in _ET_SLOTS:
            t = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if now >= t:
                t += timedelta(days=1)
            # Skip weekends (Sat=5, Sun=6) — the market is shut.
            while t.weekday() >= 5:
                t += timedelta(days=1)
            targets.append((t, n_orders, label))

        targets.sort(key=lambda x: x[0])
        next_t, next_n, next_label = targets[0]
        sleep_sec = (next_t - now).total_seconds()
        log.info(
            f"Next slot: {next_label} at {next_t.strftime('%Y-%m-%d %H:%M %Z')} "
            f"(in {sleep_sec/3600:.1f}h)"
        )
        await asyncio.sleep(sleep_sec)

        try:
            log.info(f"[{next_label}] Running all three strategies")
            # S3 first: it is the only strategy profitable in both halves of
            # the backtest, so any leftover edge should fall its way.
            for strat in ("s3", "s1", "s2"):
                await _execute_scheduled_trades(next_n, next_label, strategy=strat)
        except Exception as e:
            log.exception(f"Scheduled slot error [{next_label}]: {e}")


_seed_result: dict = {"seeded": 0, "reason": "startup not run"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    from profit_monitor import monitor_loop
    # Reconcile any seeded history BEFORE the loops start, so the weekly limit
    # and Kelly see the full trade log on this instance's very first scan.
    from auto_trader import seed_tradelog_from_env
    global _seed_result
    try:
        _seed_result = seed_tradelog_from_env()
    except Exception as e:
        log.exception(f"Trade log seeding failed: {e}")
        _seed_result = {"error": str(e)}
    tasks = [
        asyncio.create_task(_premarket_loop()),
        asyncio.create_task(_scheduled_trade_loop()),
        asyncio.create_task(monitor_loop()),
        asyncio.create_task(_monthly_report_loop()),
    ]
    yield
    for t in tasks:
        t.cancel()


# ---------- APP ----------
app = FastAPI(title="RaanuTradingBot", version="2.0", lifespan=lifespan)

# ---------- AUTH ----------
# Until this existed the deployed API was completely open: anyone with the URL
# could read the account and place orders. That was survivable only while the
# URL was unknown, which stops being true the moment the app is published.
#
# TWO tokens, deliberately:
#   API_READ_TOKEN  — reads. The phone gets this one.
#   TRADE_PIN       — anything that moves money or changes bot behaviour.
# A phone is the most losable device here, so the token it carries must not be
# able to buy, sell, or switch the auto-trader on.
API_READ_TOKEN = os.getenv("API_READ_TOKEN", "").strip()

# Origins allowed to call the API from a browser. "*" is wrong once tokens are
# involved — it lets any page a user visits read their account with a token the
# browser has.
_ALLOWED_ORIGINS = [o.strip() for o in os.getenv(
    "ALLOWED_ORIGINS",
    "https://raanu.up.railway.app,http://localhost:8000,http://127.0.0.1:8000",
).split(",") if o.strip()]


# Failed-attempt throttling. The read secret is chosen to be memorable and
# typed on a phone, which means it is short enough to grind if an attacker is
# allowed unlimited guesses against a public URL. Locking out after a handful of
# failures is what makes a human-friendly passphrase defensible at all.
#
# Counts DISTINCT wrong secrets, not failed requests. The dashboard fires about
# eight parallel calls per refresh and polls every 30s, so a tab left open with
# a stale secret produces a burst of failures without anyone guessing anything —
# counting requests locked the owner out of their own account within one page
# load, and then blocked the correct passphrase too. Brute force requires trying
# DIFFERENT secrets, so that is what gets counted; a client retrying the same
# wrong value forever still counts as one wrong value.
_AUTH_FAILS: dict[str, dict[str, float]] = {}
_MAX_FAILS = 8
_LOCKOUT_SEC = 900          # 15 minutes


def _client_ip(request: Request) -> str:
    # Railway terminates TLS upstream, so request.client is the proxy. The first
    # X-Forwarded-For entry is the caller.
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _locked_out(ip: str) -> int:
    """Seconds remaining on a lockout, or 0."""
    now = time.time()
    seen = {h: t for h, t in _AUTH_FAILS.get(ip, {}).items() if now - t < _LOCKOUT_SEC}
    _AUTH_FAILS[ip] = seen
    if len(seen) >= _MAX_FAILS:
        return int(_LOCKOUT_SEC - (now - min(seen.values())))
    return 0


def _record_fail(ip: str, presented: str):
    # Hashed, so a mistyped secret is not sitting in memory in the clear.
    h = hashlib.sha256(presented.encode("utf-8", "replace")).hexdigest()
    _AUTH_FAILS.setdefault(ip, {})[h] = time.time()
    n = len(_AUTH_FAILS[ip])
    if n >= _MAX_FAILS:
        log.warning(f"Auth lockout: {ip} after {n} distinct wrong secrets")


def _presented_token(request: Request) -> str:
    """Read the caller's token from the header, or the query for SSE.

    EventSource cannot set request headers, so /api/scan/stream is the one route
    that accepts ?token=. That does mean the token appears in access logs, which
    is why it is the READ token only — it can never authorise an order.
    """
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return (request.headers.get("x-api-token")
            or request.query_params.get("token") or "").strip()


@app.middleware("http")
async def api_auth_gate(request: Request, call_next):
    path = request.url.path
    method = request.method.upper()

    # Only /api/** is gated. GET / serves the HTML shell, which holds no data,
    # and /webhook/whatsapp must stay open for Twilio to reach it.
    if not path.startswith("/api/") or method == "OPTIONS":
        return await call_next(request)

    # Unset token = gate disabled, so a deploy cannot lock the owner out before
    # the variable is in place. Loud on every request rather than silent, or
    # "temporarily open" quietly becomes permanent.
    if not API_READ_TOKEN:
        log.warning("API_READ_TOKEN is not set — the API is OPEN to anyone with the URL")
        return await call_next(request)

    ip = _client_ip(request)
    wait = _locked_out(ip)
    if wait:
        return JSONResponse(
            {"error": "too_many_attempts", "retry_after_sec": wait},
            status_code=429, headers={"Retry-After": str(wait)},
        )

    if not secrets.compare_digest(_presented_token(request), API_READ_TOKEN):
        _record_fail(ip, _presented_token(request))
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    # Anything that is not a read needs the second secret. Deny-by-method rather
    # than by a path list: a new POST route is then protected the day it is
    # written, instead of the day someone remembers to add it here.
    # Push registration and its self-test move no money and change no bot
    # behaviour; gating them behind the trade PIN made "is push working?"
    # unanswerable without placing a trade.
    _READ_TOKEN_POSTS = {"/api/push/subscribe", "/api/push/unsubscribe",
                         "/api/push/test", "/api/push/native/register",
                         "/api/push/clear-web"}

    if method not in ("GET", "HEAD") and path not in _READ_TOKEN_POSTS:
        expected = os.getenv("TRADE_PIN", "").strip()
        presented = (request.headers.get("x-trade-token") or "").strip()
        if not expected or not secrets.compare_digest(presented, expected):
            # Counted too: the trade PIN is the shorter of the two secrets and
            # the one worth guessing, so it needs the throttle more, not less.
            _record_fail(ip, presented)
            return JSONResponse(
                {"error": "forbidden", "detail": "this action needs the trade PIN"},
                status_code=403,
            )

    # A good secret clears the slate, so a few fat-fingered attempts before a
    # correct one never accumulate into a lockout.
    _AUTH_FAILS.pop(ip, None)

    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- ALPACA CLIENT ----------
def alpaca_headers() -> dict:
    if not ALPACA_API_KEY:
        raise HTTPException(
            status_code=400,
            detail="ALPACA_API_KEY is not set. Add it to your .env file.",
        )
    return {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
        "Content-Type":        "application/json",
    }


async def alpaca_get(path: str, params: Optional[dict] = None):
    url = f"{BROKER_BASE}{path}"
    log.info(f"GET  {url}")
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.get(url, headers=alpaca_headers(), params=params)
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Network error: {e}")
    if r.status_code == 401:
        raise HTTPException(status_code=401, detail="Alpaca rejected the API key. Check ALPACA_API_KEY and ALPACA_SECRET_KEY in .env.")
    if r.status_code == 429:
        raise HTTPException(status_code=429, detail="Alpaca rate limit hit.")
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=f"Alpaca error: {r.text}")
    return r.json()


async def alpaca_post(path: str, body: dict):
    url = f"{BROKER_BASE}{path}"
    log.info(f"POST {url}")
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.post(url, headers=alpaca_headers(), json=body)
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Network error: {e}")
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=f"Alpaca error: {r.text}")
    return r.json()


async def alpaca_delete(path: str):
    url = f"{BROKER_BASE}{path}"
    log.info(f"DELETE {url}")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.delete(url, headers=alpaca_headers())
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return {"cancelled": True}


# ---------- API ENDPOINTS ----------
@app.get("/api/config")
def get_config():
    """Tells the dashboard the broker mode — never exposes keys."""
    return {
        "broker":         "alpaca",
        "mode":           ALPACA_MODE,
        "key_configured": bool(ALPACA_API_KEY),
    }


@app.get("/api/health")
def health():
    from notifier import is_configured as tg_configured
    from auto_trader import (per_trade_max_for as _per_trade_max_for,
                             weekly_limit_for as _weekly_limit_for)
    # Surfaced because a non-persistent state dir silently breaks strategy
    # attribution, the weekly trade limit and Kelly's sample.
    _persistent = bool(os.getenv("DATA_DIR", "").strip()) or (HERE / ".env").exists()
    try:
        _trade_count = len(trader.tradelog.data.get("trades", []))
    except Exception:
        _trade_count = None

    return {
        "status":         "ok",
        "broker":         "alpaca",
        "mode":           ALPACA_MODE,
        "key_configured": bool(ALPACA_API_KEY),
        "telegram_configured": tg_configured(),
        "tradelog_seed": _seed_result,
        "state": {
            "data_dir":       str(_DATA_DIR),
            "persistent":     _persistent,
            "trade_log_entries": _trade_count,
        },
        "config": {
            "stop_loss_pct":       os.getenv("STOP_LOSS_PCT", "3.0"),
            "trail_activate_pct":  os.getenv("TRAIL_ACTIVATE_PCT", os.getenv("TAKE_PROFIT_PCT", "5.0")),
            "trail_pct":           os.getenv("TRAIL_PCT", "2.5"),
            "hard_take_profit_pct": os.getenv("HARD_TAKE_PROFIT_PCT", "0"),
            "min_signal_score":    os.getenv("MIN_SIGNAL_SCORE", "60"),
            "weekly_trade_limit":  os.getenv("WEEKLY_TRADE_LIMIT", "2"),
            "per_trade_max_usd":   os.getenv("PER_TRADE_MAX_USD", "500"),
            "per_trade_max_by_strategy": {
                s: _per_trade_max_for(s) for s in ("s1", "s2", "s3")
            },
            "weekly_limit_by_strategy": {
                s: _weekly_limit_for(s) for s in ("s1", "s2", "s3")
            },
            "profit_check_sec":    os.getenv("PROFIT_CHECK_SEC", "300"),
        },
    }


@app.post("/api/telegram/test")
def telegram_test():
    from notifier import send_telegram, is_configured, _chat_id_for
    if not is_configured():
        return {"ok": False, "error": "Telegram not configured — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env"}
    ok1 = send_telegram("🧪 *RaanuTradingBot — Test*\n📊 S1 Pullback channel working.", strategy="s1")
    ok2 = send_telegram("🧪 *RaanuTradingBot — Test*\n🚀 S2 Breakout channel working.", strategy="s2")
    return {
        "ok": ok1 and ok2,
        "s1_chat": _chat_id_for("s1")[-4:] if _chat_id_for("s1") else "not set",
        "s2_chat": _chat_id_for("s2")[-4:] if _chat_id_for("s2") else "not set",
        "error": None if (ok1 and ok2) else "One or both sends failed — check chat IDs",
    }


@app.get("/api/exit-config")
def get_exit_cfg():
    from profit_monitor import get_exit_config
    return get_exit_config()


@app.post("/api/exit-config")
async def save_exit_cfg(request: Request):
    from profit_monitor import update_exit_config
    body = await request.json()
    updated = update_exit_config(body)
    return {"ok": True, "config": updated}


@app.get("/api/account/cash")
async def account_cash():
    """Account balances — mapped to the shape the dashboard expects."""
    acct = await alpaca_get("/account")
    total = float(acct.get("portfolio_value", 0))

    # Alpaca's /account has NO unrealized_pl field — it only exists per position.
    # Sum it across open positions, otherwise the dashboard shows a flat $0.00.
    open_pnl = 0.0
    try:
        for p in await alpaca_get("/positions"):
            open_pnl += float(p.get("unrealized_pl", 0) or 0)
    except Exception:
        log.warning("Could not fetch positions for open P&L")

    # Use portfolio history for daily P&L — includes realized gains from sells today.
    # last_equity comparison is unreliable on paper accounts (often returns 0).
    daily_ppl = 0.0
    try:
        hist = await alpaca_get(
            "/account/portfolio/history",
            params={"period": "1D", "timeframe": "15Min", "extended_hours": "true"},
        )
        pl_arr = hist.get("profit_loss", [])
        if pl_arr:
            daily_ppl = float(pl_arr[-1] or 0)
    except Exception:
        last_eq   = float(acct.get("last_equity", total))
        daily_ppl = total - last_eq

    # Cash still sitting in unfilled buy orders is spoken for, not free.
    from auto_trader import get_free_cash
    cash      = float(acct.get("cash", 0))
    free      = await get_free_cash()
    if free is None:
        free = cash
    committed = max(0.0, cash - free)

    return {
        "total":     total,
        "free":      free,
        "invested":  total - cash,
        "ppl":       open_pnl,
        "daily_ppl": daily_ppl,
        "blocked":   committed,
        "currency":  acct.get("currency", "USD"),
        "_raw":      acct,
    }


@app.get("/api/account/info")
async def account_info():
    return await alpaca_get("/account")


_asset_name_cache: dict[str, str] = {}


_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(?:\.(\d+))?(.*)$")


def _parse_ts(v) -> Optional[datetime]:
    """
    Parse an ISO timestamp to an aware UTC datetime, or None.

    Python 3.9's fromisoformat accepts fractional seconds only at exactly 3 or
    6 digits, and rejects a trailing 'Z'. Alpaca sends both other widths (e.g.
    '...T13:32:53.92223Z', 5 digits) and 'Z'. Parsing those raised, this
    returned None, and the caller then silently fell back to "latest BUY" —
    which is precisely the mis-attribution this function exists to prevent.
    Normalise the fraction to 6 digits before parsing.
    """
    if not v:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    m = _TS_RE.match(str(v).strip())
    if not m:
        return None
    head, frac, tail = m.group(1), m.group(2) or "0", m.group(3) or ""
    tail = tail.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        ts = datetime.fromisoformat(f"{head}.{frac[:6].ljust(6, '0')}{tail}")
    except Exception:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _strategy_resolver():
    """
    Return `resolve(symbol, when=None) -> strategy`.

    Attribution used to be a plain ticker -> strategy dict built by looping the
    trade log, so the LAST BUY for a ticker overwrote every earlier one. A
    ticker traded by two strategies at different times had its whole history —
    including the older strategy's closed round-trips — relabelled with the
    newer one. JAZZ is a live example: bought by S2 on 2026-07-23 and by S1 on
    2026-07-29, so S2's round-trip was being counted as an S1 result.

    Now each BUY keeps its own timestamp and an order is attributed to the most
    recent BUY at or before it. `when=None` (an open position) still means "the
    latest BUY", which is correct for the lot currently held.
    """
    buys: dict[str, list[tuple[Optional[datetime], str]]] = {}
    for t in trader.tradelog.data.get("trades", []):
        if t.get("action") == "BUY" and t.get("ticker"):
            buys.setdefault(t["ticker"].upper(), []).append(
                (_parse_ts(t.get("timestamp")), t.get("strategy", "s1"))
            )
    for sym in buys:
        buys[sym].sort(key=lambda x: (x[0] is None, x[0]))

    def resolve(symbol: str, when=None) -> str:
        entries = buys.get((symbol or "").upper())
        if not entries:
            return ""            # unattributed — NOT s1
        when = _parse_ts(when) if not isinstance(when, datetime) else when
        if when is None:
            return entries[-1][1]
        prior = [s for ts, s in entries if ts is None or ts <= when]
        # An order before any logged BUY belongs to the earliest known one
        # (clock skew between Alpaca's fill time and our log write).
        return prior[-1] if prior else entries[0][1]

    return resolve


@app.get("/api/portfolio")
async def portfolio():
    """Open positions, tagged with strategy and company name."""
    positions = await alpaca_get("/positions")
    resolve_strat = _strategy_resolver()

    uncached = [p.get("symbol", "").upper() for p in positions if p.get("symbol", "").upper() not in _asset_name_cache]
    if uncached:
        async with httpx.AsyncClient(timeout=10) as client:
            for sym in uncached:
                try:
                    r = await client.get(f"{BROKER_BASE}/assets/{sym}", headers=alpaca_headers())
                    if r.status_code == 200:
                        _asset_name_cache[sym] = r.json().get("name", sym)
                except Exception:
                    _asset_name_cache[sym] = sym

    out = []
    for p in positions:
        sym = p.get("symbol", "").upper()
        out.append({
            "ticker":        sym,
            "name":          _asset_name_cache.get(sym, sym),
            "quantity":      float(p.get("qty", 0)),
            "averagePrice":  float(p.get("avg_entry_price", 0)),
            "currentPrice":  float(p.get("current_price", 0)),
            "ppl":           float(p.get("unrealized_pl", 0)),
            "fxPpl":         0,
            "initialFill":   p.get("asset_id"),
            # "" (not "s1") when the log has no BUY for this symbol — an
            # unattributed position is unknown, not an S1 trade.
            "strategy":      resolve_strat(sym),
            "_raw":          p,
        })
    return out


async def _annotate_names(rows: list) -> list:
    """Attach `name` to any row carrying a `symbol`.

    Alpaca's order payload has no company name, so the dashboard's Instrument
    column could only ever show a bare ticker. Names come from the same
    process-cached /v2/assets map the scanner uses, so the cost is one fetch per
    process rather than one per row — but that first fetch is blocking httpx,
    hence the thread.
    """
    def _work():
        from scanner import get_ticker_name
        for r in rows:
            sym = r.get("symbol")
            if sym:
                r["name"] = get_ticker_name(sym)
        return rows
    try:
        return await asyncio.to_thread(_work)
    except Exception as e:
        log.warning(f"Could not attach company names: {e}")
        return rows


@app.get("/api/orders")
async def orders():
    """Open/pending orders."""
    return await _annotate_names(
        await alpaca_get("/orders", params={"status": "open", "limit": 100})
    )


@app.get("/api/history/orders")
async def history_orders(limit: int = 50):
    """Closed orders (filled, cancelled, expired), tagged with strategy."""
    orders = await alpaca_get("/orders", params={"status": "closed", "limit": min(limit, 500)})
    resolve_strat = _strategy_resolver()
    for o in orders:
        # Attributed as of the order's own time — see _strategy_resolver().
        o["strategy"] = resolve_strat(o.get("symbol"),
                                      o.get("filled_at") or o.get("created_at"))
    return await _annotate_names(orders)


class OrderRequest(BaseModel):
    ticker: str
    quantity: Optional[float] = None
    notional: Optional[float] = None  # dollar amount, alternative to qty
    strategy: Optional[str] = None    # tags the trade log; see place_buy()


@app.post("/api/orders/buy")
async def place_buy(order: OrderRequest):
    body: dict = {
        "symbol":        order.ticker.upper(),
        "side":          "buy",
        "type":          "market",
        "time_in_force": "day",
    }
    if order.notional:
        body["notional"] = str(round(order.notional, 2))
    else:
        body["qty"] = str(order.quantity)
    result = await alpaca_post("/orders", body)

    # Record the BUY, or the position is permanently unattributable. This
    # endpoint backs the Live Signals "Execute" button and never wrote to the
    # trade log, so every hand-placed buy showed as Untagged forever, was
    # invisible to strategy stats, and never reached Kelly's sample.
    #
    # Manual buys are tagged "manual", NOT s1/s2/s3, deliberately: the weekly
    # limit counts BUY entries per strategy, so tagging a hand-placed order as
    # s1 would silently consume the auto-trader's budget for the week. Pass an
    # explicit `strategy` to override.
    try:
        if isinstance(result, dict) and result.get("id"):
            trader.tradelog.record({
                "action":   "BUY",
                "ticker":   order.ticker.upper(),
                "strategy": (order.strategy or "manual").lower(),
                "usd":      round(order.notional, 2) if order.notional else None,
                "qty":      order.quantity,
                "source":   "manual-api",
                "order_id": result.get("id"),
            })
    except Exception as e:
        log.error(f"Order placed but trade log write failed for {order.ticker}: {e}")

    return result


@app.post("/api/orders/sell")
async def place_sell(order: OrderRequest):
    body: dict = {
        "symbol":        order.ticker.upper(),
        "side":          "sell",
        "type":          "market",
        "time_in_force": "day",
    }
    if order.notional:
        body["notional"] = str(round(order.notional, 2))
    else:
        body["qty"] = str(abs(order.quantity))
    return await alpaca_post("/orders", body)


@app.delete("/api/orders/{order_id}")
async def cancel_order(order_id: str):
    return await alpaca_delete(f"/orders/{order_id}")


# ---------- AUTO-TRADER CONTROL ----------
@app.get("/api/auto/status")
def auto_status():
    return trader.status()


@app.post("/api/auto/start")
def auto_start():
    if not ALPACA_API_KEY:
        raise HTTPException(status_code=400, detail="ALPACA_API_KEY not configured. Set it in .env first.")
    trader.enabled = True
    trader.event("control", "Auto-trader ENABLED")
    return {"enabled": True}


@app.post("/api/auto/stop")
def auto_stop():
    trader.enabled = False
    trader.event("control", "Auto-trader DISABLED")
    return {"enabled": False}


@app.post("/api/auto/scan-now")
async def auto_scan_now(force: bool = False):
    """Force an immediate scan + cache refresh.
    ?force=true  — bypass market-hours gate and limit to 5 stocks (test mode)."""
    from scanner import find_top_picks
    if force:
        log.info("TEST MODE — scanning 5 stocks only, market hours bypassed")
        picks = find_top_picks(n=3, max_stocks=5)
        _save_picks(picks)
    else:
        picks = await _run_scan_and_cache()
    await trader.run_one_cycle(picks=picks, force_market_open=force)
    return {"picks": len(picks), "forced": force, **trader.status()}


@app.get("/api/auto/scan-preview")
async def auto_scan_preview():
    """Return cached picks without running a new scan."""
    from auto_trader import MIN_SIGNAL_SCORE
    cached = _load_picks()
    if not cached:
        return {"message": "No scan results yet — scan runs at 07:00 and 18:00 Berlin time", "picks": []}
    return {
        "scanned_at": cached["scanned_at"],
        "min_score":  MIN_SIGNAL_SCORE,
        "picks":      cached["picks"],
        "actionable": [p for p in cached["picks"] if p.get("score", 0) >= MIN_SIGNAL_SCORE and p.get("uptrend") and p.get("ticker")],
    }


# ---------- XETRA/GETTEX SCANNER ----------
@app.get("/api/scan/top3")
async def scan_top3():
    """
    Return the latest XETRA/GETTEX picks instantly from cache.
    Cache is refreshed automatically at 07:00 and 18:00 Berlin time,
    and immediately on server startup.
    """
    from scanner import get_universe_summary
    cached = _load_picks()
    if cached:
        return {
            "picks":       cached["picks"],
            "scanned_at":  cached["scanned_at"],
            "from_cache":  True,
            "universe":    get_universe_summary(),
            "count":       len(cached["picks"]),
        }
    # First startup before cache exists — run live (happens once)
    from scanner import find_top_picks
    picks = find_top_picks(3)
    _save_picks(picks)
    return {
        "picks":      picks,
        "scanned_at": datetime.now(BERLIN).isoformat(),
        "from_cache": False,
        "universe":   get_universe_summary(),
        "count":      len(picks),
    }


@app.post("/api/auth/pin")
async def verify_pin(request: Request):
    """Verify trade PIN. Format: 2 letters + 6 digits (e.g. AB123456)."""
    body     = await request.json()
    entered  = body.get("pin", "").strip().upper()
    expected = os.getenv("TRADE_PIN", "").strip().upper()
    if not expected:
        return {"ok": True, "reason": "no_pin_configured"}
    return {"ok": entered == expected}


@app.get("/api/test/twilio")
async def test_twilio():
    """Debug endpoint — shows what Twilio credentials Railway sees and tests them."""
    import httpx
    sid   = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    frm   = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886").strip()
    to    = os.getenv("USER_WHATSAPP", "whatsapp:+919176911755").strip()

    if not sid or not token:
        return {"error": "missing_creds", "sid_set": bool(sid), "token_set": bool(token)}

    try:
        resp = httpx.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            auth=(sid, token),
            data={"From": frm, "To": to, "Body": "RaanuBot test message ✅"},
            timeout=15,
        )
        return {
            "status_code": resp.status_code,
            "sid_prefix":  sid[:8] + "...",
            "token_prefix": token[:6] + "...",
            "from": frm,
            "to":   to,
            "twilio_response": resp.json(),
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/scan/alert-now")
async def scan_alert_now():
    """Trigger morning alerts for both strategies immediately (for testing)."""
    from notifier import send_whatsapp, format_daily_alert

    cached_s1 = _load_picks()
    picks_s1  = cached_s1["picks"] if cached_s1 else []
    if not picks_s1:
        from scanner import find_top_picks
        picks_s1 = find_top_picks(3)
        _save_picks(picks_s1)

    cached_s2 = _load_picks_s2()
    picks_s2  = cached_s2["picks"] if cached_s2 else []
    if not picks_s2:
        from scanner import find_top_picks_s2
        picks_s2 = find_top_picks_s2(3)
        _save_picks_s2(picks_s2)

    msg_s1 = format_daily_alert(picks_s1, strategy="s1")
    msg_s2 = format_daily_alert(picks_s2, strategy="s2")
    ok1 = send_whatsapp(msg_s1, strategy="s1")
    ok2 = send_whatsapp(msg_s2, strategy="s2")
    return {"sent_s1": ok1, "sent_s2": ok2, "picks_s1": len(picks_s1), "picks_s2": len(picks_s2)}


# ---------- WHATSAPP WEBHOOK (Twilio) ----------

async def _handle_whatsapp_command(cmd: str):
    """
    Runs in a background task — called after TwiML is already returned to Twilio.
    This avoids Twilio's 15-second webhook timeout killing long-running commands.
    """
    from notifier import send_whatsapp, format_trade_confirm, format_portfolio_status, format_daily_alert

    try:
        if cmd == "STATUS":
            try:
                from profit_monitor import get_positions_for_status
                positions, account = await get_positions_for_status()
                reply = format_portfolio_status(positions, account)
            except Exception as e:
                reply = f"❌ Could not fetch portfolio: {e}"
            send_whatsapp(reply)

        elif cmd in ("PICKS", "SCAN"):
            send_whatsapp("🔍 Fetching latest picks for both strategies...")
            cached_s1 = _load_picks()
            if cached_s1 and cached_s1.get("picks"):
                picks_s1 = cached_s1["picks"]
            else:
                from scanner import find_top_picks
                loop = asyncio.get_event_loop()
                picks_s1 = await loop.run_in_executor(None, lambda: find_top_picks(3))
                _save_picks(picks_s1)
            send_whatsapp(format_daily_alert(picks_s1, strategy="s1"), strategy="s1")

            cached_s2 = _load_picks_s2()
            if cached_s2 and cached_s2.get("picks"):
                picks_s2 = cached_s2["picks"]
            else:
                from scanner import find_top_picks_s2
                loop = asyncio.get_event_loop()
                picks_s2 = await loop.run_in_executor(None, lambda: find_top_picks_s2(3))
                _save_picks_s2(picks_s2)
            send_whatsapp(format_daily_alert(picks_s2, strategy="s2"), strategy="s2")

        elif cmd.startswith("BUY "):
            parts  = cmd.split()
            ticker = parts[1] if len(parts) > 1 else ""
            usd    = float(parts[2]) if len(parts) > 2 else float(os.getenv("PER_TRADE_MAX_USD", "500"))
            if not ticker:
                send_whatsapp("❌ Usage: BUY TICKER or BUY TICKER 200")
            else:
                try:
                    body = {"symbol": ticker.upper(), "notional": str(usd), "side": "buy", "type": "market", "time_in_force": "day"}
                    async with httpx.AsyncClient(timeout=20) as c:
                        r = await c.post(f"{BROKER_BASE}/orders", headers=alpaca_headers(), json=body)
                    if r.status_code >= 400:
                        send_whatsapp(f"❌ Order rejected: {r.text[:200]}")
                    else:
                        resp = r.json()
                        # Record it, or the position is unattributable forever.
                        # This path posts straight to Alpaca and used to write
                        # nothing to the trade log, so a chat-placed BUY showed
                        # as Untagged, was missing from strategy stats, and
                        # never reached Kelly's sample. Tagged "manual" for the
                        # same reason as /api/orders/buy: the weekly limit
                        # counts BUY entries per strategy, and a chat order
                        # must not silently spend the auto-trader's budget.
                        try:
                            trader.tradelog.record({
                                "action":   "BUY",
                                "ticker":   ticker.upper(),
                                "strategy": "manual",
                                "usd":      usd,
                                "source":   "telegram-cmd",
                                "order_id": resp.get("id"),
                            })
                        except Exception as e:
                            log.error(f"Chat BUY placed but trade log write failed for {ticker}: {e}")
                        send_whatsapp(format_trade_confirm("BUY", ticker, usd, resp.get("status", "submitted")))
                except Exception as e:
                    send_whatsapp(f"❌ Buy failed: {e}")

        elif cmd.startswith("SELL "):
            parts  = cmd.split()
            ticker = parts[1] if len(parts) > 1 else ""
            if not ticker:
                send_whatsapp("❌ Usage: SELL TICKER")
            else:
                try:
                    async with httpx.AsyncClient(timeout=20) as c:
                        r = await c.delete(f"{BROKER_BASE}/positions/{ticker.upper()}", headers=alpaca_headers())
                    if r.status_code == 404:
                        send_whatsapp(f"⚠️ No open position in {ticker}")
                    elif r.status_code >= 400:
                        send_whatsapp(f"❌ Sell failed: {r.text[:200]}")
                    else:
                        result = r.json()
                        qty    = float(result.get("qty", 0))
                        price  = float(result.get("avg_entry_price", 0))
                        send_whatsapp(format_trade_confirm("SELL", ticker, qty * price, result.get("status", "submitted")))
                except Exception as e:
                    send_whatsapp(f"❌ Sell failed: {e}")

        else:
            send_whatsapp(
                "🤖 *RaanuTradingBot commands:*\n"
                "  *PICKS* — today's top 3 picks\n"
                "  *BUY AAPL* — buy $500 of AAPL\n"
                "  *BUY AAPL 200* — buy $200 of AAPL\n"
                "  *SELL AAPL* — close position\n"
                "  *STATUS* — portfolio summary"
            )

    except Exception as e:
        log.error(f"WhatsApp command handler error for '{cmd}': {e}")
        try:
            from notifier import send_whatsapp
            send_whatsapp(f"❌ Internal error: {e}")
        except Exception:
            pass


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(
    request: Request,
    Body: str = Form(default=""),
    From: str = Form(default=""),
):
    """
    Twilio sends incoming WhatsApp messages here.
    Returns TwiML immediately so we stay well within Twilio's 15s timeout.
    Command handling runs in a background task.
    """
    expected_from = os.getenv("USER_WHATSAPP", "whatsapp:+919176911755").strip()
    if From and From != expected_from:
        log.warning(f"WhatsApp message from unknown number: {From}")
        return PlainTextResponse("<?xml version='1.0'?><Response/>", media_type="text/xml")

    cmd = Body.strip().upper()
    log.info(f"WhatsApp command: '{cmd}' from {From}")

    # Fire-and-forget — respond to Twilio immediately to avoid timeout
    asyncio.create_task(_handle_whatsapp_command(cmd))

    return PlainTextResponse("<?xml version='1.0'?><Response/>", media_type="text/xml")


# ---------- STOCK SEARCH / MARKET DATA (Alpaca) ----------
@app.get("/api/stocks/search")
async def stocks_search(q: str = ""):
    """Search tradable US equities by symbol or name."""
    import alpaca_data
    if not q.strip():
        raise HTTPException(status_code=400, detail="Provide ?q=<symbol or name>")
    results = alpaca_data.search_assets(q.strip(), limit=15)
    return {"query": q, "results": results, "source": "alpaca"}


@app.get("/api/stocks/active")
async def stocks_most_active(top: int = 20):
    """Top stocks by volume today."""
    import alpaca_data
    data = alpaca_data.get_most_active(top=min(top, 50))
    return {"most_active": data, "source": "alpaca"}


@app.get("/api/stocks/movers")
async def stocks_market_movers(top: int = 10):
    """Top daily gainers and losers."""
    import alpaca_data
    data = alpaca_data.get_market_movers(top=min(top, 25))
    return {"gainers": data.get("gainers", []), "losers": data.get("losers", []), "source": "alpaca"}


# ---------- STREAMING SCAN ----------
@app.get("/api/scan/stream")
async def scan_stream():
    """
    SSE endpoint — scans the curated universe with ALL THREE strategies (S1
    pullback, S2 breakout, S3 leader dip) and streams qualifying stocks tagged
    with which engine surfaced them. A ticker can appear more than once if it
    passes several: those are different setups, not duplicates.
    """
    from scanner import FALLBACK_UNIVERSE, get_ticker_name, CHUNK_SIZE
    from strategy import score_from_df, batch_download, benchmark_return_3m
    from strategy2 import score_from_df_s2
    from strategy3 import score_from_df_s3

    async def generator():
        import json
        loop = asyncio.get_event_loop()

        universe = FALLBACK_UNIVERSE

        bench = await loop.run_in_executor(None, benchmark_return_3m)

        total = len(universe)
        yield f"data: {json.dumps({'status': 'downloading', 'total': total})}\n\n"

        scanned = 0
        emitted = 0
        chunks = [universe[i:i + CHUNK_SIZE] for i in range(0, len(universe), CHUNK_SIZE)]

        def _cap_label(mc):
            if not mc: return "—"
            if mc >= 200e9: return "Mega"
            if mc >= 10e9:  return "Large"
            if mc >= 2e9:   return "Mid"
            if mc >= 300e6: return "Small"
            return "Micro"

        def _fetch_market_cap(ticker):
            try:
                import yfinance as yf
                return yf.Ticker(ticker).fast_info.market_cap
            except Exception:
                return None

        for chunk in chunks:
            data = await loop.run_in_executor(None, batch_download, chunk)
            for ticker in chunk:
                df = data.get(ticker)
                scanned += 1
                if scanned % 25 == 0 or scanned == total:
                    yield f"data: {json.dumps({'progress': True, 'scanned': scanned, 'total': total})}\n\n"

                mc_fetched = False
                mc_val = None

                # S1: Pullback-in-Uptrend — high conviction only
                r1 = score_from_df(ticker, df, bench_ret_3m=bench)
                s1_pass = (r1.get("ok") and r1.get("uptrend")
                        and r1.get("score", 0) >= 70
                        and r1.get("rsi", 50) <= 68
                        and r1.get("macd", 0) >= r1.get("macd_signal", 0)
                        and (r1.get("rel_strength") or 0) > 0
                        and (r1.get("mom_3m") or 0) > 0)
                if s1_pass:
                    r1["strategy"] = "s1"
                    r1["total"] = total
                    r1["name"] = get_ticker_name(ticker)
                    mc_val = await loop.run_in_executor(None, _fetch_market_cap, ticker)
                    mc_fetched = True
                    r1["cap_label"] = _cap_label(mc_val)
                    yield f"data: {json.dumps(r1)}\n\n"
                    emitted += 1

                # S2: VCP Breakout — high conviction only
                r2 = score_from_df_s2(ticker, df, bench_ret_3m=bench)
                s2_pass = (r2.get("ok") and r2.get("stage2")
                        and r2.get("score", 0) >= 70
                        and (r2.get("rel_strength") or 0) > 0)
                if s2_pass:
                    r2["strategy"] = "s2"
                    r2["total"] = total
                    r2["name"] = get_ticker_name(ticker)
                    if not mc_fetched:
                        mc_val = await loop.run_in_executor(None, _fetch_market_cap, ticker)
                    r2["cap_label"] = _cap_label(mc_val)
                    yield f"data: {json.dumps(r2)}\n\n"
                    emitted += 1

                # S3: Leader Dip. Absent from this stream since S3 was written —
                # the browser scanner has only ever shown S1 and S2, so the one
                # strategy profitable in BOTH halves of the backtest was
                # invisible on the screen used to eyeball candidates. Same
                # omission that kept it out of the pre-market alerts.
                r3 = score_from_df_s3(ticker, df, bench_ret_3m=bench)
                if r3.get("ok") and r3.get("leader_dip") and r3.get("score", 0) >= 60:
                    r3["strategy"] = "s3"
                    r3["total"] = total
                    r3["name"] = get_ticker_name(ticker)
                    if not mc_fetched:
                        mc_val = await loop.run_in_executor(None, _fetch_market_cap, ticker)
                    r3["cap_label"] = _cap_label(mc_val)
                    yield f"data: {json.dumps(r3)}\n\n"
                    emitted += 1

        yield f"data: {json.dumps({'done': True, 'scanned': scanned, 'emitted': emitted, 'total': total})}\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------- STRATEGY COMPARISON ----------

def match_closed_trades(orders: list[dict]) -> list[dict]:
    """
    Pair filled buys and sells into closed round-trips using FIFO lot matching.

    A single sell can consume several buy lots (the bot has bought the same
    ticker more than once), and a partial sell leaves the rest of the lot open —
    so lots are drawn down share by share rather than one-buy-per-sell.
    """
    lots: dict[str, list[dict]] = {}
    for o in sorted(orders, key=lambda x: x.get("filled_at") or x.get("created_at") or ""):
        if o.get("status") != "filled":
            continue
        sym = (o.get("symbol") or "").upper()
        qty = float(o.get("filled_qty") or 0)
        px  = float(o.get("filled_avg_price") or 0)
        if qty <= 0 or px <= 0:
            continue
        if o.get("side") == "buy":
            lots.setdefault(sym, []).append({
                "qty": qty, "price": px,
                "date": o.get("filled_at") or o.get("created_at"),
                "strategy": o.get("strategy", ""),
            })

    closed: list[dict] = []
    for o in sorted(orders, key=lambda x: x.get("filled_at") or x.get("created_at") or ""):
        if o.get("status") != "filled" or o.get("side") != "sell":
            continue
        sym = (o.get("symbol") or "").upper()
        remaining = float(o.get("filled_qty") or 0)
        sell_px   = float(o.get("filled_avg_price") or 0)
        sell_date = o.get("filled_at") or o.get("created_at")
        if remaining <= 0 or sell_px <= 0:
            continue

        queue = lots.get(sym, [])
        while remaining > 1e-9 and queue:
            lot   = queue[0]
            take  = min(remaining, lot["qty"])
            pnl   = (sell_px - lot["price"]) * take
            closed.append({
                "symbol":     sym,
                "strategy":   lot["strategy"],
                "qty":        take,
                "buy_price":  lot["price"],
                "sell_price": sell_px,
                "pnl":        round(pnl, 2),
                "pct":        round((sell_px - lot["price"]) / lot["price"] * 100, 2),
                "buy_date":   lot["date"],
                "sell_date":  sell_date,
            })
            lot["qty"] -= take
            remaining  -= take
            if lot["qty"] <= 1e-9:
                queue.pop(0)
    return closed


STRATEGY_LABELS = {"s1": "📊 S1 Pullback", "s2": "🚀 S2 Breakout", "s3": "🎯 S3 Leader Dip"}


async def build_monthly_report(year: Optional[int] = None,
                               month: Optional[int] = None) -> dict:
    """
    Per-strategy performance for one calendar month, from actual Alpaca fills.

    Ranked by NET P&L, not win rate. Win rate alone is misleading — it is
    trivially raised by booking winners earlier, at the cost of payoff and
    total return (see the profit-ladder note in CLAUDE.md), so the report
    always shows win rate next to payoff and expectancy.
    """
    now = datetime.now(BERLIN)
    year = year or now.year
    month = month or now.month
    prefix = f"{year:04d}-{month:02d}"

    try:
        orders = await alpaca_get("/orders", params={
            "status": "closed", "limit": "500", "direction": "desc"
        })
    except Exception as e:
        log.error(f"Monthly report: could not fetch orders: {e}")
        orders = []

    resolve_strat = _strategy_resolver()
    for o in orders:
        o["strategy"] = resolve_strat(o.get("symbol"),
                                      o.get("filled_at") or o.get("created_at"))

    # Match across ALL history, then keep the round-trips that CLOSED this month
    # — a trade opened in June and sold in July belongs to July.
    round_trips = [r for r in match_closed_trades(orders)
                   if (r.get("sell_date") or "").startswith(prefix)]

    per_strategy = []
    for strat in ("s1", "s2", "s3"):
        rts = [r for r in round_trips if r["strategy"] == strat]
        wins = [r for r in rts if r["pnl"] > 0]
        losses = [r for r in rts if r["pnl"] <= 0]
        avg_win = (sum(r["pnl"] for r in wins) / len(wins)) if wins else 0.0
        avg_loss = abs(sum(r["pnl"] for r in losses) / len(losses)) if losses else 0.0
        net = sum(r["pnl"] for r in rts)
        per_strategy.append({
            "strategy": strat,
            "label": STRATEGY_LABELS[strat],
            "trades": len(rts),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(rts) * 100, 1) if rts else 0.0,
            "net_pnl": round(net, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "payoff_b": round(avg_win / avg_loss, 2) if avg_loss else 0.0,
            "expectancy": round(net / len(rts), 2) if rts else 0.0,
            "best": max((r["pct"] for r in rts), default=0.0),
            "worst": min((r["pct"] for r in rts), default=0.0),
        })

    traded = [s for s in per_strategy if s["trades"] > 0]
    traded.sort(key=lambda s: s["net_pnl"], reverse=True)

    return {
        "period": prefix,
        "month_name": datetime(year, month, 1).strftime("%B %Y"),
        "total_trades": len(round_trips),
        "total_pnl": round(sum(r["pnl"] for r in round_trips), 2),
        "strategies": per_strategy,
        "ranked": traded,
        "winner": traded[0] if traded else None,
    }


def format_monthly_report(rep: dict) -> str:
    """Telegram-formatted month-on-month strategy comparison."""
    lines = [f"📅 *Monthly Report — {rep['month_name']}*", ""]

    if not rep["ranked"]:
        lines.append("_No positions were closed this month._")
        return "\n".join(lines)

    w = rep["winner"]
    lines.append(f"🏆 *Best strategy: {w['label']}*")
    lines.append(f"   Net P&L *${w['net_pnl']:+,.2f}* over {w['trades']} closed trade(s)")
    lines.append(f"   Win rate *{w['win_rate']:.1f}%*  ({w['wins']}W / {w['losses']}L)")
    lines.append("")

    for s in rep["ranked"]:
        lines.append(f"{s['label']}")
        lines.append(f"   Win rate: *{s['win_rate']:.1f}%*  ({s['wins']}W / {s['losses']}L of {s['trades']})")
        lines.append(f"   Net P&L:  ${s['net_pnl']:+,.2f}   (avg ${s['expectancy']:+,.2f}/trade)")
        lines.append(f"   Payoff:   {s['payoff_b']:.2f}  (avg win ${s['avg_win']:,.2f} vs avg loss ${s['avg_loss']:,.2f})")
        lines.append(f"   Best {s['best']:+.1f}%  |  Worst {s['worst']:+.1f}%")
        lines.append("")

    idle = [s["label"] for s in rep["strategies"] if s["trades"] == 0]
    if idle:
        lines.append(f"_No closed trades: {', '.join(idle)}_")

    lines.append(f"*Total: {rep['total_trades']} trades, ${rep['total_pnl']:+,.2f}*")
    lines.append("")
    lines.append(
        "_Ranked by net P&L, not win rate — a high win rate with a low payoff "
        "loses money. Payoff below 1.00 means the average win is smaller than "
        "the average loss._"
    )
    return "\n".join(lines)


@app.get("/api/report/monthly")
async def monthly_report(year: Optional[int] = None, month: Optional[int] = None):
    """Monthly per-strategy comparison as JSON (used by the dashboard)."""
    return await build_monthly_report(year, month)


@app.post("/api/report/monthly/send")
async def monthly_report_send(year: Optional[int] = None, month: Optional[int] = None):
    """Build and push the monthly report to Telegram now."""
    from notifier import send_telegram
    rep = await build_monthly_report(year, month)
    ok = send_telegram(format_monthly_report(rep), strategy="s1")
    return {"sent": ok, "period": rep["period"], "trades": rep["total_trades"]}


async def _monthly_report_loop():
    """
    Fires at 09:00 Berlin on the 1st of each month, reporting the month that
    just ended.
    """
    from notifier import send_telegram
    log.info("Monthly report loop started — 09:00 Berlin on the 1st")
    while True:
        now = datetime.now(BERLIN)
        # First of next month at 09:00
        nxt = (now.replace(day=1, hour=9, minute=0, second=0, microsecond=0)
               + timedelta(days=32)).replace(day=1)
        if now.day == 1 and now.hour < 9:
            nxt = now.replace(hour=9, minute=0, second=0, microsecond=0)
        sleep_sec = (nxt - now).total_seconds()
        log.info(f"Next monthly report: {nxt:%Y-%m-%d %H:%M %Z} (in {sleep_sec/86400:.1f}d)")
        await asyncio.sleep(sleep_sec)

        try:
            prev = datetime.now(BERLIN).replace(day=1) - timedelta(days=1)
            rep = await build_monthly_report(prev.year, prev.month)
            send_telegram(format_monthly_report(rep), strategy="s1")
            log.info(f"Monthly report sent for {rep['period']}")
        except Exception as e:
            log.exception(f"Monthly report failed: {e}")


@app.get("/api/push/key")
async def push_key():
    """Public VAPID key. Safe to hand out — it only lets a browser subscribe."""
    import push
    return {"key": push.public_key(), "configured": push.configured()}


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request):
    import push
    return push.subscribe(await request.json())


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(request: Request):
    import push
    return push.unsubscribe((await request.json()).get("endpoint", ""))


@app.post("/api/push/clear-web")
async def push_clear_web():
    """Drop every browser/TWA push subscription.

    Two apps subscribing to the same events meant two notifications per trade,
    and tapping either one opened the TWA — because a web push belongs to the
    service worker that registered it, not to whichever app you prefer. With the
    native app in place the web channel is redundant, and one owner is the only
    stable arrangement.
    """
    import push
    n = len(push._load())
    push._save([])
    return {"ok": True, "cleared": n}


@app.post("/api/push/native/register")
async def push_native_register(request: Request):
    """Register a native (FCM) device token. Read-token only, like web push —
    registering a phone for notifications moves no money."""
    import push
    b = await request.json()
    return push.register_native(b.get("token", ""), b.get("platform", "android"))


@app.get("/api/notifications")
async def notifications():
    """Alerts from the last NOTIF_RETAIN_HOURS (default 48), newest first.

    Exists because a tapped notification is gone, and a trade signal is the
    wrong thing to lose — it carried the entry, stop and reasoning.
    """
    import push
    items = push.history()
    return {"items": items, "count": len(items),
            "retain_hours": push.NOTIF_RETAIN_HOURS}


@app.get("/api/push/status")
async def push_status():
    """Which half of push is broken: registration, or delivery?"""
    import push
    return push.status()


@app.post("/api/push/test")
async def push_test():
    """Fire a test notification. Deliberately a READ-token action.

    Confirming your own phone receives notifications is not a money-moving
    operation, and requiring the trade PIN for it meant the only way to find out
    whether push worked was to wait for a real trade — which is why it looked
    broken rather than untested.
    """
    import push
    sample = {"ticker": "HUM", "score": 87, "price": 385.88, "rsi": 52.0,
              "macd": 0.93, "atr_pct": 4.2, "ema20": 380.65, "mom_3m": 28.4,
              "rel_strength": 24.3,
              "reasons": ["Confirmed uptrend — price > EMA200, EMA50 rising",
                          "Price $385.88 above EMA50 $365.40",
                          "3M momentum +28.4%"]}
    title, body = push.format_signal(sample, "s1")
    title += " (sample)"
    # Through _fanout, not send()/send_native() directly: a self-test that
    # skips half the delivery path proves less than it appears to. It was
    # bypassing _record(), so tests never showed up in Alerts — exactly the
    # thing the test is meant to demonstrate.
    push._fanout(title, body, "test", sticky=True)
    st = push.status()
    return {"sent": len(st["native"]["devices"]) + st["web"]["subs"],
            "native_sent": len(st["native"]["devices"]),
            "web_subs": st["web"]["subs"], "recorded": True}


@app.get("/api/picks/outcomes")
async def picks_outcomes(limit: int = 40):
    """What the bot picked, and what those names actually did afterwards.

    Separate from the trade log on purpose: most picks are never bought, so
    judging the scoring engines by trades alone only ever measures the subset
    that survived the weekly limit, the cash share and the already-held check.
    """
    import picks_log
    return {"summary": picks_log.summary(), "recent": picks_log.recent(limit)}


@app.post("/api/picks/backfill")
async def picks_backfill():
    """Force the forward-return fill instead of waiting for 03:30 ET."""
    import picks_log
    return await asyncio.to_thread(picks_log.fill_forward_returns)


@app.get("/api/strategy/compare")
async def strategy_compare():
    """Return trade performance split by strategy for the dashboard Strategy tab."""
    from auto_trader import trader as _trader
    all_trades = _trader.tradelog.data.get("trades", [])

    # Real P&L comes from Alpaca fills, not from our own buy log.
    try:
        closed_orders = await alpaca_get("/orders", params={
            "status": "closed", "limit": "500", "direction": "desc"
        })
    except Exception:
        closed_orders = []

    # Tag each order with the strategy that opened the position, as of the
    # order's own time — see _strategy_resolver().
    resolve_strat = _strategy_resolver()
    for o in closed_orders:
        o["strategy"] = resolve_strat(o.get("symbol"),
                                      o.get("filled_at") or o.get("created_at"))

    round_trips = await _annotate_names(match_closed_trades(closed_orders))

    def _strategy_stats(strat: str) -> dict:
        trades = [t for t in all_trades if t.get("strategy") == strat]
        rts    = [r for r in round_trips if r["strategy"] == strat]

        wins    = [r for r in rts if r["pnl"] > 0]
        losses  = [r for r in rts if r["pnl"] <= 0]
        net_pnl = sum(r["pnl"] for r in rts)

        return {
            "strategy": strat,
            "label": {"s1": "S1 Pullback", "s2": "S2 Breakout", "s3": "S3 Leader Dip"}[strat],
            "total_trades":   len(trades),
            "closed_trades":  len(rts),
            "profitable":     len(wins),
            "loss_making":    len(losses),
            "win_rate":       round(len(wins) / len(rts) * 100, 1) if rts else 0,
            "net_pnl":        round(net_pnl, 2),
            "avg_return_pct": round(sum(r["pct"] for r in rts) / len(rts), 2) if rts else 0,
            "trades":         trades[-50:],
            "closed":         rts[-50:],
        }

    # Picks caches
    s1_picks = _load_picks()
    s2_picks = _load_picks_s2()

    s3_picks = _load_picks_s3()
    return {
        "s1": _strategy_stats("s1"),
        "s2": _strategy_stats("s2"),
        "s3": _strategy_stats("s3"),
        "s1_picks": s1_picks.get("picks", []) if s1_picks else [],
        "s2_picks": s2_picks.get("picks", []) if s2_picks else [],
        "s3_picks": s3_picks.get("picks", []) if s3_picks else [],
        "s1_scanned_at": s1_picks.get("scanned_at") if s1_picks else None,
        "s2_scanned_at": s2_picks.get("scanned_at") if s2_picks else None,
        "s3_scanned_at": s3_picks.get("scanned_at") if s3_picks else None,
        # Booked profit and loss as separate figures, over EVERY round-trip and
        # not just the attributed ones. Summing the per-strategy blocks would
        # silently drop the untagged history — most of this account's realized
        # P&L — and report a total that disagrees with Alpaca.
        "totals": _booked_totals(round_trips),
    }


def _booked_totals(round_trips: list[dict]) -> dict:
    """Gross profit, gross loss and net across all matched round-trips."""
    wins   = [r for r in round_trips if r["pnl"] > 0]
    losses = [r for r in round_trips if r["pnl"] <= 0]
    gross_profit = sum(r["pnl"] for r in wins)
    gross_loss   = sum(r["pnl"] for r in losses)     # negative or zero
    return {
        "closed_trades": len(round_trips),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      round(len(wins) / len(round_trips) * 100, 1) if round_trips else 0,
        "gross_profit":  round(gross_profit, 2),
        "gross_loss":    round(gross_loss, 2),
        "net_pnl":       round(gross_profit + gross_loss, 2),
        "avg_win":       round(gross_profit / len(wins), 2) if wins else 0,
        "avg_loss":      round(gross_loss / len(losses), 2) if losses else 0,
        # Expectancy is what decides profitability — win rate alone is
        # trivially raised by booking winners early, and has been misleading
        # here before. payoff = avg win / avg loss.
        "payoff":        round(abs((gross_profit / len(wins)) / (gross_loss / len(losses))), 2)
                         if wins and losses and gross_loss else 0,
    }


@app.get("/api/scan/stream/s2")
async def scan_stream_s2():
    """SSE endpoint — scans the curated universe with Strategy 2 (VCP Breakout)."""
    from scanner import FALLBACK_UNIVERSE, get_ticker_name, CHUNK_SIZE
    from strategy2 import score_from_df_s2
    from strategy import batch_download, benchmark_return_3m

    async def generator():
        import json
        loop = asyncio.get_event_loop()
        universe = FALLBACK_UNIVERSE
        bench = await loop.run_in_executor(None, benchmark_return_3m)
        total = len(universe)
        yield f"data: {json.dumps({'status': 'downloading', 'total': total, 'strategy': 's2'})}\n\n"

        scanned = 0
        emitted = 0
        chunks = [universe[i:i + CHUNK_SIZE] for i in range(0, len(universe), CHUNK_SIZE)]

        for chunk in chunks:
            data = await loop.run_in_executor(None, batch_download, chunk)
            for ticker in chunk:
                result = score_from_df_s2(ticker, data.get(ticker), bench_ret_3m=bench)
                scanned += 1
                if scanned % 25 == 0 or scanned == total:
                    yield f"data: {json.dumps({'progress': True, 'scanned': scanned, 'total': total})}\n\n"
                if not (result.get("ok") and result.get("stage2")):
                    continue
                if result.get("score", 0) < 60:
                    continue
                result["total"] = total
                result["name"] = get_ticker_name(ticker)
                yield f"data: {json.dumps(result)}\n\n"
                emitted += 1

        yield f"data: {json.dumps({'done': True, 'scanned': scanned, 'emitted': emitted, 'total': total})}\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/auto/scan-now/s2")
async def auto_scan_now_s2(force: bool = False):
    """Force an immediate S2 scan."""
    from scanner import find_top_picks_s2
    if force:
        log.info("[S2] TEST MODE — scanning 5 stocks only")
        picks = find_top_picks_s2(n=3, max_stocks=5)
        _save_picks_s2(picks)
    else:
        picks = await _run_scan_and_cache_s2()
    return {"picks": len(picks), "strategy": "s2", "forced": force}


# ---------- STATIC ----------
@app.get("/kite")
def kite_alias():
    """The prototype now IS the dashboard. Kept so existing /kite links land
    somewhere sensible instead of 404ing."""
    return RedirectResponse("/", status_code=307)


@app.get("/privacy")
def privacy_policy():
    """Public privacy policy — Play Console requires a reachable URL for the
    Data safety declaration, and it must not sit behind the token gate because
    Google's reviewers fetch it without credentials."""
    p = HERE / "privacy.html"
    if not p.exists():
        return JSONResponse({"error": "privacy.html not found"}, status_code=404)
    return FileResponse(p, media_type="text/html")


@app.get("/.well-known/assetlinks.json")
def asset_links():
    """Digital Asset Links — proves this site and the Android app are the same owner.

    Without it the TWA still runs but opens with a browser address bar across the
    top, which is the giveaway that it is a wrapped web page rather than an app.
    Chrome fetches this over HTTPS at install and verifies the certificate
    fingerprint of the APK against the list here.

    TWA_SHA256_FINGERPRINT comes from the signing key created at build time, and
    from Play itself once Play App Signing re-signs the upload — those are
    DIFFERENT fingerprints, and both must be listed or the app verifies in
    testing and then shows the address bar in production. Comma-separate them.
    """
    fps = [f.strip().upper() for f in
           os.getenv("TWA_SHA256_FINGERPRINT", "").split(",") if f.strip()]
    pkg = os.getenv("TWA_PACKAGE_NAME", "app.raanu.twa").strip()
    if not fps:
        # Explicit over an empty list: an empty [] looks like a valid answer to
        # Chrome and fails verification silently.
        return JSONResponse(
            {"error": "TWA_SHA256_FINGERPRINT is not set — run the bubblewrap "
                      "build, then set it to the signing key's SHA-256"},
            status_code=503,
        )
    return JSONResponse([{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {"namespace": "android_app",
                   "package_name": pkg,
                   "sha256_cert_fingerprints": fps},
    }])


# ---------- PWA ----------
# These sit outside /api/ so they are not behind the token gate: a service
# worker and a manifest must be fetchable before the user has authenticated,
# or the app cannot install and the unlock screen itself would not render
# offline.
@app.get("/manifest.webmanifest")
def pwa_manifest():
    p = HERE / "manifest.webmanifest"
    if not p.exists():
        return JSONResponse({"error": "manifest not found"}, status_code=404)
    return FileResponse(p, media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    """Served from the ROOT path on purpose.

    A service worker may only control pages at or below its own path, so one
    served from /static/sw.js could not intercept "/". Cache-Control: no-cache
    lets browsers pick up a new worker without waiting out a cached copy.
    """
    p = HERE / "sw.js"
    if not p.exists():
        return JSONResponse({"error": "sw.js not found"}, status_code=404)
    return FileResponse(p, media_type="application/javascript",
                        headers={"Cache-Control": "no-cache",
                                 "Service-Worker-Allowed": "/"})


@app.get("/icons/{name}")
def pwa_icon(name: str):
    # Basename only — an icon path is never a reason to walk the filesystem.
    p = (HERE / "icons" / Path(name).name)
    if p.suffix.lower() != ".png" or not p.exists():
        return JSONResponse({"error": "icon not found"}, status_code=404)
    return FileResponse(p, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=604800"})


@app.get("/legacy")
def legacy_dashboard():
    """The previous dark dashboard. Retained because it still has controls the
    new one does not: editable exit-rule inputs, the PIN gate, per-signal
    Execute, the equity chart and the S2/S3 scan streams. Reach for it when you
    need one of those; everything else lives at /."""
    p = HERE / "RaanuTradingBot.legacy.html"
    if not p.exists():
        return JSONResponse({"error": "RaanuTradingBot.legacy.html not found."}, status_code=404)
    return FileResponse(p)


@app.get("/")
def root():
    html_path = HERE / "RaanuTradingBot.html"
    if not html_path.exists():
        return JSONResponse({"error": "RaanuTradingBot.html not found."}, status_code=404)
    return FileResponse(html_path)


# ---------- MAIN ----------
if __name__ == "__main__":
    import uvicorn
    from auto_trader import MIN_SIGNAL_SCORE, per_trade_max_for, weekly_limit_for

    print("=" * 60)
    print("  RaanuTradingBot — Alpaca Backend")
    print("=" * 60)
    print(f"  Mode:          {ALPACA_MODE.upper()}")
    print(f"  Broker URL:    {BROKER_BASE}")
    print(f"  API key:       {'configured ✓' if ALPACA_API_KEY else 'NOT SET — edit .env'}")
    print(f"  Dashboard:     http://localhost:8000")
    print(f"  Pre-market:    03:30 ET daily — scan + Telegram alert (no orders)")
    # Driven off _ET_SLOTS, not hardcoded — the previous literal went stale the
    # moment the slot times and counts changed.
    print(f"  Trade slots:   " + " | ".join(
        f"{h:02d}:{m:02d} ET — top {n} orders" for h, m, n, _ in _ET_SLOTS))
    print(f"                 Every weekday — per-strategy weekly limit decides if an order follows")
    print(f"  Weekly limit:  S1 {weekly_limit_for('s1')} | "
          f"S2 {weekly_limit_for('s2')} | S3 {weekly_limit_for('s3')} trades/week")
    print(f"  Per-trade cap: S1 ${per_trade_max_for('s1'):,.0f} | "
          f"S2 ${per_trade_max_for('s2'):,.0f} | S3 ${per_trade_max_for('s3'):,.0f}")
    print(f"  Min score:     {MIN_SIGNAL_SCORE}/100")
    print("=" * 60)

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False)
