"""The run state machine: retries with backoff, a dead-letter queue, and replay.

The webhook receiver answers "should this delivery be worked on?". This module
answers the harder question that follows: *this* attempt at the work failed, so
what now? A triage job fetches logs, calls a provider and posts a comment, and
each of those can fail transiently, permanently, or by running out of money.

Four decisions shape everything below.

**1. The claim is a conditional write, not a query.** Postgres has
``SELECT ... FOR UPDATE SKIP LOCKED``, which is the natural answer to "hand one
job to exactly one worker" -- and SQLite has nothing like it: 3.50.4 answers that
statement with ``OperationalError: near "FOR": syntax error``, and there is no
weaker form of it to fall back to either, since plain ``FOR UPDATE`` is rejected
in the same place. Measured, not assumed, because the design turns on it. Since the
guarantee has to hold on both backends, the claim here is an ``UPDATE`` whose
``WHERE`` clause re-asserts every condition that made the job runnable, and
whose ``rowcount`` *is* the verdict. The ``SELECT`` that precedes it only
nominates candidates; it decides nothing, so it cannot be raced. This is the
same rule as the ledger's reservation and the idempotency store's insert: the
condition belongs inside the statement that writes.

**2. A lease is not a mutex, so ``attempt`` is a fencing token.** A worker holds
a job for ``lease_seconds``; if it crashes, the lease expires and another worker
takes over. But the first worker may not be dead -- it may be a paused process
or a network partition, and it can wake up and try to write its result. Every
write is therefore guarded on ``(worker, attempt)``, and the claim increments
``attempt``. A stale worker's write matches nothing, so it is refused rather
than allowed to overwrite the outcome of the attempt that replaced it. This is
also what un-sticks the crashed-mid-processing case the idempotency store
deliberately left open: nothing there needs a timeout, because the job -- not
the key -- is what gets retried.

**3. A retry that cannot succeed is not resilience, it is a delay plus a bill.**
Failures are split into retryable and terminal, and terminal ones go straight to
the dead-letter queue without burning the retry budget or waiting out a backoff.
:class:`~ci_triage.budget.BudgetExceeded` is the important member of that set: the
ceiling does not grow, so retrying a call that did not fit will never fit. For a
service whose whole premise is a per-run cost ceiling, retrying past it would be
the one unforgivable bug.

**4. Retries share the run's ceiling; a replay does not raise it.** The webhook
receiver scopes a ledger run to one CI run *attempt* and explicitly leaves the
sharing question to this module. The answer is that every triage attempt for an
event spends from the same ceiling -- otherwise "retry three times" silently
multiplies the cost cap by three and the guarantee is decoration. A replay out
of the dead-letter queue grants further *attempts*; it grants no further money,
so a job that died broke will die broke again at its next reservation, quickly
and visibly.

Two smaller notes. The dead-letter queue is a *state*, not a second table: a
separate table means copying the row, and two copies of one job's truth will
eventually disagree. And replay never resets ``attempt`` -- how many times the
job has really run is a fact, so replay extends ``max_attempts`` instead.

The clock is an argument throughout (``now=``), which is what lets the whole
state machine, backoff schedule and lease expiry included, be tested exactly and
in milliseconds rather than by sleeping and hoping.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import Engine, Row, and_, or_, select, update
from sqlalchemy.exc import IntegrityError

from .budget import BudgetExceeded
from .idempotency import IdempotencyStore
from .schema import triage_jobs

#: Job states. A job waiting out a backoff is ``PENDING`` with a future
#: ``next_attempt_at`` rather than a state of its own: two states that both mean
#: "will run again" would have to be kept in step by every query that asks what
#: is runnable, and one of them would eventually be forgotten.
PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
DEAD_LETTER = "dead_letter"

#: Outcomes of reporting a failure or a success.
RETRY_SCHEDULED = "retry_scheduled"
DEAD_LETTERED = "dead_lettered"
RECORDED = "recorded"
#: The job was claimed by someone else while this worker was working on it. Its
#: result is discarded; the attempt that replaced it owns the outcome.
LEASE_LOST = "lease_lost"

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_LEASE_SECONDS = 300.0

#: How many runnable rows one claim attempt considers. Larger is not better: the
#: candidates are only hints, and a worker that loses the race for the first
#: simply tries the next.
_CANDIDATE_BATCH = 8


class JobError(Exception):
    """Base class for state-machine failures."""


class UnknownJob(JobError):
    """No job with that id."""


class NotDeadLettered(JobError):
    """Replay was asked for a job that has not been buried.

    Replaying a live job would hand it a second worker.
    """


class TerminalError(Exception):
    """Raised by a handler to say "do not retry me".

    For failures the handler itself can recognise as permanent -- a repository
    that no longer exists, a log that will never parse. The classifier below
    treats it, and :class:`~ci_triage.budget.BudgetExceeded`, as terminal.
    """


def full_jitter(delay: float, rng: random.Random | None = None) -> float:
    """Spread a backoff delay uniformly over ``[0, delay]``.

    Not decoration. The failures that cause retries are usually shared -- a
    provider returning 429 to everyone, an API outage -- so every worker
    computes the same backoff from the same clock and the herd re-forms on each
    attempt, arriving together and being rejected together. Randomising the
    whole interval, rather than adding a small wobble to it, is what actually
    breaks that synchronisation up.
    """
    return (rng or random).uniform(0.0, delay)


def no_jitter(delay: float) -> float:
    """The exact schedule, for tests and for anyone who wants to read it."""
    return delay


@dataclass(frozen=True)
class Backoff:
    """Exponential backoff with full jitter and a cap.

    The cap matters as much as the growth: unbounded doubling reaches delays
    longer than anyone will wait for a CI comment, at which point the job is
    effectively dead but still occupying the queue.
    """

    base_seconds: float = 2.0
    cap_seconds: float = 300.0
    jitter: Callable[[float], float] = field(default=full_jitter)

    def delay_for(self, attempt: int) -> float:
        """Delay before the attempt after ``attempt`` (1-based, so ``1`` is base)."""
        if attempt < 1:
            raise ValueError("attempt is 1-based")
        # Compute the exponent against the cap rather than doubling first: a
        # large attempt count would otherwise produce a float overflow on the
        # way to a number that is immediately thrown away.
        raw = min(self.base_seconds * 2.0 ** min(attempt - 1, 32), self.cap_seconds)
        return self.jitter(raw)


@dataclass(frozen=True)
class TriageJob:
    """A point-in-time view of one job.

    ``worker`` and ``attempt`` together are the fencing token: hold this object,
    do the work, and the store will only accept a write that still matches them.
    """

    id: str
    idempotency_key: str
    run_id: str
    state: str
    attempt: int
    max_attempts: int
    next_attempt_at: datetime
    lease_expires_at: datetime | None
    worker: str | None
    last_error: str | None
    result: str | None
    replays: int

    @property
    def attempts_remaining(self) -> int:
        return max(0, self.max_attempts - self.attempt)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    """Re-attach UTC to a value SQLite handed back naive.

    SQLite has no timestamp type, so a ``DateTime(timezone=True)`` column round
    trips as a naive string and comes back without its offset. Everything here
    writes UTC, so the offset is known -- but comparing a naive value to an aware
    one raises, and it would raise inside application code far from the cause.
    Postgres returns aware values and is unaffected.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _job(row: Row) -> TriageJob:
    return TriageJob(
        id=row.id,
        idempotency_key=row.idempotency_key,
        run_id=row.run_id,
        state=row.state,
        attempt=row.attempt,
        max_attempts=row.max_attempts,
        next_attempt_at=_as_utc(row.next_attempt_at),  # type: ignore[arg-type]
        lease_expires_at=_as_utc(row.lease_expires_at),
        worker=row.worker,
        last_error=row.last_error,
        result=row.result,
        replays=row.replays,
    )


