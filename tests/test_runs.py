"""The state machine's transitions, its schedule, and its refusals.

Every test here fixes the clock. Backoff and lease expiry are the two things
most easily tested by sleeping, and sleeping would make the suite both slow and
flaky while proving less: passing ``now=`` asserts the *exact* moment a job
becomes runnable rather than that it eventually does.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ci_triage.budget import BudgetExceeded
from ci_triage.idempotency import IdempotencyStore
from ci_triage.runs import (
    DEAD_LETTER,
    DEAD_LETTERED,
    LEASE_LOST,
    PENDING,
    RECORDED,
    RETRY_SCHEDULED,
    RUNNING,
    SUCCEEDED,
    Backoff,
    JobStore,
    NotDeadLettered,
    TerminalError,
    TriageJob,
    UnknownJob,
    default_is_retryable,
    no_jitter,
    process_next,
)

T0 = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)

#: The exact schedule, no jitter: 2s, 4s, 8s, capped at 10s.
EXACT = Backoff(base_seconds=2.0, cap_seconds=10.0, jitter=no_jitter)


@pytest.fixture()
def store(engine):
    return JobStore(engine, backoff=EXACT, lease_seconds=60.0, max_attempts=3)


def _enqueue(store: JobStore, key: str = "workflow_run:acme/app:1:1:completed") -> TriageJob:
    job = store.enqueue(key, "acme/app#1.1", now=T0)
    assert job is not None
    return job


# --------------------------------------------------------------------- enqueue


def test_a_job_starts_pending_and_due_immediately(store):
    job = _enqueue(store)
    assert job.state == PENDING
    assert job.attempt == 0
    assert job.next_attempt_at == T0
    assert job.attempts_remaining == 3


def test_a_second_enqueue_of_one_key_creates_nothing(store):
    first = _enqueue(store)
    assert store.enqueue(first.idempotency_key, first.run_id, now=T0) is None
    assert store.by_key(first.idempotency_key).id == first.id


def test_max_attempts_must_allow_at_least_one_attempt(store):
    with pytest.raises(ValueError):
        store.enqueue("k", "r", max_attempts=0)


# ----------------------------------------------------------------------- claim


def test_claiming_takes_the_lease_and_counts_the_attempt(store):
    _enqueue(store)
    job = store.claim_next("worker-1", now=T0)

    assert job is not None
    assert job.state == RUNNING
    assert job.attempt == 1
    assert job.worker == "worker-1"
    assert job.lease_expires_at == T0 + timedelta(seconds=60)


def test_a_running_job_is_not_claimable_while_its_lease_holds(store):
    _enqueue(store)
    store.claim_next("worker-1", now=T0)

    # One second before the lease expires there is nothing to claim...
    assert store.claim_next("worker-2", now=T0 + timedelta(seconds=59)) is None
    # ...and the moment it does, the job is up for grabs again.
    reclaimed = store.claim_next("worker-2", now=T0 + timedelta(seconds=60))
    assert reclaimed is not None
    assert reclaimed.worker == "worker-2"
    assert reclaimed.attempt == 2, "a reclaim is an attempt; a crash loop must end"


def test_a_scheduled_retry_is_not_claimable_before_it_is_due(store):
    _enqueue(store)
    job = store.claim_next("worker-1", now=T0)
    assert store.fail(job, "provider 503", now=T0) == RETRY_SCHEDULED

    assert store.claim_next("worker-1", now=T0 + timedelta(seconds=1)) is None
    assert store.claim_next("worker-1", now=T0 + timedelta(seconds=2)) is not None


def test_an_empty_queue_claims_nothing(store):
    assert store.claim_next("worker-1", now=T0) is None


# ---------------------------------------------------------- the fencing token


def test_a_worker_whose_lease_was_stolen_cannot_write_its_result(store):
    """The lease is not a mutex, so the write has to re-check ownership.

    A paused or partitioned worker is indistinguishable from a dead one, and it
    can wake up after its job has been handed on. If ``succeed`` trusted the job
    object it was given, that late write would overwrite the outcome of the
    attempt that replaced it -- and, with the idempotency key marked completed,
    the wrong result would be replayed to every redelivery afterwards.
    """
    _enqueue(store)
    slow = store.claim_next("worker-1", now=T0)
    thief = store.claim_next("worker-2", now=T0 + timedelta(seconds=60))
    assert thief is not None and thief.attempt == 2

    # worker-1 finally finishes, holding a stale (worker, attempt) pair.
    assert store.succeed(slow, "stale diagnosis", now=T0 + timedelta(seconds=61)) == LEASE_LOST
    assert store.fail(slow, "stale failure", now=T0 + timedelta(seconds=61)) == LEASE_LOST

    current = store.get(slow.id)
    assert current.state == RUNNING, "the stale worker must not settle the job"
    assert current.worker == "worker-2"
    assert current.result is None

    # And the live attempt settles it normally.
    assert store.succeed(thief, "real diagnosis", now=T0 + timedelta(seconds=62)) == RECORDED
    assert store.get(slow.id).result == "real diagnosis"


# --------------------------------------------------------- retries and backoff


def test_the_backoff_schedule_doubles_then_caps():
    assert [EXACT.delay_for(n) for n in (1, 2, 3, 4, 5)] == [2.0, 4.0, 8.0, 10.0, 10.0]


def test_full_jitter_stays_inside_the_interval_it_spreads():
    """Full jitter must never exceed the delay it randomises, or the cap is a lie."""
    jittered = Backoff(base_seconds=4.0, cap_seconds=8.0)
    for attempt in (1, 2, 3, 10):
        exact = Backoff(
            base_seconds=4.0, cap_seconds=8.0, jitter=no_jitter
        ).delay_for(attempt)
        for _ in range(50):
            assert 0.0 <= jittered.delay_for(attempt) <= exact


def test_attempt_is_one_based_so_the_first_retry_waits_the_base_delay():
    with pytest.raises(ValueError):
        EXACT.delay_for(0)


def test_a_retry_is_scheduled_at_the_backoff_delay(store):
    _enqueue(store)
    job = store.claim_next("worker-1", now=T0)
    store.fail(job, "provider 503", now=T0)

    after = store.get(job.id)
    assert after.state == PENDING
    assert after.worker is None and after.lease_expires_at is None
    assert after.next_attempt_at == T0 + timedelta(seconds=2)
    assert after.last_error == "provider 503"
    assert after.attempt == 1, "the failed attempt is not un-counted"


def test_the_delay_grows_with_the_attempt_not_the_wall_clock(store):
    """Backoff is a function of how many times this job has failed, only."""
    _enqueue(store)
    at = T0
    for expected in (2, 4):
        job = store.claim_next("worker-1", now=at)
        store.fail(job, "provider 503", now=at)
        assert store.get(job.id).next_attempt_at == at + timedelta(seconds=expected)
        at = at + timedelta(seconds=expected)


# ------------------------------------------------------------- dead-lettering


def test_the_last_failure_buries_the_job_instead_of_scheduling_a_fourth_attempt(store):
    _enqueue(store)
    at = T0
    for _ in range(2):
        job = store.claim_next("worker-1", now=at)
        assert store.fail(job, "provider 503", now=at) == RETRY_SCHEDULED
        at = store.get(job.id).next_attempt_at

    third = store.claim_next("worker-1", now=at)
    assert third.attempt == 3
    assert store.fail(third, "provider 503", now=at) == DEAD_LETTERED

    buried = store.get(third.id)
    assert buried.state == DEAD_LETTER
    assert "attempts exhausted after 3 of 3" in buried.last_error
    assert store.claim_next("worker-1", now=at + timedelta(days=1)) is None
    assert [j.id for j in store.dead_letter_queue()] == [third.id]


def test_a_terminal_failure_skips_the_retries_entirely(store):
    """A retry that cannot succeed is a delay plus a bill, not resilience."""
    _enqueue(store)
    job = store.claim_next("worker-1", now=T0)
    assert store.fail(job, "repository deleted", retryable=False, now=T0) == DEAD_LETTERED

    buried = store.get(job.id)
    assert buried.state == DEAD_LETTER
    assert buried.attempt == 1, "two attempts were left and were deliberately not used"
    assert buried.last_error == "repository deleted"


def test_budget_exhaustion_is_terminal_and_an_unknown_error_is_not():
    """The ceiling does not grow, so a call that did not fit will never fit."""
    assert default_is_retryable(BudgetExceeded("acme/app#1.1", 500, 100)) is False
    assert default_is_retryable(TerminalError("gone")) is False
    assert default_is_retryable(TimeoutError("provider slow")) is True
    assert default_is_retryable(RuntimeError("something new")) is True


def test_a_worker_that_dies_holding_the_final_attempt_is_buried_not_resurrected(store):
    """The call that would retry a job is the call that buries it.

    Otherwise an abandoned lease on an exhausted job is claimed forever, and the
    only thing standing between that and an infinite loop is a background reaper
    somebody has to remember to run.
    """
    job = store.enqueue("k", "acme/app#1.1", max_attempts=1, now=T0)
    claimed = store.claim_next("worker-1", now=T0)
    assert claimed.attempt == 1

    # worker-1 vanishes without reporting anything. The lease lapses.
    assert store.claim_next("worker-2", now=T0 + timedelta(seconds=60)) is None
    buried = store.get(job.id)
    assert buried.state == DEAD_LETTER
    assert "lease expired with no attempts remaining" in buried.last_error


# --------------------------------------------------------------------- replay


def test_replay_grants_attempts_without_erasing_the_ones_already_used(store):
    job = store.enqueue("k", "acme/app#1.1", max_attempts=1, now=T0)
    claimed = store.claim_next("worker-1", now=T0)
    store.fail(claimed, "provider 503", now=T0)
    assert store.get(job.id).state == DEAD_LETTER

    replayed = store.replay(job.id, now=T0 + timedelta(hours=1))
    assert replayed.state == PENDING
    assert replayed.attempt == 1, "history is not rewritten by a replay"
    assert replayed.max_attempts == 2, "one further attempt, granted explicitly"
    assert replayed.replays == 1
    assert replayed.next_attempt_at == T0 + timedelta(hours=1)

    again = store.claim_next("worker-1", now=T0 + timedelta(hours=1))
    assert again is not None and again.attempt == 2
    assert store.succeed(again, "diagnosis", now=T0 + timedelta(hours=1)) == RECORDED
    assert store.get(job.id).state == SUCCEEDED


def test_replaying_a_live_job_is_refused(store):
    """Replaying a running job would hand it a second worker."""
    job = _enqueue(store)
    with pytest.raises(NotDeadLettered):
        store.replay(job.id, now=T0)

    store.claim_next("worker-1", now=T0)
    with pytest.raises(NotDeadLettered):
        store.replay(job.id, now=T0)


def test_replaying_a_job_that_does_not_exist_says_so(store):
    with pytest.raises(UnknownJob):
        store.replay("nope", now=T0)


def test_a_replay_must_grant_at_least_one_attempt(store):
    with pytest.raises(ValueError):
        store.replay("nope", extra_attempts=0)


# ------------------------------------------------------ the worker-loop driver


def test_process_next_records_success_and_completes_the_idempotency_key(engine, store):
    """Closing the loop the receiver opened: a settled job settles its key.

    Until the job finishes, a redelivery is a duplicate *in flight* and nothing
    is scheduled. Once it finishes, the redelivery can be answered with the
    stored result instead of doing the work -- and paying for it -- again.
    """
    keys = IdempotencyStore(engine)
    key = "workflow_run:acme/app:1:1:completed"
    keys.claim(key, "acme/app#1.1")
    store.enqueue(key, "acme/app#1.1", now=T0)

    outcome = process_next(
        store, lambda job: f"triaged {job.run_id}", worker="w1", idempotency=keys, now=T0
    )
    assert outcome.outcome == RECORDED
    assert store.get(outcome.job.id).result == "triaged acme/app#1.1"

    replayed = keys.claim(key, "acme/app#1.1")
    assert replayed.outcome != "first"
    assert replayed.result == "triaged acme/app#1.1"


def test_process_next_classifies_the_handler_exception(store):
    _enqueue(store)

    def broken(_job):
        raise TimeoutError("provider did not answer")

    outcome = process_next(store, broken, worker="w1", now=T0)
    assert outcome.outcome == RETRY_SCHEDULED
    assert outcome.error == "TimeoutError: provider did not answer"
    assert store.get(outcome.job.id).state == PENDING


def test_process_next_buries_a_budget_failure_on_the_first_attempt(engine, store):
    keys = IdempotencyStore(engine)
    key = "workflow_run:acme/app:1:1:completed"
    keys.claim(key, "acme/app#1.1")
    store.enqueue(key, "acme/app#1.1", now=T0)

    def too_expensive(job):
        raise BudgetExceeded(job.run_id, 500_000, 1_000)

    outcome = process_next(store, too_expensive, worker="w1", idempotency=keys, now=T0)
    assert outcome.outcome == DEAD_LETTERED
    assert store.get(outcome.job.id).attempt == 1

    # The key is settled too: a redelivery must not start the work again just
    # because GitHub retried the hook.
    redelivered = keys.claim(key, "acme/app#1.1")
    assert redelivered.outcome != "first"
    assert "dead-lettered: BudgetExceeded" in redelivered.result


def test_process_next_on_an_empty_queue_does_nothing(store):
    assert process_next(store, lambda job: "unused", worker="w1", now=T0) is None


def test_the_idempotency_key_is_only_settled_when_the_job_is(engine, store):
    """A retryable failure must leave the key claimed, not completed.

    Completing it would make the next redelivery replay a failure as though it
    were the answer, while the job it belongs to is still queued for another try.
    """
    keys = IdempotencyStore(engine)
    key = "workflow_run:acme/app:1:1:completed"
    keys.claim(key, "acme/app#1.1")
    store.enqueue(key, "acme/app#1.1", now=T0)

    def broken(_job):
        raise TimeoutError("provider did not answer")

    process_next(store, broken, worker="w1", idempotency=keys, now=T0)

    claim = keys.claim(key, "acme/app#1.1")
    assert claim.outcome == "duplicate_in_flight"
    assert claim.result is None
