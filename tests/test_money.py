from __future__ import annotations

from decimal import Decimal

import pytest

from ci_triage.money import dollars_to_micros, format_micros, micros_to_dollars


def test_whole_dollars_round_trip():
    assert dollars_to_micros("1.00") == 1_000_000
    assert micros_to_dollars(1_000_000) == Decimal("1")


def test_sub_cent_prices_survive():
    # $0.000003 per token is a realistic per-token price and is where a
    # cents-based integer representation would collapse to zero.
    assert dollars_to_micros("0.000003") == 3


def test_conversion_rounds_up_so_a_cost_is_never_understated():
    # 0.0000005 dollars is half a micro-dollar. Rounding down would let a
    # long tail of tiny charges accumulate as free spend.
    assert dollars_to_micros("0.0000005") == 1


def test_floats_are_rejected():
    with pytest.raises((TypeError, ValueError)):
        dollars_to_micros(0.001)  # type: ignore[arg-type]


def test_accumulation_is_exact():
    # The property floats break: adding a fractional-cent charge many times
    # must give a total that does not depend on ordering or accumulate drift.
    unit = dollars_to_micros("0.000003")
    assert sum(unit for _ in range(1_000_000)) == 3_000_000


def test_format_is_readable():
    assert format_micros(4_231) == "$0.004231"