def default_is_retryable(exc: BaseException) -> bool:
    """Everything is worth another try except what provably is not.

    Defaulting the *unknown* exception to retryable is the deliberate direction:
    an unrecognised transient error retried three times costs three attempts,
    while an unrecognised transient error buried on sight costs a diagnosis
    nobody gets. The two failures that are known to be permanent are named.
    """
    return not isinstance(exc, (TerminalError, BudgetExceeded))


class JobStore:
    """The state machine's storage and transitions."""

    def __init__(
        self,
        engine: Engine,
        *,
        backoff: Backoff | None = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._engine = engine
        self._backoff = backoff or Backoff()
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts

    # ---------------------------------------------------------------- enqueue

    def enqueue(
        self,
        idempotency_key: str,
        run_id: str,
        *,
        max_attempts: int | None = None,
        now: datetime | None = None,
    ) -> TriageJob | None:
        """Create the job for a claimed event.

        Returns ``None`` if a job for that key already exists. The unique
        constraint decides, not a preceding read: the receiver's idempotency
        claim already makes a second enqueue unlikely, and "unlikely" is exactly
        the class of bug that ships.
        """
        limit = self._max_attempts if max_attempts is None else max_attempts
        if limit < 1:
            raise ValueError("max_attempts must be at least 1")
        moment = now or _now()
        job_id = uuid.uuid4().hex
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    triage_jobs.insert().values(
                        id=job_id,
                        idempotency_key=idempotency_key,
                        run_id=run_id,
                        state=PENDING,
                        attempt=0,
                        max_attempts=limit,
                        next_attempt_at=moment,
                        lease_expires_at=None,
                        worker=None,
                        last_error=None,
                        result=None,
                        replays=0,
                        created_at=moment,
                        updated_at=moment,
                    )
                )
        except IntegrityError:
            return None
        return self.get(job_id)

    # ------------------------------------------------------------------ claim

    def _runnable(self, moment: datetime):
        """Rows that a worker may take: due, or abandoned by a dead lease."""
        return or_(
            and_(
                triage_jobs.c.state == PENDING,
                triage_jobs.c.next_attempt_at <= moment,
            ),
            and_(
                triage_jobs.c.state == RUNNING,
                triage_jobs.c.lease_expires_at <= moment,
            ),
        )

    def claim_next(
        self, worker: str, *, now: datetime | None = None
    ) -> TriageJob | None:
        """Take ownership of one runnable job, or return ``None`` if there is none.

        Also the place where a crashed worker's job is settled: the call that
        would retry it is the call that buries it if no attempts remain, so
        there is no background reaper to forget to run.
        """
        moment = now or _now()
        lease_until = moment + timedelta(seconds=self._lease_seconds)

        with self._engine.begin() as conn:
            candidates: Sequence[Row] = conn.execute(
                select(
                    triage_jobs.c.id,
                    triage_jobs.c.state,
                    triage_jobs.c.attempt,
                    triage_jobs.c.max_attempts,
                )
                .where(self._runnable(moment))
                .order_by(triage_jobs.c.next_attempt_at)
                .limit(_CANDIDATE_BATCH)
            ).all()

        for candidate in candidates:
            if candidate.attempt >= candidate.max_attempts:
                # An abandoned lease on a job with no attempts left. Bury it and
                # move on; the guard makes this safe to race.
                self._bury_abandoned(candidate.id, moment)
                continue

            with self._engine.begin() as conn:
                won = conn.execute(
                    update(triage_jobs)
                    .where(
                        triage_jobs.c.id == candidate.id,
                        # Every condition that made this row a candidate is
                        # re-asserted here, inside the write. The SELECT above is
                        # a hint; this is the decision.
                        self._runnable(moment),
                        triage_jobs.c.attempt < triage_jobs.c.max_attempts,
                    )
                    .values(
                        state=RUNNING,
                        # Incremented from the column, never from the value read
                        # above: two claims that both read attempt=1 must not
                        # both write attempt=2.
                        attempt=triage_jobs.c.attempt + 1,
                        worker=worker,
                        lease_expires_at=lease_until,
                        updated_at=moment,
                    )
                )
                if won.rowcount == 1:
                    row = conn.execute(
                        select(triage_jobs).where(triage_jobs.c.id == candidate.id)
                    ).one()
                    return _job(row)
            # Lost this one to another worker: try the next candidate.

        return None

    def _bury_abandoned(self, job_id: str, moment: datetime) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(triage_jobs)
                .where(
                    triage_jobs.c.id == job_id,
                    triage_jobs.c.state == RUNNING,
                    triage_jobs.c.lease_expires_at <= moment,
                    triage_jobs.c.attempt >= triage_jobs.c.max_attempts,
                )
                .values(
                    state=DEAD_LETTER,
                    worker=None,
                    lease_expires_at=None,
                    last_error=(
                        "lease expired with no attempts remaining: the worker "
                        "holding the final attempt did not report an outcome"
                    ),
                    updated_at=moment,
                )
            )

    # --------------------------------------------------------------- outcomes

    def _fence(self, job: TriageJob):
        """Only the attempt that is currently running may settle the job."""
        return and_(
            triage_jobs.c.id == job.id,
            triage_jobs.c.state == RUNNING,
            triage_jobs.c.worker == job.worker,
            triage_jobs.c.attempt == job.attempt,
        )

    def succeed(
        self, job: TriageJob, result: str | None = None, *, now: datetime | None = None
    ) -> str:
        """Record a completed triage. Returns :data:`RECORDED` or :data:`LEASE_LOST`."""
        moment = now or _now()
        with self._engine.begin() as conn:
            written = conn.execute(
                update(triage_jobs)
                .where(self._fence(job))
                .values(
                    state=SUCCEEDED,
                    result=result,
                    worker=None,
                    lease_expires_at=None,
                    last_error=None,
                    updated_at=moment,
                )
            )
        return RECORDED if written.rowcount == 1 else LEASE_LOST

    def fail(
        self,
        job: TriageJob,
        error: str,
        *,
        retryable: bool = True,
        retry_after: float | None = None,
        now: datetime | None = None,
    ) -> str:
        """Record a failed attempt and decide what happens next.

        Returns :data:`RETRY_SCHEDULED`, :data:`DEAD_LETTERED` or
        :data:`LEASE_LOST`.

        ``retry_after`` is a delay the *server* asked for, and it overrides the
        computed backoff rather than competing with it. GitHub documents a
        ``retry-after`` header on a rate-limited response and states plainly
        that continuing to call while limited can get an integration banned, so
        a locally computed schedule that happens to be shorter is not an
        opinion worth having. Jitter is then added on top instead of applied to
        the whole interval: full jitter would spread retries over ``[0, delay]``
        and put most of the herd back inside the window the server closed.
        """
        moment = now or _now()
        exhausted = job.attempt >= job.max_attempts
        if not retryable or exhausted:
            reason = (
                f"attempts exhausted after {job.attempt} of {job.max_attempts}: {error}"
                if exhausted and retryable
                else error
            )
            with self._engine.begin() as conn:
                written = conn.execute(
                    update(triage_jobs)
                    .where(self._fence(job))
                    .values(
                        state=DEAD_LETTER,
                        worker=None,
                        lease_expires_at=None,
                        last_error=reason,
                        updated_at=moment,
                    )
                )
            return DEAD_LETTERED if written.rowcount == 1 else LEASE_LOST

        if retry_after is None:
            delay = self._backoff.delay_for(job.attempt)
        else:
            delay = max(0.0, retry_after) + self._backoff.jitter(self._backoff.base_seconds)
        with self._engine.begin() as conn:
            written = conn.execute(
                update(triage_jobs)
                .where(self._fence(job))
                .values(
                    state=PENDING,
                    worker=None,
                    lease_expires_at=None,
                    next_attempt_at=moment + timedelta(seconds=delay),
                    last_error=error,
                    updated_at=moment,
                )
            )
        return RETRY_SCHEDULED if written.rowcount == 1 else LEASE_LOST

    # ----------------------------------------------------------------- replay

    def replay(
        self,
        job_id: str,
        *,
        extra_attempts: int = 1,
        now: datetime | None = None,
    ) -> TriageJob:
        """Return a dead-lettered job to the queue with further attempts granted.

        ``attempt`` is not reset: it records what actually happened, and a replay
        does not un-happen it. ``max_attempts`` is raised instead, which is also
        why a repeatedly replayed job reads honestly in the table rather than
        looking like a job that has never had trouble.

        No money is granted. The run's ceiling is untouched, so a job that died
        of :class:`~ci_triage.budget.BudgetExceeded` will fail at its first
        reservation and return here -- which is the correct outcome, arrived at
        immediately instead of after three backoffs.
        """
        if extra_attempts < 1:
            raise ValueError("a replay must grant at least one attempt")
        moment = now or _now()
        with self._engine.begin() as conn:
            written = conn.execute(
                update(triage_jobs)
                .where(
                    triage_jobs.c.id == job_id,
                    triage_jobs.c.state == DEAD_LETTER,
                )
                .values(
                    state=PENDING,
                    max_attempts=triage_jobs.c.attempt + extra_attempts,
                    next_attempt_at=moment,
                    worker=None,
                    lease_expires_at=None,
                    replays=triage_jobs.c.replays + 1,
                    updated_at=moment,
                )
            )
            if written.rowcount == 0:
                row = conn.execute(
                    select(triage_jobs.c.state).where(triage_jobs.c.id == job_id)
                ).one_or_none()
                if row is None:
                    raise UnknownJob(f"no job {job_id!r}")
                raise NotDeadLettered(
                    f"job {job_id!r} is {row.state!r}, not {DEAD_LETTER!r}"
                )
            return _job(
                conn.execute(
                    select(triage_jobs).where(triage_jobs.c.id == job_id)
                ).one()
            )

    # ------------------------------------------------------------------ reads

    def get(self, job_id: str) -> TriageJob:
        with self._engine.begin() as conn:
            row = conn.execute(
                select(triage_jobs).where(triage_jobs.c.id == job_id)
            ).one_or_none()
        if row is None:
            raise UnknownJob(f"no job {job_id!r}")
        return _job(row)

    def by_key(self, idempotency_key: str) -> TriageJob | None:
        with self._engine.begin() as conn:
            row = conn.execute(
                select(triage_jobs).where(
                    triage_jobs.c.idempotency_key == idempotency_key
                )
            ).one_or_none()
        return None if row is None else _job(row)

    def dead_letter_queue(self, *, limit: int = 100) -> list[TriageJob]:
        """The buried jobs, oldest first -- a view over state, not a second table."""
        with self._engine.begin() as conn:
            rows = conn.execute(
                select(triage_jobs)
                .where(triage_jobs.c.state == DEAD_LETTER)
                .order_by(triage_jobs.c.updated_at)
                .limit(limit)
            ).all()
        return [_job(row) for row in rows]


