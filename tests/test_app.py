"""The ASGI binding: status codes, and the two writes that admit a failure.

The receiver's own decisions are tested in ``test_webhook.py``; what is worth
testing here is everything that only exists once it is bound to HTTP -- which
status code each outcome becomes, and whether the queue really has a job after
a 202.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from ci_triage.app import ConfigError, Settings, create_app
from ci_triage.budget import Ledger
from ci_triage.runs import JobStore
from ci_triage.schema import create_engine_for, triage_jobs
from ci_triage.signature import compute_signature

SECRET = "hush"


def _payload(*, action="completed", conclusion="failure", run_id=42, run_attempt=1):
    return {
        "action": action,
        "workflow_run": {
            "id": run_id,
            "run_attempt": run_attempt,
            "conclusion": conclusion,
            "name": "CI",
        },
        "repository": {"full_name": "octo/repo", "id": 7},
    }


def _delivery(payload, *, secret=SECRET, event="workflow_run", delivery="d-1", sign=True):
    body = json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery}
    if sign:
        headers["X-Hub-Signature-256"] = compute_signature(secret, body)
    return body, headers


@pytest.fixture()
def settings():
    return Settings(
        webhook_secret=SECRET,
        database_url="sqlite://",  # unused: the engine is injected
        ceiling_micros=40_000,
        max_attempts=3,
        create_schema=False,
    )


@pytest.fixture()
def client(settings, engine):
    with TestClient(create_app(settings, engine=engine)) as c:
        yield c


def _post(client, body, headers):
    return client.post("/webhook", content=body, headers=headers)


# ------------------------------------------------------------------ admission


def test_a_failing_run_is_accepted_and_queued(client, engine):
    body, headers = _delivery(_payload())
    response = _post(client, body, headers)

    assert response.status_code == 202
    payload = response.json()
    assert payload["outcome"] == "accepted"
    assert payload["run"] == "octo/repo#42.1"
    assert payload["job_id"]

    job = JobStore(engine).by_key("workflow_run:octo/repo:42:1:completed")
    assert job is not None
    assert job.id == payload["job_id"]
    assert job.run_id == "octo/repo#42.1"


def test_the_run_budget_is_opened_at_the_ceiling_from_settings(client, engine):
    body, headers = _delivery(_payload())
    _post(client, body, headers)

    spend = Ledger(engine).spend("octo/repo#42.1")
    assert spend.ceiling_micros == 40_000
    assert spend.spent_micros == 0


def test_a_redelivery_is_two_hundred_and_queues_nothing_new(client, engine):
    body, headers = _delivery(_payload())
    first = _post(client, body, headers)
    second = _post(client, body, headers)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["outcome"] == "duplicate"

    with engine.begin() as conn:
        rows = conn.execute(select(triage_jobs.c.id)).all()
    assert len(rows) == 1


def test_a_rerun_of_the_same_workflow_run_is_a_separate_job(client, engine):
    for attempt in (1, 2):
        body, headers = _delivery(_payload(run_attempt=attempt))
        assert _post(client, body, headers).status_code == 202

    with engine.begin() as conn:
        runs = {row.run_id for row in conn.execute(select(triage_jobs.c.run_id))}
    assert runs == {"octo/repo#42.1", "octo/repo#42.2"}


def test_a_duplicate_repairs_a_claim_that_never_got_its_job(client, engine):
    """The crash between the two writes, and the reason it is not branched on.

    A process killed after claiming the idempotency key but before enqueueing
    leaves the delivery remembered as seen and nothing queued. GitHub's
    redelivery is the only thing that comes back, and it arrives as a duplicate,
    so a handler that skipped the enqueue for duplicates would drop the failure
    for good. Here the claim is made by hand and the job is not, which is exactly
    the state that crash leaves behind.
    """
    from ci_triage.idempotency import IdempotencyStore

    key = "workflow_run:octo/repo:42:1:completed"
    IdempotencyStore(engine).claim(key, "octo/repo#42.1")
    assert JobStore(engine).by_key(key) is None

    body, headers = _delivery(_payload())
    response = _post(client, body, headers)

    assert response.status_code == 200  # honest: this is a duplicate delivery
    assert response.json()["outcome"] == "duplicate"
    assert JobStore(engine).by_key(key) is not None


# ------------------------------------------------------- what is not admitted


def test_an_unsigned_delivery_is_rejected_and_queues_nothing(client, engine):
    body, headers = _delivery(_payload(), sign=False)
    response = _post(client, body, headers)

    assert response.status_code == 401
    with engine.begin() as conn:
        assert conn.execute(select(triage_jobs.c.id)).all() == []


def test_a_delivery_signed_with_the_wrong_secret_is_rejected(client):
    body, headers = _delivery(_payload(), secret="not-the-secret")
    assert _post(client, body, headers).status_code == 401


def test_an_authentic_body_that_is_not_json_is_four_hundred(client):
    body = b"{not json"
    headers = {
        "X-GitHub-Event": "workflow_run",
        "X-Hub-Signature-256": compute_signature(SECRET, body),
    }
    assert _post(client, body, headers).status_code == 400


def test_a_green_run_is_acknowledged_rather_than_errored(client, engine):
    """GitHub redelivers on any non-2xx, so 'not for us' has to be a success."""
    body, headers = _delivery(_payload(conclusion="success"))
    response = _post(client, body, headers)

    assert response.status_code == 200
    assert response.json()["outcome"] == "ignored"
    with engine.begin() as conn:
        assert conn.execute(select(triage_jobs.c.id)).all() == []


def test_an_event_we_do_not_subscribe_to_is_acknowledged(client):
    body, headers = _delivery(_payload(), event="push")
    response = _post(client, body, headers)

    assert response.status_code == 200
    assert response.json()["outcome"] == "ignored"


# ------------------------------------------------------------------- healthz


def test_healthz_reports_ok_against_a_live_database(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_healthz_reports_unhealthy_when_the_database_is_unreachable(settings, tmp_path):
    """A static 200 would let an orchestrator keep routing to a broken node.

    The engine points at a SQLite file inside a directory that does not exist,
    which fails on connect rather than on construction: an engine is lazy, so a
    health check that only asked whether one had been built would report ok.
    """
    unreachable = create_engine_for(f"sqlite:///{tmp_path / 'gone' / 'test.db'}")
    with TestClient(create_app(settings, engine=unreachable)) as client:
        response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json()["status"] == "unhealthy"


# ------------------------------------------------------------------ settings


def test_settings_read_the_environment():
    settings = Settings.from_env(
        {
            "CI_TRIAGE_WEBHOOK_SECRET": "s",
            "CI_TRIAGE_DATABASE_URL": "postgresql+psycopg://x/y",
            "CI_TRIAGE_CEILING_USD": "0.25",
            "CI_TRIAGE_MAX_ATTEMPTS": "5",
            "CI_TRIAGE_CREATE_SCHEMA": "1",
        }
    )
    assert settings.ceiling_micros == 250_000
    assert settings.max_attempts == 5
    assert settings.create_schema is True


def test_settings_default_the_optional_values():
    settings = Settings.from_env(
        {"CI_TRIAGE_WEBHOOK_SECRET": "s", "CI_TRIAGE_DATABASE_URL": "sqlite://"}
    )
    assert settings.ceiling_micros == 40_000
    assert settings.max_attempts == 3
    assert settings.create_schema is False


@pytest.mark.parametrize(
    "env",
    [
        {"CI_TRIAGE_DATABASE_URL": "sqlite://"},  # no secret
        {"CI_TRIAGE_WEBHOOK_SECRET": ""},  # empty secret, which is forgeable
        {"CI_TRIAGE_WEBHOOK_SECRET": "s"},  # no database
        {"CI_TRIAGE_WEBHOOK_SECRET": "s", "CI_TRIAGE_DATABASE_URL": "sqlite://",
         "CI_TRIAGE_CEILING_USD": "0"},
        {"CI_TRIAGE_WEBHOOK_SECRET": "s", "CI_TRIAGE_DATABASE_URL": "sqlite://",
         "CI_TRIAGE_CEILING_USD": "free"},
        {"CI_TRIAGE_WEBHOOK_SECRET": "s", "CI_TRIAGE_DATABASE_URL": "sqlite://",
         "CI_TRIAGE_MAX_ATTEMPTS": "0"},
    ],
)
def test_an_unusable_environment_fails_at_startup_not_per_request(env):
    """Boot loudly. A receiver that starts without a secret looks healthy and
    rejects every delivery for the rest of the day."""
    with pytest.raises(ConfigError):
        Settings.from_env(env)
