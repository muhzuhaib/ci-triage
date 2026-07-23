# ci-triage

[![CI](https://github.com/muhzuhaib/ci-triage/actions/workflows/ci.yml/badge.svg)](https://github.com/muhzuhaib/ci-triage/actions/workflows/ci.yml)

**A CI failure lands. A service reads the logs, asks an LLM what broke, and posts one comment on the
pull request — for at most a fixed, guaranteed amount of money.**

The interesting part is not the LLM call. It is that the spend ceiling is a *guarantee* rather than a
hope, and that the comment is posted exactly once, even though webhooks are redelivered, workers
crash mid-run, and providers rate-limit.

> **Status:** early. The budget ledger and the cost estimator that feeds it are complete and tested.
> The webhook receiver, run state machine and GitHub client are in progress. This README documents
> what exists; sections marked _(planned)_ do not exist yet.

## The problem

Three facts about this job, each of which breaks a naive implementation:

| Fact | What it breaks |
|---|---|
| CI logs are unbounded — a failing matrix job emits megabytes | Prompt cost is variable and can explode. "It's only a few cents per run" stops being true |
| GitHub redelivers webhooks; delivery is at-least-once | A duplicate run means a duplicate comment on someone's pull request |
| LLM providers rate-limit, time out, and return malformed output | Retries re-send a huge prompt. A retry storm costs 5× |

Every mechanism in this repo exists because of one of those rows. Nothing is here for decoration.

## What is built: the per-run spend ledger

```python
from ci_triage import Ledger, create_all, create_engine_for, dollars_to_micros

engine = create_engine_for("sqlite:///runs.db")
create_all(engine)
ledger = Ledger(engine)

ledger.open_run("run-42", ceiling_micros=dollars_to_micros("0.04"))

# Reserve the worst case *before* calling the provider.
reservation = ledger.reserve("run-42", dollars_to_micros("0.03"), purpose="diagnose")
try:
    response = call_the_model(...)
except Exception:
    ledger.release(reservation)      # never happened, charge nothing
    raise
else:
    ledger.commit(reservation, actual_cost_micros(response))   # settle the truth
```

If a reservation does not fit under the run's ceiling, `reserve()` raises `BudgetExceeded` and **the
provider is never called**. The money was refused before it could be spent, not measured after.

## What is built: worst-case cost estimation

The worst case above has to come from somewhere. It comes from a dated price table and a plan:

```python
from ci_triage import budget_input_chars, load_prices, plan_call_for_text

price = load_prices().get("claude-haiku-4-5-20251001")

# How much log can this run still afford?
budget = ledger.spend("run-42").remaining_micros
chars = budget_input_chars(price, budget_micros=budget, max_output_tokens=1_000)

plan = plan_call_for_text(price, log[:chars], max_output_tokens=1_000)
reservation = ledger.reserve("run-42", plan.worst_case_micros, purpose="diagnose")
# ...then send the request with max_tokens=plan.max_output_tokens
```

`plan_call` deliberately returns a **plan**, not a number: `worst_case_micros` is only an upper
bound on the bill if the request goes out with the plan's own limits, so the cost and the parameters
that make it true travel together instead of the caller being trusted to remember.

### Install and run the tests in under 60 seconds

```bash
git clone https://github.com/muhzuhaib/ci-triage && cd ci-triage
python -m pip install -e ".[dev]"
python -m pytest
```

No services, no API keys, no network. The ledger's dependency is SQLAlchemy and nothing else, which
is deliberate — the guarantees are the part worth verifying, so verifying them has to be free.

## Design decisions

### Reserve the worst case, then reconcile

The ledger holds an *authorisation*, the same shape as a payment card hold: estimate the worst-case
cost, reserve it, call, then commit the real cost and release the remainder.

Reserving the expected cost instead would be cheaper and wrong. An expected-cost reservation is
breached by any call that runs long — which is exactly the call you wanted a ceiling for. The
guarantee only means something if the reservation covers the bad case.

### The check and the write are one statement

The obvious implementation reads the running total, compares it to the ceiling, and then writes.
Under concurrency that is a time-of-check/time-of-use race: two workers both read a total with
headroom, both conclude there is room, and both spend. It is a double-spend, and it is invisible to
single-threaded tests.

So the ceiling condition lives in the `WHERE` clause of the update that adds the reservation. The
database evaluates it while holding the row lock, a reservation that would breach the ceiling matches
zero rows, and **a zero row count is the refusal**.

`tests/test_budget_concurrency.py` races 16 threads at one ceiling. It also contains a deliberately
naive check-then-act ledger and asserts that *it overspends* — because a concurrency test that has
never been observed to fail is not evidence of anything.

### "About 4 characters per token" is wrong for CI logs, by up to 4.6×

Every cost estimate eventually rests on a characters-to-tokens ratio, and the figure everyone
reaches for is ~4 chars/token. That is a number for **English prose**. CI logs are stack traces,
absolute paths, hex digests, ISO timestamps and base64, and they tokenise far worse.

Measured rather than assumed — reproduce with `python tools/measure_token_density.py`:

| Sample | chars/token (`o200k_base`) |
|---|---|
| English prose | 5.40 |
| Python traceback | 3.81 |
| pytest summary | 3.79 |
| npm / `tsc` build error | 3.77 |
| JSON payload | 2.33 |
| GitHub Actions log (ISO timestamp per line) | **1.99** |
| base64 blob | 1.64 |
| Docker `sha256:` layer digests | **1.18** |

So on the worst realistic input, a 4.0 divisor understates the token count — and therefore the cost
— by 4.6×, on precisely the payload this service was built to read. The table ships `1.18`, and
`0.90` for models on Anthropic's post-4.7 tokeniser, [which the vendor documents as emitting ~30%
more tokens for the same text](https://platform.claude.com/docs/en/about-claude/pricing).

### Estimating the log is the wrong way round; budgeting it is the right way

A worst-case ratio that pessimistic looks unusable: price a log at 1.18 and most runs get refused
for a bound that was merely cautious. That objection is real, and it is what makes the direction of
the calculation the interesting decision here.

The naive flow — take the log, estimate its cost, hope it fits — forces the estimate to be
*accurate*, which a character heuristic cannot be. So the service runs it backwards:
`budget_input_chars()` asks **how much log can I afford?**, and the log is truncated to that.
Now the pessimism costs a slightly shorter log instead of a refused run. The service never predicts
the size of its input — it chooses it.

That inversion is also what makes the bound honest. Both terms of the estimate are things the
caller controls: the input was truncated to a length we picked, and `max_tokens` on the request is a
number we set. The estimate is arithmetic on two chosen values, not a prediction about the world.

### The price table is dated data, not constants in code

`prices.json` carries `fetched_at` and a per-entry `source` URL, so "where did this number come from
and when?" always has an answer. Prices are **strings**, never JSON numbers — `json.load` turns a
bare `0.75` into a float, which would smuggle the imprecision back in one layer above the module
that exists to prevent it.

Stale and wrong are then treated differently, on purpose:

- **Wrong** — an entry carries `price_expires` and that date has passed. The provider published the
  change in advance, so we know as a fact the number is no longer the price. Pricing with it raises.
  There is a live example in the shipped table: Claude Sonnet 5's introductory rate is published as
  ending 2026-08-31.
- **Stale** — the table as a whole is older than `stale_after_days`. That is a prompt to re-check,
  not evidence any particular number is wrong, so it sets a flag and blocks nothing.

Failing hard on mere age takes a working service down over data hygiene. Failing soft on a price we
know has changed silently under-charges every run. Neither policy is right for both cases.

An unknown model raises rather than falling back to a default price — a default means the one model
we cannot cost is also the one we let through uncosted, which is how a ceiling stops being one.

### Money is an integer count of micro-dollars

Never a float, anywhere. A ceiling check is an inequality on a running total, and float addition is
not associative: accumulate a few thousand fractional-cent charges and the total depends on the order
they were added. A budget enforced to within a rounding error is not enforced.

Micro-dollars rather than cents because per-token prices are far below a cent — at cent granularity
a realistic per-token price rounds to zero.

### An overrun is recorded, not clamped

If a provider bills more than the worst case we estimated, the ledger records the true amount and
increments a separate `overrun_micros` counter. Clamping to the reserved amount would keep the books
tidy and hide the bug — a stale price table, or a provider ignoring `max_tokens`.

This is also why the guarantee is stated precisely: **reservations never exceed the ceiling.** That
is enforceable. "The provider never bills more than it said it would" is not, and claiming it would
be dishonest.

### SQLAlchemy Core, not the ORM

Four tables, touched by a handful of statements whose exact SQL is the entire point. A session's
unit-of-work would sit between those statements and the database and obscure the one thing that has
to be true.

### SQLite must be configured before it can be trusted

Two settings, both easy to omit and hard to notice missing:

- **`BEGIN IMMEDIATE`.** SQLAlchemy's default transaction is *deferred*, so a read-then-write
  transaction takes its write lock late; two of them can both read, and then one fails to upgrade
  and raises "database is locked" instead of waiting. Taking the lock up front makes writers queue.
- **`busy_timeout`.** Without it a writer that finds the lock held gives up immediately.

Postgres needs neither. The tests run on both.

### What was rejected

| Rejected | Why |
|---|---|
| Enforcing the budget in an AI gateway (LiteLLM, Bifrost, MLflow) | Gateways cap spend per key/team/tag over a **time window**. They have no concept of a run, so they cannot tell a retry of run A from the first attempt of run B. See below |
| A token-count ceiling instead of a money ceiling | Tokens are not fungible across models. A ceiling in tokens silently changes value when the model changes |
| Bundling a tokeniser to count input exactly | Per-provider, per-model, and several are not distributable at all — Anthropic's is not available offline. The service would fail closed for a reason unrelated to its job. `tiktoken` is used *once*, in `tools/`, to justify the ratio; it is not a dependency |
| A single global chars-per-token constant | The ratio differs by tokeniser generation by ~30%. It belongs in the price table next to the price, where it can be audited per model |
| Optimistic budgeting — spend first, alert after | That is monitoring, not a ceiling. The bill has already happened |
| Building on n8n / Kestra | See below |

## Why not n8n?

For a generic automation, use n8n — this is not a claim to have written a better workflow engine than
a funded team. The narrower claim is that the three properties *this* job needs are the three n8n
does not provide, and assembling them inside n8n means writing the same logic anyway, in a visual
editor where it cannot be unit-tested:

1. **No cross-execution idempotency.** A redelivered webhook arrives as a new execution; the Remove
   Duplicates node only deduplicates *within* one execution. The accepted answer is to bolt on an
   external Redis or Postgres gate.
2. **Retry is per-node and capped** at 5 attempts with a 5000 ms delay ceiling, and on a multi-item
   node one failed item retries all of them.
3. **No dead-letter queue primitive** — you assemble one from an Error Trigger, Continue On Fail and
   an IF node, and maintain it yourself.

None of that makes n8n bad; those are the costs of being general. For this job those three things are
the whole problem.

## Why not an AI gateway for the budget?

LiteLLM, Bifrost, MLflow AI Gateway and agentgateway all enforce spend limits before the provider
call, and at scale you would run one of them underneath this service. But their budget primitive is
**a key, user, team or tag over a time window, resetting by duration**. That is right for "this team
may spend $200 this month" and structurally wrong for "this one run may spend 4 cents", because the
gateway sees independent HTTP requests and cannot attribute a retry to a run.

You *can* mint an ephemeral virtual key per run with `max_budget` set, and it genuinely works — at
the cost of a key-lifecycle write per run with no automatic cleanup, and a budget that still resets
on a duration rather than on run completion.

More importantly, a gateway can only **refuse**. Deciding what happens when the ceiling is hit —
truncate the log and retry cheaper, downgrade the model, post a partial answer, or dead-letter the
run — is orchestration policy, and it belongs to the component that owns run identity. The honest
architecture is: gateway as the org-level backstop, per-run policy in the service.

## Roadmap

- [x] Per-run spend ledger with atomic reservation, commit, release
- [x] Worst-case cost estimation from a dated model price table
- [ ] Webhook receiver with signature verification and idempotency keys _(planned)_
- [ ] Run state machine: retries with backoff, dead-letter queue, replay _(planned)_
- [ ] GitHub log fetch, log truncation to fit the ceiling, comment posting _(planned)_
- [ ] Failure-injection suite: redelivery, worker kill, provider 429s, oversized logs _(planned)_
- [ ] `docker compose up` _(planned)_

## Licence

MIT
