"""
raanu.ai.context.base — the provider contract
==============================================
Every piece of information the advisor reasons from is a provider: a name, a
``fetch()``, and a promise not to take the slot down with it.

**Why a protocol rather than one big snapshot function.** The advisor's
context grew from "the market picture" to market + budget + history + picks +
positions, each with a different source (Alpaca, DynamoDB, yfinance), a
different cost, and a different consequence when it is missing. Folding them
into one function makes every failure the same failure — and the one that
matters is not like the others: a missing VIX is a smaller picture, a missing
*budget* means we do not know how much we are allowed to spend.

Two rules, both learned the expensive way:

  * **A provider degrades, it does not raise.** ``assemble()`` catches per
    provider and records the miss in ``partial``, so the model is told what it
    could not see instead of silently reasoning from a hole. That is the
    behaviour ``market_context.snapshot()`` already had; this generalises it.
  * **``required`` inverts that for capacity.** A provider marked required
    propagates its failure, because some questions have no safe default.
    ``budget`` is the only one: "how much may I spend" answered by a shrug
    must stop the slot, not proceed on optimism.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol, runtime_checkable

log = logging.getLogger("raanu.ai.context")


class ProviderError(RuntimeError):
    """A required provider could not answer. The slot must not trade."""


@runtime_checkable
class Provider(Protocol):
    """One answerable question about the world.

    ``name`` is the key the payload lands under, so it is also what the prompt
    and the tool definitions refer to. Keep it short and stable — renaming one
    silently changes what the model is looking at.
    """

    name: str
    required: bool

    async def fetch(self) -> dict | None:
        """The data, or None when there is simply nothing to report.

        Async because every real provider does blocking I/O — Alpaca,
        DynamoDB, yfinance. A sync one wraps its body in ``asyncio.to_thread``
        rather than blocking the loop the exit monitor also runs on.
        """
        ...


async def assemble(providers: list[Provider]) -> dict:
    """Run every provider CONCURRENTLY, tolerate the optional ones, surface
    what is missing.

    Returns ``{name: data, ..., "partial": [names that could not answer]}``.
    ``partial`` is deliberately the same key ``market_context`` already used,
    because the prompt already tells the model to weigh its confidence by it.

    Concurrent because these are independent network reads and the slot is on
    a clock: run serially they add their latencies together, and that time
    comes straight out of the advisor's wall-clock budget.
    """
    results = await asyncio.gather(
        *(p.fetch() for p in providers), return_exceptions=True)

    out: dict = {"partial": []}
    for provider, data in zip(providers, results, strict=True):
        if isinstance(data, BaseException):
            if getattr(provider, "required", False):
                # No safe default. Let it reach the advisor's one except and
                # become "no orders this slot".
                raise ProviderError(f"{provider.name}: {data}") from data
            log.warning(f"[context] {provider.name} unavailable: {data}")
            out["partial"].append(provider.name)
            continue
        if data is None:
            out["partial"].append(provider.name)
            continue
        out[provider.name] = data
    return out
