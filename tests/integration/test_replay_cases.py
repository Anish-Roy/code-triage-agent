from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from code_triage_agent.config.loader import LoadedConfig
from code_triage_agent.config.schema import MemoryNote
from code_triage_agent.db import Database, get_run, list_steps
from code_triage_agent.github.models import GitHubIssueComment
from code_triage_agent.models import LLMResponse, ToolCall
from code_triage_agent.runner.actions import run_marker
from code_triage_agent.runner.llm import FakeLLM
from code_triage_agent.runner.pipeline import triage_snapshot
from code_triage_agent.settings import Settings

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CASES = json.loads((FIXTURES / "llm" / "replay_cases.json").read_text(encoding="utf-8"))["cases"]


def _settings(tmp_path: Path, *, replay: bool = True) -> Settings:
    return Settings(
        _env_file=None,
        config_dir=Path(__file__).resolve().parents[2] / "config",
        database_path=tmp_path / "var" / "triage.db",
        snapshot_root=tmp_path / "snapshots",
        event_root=tmp_path / "events",
        dry_run=replay,
        replay_mode=replay,
    )


def _llm(case: dict[str, Any]) -> FakeLLM:
    turns: list[LLMResponse] = []
    reads = case.get("reads") or []
    if reads:
        turns.append(
            LLMResponse(
                tool_calls=[
                    ToolCall(id=f"read_{index}", name=read["name"], arguments=read["arguments"])
                    for index, read in enumerate(reads)
                ],
                usage={"input_tokens": 500, "output_tokens": 50},  # type: ignore[arg-type]
            )
        )
    turns.append(
        LLMResponse(
            tool_calls=[ToolCall(id="submit", name="submit_triage", arguments=case["decision"])],
            usage={"input_tokens": 800, "output_tokens": 200},  # type: ignore[arg-type]
        )
    )
    return FakeLLM(turns)


class IdempotentGitHub:
    def __init__(self) -> None:
        self.comments: list[GitHubIssueComment] = []
        self.posts = 0

    def list_issue_comments(self, repo: str, number: int) -> list[GitHubIssueComment]:
        return list(self.comments)

    def post_issue_comment(self, repo: str, number: int, body: str) -> int:
        self.posts += 1
        comment = GitHubIssueComment(id=100 + self.posts, body=body)
        self.comments.append(comment)
        return comment.id


@pytest.mark.parametrize("case", CASES, ids=[case["case_id"] for case in CASES])
def test_replay_case_classifies_and_comments(
    case: dict[str, Any],
    project_config: LoadedConfig,
    make_snapshot: object,
    tmp_path: Path,
) -> None:
    memory = [MemoryNote.model_validate(item) for item in case.get("memory", [])]
    snapshot = make_snapshot(  # type: ignore[operator]
        run_id=f"r_{case['case_id']}",
        source=case["source"],
        conclusion=case["conclusion"],
        pr_number=case["pr_number"],
        logs=case["logs"],
        diff=case["diff"],
        memory=memory,
        extra_files=case.get("files", {}),
    )
    llm = _llm(case)

    result = triage_snapshot(
        snapshot_dir=snapshot.directory,
        settings=_settings(tmp_path),
        config=project_config,
        llm=llm,
        persist=False,
        skill_override=case["skill"] if case.get("override") else None,
    )

    assert result.artifact.status == "succeeded", result.artifact.error
    assert result.artifact.decision.category == case["expected_category"]
    assert result.route.skill_id == case["skill"]
    assert result.prompt is not None
    assert result.artifact.actions == ["comment_on_pr"]
    assert result.artifact.comment is not None
    assert run_marker(snapshot.metadata.run_id) in result.artifact.comment["body"]  # type: ignore[index]
    assert result.artifact.comment["skipped_reason"] == "dry_run/replay"
    assert all(step.tool != "open_fix_pr" for step in result.artifact.steps)


def test_replayed_run_posts_one_idempotent_comment(
    project_config: LoadedConfig, make_snapshot: object, tmp_path: Path
) -> None:
    case = CASES[0]
    snapshot = make_snapshot(  # type: ignore[operator]
        run_id="r_idempotent",
        source=case["source"],
        conclusion=case["conclusion"],
        pr_number=case["pr_number"],
        logs=case["logs"],
        diff=case["diff"],
    )
    github = IdempotentGitHub()
    settings = _settings(tmp_path, replay=False)
    settings = settings.model_copy(update={"dry_run": False, "replay_mode": False})

    first = triage_snapshot(
        snapshot_dir=snapshot.directory,
        settings=settings,
        config=project_config,
        llm=_llm(case),
        github=github,
        persist=False,
    )
    second = triage_snapshot(
        snapshot_dir=snapshot.directory,
        settings=settings,
        config=project_config,
        llm=_llm(case),
        github=github,
        persist=False,
    )

    assert first.artifact.comment is not None
    assert first.artifact.comment["posted"] is True
    assert second.artifact.comment is not None
    assert second.artifact.comment["posted"] is False
    assert "already exists" in (second.artifact.comment["skipped_reason"] or "")
    assert github.posts == 1


def test_pipeline_persists_a_complete_run(
    project_config: LoadedConfig,
    make_event: object,
    make_snapshot: object,
    tmp_path: Path,
) -> None:
    event = make_event()  # type: ignore[operator]
    case = CASES[0]
    snapshot = make_snapshot(  # type: ignore[operator]
        run_id="r_persisted",
        source=case["source"],
        conclusion=case["conclusion"],
        pr_number=case["pr_number"],
        logs=case["logs"],
        diff=case["diff"],
        metadata={"event_id": event.id},
    )

    result = triage_snapshot(
        snapshot_dir=snapshot.directory,
        settings=_settings(tmp_path),
        config=project_config,
        llm=_llm(case),
        persist=True,
    )

    database = Database(tmp_path / "var" / "triage.db")
    with database.connection() as connection:
        run = get_run(connection, "r_persisted")
        steps = list_steps(connection, "r_persisted")

    assert result.artifact.status == "succeeded"
    assert run is not None
    assert run.status == "succeeded"
    assert run.category == case["expected_category"]
    assert run.trigger_id == "pr_failure"
    assert run.skill_id == "triage-pr-failure"
    assert run.skill_version == result.artifact.skill_version
    assert run.config_sha == project_config.config_sha
    assert run.actions == ["comment_on_pr"]
    assert [step.tool for step in steps] == [step.tool for step in result.artifact.steps]
