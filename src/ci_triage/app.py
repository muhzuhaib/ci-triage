"""The ASGI binding: the receiver, as something you can actually deploy.

:mod:`ci_triage.webhook` is a pure function on purpose, so this module is the
dozen lines its docstring promised: bytes and headers in, a status code out.
Everything that decides anything lives behind it, which is why there is no
business logic here to test twice.

Two things in the request path are more than plumbing.

**Admission is two idempotent writes, and neither is allowed to be the last
word on its own.** Claiming the idempotency key and creating the job are
separate statements, so a process killed between them would leave a claimed key
with no job: the delivery is remembered as seen, GitHub is told 202, and nothing
ever triages it. Redelivery would find the key claimed and drop it. So the
handler does not branch on whether the receiver called this a first sight or a
duplicate -- it opens the run budget and enqueues the job either way. Both calls
are idempotent by a database constraint rather than by a preceding read
(``open_run`` will not reset a partly spent ceiling, and ``enqueue`` is arbitrated
by the unique index on ``idempotency_key``), so the repeat costs a rejected
insert and buys recovery from a crash in the gap.

**Only a request we genuinely cannot use is answered with an error.** GitHub
redelivers on any non-2xx, so a 500 for an event we simply do not act on turns
into a retry loop that we caused. An unauthenticated body is 401 and a malformed
one is 400, because redelivering either changes nothing; everything else is 2xx.

Configuration is read from the environment once, at construction, and a missing
webhook secret is a startup failure rather than a per-request one. A service that
boots without a secret and then fails every delivery is a service that looks
healthy while doing nothing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, text

from .budget import Ledger
from .idempotency import IdempotencyStore
from .money import dollars_to_micros
from .runs import JobStore
from .schema import create_all, create_engine_for
from .signature import SignatureError
from .webhook import ACCEPTED, DUPLICATE, WebhookError, WebhookReceiver

#: What one CI run may spend on being diagnosed, if the environment does not
#: say. Four cents covers a cheap-tier call over a truncated log with room to
#: spare, and a ceiling is only useful if it is set to something.
DEFAULT_CEILING_USD = "0.04"

DEFAULT_MAX_ATTEMPTS = 3


class ConfigError(RuntimeError):
    """The environment does not describe a service that could work."""


@dataclass(frozen=True)
class Settings:
    """Everything the receiver needs, resolved once."""

    webhook_secret: str
    database_url: str
    ceiling_micros: int
    max_attempts: int
    create_schema: bool

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        """Read settings from ``CI_TRIAGE_*`` variables.

        :raises ConfigError: a required variable is missing or unusable. Raised
            at construction so the container dies loudly at boot instead of
            answering 500 to every delivery for the rest of the day.
        """
        source = os.environ if env is None else env

        secret = source.get("CI_TRIAGE_WEBHOOK_SECRET", "")
        if not secret:
            raise ConfigError(
                "CI_TRIAGE_WEBHOOK_SECRET is not set. An empty secret is not "
                "'no authentication', it is forgeable authentication: anyone can "
                "compute an HMAC keyed by the empty string."
            )

        url = source.get("CI_TRIAGE_DATABASE_URL", "")
        if not url:
            raise ConfigError("CI_TRIAGE_DATABASE_URL is not set")

        try:
            ceiling = dollars_to_micros(source.get("CI_TRIAGE_CEILING_USD", DEFAULT_CEILING_USD))
        except Exception as exc:
            raise ConfigError(f"CI_TRIAGE_CEILING_USD is not a usable amount: {exc}") from exc
        if ceiling <= 0:
            raise ConfigError("CI_TRIAGE_CEILING_USD must be greater than zero")

        raw_attempts = source.get("CI_TRIAGE_MAX_ATTEMPTS", str(DEFAULT_MAX_ATTEMPTS))
        try:
            attempts = int(raw_attempts)
        except ValueError as exc:
            raise ConfigError(f"CI_TRIAGE_MAX_ATTEMPTS is not an integer: {exc}") from exc
        if attempts < 1:
            raise ConfigError("CI_TRIAGE_MAX_ATTEMPTS must be at least 1")

        return cls(
            webhook_secret=secret,
            database_url=url,
            ceiling_micros=ceiling,
            max_attempts=attempts,
            create_schema=source.get("CI_TRIAGE_CREATE_SCHEMA", "0") == "1",
        )


def create_app(settings: Settings, *, engine: Engine | None = None) -> FastAPI:
    """Build the ASGI application.

    ``engine`` is injectable so the tests can hand in one they already hold; in
    the container it is built from ``settings.database_url``.
    """
    engine = engine or create_engine_for(settings.database_url)
    if settings.create_schema:
        create_all(engine)

    ledger = Ledger(engine)
    jobs = JobStore(engine, max_attempts=settings.max_attempts)
    receiver = WebhookReceiver(secret=settings.webhook_secret, store=IdempotencyStore(engine))

    app = FastAPI(
        title="ci-triage",
        description=(
            "Receives GitHub workflow_run deliveries, authenticates them, "
            "deduplicates them and queues one triage per failure."
        ),
    )

    @app.get("/healthz")
    def healthz() -> Response:
        """Liveness *and* the database, because one without the other lies.

        A receiver whose database is unreachable can still answer a static
        health check, and an orchestrator would go on sending it deliveries it
        cannot claim. So the check is a query.
        """
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:
            return JSONResponse({"status": "unhealthy", "detail": str(exc)}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.post("/webhook")
    async def webhook(request: Request) -> Response:
        body = await request.body()
        try:
            result = receiver.receive(body, dict(request.headers))
        except SignatureError as exc:
            # Not authentic. Redelivering will not make it authentic, but 401 is
            # the truthful answer and GitHub surfaces it in the hook's delivery
            # log, which is where someone debugging a wrong secret will look.
            return JSONResponse({"outcome": "unauthenticated", "detail": str(exc)}, status_code=401)
        except WebhookError as exc:
            return JSONResponse({"outcome": "malformed", "detail": str(exc)}, status_code=400)

        if result.event is None or result.outcome not in (ACCEPTED, DUPLICATE):
            return JSONResponse({"outcome": result.outcome, "detail": result.detail})

        # See the module docstring: unconditional, because a duplicate is also
        # how a crash between the claim and the enqueue comes back to be fixed.
        event = result.event
        ledger.open_run(event.ledger_run_id, ceiling_micros=settings.ceiling_micros)
        job = jobs.enqueue(event.idempotency_key, event.ledger_run_id)

        payload = {
            "outcome": result.outcome,
            "detail": result.detail,
            "run": event.ledger_run_id,
            "job_id": job.id if job is not None else None,
        }
        return JSONResponse(payload, status_code=202 if result.outcome == ACCEPTED else 200)

    return app


def build() -> FastAPI:
    """Factory for ``uvicorn ci_triage.app:build --factory``."""
    return create_app(Settings.from_env())
