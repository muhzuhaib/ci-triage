"""Worst-case cost estimation.

The ledger reserves the worst case before a call is made. This module is what
computes that number -- and the whole design rests on one observation:

    A cost estimate is not a bound unless the caller enforces both of its
    terms. A number that merely predicts what a call will cost is a guess, and
    reserving a guess turns the ceiling back into an average.

So :func:`plan_call` does not return a cost. It returns a :class:`CallPlan`
carrying the cost *and the request parameters that make it true*: the input was
truncated to ``max_input_tokens``, and ``max_output_tokens`` goes on the request
as ``max_tokens``. Send the plan's parameters and the bill cannot exceed
``worst_case_micros``. Send different ones and the number means nothing, which
is why they travel together instead of the caller being trusted to remember.

The inversion that makes this cheap
-----------------------------------
The obvious flow is: take the log, estimate its cost, hope it fits. That flow
forces the estimate to be *accurate*, because over-estimating means refusing a
run that would have fit -- and accuracy is exactly what a character heuristic
cannot deliver (see :mod:`ci_triage.tokens`; the honest worst-case ratio for CI
log text is 4.6x more pessimistic than the folk constant).

:func:`budget_input_tokens` runs it the other way: given what is left under the
ceiling, how much log can we afford? Now the pessimism costs nothing but a
slightly shorter log, and the run is never refused for a bound that was merely
cautious. The service does not predict the size of its input; it chooses it.

What is still not guaranteed
----------------------------
That the provider's tokeniser agrees with ours, and that the provider honours
``max_tokens``. Neither is knowable from here, so neither is claimed. Both show
up as a non-zero ``overrun_micros`` on the run, which is why the ledger records
overruns instead of clamping them: this module's assumptions are wrong loudly
or not at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from .money import format_micros
from .pricing import ModelPrice
from .tokens import DEFAULT_MESSAGE_OVERHEAD_TOKENS, chars_that_fit, upper_bound_tokens


class EstimateError(Exception):
    """Base class for estimation failures."""


class BudgetTooSmall(EstimateError):
    """The budget cannot cover even the output allowance.

    Terminal in the same way :class:`~ci_triage.budget.BudgetExceeded` is:
    truncating the input further will not help, because the shortfall is on the
    output side. The caller must lower ``max_output_tokens``, pick a cheaper
    model, or dead-letter the run.
    """


@dataclass(frozen=True)
class CallPlan:
    """A costed call, and the request parameters that make the cost a bound.

    ``worst_case_micros`` is only an upper bound on the bill if the request is
    issued with *these* limits. Reserve it, send the call with
    ``max_tokens=max_output_tokens`` and an input no longer than
    ``max_input_tokens``, and commit the actual afterwards.
    """

    model: str
    max_input_tokens: int
    max_output_tokens: int
    input_micros: int
    output_micros: int

    @property
    def worst_case_micros(self) -> int:
        return self.input_micros + self.output_micros

    def __str__(self) -> str:
        return (
            f"{self.model}: <={self.max_input_tokens} in + {self.max_output_tokens} out "
            f"= {format_micros(self.worst_case_micros)} worst case"
        )


def plan_call(
    price: ModelPrice,
    *,
    input_tokens: int,
    max_output_tokens: int,
) -> CallPlan:
    """Cost a call whose input length is already known in tokens."""
    if input_tokens < 0:
        raise ValueError("input_tokens cannot be negative")
    if max_output_tokens <= 0:
        raise ValueError(
            "max_output_tokens must be positive -- an unbounded output has no worst case, "
            "which is the whole problem this module exists to solve"
        )
    return CallPlan(
        model=price.model,
        max_input_tokens=input_tokens,
        max_output_tokens=max_output_tokens,
        input_micros=price.input_micros(input_tokens),
        output_micros=price.output_micros(max_output_tokens),
    )


def plan_call_for_text(
    price: ModelPrice,
    prompt: str,
    *,
    max_output_tokens: int,
    overhead_tokens: int = DEFAULT_MESSAGE_OVERHEAD_TOKENS,
) -> CallPlan:
    """Cost a call from the prompt text, bounding its token count upward."""
    return plan_call(
        price,
        input_tokens=upper_bound_tokens(
            prompt, price.chars_per_token, overhead_tokens=overhead_tokens
        ),
        max_output_tokens=max_output_tokens,
    )


def budget_input_tokens(
    price: ModelPrice,
    *,
    budget_micros: int,
    max_output_tokens: int,
) -> int:
    """How many input tokens fit in ``budget_micros`` once output is paid for.

    Output is reserved first because it is the part that cannot be traded away:
    a diagnosis truncated to nothing is not a cheaper diagnosis, it is a failed
    run. Input is the elastic term, so it absorbs whatever is left.
    """
    if budget_micros < 0:
        raise ValueError("budget cannot be negative")
    if max_output_tokens <= 0:
        raise ValueError("max_output_tokens must be positive")

    output_micros = price.output_micros(max_output_tokens)
    if output_micros > budget_micros:
        raise BudgetTooSmall(
            f"{price.model}: {max_output_tokens} output tokens cost "
            f"{format_micros(output_micros)}, but only {format_micros(budget_micros)} "
            f"is available. Truncating the input cannot fix this"
        )

    remaining = budget_micros - output_micros
    if price.input_per_mtok == 0:
        # A free-input model (self-hosted, or a promotional rate). No arithmetic
        # bound exists, so say so rather than returning a huge meaningless number.
        raise EstimateError(
            f"{price.model} has a zero input price, so the input is not bounded by cost. "
            f"Bound it by the model's context window instead"
        )

    # input_micros(t) = ceil(t * price). For an integer budget B, ceil(x) <= B
    # exactly when x <= B, so the largest affordable t is floor(B / price).
    return int(
        (Decimal(remaining) / price.input_per_mtok).to_integral_value(rounding=ROUND_FLOOR)
    )


def budget_input_chars(
    price: ModelPrice,
    *,
    budget_micros: int,
    max_output_tokens: int,
    overhead_tokens: int = DEFAULT_MESSAGE_OVERHEAD_TOKENS,
) -> int:
    """The same budget expressed in characters, for the log truncation step.

    Truncating a log to this many characters and pricing the result with
    :func:`plan_call_for_text` yields a plan that fits inside ``budget_micros``.
    """
    return chars_that_fit(
        budget_input_tokens(
            price, budget_micros=budget_micros, max_output_tokens=max_output_tokens
        ),
        price.chars_per_token,
        overhead_tokens=overhead_tokens,
    )
