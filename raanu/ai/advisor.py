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


# What the model may ask this system about itself. Each maps to a provider in
# raanu.ai.context; see that package for why these are tools while market and
# budget are pre-assembled.
_CONTEXT_TOOLS = [
    {
        "name": "get_trade_history",
        "description": (
            "Closed round-trips over the last N days, with win rate, payoff "
            "and EXPECTANCY per strategy. Use when deciding whether a "
            "strategy's recent record should change how much it is trusted. "
            "Expectancy decides profitability, not win rate."),
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "minimum": 7,
                                    "maximum": 365,
                                    "description": "Look-back window."}},
            "required": [],
        },
    },
    {
        "name": "get_pick_outcomes",
        "description": (
            "What past picks actually returned 1/5/20 trading days later, "
            "bucketed by score band and measured against SPY over the same "
            "window. Use to check whether a higher score has been earning a "
            "higher return lately. Refuses to conclude on a thin sample."),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_open_positions",
        "description": (
            "The current book: each position's value, unrealised P&L, and how "
            "concentrated the largest holding is. Use when weighing whether "
            "a candidate adds correlated exposure."),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


def _tools() -> list[dict]:
    """Server tools plus this system's own read-only tools.

    ``web_search`` is Anthropic-hosted and reaches the open web through a
    domain allowlist; the rest run in this process against this account's own
    DynamoDB and broker data. Keeping both in one list is fine — they differ
    in who executes them, not in how they are declared — but the trust
    boundary is not symmetric, and only the first one crosses it.
    """
    tools: list[dict] = []
    if config.llm_web_search():
        tools.append({
            "type": "web_search_20260209",
            "name": "web_search",
            "max_uses": config.llm_search_max_uses(),
            "allowed_domains": search_domains(),
        })
    if config.llm_tools_enabled():
        tools.extend(_CONTEXT_TOOLS)
    return tools


# Below this there is not enough time left for a request to plausibly finish,
# so the loop stops rather than starting one it knows will time out.
_MIN_REQUEST_SEC = 10.0

# A tool result is untrusted-by-size if not by origin: an unbounded blob would
# be re-sent on every later turn of the loop, multiplying its cost.
_MAX_TOOL_RESULT_CHARS = 20000


async def _run_tool(name: str, args: dict) -> dict:
    """Execute one context tool. Never raises — an error is a tool result.

    A failed tool must not fail the slot: the model asked an optional
    question, and "that lookup did not work" is an answer it can reason
    around. Raising here would turn a degraded picture into no trades at all,
    which is the opposite of what the tool is for.
    """
    from raanu.ai import context

    provider = context.TOOLS.get(name)
    if provider is None:
        return {"error": f"unknown tool {name!r}"}
    try:
        if name == "get_trade_history":
            days = int(args.get("days") or context.history.DEFAULT_DAYS)
            data = await provider.fetch(days=days)
        else:
            data = await provider.fetch()
    except Exception as e:
        log.warning(f"[llm] tool {name} failed: {e}")
        return {"error": f"{type(e).__name__}: {e}"}
    return data if data is not None else {"error": "no data available"}


async def _stream_once(client, kwargs: dict, *, timeout: float | None = None):
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
    if timeout is not None:
        kwargs = {**kwargs, "timeout": timeout}
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
        #
        # ⚠️ NO `cache_control` here, and that is a measured decision rather
        # than an oversight. Prompt caching was added alongside the streaming
        # fix on the theory that a replayed prefix after a timeout would pay
        # for itself. Three live slots later the numbers say otherwise:
        #
        #   cached prefix          ~12,700 tokens (search results land in it,
        #                          so it is ~10x the system prompt alone)
        #   write premium          1.25x  ->  +$0.0095 per slot
        #   read discount          0.10x  ->  -$0.0248 per slot that retries
        #   break-even retry rate  27.7%
        #
        # Slots are 85 minutes apart and the longest TTL is an hour, so
        # nothing ever reads across slots — every observed slot logged
        # `cache_read_input_tokens: 0`. The only reader is a retry, and the
        # streaming fix is precisely what made retries rare (0 in the three
        # slots since). Caching therefore bills a 25% premium as insurance
        # against a failure that no longer happens. Re-add it if the retry
        # rate ever climbs back above ~28%; `llm.response` traces both cache
        # counters, so that is a question with an answer.
    }
    tools = _tools()
    if tools:
        kwargs["tools"] = tools

    # 🔴 One deadline for the WHOLE review, tool loop included.
    #
    # llm_timeout_sec() bounds one HTTP request. With a tool loop the review
    # is several of them, so per-request timeouts no longer bound the review:
    # 3 iterations x 2 attempts x 150s = 900s against a 600s Lambda. Being
    # killed there is strictly worse than failing — the exit-monitor pass
    # shares the invocation and would be skipped, and the llm.failed row would
    # never be written, so the outage would also be invisible.
    #
    # Each request's timeout is shrunk to the time actually remaining, so an
    # overrun surfaces as a normal APITimeoutError through the single except
    # rather than as a dead Lambda.
    deadline = time.monotonic() + config.llm_total_budget_sec()
    messages: list[dict] = [{"role": "user", "content": payload}]
    tool_calls = 0

    for _ in range(config.llm_max_tool_iterations() + 1):
        left = deadline - time.monotonic()
        if left <= _MIN_REQUEST_SEC:
            raise TimeoutError(
                f"advisory review exceeded {config.llm_total_budget_sec()}s "
                f"after {tool_calls} tool call(s)")

        resp = await _stream_once(
            client, {**kwargs, "messages": messages},
            timeout=min(config.llm_timeout_sec(), left))
        stop = getattr(resp, "stop_reason", None)

        # A server-tool turn can stop mid-flight rather than erroring.
        # Resuming is the difference between a usable answer and a silently
        # truncated one that looks like a refusal to trade.
        if stop == "pause_turn":
            log.info("[llm] pause_turn — resuming")
            messages = messages + [{"role": "assistant", "content": resp.content}]
            continue

        if stop != "tool_use":
            break

        # Every tool_result for one assistant turn goes back in a SINGLE user
        # message. Splitting them teaches the model to stop calling tools in
        # parallel, which costs a round trip on every later slot.
        results = []
        for block in resp.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            tool_calls += 1
            data = await _run_tool(block.name, dict(getattr(block, "input", {}) or {}))
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(data, default=str)[:_MAX_TOOL_RESULT_CHARS],
                "is_error": isinstance(data, dict) and "error" in data,
            })
        if not results:
            break
        log.info(f"[llm] ran {len(results)} tool(s), "
                 f"{deadline - time.monotonic():.0f}s of budget left")
        messages = messages + [{"role": "assistant", "content": resp.content},
                               {"role": "user", "content": results}]
    else:
        # Ran out of iterations while the model still wanted tools. Treat it
        # as a failure rather than accepting whatever half-formed answer the
        # last turn contained.
        raise RuntimeError(
            f"advisor still calling tools after "
            f"{config.llm_max_tool_iterations()} iteration(s)")

    if getattr(resp, "stop_reason", None) == "refusal":
        raise RuntimeError(f"model refused: {getattr(resp, 'stop_details', None)}")

    verdict = getattr(resp, "parsed_output", None)
    if verdict is None:
        raise ValueError("no parsed_output on response")

    verdict._usage = _usage_of(resp)
    return verdict


