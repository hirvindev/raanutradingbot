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


# Fields the model is shown. Everything here was already computed by the
# scorer — nothing is fetched to build the prompt.
_FIELDS = ("ticker", "name", "score", "price", "rsi", "macd", "macd_signal",
           "rel_strength", "mom_1m", "mom_3m", "reasons", "in_golden_pocket",
           "uptrend", "stage2", "leader_dip", "atr_pct")

# `reasons` is prose restating numbers the model already has as its own
# fields, and the scorers emit up to nine per candidate. The first few carry
# the structural facts that have no numeric column — distance from the
# 52-week high, base tightness, the breakout volume ratio — so the list is
# capped rather than dropped.
_MAX_REASONS = 4

# Enough precision for any indicator here, and it kills the float noise that
# yfinance drags in: `0.15000000000000002` is eighteen tokens of nothing.
# Same reasoning as the 4dp rounding in the daily bars cache.
_ROUND_DP = 4


def _slim(value):
    """Round floats and cap reason lists. The payload is billed per token."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return round(value, _ROUND_DP)
    if isinstance(value, dict):
        return {k: _slim(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [_slim(v) for v in value]
    return value


def _payload(candidates: dict[str, list[dict]], context: dict, label: str) -> str:
    """What the model is shown. Only fields the scorer already computed."""
    trimmed = {}
    for strategy, picks in candidates.items():
        if not picks:
            continue
        rows = []
        for pick in picks:
            row = {k: _slim(pick.get(k)) for k in _FIELDS if pick.get(k) is not None}
            if isinstance(row.get("reasons"), list):
                row["reasons"] = row["reasons"][:_MAX_REASONS]
            rows.append(row)
        trimmed[strategy] = rows
    return json.dumps({
        "slot": label,
        "market": _slim(context),
        "candidates": trimmed,
        "note": ("Ranks are across ALL strategies together. Every candidate "
                 "listed already passed the quant's score and structure gates."),
        # ensure_ascii=False for the same reason the state layer stores a
        # native map: escaped, an em-dash is six tokens of backslash instead
        # of one character, and the scorers' reason strings are full of them.
    }, default=str, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _client():
    """The SDK client.

    ``max_retries`` is passed explicitly because the SDK's default is 2 and
    it retries timeouts — so an unset value silently makes the worst case
    three times ``llm_timeout_sec()``. That is not a hypothetical: it is what
    turned a 60s timeout into the 185s stall that produced this project's
    first fail-closed slot."""
    from anthropic import AsyncAnthropic
    return AsyncAnthropic(api_key=config.llm_api_key(),
                          timeout=config.llm_timeout_sec(),
                          max_retries=config.llm_max_retries())


def _tools() -> list[dict]:
    if not config.llm_web_search():
        return []
    return [{
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": config.llm_search_max_uses(),
        "allowed_domains": search_domains(),
    }]


async def _stream_once(client, kwargs: dict):
    """One streamed request, returning the accumulated final message.

    🔴 **Streaming is the fix, not a style choice.** The first live advisory
    slot (9 Sep 2026) timed out three times at exactly 60s each and placed no
    orders. The call was non-streaming, and a non-streaming request carrying
    adaptive thinking plus web search puts *nothing* on the socket until the
    model is completely done — so from the client's side a request that is
    working normally looks identical to a dead connection, and the only
    question is which arbitrary number the timeout was set to. Streaming
    delivers events as the model thinks, searches and writes, so the timeout
    once again measures what it is supposed to measure.

    ``messages.stream()`` takes the same ``output_format`` as
    ``messages.parse()`` and ``get_final_message()`` returns a
    ``ParsedMessage``, so the validated-verdict contract is unchanged.
    """
    async with client.messages.stream(**kwargs) as stream:
        return await stream.get_final_message()


async def _call_anthropic(payload: str) -> SlotVerdict:
    """One structured-output request. Raises on anything unexpected."""
    client = _client()
    kwargs = {
        "model": config.llm_model(),
        "max_tokens": 16000,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": payload}],
        "output_format": SlotVerdict,
        # Thinking depth, and the largest single line on this call's bill.
        "output_config": {"effort": config.llm_effort()},
        # Caches the tools + system prefix. The saving is almost entirely
        # WITHIN one request rather than across slots: a web-searching turn
        # runs several inference passes over the same ~1.3k-token prefix, and
        # a retry after a timeout replays it again. Across slots it does
        # nothing — 09:35 and 11:00 are 85 minutes apart and the longest
        # cache TTL is an hour — so do not expect a hit rate here.
        "cache_control": {"type": "ephemeral"},
    }
    tools = _tools()
    if tools:
        kwargs["tools"] = tools

    resp = await _stream_once(client, kwargs)

    # A server-tool turn can stop mid-flight rather than erroring. Resuming
    # once is the difference between a usable answer and a silently truncated
    # one that looks like a refusal to trade.
    if getattr(resp, "stop_reason", None) == "pause_turn":
        log.info("[llm] pause_turn — resuming once")
        resp = await _stream_once(client, {**kwargs, "messages": [
            {"role": "user", "content": payload},
            {"role": "assistant", "content": resp.content},
        ]})

    if getattr(resp, "stop_reason", None) == "refusal":
        raise RuntimeError(f"model refused: {getattr(resp, 'stop_details', None)}")

    verdict = getattr(resp, "parsed_output", None)
    if verdict is None:
        raise ValueError("no parsed_output on response")

    verdict._usage = _usage_of(resp)
    return verdict


def _usage_of(resp) -> dict | None:
    """Token counts, including the cache and server-tool lines.

    Recorded in full because the token-reduction work here is a claim, and an
    unmeasured claim is the thing this project keeps having to walk back.
    ``cache_read_input_tokens`` staying at zero across slots means the prefix
    cache is not engaging and the ``cache_control`` above is dead weight."""
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None
    out = {
        field: getattr(usage, field, None)
        for field in ("input_tokens", "output_tokens",
                      "cache_read_input_tokens", "cache_creation_input_tokens")
    }
    searches = getattr(getattr(usage, "server_tool_use", None), "web_search_requests", None)
    if searches is not None:
        out["web_searches"] = searches
    return {k: v for k, v in out.items() if v is not None}


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
    payload = _payload(candidates, context, label)

    trace.emit("llm.request", slot=label,
               model=config.llm_model(),
               evidence_version=EVIDENCE_VERSION,
               candidates=total,
               web_search=config.llm_web_search(),
               effort=config.llm_effort(),
               # The prompt-side token bill, tracked because it is the half
               # this code controls. `usage` on llm.response reports what the
               # call actually cost, including what web search dragged in.
               payload_chars=len(payload),
               timeout_sec=config.llm_timeout_sec(),
               max_retries=config.llm_max_retries(),
               by_strategy={k: len(v) for k, v in candidates.items() if v})

    try:
        verdict = await _call(payload)
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
                 f"{len(verdict.decisions)} decisions in {elapsed}s "
                 f"usage={getattr(verdict, '_usage', None)}")
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
