"""Measure characters-per-token on text that looks like a CI failure log.

Why this script exists
----------------------
Every LLM cost estimate eventually rests on a characters-to-tokens ratio, and
the number everyone reaches for is "about 4 characters per token". That figure
comes from English prose. CI logs are not English prose: they are stack traces,
absolute paths, hex digests, ISO timestamps and base64. Those tokenise far
worse, so a 4.0 divisor *understates* the token count on exactly the input this
service handles -- and an understated token count is an understated cost, which
is the one direction a budget estimate must never be wrong in.

So the divisor used by :mod:`ci_triage.tokens` is measured rather than assumed.
Run this script to reproduce the table in the README:

    python -m pip install tiktoken
    python tools/measure_token_density.py

``tiktoken`` is deliberately *not* a dependency of the package. It is needed to
justify the constant, not to use it -- the service must run without downloading
a tokeniser at import time.
"""

from __future__ import annotations

import sys

SAMPLES: dict[str, str] = {
    "english prose": """
The test suite failed because the database connection pool was exhausted.
This usually means a connection was checked out and never returned, so the
next request waits forever and eventually times out. Look for a missing
context manager around a session.
""",
    "python traceback": '''
Traceback (most recent call last):
  File "/home/runner/work/svc/svc/src/svc/handlers/webhook.py", line 148, in dispatch
    result = await self._router.route(payload, headers=headers)
  File "/home/runner/work/svc/svc/src/svc/routing/router.py", line 92, in route
    handler = self._registry[event_type]
              ~~~~~~~~~~~~~~^^^^^^^^^^^^
KeyError: 'workflow_run.requested'
''',
    "pytest output": """
FAILED tests/test_budget.py::test_reserve_refuses_over_ceiling - assert 0 == 1
FAILED tests/test_budget.py::test_commit_records_overrun - AssertionError
FAILED tests/test_ledger_concurrency.py::test_sixteen_threads_one_ceiling
=========== 3 failed, 51 passed, 2 skipped, 1 xfailed in 41.83s ============
""",
    "actions timestamps": """
2026-07-23T09:14:02.4821193Z ##[group]Run actions/checkout@v4
2026-07-23T09:14:02.4822014Z with:
2026-07-23T09:14:02.4822530Z   repository: muhzuhaib/ci-triage
2026-07-23T09:14:02.4823097Z   fetch-depth: 1
2026-07-23T09:14:03.1120044Z ##[endgroup]
""",
    "npm build error": """
ERROR in ./src/components/Chart.tsx:41:12
TS2345: Argument of type '{ data: Series[]; onSelect: (i: number) => void; }'
is not assignable to parameter of type 'IntrinsicAttributes & ChartProps'.
  Property 'onSelect' does not exist on type 'IntrinsicAttributes & ChartProps'.
""",
    "docker layer digests": """
#8 sha256:9d3f2b8c1a7e4f60b52d8e9c0a1b3f4d6e7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c
#8 extracting sha256:4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b
#8 DONE 12.4s
""",
    "json payload": """
{"run_id":"18422190381","status":"completed","conclusion":"failure",
"head_sha":"7f3c1a9e2b8d4f6a0c5e7b91d3f2a4c6e8b0d1f3","run_attempt":2,
"repository":{"full_name":"muhzuhaib/ci-triage","private":false}}
""",
    "base64 blob": (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggolNTQ0ZGY4YzRhOWIyZTdkMWYw"
        "Y2E4YjNkNmU5ZjJhMWM0ZDdiOGU1ZjBhM2M2ZDlmMmI1ZThhMWM0ZDdiOGU1"
    ),
}


def main() -> int:
    try:
        import tiktoken
    except ImportError:
        print("pip install tiktoken to run this measurement", file=sys.stderr)
        return 1

    encodings = {
        "o200k_base": tiktoken.get_encoding("o200k_base"),
        "cl100k_base": tiktoken.get_encoding("cl100k_base"),
    }

    print(f"{'sample':<22} {'chars':>7} " + " ".join(f"{n:>13}" for n in encodings))
    print("-" * 62)

    worst = {name: float("inf") for name in encodings}
    for label, text in SAMPLES.items():
        body = text.strip()
        cells = []
        for name, enc in encodings.items():
            ratio = len(body) / len(enc.encode(body))
            worst[name] = min(worst[name], ratio)
            cells.append(f"{ratio:>13.2f}")
        print(f"{label:<22} {len(body):>7} " + " ".join(cells))

    print("-" * 62)
    print(
        f"{'worst case':<22} {'':>7} "
        + " ".join(f"{worst[n]:>13.2f}" for n in encodings)
    )
    print(
        "\nchars_per_token must be set at or below the worst case, never at the\n"
        "prose figure -- the estimate has to hold on the ugliest log, not the\n"
        "average one."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
