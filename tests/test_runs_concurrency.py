"""The test the claim exists to pass.

A queue that hands one job to one worker is trivially true with one worker. The
double-processing appears when a pool of them polls the same table at the same
moment -- and every test in ``test_runs.py`` passes against a claim that reads
first and writes second.

So this module races real threads at one job and asserts exactly one wins, and it
carries a deliberately naive store to prove the harness can catch a double
claim. A concurrency test that has never been observed to fail is not evidence
of anything.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from ci_triage.runs import (
    PENDING,
    RUNNING,
    Backoff,
    JobStore,
    TriageJob,
    _job,
    no_jitter,
)
from ci_triage.schema import triage_jobs

T0 = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)
EXACT = Backoff(base_seconds=2.0, cap_seconds=10.0, jitter=no_jitter)
WORKERS = 16


class NaiveJobStore(JobStore):
    """How you would write the claim if you weren't thinking about concurrency.

    Find a pending job, then mark it running -- with the "is it pending?"
    decision taken from the read rather than re-asserted in the write. The
    ``UPDATE`` is guarded on the primary key alone, which feels sufficient
    ("I just checked, it was pending") and is the whole bug: sixteen workers read
    the same pending row, all sixteen write, and all sixteen return a job. Each
    then fetches the same logs, spends from the same ceiling and posts the same
    comment.

    Note that it is not enough for the naive version to be wrong -- it has to be
    *observably* wrong on both backends. Hence the barrier at the gap: SQLite's
    ``BEGIN IMMEDIATE`` serialises writers so thoroughly that a sleep-based
    window can close before the late readers arrive, and the control case would
    then pass and prove nothing.
    """

    def __init__(self, engine, *, gap_barrier: threading.Barrier | None = None, **kw) -> None:
        super().__init__(engine, **kw)
        self._gap_barrier = gap_barrier

    def claim_next(self, worker: str, *, now: datetime | None = None):  # type: ignore[override]
        moment = now or T0
        # --- transaction 1: find a job, and decide it is ours ---
        with self._engine.begin() as conn:
            row = conn.execute(
                select(triage_jobs)
                .where(
                    triage_jobs.c.state == PENDING,
                    triage_jobs.c.next_attempt_at <= moment,
                )
                .order_by(triage_jobs.c.next_attempt_at)
                .limit(1)
            ).one_or_none()
        if row is None:
            return None

        # ---- the gap: every other worker has now read the same row ----
        if self._gap_barrier is not None:
            self._gap_barrier.wait()

        # --- transaction 2: write, on a decision that is already stale ---
        with self._engine.begin() as conn:
            conn.execute(
                update(triage_jobs)
                .where(triage_jobs.c.id == row.id)  # the missing conditions
                .values(
                    state=RUNNING,
                    attempt=triage_jobs.c.attempt + 1,
                    worker=worker,
                    lease_expires_at=moment + timedelta(seconds=self._lease_seconds),
                    updated_at=moment,
                )
            )
            fresh = conn.execute(
                select(triage_jobs).where(triage_jobs.c.id == row.id)
            ).one()
        return _job(fresh)


def _race(store: JobStore, workers: int, *, now: datetime):
    """Fire ``workers`` claims at one queue simultaneously; collect the winners."""
    barrier = threading.Barrier(workers)
    claimed: list[TriageJob] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def attempt(_i: int) -> None:
        barrier.wait()
        try:
            job = store.claim_next(f"worker-{_i}", now=now)
        except Exception as exc:  # noqa: BLE001 - the test records, doesn't hide
            with lock:
                errors.append(exc)
            return
        if job is not None:
            with lock:
                claimed.append(job)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(attempt, range(workers)))

    return claimed, errors


def test_exactly_one_worker_claims_a_job(engine):
    store = JobStore(engine, backoff=EXACT, lease_seconds=60.0)
    store.enqueue("k", "acme/app#1.1", now=T0)

    claimed, errors = _race(store, WORKERS, now=T0)

    assert errors == []
    assert len(claimed) == 1, "one job, one worker, one comment on the pull request"
    assert claimed[0].attempt == 1

    row = store.get(claimed[0].id)
    assert row.state == RUNNING
    assert row.worker == claimed[0].worker
    assert row.attempt == 1, "the attempt counter must not be advanced by the losers"


def test_an_expired_lease_is_reclaimed_by_exactly_one_worker(engine):
    """Recovery must not itself be a race, or a crash becomes a double-post."""
    store = JobStore(engine, backoff=EXACT, lease_seconds=60.0)
    store.enqueue("k", "acme/app#1.1", now=T0)
    store.claim_next("worker-dead", now=T0)

    claimed, errors = _race(store, WORKERS, now=T0 + timedelta(seconds=60))

    assert errors == []
    assert len(claimed) == 1
    assert claimed[0].attempt == 2
    assert store.get(claimed[0].id).attempt == 2


def test_a_pool_of_workers_drains_a_queue_without_repeating_a_job(engine):
    """Sixteen workers, eight jobs: every job goes out once and only once."""
    store = JobStore(engine, backoff=EXACT, lease_seconds=60.0)
    for n in range(8):
        store.enqueue(f"key-{n}", f"acme/app#{n}.1", now=T0)

    claimed, errors = _race(store, WORKERS, now=T0)

    assert errors == []
    assert len(claimed) == 8
    assert len({job.id for job in claimed}) == 8, "no job may be handed out twice"
    assert {job.attempt for job in claimed} == {1}


def test_the_naive_claim_hands_one_job_to_everyone__proving_this_test_has_teeth(engine):
    """The control case. If this stops failing, the harness has stopped testing.

    Sixteen workers all receive the same job, so sixteen diagnoses are fetched,
    charged and posted -- and nothing is raised to announce it. That silence is
    what makes the read-first version so easy to ship.
    """
    naive = NaiveJobStore(
        engine,
        gap_barrier=threading.Barrier(WORKERS),
        backoff=EXACT,
        lease_seconds=60.0,
    )
    naive.enqueue("k", "acme/app#1.1", now=T0)

    claimed, _errors = _race(naive, WORKERS, now=T0)

    assert len(claimed) > 1, "the read-first claim must let more than one worker win"
    assert len({job.id for job in claimed}) == 1, "and it is the same job every time"
