"""The handler, end to end: a failed run in, a comment out, under the ceiling.

The GitHub API is replaced by an ``httpx.MockTransport`` and the provider by a
callable that returns whatever the test needs it to cost. Everything between the
two -- the budget arithmetic, the truncation, the reservation, the settlement and
the comment -- is the real code.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from ci_triage.budget import Ledger
from ci_triage.estimate import CallPlan
from ci_triage.github import GitHubClient
from ci_triage.money import dollars_to_micros
from ci_triage.pricing import ModelPrice
from ci_triage.runs import DEAD_LETTERED, JobStore, RECORDED, TerminalError, process_next
from ci_triage.triage import Diagnosis, make_handler, run_triage

API = "https://api.github.com"
RUN_KEY = "muhzuhaib/ci-triage#42.1"

PRICE = ModelPrice(
    model="test-model",
    provider="test",
    input_per_mtok=Decimal("1.00"),
    output_per_mtok=Decimal("5.00"),
    chars_per_token=Decimal("1.18"),
    source="test",
)

LOG = "".join(
    f"2026-07-29T23:12:5{i % 10}.460749{i}Z line {i} of a build that failed\n"
    for i in range(400)
)


def api_handler(
    *,
    jobs=None,
    log=LOG,
    pulls=None,
    comments=None,
    record=None,
):
    """A stand-in GitHub, closed over what this test wants it to say."""
    jobs = jobs if jobs is not None else [
        {"id": 1, "name": "tests (3.13)", "conclusion": "failure", "head_sha": "abc123"}
    ]
    pulls = pulls if pulls is not None else [{"number": 7}]
    comments = comments if comments is not None else []
    record = record if record is not None else {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": jobs})
        if "/actions/jobs/" in path and path.endswith("/logs"):
            return httpx.Response(200, text=log)
        if path.endswith("/pulls"):
            return httpx.Response(200, json=pulls)
        if request.method == "GET" and path.endswith("/comments"):
            return httpx.Response(200, json=comments)
        if request.method == "POST":
            record["created"] = json.loads(request.content)["body"]
            return httpx.Response(201, json={"id": 99, "html_url": f"{API}/c/99"})
        if request.method == "PATCH":
            record["patched"] = json.loads(request.content)["body"]
            return httpx.Response(200, json={"id": 55, "html_url": f"{API}/c/55"})
        raise AssertionError(f"unexpected {request.method} {path}")  # pragma: no cover

    return handler


def client_for(handler) -> GitHubClient:
    return GitHubClient(
        "ghp_test", client=httpx.Client(transport=httpx.MockTransport(handler))
    )


@pytest.fixture()
def job(engine, ledger):
    ledger.open_run(RUN_KEY, dollars_to_micros("0.05"))
    store = JobStore(engine)
    return store.enqueue("workflow_run:muhzuhaib/ci-triage:42:1:completed", RUN_KEY)


def cheap_diagnosis(prompt: str, plan: CallPlan) -> Diagnosis:
    return Diagnosis(text="The build failed at line 399.", cost_micros=1_200)


def test_a_triage_posts_one_comment_and_settles_the_ledger(job, ledger):
    record = {}

    outcome = run_triage(
        job,
        client=client_for(api_handler(record=record)),
        ledger=ledger,
        price=PRICE,
        diagnose=cheap_diagnosis,
    )

    assert "posted comment on #7" in outcome.summary
    assert "The build failed at line 399." in record["created"]
    spend = ledger.spend(RUN_KEY)
    assert spend.spent_micros == 1_200
    assert spend.reserved_micros == 0  # the hold was settled, not left standing


def test_the_prompt_is_bounded_by_what_the_run_can_still_afford(job, ledger):
    """The ceiling decides the prompt size, not the other way round."""
    seen = {}

    def measuring_diagnosis(prompt: str, plan: CallPlan) -> Diagnosis:
        seen["prompt"] = prompt
        seen["plan"] = plan
        return Diagnosis("ok", cost_micros=10)

    # Leave only a sliver of the ceiling unspent, so the affordable log is small.
    reservation = ledger.reserve(RUN_KEY, dollars_to_micros("0.045"), purpose="earlier")
    ledger.commit(reservation, dollars_to_micros("0.045"))

    run_triage(
        job,
        client=client_for(api_handler()),
        ledger=ledger,
        price=PRICE,
        diagnose=measuring_diagnosis,
    )

    assert seen["plan"].worst_case_micros <= dollars_to_micros("0.005")
    assert "line 399" in seen["prompt"]  # the tail survived
    assert "line 0 of" not in seen["prompt"]  # the provisioning did not
    assert "2026-07-29T" not in seen["prompt"]  # nor did the timestamps


def test_the_comment_says_how_much_of_the_log_was_read(job, ledger):
    """A truncated log is the likeliest reason for a confident wrong answer.

    So the footer reports what the model actually saw, and it reports the log
    rather than the prompt: counting the headers and the truncation note would
    let it claim more characters than the log contained, which is the one number
    a reader is entitled to trust.
    """
    record = {}
    spent = ledger.reserve(RUN_KEY, dollars_to_micros("0.046"), purpose="earlier")
    ledger.commit(spent, dollars_to_micros("0.046"))

    run_triage(
        job,
        client=client_for(api_handler(record=record)),
        ledger=ledger,
        price=PRICE,
        diagnose=cheap_diagnosis,
    )

    assert "log characters" in record["created"]
    assert "earlier lines dropped" in record["created"]
    kept, original = (
        int(n.replace(",", ""))
        for n in re.search(r"from ([\d,]+) of ([\d,]+) log characters", record["created"]).groups()
    )
    assert 0 < kept < original


def test_a_provider_failure_releases_the_hold_rather_than_charging_for_it(job, ledger):
    def exploding(prompt: str, plan: CallPlan) -> Diagnosis:
        raise RuntimeError("provider returned 500")

    with pytest.raises(RuntimeError):
        run_triage(
            job,
            client=client_for(api_handler()),
            ledger=ledger,
            price=PRICE,
            diagnose=exploding,
        )

    spend = ledger.spend(RUN_KEY)
    assert spend.spent_micros == 0
    assert spend.reserved_micros == 0
    assert spend.remaining_micros == dollars_to_micros("0.05")


def test_a_second_delivery_edits_the_first_comment(job, ledger):
    """The marker is what makes a repeat an edit instead of a second opinion."""
    record = {}
    first = {}

    run_triage(
        job,
        client=client_for(api_handler(record=first)),
        ledger=ledger,
        price=PRICE,
        diagnose=cheap_diagnosis,
    )
    existing = [{"id": 55, "body": first["created"]}]

    outcome = run_triage(
        job,
        client=client_for(api_handler(comments=existing, record=record)),
        ledger=ledger,
        price=PRICE,
        diagnose=cheap_diagnosis,
    )

    assert "updated comment on #7" in outcome.summary
    assert "created" not in record
    assert "patched" in record


def test_a_run_with_no_pull_request_keeps_the_answer_it_paid_for(job, ledger):
    outcome = run_triage(
        job,
        client=client_for(api_handler(pulls=[])),
        ledger=ledger,
        price=PRICE,
        diagnose=cheap_diagnosis,
    )

    assert "not posted" in outcome.summary
    assert outcome.diagnosis == "The build failed at line 399."
    # Paid for, so it is recorded rather than thrown away and bought again.
    assert ledger.spend(RUN_KEY).spent_micros == 1_200


def test_a_run_whose_jobs_all_passed_spends_nothing(job, ledger):
    outcome = run_triage(
        job,
        client=client_for(api_handler(jobs=[{"id": 1, "name": "t", "conclusion": "success"}])),
        ledger=ledger,
        price=PRICE,
        diagnose=cheap_diagnosis,
    )

    assert "no failed jobs" in outcome.summary
    assert ledger.spend(RUN_KEY).spent_micros == 0


def test_a_budget_too_small_for_any_answer_is_terminal(job, ledger):
    """Terminal, not retryable: the shortfall is on the output side.

    Truncating the log cannot fix a budget that cannot pay for the reply, so
    three attempts would be three delays and no diagnosis.
    """
    reservation = ledger.reserve(RUN_KEY, dollars_to_micros("0.0499"), purpose="earlier")
    ledger.commit(reservation, dollars_to_micros("0.0499"))

    with pytest.raises(TerminalError):
        run_triage(
            job,
            client=client_for(api_handler()),
            ledger=ledger,
            price=PRICE,
            diagnose=cheap_diagnosis,
        )


def test_a_run_id_that_does_not_parse_is_terminal(engine, ledger):
    store = JobStore(engine)
    ledger.open_run("nonsense", dollars_to_micros("0.05"))
    broken = store.enqueue("key", "nonsense")

    with pytest.raises(TerminalError):
        run_triage(
            broken,
            client=client_for(api_handler()),
            ledger=ledger,
            price=PRICE,
            diagnose=cheap_diagnosis,
        )


# ------------------------------------------- the handler inside the state machine


def test_the_state_machine_runs_the_handler_and_records_the_comment(engine, ledger):
    ledger.open_run(RUN_KEY, dollars_to_micros("0.05"))
    store = JobStore(engine)
    store.enqueue("workflow_run:muhzuhaib/ci-triage:42:1:completed", RUN_KEY)
    handler = make_handler(
        client=client_for(api_handler()),
        ledger=ledger,
        price=PRICE,
        diagnose=cheap_diagnosis,
    )

    result = process_next(store, handler, worker="worker-1")

    assert result is not None
    assert result.outcome == RECORDED
    assert "posted comment on #7" in store.get(result.job.id).result


def test_a_missing_repository_is_buried_without_burning_three_attempts(engine, ledger):
    ledger.open_run(RUN_KEY, dollars_to_micros("0.05"))
    store = JobStore(engine)
    store.enqueue("workflow_run:muhzuhaib/ci-triage:42:1:completed", RUN_KEY)

    def gone(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found")

    handler = make_handler(
        client=client_for(gone), ledger=ledger, price=PRICE, diagnose=cheap_diagnosis
    )

    result = process_next(store, handler, worker="worker-1")

    assert result is not None
    assert result.outcome == DEAD_LETTERED
    assert store.get(result.job.id).attempt == 1


def test_a_rate_limit_reschedules_for_when_the_server_said(engine, ledger):
    """The server's retry-after wins over the local backoff schedule.

    GitHub documents that continuing to call while limited can get an
    integration banned, so a computed delay that happens to be shorter is not an
    opinion worth having.
    """
    moment = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    ledger.open_run(RUN_KEY, dollars_to_micros("0.05"))
    store = JobStore(engine)
    store.enqueue("workflow_run:muhzuhaib/ci-triage:42:1:completed", RUN_KEY, now=moment)

    def limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "900"}, text="slow down")

    handler = make_handler(
        client=client_for(limited), ledger=ledger, price=PRICE, diagnose=cheap_diagnosis
    )

    result = process_next(store, handler, worker="worker-1", now=moment)

    assert result is not None
    job = store.get(result.job.id)
    # The computed backoff for attempt 1 is ~2 seconds. The server asked for 900,
    # and the scheduler has to take the larger, not the one it worked out itself.
    assert (job.next_attempt_at - moment).total_seconds() >= 900
