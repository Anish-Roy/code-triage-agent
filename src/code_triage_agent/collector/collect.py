"""Collector orchestration: metadata + logs + diff -> signature + snapshot.

This path must never instantiate an LLM client. It is plain code by design.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from code_triage_agent.collector.redact import Redactor
from code_triage_agent.collector.signature import build_signature
from code_triage_agent.collector.snapshot import SnapshotPaths, write_snapshot
from code_triage_agent.collector.trim import MAX_FAILED_JOBS, JobLogInput, trim_logs
from code_triage_agent.config.loader import LoadedConfig
from code_triage_agent.config.schema import MemoryNote
from code_triage_agent.db import (
    Database,
    create_run,
    finish_run,
    new_run_id,
    transaction,
    upsert_signature,
    utcnow,
)
from code_triage_agent.github.client import GitHubNotFoundError
from code_triage_agent.github.logs import extract_log_text
from code_triage_agent.github.models import (
    GitHubJob,
    GitHubPullRequest,
    GitHubStep,
    GitHubWorkflowRun,
)
from code_triage_agent.models import (
    FAILED_CONCLUSIONS,
    EventRecord,
    FailedJobInfo,
    FailedStepInfo,
    RunRecord,
    SnapshotMetadata,
    TrimResult,
    WorkflowRunEvent,
)

logger = logging.getLogger(__name__)


class CollectorError(RuntimeError):
    """Raised when a failure cannot be collected into a usable snapshot."""


class GitHubReadClient(Protocol):
    def get_workflow_run(self, repo: str, run_id: int) -> GitHubWorkflowRun: ...

    def list_workflow_jobs(
        self, repo: str, run_id: int, *, attempt: int | None = None
    ) -> list[GitHubJob]: ...

    def download_job_logs(self, repo: str, job_id: int) -> bytes: ...

    def get_pull_request(self, repo: str, number: int) -> GitHubPullRequest: ...

    def get_pull_request_diff(self, repo: str, number: int) -> str: ...


@dataclass(frozen=True)
class CollectedRun:
    run: RunRecord
    snapshot: SnapshotPaths
    metadata: SnapshotMetadata
    trim: TrimResult
    signature: str


class Collector:
    def __init__(
        self,
        *,
        client: GitHubReadClient,
        config: LoadedConfig,
        database: Database,
        snapshot_root: Path,
        redactor: Redactor | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._config = config
        self._database = database
        self._snapshot_root = snapshot_root
        self._redactor = redactor or Redactor()
        self._clock = clock or utcnow

    def collect(self, event: EventRecord) -> CollectedRun:
        payload = self._load_payload(event)
        wrapped = WorkflowRunEvent.model_validate(payload)
        run_id = new_run_id()
        now = self._clock()

        with self._database.connection() as connection:
            create_run(
                connection,
                event_id=event.id,
                run_id=run_id,
                config_sha=self._config.config_sha,
                now=now,
            )

        try:
            collected = self._collect(event, wrapped, run_id)
        except Exception as exc:
            logger.exception("collection failed", extra={"event_id": event.id, "run_id": run_id})
            with self._database.connection() as connection:
                finish_run(
                    connection,
                    run_id,
                    status="failed",
                    summary=f"{type(exc).__name__}: {exc}",
                    now=self._clock(),
                )
            raise

        logger.info(
            "collected failure",
            extra={"run_id": run_id, "signature": collected.signature},
        )
        return collected

    def _collect(
        self,
        event: EventRecord,
        wrapped: WorkflowRunEvent,
        run_id: str,
    ) -> CollectedRun:
        repo = event.repo
        run_detail = self._client.get_workflow_run(repo, event.workflow_run_id)
        jobs = self._client.list_workflow_jobs(
            repo, event.workflow_run_id, attempt=event.run_attempt
        )
        failed_jobs = [job for job in jobs if job.conclusion in FAILED_CONCLUSIONS]
        if not failed_jobs:
            raise CollectorError(
                f"no failed jobs found for {repo} run {event.workflow_run_id} "
                f"attempt {event.run_attempt}"
            )

        selected_jobs = failed_jobs[:MAX_FAILED_JOBS]
        inputs: list[JobLogInput] = []
        failed_job_info: list[FailedJobInfo] = []
        for job in failed_jobs:
            steps = _failed_steps(job)
            failed_job_info.append(
                FailedJobInfo(
                    job_id=job.id,
                    name=job.name,
                    conclusion=job.conclusion,
                    failed_steps=[
                        FailedStepInfo(
                            number=step.number,
                            name=step.name,
                            conclusion=step.conclusion,
                        )
                        for step in steps
                    ],
                )
            )

        for job in selected_jobs:
            steps = _failed_steps(job)
            primary_step = steps[0].name if steps else None
            raw_log = self._client.download_job_logs(repo, job.id)
            inputs.append(
                JobLogInput(
                    job_id=job.id,
                    job_name=job.name,
                    failed_step=primary_step,
                    text=extract_log_text(raw_log),
                )
            )

        trim = trim_logs(inputs)
        omitted_jobs = len(failed_jobs) - len(selected_jobs)
        log_redaction = self._redactor.redact(trim.text)

        pr_number = _resolve_pr_number(run_detail, wrapped)
        diff = ""
        base_sha = run_detail.head_sha
        if pr_number is not None:
            try:
                pull_request = self._client.get_pull_request(repo, pr_number)
            except GitHubNotFoundError:
                logger.warning("pull request %s not found; continuing without diff", pr_number)
            else:
                base_sha = pull_request.base.sha
                diff = self._client.get_pull_request_diff(repo, pr_number)

        diff_redaction = self._redactor.redact(diff)
        counts: Counter[str] = Counter(log_redaction.counts)
        counts.update(diff_redaction.counts)

        primary_job = selected_jobs[0]
        primary_steps = _failed_steps(primary_job)
        signature = build_signature(
            job_name=primary_job.name,
            step_name=primary_steps[0].name if primary_steps else None,
            text=log_redaction.text,
        )
        memory_notes = self._matching_memory(signature.value, repo)

        metadata = SnapshotMetadata(
            run_id=run_id,
            event_id=event.id,
            delivery_id=event.delivery_id,
            repo=repo,
            workflow_run_id=event.workflow_run_id,
            run_attempt=event.run_attempt,
            workflow_name=run_detail.name,
            source=wrapped.source,
            head_sha=run_detail.head_sha,
            base_sha=base_sha,
            pr_number=pr_number,
            signature=signature.value,
            config_sha=self._config.config_sha,
            failed_jobs=failed_job_info,
            trim=trim.jobs,
            dropped_jobs=trim.dropped_jobs + omitted_jobs,
            total_bytes=trim.total_bytes,
            redaction_counts=dict(counts),
            memory_matches=len(memory_notes),
            diff_available=bool(diff.strip()),
            collected_at=self._clock(),
        )

        snapshot = write_snapshot(
            self._snapshot_root,
            run_id,
            metadata=metadata,
            logs=log_redaction.text,
            diff=diff_redaction.text,
            memory_notes=memory_notes,
            signature=signature.value,
        )

        with self._database.connection() as connection, transaction(connection):
            upsert_signature(
                connection,
                signature=signature.value,
                exemplar=signature.exemplar,
                job_name=primary_job.name,
                step_name=primary_steps[0].name if primary_steps else "",
                repo=repo,
                now=self._clock(),
            )
            run = finish_run(
                connection,
                run_id,
                status="collected",
                signature=signature.value,
                snapshot_path=str(snapshot.directory),
                summary=(
                    f"{len(selected_jobs)} failed job(s); "
                    f"{trim.total_bytes} bytes of log; signature {signature.value}"
                ),
                now=self._clock(),
            )
        if run is None:  # pragma: no cover - run was created above
            raise CollectorError(f"run {run_id} disappeared")

        return CollectedRun(
            run=run,
            snapshot=snapshot,
            metadata=metadata,
            trim=trim,
            signature=signature.value,
        )

    def _load_payload(self, event: EventRecord) -> dict[str, object]:
        if event.payload:
            return event.payload
        path = Path(event.payload_path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CollectorError(f"could not read event payload {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise CollectorError(f"event payload is not an object: {path}")
        return raw

    def _matching_memory(self, signature: str, repo: str) -> list[MemoryNote]:
        return [
            note
            for note in self._config.memory_notes
            if note.signature == signature and (note.repo is None or note.repo == repo)
        ]


def _failed_steps(job: GitHubJob) -> list[GitHubStep]:
    failed = [step for step in job.steps if step.conclusion in FAILED_CONCLUSIONS]
    if failed:
        return failed
    completed = [step for step in job.steps if step.conclusion not in {None, "skipped"}]
    return completed[-1:] if completed else []


def _resolve_pr_number(run: GitHubWorkflowRun, event: WorkflowRunEvent) -> int | None:
    if run.pull_requests:
        return run.pull_requests[0].number
    if event.workflow_run.pull_requests:
        return event.workflow_run.pull_requests[0].number
    return None
