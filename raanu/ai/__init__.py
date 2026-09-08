"""
raanu.ai — the LLM advisory layer
==================================
The quant engines decide *what could be bought*. This package decides, once
per execution slot, whether today is worth trading, which of those candidates
to take, how to split the money, and how to manage each position out.

It can never invent a candidate the quant did not surface — that constraint is
enforced in :mod:`raanu.ai.schema`, not merely requested in the prompt.

Nothing here is imported at module scope by the trading path; ``schedule.py``
imports :func:`raanu.ai.advisor.review_slot` inside the function that uses it,
so a missing ``anthropic`` install cannot break a scan.
"""
