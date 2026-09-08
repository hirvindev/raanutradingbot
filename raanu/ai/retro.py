"""
raanu.ai.retro — what actually made the week good or bad
=========================================================
Joins the decision trace to what happened afterwards, and asks the model to
explain the week.

This is why the trace exists. Individually, neither record can answer the
question: `picks_log` knows a pick returned -4% but not why it was bought,
and the trace knows the advisor called it risk_on at rank 1 but not what
followed. Together they support the only question worth asking of any of
this — **did the reasoning predict the outcome?**

Deliberately **read-only**. It produces a report and changes no setting. One
week is nowhere near enough to retune anything: this project has a documented
history of configurations that looked excellent in one half of a window and
lost money in the other, which is what `--robustness` exists to catch. A
retrospective that quietly adjusted live parameters would be re-committing
that exact error, automatically and on less data.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from raanu import config, trace
from raanu.ai.prompts import RETRO_SYSTEM_PROMPT

log = logging.getLogger("raanu.ai.retro")


def _outcomes(days: int) -> list[dict]:
    """Picks from the window that have a forward return yet, with the
    advisor's verdict still attached to each row."""
    try:
        from raanu import state
        from raanu.state import keys

        start = (datetime.now(UTC).date() - timedelta(days=days)).isoformat()
        rows = [r.data for r in state.query(keys.PICK, sk_gte=f"{start}#")]
        keep = ("date", "strategy", "ticker", "score", "fwd", "spy",
                "llm_approve", "llm_rank", "llm_confidence", "llm_rationale",
                "llm_regime", "llm_trade_today", "llm_exit_plan")
        return [{k: r.get(k) for k in keep if r.get(k) is not None} for r in rows]
    except Exception as e:
        log.warning(f"[retro] outcomes unavailable: {e}")
        return []


def _closed_trades(days: int) -> list[dict]:
    """Round-trips closed inside the window, with realized P&L."""
    try:
        from raanu.trading.trader import get_trader

        cutoff = datetime.now(UTC) - timedelta(days=days)
        out = []
        for t in get_trader().tradelog.all_trades():
            if (t.get("action") or "").upper() != "SELL":
                continue
            try:
                ts = datetime.fromisoformat(t["timestamp"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
            except Exception:
                continue
            if ts < cutoff:
                continue
            out.append({k: t.get(k) for k in
                        ("ticker", "strategy", "realized_pnl", "pct",
                         "exit_reason", "timestamp") if t.get(k) is not None})
        return out
    except Exception as e:
        log.warning(f"[retro] trades unavailable: {e}")
        return []


def gather(days: int = 7) -> dict:
    """Everything the review reasons over. Useful on its own for debugging."""
    events = trace.window(days)
    # Decision-shaped events only. The full trace includes per-scan detail that
    # would crowd out the signal without adding to the question being asked.
    interesting = {"llm.response", "llm.failed", "gate.blocked",
                   "order.sized", "order.placed", "order.failed", "exit.fired"}
    return {
        "window_days": days,
        "summary": trace.summarise(days),
        "decisions": [e for e in events if e.get("event") in interesting],
        "pick_outcomes": _outcomes(days),
        "closed_trades": _closed_trades(days),
    }


async def weekly_review(days: int = 7) -> dict:
    """Ask the model what drove the week. Returns the report, or an error dict.

    Never raises: a research job must not be able to disturb the trading path,
    and this runs on the same worker.
    """
    data = gather(days)

    if not data["decisions"] and not data["closed_trades"]:
        return {"ok": False, "reason": "nothing happened in the window"}

    try:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=config.llm_api_key(),
                                timeout=config.llm_timeout_sec())
        resp = await client.messages.create(
            model=config.llm_model(),
            max_tokens=16000,
            system=RETRO_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(data, default=str)}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        trace.emit("retro.done", days=days, decisions=len(data["decisions"]),
                   trades=len(data["closed_trades"]), report=text)
        return {"ok": True, "days": days, "report": text,
                "decisions": len(data["decisions"]),
                "closed_trades": len(data["closed_trades"])}
    except Exception as e:
        log.warning(f"[retro] review failed: {type(e).__name__}: {e}")
        trace.emit("retro.failed", days=days, error_type=type(e).__name__, error=str(e))
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


async def review_and_notify(days: int = 7) -> dict:
    """Weekly review, sent to Telegram. The scheduled entry point."""
    if not config.llm_retro_enabled():
        return {"ok": False, "reason": "LLM_RETRO_ENABLED is off"}

    result = await weekly_review(days)
    if not result.get("ok"):
        return result
    try:
        from raanu.notify.telegram import send_telegram
        send_telegram(f"🔍 *RaanuBot — {days}-day review*\n\n{result['report']}")
    except Exception as e:
        log.warning(f"[retro] telegram send skipped: {e}")
    return result
