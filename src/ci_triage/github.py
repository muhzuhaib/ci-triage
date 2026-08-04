"""The GitHub side of a triage: read the failed jobs' logs, post one comment.

Lives outside the package's core import on purpose. The ledger, the receiver and
the state machine depend on SQLAlchemy and nothing else, which is what lets the
guarantees be tested in seconds anywhere; this module needs an HTTP client, so
it ships behind ``pip install ci-triage[service]`` and is imported explicitly::

    from ci_triage.github import GitHubClient

Four decisions worth the space.

**The redirect is followed by hand, without the token.** ``GET
/repos/{owner}/{repo}/actions/jobs/{job_id}/logs`` answers ``302`` with a
``Location`` pointing at a storage host, and
`the documentation <https://docs.github.com/en/rest/actions/workflow-jobs>`_
notes the link expires after one minute. Letting the HTTP client follow that
automatically means trusting its redirect policy not to forward the
``Authorization`` header to a third party. Some clients strip it across origins
and some do not, and a credential leak is not a thing to hold by convention, so
the redirect is read and re-issued here with no auth header at all.

**Only the failed jobs' logs are fetched.** The whole-run endpoint returns a zip
of every job, which on a green-except-one matrix is mostly logs of things that
worked. The jobs list carries each job's ``conclusion``, so the failures can be
named first and fetched individually. That also keeps an unbounded archive out
of memory, and it means the budget is spent on text that is about the failure.

**Errors are classified where the facts are, not where the retry is.** A
``403``/``429`` carrying rate-limit headers is transient and carries the
server's own instruction about when to come back; a ``404`` or ``422`` will
answer the same way forever. So the permanent ones subclass
:class:`~ci_triage.runs.TerminalError`, which the state machine's default
classifier already buries without burning the retry budget, and the transient
ones carry ``retry_after_seconds`` for the scheduler to honour. GitHub's
guidance is explicit that ignoring it risks a ban, so it is data the exception
must not drop.

**The comment is identified by a marker inside itself.** Creating a comment has
no idempotency key, so "post exactly once" cannot be asked of the API. Instead
every comment carries an invisible HTML marker naming the run, and posting means
*upserting*: find the marker, edit that comment, or create one if there is none.
The limitation is stated plainly in the README rather than papered over: the
find-then-create is a check-then-act race, and unlike the ledger and the queue
there is no conditional write available to close it. What closes it in practice
is the lease, which means at most one worker is doing this at a time; what the
marker guarantees on its own is that a repeat lands as an edit rather than a
second opinion under the first.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from .runs import TerminalError

DEFAULT_API_URL = "https://api.github.com"
DEFAULT_API_VERSION = "2022-11-28"
DEFAULT_TIMEOUT = 30.0

#: Job conclusions that mean this job is why the run failed. ``cancelled`` and
#: ``skipped`` are excluded for the same reason the receiver excludes them: they
#: are not a defect anyone wants a diagnosis of.
FAILING_CONCLUSIONS = frozenset({"failure", "timed_out"})

#: How many pages of existing comments to read looking for our marker. A pull
#: request with more comments than this is vanishingly rare, and the cap is
#: reported rather than silently truncating the search: see :meth:`upsert_comment`.
_MAX_COMMENT_PAGES = 10

_NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')


class GitHubError(Exception):
    """Base class for failures talking to GitHub."""


class TransientGitHubError(GitHubError):
    """Worth another attempt: a 5xx, a timeout, or a rate limit.

    ``retry_after_seconds`` is the server's own instruction where it gave one.
    :func:`~ci_triage.runs.process_next` reads the attribute off the exception
    without knowing this class exists, which is what keeps the state machine
    free of an HTTP dependency.
    """

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class PermanentGitHubError(GitHubError, TerminalError):
    """A repository that is gone, a request that will never be accepted.

    Subclasses :class:`~ci_triage.runs.TerminalError` so the state machine's
    default classifier dead-letters it immediately. Retrying a 404 three times
    with backoff is a delay plus a bill.
    """


class NoPullRequest(PermanentGitHubError):
    """The run has no pull request to comment on.

    Permanent by construction: a push to a branch with no open pull request will
    not grow one because we waited. The diagnosis is still recorded on the job,
    it simply has nowhere to be posted.
    """


@dataclass(frozen=True)
class JobRef:
    """One job of a workflow run.

    ``head_sha`` is carried because the jobs response already contains it and
    the pull-request lookup needs it. Taking it from here rather than from the
    delivery payload is what lets the worker work from the job row alone.
    """

    id: int
    name: str
    conclusion: str | None
    head_sha: str | None = None


@dataclass(frozen=True)
class PostedComment:
    """Where the diagnosis ended up, and whether it displaced an earlier one."""

    id: int
    url: str
    updated: bool


def marker_for(run_key: str) -> str:
    """The invisible tag that makes a comment findable again.

    An HTML comment renders as nothing, so the identity travels with the text
    the reader sees without appearing in it.
    """
    return f"<!-- ci-triage:{run_key} -->"


def _retry_after_from(headers: Mapping[str, str], now: float | None = None) -> float | None:
    """Read GitHub's documented rate-limit instruction out of the headers.

    Two forms, in the order the documentation gives them: ``retry-after`` in
    seconds, and ``x-ratelimit-reset`` as a UTC epoch second, which applies when
    ``x-ratelimit-remaining`` is ``0``.
    """
    lower = {k.lower(): v for k, v in headers.items()}
    raw = lower.get("retry-after")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    if lower.get("x-ratelimit-remaining") == "0":
        reset = lower.get("x-ratelimit-reset")
        if reset:
            try:
                return max(0.0, float(reset) - (now if now is not None else time.time()))
            except ValueError:
                pass
    return None


class GitHubClient:
    """The calls one triage makes, and nothing else.

    The ``httpx.Client`` is injected rather than built here so tests can hand it
    an ``httpx.MockTransport``: the request building, the redirect handling and
    the error classification are then exercised for real, with only the network
    replaced. A client that is only ever exercised through a hand-written fake
    proves the fake works.
    """

    def __init__(
        self,
        token: str,
        *,
        client: httpx.Client | None = None,
        api_url: str = DEFAULT_API_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if not token:
            raise ValueError("a GitHub token is required")
        self._token = token
        self._api_url = api_url.rstrip("/")
        # follow_redirects stays off: the log download's redirect is handled
        # explicitly below so the token is never sent to the storage host.
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=False)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": DEFAULT_API_VERSION,
        }

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if url.startswith("/"):
            url = f"{self._api_url}{url}"
        try:
            response = self._client.request(method, url, headers=self._headers(), **kwargs)
        except httpx.TimeoutException as exc:
            raise TransientGitHubError(f"{method} {url} timed out: {exc}") from exc
        except httpx.TransportError as exc:
            raise TransientGitHubError(f"{method} {url} failed: {exc}") from exc
        return self._checked(response, method, url)

    def _checked(self, response: httpx.Response, method: str, url: str) -> httpx.Response:
        status = response.status_code
        if status < 400:
            return response

        detail = f"{method} {url} -> {status}"
        if status in (403, 429):
            # Both codes cover the primary and the secondary rate limit, which is
            # why the headers decide rather than the code. A 403 with no
            # rate-limit signal is a permissions problem, and no amount of
            # waiting fixes a token that lacks the scope.
            retry_after = _retry_after_from(response.headers)
            if retry_after is not None:
                raise TransientGitHubError(
                    f"{detail} rate limited; retry after {retry_after:.0f}s",
                    retry_after_seconds=retry_after,
                )
            if "secondary rate limit" in response.text.lower():
                raise TransientGitHubError(f"{detail} secondary rate limit")
            raise PermanentGitHubError(f"{detail} forbidden: {response.text[:200]}")
        if status >= 500:
            raise TransientGitHubError(f"{detail} server error")
        raise PermanentGitHubError(f"{detail} {response.text[:200]}")

    def _paginate(self, url: str, *, params: dict[str, Any] | None = None) -> Iterator[Any]:
        """Walk ``rel="next"`` until it stops, yielding each page's items."""
        response = self._request("GET", url, params=params)
        while True:
            yield response.json()
            match = _NEXT_LINK.search(response.headers.get("link", ""))
            if not match:
                return
            response = self._request("GET", match.group(1))

    # ------------------------------------------------------------------- jobs

    def failed_jobs(
        self, repo: str, run_id: int, *, run_attempt: int | None = None
    ) -> list[JobRef]:
        """The jobs of this run that failed, in the order GitHub lists them.

        Scoped to the attempt when one is given. A re-run reuses the workflow
        run id and only increments the attempt, so asking for the run without
        the attempt can answer with a different attempt's jobs than the delivery
        was about.
        """
        base = f"/repos/{repo}/actions/runs/{run_id}"
        url = f"{base}/attempts/{run_attempt}/jobs" if run_attempt else f"{base}/jobs"
        params = {"per_page": 100}
        if run_attempt is None:
            params["filter"] = "latest"

        jobs: list[JobRef] = []
        for page in self._paginate(url, params=params):
            for job in page.get("jobs", []):
                if job.get("conclusion") in FAILING_CONCLUSIONS:
                    jobs.append(
                        JobRef(
                            id=int(job["id"]),
                            name=str(job.get("name", job["id"])),
                            conclusion=job.get("conclusion"),
                            head_sha=job.get("head_sha"),
                        )
                    )
        return jobs

    def job_log(self, repo: str, job_id: int) -> str:
        """Download one job's log as text.

        The API answers ``302`` and the log lives at the ``Location``. That
        second request is made with no ``Authorization`` header: the URL already
        carries its own short-lived credential, and the token has no business
        being sent to a host outside GitHub's API.
        """
        url = f"{self._api_url}/repos/{repo}/actions/jobs/{job_id}/logs"
        try:
            response = self._client.request("GET", url, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise TransientGitHubError(f"log download timed out: {exc}") from exc
        except httpx.TransportError as exc:
            raise TransientGitHubError(f"log download failed: {exc}") from exc

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            if not location:
                raise PermanentGitHubError("log redirect carried no Location header")
            try:
                response = self._client.request("GET", location)
            except httpx.TimeoutException as exc:
                raise TransientGitHubError(f"log fetch timed out: {exc}") from exc
            except httpx.TransportError as exc:
                raise TransientGitHubError(f"log fetch failed: {exc}") from exc

        self._checked(response, "GET", url)
        return response.text

    # ----------------------------------------------------------------- the PR

    def pull_request_for(
        self, repo: str, head_sha: str, *, from_payload: Sequence[Mapping[str, Any]] = ()
    ) -> int:
        """Which pull request this run belongs to.

        The delivery's own ``workflow_run.pull_requests`` is used when it has
        one, because it costs no request. It is regularly empty though: it does
        not include pull requests from forks, so a repository's most common
        outside contribution is exactly the case it does not cover. The fallback
        asks which pull requests the head commit belongs to.

        :raises NoPullRequest: the run has none, which is normal for a push to a
            branch nobody has opened one for.
        """
        for entry in from_payload:
            number = entry.get("number")
            if isinstance(number, int):
                return number

        for page in self._paginate(
            f"/repos/{repo}/commits/{head_sha}/pulls", params={"per_page": 100}
        ):
            for pull in page:
                number = pull.get("number")
                if isinstance(number, int):
                    return number
        raise NoPullRequest(f"no pull request contains {head_sha} in {repo}")

    # --------------------------------------------------------------- comments

    def find_comment(self, repo: str, issue_number: int, marker: str) -> int | None:
        """The id of our earlier comment on this thread, if it is still there.

        Returns ``None`` when the search reaches :data:`_MAX_COMMENT_PAGES`
        without a hit as well as when there genuinely is none. The two are the
        same decision for the caller, and the cap is small enough to be
        honest about: the cost of being wrong is a second comment, not a wrong
        diagnosis.
        """
        pages = 0
        for page in self._paginate(
            f"/repos/{repo}/issues/{issue_number}/comments", params={"per_page": 100}
        ):
            for comment in page:
                if marker in (comment.get("body") or ""):
                    return int(comment["id"])
            pages += 1
            if pages >= _MAX_COMMENT_PAGES:
                return None
        return None

    def upsert_comment(
        self, repo: str, issue_number: int, *, marker: str, body: str
    ) -> PostedComment:
        """Post the diagnosis, or replace the one already posted for this run.

        The marker is prepended to the body, so the comment this writes can be
        found by the attempt that follows it.
        """
        text = f"{marker}\n{body}"
        existing = self.find_comment(repo, issue_number, marker)
        if existing is not None:
            response = self._request(
                "PATCH", f"/repos/{repo}/issues/comments/{existing}", json={"body": text}
            )
            payload = response.json()
            return PostedComment(int(payload["id"]), payload.get("html_url", ""), True)

        response = self._request(
            "POST", f"/repos/{repo}/issues/{issue_number}/comments", json={"body": text}
        )
        payload = response.json()
        return PostedComment(int(payload["id"]), payload.get("html_url", ""), False)
