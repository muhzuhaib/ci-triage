"""The test the idempotency store exists to pass.

Exactly-once is a claim about behaviour under a redelivery race, so a
single-threaded test proves the wrong thing -- a read-first store passes every
test in ``test_idempotency.py``. This module races real threads at one key and
asserts that exactly one wins, and it carries a deliberately naive store to
prove the harness can actually catch a double-claim.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from sqlalchemy import select

from ci_triage.idempotency import (
    DUPLICATE_IN_FLIGHT,
    FIRST,
    PROCESSING,
    Claim,
    IdempotencyStore,
)
from sqlalchemy.exc import IntegrityError

from ci_triage.schema import idempotency_keys


class NaiveStore(IdempotencyStore):
    """Check-then-act: decide "I am first" from a *read*, insert as an afterthought.

    This is the realistic mistake, and it is worse than it looks. The primary
    key on ``idempotency_keys`` means two workers can never create two rows -- so
    a developer who reads "absent", concludes they own the event, and posts the
    comment has already caused the double-post *before* the bookkeeping insert
    runs. When that insert then hits the constraint, swallowing it ("row already
    there, fine") feels harmless and cements the bug: both workers returned
    FIRST and both acted.

    The correct store never decides from the read; the insert *is* the decision,
    and the constraint violation *is* the duplicate signal. Present only so the
    test below can show the harness catches a double-claim.
    """

    def __init__(self, engine, *, gap_barrier: threading.Barrier | None = None) -> None:
        super().__init__(engine)
        # When set, every worker waits here after reading and before inserting,
        # so all reads provably complete before any insert. See the gap below.
        self._gap_barrier = gap_barrier

    def claim(self, key: str, run_id: str) -> Claim:  # type: ignore[override]
        # --- transaction 1: look, and decide ownership from what we saw ---
        with self._engine.begin() as conn:
            row = conn.execute(
                select(idempotency_keys).where(idempotency_keys.c.key == key)
            ).one_or_none()
        if row is not None:
            return Claim(DUPLICATE_IN_FLIGHT)

        # ---- the gap: every other barrier-released worker also read "absent" ----
        # This stands in for the real work -- fetch logs, call the LLM, post the
        # comment -- which happens *after* deciding we are first and *before*
        # recording it.
        #
        # A barrier here is deterministic where a sleep is a race, for the same
        # reason spelled out in the ledger's naive control: SQLite's
        # ``BEGIN IMMEDIATE`` serialises the reads, so with a bare sleep the
        # early workers can insert before the late workers have read, the window
        # closes, and the control case fails to demonstrate the bug it exists to
        # demonstrate. The barrier holds every worker until all sixteen have read
        # the same "absent", which is the interleaving Postgres produces
        # naturally under genuinely concurrent transactions.
        if self._gap_barrier is not None:
            self._gap_barrier.wait()
        else:
            time.sleep(0.02)

        # We have already decided we are first. The insert below is treated as
        # mere record-keeping, and its failure as nothing to worry about.
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    idempotency_keys.insert().values(
                        key=key,
                        run_id=run_id,
                        state=PROCESSING,
                        result=None,
                        created_at=datetime.now(timezone.utc),
                        completed_at=None,
                    )
                )
        except IntegrityError:
            pass  # "already recorded -- no matter, we know we're first"
        return Claim(FIRST)


def _race(store: IdempotencyStore, key: str, workers: int):
    """Fire ``workers`` claims at one key simultaneously; count the winners."""
    barrier = threading.Barrier(workers)
    firsts: list[Claim] = []
    others: list[Claim] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def attempt(_i: int) -> None:
        barrier.wait()
        try:
            claim = store.claim(key, "run-1")
        except Exception as exc:  # noqa: BLE001 - the test records, doesn't hide
            with lock:
                errors.append(exc)
            return
        with lock:
            (firsts if claim.is_first else others).append(claim)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(attempt, range(workers)))

    return firsts, others, errors


def test_exactly_one_claim_wins_the_race(engine):
    store = IdempotencyStore(engine)
    firsts, others, errors = _race(store, "hot-key", workers=16)

    assert errors == []
    assert len(firsts) == 1, "exactly one delivery may own the key"
    assert len(others) == 15

    # And the row exists exactly once.
    with engine.begin() as conn:
        rows = conn.execute(
            select(idempotency_keys).where(idempotency_keys.c.key == "hot-key")
        ).all()
    assert len(rows) == 1


def test_the_naive_store_double_claims__proving_this_test_has_teeth(engine):
    """The control case. If this stops failing, the harness has stopped testing.

    Workers that read "absent" before anyone inserts all decide they are first.
    More than one FIRST means more than one comment would be posted -- the exact
    outcome the store exists to prevent -- and, as with the ledger, nothing is
    raised to announce it.

    The gap barrier makes that certain rather than likely: every worker reads
    before any worker inserts, on both backends, every run.
    """
    workers = 16
    naive = NaiveStore(engine, gap_barrier=threading.Barrier(workers))
    firsts, _others, _errors = _race(naive, "hot-key", workers=workers)

    assert len(firsts) > 1, "the read-first store must let more than one caller win"
