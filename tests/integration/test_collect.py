from __future__ import annotations

import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from code_triage_agent.collector.collect import Collector, CollectorError
from code_triage_agent.collector.snapshot import load_snapshot
from code_triage_agent.config.loader import load_config
from code_triage_agent.config.schema import MemoryNote
from code_triage_agent.db import Database, get_event, get_signature, list_runs
from code_triage_agent.github.client import GitHubNotFoundError
from code_triage_agent.github.models import GitHubJob, GitHubPullRequest, GitHubWorkflowRun
from code_triage_agent.github.webhook import compute_signature
from code_triage_agent.jobs.processor import CollectProcessor
from code_triage_agent.jobs.worker import Worker
from code_triage_agent.models import EventRecord
from code_triage_agent.server.app import create_app
from code_triage_agent.settings import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)

CANARIES = [
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "AKIAIOSFODNN7EXAMPLE",
    "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "hunter2-super-secret",
    "json-secret-value",
    "s3cr3t-pass",
    "abcdef0123456789abcdef0123456789",
]


def _json(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / "github" / name).read_text(encoding="utf-8"))


class FixtureGitHubClient:
    def __init__(
        self,
        *,
        log_text: str | None = None,
        diff: str | None = None,
        jobs: dict[str, object] | None = None,
        pull_request_missing: bool = False,
    ) -> None:
        self._log_text = (
            log_text
            if log_text is not None
            else (FIXTURES / "logs" / "malicious_job.log").read_text(encoding="utf-8")
        )
        self._diff = diff if diff is not None else (FIXTURES / "github" / "diff.patch").read_text()
        self._jobs = jobs or _json("jobs.json")
        self._pull_request_missing = pull_request_missing
        self.downloads: list[int] = []

    def get_workflow_run(self, repo: str, run_id: int) -> GitHubWorkflowRun:
        return GitHubWorkflowRun.model_validate(_json("workflow_run.json"))

    def list_workflow_jobs(
        self, repo: str, run_id: int, *, attempt: int | None = None
    ) -> list[GitHubJob]:
        return [GitHubJob.model_validate(job) for job in self._jobs["jobs"]]  # type: ignore[index]

    def download_job_logs(self, repo: str, job_id: int) -> bytes:
        self.downloads.append(job_id)
        return self._log_text.encode("utf-8")

    def get_pull_request(self, repo: str, number: int) -> GitHubPullRequest:
        if self._pull_request_missing:
            raise GitHubNotFoundError("pull request deleted", status_code=404)
        return GitHubPullRequest.model_validate(_json("pull_request.json"))

    def get_pull_request_diff(self, repo: str, number: int) -> str:
        return self._diff


def _collector(
    *,
    client: FixtureGitHubClient,
    config,
    database: Database,
    snapshot_root: Path,
) -> Collector:  # type: ignore[no-untyped-def]
    return Collector(
        client=client,
        config=config,
        database=database,
        snapshot_root=snapshot_root,
        clock=lambda: NOW,
    )


def test_collect_produces_complete_redacted_snapshot(
    database: Database,
    make_event,  # type: ignore[no-untyped-def]
    project_config,  # type: ignore[no-untyped-def]
    settings,  # type: ignore[no-untyped-def]
) -> None:
    event: EventRecord = make_event()
    collector = _collector(
        client=FixtureGitHubClient(),
        config=project_config,
        database=database,
        snapshot_root=settings.snapshot_root,
    )

    result = collector.collect(event)

    assert result.run.status == "collected"
    assert result.run.snapshot_path == str(result.snapshot.directory)
    assert result.signature.startswith("sig_v1:")
    assert result.metadata.memory_matches == 0

    loaded = load_snapshot(result.snapshot.directory)
    assert loaded.metadata.repo == "acme/widgets"
    assert loaded.metadata.workflow_run_id == 9001
    assert loaded.metadata.workflow_name == "CI"
    assert loaded.metadata.source == "pull_request"
    assert loaded.metadata.pr_number == 42
    assert loaded.metadata.base_sha == "fedcba9876543210fedcba9876543210fedcba98"
    assert loaded.metadata.head_sha == "abc1234def567890abc1234def567890abc12345"
    assert loaded.metadata.diff_available is True
    assert loaded.metadata.failed_jobs[0].name == "test (3.13)"
    assert loaded.metadata.failed_jobs[0].failed_steps[0].name == "Run pytest"
    assert loaded.metadata.trim[0].kept_lines > 0
    assert loaded.metadata.redaction_counts

    for canary in CANARIES:
        assert canary not in loaded.logs
        assert canary not in loaded.diff
    assert "[REDACTED:github_token]" in loaded.logs
    assert "[REDACTED:private_key]" in loaded.logs
    assert loaded.signature == result.signature
    assert loaded.diff.startswith("diff --git")

    with database.connection() as connection:
        record = get_signature(connection, result.signature)
    assert record is not None
    assert record.count == 1
    assert record.repo == "acme/widgets"
    assert CANARIES[0] not in record.exemplar


