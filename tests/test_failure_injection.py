"""What survives a worker being killed, a provider saying no, and a huge log.

Every other test in this suite asks whether the code does the right thing when
it is allowed to finish. This one takes the finishing away. The distinction
matters more here than in most projects, because the README makes promises that
are only interesting under failure: one comment however many times a delivery
arrives, and a per-run spend ceiling that holds no matter what breaks.

Three tools, and each is chosen to model a real thing rather than a convenient
one.

**A kill is a ``BaseException``.** :func:`~ci_triage.runs.process_next` catches
``Exception`` and turns it into a retry or a burial, which is the handling path,
not the crash path. :class:`WorkerKilled` deliberately escapes that, so the job
is left ``RUNNING`` with a live lease and nothing recorded -- which is exactly
what a ``SIGKILL`` leaves behind, and exactly what the lease exists to recover
from. Killing the worker with an ordinary exception would test the error
handler and call it a crash.

**GitHub is a fake that remembers.** The stand-in in ``test_triage.py`` answers
questions and forgets them, which is right for testing one call. Here the
question is what the pull request *looks like* after a crash and a recovery, and
that cannot be asked of a fake with no state.

**The clock is an argument, so the lease expires without anyone waiting.** The
whole file runs in milliseconds and is exact: nothing sleeps, nothing polls, and
there is no timing window that could make a failure intermittent.

The suite found one defect, and it is the reason
:meth:`~ci_triage.budget.Ledger.reclaim` exists: a worker killed between
reserving and settling left its hold standing for ever, so a run could go broke
having spent nothing. ``test_a_ledger_that_never_reclaims...`` is that bug,
preserved as a control case.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from ci_triage.budget import COMMITTED, HELD, RECLAIMED, Ledger
from ci_triage.estimate import CallPlan
from ci_triage.github import GitHubClient, marker_for
from ci_triage.idempotency import DUPLICATE_COMPLETED, IdempotencyStore
from ci_triage.money import dollars_to_micros
from ci_triage.pricing import ModelPrice
from ci_triage.runs import (
    DEAD_LETTER,
    DEAD_LETTERED,
    PENDING,
    RECORDED,
    RUNNING,
    SUCCEEDED,
    Backoff,
    JobStore,
    no_jitter,
    process_next,
)
from ci_triage.schema import reservations
from ci_triage.triage import Diagnosis, make_handler

API = "https://api.github.com"
RUN_KEY = "muhzuhaib/ci-triage#42.1"
EVENT_KEY = "workflow_run:muhzuhaib/ci-triage:42:1:completed"
MARKER = marker_for(RUN_KEY)

T0 = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)
LEASE = timedelta(seconds=60)
EXACT = Backoff(base_seconds=2.0, cap_seconds=300.0, jitter=no_jitter)

CEILING = dollars_to_micros("0.05")
COST = 1_200

PRICE = ModelPrice(
    model="test-model",
    provider="test",
    input_per_mtok=Decimal("1.00"),
    output_per_mtok=Decimal("5.00"),
    chars_per_token=Decimal("1.18"),
    source="test",
)

LOG = "".join(
    f"2026-08-06T11:12:5{i % 10}.460749{i}Z line {i} of a build that failed\n"
    for i in range(400)
)

FAILED_JOB = {"id": 1, "name": "tests (3.13)", "conclusion": "failure", "head_sha": "abc123"}


class WorkerKilled(BaseException):
    """The process died here.

    A ``BaseException`` on purpose: see the module docstring. If this were an
    ``Exception`` the state machine would catch it, schedule a polite retry, and
    the test would be exercising the error path it was written to bypass.
    """


# --------------------------------------------------------------- the fake API


class FakeGitHub:
    """A GitHub that keeps what was posted to it, and can die mid-request.

    ``die_after`` is a substring matched against ``"<METHOD> <path>"``. The
    request is served in full *before* the kill, because the interesting crash
    is the one where the server did the work and the client never found out --
    a comment that exists on the pull request and a worker that cannot know it.
    """

    def __init__(self, *, log: str = LOG, jobs=None, pulls=None) -> None:
        self.log = log
        self.jobs = [FAILED_JOB] if jobs is None else jobs
        self.pulls = [{"number": 7}] if pulls is None else pulls
        self.comments: list[dict] = []
        self.requests: list[str] = []
        self.die_after: str | None = None
        self._next_id = 100

    def client(self) -> GitHubClient:
        return GitHubClient(
            "ghp_test", client=httpx.Client(transport=httpx.MockTransport(self._handle))
        )

    def tagged(self, marker: str = MARKER) -> list[dict]:
        """The comments carrying our marker. There must never be two."""
        return [c for c in self.comments if marker in c["body"]]

    def count(self, needle: str) -> int:
        return sum(1 for r in self.requests if needle in r)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        response = self._serve(request)
        where = f"{request.method} {request.url.path}"
        self.requests.append(where)
        if self.die_after is not None and self.die_after in where:
            self.die_after = None  # the worker that replaces this one must get through
            raise WorkerKilled(where)
        return response

    def _serve(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": self.jobs})
        if "/actions/jobs/" in path and path.endswith("/logs"):
            return httpx.Response(200, text=self.log)
        if path.endswith("/pulls"):
            return httpx.Response(200, json=self.pulls)
        if method == "GET" and path.endswith("/comments"):
            return httpx.Response(200, json=list(self.comments))
        if method == "POST" and path.endswith("/comments"):
            self._next_id += 1
            comment = {
                "id": self._next_id,
                "body": json.loads(request.content)["body"],
                "html_url": f"{API}/c/{self._next_id}",
            }
            self.comments.append(comment)
            return httpx.Response(201, json=comment)
        if method == "PATCH":
            wanted = int(path.rsplit("/", 1)[-1])
            for comment in self.comments:
                if comment["id"] == wanted:
                    comment["body"] = json.loads(request.content)["body"]
                    return httpx.Response(200, json=comment)
            return httpx.Response(404, text="no such comment")
        raise AssertionError(f"unexpected {method} {path}")  # pragma: no cover


# ------------------------------------------------------------- the fake worker


class CrashingLedger(Ledger):
    """A ledger whose process dies at one named seam, once.

    Once, because a crash that repeats for ever tests nothing but the retry
    limit. Clearing ``die_on`` as it fires is what lets the same object serve
    the worker that recovers.
    """

    def __init__(self, engine, *, die_on: str | None = None) -> None:
        super().__init__(engine)
        self.die_on = die_on

    def _die(self, where: str) -> None:
        if self.die_on == where:
            self.die_on = None
            raise WorkerKilled(where)

    def reserve(self, *args, **kwargs):  # type: ignore[override]
        reservation = super().reserve(*args, **kwargs)
        self._die("after-reserve")
        return reservation

    def commit(self, reservation, actual_micros: int) -> None:  # type: ignore[override]
        self._die("before-commit")
        super().commit(reservation, actual_micros)
        self._die("after-commit")


class LeakyLedger(CrashingLedger):
    """The ledger as it was before this file existed: a hold is never taken back.

    Kept as the control case. See
    ``test_a_ledger_that_never_reclaims_goes_broke_having_spent_nothing``.
    """

    def reclaim(self, run_id: str, *, before_attempt: int) -> int:  # type: ignore[override]
        return 0


def cheap_diagnosis(prompt: str, plan: CallPlan) -> Diagnosis:
    return Diagnosis(text="The build failed at line 399.", cost_micros=COST)


def work(
    store: JobStore,
    ledger: Ledger,
    github: FakeGitHub,
    *,
    worker: str,
    now: datetime,
    diagnose=cheap_diagnosis,
    idempotency: IdempotencyStore | None = None,
    die_after_handler: bool = False,
):
    """One turn of a worker that may not survive it.

    Returns the outcome, or ``None`` when the process died: the job is then left
    ``RUNNING`` with a live lease and no result, which is the state a killed
    worker really leaves and the one the lease is there to clean up.
    """
    handler = make_handler(
        client=github.client(), ledger=ledger, price=PRICE, diagnose=diagnose
    )
    if die_after_handler:
        inner = handler

        def handler(job):  # type: ignore[misc]
            inner(job)
            # Everything is done and paid for. The process dies before the one
            # write that would say so.
            raise WorkerKilled("after-handler")

    try:
        return process_next(
            store, handler, worker=worker, idempotency=idempotency, now=now
        )
    except WorkerKilled:
        return None


@pytest.fixture()
def store(engine, ledger):
    ledger.open_run(RUN_KEY, CEILING)
    queue = JobStore(engine, backoff=EXACT, lease_seconds=LEASE.total_seconds())
    queue.enqueue(EVENT_KEY, RUN_KEY, max_attempts=8, now=T0)
    return queue


def held_reservations(engine) -> list:
    with engine.begin() as conn:
        return conn.execute(
            select(reservations).where(reservations.c.state == HELD)
        ).all()


# ------------------------------------------------------- a worker that is killed


def test_a_worker_killed_after_reserving_gives_the_money_back_on_the_next_attempt(
    engine, ledger, store
):
    """The defect this file was written to find, and the fix for it.

    The hold is real while the dead worker's lease is: nothing can tell it apart
    from a call still in flight, and guessing would be worse than waiting. What
    must not happen is that it outlives the attempt that took it.
    """
    github = FakeGitHub()

    assert work(store, CrashingLedger(engine, die_on="after-reserve"), github,
                worker="doomed", now=T0) is None

    crashed = ledger.spend(RUN_KEY)
    assert crashed.reserved_micros > 0, "the dead worker's hold is still standing"
    assert crashed.spent_micros == 0, "and nothing was ever bought with it"

    outcome = work(store, Ledger(engine), github, worker="replacement", now=T0 + LEASE)

    assert outcome is not None and outcome.outcome == RECORDED
    settled = ledger.spend(RUN_KEY)
    assert settled.reserved_micros == 0, "the abandoned hold was reclaimed"
    assert settled.spent_micros == COST, "and only the real call was charged"
    assert settled.remaining_micros == CEILING - COST
    assert len(github.tagged()) == 1


def test_a_ledger_that_never_reclaims_goes_broke_having_spent_nothing(engine, ledger, store):
    """The control case. If this stops failing, the fix above has stopped mattering.

    Each crash leaves behind a hold sized to what the run could afford at the
    time, so the ceiling ratchets down attempt by attempt while ``spent`` stays
    at zero. The end state is the one that makes this worth a test rather than a
    comment: the job is dead-lettered for want of money, and the money is all
    still there.
    """
    github = FakeGitHub()
    leaky = LeakyLedger(engine)

    outcome = None
    moment = T0
    for _ in range(8):
        leaky.die_on = "after-reserve"
        outcome = work(store, leaky, github, worker="doomed", now=moment)
        moment += LEASE
        if outcome is not None and outcome.outcome == DEAD_LETTERED:
            break

    assert outcome is not None and outcome.outcome == DEAD_LETTERED
    spend = ledger.spend(RUN_KEY)
    assert spend.spent_micros == 0, "the run is broke and has bought nothing"
    assert spend.reserved_micros > 0, "every penny of the ceiling is held by a dead worker"
    assert len(github.tagged()) == 0, "and there is no diagnosis to show for it"
    # Buried for want of money that is all still there, which is the shape of
    # this bug: nothing throws, nothing is logged, the service simply stops
    # being able to answer and the ledger says it never spent anything.
    assert "output tokens cost" in store.by_key(EVENT_KEY).last_error


def test_a_call_that_lands_after_its_hold_was_reclaimed_is_charged_as_an_overrun(
    engine, ledger, store
):
    """A worker can be paused rather than dead, and then pay after being replaced.

    Reclaiming makes that possible, so the cost has to go somewhere. It goes in
    as an overrun, which is the ledger's existing word for money that was really
    spent outside what the ceiling authorised. The alternative -- dropping it,
    because the hold that would have covered it is gone -- would make the books
    balance by understating the bill.
    """
    zombie = Ledger(engine)
    job = store.claim_next("paused", now=T0)
    reservation = zombie.reserve(RUN_KEY, 20_000, attempt=job.attempt, purpose="diagnose")

    # Its lease expires and the next attempt takes the hold back.
    ledger.reclaim(RUN_KEY, before_attempt=job.attempt + 1)
    assert ledger.spend(RUN_KEY).reserved_micros == 0

    # It wakes up, and its call had in fact reached the provider.
    zombie.commit(reservation, 9_000)

    spend = ledger.spend(RUN_KEY)
    assert spend.spent_micros == 9_000, "the money left the building; the ledger says so"
    assert spend.overrun_micros == 9_000, "all of it outside what the ceiling authorised"
    assert spend.reserved_micros == 0, "and the hold is not given back twice"
    with engine.begin() as conn:
        state = conn.execute(
            select(reservations.c.state).where(reservations.c.id == reservation.id)
        ).scalar_one()
    assert state == COMMITTED


def test_a_reclaimed_hold_stays_in_the_table_as_the_record_of_what_is_not_known(
    engine, ledger, store
):
    """The residual risk, made auditable instead of argued away.

    A worker killed between the provider answering and the commit landing spent
    money that nothing recorded, and no amount of design recovers a fact that
    died with the process. What the ledger can do is refuse to pretend: the hold
    is not deleted, it is marked ``reclaimed`` with the attempt that took it, so
    every point at which the run may have paid without knowing is a row someone
    can go and look at. Reconciling against a provider invoice needs exactly
    this list and nothing else.
    """
    github = FakeGitHub()

    assert work(store, CrashingLedger(engine, die_on="before-commit"), github,
                worker="doomed", now=T0) is None
    work(store, Ledger(engine), github, worker="replacement", now=T0 + LEASE)

    with engine.begin() as conn:
        rows = conn.execute(
            select(reservations.c.state, reservations.c.attempt, reservations.c.held_micros)
            .where(reservations.c.run_id == RUN_KEY)
            .order_by(reservations.c.attempt)
        ).all()

    assert [r.state for r in rows] == [RECLAIMED, COMMITTED]
    assert [r.attempt for r in rows] == [1, 2]
    assert rows[0].held_micros > 0, "how much the unrecorded call could have cost"


def test_reclaim_leaves_the_running_attempt_s_own_hold_alone(engine, ledger, store):
    """Strictly earlier, or the fix would rob the worker it is protecting."""
    job = store.claim_next("worker-1", now=T0)
    live = ledger.reserve(RUN_KEY, 5_000, attempt=job.attempt, purpose="diagnose")

    assert ledger.reclaim(RUN_KEY, before_attempt=job.attempt) == 0
    assert ledger.spend(RUN_KEY).reserved_micros == 5_000

    ledger.commit(live, 4_000)
    assert ledger.spend(RUN_KEY).overrun_micros == 0


@pytest.mark.parametrize(
    "seam",
    [
        "github:/attempts/1/jobs",  # before a penny is committed to anything
        "github:/logs",  # mid download
        "ledger:after-reserve",  # holding money, call not yet made
        "ledger:before-commit",  # answer bought, books not written
        "ledger:after-commit",  # paid and recorded, nothing posted
        "github:POST ",  # the comment exists; the worker never learns its id
        "handler:after",  # all done, and the job never marked done
    ],
)
def test_a_kill_at_any_seam_ends_with_one_comment_and_the_ceiling_intact(
    engine, ledger, store, seam
):
    """The sweep. Kill the worker at each seam in turn and check the promises.

    The promises are the README's, and they are checked as state rather than as
    return values: what the pull request holds, what the ledger totals, and
    whether the job is still in the queue. A worker that dies at any of these
    points must cost at most a repeated diagnosis, never a second comment and
    never a breached ceiling.
    """
    kind, where = seam.split(":", 1)
    github = FakeGitHub()
    crashing = CrashingLedger(engine, die_on=where if kind == "ledger" else None)
    if kind == "github":
        github.die_after = where

    assert work(
        store,
        crashing,
        github,
        worker="doomed",
        now=T0,
        die_after_handler=(kind == "handler"),
    ) is None

    job = store.by_key(EVENT_KEY)
    assert job.state == RUNNING, "a killed worker leaves the job leased, not lost"

    outcome = work(store, Ledger(engine), github, worker="replacement", now=T0 + LEASE)

    assert outcome is not None and outcome.outcome == RECORDED
    assert store.by_key(EVENT_KEY).state == SUCCEEDED
    assert len(github.tagged()) == 1, "one run, one comment, whatever died where"
    assert github.count("POST ") == 1, "recovery edits the first comment, never adds one"

    spend = ledger.spend(RUN_KEY)
    assert spend.committed_and_held <= CEILING, "the ceiling holds through the crash"
    assert spend.reserved_micros == 0, "and no hold outlived the attempt that took it"
    assert held_reservations(engine) == []
    # A crash after the provider was paid means the answer is bought twice: the
    # first purchase was never recorded, so nothing knows it happened. That is
    # the honest cost of the crash, and the ceiling is what bounds it.
    assert spend.spent_micros in (COST, 2 * COST)


# ------------------------------------------------------------------ redelivery


def test_a_redelivery_after_success_replays_instead_of_paying_again(engine, ledger, store):
    """GitHub redelivering a hook is not a decision to spend the budget again."""
    github = FakeGitHub()
    keys = IdempotencyStore(engine)
    keys.claim(EVENT_KEY, RUN_KEY)

    work(store, Ledger(engine), github, worker="worker-1", now=T0, idempotency=keys)

    assert ledger.spend(RUN_KEY).spent_micros == COST
    replay = keys.claim(EVENT_KEY, RUN_KEY)
    assert replay.outcome == DUPLICATE_COMPLETED
    assert "posted comment on #7" in replay.result
    assert store.enqueue(EVENT_KEY, RUN_KEY, now=T0 + LEASE) is None
    assert ledger.spend(RUN_KEY).spent_micros == COST
    assert len(github.tagged()) == 1


def test_a_redelivery_of_an_event_whose_worker_was_killed_still_costs_one_comment(
    engine, ledger, store
):
    """The nastiest ordinary case: a crash and a redelivery, together.

    The redelivery cannot enqueue a second job -- the idempotency key is a
    primary key -- so the crash is recovered by the lease rather than by the
    delivery, and the two do not compound.
    """
    github = FakeGitHub()
    keys = IdempotencyStore(engine)
    keys.claim(EVENT_KEY, RUN_KEY)

    github.die_after = "POST "
    assert work(store, Ledger(engine), github, worker="doomed", now=T0,
                idempotency=keys) is None

    # The redelivery arrives while the job is still leased to the dead worker.
    # It cannot enqueue a second job, and a worker acting on it finds nothing
    # runnable: the lease has not expired, so recovery is not yet anyone's to do.
    assert store.enqueue(EVENT_KEY, RUN_KEY, now=T0 + timedelta(seconds=5)) is None
    assert work(store, Ledger(engine), github, worker="eager",
                now=T0 + timedelta(seconds=5)) is None

    outcome = work(store, Ledger(engine), github, worker="replacement",
                   now=T0 + LEASE, idempotency=keys)

    assert outcome is not None and outcome.outcome == RECORDED
    assert len(github.tagged()) == 1
    assert github.count("POST ") == 1
    assert ledger.spend(RUN_KEY).committed_and_held <= CEILING


# ---------------------------------------------------------------- provider 429


class ProviderRateLimited(Exception):
    """What a provider saying "not now" looks like to the state machine.

    Deliberately not one of this package's exception types. The scheduler reads
    ``retry_after_seconds`` off whatever it catches, by attribute, so anyone's
    client can carry the server's instruction without the queue growing a
    dependency on it. That claim is only worth anything if something outside the
    package proves it, which is what this class is for.
    """

    def __init__(self, message: str, *, retry_after_seconds: float) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def test_a_rate_limited_attempt_waits_as_long_as_asked_and_holds_no_money(
    engine, ledger, store
):
    def limited(prompt: str, plan: CallPlan) -> Diagnosis:
        raise ProviderRateLimited("429 too many requests", retry_after_seconds=900)

    outcome = work(store, Ledger(engine), FakeGitHub(), worker="worker-1",
                   now=T0, diagnose=limited)

    assert outcome is not None
    job = store.by_key(EVENT_KEY)
    assert (job.next_attempt_at - T0).total_seconds() >= 900
    spend = ledger.spend(RUN_KEY)
    assert spend.reserved_micros == 0, "a hold must not survive the call it was for"
    assert spend.spent_micros == 0, "and a refused call is not a purchase"
    assert held_reservations(engine) == []


def test_a_provider_that_never_recovers_is_buried_with_the_ceiling_untouched(
    engine, ledger, store
):
    """Four attempts, four rate limits, and the run has still bought nothing.

    The point is the last line: burying a job must not be a way of losing the
    money it was holding.
    """
    def limited(prompt: str, plan: CallPlan) -> Diagnosis:
        raise ProviderRateLimited("429 too many requests", retry_after_seconds=30)

    github = FakeGitHub()
    moment = T0
    outcome = None
    for _ in range(8):
        outcome = work(store, Ledger(engine), github, worker="worker-1",
                       now=moment, diagnose=limited)
        moment += timedelta(seconds=931)
        if outcome is not None and outcome.outcome == DEAD_LETTERED:
            break

    assert outcome is not None and outcome.outcome == DEAD_LETTERED
    assert store.by_key(EVENT_KEY).state == DEAD_LETTER
    assert ledger.spend(RUN_KEY).remaining_micros == CEILING
    assert len(github.tagged()) == 0

    # And the way back is a replay, which grants attempts and no money.
    revived = store.replay(store.by_key(EVENT_KEY).id, now=moment)
    assert revived.state == PENDING
    assert ledger.spend(RUN_KEY).remaining_micros == CEILING


# --------------------------------------------------------------- oversized logs


def test_a_log_far_larger_than_the_ceiling_is_truncated_rather_than_refused(
    engine, ledger, store
):
    """Six megabytes of log against five cents of budget.

    The service never asks whether it can afford this log, which would make the
    answer depend on how noisy someone else's build was. It asks how much log it
    can afford, which always has one.
    """
    huge = "".join(
        f"2026-08-06T11:12:5{i % 10}.4607491Z line {i} of a very noisy build\n"
        for i in range(100_000)
    )
    assert len(huge) > 6_000_000
    seen: dict = {}

    def measuring(prompt: str, plan: CallPlan) -> Diagnosis:
        seen["prompt"] = prompt
        seen["plan"] = plan
        return Diagnosis("The build failed at the end.", cost_micros=COST)

    outcome = work(store, Ledger(engine), FakeGitHub(log=huge), worker="worker-1",
                   now=T0, diagnose=measuring)

    assert outcome is not None and outcome.outcome == RECORDED
    assert seen["plan"].worst_case_micros <= CEILING
    assert len(seen["prompt"]) < len(huge) / 100, "the log was cut to fit, not sent"
    assert "line 99999" in seen["prompt"], "and it is the tail that survived"
    assert ledger.spend(RUN_KEY).committed_and_held == COST


def test_an_oversized_log_after_a_crash_fits_the_budget_that_is_actually_left(
    engine, ledger, store
):
    """The two failures interact: the affordable size is recomputed, not cached.

    A crash leaves less headroom until it is reclaimed, and the truncation has
    to be sized against what the run can really afford at the moment it happens.
    """
    huge = "".join(f"2026-08-06T11:12:50.4607491Z noisy line {i}\n" for i in range(40_000))
    github = FakeGitHub(log=huge)
    plans: list[CallPlan] = []

    def measuring(prompt: str, plan: CallPlan) -> Diagnosis:
        plans.append(plan)
        return Diagnosis("The build failed at the end.", cost_micros=COST)

    spent = ledger.reserve(RUN_KEY, dollars_to_micros("0.03"), purpose="earlier")
    ledger.commit(spent, dollars_to_micros("0.03"))

    assert work(store, CrashingLedger(engine, die_on="after-reserve"), github,
                worker="doomed", now=T0, diagnose=measuring) is None
    outcome = work(store, Ledger(engine), github, worker="replacement",
                   now=T0 + LEASE, diagnose=measuring)

    assert outcome is not None and outcome.outcome == RECORDED
    assert len(plans) == 1, "the crashed attempt died before it reached the provider"
    assert plans[0].worst_case_micros <= CEILING - dollars_to_micros("0.03")
    assert ledger.spend(RUN_KEY).committed_and_held <= CEILING
