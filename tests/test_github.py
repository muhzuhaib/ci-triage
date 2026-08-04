"""The GitHub client, exercised over ``httpx.MockTransport``.

The transport is replaced; nothing else is. The request building, the redirect
handling, the pagination and the error classification are the real code paths,
which is the difference between testing the client and testing a stand-in for
it.
"""

from __future__ import annotations

import json

import httpx
import pytest

from ci_triage.github import (
    GitHubClient,
    NoPullRequest,
    PermanentGitHubError,
    TransientGitHubError,
    marker_for,
)
from ci_triage.runs import TerminalError

API = "https://api.github.com"


def client_for(handler) -> GitHubClient:
    transport = httpx.MockTransport(handler)
    return GitHubClient(
        "ghp_test_token",
        client=httpx.Client(transport=transport, follow_redirects=False),
    )


def json_response(payload, *, status=200, headers=None) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(payload), headers=headers or {})


# ----------------------------------------------------------------------- jobs


def test_failed_jobs_keeps_only_the_jobs_that_failed():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return json_response(
            {
                "jobs": [
                    {"id": 1, "name": "lint", "conclusion": "success"},
                    {"id": 2, "name": "tests (3.10)", "conclusion": "failure"},
                    {"id": 3, "name": "tests (3.13)", "conclusion": "timed_out"},
                    {"id": 4, "name": "docs", "conclusion": "cancelled"},
                    {"id": 5, "name": "still going", "conclusion": None},
                ]
            }
        )

    jobs = client_for(handler).failed_jobs("o/r", 42, run_attempt=2)

    assert [j.id for j in jobs] == [2, 3]
    assert [j.name for j in jobs] == ["tests (3.10)", "tests (3.13)"]
    assert seen["auth"] == "Bearer ghp_test_token"


def test_failed_jobs_asks_for_the_attempt_the_delivery_was_about():
    """A re-run reuses the run id, so the attempt has to be in the URL.

    Without it the client can read the jobs of a *later* attempt than the
    delivery that is being triaged, and diagnose a failure that has already
    been fixed.
    """
    urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return json_response({"jobs": []})

    c = client_for(handler)
    c.failed_jobs("o/r", 42, run_attempt=3)
    c.failed_jobs("o/r", 42)

    assert urls[0].startswith(f"{API}/repos/o/r/actions/runs/42/attempts/3/jobs")
    assert urls[1].startswith(f"{API}/repos/o/r/actions/runs/42/jobs")
    assert "filter=latest" in urls[1]
    assert "filter" not in urls[0]


def test_failed_jobs_follows_pagination():
    page_two = f"{API}/repos/o/r/actions/runs/42/jobs?page=2"

    def handler(request: httpx.Request) -> httpx.Response:
        if "page=2" in str(request.url):
            return json_response({"jobs": [{"id": 9, "name": "b", "conclusion": "failure"}]})
        return json_response(
            {"jobs": [{"id": 8, "name": "a", "conclusion": "failure"}]},
            headers={"link": f'<{page_two}>; rel="next"'},
        )

    jobs = client_for(handler).failed_jobs("o/r", 42)

    assert [j.id for j in jobs] == [8, 9]


def test_job_ref_carries_the_head_sha_for_the_pull_request_lookup():
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            {"jobs": [{"id": 1, "name": "t", "conclusion": "failure", "head_sha": "abc123"}]}
        )

    assert client_for(handler).failed_jobs("o/r", 1)[0].head_sha == "abc123"


# ------------------------------------------------------------------- job logs


