"""Typed GitHub REST client with retries, rate-limit handling, and logging."""

from __future__ import annotations

import base64
import logging
import random
import time
from collections.abc import Callable
from typing import Any

import httpx

from code_triage_agent.github.auth import GitHubAuthError, TokenProvider
from code_triage_agent.github.models import (
    GitHubIssueComment,
    GitHubJob,
    GitHubJobsResponse,
    GitHubPullRequest,
    GitHubWorkflowRun,
)

DEFAULT_API_URL = "https://api.github.com"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_PAGES = 10
_PAGE_SIZE = 100

logger = logging.getLogger(__name__)


class GitHubClientError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubNotFoundError(GitHubClientError):
    pass


class GitHubRateLimitError(GitHubClientError):
    pass


class GitHubClient:
    """Small, typed wrapper around the GitHub REST endpoints we need.

    Retries use exponential backoff with jitter for network errors and 5xx
    responses. A single 401 forces a token refresh. Rate-limit responses are
    honoured with a capped wait so a worker cannot hang indefinitely.
    """

    def __init__(
        self,
        *,
        auth: TokenProvider | None = None,
        token: str | None = None,
        api_url: str = DEFAULT_API_URL,
        client: httpx.Client | None = None,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        max_backoff: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if auth is None and token is None:
            raise ValueError("GitHubClient requires either auth or token")
        self._auth = auth
        self._token = token
        self._api_url = api_url.rstrip("/")
        self._client = client or httpx.Client(timeout=30.0, follow_redirects=False)
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._max_backoff = max_backoff
        self._sleep = sleep
        self._jitter = jitter
        self._clock = clock

    def close(self) -> None:
        self._client.close()

    # -- Reads ---------------------------------------------------------

    def get_workflow_run(self, repo: str, run_id: int) -> GitHubWorkflowRun:
        response = self._request("GET", f"/repos/{repo}/actions/runs/{run_id}")
        return GitHubWorkflowRun.model_validate(response.json())

    def list_workflow_jobs(
        self,
        repo: str,
        run_id: int,
        *,
        attempt: int | None = None,
    ) -> list[GitHubJob]:
        if attempt is None:
            path = f"/repos/{repo}/actions/runs/{run_id}/jobs"
        else:
            path = f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs"

        jobs: list[GitHubJob] = []
        for page in range(1, _MAX_PAGES + 1):
            response = self._request(
                "GET",
                path,
                params={"per_page": _PAGE_SIZE, "page": page},
            )
            parsed = GitHubJobsResponse.model_validate(response.json())
            jobs.extend(parsed.jobs)
            if len(parsed.jobs) < _PAGE_SIZE or len(jobs) >= parsed.total_count:
                break
        return jobs

    def download_job_logs(self, repo: str, job_id: int) -> bytes:
        """Download a job log, following GitHub's redirect without the token."""
        response = self._request(
            "GET",
            f"/repos/{repo}/actions/jobs/{job_id}/logs",
            expected=(200, *_REDIRECT_STATUSES),
        )
        if response.status_code in _REDIRECT_STATUSES:
            location = response.headers.get("location")
            if not location:
                raise GitHubClientError("job log redirect had no Location header")
            if not location.startswith("https://"):
                raise GitHubClientError("job log redirect must use https")
            try:
                response = self._client.get(location, follow_redirects=True, timeout=60.0)
            except httpx.HTTPError as exc:
                raise GitHubClientError(f"job log download failed: {exc}") from exc
            if response.status_code != 200:
                raise GitHubClientError(
                    f"job log download returned {response.status_code}",
                    status_code=response.status_code,
                )
        return response.content

    def get_pull_request(self, repo: str, number: int) -> GitHubPullRequest:
        response = self._request("GET", f"/repos/{repo}/pulls/{number}")
        return GitHubPullRequest.model_validate(response.json())

    def get_pull_request_diff(self, repo: str, number: int) -> str:
        response = self._request(
            "GET",
            f"/repos/{repo}/pulls/{number}",
            headers={"Accept": "application/vnd.github.diff"},
        )
        return response.text

    # -- Writes --------------------------------------------------------

    def post_issue_comment(self, repo: str, number: int, body: str) -> int:
        response = self._request(
            "POST",
            f"/repos/{repo}/issues/{number}/comments",
            json_body={"body": body},
            expected=(201,),
        )
        return GitHubIssueComment.model_validate(response.json()).id

    def rerun_failed_jobs(self, repo: str, run_id: int) -> None:
        self._request(
            "POST",
            f"/repos/{repo}/actions/runs/{run_id}/rerun-failed-jobs",
            expected=(201,),
        )

    def create_branch(self, repo: str, branch: str, sha: str) -> None:
        self._request(
            "POST",
            f"/repos/{repo}/git/refs",
            json_body={"ref": f"refs/heads/{branch}", "sha": sha},
            expected=(201,),
        )

    def put_file(
        self,
        repo: str,
        path: str,
        *,
        content: str,
        message: str,
        branch: str,
        sha: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha is not None:
            payload["sha"] = sha
        response = self._request(
            "PUT",
            f"/repos/{repo}/contents/{path.lstrip('/')}",
            json_body=payload,
            expected=(200, 201),
        )
        content_obj = response.json().get("content") or {}
        file_sha = content_obj.get("sha")
        if not isinstance(file_sha, str):
            raise GitHubClientError("contents API response did not include a file sha")
        return file_sha

    def create_pull_request(
        self,
        repo: str,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> int:
        response = self._request(
            "POST",
            f"/repos/{repo}/pulls",
            json_body={"title": title, "body": body, "head": head, "base": base},
            expected=(201,),
        )
        return GitHubPullRequest.model_validate(response.json()).number

    # -- Internals -----------------------------------------------------

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        token = self._token
        if self._auth is not None:
            try:
                token = self._auth.get_token()
            except GitHubAuthError as exc:
                raise GitHubClientError(f"could not obtain GitHub token: {exc}") from exc
        if not token:
            raise GitHubClientError("no GitHub token available")
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "code-triage-agent",
        }
        if extra:
            headers.update(extra)
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> httpx.Response:
        url = f"{self._api_url}{path}"
        attempt = 0
        auth_retried = False

        while True:
            try:
                response = self._client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=self._headers(headers),
                    follow_redirects=False,
                )
            except httpx.HTTPError as exc:
                if attempt >= self._max_retries:
                    raise GitHubClientError(f"{method} {path} failed: {exc}") from exc
                self._backoff(attempt)
                attempt += 1
                continue

            if response.status_code == 401 and self._auth is not None and not auth_retried:
                logger.info("github token rejected; refreshing", extra={"path": path})
                self._auth.invalidate()
                auth_retried = True
                continue

            if _is_rate_limited(response):
                if attempt >= self._max_retries:
                    raise GitHubRateLimitError(
                        f"{method} {path} rate limited", status_code=response.status_code
                    )
                self._sleep(self._rate_limit_wait(response, attempt))
                attempt += 1
                continue

            if 500 <= response.status_code < 600:
                if attempt >= self._max_retries:
                    raise GitHubClientError(
                        f"{method} {path} returned {response.status_code}",
                        status_code=response.status_code,
                    )
                self._backoff(attempt)
                attempt += 1
                continue

            if response.status_code not in expected:
                detail = response.text[:500]
                if response.status_code == 404:
                    raise GitHubNotFoundError(
                        f"{method} {path} returned 404: {detail}",
                        status_code=404,
                    )
                raise GitHubClientError(
                    f"{method} {path} returned {response.status_code}: {detail}",
                    status_code=response.status_code,
                )

            logger.debug(
                "github request",
                extra={"method": method, "path": path, "status": response.status_code},
            )
            return response

    def _backoff(self, attempt: int) -> None:
        delay = min(self._backoff_base * (2**attempt), self._max_backoff)
        self._sleep(delay + self._jitter() * self._backoff_base)

    def _rate_limit_wait(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("retry-after")
        if retry_after is not None:
            try:
                return min(max(float(retry_after), 0.0), 60.0)
            except ValueError:
                pass

        reset = response.headers.get("x-ratelimit-reset")
        if reset is not None:
            try:
                return min(max(float(reset) - self._clock(), 0.0), 60.0)
            except ValueError:
                pass

        backoff: float = self._backoff_base * float(2**attempt)
        return min(backoff, 60.0)


def _is_rate_limited(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    if response.headers.get("x-ratelimit-remaining") == "0":
        return True
    return "retry-after" in response.headers
