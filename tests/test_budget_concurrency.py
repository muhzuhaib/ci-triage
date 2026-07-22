"""The test the ledger exists to pass.

A budget that holds in a single-threaded test proves almost nothing -- the
check-then-act implementation passes every test in ``test_budget.py``. The
overspend only appears when two workers act on the same run at the same time.

So this module drives real threads, and it also contains a deliberately naive
ledger used to prove the test can actually fail. A concurrency test that has
never been seen to fail is not evidence of anything.
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select, update

from ci_triage.budget import BudgetExceeded, Ledger, Reservation
from ci_triage.schema import reservations, run_budgets


class NaiveLedger(Ledger):
    """How you would write it if you weren't thinking about concurrency.

    Reads the total, decides, then writes -- with the decision outside the
    statement that does the write. Present only so the test below can show that
    the harness catches the bug.

    **A note on why the read and the write are in separate transactions here.**
    They are split deliberately, and it is not a strawman. A worker that reads
    budget state, does some work, and then records a charge is the ordinary
    shape of this mistake. It also makes the test backend-independent: SQLite
    configured with ``BEGIN IMMEDIATE`` (see ``schema.create_engine_for``)
    serialises write transactions, so a check-then-act contained *within a
    single* transaction is accidentally safe there -- while the identical code
    on Postgres, under read-committed with genuinely concurrent transactions,
    overspends. Writing the naive version this way means the test demonstrates
    the same failure on both backends instead of passing on SQLite for a reason
    that has nothing to do with the code being correct.
    """

    def reserve(self, run_id, amount_micros, *, attempt=1, purpose=""):  # type: ignore[override]
        # --- transaction 1: read and decide ---
        with self._engine.begin() as conn:
            row = conn.execute(
                select(run_budgets).where(run_budgets.c.run_id == run_id)
            ).one()
            remaining = row.ceiling_micros - row.spent_micros - row.reserved_micros
            created_at = row.created_at

        if amount_micros > remaining:
            raise BudgetExceeded(run_id, amount_micros, remaining)

        # ---- the gap: every other worker reserves right here ----
        time.sleep(0.01)

        # --- transaction 2: write, on a decision that is now stale ---
        with self._engine.begin() as conn:
            conn.execute(
                update(run_budgets)
                .where(run_budgets.c.run_id == run_id)
                .values(
                    reserved_micros=run_budgets.c.reserved_micros + amount_micros
                )
            )
            reservation_id = uuid.uuid4().hex
            conn.execute(
                reservations.insert().values(
                    id=reservation_id,
                    run_id=run_id,
                    held_micros=amount_micros,
                    actual_micros=None,
                    state="held",
                    attempt=attempt,
                    purpose=purpose,
                    created_at=created_at,
                )
            )

        return Reservation(reservation_id, run_id, amount_micros, attempt, purpose)


def _hammer(ledger: Ledger, run_id: str, workers: int, amount: int):
    """Fire ``workers`` reservations at one run simultaneously.

    A barrier lines the threads up so they contend rather than politely
    arriving one after another.
    """
    barrier = threading.Barrier(workers)
    granted: list[object] = []
    refused: list[BudgetExceeded] = []
    lock = threading.Lock()

    def attempt(_i: int) -> None:
        barrier.wait()
        try:
            res = ledger.reserve(run_id, amount)
        except BudgetExceeded as exc:
            with lock:
                refused.append(exc)
        else:
            with lock:
                granted.append(res)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(attempt, range(workers)))

    return granted, refused


def test_concurrent_reservations_never_breach_the_ceiling(ledger: Ledger):
    # Ceiling fits exactly 4 reservations; 16 workers race for them.
    ledger.open_run("r1", 40_000)
    granted, refused = _hammer(ledger, "r1", workers=16, amount=10_000)

    assert len(granted) == 4
    assert len(refused) == 12

    spend = ledger.spend("r1")
    assert spend.reserved_micros == 40_000
    assert spend.committed_and_held <= spend.ceiling_micros


def test_the_ceiling_holds_under_a_mixed_commit_and_release_race(ledger: Ledger):
    ledger.open_run("r1", 100_000)
    granted, _ = _hammer(ledger, "r1", workers=32, amount=5_000)

    # Settle every granted reservation concurrently, half committing the full
    # amount and half releasing, then confirm the books balance.
    def settle(item):
        index, res = item
        if index % 2 == 0:
            ledger.commit(res, res.held_micros)
        else:
            ledger.release(res)

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(settle, enumerate(granted)))

    spend = ledger.spend("r1")
    assert spend.reserved_micros == 0
    assert spend.spent_micros == sum(
        r.held_micros for i, r in enumerate(granted) if i % 2 == 0
    )
    assert spend.committed_and_held <= spend.ceiling_micros


def test_the_naive_ledger_overspends__proving_this_test_has_teeth(engine):
    """The control case. If this ever stops failing, the harness has stopped testing.

    Workers that read the budget before anyone writes all see the same headroom
    and all conclude they fit. Against a ceiling that holds exactly four, more
    than four get through -- and no error is raised anywhere, which is what
    makes this bug expensive: nothing in the logs says it happened.

    The *number* that gets through is not asserted, because it depends on lock
    scheduling: SQLite serialises transactions, so some threads happen to read
    after an earlier writer has landed and are correctly refused. That
    scheduling detail is exactly why the overspend is a bad thing to go looking
    for in production and a good thing to pin down with a control case here.
    What is asserted is the invariant breach itself.
    """
    naive = NaiveLedger(engine)
    naive.open_run("r1", 40_000)
    granted, _refused = _hammer(naive, "r1", workers=16, amount=10_000)

    spend = naive.spend("r1")
    assert len(granted) > 4, "ceiling of 40,000 fits exactly four 10,000 reservations"
    assert spend.reserved_micros > spend.ceiling_micros
    assert spend.committed_and_held > spend.ceiling_micros
