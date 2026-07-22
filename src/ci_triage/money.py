"""Money as integer micro-dollars.

Every amount in this project is an ``int`` count of micro-dollars (1e-6 USD).
Nothing anywhere holds a monetary value in a float.

This is not pedantry. A ceiling check is an inequality on a running total, and
floats make running totals non-associative: accumulate a few thousand
fractional-cent LLM charges and the total depends on the order they were added.
A budget that is enforced to within a rounding error is not enforced. The same
reasoning is why payment systems store minor units as integers -- micro-dollars
rather than cents only because per-token prices are far below a cent.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

MICROS_PER_DOLLAR = 1_000_000


def dollars_to_micros(amount: str | Decimal) -> int:
    """Convert a dollar amount to micro-dollars, rounding *up*.

    Takes a string or ``Decimal`` rather than a float on purpose -- accepting a
    float here would reintroduce exactly the imprecision this module exists to
    avoid. Rounds up so that converting a price can never understate a cost.
    """
    if isinstance(amount, float):
        raise TypeError(
            "pass prices as str or Decimal, not float -- float(0.001) is not 0.001 "
            "and the error compounds across a run's charges"
        )
    if isinstance(amount, str):
        amount = Decimal(amount)
    elif not isinstance(amount, Decimal):
        raise TypeError(f"expected str or Decimal, got {type(amount).__name__}")
    return int((amount * MICROS_PER_DOLLAR).to_integral_value(rounding=ROUND_CEILING))


def micros_to_dollars(micros: int) -> Decimal:
    """Convert micro-dollars back to a ``Decimal`` dollar amount, for display."""
    return Decimal(micros) / MICROS_PER_DOLLAR


def format_micros(micros: int) -> str:
    """Render micro-dollars for humans, e.g. ``$0.004231``."""
    return f"${micros_to_dollars(micros):.6f}"
