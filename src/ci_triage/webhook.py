"""The webhook receiver: turn a raw GitHub delivery into a single decision.

This is deliberately framework-agnostic. It takes the raw body bytes and the
request headers and returns a :class:`ReceiveResult`; binding it to FastAPI or
any ASGI server is a dozen lines that belong with the deployment, not here.
Keeping the core a pure function is what lets the whole receive path -- signature
check, event filtering, idempotency -- be tested in milliseconds with no HTTP
server and no network, which is the same principle the ledger is built on.

The order of operations is a security decision, not an arrangement of
convenience:

1. **Authenticate the raw bytes first.** The HMAC is over the body exactly as
   sent, and an unauthenticated body should never reach a parser. So the
   signature is checked before ``json.loads`` runs.
2. **Filter to what we act on.** Only a ``workflow_run`` that *completed* with a
   failing conclusion is actionable. Everything else -- other events, the
   ``requested``/``in_progress`` actions, a green run -- is acknowledged with
   success and dropped. Acknowledging matters: GitHub redelivers on any non-2xx,
   so returning an error for an event we simply do not care about would make
   GitHub retry it forever.
3. **Claim exactly once.** The actionable event is reduced to a content-derived
   idempotency key and claimed. A first claim is ``ACCEPTED`` and the caller
   does the work; a duplicate is reported as such and no work is scheduled.

The idempotency key includes ``run_attempt`` on purpose. GitHub's "re-run failed
jobs" reuses the same ``workflow_run.id`` and only increments ``run_attempt``, so
a key without the attempt would dedupe a genuine re-run against the original
failure and the re-run would never be triaged.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .idempotency import (
    DUPLICATE_COMPLETED,
    DUPLICATE_IN_FLIGHT,
    FIRST,
    IdempotencyStore,
)
from .signature import verify_signature

#: Outcomes of receiving a delivery.
ACCEPTED = "accepted"  # first sight of an actionable failure; caller does the work
DUPLICATE = "duplicate"  # already seen; acknowledged, nothing scheduled
IGNORED = "ignored"  # authenticated but not something we act on

#: Conclusions that mean "CI broke and someone wants to know why". ``cancelled``,
#: ``skipped`` and the rest are not failures worth spending a diagnosis on.
DEFAULT_ACTIONABLE_CONCLUSIONS = frozenset({"failure", "timed_out"})

SUBSCRIBED_EVENT = "workflow_run"


class WebhookError(Exception):
    """A body that was authentic but could not be understood.

    Distinct from a signature error: the request proved it came from GitHub, but
    its shape was not what a ``workflow_run`` delivery should be. Maps to 400,
    not 401 -- retrying it will not help, so the caller should not make GitHub
    redeliver.
    """


@dataclass(frozen=True)
class WorkflowRunEvent:
    """The parts of a ``workflow_run`` delivery this service acts on."""

    delivery_id: str | None
    repo: str
    run_id: int
    run_attempt: int
    action: str
    conclusion: str | None
    idempotency_key: str
    payload: Mapping[str, Any] = field(repr=False)

    @property
    def ledger_run_id(self) -> str:
        """The identifier the spend ledger scopes a ceiling to.

        Per-attempt, because each re-run is a fresh diagnosis that costs money;
        whether attempts should share one ceiling is orchestration policy for the
        run state machine, and is intentionally not decided here.
        """
        return f"{self.repo}#{self.run_id}.{self.run_attempt}"


@dataclass(frozen=True)
class ReceiveResult:
    outcome: str
    detail: str
    event: WorkflowRunEvent | None = None
    prior_result: str | None = None

    @property
    def should_process(self) -> bool:
        return self.outcome == ACCEPTED


def _lower_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Header names are case-insensitive; ASGI/WSGI present them inconsistently."""
    return {k.lower(): v for k, v in headers.items()}


class WebhookReceiver:
    """Authenticate, filter and deduplicate GitHub ``workflow_run`` deliveries."""

    def __init__(
        self,
        secret: str | bytes,
        store: IdempotencyStore,
        *,
        actionable_conclusions: frozenset[str] = DEFAULT_ACTIONABLE_CONCLUSIONS,
    ) -> None:
        self._secret = secret
        self._store = store
        self._actionable = actionable_conclusions

    def receive(self, body: bytes, headers: Mapping[str, str]) -> ReceiveResult:
        """Process one raw delivery. See the module docstring for the ordering.

        :raises ci_triage.signature.SignatureError: the body failed
            authentication. The caller should reply 401 and *not* schedule work.
        :raises WebhookError: authentic but malformed. Reply 400.
        """
        h = _lower_headers(headers)

        # 1. Authenticate the raw bytes before anything parses them.
        verify_signature(self._secret, body, h.get("x-hub-signature-256"))

        event_type = h.get("x-github-event")
        delivery_id = h.get("x-github-delivery")

        # 2a. Not an event we subscribe to. Acknowledge so GitHub stops.
        if event_type != SUBSCRIBED_EVENT:
            return ReceiveResult(IGNORED, f"event {event_type!r} is not handled")

        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise WebhookError(f"body is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise WebhookError("body is not a JSON object")

        action = payload.get("action")
        run = payload.get("workflow_run")
        repo = payload.get("repository", {})
        if not isinstance(run, dict) or not isinstance(repo, dict):
            raise WebhookError("payload is missing workflow_run or repository")

        # 2b. Only a completed, failing run is actionable.
        if action != "completed":
            return ReceiveResult(IGNORED, f"action {action!r} is not 'completed'")

        conclusion = run.get("conclusion")
        if conclusion not in self._actionable:
            return ReceiveResult(IGNORED, f"conclusion {conclusion!r} is not actionable")

        try:
            run_id = int(run["id"])
            run_attempt = int(run.get("run_attempt", 1))
        except (KeyError, TypeError, ValueError) as exc:
            raise WebhookError(f"workflow_run has no usable id/run_attempt: {exc}") from exc

        repo_name = repo.get("full_name") or str(repo.get("id", "unknown"))
        key = f"{SUBSCRIBED_EVENT}:{repo_name}:{run_id}:{run_attempt}:{action}"

        event = WorkflowRunEvent(
            delivery_id=delivery_id,
            repo=repo_name,
            run_id=run_id,
            run_attempt=run_attempt,
            action=action,
            conclusion=conclusion,
            idempotency_key=key,
            payload=payload,
        )

        # 3. Claim exactly once.
        claim = self._store.claim(key, event.ledger_run_id)
        if claim.outcome == FIRST:
            return ReceiveResult(ACCEPTED, "first delivery; scheduled for triage", event)
        if claim.outcome == DUPLICATE_COMPLETED:
            return ReceiveResult(
                DUPLICATE, "already triaged; replaying prior result", event, claim.result
            )
        assert claim.outcome == DUPLICATE_IN_FLIGHT
        return ReceiveResult(DUPLICATE, "another delivery is being triaged now", event)
