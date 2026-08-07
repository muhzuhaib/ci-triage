"""Send one signed ``workflow_run`` delivery to a running receiver.

Two uses, and the second is why it is committed rather than pasted into a CI
step. Locally it is how you see the stack do something after ``docker compose
up``, without waiting for a real CI job to fail. In CI it is the integration
check: the compose stack is brought up and this posts a real HTTP request at
it, so the Dockerfile, the Postgres driver, the schema creation and the
signature path are all exercised by something outside the container.

Standard library only, on purpose. It has to run on a bare GitHub runner with
nothing installed, and the HMAC it computes has to be computed the same way
:func:`ci_triage.signature.compute_signature` does. Twelve lines of ``hmac`` is
a smaller risk than a dependency that has to be installed before the check can
run.

Examples::

    python tools/send_delivery.py --secret hush
    python tools/send_delivery.py --secret hush --expect 200      # a redelivery
    python tools/send_delivery.py --secret wrong --expect 401
    python tools/send_delivery.py --secret hush --conclusion success --expect 200
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import urllib.error
import urllib.request


def build_payload(repo: str, run_id: int, attempt: int, conclusion: str) -> dict:
    """The parts of a real ``workflow_run`` delivery the receiver reads."""
    return {
        "action": "completed",
        "workflow_run": {
            "id": run_id,
            "run_attempt": attempt,
            "conclusion": conclusion,
            "name": "CI",
        },
        "repository": {"full_name": repo, "id": 7},
    }


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8000/webhook")
    parser.add_argument("--secret", required=True)
    parser.add_argument("--repo", default="octo/repo")
    parser.add_argument("--run-id", type=int, default=42)
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--conclusion", default="failure")
    parser.add_argument("--event", default="workflow_run")
    parser.add_argument(
        "--unsigned", action="store_true", help="omit the signature header entirely"
    )
    parser.add_argument(
        "--expect",
        type=int,
        default=None,
        help="exit non-zero unless the response has this status",
    )
    args = parser.parse_args(argv)

    body = json.dumps(
        build_payload(args.repo, args.run_id, args.attempt, args.conclusion)
    ).encode()
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": args.event,
        "X-GitHub-Delivery": f"local-{args.run_id}-{args.attempt}",
    }
    if not args.unsigned:
        headers["X-Hub-Signature-256"] = sign(args.secret, body)

    request = urllib.request.Request(args.url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status, text = response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        # A 401 or a 400 is an answer, not a transport failure. The whole point
        # of this script is to assert on those, so they must not raise past here.
        status, text = exc.code, exc.read().decode()

    print(f"{status} {text}")

    if args.expect is not None and status != args.expect:
        print(f"expected {args.expect}, got {status}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
