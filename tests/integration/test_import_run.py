from __future__ import annotations

from pathlib import Path

import pytest

from code_triage_agent.db import Database, create_run, finish_run, get_eval
from code_triage_agent.evals.cases import CaseError, load_case
from code_triage_agent.evals.import_run import import_run
from code_triage_agent.models import TriageDecision


def _seed_run(
    database: Database,
    make_event: object,
    make_snapshot: object,
    *,
    run_id: str = "r_import",
    category: str = "real_bug",
) -> str:
    event = make_event(  # type: ignore[operator]
        delivery_id=f"delivery-{run_id}",
        workflow_run_id=9100,
    )
    snapshot = make_snapshot(  # type: ignore[operator]
        run_id=run_id,
        pr_number=12,
        metadata={"event_id": event.id, "workflow_run_id": 9100},
    )
    decision = TriageDecision(
        category=category,
        confidence=0.9,
        summary="Imported decision.",
        evidence=[
            {
                "source": "log",
                "quote": "AssertionError: expected 200 got 500",
                "why": "test assertion",
            }
        ],
        recommended_action={"type": "comment", "params": {}},
    )
    with database.connection() as connection:
        create_run(
            connection,
            event_id=event.id,
            run_id=run_id,
            skill_id="triage-pr-failure",
            signature="sig_v1:import",
            snapshot_path=str(snapshot.directory),
        )
        finish_run(
            connection,
            run_id,
            status="succeeded",
            category=category,
            predicted_action="comment",
            actions=["comment_on_pr"],
            snapshot_path=str(snapshot.directory),
            decision=decision,
        )
    return run_id


def test_import_copies_snapshot_and_writes_case(
    database: Database,
    make_event: object,
    make_snapshot: object,
    tmp_path: Path,
) -> None:
    run_id = _seed_run(database, make_event, make_snapshot)

    target = import_run(
        database,
        run_id=run_id,
        target_dir=tmp_path / "evals" / "frozen",
        labeler="tester",
    )

    case = load_case(target)
    assert case.case_id == target.name
    assert case.expected_category == "real_bug"
    assert case.expected_action == "comment"
    assert case.labeler == "tester"
    assert case.expected_skill == "triage-pr-failure"
    assert "imported" in case.tags
    assert (target / "snapshot" / "metadata.json").is_file()
    with database.connection() as connection:
        eval_row = get_eval(connection, case.case_id)
    assert eval_row is not None
    assert eval_row.set_name == "frozen"


def test_import_refuses_to_overwrite(
    database: Database,
    make_event: object,
    make_snapshot: object,
    tmp_path: Path,
) -> None:
    run_id = _seed_run(database, make_event, make_snapshot)
    target_dir = tmp_path / "evals" / "frozen"
    import_run(database, run_id=run_id, target_dir=target_dir, case_id="kept-case")

    with pytest.raises(CaseError, match="refusing to overwrite"):
        import_run(database, run_id=run_id, target_dir=target_dir, case_id="kept-case")


def test_import_rejects_path_traversal(
    database: Database,
    make_event: object,
    make_snapshot: object,
    tmp_path: Path,
) -> None:
    run_id = _seed_run(database, make_event, make_snapshot)

    with pytest.raises(CaseError, match="invalid case_id"):
        import_run(database, run_id=run_id, target_dir=tmp_path / "set", case_id="../escape")
    assert not (tmp_path / "escape").exists()


def test_import_does_not_leave_a_partial_case(
    database: Database,
    make_event: object,
    make_snapshot: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _seed_run(database, make_event, make_snapshot)
    target_dir = tmp_path / "set"

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr("code_triage_agent.evals.import_run.write_case", _boom)

    with pytest.raises(RuntimeError, match="disk full"):
        import_run(database, run_id=run_id, target_dir=target_dir, case_id="partial")
    assert not (target_dir / "partial").exists()
