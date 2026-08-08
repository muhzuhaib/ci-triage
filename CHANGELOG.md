# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the major version is `0`, the
public API may change between minor versions.

Versions before 0.5.0 were developed under a version number in `pyproject.toml` but were never
tagged, so `v0.5.0` is the first git tag in this repository. The entries below were reconstructed
from the commit history rather than written at the time; each one names the roadmap item it
completed. Every version from 0.5.0 onward is tagged when it is released.

## [Unreleased]

Nothing yet.

## [0.5.0] - 2026-08-07

The receiver became something you can deploy rather than only import.

### Added

- `ci_triage.app`, an ASGI application that binds the receiver to HTTP, with a `/healthz` endpoint
  that runs a real query rather than answering a constant.
- A `Dockerfile` and a `compose.yaml` bringing up the receiver and Postgres with one command.
- `tools/send_delivery.py`, which posts correctly signed deliveries at a running stack from outside
  the container.
- A `docker compose up` job in CI that starts the stack, posts real deliveries at it, and then reads
  Postgres to confirm what those deliveries left behind.

### Fixed

- A crash between claiming an idempotency key and creating the job left the delivery remembered as
  seen with nothing queued, and the redelivery that would have repaired it was then discarded as a
  duplicate. The handler now enqueues on duplicates as well, which is safe because both writes are
  idempotent by a database constraint. Disabling the fix fails exactly one test.
- The version was stated in two places that could drift apart. It is now stated once.

## [0.4.0] - 2026-08-06

### Added

- A failure-injection suite that kills the worker at each seam of a triage, using an exception that
  the ordinary error handling cannot catch, so the test proves recovery rather than proving the
  handler runs.
- `Ledger.reclaim`, which releases a hold left standing by a worker that stopped existing. It reuses
  the queue's `attempt` counter as a fencing token instead of guessing from a timeout, because a hold
  standing for five minutes looks exactly like a call that has been running for five minutes.

### Fixed

- A worker killed between reserving and settling left its reservation standing for ever, so a later
  run could be dead-lettered for want of money it had never spent, with nothing recording why. The
  suite above found this on its first run, after 235 passing tests had walked past it.

## [0.3.0] - 2026-08-04

The GitHub side: reading the failure and answering it.

### Added

- Fetching the logs of the jobs that actually failed, and truncating them tail first, because the
  error is at the end.
- Posting exactly one comment per run.
- Honouring `Retry-After` on rate limits rather than guessing a backoff.
- Recovering the run coordinates from the ledger run id, so a retry knows which pull request it
  belongs to.
- An end-to-end triage of one failed run under the spend ceiling.

## [0.2.0] - 2026-07-23

Admission, cost and the run lifecycle.

### Added

- A dated model price table carrying `fetched_at` and a per-entry source URL, with expiry treated as
  wrong and mere age treated as suspect.
- Worst-case cost estimation expressed as a plan rather than a number, driven as one mechanism with
  the ledger.
- Webhook signature verification over the raw request bytes, in constant time, refusing an empty
  secret and rejecting the legacy SHA-1 header.
- Exactly-once admission, with a single `INSERT` as the claim and the primary key arbitrating the
  race.
- The run state machine: retry, dead-letter and replay, with the clock passed in as an argument.
- A claim that hands one job to exactly one worker, shipped with a control case that double-claims.

## [0.1.0] - 2026-07-22

### Added

- The per-run spend ledger, with reservation, commit and release evaluated inside the database's own
  lock so the ceiling condition cannot be raced.
- Money as an integer count of micro-dollars, never a float.
- The schema, including the SQLite settings correctness depends on.
- A concurrency test racing 16 threads at one ceiling, carrying a deliberately naive ledger as a
  control that demonstrably overspends.
- CI running the same suite on both SQLite and Postgres.
- A characters-per-token ratio measured on real CI log content rather than assumed.

[Unreleased]: https://github.com/muhzuhaib/ci-triage/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/muhzuhaib/ci-triage/releases/tag/v0.5.0
