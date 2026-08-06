"""The per-run spend ledger.

The whole project rests on one guarantee:

    For any run, the sum of money committed plus money currently reserved
    never exceeds that run's ceiling -- no matter how many workers act on the
    run at once, how many times an attempt is retried, or how many times the
    triggering webhook is redelivered.

The mechanism is reserve-then-reconcile, the same shape as an authorisation
hold on a payment card:

1. Estimate the *worst case* cost of a call before making it.
2. Reserve that amount atomically. If it does not fit under the ceiling, the
   call never happens.
3. Make the call.
4. Commit the real cost and release the unused remainder.

Reserving the worst case rather than the expected cost is what makes the
ceiling a guarantee instead of an average. An expected-cost reservation is
breached by any call that runs long, which is precisely the call you wanted the
ceiling for.

Why not check-then-act
----------------------
The obvious implementation reads the current total, compares it to the ceiling,
and then writes. Under concurrency that is a time-of-check/time-of-use race:
two workers both read a total with headroom, both conclude there is room, and
both spend. It is the same bug as a double-spend, and it does not show up in
single-threaded tests -- which is why ``tests/test_budget_concurrency.py``
exists and drives real threads.

The fix is that the check and the write are one statement. The ``WHERE`` clause
carries the ceiling condition, so the database evaluates it against the row it
is about to modify while holding the lock on that row. A reservation that would
breach the ceiling matches zero rows, and a zero row count *is* the refusal.

Why a hold has to be reclaimable
--------------------------------
Reserve-then-reconcile assumes the reserving process comes back to reconcile.
A killed worker does not, and its hold would then stand for ever: the run's
ceiling shrinks by the worst case of a call that may never have been made, and
because retries share one ceiling, three crashes at that seam leave a run broke
having spent nothing. ``tests/test_failure_injection.py`` is what found this,
and it is why :meth:`Ledger.reclaim` exists -- the ``attempt`` a reservation
already carries is the queue's fencing token, so a hold from an attempt earlier
than the one now running provably belongs to a worker that no longer owns the
job.

That leaves one honest edge, and it is recorded rather than hidden. A worker can
be merely paused rather than dead, so its call may land after its hold was
reclaimed. :meth:`Ledger.commit` therefore still records that cost, in full, as
an overrun: the money left the building outside the ceiling's protection, and a
ledger that quietly dropped it would be balancing its books by losing evidence.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import Engine, select, update
from sqlalchemy.exc import IntegrityError

from .money import format_micros
from .schema import reservations, run_budgets

HELD = "held"
COMMITTED = "committed"
RELEASED = "released"
#: Returned to the ceiling by someone other than the worker that took it, because
#: that worker's attempt is over. Distinct from :data:`RELEASED` so the table
#: still says which holds were given back and which were taken back, and so a
#: late commit can be told apart from a repeated one.
RECLAIMED = "reclaimed"


class BudgetError(Exception):
    """Base class for ledger failures."""


class BudgetExceeded(BudgetError):
    """A reservation was refused because it would breach the run's ceiling.

    This is a *terminal* error, not a retryable one. Retrying a call that did
    not fit will not make it fit -- the caller must either reduce the request
    (a shorter prompt, a cheaper model) or dead-letter the run.
    """

    def __init__(self, run_id: str, requested: int, remaining: int) -> None:
        self.run_id = run_id
        self.requested = requested
        self.remaining = remaining
        super().__init__(
            f"run {run_id!r}: reservation of {format_micros(requested)} refused, "
            f"only {format_micros(remaining)} remains under the ceiling"
        )


class UnknownRun(BudgetError):
    """Operation attempted against a run that has no budget row."""


@dataclass(frozen=True)
class Reservation:
    """A held claim on part of a run's ceiling."""

    id: str
    run_id: str
    held_micros: int
    attempt: int
    purpose: str