def _reject_incoherent(verdict: SlotVerdict, candidates: int) -> None:
    """Raise on a verdict that is well-formed but self-contradictory.

    ``trade_today=True`` with an EMPTY decisions list is the one that matters.
    It parses, so nothing downstream objects — but ``approved_for()`` drops
    every candidate for want of a decision, so the slot places no orders while
    the trace records a perfectly successful ``llm.response``. That is a
    silent no-trade, which is the exact failure mode this whole module exists
    to make loud.

    Measured, not hypothetical: 1 in 8 live calls on 9 Sep 2026 came back this
    way, at BOTH medium and high effort, on input that the other 7 answered
    with six decisions. It is model variance, not a setting.

    Raising sends it through the same ``except`` as a timeout, so the outcome
    is unchanged — no orders — but it is now an ``llm.failed`` row naming the
    reason instead of an ``llm.response`` that looks fine.
    """
    if verdict.trade_today and candidates and not verdict.decisions:
        raise ValueError(
            f"incoherent verdict: trade_today=True with 0 decisions for "
            f"{candidates} candidate(s)")


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
               tools_enabled=config.llm_tools_enabled(),
               total_budget_sec=config.llm_total_budget_sec(),
               # The prompt-side token bill, tracked because it is the half
               # this code controls. `usage` on llm.response reports what the
               # call actually cost, including what web search dragged in.
               payload_chars=len(payload),
               timeout_sec=config.llm_timeout_sec(),
               max_retries=config.llm_max_retries(),
               by_strategy={k: len(v) for k, v in candidates.items() if v})

    try:
        verdict = await _call(payload)
        _reject_incoherent(verdict, total)
        elapsed = round(time.monotonic() - started, 2)

        trace.emit("llm.response", slot=label,
                   elapsed_sec=elapsed,
                   trade_today=verdict.trade_today,
                   regime=verdict.regime,
                   market_summary=verdict.market_summary,
                   usd_to_deploy=verdict.usd_to_deploy,
                   pacing_note=verdict.pacing_note,
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
