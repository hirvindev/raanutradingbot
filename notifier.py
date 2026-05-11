"""
notifier.py — WhatsApp alerts via Twilio
=========================================
Sends morning picks, trade confirmations, and profit/loss alerts
to the user's WhatsApp. Executes BUY/SELL commands from replies.
"""

import os
import logging
import httpx
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("raanu.notifier")
BERLIN = ZoneInfo("Europe/Berlin")


def _creds() -> dict:
    return {
        "sid":   os.getenv("TWILIO_ACCOUNT_SID", "").strip(),
        "token": os.getenv("TWILIO_AUTH_TOKEN", "").strip(),
        "from":  os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886").strip(),
        "to":    os.getenv("USER_WHATSAPP", "whatsapp:+919176911755").strip(),
    }


def is_configured() -> bool:
    c = _creds()
    return bool(c["sid"] and c["token"])


def send_whatsapp(message: str) -> bool:
    """Send a WhatsApp message to the configured number."""
    c = _creds()
    if not c["sid"] or not c["token"]:
        log.warning("Twilio not configured — WhatsApp skipped. Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in .env")
        return False

    try:
        resp = httpx.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{c['sid']}/Messages.json",
            auth=(c["sid"], c["token"]),
            data={"From": c["from"], "To": c["to"], "Body": message},
            timeout=15,
        )
        resp.raise_for_status()
        log.info(f"WhatsApp sent — sid: {resp.json().get('sid', '?')}")
        return True
    except Exception as e:
        log.error(f"WhatsApp send failed: {e}")
        return False


# ── MESSAGE FORMATTERS ───────────────────────────────────────────────────────

def format_daily_alert(picks: list[dict]) -> str:
    now = datetime.now(BERLIN)
    lines = [
        "🤖 *RaanuTradingBot — Morning Alert*",
        f"📅 {now.strftime('%A, %d %b %Y')} | 🕢 07:30 Berlin\n",
    ]

    if not picks:
        lines += [
            "⚠️ *No strong signals today.*",
            "Market may be choppy — hold existing positions.",
            "",
            "Reply *STATUS* to see your portfolio.",
        ]
        return "\n".join(lines)

    lines.append(f"📊 *Top {len(picks)} XETRA/GETTEX picks:*\n")
    rank_emoji = ["1️⃣", "2️⃣", "3️⃣"]

    dashboard_url = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
    if dashboard_url and not dashboard_url.startswith("http"):
        dashboard_url = "https://" + dashboard_url

    for i, p in enumerate(picks):
        score = p.get("score", 0)
        heat = "🔥" if score >= 75 else "📈" if score >= 60 else "📊"
        ticker = p["ticker"].replace(".DE", "")
        adr = p.get("us_adr")
        buy_ticker = adr or ticker
        reasons = " | ".join(p.get("reasons", [])[:2])

        lines += [
            f"{rank_emoji[i]} *{ticker}* (XETRA) {heat} Score {score}/100",
            f"   💶 €{p.get('price', 0):.2f} | RSI {p.get('rsi', 0):.0f} | {reasons}",
            f"   🇺🇸 Buy on Alpaca as: *{buy_ticker}*",
            "",
        ]

    top = picks[0]
    top_adr = top.get("us_adr") or top["ticker"].replace(".DE", "")

    lines += [
        "─────────────────────",
        "📲 *Reply to act:*",
        f"  *BUY {top_adr}* — buy top pick ($500)",
        f"  *BUY {top_adr} 200* — custom USD amount",
        f"  *SELL {top_adr}* — close position",
        f"  *STATUS* — portfolio summary",
        f"  *PICKS* — refresh now",
    ]
    if dashboard_url:
        lines += ["", f"🖥 Dashboard: {dashboard_url}"]
    return "\n".join(lines)


def format_trade_confirm(action: str, ticker: str, usd: float, status: str) -> str:
    emoji = "✅" if action == "BUY" else "🔴"
    verb = "Bought" if action == "BUY" else "Sold"
    return (
        f"{emoji} *{verb}: {ticker}*\n"
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
    cash  = float(account.get("free", account.get("cash", 0)))
    pnl   = float(account.get("ppl", account.get("unrealized_pl", 0)))

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