@dataclass(frozen=True)
class ProcessOutcome:
    """What one turn of the worker loop did."""

    job: TriageJob
    outcome: str
    error: str | None = None


def process_next(
    store: JobStore,
    handler: Callable[[TriageJob], str | None],
    *,
    worker: str,
    idempotency: IdempotencyStore | None = None,
    is_retryable: Callable[[BaseException], bool] = default_is_retryable,
    now: datetime | None = None,
) -> ProcessOutcome | None:
    """Claim one job, run ``handler`` on it, and record the outcome.

    Returns ``None`` when there is nothing runnable. Pass ``idempotency`` to
    close the loop the receiver opened: a settled job marks its key completed, so
    a later redelivery of the same event replays the stored result instead of
    starting the work again. A dead-lettered job is settled too -- GitHub
    redelivering a hook is not a decision to spend the budget again, and replay
    out of the dead-letter queue is the deliberate path back.

    Only ``Exception`` is caught. A ``KeyboardInterrupt`` or ``SystemExit``
    leaves the job ``RUNNING`` with a live lease, which is exactly the crash
    case: the lease expires and another worker picks it up. That is the designed
    recovery path, not a gap in it.
    """
    moment = now or _now()
    job = store.claim_next(worker, now=moment)
    if job is None:
        return None

    try:
        result = handler(job)
    except Exception as exc:  # noqa: BLE001 - classified below, never swallowed
        error = f"{type(exc).__name__}: {exc}"
        # Read duck-typed rather than imported: an exception that knows when the
        # server will accept work again should be able to say so without this
        # module growing a dependency on whichever client raised it.
        retry_after = getattr(exc, "retry_after_seconds", None)
        outcome = store.fail(
            job,
            error,
            retryable=is_retryable(exc),
            retry_after=retry_after if isinstance(retry_after, (int, float)) else None,
            now=now or _now(),
        )
        if outcome == DEAD_LETTERED and idempotency is not None:
            idempotency.complete(job.idempotency_key, f"dead-lettered: {error}")
        return ProcessOutcome(job, outcome, error)

    outcome = store.succeed(job, result, now=now or _now())
    if outcome == RECORDED and idempotency is not None:
        idempotency.complete(job.idempotency_key, result)
    return ProcessOutcome(job, outcome)
