"""Exactly-once processing, enforced by the database rather than by a check.

The side effect this service must not repeat is *posting a comment on a
stranger's pull request*. GitHub delivery is at-least-once -- a hook is
redelivered on any non-2xx response, on a manual redelivery, and on GitHub's own
retries -- so "have I seen this before?" cannot be answered by looking, only by
claiming.

The claim is a single ``INSERT`` of the key. If it succeeds, this caller is the
first and only owner of that key; if it raises the primary-key violation, some
other delivery already owns it. The uniqueness constraint does the arbitration,
under the row lock, in one statement -- so two workers racing the same
redelivered event cannot both conclude they are first. This is the same shape as
the ledger's atomic reservation and its ``open_run``: *the constraint is the
guarantee*, application code only reads the verdict.

A read-first version -- ``SELECT`` the key, and ``INSERT`` if absent -- is the
check-then-act race again: both workers read "absent", both insert, and now the
comment is posted twice. It is invisible to single-threaded tests, which is why
``tests/test_idempotency_concurrency.py`` drives real threads and asserts that
exactly one of them wins the claim.

Key choice: the key is derived from the *event's content*, not from the delivery
envelope. GitHub's ``X-GitHub-Delivery`` GUID is documented only as identifying
"the event", with no stated guarantee that a redelivery reuses it -- so keying on
it could let a redelivery through as new. Keying on what happened
(``workflow_run:<repo>:<run_id>:<action>``) is stable across redeliveries by
construction, whatever the envelope does. See :mod:`ci_triage.webhook`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import Engine, delete, select, update
from sqlalchemy.exc import IntegrityError

from .schema import idempotency_keys

PROCESSING = "processing"
COMPLETED = "completed"

#: Outcomes of a claim.
FIRST = "first"  # this caller owns the key and must do the work
DUPLICATE_IN_FLIGHT = "duplicate_in_flight"  # another delivery is doing it now
DUPLICATE_COMPLETED = "duplicate_completed"  # already done; replay the result


class IdempotencyError(Exception):
    """Base class for idempotency-store failures."""


@dataclass(frozen=True)
class Claim:
    """The verdict on a claim attempt.

    ``result`` carries the stored outcome of the original processing only when
    ``outcome`` is :data:`DUPLICATE_COMPLETED`, so a redelivery can reply with
    what the first delivery produced instead of doing the work again.
    """

    outcome: str
    result: str | None = None

    @property
    def is_first(self) -> bool:
        return self.outcome == FIRST


def _now() -> datetime:
    return datetime.now(timezone.utc)


class IdempotencyStore:
    """A durable record of which event keys have been, or are being, handled."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def claim(self, key: str, run_id: str) -> Claim:
        """Attempt to become the sole owner of ``key``.

        Returns :data:`FIRST` if this call inserted the key -- the caller must
        then do the work and finish with :meth:`complete` or :meth:`release`.
        Otherwise the key already exists and the claim reports whether the prior
        owner is still in flight or has completed.
        """
        # Bounded loop only to handle a genuine race: the key can be *released*
        # (deleted) by another worker between our failed insert and our read, in
        # which case it is free again and we retry the insert. Without a
        # release path this loop would run at most twice; the cap is a
        # backstop, not an expected code path.
        for _ in range(5):
            try:
                with self._engine.begin() as conn:
                    conn.execute(
                        idempotency_keys.insert().values(
                            key=key,
                            run_id=run_id,
                            state=PROCESSING,
                            result=None,
                            created_at=_now(),
                            completed_at=None,
                        )
                    )
                return Claim(FIRST)
            except IntegrityError:
                with self._engine.begin() as conn:
                    row = conn.execute(
                        select(idempotency_keys).where(idempotency_keys.c.key == key)
                    ).one_or_none()
                if row is None:
                    # Released between our insert and our read -- it is free
                    # again. Retry the claim.
                    continue
                if row.state == COMPLETED:
                    return Claim(DUPLICATE_COMPLETED, row.result)
                return Claim(DUPLICATE_IN_FLIGHT)

        raise IdempotencyError(
            f"could not settle a claim on {key!r}: it was repeatedly created and "
            "released by other workers"
        )

    def complete(self, key: str, result: str | None = None) -> None:
        """Mark a claimed key as done and store its result for replay.

        Only moves a key that is still ``processing``; a no-op otherwise, so a
        double-complete cannot overwrite a settled result.
        """
        with self._engine.begin() as conn:
            conn.execute(
                update(idempotency_keys)
                .where(
                    idempotency_keys.c.key == key,
                    idempotency_keys.c.state == PROCESSING,
                )
                .values(state=COMPLETED, result=result, completed_at=_now())
            )

    def release(self, key: str) -> None:
        """Give up a claim so the event can be processed by a later delivery.

        This is the path for a delivery that failed *before* the side effect
        happened -- a crash during setup, a transient error fetching logs. The
        key is deleted so a redelivery is treated as new rather than as a
        duplicate of work that never occurred, mirroring the ledger's
        ``release`` (which distinguishes "cost nothing" from "never happened").

        Only releases a key still ``processing``: once ``completed``, the work
        did happen and must stay deduplicated.
        """
        with self._engine.begin() as conn:
            conn.execute(
                delete(idempotency_keys).where(
                    idempotency_keys.c.key == key,
                    idempotency_keys.c.state == PROCESSING,
                )
            )
