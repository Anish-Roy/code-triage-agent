from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta

from code_triage_agent.background import outcomes as outcomes_module
from code_triage_agent.background.outcomes import collect_outcomes
from code_triage_agent.db import (
    Database,
    create_run,
    finish_run,
    get_outcome,
    get_outcome_check,
    get_run,
    upsert_outcome_check,
)
from code_triage_agent.github.models import (
    GitHubBranchRef,
    GitHubIssueComment,
    GitHubPullRequest,
    GitHubWorkflowRun,
)
from code_triage_agent.runner.actions import run_marker

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


class FakeGitHub:
    def __init__(self) -> None:
        self.workflow_runs: dict[int, GitHubWorkflowRun] = {}
        self.pull_requests: dict[int, GitHubPullRequest] = {}
        self.comments: dict[int, list[GitHubIssueComment]] = {}
        self.calls = 0

    def get_workflow_run(self, repo: str, run_id: int) -> GitHubWorkflowRun:
        self.calls += 1
        return self.workflow_runs[run_id]

    def get_pull_request(self, repo: str, number: int) -> GitHubPullRequest:
        self.calls += 1
        return self.pull_requests[number]

    def list_issue_comments(self, repo: str, number: int) -> list[GitHubIssueComment]:
        self.calls += 1
        return list(self.comments.get(number, []))


def _completed_run(*, attempt: int = 1, conclusion: str = "failure") -> GitHubWorkflowRun:
    return GitHubWorkflowRun(
        id=1,
        run_attempt=attempt,
        status="completed",
        conclusion=conclusion,
    )


def _pull(number: int = 7, *, state: str = "closed", merged: bool = True) -> GitHubPullRequest:
    return GitHubPullRequest(
        number=number,
        state=state,
        merged=merged,
        head=GitHubBranchRef(ref="feature", sha="abc"),
        base=GitHubBranchRef(ref="main", sha="def"),
    )


def _make_run(
    database: Database,
    make_event: object,
    make_snapshot: object,
    *,
    run_id: str = "r_outcome",
    workflow_run_id: int = 9001,
    category: str = "flaky_test",
    predicted_action: str = "comment",
    actions: list[str] | None = None,
    pr_number: int | None = 7,
    finished_at: datetime = NOW,
) -> tuple[str, int]:
    event = make_event(  # type: ignore[operator]
        delivery_id=f"delivery-{run_id}",
        workflow_run_id=workflow_run_id,
    )
    snapshot = make_snapshot(  # type: ignore[operator]
        run_id=run_id,
        pr_number=pr_number,
        signature="sig_v1:test",
        metadata={"event_id": event.id, "workflow_run_id": workflow_run_id},
    )
    with database.connection() as connection:
        create_run(
            connection,
            event_id=event.id,
            run_id=run_id,
            skill_id="triage-pr-failure",
            signature="sig_v1:test",
            snapshot_path=str(snapshot.directory),
            now=finished_at - timedelta(minutes=5),
        )
        finish_run(
            connection,
            run_id,
            status="succeeded",
            category=category,
            predicted_action=predicted_action,
            actions=actions if actions is not None else ["comment_on_pr"],
            snapshot_path=str(snapshot.directory),
            now=finished_at,
        )
    return run_id, workflow_run_id


def _outcome(database: Database, run_id: str) -> tuple[str | None, str | None]:
    with database.connection() as connection:
        outcome = get_outcome(connection, run_id)
        run = get_run(connection, run_id)
    return (
        outcome.label if outcome else None,
        run.outcome if run else None,
    )


def test_flaky_rerun_pass_is_correct(
    database: Database, make_event: object, make_snapshot: object
) -> None:
    run_id, github_run_id = _make_run(database, make_event, make_snapshot)
    github = FakeGitHub()
    github.workflow_runs[github_run_id] = _completed_run(attempt=2, conclusion="success")

    summary = collect_outcomes(database, github, clock=lambda: NOW + timedelta(hours=1))

    assert summary.checked == 1
    assert summary.labeled == 1
    assert _outcome(database, run_id) == ("correct", "correct")
    with database.connection() as connection:
        outcome = get_outcome(connection, run_id)
        check = get_outcome_check(connection, run_id)
    assert outcome is not None
    assert outcome.evidence["rule"] == "flaky_rerun_passed"
    assert outcome.signals["rerun_conclusion"] == "success"
    assert check is not None
    assert check.terminal is True


def test_flaky_rerun_failure_is_incorrect(
    database: Database, make_event: object, make_snapshot: object
) -> None:
    run_id, github_run_id = _make_run(database, make_event, make_snapshot)
    github = FakeGitHub()
    github.workflow_runs[github_run_id] = _completed_run(attempt=2, conclusion="failure")

    collect_outcomes(database, github, clock=lambda: NOW + timedelta(hours=1))

    assert _outcome(database, run_id) == ("incorrect", "incorrect")


def test_merged_fix_pr_is_correct(
    database: Database, make_event: object, make_snapshot: object
) -> None:
    run_id, github_run_id = _make_run(database, make_event, make_snapshot, category="real_bug")
    github = FakeGitHub()
    github.workflow_runs[github_run_id] = _completed_run()
    github.pull_requests[7] = _pull()

    collect_outcomes(database, github, clock=lambda: NOW + timedelta(hours=1))

    assert _outcome(database, run_id) == ("correct", "correct")


