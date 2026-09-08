"""
raanu.state.coerce — float <-> Decimal at the DynamoDB boundary
================================================================
DynamoDB has one number type and boto3 maps it to ``Decimal``. It **rejects
Python floats outright**::

    >>> TypeSerializer().serialize({"price": 90.27})
    TypeError: Float types are not supported. Use Decimal types instead.

Every payload in this project is float-saturated — prices, ATR, %B, momentum,
P&L — and there is no ``Decimal`` anywhere outside this module. That is the
whole reason state used to be stored as an escaped JSON string: a string
sidesteps the number type entirely.

Storing real JSON instead means converting, and **where** the conversion
happens is the load-bearing decision. It happens here, at the state boundary,
so callers hand in plain floats and get plain floats back. Nothing outside
``raanu.state`` ever sees a ``Decimal``.

That matters concretely. ``raanu/trading/exits.py:390`` does::

    atr_pct = (atr / entry * 100) if atr else None

``atr`` comes straight out of stored state and is never cast; ``entry`` is a
float from Alpaca. ``Decimal / float`` raises ``TypeError``, and that line sits
in the live stop-loss path inside a ``try`` that would swallow it and skip the
exit check. Converting at the boundary means that line never changes and never
sees a Decimal.

Two conversions are deliberately lossy, both verified against real production
data (see tests/test_state_coerce.py):

* **NaN and Infinity become None.** DynamoDB rejects them
  (``TypeError: Infinity and NaN not supported``). The previous string encoding
  let them through only by accident: ``json.dumps`` emits a bare ``NaN``, which
  is invalid JSON that ``json.loads`` happens to accept back. pandas produces
  NaN readily, so this path is real — making it explicit turns silent bad data
  into an explicit null.
* **Integral floats come back as int** (``5000.0`` -> ``5000``). DynamoDB's
  number type cannot tell them apart. Verified harmless: there is no
  ``isinstance(x, float)`` or ``isinstance(x, int)`` check anywhere in
  ``raanu/`` or ``handlers/``, Python arithmetic is unaffected, and the
  dashboard formats numbers through ``Number(n).toFixed(2)``.
"""

from __future__ import annotations

import math
from decimal import Decimal

__all__ = ["to_dynamo", "from_dynamo"]


def to_dynamo(obj):
    """Python -> DynamoDB-safe. Floats become Decimal; NaN/Inf become None."""
    # bool before int: bool IS an int in Python, and DynamoDB has a real
    # boolean type we would otherwise collapse into 0/1.
    if obj is None or isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        # Decimal(str(f)), never Decimal(f): the latter expands 0.1 to its full
        # 55-digit binary value, which trips boto3's Inexact/Rounded context.
        return Decimal(str(obj))
    if isinstance(obj, int):
        return obj
    if isinstance(obj, dict):
        return {k: to_dynamo(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_dynamo(v) for v in obj]
    return obj


def from_dynamo(obj):
    """DynamoDB -> Python. Decimal becomes int when integral, else float."""
    if isinstance(obj, bool) or obj is None:
        return obj
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, dict):
        return {k: from_dynamo(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [from_dynamo(v) for v in obj]
    return obj
