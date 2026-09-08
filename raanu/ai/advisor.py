"""
raanu.ai.advisor — one call per execution slot
===============================================
Takes the quant's candidates plus the market picture, asks the model, and
returns a validated :class:`~raanu.ai.schema.SlotVerdict` — or ``None``.

**``None`` means "place no orders this slot".** That is the whole failure
contract, and it is deliberately the only one: network error, timeout,
refusal, schema violation, empty response — every path funnels through a
single ``except Exception`` and returns ``None``. One choke point is what
makes fail-closed a property rather than an aspiration, matching the same
philosophy as the auto-trader switch, where an unreadable store reads as OFF.

Fail-closed here blocks *entries* only. The exit monitor never consults this
module, so an API outage can never strand an open position. Keep it that way.
"""

from __future__ import annotations

import json
import logging
import time

from raanu import config, trace
from raanu.ai.prompts import EVIDENCE_VERSION, SYSTEM_PROMPT
from raanu.ai.schema import SlotVerdict

log = logging.getLogger("raanu.ai.advisor")

# Domains the model may search for macro context. Search results are
# third-party text that the model then acts on, so the surface is narrowed to
# outlets that report events rather than sell opinions. It is establishing
# what happened, not sourcing stock tips — which is why Benzinga, MarketBeat
# and TradingView are deliberately absent despite being reachable.
#
# ⚠️ EVERY ENTRY IS VERIFIED REACHABLE BY ANTHROPIC'S CRAWLER (2026-09-08).
# The API rejects the WHOLE REQUEST with a 400 if any listed domain blocks the
# crawler, and because this advisor fails closed that 400 becomes a silent
# no-trade day. The first version of this list contained reuters.com,
# apnews.com, wsj.com, ft.com and marketwatch.com — all five are blocked, so
# it would have stopped trading entirely on its first live slot.
#
# Do NOT add a domain here without testing it. Overridable via
# LLM_SEARCH_DOMAINS so a crawler-access change can be fixed without a deploy.
DEFAULT_SEARCH_DOMAINS = [
    "bloomberg.com", "cnbc.com", "federalreserve.gov", "finance.yahoo.com",
    "axios.com", "npr.org", "cnn.com", "fortune.com",
]


def search_domains() -> list[str]:
    return config.env_list("LLM_SEARCH_DOMAINS") or DEFAULT_SEARCH_DOMAINS


def _payload(candidates: dict[str, list[dict]], context: dict, label: str) -> str:
    """What the model is shown. Only fields the scorer already computed."""
    fields = ("ticker", "name", "score", "price", "rsi", "macd", "macd_signal",
              "rel_strength", "mom_1m", "mom_3m", "reasons", "in_golden_pocket",
              "uptrend", "stage2", "leader_dip", "atr_pct")
    trimmed = {
        strategy: [
            {k: pick.get(k) for k in fields if pick.get(k) is not None}
            for pick in picks
        ]
        for strategy, picks in candidates.items() if picks
    }
    return json.dumps({
        "slot": label,
        "market": context,
        "candidates": trimmed,
        "note": ("Ranks are across ALL strategies together. Every candidate "
                 "listed already passed the quant's score and structure gates."),
    }, default=str, sort_keys=True)


def _client():
    from anthropic import AsyncAnthropic
    return AsyncAnthropic(api_key=config.llm_api_key(),
                          timeout=config.llm_timeout_sec())


def _tools() -> list[dict]:
    if not config.llm_web_search():
        return []
    return [{
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": 3,
        "allowed_domains": search_domains(),
    }]


async def _call_anthropic(payload: str) -> SlotVerdict:
    """One structured-output request. Raises on anything unexpected."""
    client = _client()
    kwargs = {
        "model": config.llm_model(),
        "max_tokens": 16000,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": payload}],
        "output_format": SlotVerdict,
    }
    tools = _tools()
    if tools:
        kwargs["tools"] = tools

    resp = await client.messages.parse(**kwargs)

    # A server-tool turn can stop mid-flight rather than erroring. Resuming
    # once is the difference between a usable answer and a silently truncated
    # one that looks like a refusal to trade.
    if getattr(resp, "stop_reason", None) == "pause_turn":
        log.info("[llm] pause_turn — resuming once")
        resp = await client.messages.parse(
            **{**kwargs, "messages": [
                {"role": "user", "content": payload},
                {"role": "assistant", "content": resp.content},
            ]})

    if getattr(resp, "stop_reason", None) == "refusal":
        raise RuntimeError(f"model refused: {getattr(resp, 'stop_details', None)}")

    verdict = getattr(resp, "parsed_output", None)
    if verdict is None:
        raise ValueError("no parsed_output on response")

    usage = getattr(resp, "usage", None)
    if usage is not None:
        verdict._usage = {
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        }
    return verdict


async def _call(payload: str) -> SlotVerdict:
    provider = config.llm_provider()
    if provider == "anthropic":
        return await _call_anthropic(payload)
    # Deliberately not a stub for every provider we might one day want: an
    # untested branch is not portability. config.llm_provider() exists so the
    # switch is a config value; add a branch when a second one is real.
    raise ValueError(f"unsupported LLM_PROVIDER: {provider!r}")


async def review_slot(candidates: dict[str, list[dict]], context: dict,
                      label: str) -> SlotVerdict | None:
    """Review one slot's candidates. ``None`` ⇒ place no orders this slot."""
    total = sum(len(v) for v in candidates.values())
    started = time.monotonic()

    trace.emit("llm.request", slot=label,
               model=config.llm_model(),
               evidence_version=EVIDENCE_VERSION,
               candidates=total,
               web_search=config.llm_web_search(),
               by_strategy={k: len(v) for k, v in candidates.items() if v})

    try:
        verdict = await _call(_payload(candidates, context, label))
        elapsed = round(time.monotonic() - started, 2)

        trace.emit("llm.response", slot=label,
                   elapsed_sec=elapsed,
                   trade_today=verdict.trade_today,
                   regime=verdict.regime,
                   market_summary=verdict.market_summary,
                   budget_pct=verdict.budget_pct,
                   usage=getattr(verdict, "_usage", None),
                   decisions=[d.model_dump() for d in verdict.decisions])

        log.info(f"[llm][{label}] {verdict.regime} trade_today={verdict.trade_today} "
                 f"{len(verdict.decisions)} decisions in {elapsed}s")
        return verdict

    except Exception as e:
        elapsed = round(time.monotonic() - started, 2)
        # The single choke point. Every failure mode lands here and becomes
        # "no orders", never a partial or guessed verdict.
        log.warning(f"[llm][{label}] {type(e).__name__}: {e} — failing closed "
                    f"after {elapsed}s, no orders this slot")
        trace.emit("llm.failed", slot=label,
                   error_type=type(e).__name__, error=str(e),
                   elapsed_sec=elapsed, candidates=total)
        return None
