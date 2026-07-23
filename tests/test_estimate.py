from __future__ import annotations

from decimal import Decimal

import pytest

from ci_triage.estimate import (
    BudgetTooSmall,
    EstimateError,
    budget_input_chars,
    budget_input_tokens,
    plan_call,
    plan_call_for_text,
)
from ci_triage.money import dollars_to_micros
from ci_triage.pricing import ModelPrice
from ci_triage.tokens import DEFAULT_MESSAGE_OVERHEAD_TOKENS, chars_that_fit

PRICE = ModelPrice(
    model="test-model",
    provider="test",
    input_per_mtok=Decimal("1.00"),
    output_per_mtok=Decimal("5.00"),
    chars_per_token=Decimal("1.18"),
    source="https://example.invalid",
)

FREE_INPUT = ModelPrice(
    model="free-input",
    provider="test",
    input_per_mtok=Decimal("0"),
    output_per_mtok=Decimal("5.00"),
    chars_per_token=Decimal("1.18"),
    source="https://example.invalid",
)


# ------------------------------------------------------------------- planning


def test_a_plan_carries_the_parameters_that_make_its_cost_true():
    plan = plan_call(PRICE, input_tokens=10_000, max_output_tokens=1_000)
    assert plan.max_input_tokens == 10_000
    assert plan.max_output_tokens == 1_000
    assert plan.input_micros == 10_000  # 10k tokens at $1/MTok
    assert plan.output_micros == 5_000  # 1k tokens at $5/MTok
    assert plan.worst_case_micros == 15_000  # $0.015


def test_output_must_be_capped():
    """An uncapped output has no worst case, so there is nothing to reserve."""
    with pytest.raises(ValueError, match="no worst case"):
        plan_call(PRICE, input_tokens=100, max_output_tokens=0)


def test_negative_input_is_rejected():
    with pytest.raises(ValueError):
        plan_call(PRICE, input_tokens=-1, max_output_tokens=100)


def test_planning_from_text_bounds_the_token_count_upward():
    prompt = "x" * 1180
    plan = plan_call_for_text(PRICE, prompt, max_output_tokens=100)
    # 1180 chars / 1.18 = 1000 tokens, plus message framing.
    assert plan.max_input_tokens == 1000 + DEFAULT_MESSAGE_OVERHEAD_TOKENS


def test_an_empty_prompt_still_costs_its_framing():
    plan = plan_call_for_text(PRICE, "", max_output_tokens=100)
    assert plan.max_input_tokens == DEFAULT_MESSAGE_OVERHEAD_TOKENS


def test_plan_renders_for_a_log_line():
    plan = plan_call(PRICE, input_tokens=10_000, max_output_tokens=1_000)
    assert "test-model" in str(plan)
    assert "$0.015000" in str(plan)


# --------------------------------------------------------------- the inversion


def test_budgeting_input_leaves_room_for_the_output():
    budget = dollars_to_micros("0.05")  # 50_000 micros
    tokens = budget_input_tokens(PRICE, budget_micros=budget, max_output_tokens=2_000)
    # Output: 2000 * $5/MTok = 10_000 micros. Remaining 40_000 at $1/MTok = 40_000 tokens.
    assert tokens == 40_000


def test_a_budgeted_plan_actually_fits_the_budget():
    """The property the whole module exists for."""
    budget = dollars_to_micros("0.04")
    for max_out in (1, 50, 500, 2_000):
        tokens = budget_input_tokens(
            PRICE, budget_micros=budget, max_output_tokens=max_out
        )
        plan = plan_call(PRICE, input_tokens=tokens, max_output_tokens=max_out)
        assert plan.worst_case_micros <= budget
        # ...and it is the *largest* input that fits: one more token breaks it.
        bigger = plan_call(PRICE, input_tokens=tokens + 1, max_output_tokens=max_out)
        assert bigger.worst_case_micros > budget


def test_budgeted_characters_round_trip_back_under_the_budget():
    """Truncate to this many characters and the plan still fits.

    This is the chain the truncation step depends on: micros -> tokens -> chars
    -> (truncate) -> tokens -> micros. Every step rounds in the safe direction,
    so the round trip can lose room but must never gain any.
    """
    budget = dollars_to_micros("0.04")
    chars = budget_input_chars(PRICE, budget_micros=budget, max_output_tokens=500)
    log = "#" * chars
    plan = plan_call_for_text(PRICE, log, max_output_tokens=500)
    assert plan.worst_case_micros <= budget


@pytest.mark.parametrize("budget_dollars", ["0.001", "0.01", "0.04", "0.5", "2.00"])
@pytest.mark.parametrize("max_out", [1, 17, 256, 4096])
def test_round_trip_holds_across_the_grid(budget_dollars, max_out):
    budget = dollars_to_micros(budget_dollars)
    try:
        chars = budget_input_chars(
            PRICE, budget_micros=budget, max_output_tokens=max_out
        )
    except BudgetTooSmall:
        return  # covered by its own test
    plan = plan_call_for_text(PRICE, "#" * chars, max_output_tokens=max_out)
    assert plan.worst_case_micros <= budget


def test_a_budget_that_cannot_cover_the_output_is_terminal():
    """Truncating the input cannot fix a shortfall on the output side."""
    budget = dollars_to_micros("0.001")  # 1_000 micros
    with pytest.raises(BudgetTooSmall) as exc:
        budget_input_tokens(PRICE, budget_micros=budget, max_output_tokens=10_000)
    assert "Truncating the input cannot fix this" in str(exc.value)


def test_exactly_enough_for_the_output_leaves_zero_input():
    budget = PRICE.output_micros(1_000)
    assert budget_input_tokens(PRICE, budget_micros=budget, max_output_tokens=1_000) == 0


def test_zero_budget_with_any_output_is_terminal():
    with pytest.raises(BudgetTooSmall):
        budget_input_tokens(PRICE, budget_micros=0, max_output_tokens=1)


def test_a_free_input_model_says_so_rather_than_returning_a_huge_number():
    with pytest.raises(EstimateError, match="context window"):
        budget_input_tokens(FREE_INPUT, budget_micros=1_000_000, max_output_tokens=10)


def test_overhead_can_exhaust_a_tiny_character_budget():
    """Never returns a negative length; zero characters is the honest answer."""
    assert chars_that_fit(3, Decimal("1.18"), overhead_tokens=8) == 0
