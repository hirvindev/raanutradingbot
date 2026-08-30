"""
raanu.trading.reports — closed round-trips and the monthly report
==================================================================
FIFO lot matching over filled orders, and the monthly performance summary
built from it. Lives in the trading layer, not the API layer: the scheduled
report sender needs this without any HTTP involved.
"""

from __future__ import annotations

import logging
from datetime import datetime

from raanu.clock import BERLIN
from raanu.market.rest import alpaca_get

log = logging.getLogger("raanu.trading.reports")


def _strategy_resolver():
    """Late import: the resolver lives with the portfolio routes that own
    position/strategy attribution."""
    from raanu.api.routes.account import _strategy_resolver as resolver
    return resolver


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


async def build_monthly_report(year: int | None = None,
                               month: int | None = None) -> dict:
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
