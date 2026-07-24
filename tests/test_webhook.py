"""The receiver: authenticate, filter to actionable failures, deduplicate."""

from __future__ import annotations

import json

import pytest

from ci_triage.idempotency import IdempotencyStore
from ci_triage.signature import InvalidSignature, MissingSignature, compute_signature
from ci_triage.webhook import (
    ACCEPTED,
    DUPLICATE,
    IGNORED,
    WebhookError,
    WebhookReceiver,
)

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
    headers = {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
    }
    if sign:
        headers["X-Hub-Signature-256"] = compute_signature(secret, body)
    return body, headers


@pytest.fixture()
def receiver(engine):
    return WebhookReceiver(SECRET, IdempotencyStore(engine))


def test_a_failing_run_is_accepted(receiver):
    body, headers = _delivery(_payload())
    result = receiver.receive(body, headers)

    assert result.outcome == ACCEPTED
    assert result.should_process
    assert result.event is not None
    assert result.event.repo == "octo/repo"
    assert result.event.run_id == 42
    assert result.event.conclusion == "failure"
    assert result.event.ledger_run_id == "octo/repo#42.1"


def test_a_timed_out_run_is_also_actionable(receiver):
    body, headers = _delivery(_payload(conclusion="timed_out"))
    assert receiver.receive(body, headers).outcome == ACCEPTED


def test_a_successful_run_is_ignored(receiver):
    body, headers = _delivery(_payload(conclusion="success"))
    result = receiver.receive(body, headers)
    assert result.outcome == IGNORED
    assert not result.should_process


def test_a_non_completed_action_is_ignored(receiver):
    body, headers = _delivery(_payload(action="requested", conclusion=None))
    assert receiver.receive(body, headers).outcome == IGNORED


def test_an_unsubscribed_event_is_ignored(receiver):
    # A push event is authentic but not something we triage. Ignored, not error,
    # so GitHub does not retry it.
    body, headers = _delivery({"pushed": True}, event="push")
    assert receiver.receive(body, headers).outcome == IGNORED


def test_a_redelivery_is_a_duplicate_even_with_a_new_delivery_id(receiver):
    body, headers = _delivery(_payload(), delivery="first")
    assert receiver.receive(body, headers).outcome == ACCEPTED

    # GitHub redelivers the same event under a *new* X-GitHub-Delivery GUID.
    body2, headers2 = _delivery(_payload(), delivery="second")
    result = receiver.receive(body2, headers2)
    assert result.outcome == DUPLICATE


def test_a_redelivery_after_completion_replays_the_prior_result(receiver, engine):
    body, headers = _delivery(_payload())
    first = receiver.receive(body, headers)
    # Simulate the run finishing and recording its outcome.
    IdempotencyStore(engine).complete(first.event.idempotency_key, result="comment#99")

    body2, headers2 = _delivery(_payload(), delivery="second")
    result = receiver.receive(body2, headers2)
    assert result.outcome == DUPLICATE
    assert result.prior_result == "comment#99"


def test_a_rerun_is_not_deduped_against_the_original(receiver):
    # Same workflow_run.id, incremented run_attempt: a genuine re-run that must
    # be triaged in its own right.
    body1, headers1 = _delivery(_payload(run_attempt=1), delivery="a")
    body2, headers2 = _delivery(_payload(run_attempt=2), delivery="b")

    assert receiver.receive(body1, headers1).outcome == ACCEPTED
    r2 = receiver.receive(body2, headers2)
    assert r2.outcome == ACCEPTED
    assert r2.event.ledger_run_id == "octo/repo#42.2"


def test_a_bad_signature_raises_before_any_processing(receiver):
    body, headers = _delivery(_payload())
    headers["X-Hub-Signature-256"] = compute_signature("the wrong secret", body)
    with pytest.raises(InvalidSignature):
        receiver.receive(body, headers)


def test_a_missing_signature_raises(receiver):
    body, headers = _delivery(_payload(), sign=False)
    with pytest.raises(MissingSignature):
        receiver.receive(body, headers)


def test_a_body_that_does_not_match_its_signature_raises(receiver):
    body, headers = _delivery(_payload())
    tampered = body + b" "  # signature was computed over the original bytes
    with pytest.raises(InvalidSignature):
        receiver.receive(tampered, headers)


def test_authentic_but_malformed_json_is_a_webhook_error(engine):
    receiver = WebhookReceiver(SECRET, IdempotencyStore(engine))
    body = b"{not json"
    headers = {
        "X-GitHub-Event": "workflow_run",
        "X-Hub-Signature-256": compute_signature(SECRET, body),
    }
    with pytest.raises(WebhookError):
        receiver.receive(body, headers)


def test_authentic_workflow_run_missing_fields_is_a_webhook_error(engine):
    receiver = WebhookReceiver(SECRET, IdempotencyStore(engine))
    body = json.dumps({"action": "completed"}).encode()  # no workflow_run
    headers = {
        "X-GitHub-Event": "workflow_run",
        "X-Hub-Signature-256": compute_signature(SECRET, body),
    }
    with pytest.raises(WebhookError):
        receiver.receive(body, headers)


def test_headers_are_matched_case_insensitively(receiver):
    body, headers = _delivery(_payload())
    lowered = {k.lower(): v for k, v in headers.items()}
    assert receiver.receive(body, lowered).outcome == ACCEPTED
