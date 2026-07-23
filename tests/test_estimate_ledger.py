"""The estimator and the ledger, working as one mechanism.

The two halves are only worth anything together: the ledger enforces a ceiling
on numbers the estimator produces, and the estimator's numbers are only bounds
because the ledger refuses anything that does not fit. These tests drive the
loop the service will actually run.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from ci_triage.budget import BudgetExceeded
from ci_triage.estimate import budget_input_chars, plan_call, plan_call_for_text
from ci_triage.money import dollars_to_micros
from ci_triage.pricing import ModelPrice

PRICE = ModelPrice(
    model="test-model",
    provider="test",
    input_per_mtok=Decimal("1.00"),
    output_per_mtok=Decimal("5.00"),
    chars_per_token=Decimal("1.18"),
    source="https://example.invalid",
)

CEILING = dollars_to_micros("0.04")


def test_plan_reserve_commit_is_the_happy_path(ledger):
    ledger.open_run("run-1", CEILING)

    plan = plan_call(PRICE, input_tokens=20_000, max_output_tokens=1_000)
    reservation = ledger.reserve(
        "run-1", plan.worst_case_micros, purpose="diagnose"
    )

    # The model came back well under its cap, as it usually will.
    actual = PRICE.input_micros(20_000) + PRICE.output_micros(180)
    ledger.commit(reservation, actual)

    spend = ledger.spend("run-1")
    assert spend.spent_micros == actual
    assert spend.reserved_micros == 0
    assert spend.overrun_micros == 0
    # The unused part of the hold went back to the run, so the next call can use it.
    assert spend.remaining_micros == CEILING - actual


def test_an_oversized_log_is_refused_before_the_provider_is_called(ledger):
    """The refusal has to happen at reserve time, not after the bill arrives."""
    ledger.open_run("run-2", CEILING)

    huge = "#" * 5_000_000  # a failing matrix job's log
    plan = plan_call_for_text(PRICE, huge, max_output_tokens=1_000)
    assert plan.worst_case_micros > CEILING

    with pytest.raises(BudgetExceeded):
        ledger.reserve("run-2", plan.worst_case_micros, purpose="diagnose")

    assert ledger.spend("run-2").spent_micros == 0


def test_budgeting_the_log_to_the_remaining_ceiling_turns_a_refusal_into_a_run(ledger):
    """The inversion, end to end.

    Instead of pricing the whole log and being refused, ask what fits, truncate
    to that, and the run proceeds -- with a shorter log rather than no answer.
    """
    ledger.open_run("run-3", CEILING)
    # A first call already consumed part of the ceiling.
    first = ledger.reserve("run-3", dollars_to_micros("0.01"))
    ledger.commit(first, dollars_to_micros("0.01"))

    remaining = ledger.spend("run-3").remaining_micros
    budgeted_chars = budget_input_chars(
        PRICE, budget_micros=remaining, max_output_tokens=1_000
    )

    huge = "#" * 5_000_000
    truncated = huge[:budgeted_chars]
    assert 0 < len(truncated) < len(huge)

    plan = plan_call_for_text(PRICE, truncated, max_output_tokens=1_000)
    assert plan.worst_case_micros <= remaining

    reservation = ledger.reserve("run-3", plan.worst_case_micros)
    ledger.commit(reservation, plan.worst_case_micros)  # the pessimal case
    assert ledger.spend("run-3").remaining_micros >= 0


def test_retries_draw_on_the_same_run_ceiling_until_it_is_gone(ledger):
    """A retry storm is bounded by the run, not by the number of attempts.

    This is the property an AI gateway's per-key, per-window budget cannot give
    you: it cannot tell attempt 4 of this run from attempt 1 of the next one.
    """
    ledger.open_run("run-4", CEILING)
    plan = plan_call(PRICE, input_tokens=8_000, max_output_tokens=1_000)

    attempts = 0
    while True:
        try:
            reservation = ledger.reserve(
                "run-4", plan.worst_case_micros, attempt=attempts + 1
            )
        except BudgetExceeded:
            break
        attempts += 1
        ledger.commit(reservation, plan.worst_case_micros)
        assert attempts < 100  # the loop must terminate by budget, not by luck

    assert attempts == 3  # 13_000 micros a go, into 40_000
    spend = ledger.spend("run-4")
    assert spend.spent_micros <= CEILING
    assert spend.remaining_micros < plan.worst_case_micros


def test_an_underestimate_is_recorded_as_an_overrun_not_hidden(ledger):
    """If the tokeniser proxy is wrong, it must be visible on the run."""
    ledger.open_run("run-5", CEILING)
    plan = plan_call(PRICE, input_tokens=1_000, max_output_tokens=100)
    reservation = ledger.reserve("run-5", plan.worst_case_micros)

    # The provider billed 30% more input tokens than our ratio predicted --
    # exactly the failure mode of using one provider's tokeniser to price
    # another's.
    ledger.commit(reservation, int(plan.worst_case_micros * 1.3))

    assert ledger.spend("run-5").overrun_micros > 0
