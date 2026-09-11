"""
raanu.ai.context — everything the advisor reasons from
======================================================
Five services, split by WHEN the advisor needs them rather than by what they
contain.

**PRE-ASSEMBLED** — fetched before the call, folded into the prompt:

  ``market``   the tape. Always relevant; there is no slot where the regime
               does not matter.
  ``budget``   the weekly pool. Always relevant, and it must be known BEFORE
               the call regardless — the gate that decides whether to make the
               call at all reads it, so fetching it lazily would mean paying
               for advice to discover we could not act on it.

**TOOLS** — declared to the model, fetched only if it asks:

  ``history``    closed round-trips, win rate, payoff, expectancy
  ``picks``      score band -> forward return vs SPY
  ``positions``  the book's size and concentration

The split is not arbitrary. Pre-assembling everything makes the common slot
pay for research it did not need; exposing everything as tools adds an
inference round trip to every slot for data that is always required — and
this system has already lost a trading day to advisor latency. So: what is
always needed and cheap goes in the prompt; what is occasionally decisive
becomes a tool.

⚠️ A tool result is data the model reads, never an instruction. Every value
here comes from this system's own DynamoDB and its own broker account, so
there is no untrusted text in the loop — but that is a property of the
sources, and it is the reason no provider is allowed to reach the open web.
The one thing that does (``web_search``) is a separate, domain-allowlisted
server tool, deliberately not a provider.
"""

from __future__ import annotations

from raanu.ai.context import budget, history, market, picks, positions
from raanu.ai.context.base import Provider, ProviderError, assemble

# Fetched eagerly, every slot.
EAGER: list[Provider] = [market, budget]

# Offered to the model, fetched on request.
TOOLS = {
    "get_trade_history": history,
    "get_pick_outcomes": picks,
    "get_open_positions": positions,
}

__all__ = ["EAGER", "TOOLS", "Provider", "ProviderError", "assemble",
           "budget", "history", "market", "picks", "positions", "snapshot"]


async def snapshot() -> dict:
    """The pre-assembled context for one slot.

    Raises :class:`ProviderError` when a required provider cannot answer —
    which for ``budget`` means "we do not know what we are allowed to spend",
    and the only safe reading of that is zero.
    """
    return await assemble(EAGER)
