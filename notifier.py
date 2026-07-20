"""
notifier.py — Telegram alerts via Bot API
==========================================
Sends trade alerts, portfolio status, and profit/loss notifications
to the configured Telegram chat. No session expiry — works 24/7.
"""

import os
import logging
import httpx
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("raanu.notifier")
BERLIN = ZoneInfo("Europe/Berlin")


def _token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def _chat_id_for(strategy: str = "") -> str:
    """Return the chat ID for a strategy, falling back to the default."""
    if strategy == "s1":
        sid = os.getenv("TELEGRAM_CHAT_ID_S1", "").strip()
        if sid:
            return sid
    elif strategy == "s2":
        sid = os.getenv("TELEGRAM_CHAT_ID_S2", "").strip()
        if sid:
            return sid
    return os.getenv("TELEGRAM_CHAT_ID", "").strip()


def is_configured() -> bool:
    return bool(_token() and _chat_id_for())


def send_telegram(message: str, strategy: str = "") -> bool:
    """Send a Telegram message to the strategy-specific or default chat."""
    token = _token()
    chat_id = _chat_id_for(strategy)
    if not token or not chat_id:
        log.warning("Telegram not configured — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        return False
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=15,
        )
        resp.raise_for_status()
        log.info(f"Telegram sent [{strategy or 'general'}] — message_id: {resp.json().get('result', {}).get('message_id', '?')}")
        return True
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return False


# Keep send_whatsapp as an alias so existing callers don't break
def send_whatsapp(message: str, strategy: str = "") -> bool:
    return send_telegram(message, strategy=strategy)


# ── MESSAGE FORMATTERS ───────────────────────────────────────────────────────

def format_daily_alert(picks: list[dict], strategy: str = "") -> str:
    now = datetime.now(BERLIN)
    strat_label = f" [{strategy.upper()}]" if strategy else ""
    lines = [
        f"🤖 *RaanuTradingBot — Morning Alert{strat_label}*",
        f"📅 {now.strftime('%A, %d %b %Y')} | 🕢 07:00 Berlin\n",
    ]

    if not picks:
        lines += [
            "⚠️ *No strong signals today.*",
            "Market may be choppy — holding existing positions.",
        ]
        return "\n".join(lines)

    lines.append(f"📊 *Top {len(picks)} US picks:*\n")
    rank_emoji = ["1️⃣", "2️⃣", "3️⃣"]

    for i, p in enumerate(picks):
        score   = p.get("score", 0)
        heat    = "🔥" if score >= 75 else "📈" if score >= 60 else "📊"
        ticker  = p["ticker"]
        reasons = " | ".join(p.get("reasons", [])[:2])
        lines += [
            f"{rank_emoji[i]} *{ticker}* {heat} Score {score}/100",
            f"   💵 ${p.get('price', 0):.2f} | RSI {p.get('rsi', 0):.0f} | {reasons}",
            "",
        ]

    return "\n".join(lines)


def _strat_tag(strategy: str) -> str:
    if strategy == "s1":
        return "📊 S1 Pullback"
    if strategy == "s2":
        return "🚀 S2 Breakout"
    return ""


def format_pre_trade_alert(ticker: str, _unused: str, usd: float, score: int,
                           free_cash: float, reasons: list[str],
                           strategy: str = "") -> str:
    reasons_str = " | ".join(reasons[:2]) if reasons else "momentum signal"
    tag = f"\n   Strategy: *{_strat_tag(strategy)}*" if strategy else ""
    return (
        f"⚡ *RaanuBot — About to BUY*\n"
        f"   Stock: *{ticker}*{tag}\n"
        f"   Amount: *${usd:.2f}*\n"
        f"   Score: {score}/100\n"
        f"   Signal: {reasons_str}\n"
        f"   Free cash: ${free_cash:,.2f}\n"
        f"   _Order submitting now — check dashboard to cancel_"
    )


def format_trade_confirm(action: str, ticker: str, usd: float, status: str,
                         strategy: str = "") -> str:
    emoji = "✅" if action == "BUY" else "🔴"
    verb  = "Bought" if action == "BUY" else "Sold"
    tag = f"\n   Strategy: *{_strat_tag(strategy)}*" if strategy else ""
    return (
        f"{emoji} *{verb}: {ticker}*{tag}\n"
        f"   Amount: ${usd:.2f}\n"
        f"   Status: {status}\n"
        f"   _via RaanuTradingBot (Alpaca Paper)_"
    )


def format_profit_alert(ticker: str, entry: float, exit_price: float,
                        pnl: float, pct: float, reason: str) -> str:
    emoji = "💰" if pnl >= 0 else "🛑"
    return (
        f"{emoji} *Auto-close: {ticker}*\n"
        f"   Entry: ${entry:.2f} → Exit: ${exit_price:.2f}\n"
        f"   P&L: *${pnl:+.2f} ({pct:+.2f}%)*\n"
        f"   Reason: {reason}\n"
        f"   _Position closed automatically_"
    )


def format_portfolio_status(positions: list[dict], account: dict) -> str:
    total = float(account.get("total", account.get("portfolio_value", 0)))
    cash  = float(account.get("free",  account.get("cash", 0)))
    pnl   = float(account.get("ppl",   account.get("unrealized_pl", 0)))

    lines = [
        "📊 *Portfolio Status*",
        f"   💼 Total: ${total:,.2f}",
        f"   💵 Cash:  ${cash:,.2f}",
        f"   📈 Open P&L: ${pnl:+,.2f}",
        "",
    ]

    if not positions:
        lines.append("   No open positions.")
    else:
        lines.append(f"   *{len(positions)} open position(s):*")
        for p in positions[:5]:
            sym  = p.get("symbol") or p.get("ticker", "?")
            qty  = float(p.get("qty", p.get("quantity", 0)))
            upnl = float(p.get("unrealized_pl", p.get("ppl", 0)))
            upct = float(p.get("unrealized_plpc", 0)) * 100
            arrow = "📈" if upnl >= 0 else "📉"
            lines.append(f"   {arrow} {sym} ×{qty:.2f} | P&L ${upnl:+.2f} ({upct:+.1f}%)")

    return "\n".join(lines)
