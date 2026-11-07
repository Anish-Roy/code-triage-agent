from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import respx

from code_triage_agent.github.client import (
    GitHubClient,
    GitHubClientError,
    GitHubNotFoundError,
    GitHubRateLimitError,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "github"
BASE = "https://api.github.com"
REPO = "acme/widgets"


def _json(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _client(*, sleep: Callable[[float], None] | None = None, **kwargs: object) -> GitHubClient:
    return GitHubClient(
        token="test-token",
        client=httpx.Client(),
        sleep=sleep or (lambda _: None),
        jitter=lambda: 0.0,
        **kwargs,  # type: ignore[arg-type]
    )


@respx.mock
def test_get_workflow_run() -> None:
    respx.get(f"{BASE}/repos/{REPO}/actions/runs/9001").mock(
        return_value=httpx.Response(200, json=_json("workflow_run.json"))
    )
    client = _client()

    run = client.get_workflow_run(REPO, 9001)

    assert run.id == 9001
    assert run.run_attempt == 1
    assert run.pull_requests[0].number == 42
    client.close()


@respx.mock
def test_list_workflow_jobs_paginates() -> None:
    first_page = {"total_count": 101, "jobs": [_job(f"job-{index}") for index in range(100)]}
    second_page = {"total_count": 101, "jobs": [_job("job-100")]}
    route = respx.get(f"{BASE}/repos/{REPO}/actions/runs/9001/attempts/2/jobs").mock(
        side_effect=[
            httpx.Response(200, json=first_page),
            httpx.Response(200, json=second_page),
        ]
    )
    client = _client()

    jobs = client.list_workflow_jobs(REPO, 9001, attempt=2)

    assert len(jobs) == 101
    assert route.call_count == 2
    assert route.calls[0].request.url.params["page"] == "1"
    assert route.calls[1].request.url.params["page"] == "2"
    client.close()


@respx.mock
def test_download_job_logs_follows_redirect_without_token() -> None:
    archive = _zip_bytes("pytest output")
    api_route = respx.get(f"{BASE}/repos/{REPO}/actions/jobs/5001/logs").mock(
        return_value=httpx.Response(302, headers={"location": "https://logs.example.com/job/5001"})
    )
    blob_route = respx.get("https://logs.example.com/job/5001").mock(
        return_value=httpx.Response(200, content=archive)
    )
    client = _client()

    data = client.download_job_logs(REPO, 5001)

    assert data == archive
    assert api_route.called
    assert blob_route.called
    assert blob_route.calls[0].request.headers.get("authorization") is None
    client.close()


@respx.mock
def test_download_job_logs_rejects_non_https_redirect() -> None:
    respx.get(f"{BASE}/repos/{REPO}/actions/jobs/5001/logs").mock(
        return_value=httpx.Response(302, headers={"location": "http://evil.example/log"})
    )
    client = _client()

    with pytest.raises(GitHubClientError, match="https"):
        client.download_job_logs(REPO, 5001)
    client.close()


@respx.mock
def test_get_pull_request_diff_uses_diff_accept_header() -> None:
    route = respx.get(f"{BASE}/repos/{REPO}/pulls/42").mock(
        return_value=httpx.Response(200, text="diff --git a/x b/x")
    )
    client = _client()

    diff = client.get_pull_request_diff(REPO, 42)

    assert diff.startswith("diff --git")
    assert route.calls[0].request.headers["accept"] == "application/vnd.github.diff"
    client.close()


@respx.mock
def test_retries_server_errors_with_backoff() -> None:
    delays: list[float] = []
    respx.get(f"{BASE}/repos/{REPO}/actions/runs/9001").mock(
        side_effect=[
            httpx.Response(500, text="oops"),
            httpx.Response(502, text="oops"),
            httpx.Response(200, json=_json("workflow_run.json")),
        ]
    )
    client = _client(sleep=delays.append)

    run = client.get_workflow_run(REPO, 9001)

    assert run.id == 9001
    assert delays == [0.5, 1.0]
    client.close()


@respx.mock
def test_honours_retry_after_on_rate_limit() -> None:
    delays: list[float] = []
    respx.get(f"{BASE}/repos/{REPO}/actions/runs/9001").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "2"}),
            httpx.Response(200, json=_json("workflow_run.json")),
        ]
    )
    client = _client(sleep=delays.append)

    client.get_workflow_run(REPO, 9001)

    assert delays == [2.0]
    client.close()