def test_closed_fix_pr_is_incorrect(
    database: Database, make_event: object, make_snapshot: object
) -> None:
    run_id, github_run_id = _make_run(database, make_event, make_snapshot, category="real_bug")
    github = FakeGitHub()
    github.workflow_runs[github_run_id] = _completed_run()
    github.pull_requests[7] = _pull(state="closed", merged=False)

    collect_outcomes(database, github, clock=lambda: NOW + timedelta(hours=1))

    assert _outcome(database, run_id) == ("incorrect", "incorrect")


def test_human_feedback_overrides_machine_signals(
    database: Database, make_event: object, make_snapshot: object
) -> None:
    run_id, github_run_id = _make_run(database, make_event, make_snapshot)
    github = FakeGitHub()
    github.workflow_runs[github_run_id] = _completed_run(attempt=2, conclusion="failure")
    github.comments[7] = [
        GitHubIssueComment(id=1, body="noise"),
        GitHubIssueComment(id=2, body=f"agent\n{run_marker(run_id)}"),
        GitHubIssueComment(id=3, body="triage-label: correct"),
    ]

    collect_outcomes(database, github, clock=lambda: NOW + timedelta(hours=1))

    with database.connection() as connection:
        outcome = get_outcome(connection, run_id)
    assert outcome is not None
    assert outcome.label == "correct"
    assert outcome.evidence["rule"] == "human_feedback"


def test_unusable_signal_stays_open_with_backoff(
    database: Database, make_event: object, make_snapshot: object
) -> None:
    run_id, github_run_id = _make_run(
        database, make_event, make_snapshot, category="real_bug", pr_number=None
    )
    github = FakeGitHub()
    github.workflow_runs[github_run_id] = _completed_run()

    first = collect_outcomes(database, github, clock=lambda: NOW + timedelta(hours=1))

    assert first.unknown == 1
    assert _outcome(database, run_id) == (None, None)
    with database.connection() as connection:
        check = get_outcome_check(connection, run_id)
    assert check is not None
    assert check.attempts == 1
    assert check.terminal is False
    assert check.next_check_at == NOW + timedelta(hours=2)

    second = collect_outcomes(database, github, clock=lambda: NOW + timedelta(minutes=90))
    assert second.checked == 0

    third = collect_outcomes(database, github, clock=lambda: NOW + timedelta(hours=2))
    assert third.checked == 1


def test_no_recommended_action_stops_polling(
    database: Database, make_event: object, make_snapshot: object
) -> None:
    run_id, github_run_id = _make_run(
        database,
        make_event,
        make_snapshot,
        category="unknown",
        predicted_action="none",
        actions=[],
        pr_number=None,
    )
    github = FakeGitHub()
    github.workflow_runs[github_run_id] = _completed_run()

    summary = collect_outcomes(database, github, clock=lambda: NOW + timedelta(hours=1))

    assert summary.terminal == 1
    with database.connection() as connection:
        check = get_outcome_check(connection, run_id)
    assert check is not None
    assert check.terminal is True
    assert check.next_check_at is None


def test_runs_older_than_the_window_are_terminal(
    database: Database, make_event: object, make_snapshot: object
) -> None:
    run_id, _ = _make_run(
        database,
        make_event,
        make_snapshot,
        finished_at=NOW - timedelta(days=8),
    )
    github = FakeGitHub()

    summary = collect_outcomes(database, github, clock=lambda: NOW)

    assert summary.terminal == 1
    assert github.calls == 0
    with database.connection() as connection:
        check = get_outcome_check(connection, run_id)
    assert check is not None
    assert check.terminal is True


def test_github_errors_are_recorded_without_aborting_the_batch(
    database: Database, make_event: object, make_snapshot: object
) -> None:
    run_id, _ = _make_run(database, make_event, make_snapshot)

    class BrokenGitHub(FakeGitHub):
        def get_workflow_run(self, repo: str, run_id: int) -> GitHubWorkflowRun:
            raise RuntimeError("rate limited")

    summary = collect_outcomes(database, BrokenGitHub(), clock=lambda: NOW + timedelta(hours=1))

    assert summary.errors == 1
    with database.connection() as connection:
        check = get_outcome_check(connection, run_id)
    assert check is not None
    assert check.error is not None
    assert "rate limited" in check.error
    assert check.attempts == 1


def test_collection_is_idempotent(
    database: Database, make_event: object, make_snapshot: object
) -> None:
    run_id, github_run_id = _make_run(database, make_event, make_snapshot)
    github = FakeGitHub()
    github.workflow_runs[github_run_id] = _completed_run(attempt=2, conclusion="success")

    first = collect_outcomes(database, github, clock=lambda: NOW + timedelta(hours=1))
    second = collect_outcomes(database, github, clock=lambda: NOW + timedelta(hours=2))

    assert first.labeled == 1
    assert second.checked == 0
    assert _outcome(database, run_id) == ("correct", "correct")


def test_attempt_cap_stops_a_stuck_run(
    database: Database, make_event: object, make_snapshot: object
) -> None:
    run_id, _ = _make_run(database, make_event, make_snapshot, category="real_bug")
    with database.connection() as connection:
        upsert_outcome_check(
            connection,
            run_id=run_id,
            attempts=8,
            first_checked_at=NOW,
            last_checked_at=NOW,
            next_check_at=NOW,
            terminal=False,
        )
    github = FakeGitHub()

    summary = collect_outcomes(database, github, clock=lambda: NOW + timedelta(hours=1))

    assert summary.terminal == 1
    with database.connection() as connection:
        check = get_outcome_check(connection, run_id)
    assert check is not None
    assert check.terminal is True
    assert github.calls == 0


def test_outcome_collector_has_no_llm_import() -> None:
    tree = ast.parse(inspect.getsource(outcomes_module))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    assert not any("llm" in name for name in imports)
