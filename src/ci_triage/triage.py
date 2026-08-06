"""The handler: one failed run in, one comment out, under the ceiling.

Everything else in this package is a mechanism. This is the policy that spends
them, and it is deliberately the smallest module of the lot, because
:func:`~ci_triage.runs.process_next` already owns claiming, retrying, burying
and the exactly-once bookkeeping. What is left here is the order of operations,
and the order is the design::

    coordinates -> reclaim a dead attempt's hold -> failed jobs
                -> what can we afford -> fetch and truncate
                -> reserve the worst case -> ask -> commit the truth -> post

Four things in that sequence are load-bearing.

**The first act of an attempt is to undo the last one's crash.** A worker killed
mid-attempt leaves a hold standing against the run's ceiling, and since retries
share that one ceiling, a few crashes would leave the run unable to afford an
answer it had never actually paid for. The queue's ``attempt`` counter is what
makes this safe to do: see :meth:`~ci_triage.budget.Ledger.reclaim`.

**The affordable size is computed before the log is fetched, not after.** It is
the inversion :mod:`ci_triage.estimate` exists for: the service never asks "can
I afford this log?", which would make a refusal depend on how noisy someone
else's build was, but "how much log can I afford?", which always has an answer.

**The provider call is an injected callable.** This package never chooses a
vendor, never holds an API key, and never makes a network call to one. The seam
takes a prompt and a :class:`~ci_triage.estimate.CallPlan` and returns text plus
what it actually cost, which is exactly the contract the ledger needs to settle.
It also means the whole path below is testable without a provider account, and
the reservation arithmetic can be tested against a diagnosis that deliberately
costs more than its plan.

**Paying for an answer and then failing to post it is not a reason to throw the
answer away.** If the run has no pull request to comment on, the diagnosis is
recorded as the job's result and the job succeeds. Burying it would discard
something already paid for, and a redelivery would then pay for it again.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .budget import Ledger
from .estimate import BudgetTooSmall, CallPlan, budget_input_chars, plan_call_for_text
from .github import GitHubClient, NoPullRequest
from .github import marker_for
from .logs import LogSection, PreparedLog, prepare_log
from .money import format_micros
from .pricing import ModelPrice
from .runs import TerminalError, TriageJob
from .webhook import parse_ledger_run_id

#: Prepended to the log before it is sent. It is input like any other, so its
#: length is subtracted from the affordable characters rather than added on top.
DEFAULT_INSTRUCTION = (
    "The following CI job logs are from a failed run. Say what broke and why, "
    "in at most five sentences, quoting the line that shows it. If the logs do "
    "not contain enough to tell, say that instead of guessing.\n\n"
)

DEFAULT_MAX_OUTPUT_TOKENS = 700


@dataclass(frozen=True)
class Diagnosis:
    """What the provider said, and what it charged for saying it."""

    text: str
    cost_micros: int


#: The provider seam. Takes the prompt and the plan whose limits make the plan's
#: cost a bound, and returns the answer and the real cost.
Diagnose = Callable[[str, CallPlan], Diagnosis]


@dataclass(frozen=True)
class TriageOutcome:
    """What one triage did, in the form the job store records."""

    summary: str
    diagnosis: str | None = None
    comment_url: str | None = None
    plan: CallPlan | None = None
    log: PreparedLog | None = None

    def __str__(self) -> str:
        return self.summary


def render_comment(diagnosis: Diagnosis, plan: CallPlan, log: PreparedLog) -> str:
    """The comment body: the answer first, the provenance under it.

    The footer is not decoration. A reader who is being told what broke by a
    machine is entitled to know how much of the log it actually saw, and a
    truncated log is the single most likely reason for a confident wrong answer.
    """
    footer = (
        f"_Diagnosed from {log.kept_chars:,} of {log.original_chars:,} log characters"
    )
    if log.truncated:
        footer += f" ({log.dropped_lines:,} earlier lines dropped)"
    footer += f", model `{plan.model}`, cost {format_micros(diagnosis.cost_micros)}._"
    return f"{diagnosis.text.strip()}\n\n{footer}"


def make_handler(
    *,
    client: GitHubClient,
    ledger: Ledger,
    price: ModelPrice,
    diagnose: Diagnose,
    instruction: str = DEFAULT_INSTRUCTION,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
) -> Callable[[TriageJob], str]:
    """Build the handler :func:`~ci_triage.runs.process_next` calls.

    The returned callable raises on failure, which is the state machine's
    interface: a :class:`~ci_triage.runs.TerminalError` is buried immediately,
    anything else is retried within the run's one ceiling.
    """

    def handler(job: TriageJob) -> str:
        return str(run_triage(
            job,
            client=client,
            ledger=ledger,
            price=price,
            diagnose=diagnose,
            instruction=instruction,
            max_output_tokens=max_output_tokens,
        ))

    return handler


def run_triage(
    job: TriageJob,
    *,
    client: GitHubClient,
    ledger: Ledger,
    price: ModelPrice,
    diagnose: Diagnose,
    instruction: str = DEFAULT_INSTRUCTION,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
) -> TriageOutcome:
    """Triage one job. See the module docstring for why the order is the design."""
    try:
        repo, run_id, run_attempt = parse_ledger_run_id(job.run_id)
    except ValueError as exc:
        # A run id that does not parse will not start parsing on the third
        # attempt, so this is terminal rather than a transient read failure.
        raise TerminalError(str(exc)) from exc

    # Before anything is read or spent: give back what an attempt that is over
    # left held. A worker killed between reserving and settling cannot do this
    # for itself, and its hold would otherwise shrink this run's ceiling for
    # good -- which, since retries share one ceiling, is how a run goes broke
    # having spent nothing. Done here rather than after the early return below,
    # so a crash followed by a run with nothing to read still frees the money.
    ledger.reclaim(job.run_id, before_attempt=job.attempt)

    failed = client.failed_jobs(repo, run_id, run_attempt=run_attempt)
    if not failed:
        # The run concluded as a failure but no job did: a startup failure, or a
        # cancellation that raced the conclusion. There is nothing to read, and
        # nothing to spend money on.
        return TriageOutcome(f"{repo}#{run_id}.{run_attempt}: no failed jobs to read")

    remaining = ledger.spend(job.run_id).remaining_micros
    try:
        affordable = budget_input_chars(
            price, budget_micros=remaining, max_output_tokens=max_output_tokens
        )
    except BudgetTooSmall as exc:
        # Its own shortfall is on the output side, so a shorter log cannot fix
        # it. Terminal for the same reason BudgetExceeded is.
        raise TerminalError(str(exc)) from exc

    sections = [
        LogSection(name=ref.name, text=client.job_log(repo, ref.id)) for ref in failed
    ]
    log = prepare_log(sections, max(0, affordable - len(instruction)))
    prompt = instruction + log.text

    plan = plan_call_for_text(price, prompt, max_output_tokens=max_output_tokens)
    reservation = ledger.reserve(
        job.run_id, plan.worst_case_micros, attempt=job.attempt, purpose="diagnose"
    )
    try:
        diagnosis = diagnose(prompt, plan)
    except Exception:
        # The call may or may not have reached the provider, but nothing came
        # back to charge for. Release rather than commit zero: the reservations
        # table then still distinguishes "cost nothing" from "never happened".
        ledger.release(reservation)
        raise
    ledger.commit(reservation, diagnosis.cost_micros)

    body = render_comment(diagnosis, plan, log)
    marker = marker_for(job.run_id)
    head_sha = next((ref.head_sha for ref in failed if ref.head_sha), None)
    try:
        if head_sha is None:
            raise NoPullRequest(f"no head sha on any failed job of {job.run_id}")
        number = client.pull_request_for(repo, head_sha)
    except NoPullRequest as exc:
        # Paid for and useful, with nowhere to put it. Recorded on the job so it
        # is not lost, and *not* raised: dead-lettering here would throw away an
        # answer that has already been bought, and a redelivery would buy it again.
        return TriageOutcome(
            f"{job.run_id}: diagnosed, not posted ({exc})",
            diagnosis=diagnosis.text,
            plan=plan,
            log=log,
        )

    posted = client.upsert_comment(repo, number, marker=marker, body=body)
    verb = "updated" if posted.updated else "posted"
    return TriageOutcome(
        f"{job.run_id}: {verb} comment on #{number} ({posted.url})",
        diagnosis=diagnosis.text,
        comment_url=posted.url,
        plan=plan,
        log=log,
    )
