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
            # Guard against double-settlement: only adjust the run totals if
            # this call is the one that moved the reservation out of HELD.
            if settled.rowcount == 0:
                return

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

    def release(self, reservation: Reservation) -> None:
        """Return a held reservation to the run's budget, charging nothing.

        This is the path for a call that never reached the provider -- a
        connection failure, a timeout before the request was sent, a crash
        during setup. Releasing rather than committing zero keeps the
        distinction visible in the reservations table between "cost nothing"
        and "never happened".
        """
        with self._engine.begin() as conn:
            settled = conn.execute(
                update(reservations)
                .where(
                    reservations.c.id == reservation.id,
                    reservations.c.state == HELD,
                )
                .values(state=RELEASED, actual_micros=0, settled_at=_now())
            )
            if settled.rowcount == 0:
                return

            conn.execute(
                update(run_budgets)
                .where(run_budgets.c.run_id == reservation.run_id)
                .values(
                    reserved_micros=run_budgets.c.reserved_micros
                    - reservation.held_micros
                )
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
