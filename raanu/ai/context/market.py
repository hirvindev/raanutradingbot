"""
raanu.ai.context.market — the tape, as a provider
==================================================
A thin adapter over :mod:`raanu.ai.market_context`, which already produced
exactly this shape and already degraded rather than raising.

Deliberately NOT a rewrite. That module carries hard-won detail — three
readings per symbol because a bought -1.5% gap is a different tape from a
sliding one, all eleven sector SPDRs because breadth beats any single index,
and an explicit warning never to source it from the bars cache (those freeze
at the first fetch of the ET day, so an intraday regime read from them is
stale by construction). Wrapping it keeps that; reimplementing it would lose
it one comment at a time.
"""

from __future__ import annotations

import asyncio

name = "market"
required = False


async def fetch() -> dict | None:
    from raanu.ai import market_context
    # snapshot() does blocking HTTP plus a yfinance call, so it must not run
    # on the event loop the exit monitor shares.
    data = await asyncio.to_thread(market_context.snapshot)
    if not data:
        return None
    # `partial` is assemble()'s key; fold this provider's own misses into a
    # named field so two levels of "what is missing" do not collide.
    inner = data.pop("partial", None)
    if inner:
        data["missing_symbols"] = inner
    return data
