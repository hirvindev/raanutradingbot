"""The float<->Decimal boundary.

DynamoDB rejects Python floats and this codebase is float-saturated, which is
the entire reason state used to be an escaped JSON string. Storing real JSON
means converting, so these tests pin the conversion — including the two places
it is deliberately lossy.

The old suite had no float round-trip through either backend at all (every
value in tests/test_state.py was an int or a string), which is why this shape
was never stress-tested.
"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from raanu.state.coerce import from_dynamo, to_dynamo


def roundtrip(value):
    return from_dynamo(to_dynamo(value))


class TestFloats:
    def test_a_plain_float_survives(self):
        assert roundtrip({"price": 90.27}) == {"price": 90.27}
        assert isinstance(roundtrip({"p": 90.27})["p"], float)

    def test_a_float32_artefact_survives_exactly(self):
        """A real value from the live picks log. yfinance hands back float32
        artefacts like this and they must not be rounded on the way through."""
        assert roundtrip({"p": 155.58999633789062})["p"] == 155.58999633789062

    def test_decimal_uses_str_not_the_binary_expansion(self):
        """Decimal(0.1) is 0.1000000000000000055511151231257827…, which trips
        boto3's Inexact/Rounded context. Decimal(str(0.1)) is exactly '0.1'."""
        assert to_dynamo(0.1) == Decimal("0.1")

    def test_negative_and_tiny_floats(self):
        for v in (-2.05, -0.0001, 1e-9, 1234567.891):
            assert roundtrip({"v": v})["v"] == v


class TestLossyByDesign:
    def test_nan_becomes_none(self):
        """DynamoDB rejects NaN outright. The previous string encoding let it
        through only by accident — json.dumps emits a bare NaN, which is
        invalid JSON that json.loads happens to accept back. pandas produces
        NaN readily, so this path is real."""
        assert roundtrip({"v": float("nan")}) == {"v": None}

    def test_infinity_becomes_none(self):
        assert roundtrip({"v": float("inf")}) == {"v": None}
        assert roundtrip({"v": float("-inf")}) == {"v": None}

    def test_an_integral_float_comes_back_as_int(self):
        """DynamoDB's number type cannot tell 5000.0 from 5000. Asserted
        deliberately so it is a known contract, not a surprise: no code in
        raanu/ or handlers/ does isinstance(x, float), and Python arithmetic
        is unaffected."""
        assert roundtrip({"usd": 5000.0}) == {"usd": 5000}
        assert 5000 == 5000.0        # the reason it is safe


class TestOtherTypes:
    def test_bools_stay_bools_and_do_not_become_numbers(self):
        """bool is a subclass of int in Python — converting in the wrong order
        would silently collapse True into 1 and lose DynamoDB's boolean type."""
        out = roundtrip({"uptrend": True, "in_golden_pocket": False})
        assert out == {"uptrend": True, "in_golden_pocket": False}
        assert isinstance(out["uptrend"], bool)

    def test_none_strings_and_ints_are_untouched(self):
        payload = {"a": None, "b": "text", "c": 42, "d": ""}
        assert roundtrip(payload) == payload

    def test_nesting_is_walked_all_the_way_down(self):
        payload = {"fwd": {"d1": -2.05, "d5": None},
                   "reasons": ["a", "b"],
                   "legs": [{"px": 1.5}, {"px": 2.25}]}
        assert roundtrip(payload) == payload

    def test_empty_containers_survive(self):
        assert roundtrip({"fwd": {}, "reasons": []}) == {"fwd": {}, "reasons": []}


class TestAgainstRealBoto3:
    """The converter is only useful if boto3 actually accepts its output."""

    def test_boto3_accepts_converted_payloads_and_rejects_raw_floats(self):
        serializer = pytest.importorskip(
            "boto3.dynamodb.types", reason="boto3 not installed").TypeSerializer()

        with pytest.raises(TypeError):
            serializer.serialize({"price": 90.27})          # the problem

        payload = {"price": 90.27, "score": 78, "ok": True,
                   "fwd": {"d1": -2.05}, "reasons": ["x"], "nothing": None}
        serializer.serialize(to_dynamo(payload))            # the fix

    def test_a_full_boto3_roundtrip_is_lossless(self):
        types = pytest.importorskip("boto3.dynamodb.types", reason="boto3 not installed")
        payload = {"price": 90.27, "score": 78, "uptrend": True,
                   "fwd": {"d1": -2.05, "d5": 3.4}, "reasons": ["a"]}
        wire = types.TypeSerializer().serialize(to_dynamo(payload))
        assert from_dynamo(types.TypeDeserializer().deserialize(wire)) == payload

    def test_nan_would_otherwise_be_rejected_by_boto3(self):
        types = pytest.importorskip("boto3.dynamodb.types", reason="boto3 not installed")
        with pytest.raises(TypeError):
            types.TypeSerializer().serialize({"v": Decimal(str(math.nan))})
        types.TypeSerializer().serialize(to_dynamo({"v": math.nan}))   # coerced
