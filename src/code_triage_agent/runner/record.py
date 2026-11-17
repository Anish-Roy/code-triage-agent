"""Persist a complete run record, even when the loop ends badly.

The artifact is the CLI/golden representation; the database rows are what the
worker and later phases read. Both carry the same decision, steps, cost, and
config/skill versions.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from code_triage_agent.db import (
    Database,
    create_run,
    finish_run,
    get_event,
    get_run,
    record_step,
    to_iso,
    utcnow,
)
from code_triage_agent.models import (
    RunnerStatus,
    RunnerStep,
    SnapshotMetadata,
    TokenUsage,
    TriageDecision,
)
from code_triage_agent.runner.actions import CommentResult
from code_triage_agent.runner.loop import LoopResult, safe_unknown_decision


@dataclass
class RunArtifact:
    run_id: str
    event_id: int
    trigger_id: str | None
    skill_id: str | None
    skill_version: str | None
    signature: str
    snapshot_path: str
    config_sha: str
    status: RunnerStatus
    decision: TriageDecision
    actions: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    usage: TokenUsage = field(default_factory=TokenUsage)
    steps: list[RunnerStep] = field(default_factory=list)
    error: str | None = None
    comment: dict[str, object] | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "event_id": self.event_id,
            "trigger_id": self.trigger_id,
            "skill_id": self.skill_id,
            "skill_version": self.skill_version,
            "signature": self.signature,
            "snapshot_path": self.snapshot_path,
            "config_sha": self.config_sha,
            "status": self.status,
            "decision": self.decision.model_dump(mode="json"),
            "actions": list(self.actions),
            "cost_usd": round(self.cost_usd, 6),
            "usage": self.usage.model_dump(mode="json"),
            "steps": [step.model_dump(mode="json") for step in self.steps],
            "error": self.error,
            "comment": self.comment,
            "started_at": to_iso(self.started_at) if self.started_at else None,
            "finished_at": to_iso(self.finished_at) if self.finished_at else None,
        }


class RunRecorder:
    """Writes run and step rows when a matching event exists in the database."""

    def __init__(
        self,
        *,
        database: Database | None,
        metadata: SnapshotMetadata,
        trigger_id: str | None,
        skill_id: str | None,
        skill_version: str | None,
        snapshot_path: Path,
        config_sha: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._metadata = metadata
        self._trigger_id = trigger_id
        self._skill_id = skill_id
        self._skill_version = skill_version
        self._snapshot_path = snapshot_path
        self._config_sha = config_sha or metadata.config_sha
        self._clock = clock or utcnow
        self._persistent = False
        self.persistence_error: str | None = None
        self._started_at = self._clock()
        self._steps: list[RunnerStep] = []

    @property
    def persistent(self) -> bool:
        return self._persistent

    def start(self) -> None:
        if self._database is None:
            return
        try:
            with self._database.connection() as connection:
                existing = get_run(connection, self._metadata.run_id)
                if existing is not None:
                    self._persistent = True
                    self._started_at = existing.started_at
                    return
                if get_event(connection, self._metadata.event_id) is None:
                    return
                create_run(
                    connection,
                    event_id=self._metadata.event_id,
                    run_id=self._metadata.run_id,
                    trigger_id=self._trigger_id,
                    skill_id=self._skill_id,
                    skill_version=self._skill_version,
                    signature=self._metadata.signature,
                    snapshot_path=str(self._snapshot_path),
                    config_sha=self._config_sha,
                    now=self._started_at,
                )
                self._persistent = True
        except sqlite3.Error as exc:  # a broken DB must not hide the decision
            self.persistence_error = f"{type(exc).__name__}: {exc}"

    def add_steps(self, steps: Sequence[RunnerStep]) -> None:
        for step in steps:
            self._steps.append(step)
            if not self._persistent or self._database is None:
                continue
            try:
                with self._database.connection() as connection:
                    record_step(
                        connection,
                        run_id=self._metadata.run_id,
                        seq=step.seq,
                        tool=step.tool,
                        args=step.args,
                        result=step.result,
                        is_error=step.is_error,
                        latency_ms=step.latency_ms,
                        cost_usd=step.cost_usd,
                    )
            except sqlite3.Error as exc:
                self.persistence_error = f"{type(exc).__name__}: {exc}"

    def finish(
        self,
        result: LoopResult,
        *,
        actions: Sequence[str] = (),
        comment: CommentResult | None = None,
    ) -> RunArtifact:
        finished_at = self._clock()
        if self._persistent and self._database is not None:
            try:
                with self._database.connection() as connection:
                    finish_run(
                        connection,
                        self._metadata.run_id,
                        status=result.status,
                        category=result.decision.category,
                        confidence=result.decision.confidence,
                        summary=result.decision.summary,
                        actions=list(actions),
                        cost_usd=result.cost_usd,
                        trigger_id=self._trigger_id,
                        skill_id=self._skill_id,
                        skill_version=self._skill_version,
                        config_sha=self._config_sha,
                        now=finished_at,
                    )
            except sqlite3.Error as exc:
                self.persistence_error = f"{type(exc).__name__}: {exc}"

        comment_dict: dict[str, object] | None = None
        if comment is not None:
            comment_dict = {
                "posted": comment.posted,
                "comment_id": comment.comment_id,
                "skipped_reason": comment.skipped_reason,
                "body": comment.body,
            }

        return RunArtifact(
            run_id=self._metadata.run_id,
            event_id=self._metadata.event_id,
            trigger_id=self._trigger_id,
            skill_id=self._skill_id,
            skill_version=self._skill_version,
            signature=self._metadata.signature,
            snapshot_path=str(self._snapshot_path),
            config_sha=self._config_sha,
            status=result.status,
            decision=result.decision,
            actions=list(actions),
            cost_usd=result.cost_usd,
            usage=result.usage,
            steps=list(self._steps),
            error=result.error,
            comment=comment_dict,
            started_at=self._started_at,
            finished_at=finished_at,
        )

    def finish_failure(self, error: str, *, actions: Sequence[str] = ()) -> RunArtifact:
        result = LoopResult(
            decision=safe_unknown_decision(error),
            status="failed",
            steps=[],
            error=error,
        )
        return self.finish(result, actions=actions)
