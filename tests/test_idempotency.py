"""The idempotency store's single-threaded contract.

The race is covered separately in ``test_idempotency_concurrency.py`` -- these
tests pin down the lifecycle: claim, complete, replay, release.
"""

from __future__ import annotations

import pytest

from ci_triage.idempotency import (
    COMPLETED,
    DUPLICATE_COMPLETED,
    DUPLICATE_IN_FLIGHT,
    FIRST,
    PROCESSING,
    IdempotencyStore,
)
from ci_triage.schema import idempotency_keys
from sqlalchemy import select


@pytest.fixture()
def store(engine):
    return IdempotencyStore(engine)


def test_a_first_claim_wins(store):
    claim = store.claim("k1", "run-1")
    assert claim.outcome == FIRST
    assert claim.is_first
    assert claim.result is None


def test_a_second_claim_while_in_flight_is_a_duplicate(store):
    store.claim("k1", "run-1")
    claim = store.claim("k1", "run-1")
    assert claim.outcome == DUPLICATE_IN_FLIGHT
    assert not claim.is_first


def test_a_claim_after_completion_replays_the_result(store):
    store.claim("k1", "run-1")
    store.complete("k1", result="posted comment 12345")

    claim = store.claim("k1", "run-1")
    assert claim.outcome == DUPLICATE_COMPLETED
    assert claim.result == "posted comment 12345"


def test_complete_records_state_and_result(store, engine):
    store.claim("k1", "run-1")
    store.complete("k1", result="done")
    with engine.begin() as conn:
        row = conn.execute(
            select(idempotency_keys).where(idempotency_keys.c.key == "k1")
        ).one()
    assert row.state == COMPLETED
    assert row.result == "done"
    assert row.completed_at is not None


def test_complete_is_idempotent_and_does_not_overwrite(store):
    store.claim("k1", "run-1")
    store.complete("k1", result="first result")
    # A second completion (e.g. a retried settle) must not clobber the record.
    store.complete("k1", result="second result")
    claim = store.claim("k1", "run-1")
    assert claim.result == "first result"


def test_release_frees_a_key_for_reprocessing(store):
    store.claim("k1", "run-1")
    store.release("k1")
    # After release the event is treated as never-seen: a redelivery is FIRST.
    claim = store.claim("k1", "run-1")
    assert claim.outcome == FIRST


def test_release_does_not_touch_a_completed_key(store, engine):
    store.claim("k1", "run-1")
    store.complete("k1", result="done")
    store.release("k1")  # must be a no-op: the work really happened
    with engine.begin() as conn:
        row = conn.execute(
            select(idempotency_keys).where(idempotency_keys.c.key == "k1")
        ).one_or_none()
    assert row is not None
    assert row.state == COMPLETED


def test_different_keys_are_independent(store):
    assert store.claim("k1", "run-1").outcome == FIRST
    assert store.claim("k2", "run-1").outcome == FIRST
    assert store.claim("k1", "run-1").outcome == DUPLICATE_IN_FLIGHT


def test_a_fresh_claim_starts_processing(store, engine):
    store.claim("k1", "run-1")
    with engine.begin() as conn:
        row = conn.execute(
            select(idempotency_keys).where(idempotency_keys.c.key == "k1")
        ).one()
    assert row.state == PROCESSING
    assert row.run_id == "run-1"
    assert row.result is None