def test_signature_is_stable_across_collections(
    database: Database,
    make_event,  # type: ignore[no-untyped-def]
    project_config,  # type: ignore[no-untyped-def]
    settings,  # type: ignore[no-untyped-def]
) -> None:
    event = make_event()
    client = FixtureGitHubClient()
    collector = _collector(
        client=client,
        config=project_config,
        database=database,
        snapshot_root=settings.snapshot_root,
    )

    first = collector.collect(event)
    second = collector.collect(event)

    assert first.signature == second.signature
    with database.connection() as connection:
        record = get_signature(connection, first.signature)
        runs = list_runs(connection, event_id=event.id)
    assert record is not None
    assert record.count == 2
    assert len(runs) == 2


def test_memory_notes_are_matched_by_signature(
    database: Database,
    make_event,  # type: ignore[no-untyped-def]
    project_config,  # type: ignore[no-untyped-def]
    settings,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    event = make_event()
    first = _collector(
        client=FixtureGitHubClient(),
        config=project_config,
        database=database,
        snapshot_root=settings.snapshot_root,
    ).collect(event)

    config_dir = tmp_path / "config-with-memory"
    shutil.copytree(PROJECT_ROOT / "config", config_dir)
    note = MemoryNote(
        signature=first.signature,
        repo="acme/widgets",
        note="Integration timeouts are infra, not flaky.",
        confidence="high",
        evidence_runs=["r_123"],
        created_at=NOW,
        source="human",
    )
    with (config_dir / "memory" / "known_failures.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(note.model_dump(mode="json")) + "\n")
    config_with_memory = load_config(config_dir)

    second = _collector(
        client=FixtureGitHubClient(),
        config=config_with_memory,
        database=database,
        snapshot_root=settings.snapshot_root,
    ).collect(event)

    assert second.metadata.memory_matches == 1
    loaded = load_snapshot(second.snapshot.directory)
    assert len(loaded.memory) == 1
    assert loaded.memory[0].note.startswith("Integration timeouts")


def test_diff_is_redacted(
    database: Database,
    make_event,  # type: ignore[no-untyped-def]
    project_config,  # type: ignore[no-untyped-def]
    settings,  # type: ignore[no-untyped-def]
) -> None:
    event = make_event()
    canary = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    diff = f"+GITHUB_TOKEN={canary}\n"
    result = _collector(
        client=FixtureGitHubClient(diff=diff),
        config=project_config,
        database=database,
        snapshot_root=settings.snapshot_root,
    ).collect(event)

    loaded = load_snapshot(result.snapshot.directory)
    assert canary not in loaded.diff
    assert "[REDACTED:" in loaded.diff


def test_collection_failure_records_failed_run(
    database: Database,
    make_event,  # type: ignore[no-untyped-def]
    project_config,  # type: ignore[no-untyped-def]
    settings,  # type: ignore[no-untyped-def]
) -> None:
    event = make_event()
    empty_jobs = {"total_count": 1, "jobs": [{"id": 1, "name": "build", "conclusion": "success"}]}
    collector = _collector(
        client=FixtureGitHubClient(jobs=empty_jobs),
        config=project_config,
        database=database,
        snapshot_root=settings.snapshot_root,
    )

    with pytest.raises(CollectorError, match="no failed jobs"):
        collector.collect(event)

    with database.connection() as connection:
        runs = list_runs(connection, event_id=event.id)
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert runs[0].summary is not None
    assert "no failed jobs" in runs[0].summary


def test_only_three_failed_jobs_are_downloaded(
    database: Database,
    make_event,  # type: ignore[no-untyped-def]
    project_config,  # type: ignore[no-untyped-def]
    settings,  # type: ignore[no-untyped-def]
) -> None:
    event = make_event()
    jobs = {
        "total_count": 5,
        "jobs": [
            {
                "id": 6000 + index,
                "name": f"job-{index}",
                "conclusion": "failure",
                "steps": [
                    {
                        "number": 1,
                        "name": "Run pytest",
                        "conclusion": "failure",
                    }
                ],
            }
            for index in range(5)
        ],
    }
    client = FixtureGitHubClient(jobs=jobs)
    result = _collector(
        client=client,
        config=project_config,
        database=database,
        snapshot_root=settings.snapshot_root,
    ).collect(event)

    assert client.downloads == [6000, 6001, 6002]
    assert len(result.metadata.failed_jobs) == 5
    assert len(result.metadata.trim) == 3
    assert result.metadata.dropped_jobs == 2


def test_missing_pull_request_degrades_to_no_diff(
    database: Database,
    make_event,  # type: ignore[no-untyped-def]
    project_config,  # type: ignore[no-untyped-def]
    settings,  # type: ignore[no-untyped-def]
) -> None:
    event = make_event()
    result = _collector(
        client=FixtureGitHubClient(pull_request_missing=True),
        config=project_config,
        database=database,
        snapshot_root=settings.snapshot_root,
    ).collect(event)

    assert result.run.status == "collected"
    assert result.metadata.pr_number == 42
    assert result.metadata.diff_available is False


def test_collector_never_imports_an_llm(
    database: Database,
    make_event,  # type: ignore[no-untyped-def]
    project_config,  # type: ignore[no-untyped-def]
    settings,  # type: ignore[no-untyped-def]
) -> None:
    _collector(
        client=FixtureGitHubClient(),
        config=project_config,
        database=database,
        snapshot_root=settings.snapshot_root,
    ).collect(make_event())

    forbidden = {"code_triage_agent.runner.llm", "anthropic", "openai"}
    assert forbidden.isdisjoint(sys.modules)


def test_webhook_to_snapshot_pipeline(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    webhook_secret = "integration-secret"
    settings = Settings(
        _env_file=None,
        config_dir=PROJECT_ROOT / "config",
        database_path=tmp_path / "var" / "triage.db",
        snapshot_root=tmp_path / "snapshots",
        event_root=tmp_path / "events",
        github_webhook_secret=webhook_secret,
    )
    app = create_app(settings)
    body = (FIXTURES / "github" / "workflow_run_event.json").read_bytes()
    response = TestClient(app).post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "workflow_run",
            "X-GitHub-Delivery": "pipeline-delivery",
            "X-Hub-Signature-256": compute_signature(webhook_secret, body),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 202
    assert response.json()["status"] == "accepted"

    database = Database(settings.database_path)
    config = load_config(PROJECT_ROOT / "config")
    collector = Collector(
        client=FixtureGitHubClient(),
        config=config,
        database=database,
        snapshot_root=settings.snapshot_root,
        clock=lambda: NOW,
    )
    processor = CollectProcessor(collector=collector)
    worker = Worker(database=database, processor=processor, clock=lambda: NOW)

    assert worker.run_once() is True

    with database.connection() as connection:
        event = get_event(connection, response.json()["event_id"])
        runs = list_runs(connection, event_id=event.id if event else -1)
    assert event is not None
    assert event.status == "done"
    assert len(runs) == 1
    assert runs[0].status == "collected"
    snapshot = load_snapshot(Path(runs[0].snapshot_path or ""))
    assert snapshot.metadata.pr_number == 42
    assert snapshot.metadata.signature.startswith("sig_v1:")
    assert "AssertionError: expected 200 got 500" in snapshot.logs
