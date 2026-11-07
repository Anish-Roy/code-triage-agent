"""Shared Pydantic models used across the server, worker, and collector."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EventStatus = Literal["pending", "processing", "done", "failed", "dead"]
RunStatus = Literal["running", "collected", "succeeded", "failed", "budget_exceeded"]
Conclusion = Literal["failure", "timed_out", "cancelled", "success", "skipped", "neutral"]

FAILED_CONCLUSIONS = frozenset({"failure", "timed_out"})
ACTIONABLE_ACTIONS = frozenset({"completed"})


class RepoRef(BaseModel):
    """The ``repository`` object embedded in a webhook payload."""

    model_config = ConfigDict(extra="ignore")

    full_name: str = Field(min_length=1)


class PullRequestRef(BaseModel):
    """A pull request reference embedded in a ``workflow_run`` payload."""

    model_config = ConfigDict(extra="ignore")

    number: int = Field(ge=1)


class WorkflowRunPayload(BaseModel):
    """The subset of ``workflow_run`` from a webhook payload we depend on."""

    model_config = ConfigDict(extra="ignore")

    id: int
    run_attempt: int = Field(default=1, ge=1)
    name: str | None = None
    event: str = ""
    head_branch: str | None = None
    head_sha: str | None = None
    base_sha: str | None = None
    conclusion: str | None = None
    status: str | None = None
    html_url: str | None = None
    pull_requests: list[PullRequestRef] = Field(default_factory=list)


class WorkflowRunEvent(BaseModel):
    """A parsed ``workflow_run`` webhook delivery."""

    model_config = ConfigDict(extra="ignore")

    action: str
    repository: RepoRef
    workflow_run: WorkflowRunPayload

    @property
    def source(self) -> str:
        """Map the workflow's triggering event onto a trigger ``when.source``."""
        event = self.workflow_run.event
        if event in {"pull_request", "pull_request_target"}:
            return "pull_request"
        if event == "push":
            return "push"
        if event == "schedule":
            return "schedule"
        return "any"

    @property
    def is_actionable(self) -> bool:
        return (
            self.action in ACTIONABLE_ACTIONS and self.workflow_run.conclusion in FAILED_CONCLUSIONS
        )

    @property
    def dedupe_key(self) -> tuple[str, int, int]:
        return (
            self.repository.full_name,
            self.workflow_run.id,
            self.workflow_run.run_attempt,
        )


class EventRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    delivery_id: str
    repo: str
    workflow_run_id: int
    run_attempt: int
    conclusion: str
    event_name: str = "workflow_run"
    payload_path: str
    status: EventStatus
    attempts: int = 0
    error: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    claimed_at: datetime | None = None
    next_attempt_at: datetime | None = None
    received_at: datetime
    updated_at: datetime


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    event_id: int
    trigger_id: str | None = None
    skill_id: str | None = None
    skill_version: str | None = None
    signature: str | None = None
    snapshot_path: str | None = None
    category: str | None = None
    confidence: float | None = None
    summary: str | None = None
    actions: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    status: RunStatus
    config_sha: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


class StepRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    run_id: str
    seq: int
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    is_error: bool = False
    latency_ms: int | None = None
    cost_usd: float = 0.0
    created_at: datetime


class SignatureRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    signature: str
    exemplar: str
    job_name: str
    step_name: str
    repo: str | None = None
    first_seen: datetime
    last_seen: datetime
    count: int = 1


class FailedStepInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int | None = None
    name: str
    conclusion: str | None = None


class FailedJobInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: int
    name: str
    conclusion: str | None = None
    failed_steps: list[FailedStepInfo] = Field(default_factory=list)


class LineRange(BaseModel):
    """A 1-based, inclusive range of lines kept from a job log."""

    model_config = ConfigDict(extra="forbid")

    start: int = Field(ge=1)
    end: int = Field(ge=1)


class JobLogTrim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: int
    job_name: str
    step_name: str | None = None
    line_ranges: list[LineRange] = Field(default_factory=list)
    kept_lines: int = 0
    total_lines: int = 0
    truncated: bool = False


class TrimResult(BaseModel):
    """The trimmed, ready-to-store log text plus provenance for each job."""

    model_config = ConfigDict(extra="forbid")

    text: str
    jobs: list[JobLogTrim] = Field(default_factory=list)
    dropped_jobs: int = 0
    total_bytes: int = 0


class SignatureResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str
    exemplar: str


class SnapshotMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    event_id: int
    delivery_id: str
    repo: str
    workflow_run_id: int
    run_attempt: int
    workflow_name: str | None = None
    source: str
    head_sha: str | None = None
    base_sha: str | None = None
    pr_number: int | None = None
    signature: str
    config_sha: str
    failed_jobs: list[FailedJobInfo] = Field(default_factory=list)
    trim: list[JobLogTrim] = Field(default_factory=list)
    dropped_jobs: int = 0
    total_bytes: int = 0
    redaction_counts: dict[str, int] = Field(default_factory=dict)
    memory_matches: int = 0
    diff_available: bool = False
    collected_at: datetime
