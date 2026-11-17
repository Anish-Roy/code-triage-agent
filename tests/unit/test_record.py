from __future__ import annotations

from pathlib import Path

from code_triage_agent.db import Database, get_run, list_steps
from code_triage_agent.models import (
    Evidence,
    RecommendedAction,
    RunnerStep,
    TokenUsage,
    TriageDecision,
)
from code_triage_agent.runner.actions import CommentResult
from code_triage_agent.runner.loop import LoopResult
from code_triage_agent.runner.record import RunRecorder


def _decision() -> TriageDecision:
    return TriageDecision(
        category="real_bug",
        confidence=0.8,
        summary="Login returns 500.",
        evidence=[Evidence(source="log", quote="expected 200 got 500", why="assertion")],
        recommended_action=RecommendedAction(type="comment"),
    )


def test_recorder_persists_run_and_steps(
    database: Database, settings: object, make_event: object, make_snapshot: object
) -> None:
    event = make_event()  # type: ignore[operator]
    snapshot = make_snapshot(metadata={"event_id": event.id, "run_id": "r_record"})  # type: ignore[operator]
    recorder = RunRecorder(
        database=database,
        metadata=snapshot.metadata,
        trigger_id="pr_failure",
        skill_id="triage-pr-failure",
        skill_version="v1",
        snapshot_path=snapshot.directory,
        config_sha="sha",
    )
    recorder.start()

    recorder.add_steps(
        [
            RunnerStep(seq=1, kind="llm", tool="llm:fake", args={}, result={"content": ""}),
            RunnerStep(
                seq=2,
                kind="action",
                tool="submit_triage",
                args={"decision": _decision().model_dump(mode="json")},
                result={"accepted": True},
            ),
        ]
    )
    artifact = recorder.finish(
        LoopResult(
            decision=_decision(),
            status="succeeded",
            cost_usd=0.01,
            usage=TokenUsage(input_tokens=100, output_tokens=50),
        ),
        actions=["comment_on_pr"],
        comment=CommentResult(posted=True, body="body", comment_id=9),
    )

    with database.connection() as connection:
        run = get_run(connection, "r_record")
        steps = list_steps(connection, "r_record")

    assert recorder.persistent is True
    assert run is not None
    assert run.status == "succeeded"
    assert run.category == "real_bug"
    assert run.trigger_id == "pr_failure"
    assert run.skill_id == "triage-pr-failure"
    assert run.actions == ["comment_on_pr"]
    assert run.cost_usd == 0.01
    assert [step.tool for step in steps] == ["llm:fake", "submit_triage"]
    assert artifact.comment is not None
    assert artifact.comment["posted"] is True


def test_recorder_does_not_persist_without_an_event(
    database: Database, make_snapshot: object
) -> None:
    snapshot = make_snapshot(metadata={"event_id": 999, "run_id": "r_orphan"})  # type: ignore[operator]
    recorder = RunRecorder(
        database=database,
        metadata=snapshot.metadata,
        trigger_id=None,
        skill_id=None,
        skill_version=None,
        snapshot_path=snapshot.directory,
    )
    recorder.start()

    artifact = recorder.finish_failure("model crashed")

    with database.connection() as connection:
        assert get_run(connection, "r_orphan") is None
    assert artifact.status == "failed"
    assert artifact.decision.category == "unknown"
    assert artifact.error == "model crashed"


def test_recorder_upserts_steps_on_a_rerun(database: Database, make_snapshot: object) -> None:
    from code_triage_agent.db import insert_event

    with database.connection() as connection:
        event = insert_event(
            connection,
            delivery_id="delivery-rerun",
            repo="acme/widgets",
            workflow_run_id=9001,
            run_attempt=1,
            conclusion="failure",
            payload_path="payload.json",
        )
    assert event is not None
    snapshot = make_snapshot(metadata={"event_id": event.id, "run_id": "r_rerun"})  # type: ignore[operator]
    recorder = RunRecorder(
        database=database,
        metadata=snapshot.metadata,
        trigger_id="pr_failure",
        skill_id="triage-pr-failure",
        skill_version="v1",
        snapshot_path=snapshot.directory,
    )
    recorder.start()

    recorder.add_steps([RunnerStep(seq=1, kind="llm", tool="llm:first", args={}, result={})])
    recorder.add_steps([RunnerStep(seq=1, kind="llm", tool="llm:second", args={}, result={})])

    with database.connection() as connection:
        steps = list_steps(connection, "r_rerun")
    assert [step.tool for step in steps] == ["llm:second"]


def test_artifact_is_json_serializable(make_snapshot: object) -> None:
    snapshot = make_snapshot()  # type: ignore[operator]
    recorder = RunRecorder(
        database=None,
        metadata=snapshot.metadata,
        trigger_id="pr_failure",
        skill_id="triage-pr-failure",
        skill_version="v1",
        snapshot_path=Path(snapshot.directory),
    )
    recorder.start()
    artifact = recorder.finish(
        LoopResult(decision=_decision(), status="succeeded", cost_usd=0.0),
        actions=[],
    )

    payload = artifact.to_dict()

    assert payload["decision"]["category"] == "real_bug"  # type: ignore[index]
    assert payload["steps"] == []
    assert payload["started_at"] is not None