def test_job_log_follows_the_redirect_without_forwarding_the_token():
    """The storage host must never see the credential.

    GitHub answers the log endpoint with a 302 to a short-lived storage URL that
    carries its own authorisation. Whether a client strips ``Authorization``
    across origins is its own policy, and a token leak is not something to hold
    by convention, so the redirect is issued here explicitly and this test is
    what keeps it that way.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api.github.com":
            return httpx.Response(
                302, headers={"location": "https://blob.example.com/logs/1?sig=abc"}
            )
        return httpx.Response(200, text="2026-07-29T23:12:55.4607494Z boom\n")

    log = client_for(handler).job_log("o/r", 1)

    assert log == "2026-07-29T23:12:55.4607494Z boom\n"
    assert requests[0].headers.get("authorization") == "Bearer ghp_test_token"
    assert "authorization" not in requests[1].headers


def test_job_log_without_a_location_header_is_permanent():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302)

    with pytest.raises(PermanentGitHubError):
        client_for(handler).job_log("o/r", 1)


def test_a_transport_failure_is_transient():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection reset", request=request)

    with pytest.raises(TransientGitHubError):
        client_for(handler).job_log("o/r", 1)


# ------------------------------------------------------------- classification


def test_a_rate_limited_response_carries_the_servers_own_retry_after():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "63"}, text="slow down")

    with pytest.raises(TransientGitHubError) as caught:
        client_for(handler).failed_jobs("o/r", 1)

    assert caught.value.retry_after_seconds == 63.0


def test_an_exhausted_primary_limit_is_read_from_the_reset_header(monkeypatch):
    """``x-ratelimit-remaining: 0`` plus a reset epoch is the documented form."""
    import ci_triage.github as gh

    monkeypatch.setattr(gh.time, "time", lambda: 1_000.0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1120"},
            text="API rate limit exceeded",
        )

    with pytest.raises(TransientGitHubError) as caught:
        client_for(handler).failed_jobs("o/r", 1)

    assert caught.value.retry_after_seconds == 120.0


def test_a_secondary_limit_without_headers_is_still_transient():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="You have exceeded a secondary rate limit")

    with pytest.raises(TransientGitHubError):
        client_for(handler).failed_jobs("o/r", 1)


def test_a_forbidden_response_with_no_rate_limit_signal_is_permanent():
    """A token without the scope does not grow one while we wait."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Resource not accessible by personal access token")

    with pytest.raises(PermanentGitHubError):
        client_for(handler).failed_jobs("o/r", 1)


def test_a_missing_repository_is_permanent_and_the_state_machine_buries_it():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found")

    with pytest.raises(PermanentGitHubError) as caught:
        client_for(handler).failed_jobs("o/r", 1)

    # The classifier in runs.py needs no knowledge of HTTP for this to work.
    assert isinstance(caught.value, TerminalError)


def test_a_server_error_is_transient():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="Bad gateway")

    with pytest.raises(TransientGitHubError):
        client_for(handler).failed_jobs("o/r", 1)


# ------------------------------------------------------------- pull requests


def test_the_payloads_pull_request_is_used_without_a_request():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request should have been made")

    number = client_for(handler).pull_request_for(
        "o/r", "abc123", from_payload=[{"number": 7}]
    )

    assert number == 7


def test_a_fork_pull_request_is_found_by_its_head_commit():
    """``workflow_run.pull_requests`` is empty for forks, which is the common case."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(f"{API}/repos/o/r/commits/abc123/pulls")
        return json_response([{"number": 11}])

    assert client_for(handler).pull_request_for("o/r", "abc123", from_payload=[]) == 11


def test_no_pull_request_anywhere_is_permanent():
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response([])

    with pytest.raises(NoPullRequest) as caught:
        client_for(handler).pull_request_for("o/r", "abc123")

    assert isinstance(caught.value, TerminalError)


# ------------------------------------------------------------------ comments


def test_a_first_diagnosis_is_created_and_carries_its_marker():
    marker = marker_for("o/r#42.1")
    posted = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return json_response([{"id": 1, "body": "unrelated chatter"}])
        posted["body"] = json.loads(request.content)["body"]
        posted["url"] = str(request.url)
        return json_response({"id": 99, "html_url": "https://github.com/o/r/pull/7#c99"}, status=201)

    result = client_for(handler).upsert_comment("o/r", 7, marker=marker, body="it broke")

    assert result.id == 99
    assert result.updated is False
    assert posted["body"].startswith(marker)
    assert posted["url"] == f"{API}/repos/o/r/issues/7/comments"


def test_a_repeat_edits_the_comment_that_carries_the_marker():
    """No idempotency key exists for creating a comment, so identity lives in it.

    Without the marker a redelivered failure appends a second opinion under the
    first, and the pull request grows one comment per attempt.
    """
    marker = marker_for("o/r#42.1")
    patched = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return json_response(
                [
                    {"id": 1, "body": "unrelated"},
                    {"id": 55, "body": f"{marker}\nan earlier diagnosis"},
                ]
            )
        assert request.method == "PATCH"
        patched["url"] = str(request.url)
        patched["body"] = json.loads(request.content)["body"]
        return json_response({"id": 55, "html_url": "https://github.com/o/r/pull/7#c55"})

    result = client_for(handler).upsert_comment("o/r", 7, marker=marker, body="fresher")

    assert result.updated is True
    assert result.id == 55
    assert patched["url"] == f"{API}/repos/o/r/issues/comments/55"
    assert "fresher" in patched["body"]


def test_another_runs_marker_is_not_mistaken_for_ours():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return json_response([{"id": 3, "body": marker_for("o/r#42.1") + "\nold"}])
        return json_response({"id": 4, "html_url": "u"}, status=201)

    result = client_for(handler).upsert_comment(
        "o/r", 7, marker=marker_for("o/r#42.2"), body="the re-run also failed"
    )

    assert result.updated is False


def test_a_token_is_required():
    with pytest.raises(ValueError):
        GitHubClient("")