@respx.mock
def test_rate_limit_exhaustion_raises() -> None:
    respx.get(f"{BASE}/repos/{REPO}/actions/runs/9001").mock(
        return_value=httpx.Response(429, headers={"retry-after": "0"})
    )
    client = _client(max_retries=1)

    with pytest.raises(GitHubRateLimitError):
        client.get_workflow_run(REPO, 9001)
    client.close()


@respx.mock
def test_not_found_raises_typed_error() -> None:
    respx.get(f"{BASE}/repos/{REPO}/actions/runs/123").mock(
        return_value=httpx.Response(404, text="Not Found")
    )
    client = _client()

    with pytest.raises(GitHubNotFoundError):
        client.get_workflow_run(REPO, 123)
    client.close()


class RefreshAuth:
    def __init__(self) -> None:
        self.calls = 0
        self.invalidations = 0

    def get_token(self, *, force_refresh: bool = False) -> str:
        self.calls += 1
        return "old-token" if self.calls == 1 else "new-token"

    def invalidate(self) -> None:
        self.invalidations += 1


@respx.mock
def test_401_forces_token_refresh_and_retry() -> None:
    route = respx.get(f"{BASE}/repos/{REPO}/actions/runs/9001").mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, json=_json("workflow_run.json")),
        ]
    )
    auth = RefreshAuth()
    client = GitHubClient(auth=auth, client=httpx.Client(), sleep=lambda _: None)

    client.get_workflow_run(REPO, 9001)

    assert auth.invalidations == 1
    assert route.calls[0].request.headers["authorization"] == "Bearer old-token"
    assert route.calls[1].request.headers["authorization"] == "Bearer new-token"
    client.close()


@respx.mock
def test_post_comment_returns_id() -> None:
    route = respx.post(f"{BASE}/repos/{REPO}/issues/42/comments").mock(
        return_value=httpx.Response(201, json={"id": 77})
    )
    client = _client()

    comment_id = client.post_issue_comment(REPO, 42, "hello")

    assert comment_id == 77
    assert json.loads(route.calls[0].request.content) == {"body": "hello"}
    client.close()


@respx.mock
def test_rerun_failed_jobs() -> None:
    route = respx.post(f"{BASE}/repos/{REPO}/actions/runs/9001/rerun-failed-jobs").mock(
        return_value=httpx.Response(201)
    )
    client = _client()

    client.rerun_failed_jobs(REPO, 9001)

    assert route.called
    client.close()


@respx.mock
def test_fix_pr_helpers() -> None:
    branch_route = respx.post(f"{BASE}/repos/{REPO}/git/refs").mock(
        return_value=httpx.Response(201, json={})
    )
    file_route = respx.put(f"{BASE}/repos/{REPO}/contents/src/fix.py").mock(
        return_value=httpx.Response(201, json={"content": {"sha": "file-sha"}})
    )
    pr_route = respx.post(f"{BASE}/repos/{REPO}/pulls").mock(
        return_value=httpx.Response(
            201,
            json={
                "number": 99,
                "state": "open",
                "head": {"ref": "triage/fix", "sha": "abc"},
                "base": {"ref": "main", "sha": "def"},
            },
        )
    )
    client = _client()

    client.create_branch(REPO, "triage/fix", "abc")
    file_sha = client.put_file(
        REPO,
        "src/fix.py",
        content="print('fixed')\n",
        message="fix",
        branch="triage/fix",
    )
    number = client.create_pull_request(
        REPO,
        title="fix",
        body="body",
        head="triage/fix",
        base="main",
    )

    assert file_sha == "file-sha"
    assert number == 99
    assert branch_route.called and file_route.called and pr_route.called
    client.close()


@respx.mock
def test_unexpected_status_raises_client_error() -> None:
    respx.get(f"{BASE}/repos/{REPO}/actions/runs/9001").mock(
        return_value=httpx.Response(422, text="bad")
    )
    client = _client()

    with pytest.raises(GitHubClientError) as excinfo:
        client.get_workflow_run(REPO, 9001)
    assert excinfo.value.status_code == 422
    client.close()


def _job(name: str) -> dict[str, object]:
    return {"id": abs(hash(name)) % 100000, "name": name, "conclusion": "success", "steps": []}


def _zip_bytes(text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("0_test.txt", text)
    return buffer.getvalue()
