"""Shared Pydantic models used across the server, worker, and collector."""

from __future__ import annotations

from dataclasses import dataclass, field
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
    predicted_action: str | None = None
    decision: dict[str, Any] | None = None
    outcome: str | None = None
    outcome_checked_at: datetime | None = None
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
    conclusion: str = "failure"
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


# ---------------------------------------------------------------------------
# Runner contracts (Phase 2)
# ---------------------------------------------------------------------------

Category = Literal["flaky_test", "infra_issue", "dependency_or_config", "real_bug", "unknown"]
EvidenceSource = Literal["log", "diff", "memory", "code"]
RecommendedActionType = Literal["comment", "rerun", "fix_pr", "issue", "none"]
RunnerStatus = Literal["succeeded", "failed", "budget_exceeded"]


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: EvidenceSource
    quote: str = Field(min_length=1)
    why: str = Field(min_length=1)


class RecommendedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: RecommendedActionType = "none"
    params: dict[str, Any] = Field(default_factory=dict)


class TriageDecision(BaseModel):
    """The exact object the model must pass to ``submit_triage``."""

    model_config = ConfigDict(extra="forbid")

    category: Category
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = Field(min_length=1)
    evidence: list[Evidence] = Field(default_factory=list)
    memory_note: str | None = None
    recommended_action: RecommendedAction = Field(default_factory=RecommendedAction)
    needs_human: bool = False


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class LLMMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class LLMResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    stop_reason: str | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    model: str = ""
    cost_usd: float = Field(default=0.0, ge=0.0)


class ToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    content: Any = None
    error: str | None = None


class RunnerStep(BaseModel):
    """One audited unit of work: a model turn, a tool call, or an action."""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)
    kind: Literal["llm", "tool", "action"]
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    is_error: bool = False
    latency_ms: int = 0
    cost_usd: float = 0.0


# ---------------------------------------------------------------------------
# Phase 3 contracts: outcomes, eval cases, and replay reports
# ---------------------------------------------------------------------------

OutcomeLabel = Literal["correct", "incorrect", "unknown"]
EvalExpectedAction = Literal["comment", "rerun", "fix_pr", "issue", "none"]
CaseLabel = Literal["correct", "incorrect", "error"]


@dataclass(frozen=True)
class OutcomeSignals:
    """Raw signals gathered from GitHub plus deterministic local history.

    This deliberately has no LLM dependency. Every field is evidence that the
    label table in :mod:`code_triage_agent.background.score` can cite.
    """

    checked_at: datetime
    rerun_conclusion: str | None = None
    rerun_run_attempt: int | None = None
    rerun_signature_matches: bool | None = None
    fix_pr_state: str | None = None
    fix_pr_merged: bool | None = None
    fix_pr_reverted: bool = False
    fix_pr_number: int | None = None
    signature_recurred: bool = False
    recurrence_run_ids: tuple[str, ...] = ()
    human_feedback: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at.isoformat(),
            "rerun_conclusion": self.rerun_conclusion,
            "rerun_run_attempt": self.rerun_run_attempt,
            "rerun_signature_matches": self.rerun_signature_matches,
            "fix_pr_state": self.fix_pr_state,
            "fix_pr_merged": self.fix_pr_merged,
            "fix_pr_reverted": self.fix_pr_reverted,
            "fix_pr_number": self.fix_pr_number,
            "signature_recurred": self.signature_recurred,
            "recurrence_run_ids": list(self.recurrence_run_ids),
            "human_feedback": self.human_feedback,
            "evidence": dict(self.evidence),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> OutcomeSignals:
        checked_at = value.get("checked_at")
        if not isinstance(checked_at, str):
            raise ValueError("signals.checked_at must be an ISO-8601 string")
        recurrence = value.get("recurrence_run_ids") or []
        if not isinstance(recurrence, list):
            raise ValueError("signals.recurrence_run_ids must be a list")
        evidence = value.get("evidence") or {}
        if not isinstance(evidence, dict):
            raise ValueError("signals.evidence must be an object")
        return cls(
            checked_at=datetime.fromisoformat(checked_at),
            rerun_conclusion=value.get("rerun_conclusion"),
            rerun_run_attempt=value.get("rerun_run_attempt"),
            rerun_signature_matches=value.get("rerun_signature_matches"),
            fix_pr_state=value.get("fix_pr_state"),
            fix_pr_merged=value.get("fix_pr_merged"),
            fix_pr_reverted=bool(value.get("fix_pr_reverted", False)),
            fix_pr_number=value.get("fix_pr_number"),
            signature_recurred=bool(value.get("signature_recurred", False)),
            recurrence_run_ids=tuple(str(item) for item in recurrence),
            human_feedback=value.get("human_feedback"),
            evidence=evidence,
        )


class OutcomeRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    run_id: str
    label: OutcomeLabel
    signals: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    collected_at: datetime
    updated_at: datetime | None = None
    terminal: bool = False


class OutcomeCheckRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    run_id: str
    attempts: int = 0
    first_checked_at: datetime | None = None
    last_checked_at: datetime | None = None
    next_check_at: datetime | None = None
    terminal: bool = False
    error: str | None = None


class EvalRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    case_id: str
    set_name: str
    snapshot_path: str
    expected_category: str | None = None
    expected_action: str | None = None
    notes: str | None = None
    labeler: str | None = None
    reviewer: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class EvalCase(BaseModel):
    """The authoritative ``case.json`` record for one frozen eval case."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    snapshot_path: str = Field(min_length=1)
    expected_category: Category
    expected_action: EvalExpectedAction = "comment"
    labeler: str = Field(default="unlabeled", min_length=1)
    notes: str = ""
    expected_skill: str | None = None
    reviewer: str | None = None
    tags: list[str] = Field(default_factory=list)
    hard_boundary: bool = False
    reviewed_at: datetime | None = None


class CaseReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    skill_id: str | None = None
    expected_category: str
    predicted_category: str
    expected_action: str
    predicted_action: str
    label: CaseLabel
    action_correct: bool = False
    cost_usd: float = 0.0
    runtime_ms: int = 0
    status: str | None = None
    failure_reason: str | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class ReplayMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_cases: int = 0
    correct_cases: int = 0
    accuracy: float = 0.0
    per_skill_accuracy: dict[str, float] = Field(default_factory=dict)
    per_category_accuracy: dict[str, float] = Field(default_factory=dict)
    confusion_matrix: dict[str, dict[str, int]] = Field(default_factory=dict)
    false_flaky_count: int = 0
    non_flaky_cases: int = 0
    false_flaky_rate: float = 0.0
    actions_attempted: int = 0
    actions_succeeded: int = 0
    action_precision: float = 0.0
    median_cost_usd: float = 0.0
    total_cost_usd: float = 0.0
    cost_efficiency: float = 1.0
    composite_score: float = 0.0
    failure_reasons: dict[str, int] = Field(default_factory=dict)


class GateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool = True
    failures: list[str] = Field(default_factory=list)
    checks: dict[str, bool] = Field(default_factory=dict)


class ReplayReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_at: datetime
    config_sha: str
    set_name: str
    model: str
    cases: list[CaseReport] = Field(default_factory=list)
    metrics: ReplayMetrics = Field(default_factory=ReplayMetrics)
    skill_versions: dict[str, str] = Field(default_factory=dict)
    budget_usd: float = 0.0
    runtime_ms: int = 0
    gates: GateResult | None = None
    baseline: str | None = None
