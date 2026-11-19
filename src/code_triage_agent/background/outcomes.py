"""Collect outcome signals for finished runs without calling an LLM.

The collector is deliberately boring: read a finished run, ask GitHub (or the
local history) a few narrow questions, persist the raw signals, and let
:mod:`code_triage_agent.background.score` decide the label.

Rules enforced here:

* only finished, unresolved runs are polled;
* attempts are capped and every retry gets a ``next_check_at``;
* the seven-day recurrence window is a hard stop, not a hint;
* a run that recommended no action is not polled forever;
* GitHub errors are recorded per run and never abort the batch.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from code_triage_agent.background.score import LabelDecision, decide_label
from code_triage_agent.db import (
    Database,
    get_event,
    get_outcome,
    get_outcome_check,
    list_due_outcome_runs,
    list_expired_outcome_runs,
    list_runs_for_signature,
    to_iso,
    update_run_outcome,
    upsert_outcome,
    upsert_outcome_check,
    utcnow,
)
from code_triage_agent.github.models import (
    GitHubIssueComment,
    GitHubPullRequest,
    GitHubWorkflowRun,
)
from code_triage_agent.models import EventRecord, OutcomeSignals, RunRecord
from code_triage_agent.runner.actions import run_marker

MAX_ATTEMPTS = 8
RECURRENCE_WINDOW = timedelta(days=7)
BACKOFF_STEPS = (
    timedelta(hours=1),
    timedelta(hours=6),
    timedelta(hours=24),
)
_FEEDBACK = re.compile(r"triage[-\s]label:\s*(correct|incorrect|unknown)", re.IGNORECASE)


class OutcomeGitHubClient(Protocol):
    def get_workflow_run(self, repo: str, run_id: int) -> GitHubWorkflowRun: ...

    def get_pull_request(self, repo: str, number: int) -> GitHubPullRequest: ...

    def list_issue_comments(self, repo: str, number: int) -> list[GitHubIssueComment]: ...


@dataclass
class OutcomeCollectionSummary:
    checked: int = 0
    labeled: int = 0
    unknown: int = 0
    terminal: int = 0
    errors: int = 0
    skipped: int = 0
    labels: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "checked": self.checked,
            "labeled": self.labeled,
            "unknown": self.unknown,
            "terminal": self.terminal,
            "errors": self.errors,
            "skipped": self.skipped,
            "labels": dict(self.labels),
        }


def collect_outcomes(
    database: Database,
    github: OutcomeGitHubClient,
    *,
    clock: Callable[[], datetime] | None = None,
    limit: int = 50,
    repo: str | None = None,
    window_days: int = 7,
) -> OutcomeCollectionSummary:
    """Check due runs and persist labels and bookkeeping.

    The function is idempotent: runs already labeled or marked terminal are not
    returned by ``list_due_outcome_runs``.
    """
    now = (clock or utcnow)()
    summary = OutcomeCollectionSummary()

    with database.connection() as connection:
        # Close the book on runs whose window has passed. They are never polled,
        # but they still need a terminal check row so they do not show up as
        # unresolved forever.
        expired = list_expired_outcome_runs(connection, now=now, window_days=window_days)
        for run in expired:
            event = get_event(connection, run.event_id)
            if event is None or (repo is not None and event.repo != repo):
                continue
            check = get_outcome_check(connection, run.id)
            upsert_outcome_check(
                connection,
                run_id=run.id,
                attempts=check.attempts if check else 0,
                first_checked_at=(check.first_checked_at if check else None) or now,
                last_checked_at=now,
                next_check_at=None,
                terminal=True,
                error=check.error if check else None,
            )
            summary.terminal += 1

        runs = list_due_outcome_runs(connection, now=now, limit=limit, window_days=window_days)
        for run in runs:
            event = get_event(connection, run.event_id)
            if event is None:
                summary.skipped += 1
                continue
            if repo is not None and event.repo != repo:
                summary.skipped += 1
                continue
            try:
                result = _check_run(
                    connection,
                    run=run,
                    event=event,
                    github=github,
                    now=now,
                    window_days=window_days,
                )
            except Exception as exc:  # one bad run must not stop the batch
                _record_error(connection, run, error=f"{type(exc).__name__}: {exc}", now=now)
                summary.errors += 1
                continue

            summary.checked += 1
            if result.label == "unknown":
                summary.unknown += 1
            else:
                summary.labeled += 1
                summary.labels[result.label] = summary.labels.get(result.label, 0) + 1
            if result.terminal:
                summary.terminal += 1

    return summary


@dataclass(frozen=True)
class _RunCheck:
    label: str
    terminal: bool


def _check_run(
    connection: sqlite3.Connection,
    *,
    run: RunRecord,
    event: EventRecord,
    github: OutcomeGitHubClient,
    now: datetime,
    window_days: int,
) -> _RunCheck:
    existing = get_outcome(connection, run.id)
    if existing is not None:
        return _RunCheck(label=existing.label, terminal=existing.terminal)

    check = get_outcome_check(connection, run.id)
    attempts = (check.attempts if check else 0) + 1
    first_checked = check.first_checked_at if check and check.first_checked_at else now
    finished_at = run.finished_at or run.started_at
    past_window = finished_at + timedelta(days=window_days) <= now

    if past_window or attempts > MAX_ATTEMPTS:
        upsert_outcome_check(
            connection,
            run_id=run.id,
            attempts=attempts,
            first_checked_at=first_checked,
            last_checked_at=now,
            next_check_at=None,
            terminal=True,
            error=check.error if check else None,
        )
        return _RunCheck(label="unknown", terminal=True)

    signals = _collect_signals(
        connection,
        run=run,
        event=event,
        github=github,
        now=now,
        window_days=window_days,
    )
    decision = decide_label(signals, predicted_category=run.category, now=now)

    no_action = (run.predicted_action in {None, "none"}) and not run.actions
    terminal = decision.label != "unknown" or no_action
    if decision.label != "unknown":
        upsert_outcome(
            connection,
            run_id=run.id,
            label=decision.label,
            signals=signals.to_dict(),
            evidence={
                "rule": decision.rule,
                "reason": decision.reason,
                **decision.evidence,
            },
            terminal=terminal,
            now=now,
        )
        update_run_outcome(connection, run.id, outcome=decision.label, checked_at=now)

    upsert_outcome_check(
        connection,
        run_id=run.id,
        attempts=attempts,
        first_checked_at=first_checked,
        last_checked_at=now,
        next_check_at=_next_check(now, attempts, terminal=terminal),
        terminal=terminal,
        error=None,
    )
    return _RunCheck(label=decision.label, terminal=terminal)


def _collect_signals(
    connection: sqlite3.Connection,
    *,
    run: RunRecord,
    event: EventRecord,
    github: OutcomeGitHubClient,
    now: datetime,
    window_days: int,
) -> OutcomeSignals:
    evidence: dict[str, object] = {
        "repo": event.repo,
        "workflow_run_id": event.workflow_run_id,
        "original_run_attempt": event.run_attempt,
        "predicted_category": run.category,
    }

    rerun_conclusion, rerun_attempt = _rerun_signal(
        github, repo=event.repo, run_id=event.workflow_run_id, original_attempt=event.run_attempt
    )
    if rerun_conclusion is not None:
        evidence["rerun"] = {
            "conclusion": rerun_conclusion,
            "run_attempt": rerun_attempt,
        }

    pr_number = _snapshot_pr_number(run.snapshot_path)
    pr_state: str | None = None
    pr_merged: bool | None = None
    pr_reverted = False
    if pr_number is not None:
        try:
            pull = github.get_pull_request(event.repo, pr_number)
        except Exception as exc:
            evidence["fix_pr_error"] = f"{type(exc).__name__}: {exc}"
        else:
            pr_state = pull.state
            pr_merged = pull.merged
            pr_reverted = getattr(pull, "reverted", False)
            evidence["fix_pr"] = {
                "number": pull.number,
                "state": pull.state,
                "merged": pull.merged,
            }

    recurrence = _recurrence_signal(
        connection,
        run=run,
        now=now,
        window_days=window_days,
    )
    if recurrence:
        evidence["recurrence"] = {
            "run_ids": [item.id for item in recurrence],
            "first_at": to_iso(recurrence[0].started_at),
        }
        evidence["recurrence_at"] = to_iso(recurrence[0].started_at)

    human = _human_feedback(github, repo=event.repo, pr_number=pr_number, run_id=run.id)
    if human is not None:
        evidence["human_feedback"] = human

    return OutcomeSignals(
        checked_at=now,
        rerun_conclusion=rerun_conclusion,
        rerun_run_attempt=rerun_attempt,
        # A rerun of the same workflow run is treated as the same failure unless
        # a future collector records a distinct signature for the new attempt.
        rerun_signature_matches=None,
        fix_pr_state=pr_state,
        fix_pr_merged=pr_merged,
        fix_pr_reverted=pr_reverted,
        fix_pr_number=pr_number,
        signature_recurred=bool(recurrence),
        recurrence_run_ids=tuple(item.id for item in recurrence),
        human_feedback=human,
        evidence=evidence,
    )


def _rerun_signal(
    github: OutcomeGitHubClient,
    *,
    repo: str,
    run_id: int,
    original_attempt: int,
) -> tuple[str | None, int | None]:
    latest = github.get_workflow_run(repo, run_id)
    if latest.run_attempt <= original_attempt:
        return None, None
    if latest.status != "completed":
        return None, latest.run_attempt
    return latest.conclusion, latest.run_attempt


def _recurrence_signal(
    connection: sqlite3.Connection,
    *,
    run: RunRecord,
    now: datetime,
    window_days: int,
) -> list[RunRecord]:
    if not run.signature:
        return []
    finished_at = run.finished_at or run.started_at
    before = min(finished_at + timedelta(days=window_days), now)
    if finished_at >= now:
        return []
    return list_runs_for_signature(
        connection,
        signature=run.signature,
        after=finished_at,
        before=before,
        exclude_run_id=run.id,
    )


def _human_feedback(
    github: OutcomeGitHubClient,
    *,
    repo: str,
    pr_number: int | None,
    run_id: str,
) -> str | None:
    if pr_number is None:
        return None
    try:
        comments = github.list_issue_comments(repo, pr_number)
    except Exception:
        return None
    marker = run_marker(run_id)
    seen_marker = False
    for comment in comments:
        body = comment.body or ""
        if marker in body:
            seen_marker = True
            continue
        # Only trust replies after our marker when it exists. This keeps a
        # human's unrelated "correct" comment from labeling the run.
        if seen_marker:
            match = _FEEDBACK.search(body)
            if match:
                return f"triage-label: {match.group(1).lower()}"
    return None


def _snapshot_pr_number(snapshot_path: str | None) -> int | None:
    if not snapshot_path:
        return None
    metadata = Path(snapshot_path) / "metadata.json"
    if not metadata.is_file():
        return None
    try:
        raw = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = raw.get("pr_number") if isinstance(raw, dict) else None
    return value if isinstance(value, int) else None


def _record_error(
    connection: sqlite3.Connection,
    run: RunRecord,
    *,
    error: str,
    now: datetime,
) -> None:
    check = get_outcome_check(connection, run.id)
    attempts = (check.attempts if check else 0) + 1
    terminal = attempts > MAX_ATTEMPTS
    upsert_outcome_check(
        connection,
        run_id=run.id,
        attempts=attempts,
        first_checked_at=check.first_checked_at if check else now,
        last_checked_at=now,
        next_check_at=_next_check(now, attempts, terminal=terminal),
        terminal=terminal,
        error=error,
    )


def _next_check(now: datetime, attempts: int, *, terminal: bool) -> datetime | None:
    if terminal:
        return None
    index = min(max(attempts - 1, 0), len(BACKOFF_STEPS) - 1)
    return now + BACKOFF_STEPS[index]


def outcome_decision(run: RunRecord, signals: OutcomeSignals) -> LabelDecision:
    """Small public helper for callers that already have signals."""
    return decide_label(signals, predicted_category=run.category)


__all__ = [
    "BACKOFF_STEPS",
    "MAX_ATTEMPTS",
    "OutcomeCollectionSummary",
    "OutcomeGitHubClient",
    "collect_outcomes",
    "outcome_decision",
]
