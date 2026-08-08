# Contributing

Issues, questions and pull requests are all welcome. This is a small project, so the process is
short.

## Where to put things

- **A question, or an idea you want to talk through first:**
  [Discussions](https://github.com/muhzuhaib/ci-triage/discussions).
- **Something is broken, or the README says something untrue:**
  [open an issue](https://github.com/muhzuhaib/ci-triage/issues/new/choose).
- **A security problem:** do not open an issue. See [SECURITY.md](SECURITY.md).

## Running the tests

No services, no API keys and no network are needed for the default suite.

```bash
git clone https://github.com/muhzuhaib/ci-triage && cd ci-triage
python -m pip install -e ".[dev]"
python -m pytest -q
```

The guarantees this project makes are about concurrency and crashes, and SQLite serialises writers,
which can hide a race that Postgres exposes. So the same suite also runs against Postgres in CI. To
run it that way locally, point it at a database you do not mind losing:

```bash
python -m pip install -e ".[dev,postgres]"
CI_TRIAGE_TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/ci_triage_test python -m pytest -q
```

## What a good pull request looks like here

- **One concern per pull request.** A small diff that does one thing gets read and merged; a large
  one that does four waits.
- **A test that fails without the change.** For anything touching the ledger, the idempotency store
  or the run queue, that means a test which can actually be observed to fail. A concurrency test that
  has never failed is not evidence of anything, which is why the suite ships naive control
  implementations that demonstrably exhibit the bug being prevented.
- **A crash is not a race.** A thread that is racing is still running. If a change affects a resource
  acquired and released by the same process, the test has to stop that process in between, and the
  kill has to be something the ordinary error handling cannot catch.
- **Factual claims get a source.** Prices, protocol headers and API behaviour come from the vendor's
  own page or from a measurement committed alongside the change, never from memory.
- **Keep the README honest.** If a change makes something in it wrong, fix it in the same pull
  request.

CI runs the suite on Python 3.10 and 3.13, against SQLite and Postgres, and starts the
`docker compose` stack and posts real signed deliveries at it. All of it has to be green.

## Style

Nothing is enforced by a formatter. Match the surrounding code, and write comments that say why
something is the way it is rather than restating what the line does.