@dataclass(frozen=True)
class RunSpend:
    """A point-in-time view of one run's ledger."""

    run_id: str
    ceiling_micros: int
    spent_micros: int
    reserved_micros: int
    overrun_micros: int

    @property
    def committed_and_held(self) -> int:
        return self.spent_micros + self.reserved_micros

    @property
    def remaining_micros(self) -> int:
        return self.ceiling_micros - self.committed_and_held


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Ledger:
    """Enforces a hard spend ceiling per run."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ------------------------------------------------------------------ setup

    def open_run(self, run_id: str, ceiling_micros: int) -> bool:
        """Create the budget row for a run.

        Returns ``True`` if this call created it and ``False`` if it already
        existed. Idempotent on purpose: a redelivered webhook re-enters this
        path, and re-opening a run must never reset a ceiling that has already
        been partly spent.
        """
        if ceiling_micros <= 0:
            raise ValueError("ceiling must be positive")
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    run_budgets.insert().values(
                        run_id=run_id,
                        ceiling_micros=ceiling_micros,
                        spent_micros=0,
                        reserved_micros=0,
                        overrun_micros=0,
                        created_at=_now(),
                    )
                )
            return True
        except IntegrityError:
            return False

    # ------------------------------------------------------------------ spend

    def reserve(
        self,
        run_id: str,
        amount_micros: int,
        *,
        attempt: int = 1,
        purpose: str = "",
    ) -> Reservation:
        """Atomically hold ``amount_micros`` against the run's ceiling.

        Raises :class:`BudgetExceeded` if it does not fit, :class:`UnknownRun`
        if the run was never opened.
        """
        if amount_micros < 0:
            raise ValueError("reservation amount cannot be negative")

        reservation_id = uuid.uuid4().hex
        with self._engine.begin() as conn:
            # The check and the write are one statement. See the module
            # docstring -- splitting these is the double-spend bug.
            result = conn.execute(
                update(run_budgets)
                .where(
                    run_budgets.c.run_id == run_id,
                    run_budgets.c.spent_micros
                    + run_budgets.c.reserved_micros
                    + amount_micros
                    <= run_budgets.c.ceiling_micros,
                )
                .values(reserved_micros=run_budgets.c.reserved_micros + amount_micros)
            )

            if result.rowcount == 0:
                # Zero rows means one of two things: no such run, or no room.
                # Distinguish them only now, on the failure path, so the happy
                # path stays a single round trip.
                row = conn.execute(
                    select(run_budgets).where(run_budgets.c.run_id == run_id)
                ).one_or_none()
                if row is None:
                    raise UnknownRun(f"no budget row for run {run_id!r}")
                remaining = (
                    row.ceiling_micros - row.spent_micros - row.reserved_micros
                )
                raise BudgetExceeded(run_id, amount_micros, remaining)

            conn.execute(
                reservations.insert().values(
                    id=reservation_id,
                    run_id=run_id,
                    held_micros=amount_micros,
                    actual_micros=None,
                    state=HELD,
                    attempt=attempt,
                    purpose=purpose,
                    created_at=_now(),
                )
            )

        return Reservation(
            id=reservation_id,
            run_id=run_id,
            held_micros=amount_micros,
            attempt=attempt,
            purpose=purpose,
        )

    def commit(self, reservation: Reservation, actual_micros: int) -> None:
        """Settle a reservation with the cost the provider actually reported.

        The held amount is released and ``actual_micros`` is recorded as spent.

        If the actual cost exceeds what was held, the excess is recorded in
        ``overrun_micros`` and *is* charged. That is deliberate: the ledger's
        job is to record what was truly spent, not to make the books look
        balanced. A non-zero overrun means the estimate was wrong -- a stale
        price table or a provider ignoring ``max_tokens`` -- and it should be
        visible rather than silently clamped. The stated guarantee is therefore
        about reservations, which are enforced, not about provider honesty,
        which cannot be.

        A commit whose hold was :data:`RECLAIMED` while the worker was away is
        still recorded, and the whole cost is an overrun. The hold that
        authorised it has already gone back to the ceiling and may since have
        been spent by the attempt that replaced this one, so there is nothing
        left to charge it against. Dropping it would be the more comfortable
        answer and the dishonest one: the money was spent, and the run's true
        total is the thing this table exists to state.
        """
        if actual_micros < 0:
            raise ValueError("actual cost cannot be negative")

        overrun = max(0, actual_micros - reservation.held_micros)
        with self._engine.begin() as conn:
            settled = conn.execute(
                update(reservations)
                .where(
                    reservations.c.id == reservation.id,
                    reservations.c.state == HELD,
                )
                .values(
                    state=COMMITTED,
                    actual_micros=actual_micros,
                    settled_at=_now(),
                )
            )
            if settled.rowcount == 1:
                conn.execute(
                    update(run_budgets)
                    .where(run_budgets.c.run_id == reservation.run_id)
                    .values(
                        reserved_micros=run_budgets.c.reserved_micros
                        - reservation.held_micros,
                        spent_micros=run_budgets.c.spent_micros + actual_micros,
                        overrun_micros=run_budgets.c.overrun_micros + overrun,
                    )
                )
                return

            # Not held any more. Either this call is a repeat -- in which case
            # the row is already COMMITTED or RELEASED and there is nothing to
            # do -- or the hold was reclaimed and this is the late call landing
            # after all. Only the second case matches, and it is the same rule
            # as everywhere else here: the condition is inside the write.
            late = conn.execute(
                update(reservations)
                .where(
                    reservations.c.id == reservation.id,
                    reservations.c.state == RECLAIMED,
                )
                .values(
                    state=COMMITTED,
                    actual_micros=actual_micros,
                    settled_at=_now(),
                )
            )
            if late.rowcount == 0:
                return

            conn.execute(
                update(run_budgets)
                .where(run_budgets.c.run_id == reservation.run_id)
                .values(
                    spent_micros=run_budgets.c.spent_micros + actual_micros,
                    overrun_micros=run_budgets.c.overrun_micros + actual_micros,
                )
            )

    def _settle_hold(
        self, reservation_id: str, run_id: str, held_micros: int, state: str
    ) -> int:
        """Move one HELD reservation to ``state`` and give the money back.

        Returns what was actually returned to the ceiling, which is zero when
        this call was not the one that moved the row. The ``state == HELD``
        condition lives in the ``WHERE`` clause, so two callers racing to give
        the same hold back cannot both subtract it.
        """
        with self._engine.begin() as conn:
            settled = conn.execute(
                update(reservations)
                .where(
                    reservations.c.id == reservation_id,
                    reservations.c.state == HELD,
                )
                .values(state=state, actual_micros=0, settled_at=_now())
            )
            if settled.rowcount == 0:
                return 0

            conn.execute(
                update(run_budgets)
                .where(run_budgets.c.run_id == run_id)
                .values(
                    reserved_micros=run_budgets.c.reserved_micros - held_micros
                )
            )
        return held_micros

    def release(self, reservation: Reservation) -> int:
        """Return a held reservation to the run's budget, charging nothing.

        This is the path for a call that never reached the provider -- a
        connection failure, a timeout before the request was sent, a crash
        during setup. Releasing rather than committing zero keeps the
        distinction visible in the reservations table between "cost nothing"
        and "never happened".

        Returns the micros returned to the ceiling, or zero if the reservation
        was already settled.
        """
        return self._settle_hold(
            reservation.id, reservation.run_id, reservation.held_micros, RELEASED
        )

    def reclaim(self, run_id: str, *, before_attempt: int) -> int:
        """Give back every hold this run still carries from an earlier attempt.

        A worker that is killed between :meth:`reserve` and :meth:`commit`
        leaves its hold standing, and nothing in the ledger alone can tell that
        hold apart from one belonging to a call still in flight. The queue can:
        it hands a job to one worker at a time and increments ``attempt`` when
        it does, so a hold recorded under an attempt *earlier* than the one now
        running belongs to a worker that has already been replaced. Called at
        the start of each attempt, this is what stops a crash from permanently
        shrinking the ceiling.

        The fencing token is what makes it safe, so the comparison is strictly
        ``<``: the reservation this attempt is about to take is never in scope,
        and neither is one taken by an attempt that has not been superseded.
        It relies on a run having one job, which is what the receiver's
        idempotency key -- run id *and* run attempt -- already guarantees.

        Returns the total returned to the ceiling.
        """
        with self._engine.begin() as conn:
            stale = conn.execute(
                select(reservations.c.id, reservations.c.held_micros).where(
                    reservations.c.run_id == run_id,
                    reservations.c.state == HELD,
                    reservations.c.attempt < before_attempt,
                )
            ).all()

        # The select above nominates; each write below decides. Same shape as
        # the queue's claim, and for the same reason: another worker may be
        # doing this at the same moment.
        return sum(
            self._settle_hold(row.id, run_id, row.held_micros, RECLAIMED)
            for row in stale
        )

    # ------------------------------------------------------------------ reads

    def spend(self, run_id: str) -> RunSpend:
        with self._engine.begin() as conn:
            row = conn.execute(
                select(run_budgets).where(run_budgets.c.run_id == run_id)
            ).one_or_none()
        if row is None:
            raise UnknownRun(f"no budget row for run {run_id!r}")
        return RunSpend(
            run_id=row.run_id,
            ceiling_micros=row.ceiling_micros,
            spent_micros=row.spent_micros,
            reserved_micros=row.reserved_micros,
            overrun_micros=row.overrun_micros,
        )
