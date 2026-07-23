"""Upper bounds on token counts.

This module is deliberately small, deliberately separate, and deliberately the
only place in the project where a number is *guessed*. Everything else works on
token counts that are either measured or chosen; here, and only here, a length
in characters is turned into a length in tokens without running the provider's
tokeniser.

Why not just run the tokeniser
------------------------------
Two reasons, and the second is the real one:

1. Tokenisers are per-provider, per-model, and several are not distributable --
   Anthropic's is not available offline at all. A service that could only price
   a call by downloading a vendor's BPE table at start-up would fail closed for
   a reason unrelated to its job.
2. **The estimate is not a prediction, it is a budget.** The service does not
   need to know how many tokens a log *will* be. It needs to decide how much
   log it can afford to send, then send exactly that much. Direction of use is
   inverted, so accuracy matters far less than never under-counting.

Which is why every function here rounds *up*, and why ``chars_per_token`` comes
from the price table as a per-model worst case rather than a global constant.

The number everyone gets wrong
------------------------------
"About 4 characters per token" is the folk constant, and it is a figure for
English prose. Measured on real CI log content (``tools/measure_token_density.py``):

    english prose            5.40 chars/token
    python traceback         3.81
    pytest summary           3.79
    npm/tsc build error      3.77
    JSON payload             2.33
    Actions timestamps       1.99   <- every line prefixed with an ISO timestamp
    base64 blob              1.64
    docker sha256 digests    1.18

So on the worst realistic input a 4.0 divisor understates the token count by
**4.6x** -- and an understated token count is an understated cost, on precisely
the payload this service was built to read.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

#: Per-message framing that chat APIs add and callers forget: role markers,
#: message delimiters, and the model's own turn preamble. A handful of tokens
#: per message is noise on a 100k-token prompt and is not noise on the small
#: prompts a cheap triage model gets, so it is charged rather than ignored.
DEFAULT_MESSAGE_OVERHEAD_TOKENS = 8


def upper_bound_tokens(
    text: str,
    chars_per_token: Decimal,
    *,
    overhead_tokens: int = DEFAULT_MESSAGE_OVERHEAD_TOKENS,
) -> int:
    """Return a conservative upper bound on the tokens ``text`` will occupy.

    Rounds up, then adds framing overhead. Never returns less than the
    overhead, so an empty string still costs what an empty message costs.
    """
    if chars_per_token <= 0:
        raise ValueError("chars_per_token must be positive")
    if overhead_tokens < 0:
        raise ValueError("overhead_tokens cannot be negative")

    body = (
        int((Decimal(len(text)) / chars_per_token).to_integral_value(ROUND_CEILING))
        if text
        else 0
    )
    return body + overhead_tokens


def chars_that_fit(
    token_budget: int,
    chars_per_token: Decimal,
    *,
    overhead_tokens: int = DEFAULT_MESSAGE_OVERHEAD_TOKENS,
) -> int:
    """The inverse: how many characters fit inside ``token_budget`` tokens.

    This is the function the truncation step actually uses. Rounds *down*, so
    the text it sizes is guaranteed to bound back to no more than
    ``token_budget`` under :func:`upper_bound_tokens`. Returns 0 rather than a
    negative number when the overhead alone exhausts the budget.
    """
    if chars_per_token <= 0:
        raise ValueError("chars_per_token must be positive")

    body_tokens = token_budget - overhead_tokens
    if body_tokens <= 0:
        return 0
    return int((Decimal(body_tokens) * chars_per_token).to_integral_value(ROUND_FLOOR))
