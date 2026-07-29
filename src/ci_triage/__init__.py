"""ci-triage -- webhook-driven CI failure triage with a hard per-run spend ceiling."""

from .budget import BudgetExceeded, Ledger, Reservation, RunSpend, UnknownRun
from .estimate import (
    BudgetTooSmall,
    CallPlan,
    budget_input_chars,
    budget_input_tokens,
    plan_call,
    plan_call_for_text,
)
from .idempotency import (
    Claim,
    IdempotencyError,
    IdempotencyStore,
)
from .money import dollars_to_micros, format_micros, micros_to_dollars
from .pricing import ExpiredPrice, ModelPrice, PriceTable, UnknownModel, load_prices
from .runs import (
    Backoff,
    JobError,
    JobStore,
    NotDeadLettered,
    ProcessOutcome,
    TerminalError,
    TriageJob,
    UnknownJob,
    default_is_retryable,
    full_jitter,
    no_jitter,
    process_next,
)
from .schema import create_all, create_engine_for
from .signature import (
    InvalidSignature,
    MissingSignature,
    SignatureError,
    compute_signature,
    verify_signature,
)
from .tokens import chars_that_fit, upper_bound_tokens
from .webhook import (
    ReceiveResult,
    WebhookError,
    WebhookReceiver,
    WorkflowRunEvent,
)

__version__ = "0.2.0"

__all__ = [
    "Backoff",
    "BudgetExceeded",
    "BudgetTooSmall",
    "CallPlan",
    "Claim",
    "ExpiredPrice",
    "IdempotencyError",
    "IdempotencyStore",
    "InvalidSignature",
    "JobError",
    "JobStore",
    "Ledger",
    "MissingSignature",
    "ModelPrice",
    "NotDeadLettered",
    "PriceTable",
    "ProcessOutcome",
    "ReceiveResult",
    "Reservation",
    "RunSpend",
    "SignatureError",
    "TerminalError",
    "TriageJob",
    "UnknownJob",
    "UnknownModel",
    "UnknownRun",
    "WebhookError",
    "WebhookReceiver",
    "WorkflowRunEvent",
    "budget_input_chars",
    "budget_input_tokens",
    "chars_that_fit",
    "compute_signature",
    "create_all",
    "create_engine_for",
    "default_is_retryable",
    "dollars_to_micros",
    "format_micros",
    "full_jitter",
    "load_prices",
    "micros_to_dollars",
    "no_jitter",
    "plan_call",
    "plan_call_for_text",
    "process_next",
    "upper_bound_tokens",
    "verify_signature",
]
