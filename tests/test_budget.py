from __future__ import annotations

import pytest

from ci_triage.budget import BudgetExceeded, Ledger, UnknownRun


def test_open_run_is_idempotent(ledger: Ledger):
    assert ledger.open_run("r1", 10_000) is True
    assert ledger.open_run("r1", 10_000) is False


def test_reopening_a_run_does_not_reset_a_partly_spent_ceiling(ledger: Ledger):
    # The redelivery case: the same webhook arrives twice, the handler calls
    # open_run again, and the budget must not be handed back.
    ledger.open_run("r1", 10_000)
    res = ledger.reserve("r1", 6_000)
    ledger.commit(res, 6_000)

    ledger.open_run("r1", 10_000)

    assert ledger.spend("r1").spent_micros == 6_000
    with pytest.raises(BudgetExceeded):
        ledger.reserve("r1", 5_000)


def test_reserve_then_commit_records_actual_cost(ledger: Ledger):
    ledger.open_run("r1", 10_000)
    res = ledger.reserve("r1", 4_000, purpose="diagnose")

    held = ledger.spend("r1")
    assert held.reserved_micros == 4_000
    assert held.spent_micros == 0

    ledger.commit(res, 2_500)

    settled = ledger.spend("r1")
    assert settled.reserved_micros == 0
    assert settled.spent_micros == 2_500
    # The unused 1,500 came back rather than staying locked up.
    assert settled.remaining_micros == 7_500


def test_release_charges_nothing(ledger: Ledger):
    ledger.open_run("r1", 10_000)
    res = ledger.reserve("r1", 4_000)
    ledger.release(res)

    spend = ledger.spend("r1")
    assert spend.spent_micros == 0
    assert spend.reserved_micros == 0
    assert spend.remaining_micros == 10_000


def test_reservation_that_would_breach_the_ceiling_is_refused(ledger: Ledger):
    ledger.open_run("r1", 10_000)
    ledger.reserve("r1", 8_000)

    with pytest.raises(BudgetExceeded) as excinfo:
        ledger.reserve("r1", 3_000)

    assert excinfo.value.remaining == 2_000
    # Nothing partial was applied.
    assert ledger.spend("r1").reserved_micros == 8_000


def test_a_reservation_exactly_filling_the_ceiling_is_allowed(ledger: Ledger):
    ledger.open_run("r1", 10_000)
    ledger.reserve("r1", 10_000)
    assert ledger.spend("r1").remaining_micros == 0


def test_held_money_blocks_a_second_call_before_the_first_settles(ledger: Ledger):
    # The reason reservations exist at all: while a call is in flight its cost
    # is unknown, and the budget must already account for the worst case.
    ledger.open_run("r1", 10_000)
    ledger.reserve("r1", 7_000)
    with pytest.raises(BudgetExceeded):
        ledger.reserve("r1", 7_000)


def test_retries_share_one_ceiling_rather_than_getting_one_each(ledger: Ledger):
    # A retry storm must not multiply the bill. Five attempts at 3,000 against
    # a 10,000 ceiling: three fit, the rest are refused.
    ledger.open_run("r1", 10_000)
    accepted = 0
    for attempt in range(1, 6):
        try:
            res = ledger.reserve("r1", 3_000, attempt=attempt)
        except BudgetExceeded:
            continue
        ledger.commit(res, 3_000)
        accepted += 1

    assert accepted == 3
    assert ledger.spend("r1").spent_micros == 9_000


def test_overrun_is_recorded_rather_than_hidden(ledger: Ledger):
    # If a provider bills more than the worst case we estimated, the ledger
    # records the truth and flags it. Clamping would make the books balance
    # and the bug invisible.
    ledger.open_run("r1", 10_000)
    res = ledger.reserve("r1", 2_000)
    ledger.commit(res, 2_600)

    spend = ledger.spend("r1")
    assert spend.spent_micros == 2_600
    assert spend.overrun_micros == 600


def test_double_commit_does_not_double_charge(ledger: Ledger):
    ledger.open_run("r1", 10_000)
    res = ledger.reserve("r1", 4_000)
    ledger.commit(res, 3_000)
    ledger.commit(res, 3_000)

    assert ledger.spend("r1").spent_micros == 3_000


def test_release_after_commit_does_not_refund(ledger: Ledger):
    ledger.open_run("r1", 10_000)
    res = ledger.reserve("r1", 4_000)
    ledger.commit(res, 3_000)
    ledger.release(res)

    spend = ledger.spend("r1")
    assert spend.spent_micros == 3_000
    assert spend.reserved_micros == 0


def test_runs_have_independent_ceilings(ledger: Ledger):
    ledger.open_run("r1", 10_000)
    ledger.open_run("r2", 10_000)
    ledger.reserve("r1", 10_000)

    ledger.reserve("r2", 10_000)  # unaffected by r1 being full


def test_unknown_run_is_distinguished_from_no_budget_left(ledger: Ledger):
    with pytest.raises(UnknownRun):
        ledger.reserve("never-opened", 1)


def test_zero_and_negative_ceilings_are_rejected(ledger: Ledger):
    with pytest.raises(ValueError):
        ledger.open_run("r1", 0)
    with pytest.raises(ValueError):
        ledger.open_run("r1", -5)


def test_negative_amounts_are_rejected(ledger: Ledger):
    ledger.open_run("r1", 10_000)
    with pytest.raises(ValueError):
        ledger.reserve("r1", -1)
    res = ledger.reserve("r1", 100)
    with pytest.raises(ValueError):
        ledger.commit(res, -1)
