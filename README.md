# ci-triage

[![CI](https://github.com/muhzuhaib/ci-triage/actions/workflows/ci.yml/badge.svg)](https://github.com/muhzuhaib/ci-triage/actions/workflows/ci.yml)

**A CI failure lands. A service reads the logs, asks an LLM what broke, and posts one comment on the
pull request — for at most a fixed, guaranteed amount of money.**

The interesting part is not the LLM call. It is that the spend ceiling is a *guarantee* rather than a
hope, and that the comment is posted exactly once, even though webhooks are redelivered, workers
crash mid-run, and providers rate-limit.

> **Status:** early. Complete and tested: the budget ledger, the cost estimator that feeds it, the
> webhook receiver that admits work (signature verification and exactly-once idempotency), the run
> state machine that retries, buries and replays it, and the GitHub side that reads the failed jobs'
> logs and posts the answer. The failure-injection suite is next. This README documents what exists;
> sections marked _(planned)_ do not exist yet.

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

## What is built: the webhook receiver

A GitHub delivery has to be admitted before any of that runs, and admission is where two of the three
opening problems live: an unauthenticated body, and a redelivered one.

```python
from ci_triage import IdempotencyStore, WebhookReceiver

receiver = WebhookReceiver(secret=WEBHOOK_SECRET, store=IdempotencyStore(engine))

result = receiver.receive(raw_body_bytes, request_headers)
if result.should_process:
    triage(result.event)          # first sight of this failure — do the work
# else: authenticated but a duplicate or not a failure — acknowledged, nothing scheduled
```

`receive()` does three things in an order that is a security decision, not a convenience:

1. **Authenticate the raw bytes**, before anything parses them. The HMAC is over the body exactly as
   sent, so verification runs on the received bytes and an unauthenticated body never reaches
   `json.loads`.
2. **Filter to what we act on** — a `workflow_run` that *completed* with a failing conclusion.
   Everything else is acknowledged with success and dropped, because GitHub redelivers on any non-2xx
   and returning an error for an event we do not care about would make it retry forever.
3. **Claim exactly once**, so a redelivery is recognised and the comment is posted a single time.

It is deliberately framework-agnostic: bytes and headers in, a decision out. Binding it to FastAPI or
any ASGI server is a dozen lines that belong with the deployment — keeping the core a pure function is
what lets the whole admission path be tested in milliseconds with no server and no network.

## What is built: the run state machine

Admission decides whether to work on a failure. The state machine decides what happens when the work
itself fails, which it will: providers return 429, log downloads time out, workers get killed
mid-attempt, and some failures will never succeed no matter how often they are retried.

```python
from ci_triage import IdempotencyStore, JobStore, process_next

jobs = JobStore(engine, max_attempts=3)
jobs.enqueue(result.event.idempotency_key, result.event.ledger_run_id)

# ...in each worker process, in a loop:
process_next(jobs, triage, worker="worker-3", idempotency=IdempotencyStore(engine))
```

A job is `pending`, `running`, `succeeded` or `dead_letter`. A job waiting out a backoff is `pending`
with a future due time rather than a state of its own, because two states that both mean "will run
again" have to be kept in step by every query that asks what is runnable, and one of them eventually
is not.

What the machine guarantees, and how each guarantee is tested:

| Guarantee | Mechanism | Test |
|---|---|---|
| One job goes to exactly one worker | conditional `UPDATE`; the row count is the verdict | 16 threads race one job |
| A crashed worker's job is retried | leases with an expiry; a lapsed lease is reclaimable | reclaim races 16 threads |
| A crashed worker's job is not immortal | the call that would retry it buries it when no attempts remain | lease lapses on the final attempt |
| A revived worker cannot overwrite the attempt that replaced it | `(worker, attempt)` fencing token on every write | stolen lease, stale write refused |
| Retries do not multiply the cost ceiling | every attempt spends from the one run budget | budget failure is terminal on attempt 1 |
| A hopeless failure is not retried three times first | failures classified retryable or terminal | `BudgetExceeded` goes straight to the queue |
| Replay grants attempts, not money | `max_attempts` is raised; `attempt` and the ceiling are untouched | replay, then claim, then succeed |

### Install and run the tests in under 60 seconds

```bash
git clone https://github.com/muhzuhaib/ci-triage && cd ci-triage
python -m pip install -e ".[dev]"
python -m pytest
```

No services, no API keys, no network. The ledger's dependency is SQLAlchemy and nothing else, which
is deliberate — the guarantees are the part worth verifying, so verifying them has to be free.

## What is built: the GitHub side

Reading the logs and posting the answer is where the budget stops being arithmetic and starts
buying something.

```python
from ci_triage.github import GitHubClient
from ci_triage.triage import make_handler

handler = make_handler(
    client=GitHubClient(GITHUB_TOKEN),
    ledger=ledger,
    price=load_prices().get("claude-haiku-4-5-20251001"),
    diagnose=ask_your_provider,      # (prompt, plan) -> Diagnosis(text, cost_micros)
)

process_next(jobs, handler, worker="worker-3", idempotency=IdempotencyStore(engine))
```

The order inside the handler is the design:

```
coordinates -> failed jobs -> what can we afford -> fetch and truncate
            -> reserve the worst case -> ask -> commit the truth -> post one comment
```

The provider is an injected callable. This package never picks a vendor, never holds an API key and
never calls one, which is also what lets the whole path above be tested without an account: the
tests run it end to end against `httpx.MockTransport` and a `diagnose` that returns whatever cost
the test needs it to.

### Only the failed jobs are read, and their timestamps are thrown away first

The whole-run log endpoint returns a zip of every job, which on a green-except-one matrix is mostly
logs of things that worked. The jobs list carries each job's `conclusion`, so the failures are named
first and fetched one at a time.

Then every line of what comes back is prefixed with `2026-07-29T23:12:55.4607494Z `, and dropping
that prefix is the single largest thing that can be done for the size of the prompt. Measured on a
real 547-line job log from this repository's own CI, committed at
`tools/samples/actions-job.log` so the number can be checked
(`python tools/measure_timestamp_cost.py`):

| | chars | tokens | chars/token |
|---|---|---|---|
| raw | 58,853 | 24,387 | 2.41 |
| timestamps stripped | 42,990 | 14,838 | 2.90 |

The prefixes are **27% of the characters but 39% of the tokens**, because a run of digits and
punctuation tokenises worse than the English and code around it. The same budget therefore buys
**1.64x more real log** once they are gone.

What is left is truncated from the front, because a CI log opens with runner provisioning and
dependency resolution, which are identical on the runs that pass, and ends with the failing
assertion, which is not. Several failed jobs share the budget by water filling: equal shares, and a
job whose log is smaller than its share returns the remainder to the ones still over. The per-job
headers and the note saying what was dropped are counted inside the ceiling rather than added on
top, because a bound exceeded by the machinery announcing the bound is not a bound.

### The comment is identified by a marker inside itself

Creating a comment has no idempotency key, so "post exactly once" is not something the API can be
asked for. Every comment therefore carries an invisible HTML marker naming the run, and posting
means upserting: find the marker, edit that comment, or create one if there is none. A redelivery
lands as an edit rather than a second opinion underneath the first.

**The honest limitation:** find-then-create is a check-then-act race, and unlike the ledger and the
queue there is no conditional write available to close it. What makes it safe in practice is the
lease, which means one worker is doing this at a time. What the marker guarantees on its own is
recovery, not exclusion. `tests/test_failure_injection.py` kills a worker at the two points where
this would show, including the one where the comment reaches GitHub and the worker never learns its
id, and asserts the pull request ends up with exactly one comment either way.

## What is built: the failure-injection suite

Every guarantee above was, until this point, proved by code that was allowed to finish. That is a
weak kind of proof for a service whose promises are all about what happens when things break, so
`tests/test_failure_injection.py` takes the finishing away: it kills the worker at each seam of a
triage in turn, then lets the lease expire and a replacement take over, and checks the same four
things every time.

| The promise | How it is checked |
|---|---|
| One run, one comment | the fake GitHub keeps what was posted to it, so the count is of real state |
| The ceiling holds | `spent + reserved <= ceiling` after the crash and the recovery |
| No job is lost | the job ends `succeeded` or `dead_letter`, never stuck `running` |
| No money is stranded | no reservation is left `held` by an attempt that is over |

A kill is a `BaseException`, not an exception. `process_next` catches `Exception` and turns it into a
retry or a burial, which is the handling path, not the crash path. Killing a worker with something
catchable would test the error handler and call it a crash. What escapes instead leaves the job
`running` with a live lease and nothing recorded, which is what a `SIGKILL` really leaves behind.

**It found a defect on the first run, and the defect was in the ledger.** A worker killed between
reserving and settling left its hold standing for ever. Nothing timed it out, and because retries
share one ceiling, the run's budget ratcheted down with each crash until the job was dead-lettered
for lack of money it had never spent. Nothing threw, nothing logged, and the ledger reported zero
spent. That failure is kept as a control case, so the test that proves the fix has been seen to fail
without it.

## Design decisions

### The log redirect is followed by hand, without the token

`GET /repos/{owner}/{repo}/actions/jobs/{job_id}/logs` answers `302` with a `Location` pointing at a
storage host, and [the documentation](https://docs.github.com/en/rest/actions/workflow-jobs) notes
the link expires after one minute. Letting the HTTP client follow that automatically means trusting
its redirect policy not to forward the `Authorization` header to a third party. Some clients strip
it across origins and some do not, and a credential leak is not a thing to hold by convention, so
the redirect is read and re-issued explicitly with no auth header at all. A test asserts the second
request carries none, so a later refactor to `follow_redirects=True` cannot quietly undo it.

### The server's `retry-after` outranks our own backoff

A `403` or `429` from GitHub can mean the primary or the secondary rate limit, so the response
headers decide rather than the status code: `retry-after`, or `x-ratelimit-reset` when
`x-ratelimit-remaining` is `0`. GitHub
[states plainly](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api)
that continuing to call while limited can get an integration banned, so a locally computed backoff
that happens to be shorter is not an opinion worth having. The exception carries the number, and the
state machine reads it off the exception without importing anything HTTP, which is what keeps the
queue free of a dependency on whichever client raised it.

Jitter is then *added* to the server's delay rather than applied across it. Full jitter over
`[0, delay]` would put most of the herd back inside the window the server just closed.

### A 404 is buried, a 502 is retried, and the exception type says which

The client raises two families: transient ones for timeouts, 5xx and rate limits, and permanent ones
that subclass the state machine's `TerminalError`. The classifier in `runs.py` therefore needs no
knowledge of HTTP to do the right thing, and a repository that no longer exists is not retried three
times with backoff before anyone admits it.

### A diagnosis that cannot be posted is still recorded

If the run has no pull request to comment on, which is normal for a push to a branch nobody has
opened one for, the answer is stored as the job's result and the job succeeds. Dead-lettering there
would throw away something already paid for, and a redelivery would buy it again.

### Reserve the worst case, then reconcile

The ledger holds an *authorisation*, the same shape as a payment card hold: estimate the worst-case
cost, reserve it, call, then commit the real cost and release the remainder.

Reserving the expected cost instead would be cheaper and wrong. An expected-cost reservation is
breached by any call that runs long — which is exactly the call you wanted a ceiling for. The
guarantee only means something if the reservation covers the bad case.

### A hold has to be reclaimable, and the queue's fencing token is what makes that safe

Reserve-then-reconcile assumes the reserving process comes back to reconcile. A killed one does not,
and no timeout would help: a hold that has stood for five minutes looks exactly like a call that has
been running for five minutes, and guessing wrong either strands the money or double-spends it.

The queue already knows the answer. It hands a job to one worker at a time and increments `attempt`
when it does, so a hold recorded under an attempt *earlier* than the one now running provably belongs
to a worker that has been replaced. `Ledger.reclaim(run_id, before_attempt=...)` gives those back,
and it is the first thing each attempt does. The comparison is strictly `<`: the attempt now running
is never in scope, so the fix cannot rob the worker it exists to protect.

### A reclaimed hold is marked, not deleted

Reclaiming makes one thing possible that could not happen before: a worker can be merely paused
rather than dead, so its call may land after its hold was taken back. `commit` still records that
cost, in full, as an overrun. The money left the building outside what the ceiling authorised, and
the alternative, dropping it because the hold that would have covered it is gone, would make the
books balance by losing evidence.

**The honest limitation, and it is the sharpest one in the project.** A worker killed between the
provider answering and the commit landing spent money that nothing recorded, and no design recovers a
fact that died with the process. So the ceiling bounds what the ledger *authorises*, and each crash
at that one seam can cost up to one call's worth beyond it. What the ledger can do is refuse to
pretend: the hold is not deleted, it is marked `reclaimed` with the attempt that took it, so every
point at which the run may have paid without knowing is a row to go and look at. Reconciling against
a provider invoice needs exactly that list.

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

### The signature is checked on raw bytes, in constant time, and fails closed

Three details in verifying GitHub's `X-Hub-Signature-256` are each a vulnerability if got wrong, not a
style preference:

- **Over the raw bytes.** The HMAC covers the body exactly as sent. Parse-then-reserialise changes the
  bytes — key order, whitespace, unicode escaping — so a signature checked against reserialised JSON
  never matches. This is also why authentication runs *before* `json.loads`: an unauthenticated body
  is not handed to a parser.
- **Constant-time comparison.** A byte-by-byte `==` returns as soon as it finds a difference, leaking
  through timing how many leading bytes were right — enough to forge a signature one byte at a time.
  `hmac.compare_digest` exists to close exactly that oracle, and a test asserts the comparison goes
  through it so a refactor cannot quietly reintroduce `==`.
- **An empty secret raises.** An unconfigured secret is not "no security", it is *forgeable* security —
  anyone can compute an HMAC keyed by the empty string. Refusing to verify at all is safer.

The legacy SHA-1 `X-Hub-Signature` header is rejected rather than accepted for compatibility; accepting
it would let a caller downgrade the check to a broken hash.

### Idempotency is keyed on the event, not the delivery — and the key includes the attempt

Exactly-once rests on the same move as the ledger: a single `INSERT` of the key is the claim, and the
primary-key constraint arbitrates the race under the row lock. A read-first "have I seen this?" is the
check-then-act double-post again, invisible to single-threaded tests — so
`tests/test_idempotency_concurrency.py` races real threads and carries a naive store as a control that
double-claims, proving the harness has teeth.

Two choices in *what* the key is made of:

- **The event, not the envelope.** GitHub's `X-GitHub-Delivery` GUID is documented only as identifying
  "the event", with no guarantee a redelivery reuses it — so keying on it could let a redelivery
  through as new. The key is derived from what happened
  (`workflow_run:<repo>:<run_id>:<run_attempt>:<action>`), which is stable across redeliveries by
  construction, whatever the envelope does.
- **`run_attempt` is in the key.** GitHub's "re-run failed jobs" reuses the same `workflow_run.id` and
  only bumps `run_attempt`. A key without it would dedupe a genuine re-run against the original failure,
  and the re-run would never be triaged.

### The queue claim is a conditional write, because `SKIP LOCKED` is not portable

Postgres has `SELECT ... FOR UPDATE SKIP LOCKED`, which is the natural way to hand one queued job to
exactly one worker. SQLite has nothing like it, and this project's guarantees are meant to hold on the
database people actually deploy on *and* on the one that makes the tests free to run.

So the claim is the same move as the ledger's reservation. A `SELECT` nominates candidate rows and
decides nothing, then an `UPDATE` re-asserts every condition that made the row runnable, and its row
count is the verdict. A worker that loses the race for one candidate simply tries the next.
`tests/test_runs_concurrency.py` races 16 threads at one job, and carries a naive store whose `UPDATE`
is guarded on the primary key alone, on the reasonable-sounding grounds that it just checked the row
was pending. All 16 workers win that one, which is 16 identical comments on somebody's pull request.

### A lease is not a mutex, so `attempt` is a fencing token

A worker holds a job for a lease period; if it dies, the lease lapses and another worker takes over.
The hazard is that a dead worker and a paused one look identical from the outside. The original can
wake up after its job has been handed on, and if the store trusted the job object it was handed, that
late write would overwrite the outcome of the attempt that replaced it. With the idempotency key then
marked completed, the stale result would be replayed to every redelivery afterwards.

The claim increments `attempt`, and every write is guarded on `(worker, attempt)`. A revived worker's
write matches no rows and is reported back to it as a lost lease. This is also what un-sticks the
crashed-mid-processing case the idempotency store deliberately left open: no timeout on the key is
needed, because the job is the thing that gets retried, not the key.

### A retry that cannot succeed is a delay plus a bill

Failures are classified retryable or terminal, and terminal ones go straight to the dead-letter queue
without spending the retry budget or waiting out a backoff. `BudgetExceeded` is the important member of
that set: the ceiling does not grow, so a call that did not fit will never fit. For a service whose
entire premise is a per-run cost ceiling, retrying past it would be the one unforgivable bug.

Unrecognised exceptions default to retryable, and that direction is deliberate. An unknown transient
error retried three times costs three attempts; an unknown transient error buried on sight costs a
diagnosis nobody gets.

Backoff is exponential with a cap and **full** jitter, meaning the delay is randomised over the whole
interval rather than nudged by a few per cent. The failures that cause retries are usually shared, such
as a provider rate-limiting everyone at once, so without jitter every worker computes the same delay
from the same clock and the herd re-forms on every attempt.

### Retries share the run's ceiling, and a replay does not raise it

The receiver scopes a ledger run to one CI run attempt and leaves this question open on purpose. The
answer is that every triage attempt for an event spends from the same ceiling, because otherwise "retry
up to three times" quietly multiplies the cost cap by three and the guarantee is decoration.

Replay follows from that. Taking a job out of the dead-letter queue grants further attempts by raising
`max_attempts`; it grants no further money. A job that died broke will fail at its first reservation and
come straight back, which is the correct outcome arrived at in one second instead of after three
backoffs. `attempt` is never reset either: how many times the job has really run is a fact, and a
replayed job should read honestly in the table rather than looking like one that has never had trouble.

### The dead-letter queue is a state, not a table

A separate `dead_letters` table means copying the row, and two copies of one job's truth will
eventually disagree about which is current. The queue is a view over `state = 'dead_letter'`, ordered by
when each job got there, so replay is one state transition on the row that was already there.

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
- [x] Webhook receiver with signature verification and idempotency keys
- [x] Run state machine: retries with backoff, dead-letter queue, replay
- [x] GitHub log fetch, log truncation to fit the ceiling, comment posting
- [x] Failure-injection suite: redelivery, worker kill, provider 429s, oversized logs
- [ ] `docker compose up` _(planned)_

## Licence

MIT
