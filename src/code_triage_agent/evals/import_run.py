"""Import a finished run into a new eval case without overwriting anything."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import cast

from code_triage_agent.db import Database, get_run, upsert_eval, utcnow
from code_triage_agent.evals.cases import (
    SNAPSHOT_DIR,
    CaseError,
    is_within,
    validate_case_id,
    write_case,
)
from code_triage_agent.models import (
    Category,
    EvalCase,
    EvalExpectedAction,
    RunRecord,
)

_ALLOWED_CATEGORIES = {
    "flaky_test",
    "infra_issue",
    "dependency_or_config",
    "real_bug",
    "unknown",
}

_ACTION_MAP: dict[str, EvalExpectedAction] = {
    "comment": "comment",
    "comment_on_pr": "comment",
    "rerun": "rerun",
    "rerun_failed_jobs": "rerun",
    "fix_pr": "fix_pr",
    "open_fix_pr": "fix_pr",
    "issue": "issue",
    "open_issue": "issue",
}


def import_run(
    database: Database,
    *,
    run_id: str,
    target_dir: Path,
    case_id: str | None = None,
    labeler: str = "unlabeled",
    notes: str | None = None,
    reviewer: str | None = None,
    set_name: str = "frozen",
    now: datetime | None = None,
) -> Path:
    """Copy a run snapshot and decision into ``target_dir/<case_id>``.

    The starting label is the run's own prediction. It is explicitly marked as
    unlabeled-by-default so a human can correct it before the case is trusted.
    """
    with database.connection() as connection:
        run = get_run(connection, run_id)
    if run is None:
        raise CaseError(f"run not found: {run_id}")

    identifier = validate_case_id(case_id or _default_case_id(run))
    target_root = target_dir.resolve()
    target_case = (target_root / identifier).resolve()
    if not is_within(target_case, target_root):
        raise CaseError(f"case path escapes the target set: {target_case}")
    if target_case.exists():
        raise CaseError(f"refusing to overwrite existing case: {target_case}")

    snapshot = _run_snapshot(run)
    target_case.mkdir(parents=True, exist_ok=False)
    try:
        shutil.copytree(snapshot, target_case / SNAPSHOT_DIR)
    except Exception:
        shutil.rmtree(target_case, ignore_errors=True)
        raise

    case = EvalCase(
        case_id=identifier,
        snapshot_path=SNAPSHOT_DIR,
        expected_category=_expected_category(run),
        expected_action=_expected_action(run),
        labeler=labeler,
        notes=notes
        or (
            f"Imported from run {run.id}. The starting label is the run's own "
            "prediction; a human must confirm it before the case is treated as "
            "ground truth."
        ),
        expected_skill=run.skill_id,
        reviewer=reviewer,
        tags=["imported"],
        reviewed_at=None,
    )
    try:
        write_case(target_case, case)
    except Exception:
        shutil.rmtree(target_case, ignore_errors=True)
        raise

    with database.connection() as connection:
        upsert_eval(
            connection,
            case_id=identifier,
            set_name=set_name,
            snapshot_path=str(target_case / SNAPSHOT_DIR),
            expected_category=case.expected_category,
            expected_action=case.expected_action,
            notes=case.notes,
            labeler=case.labeler,
            reviewer=case.reviewer,
            now=now or utcnow(),
        )
    return target_case


def _run_snapshot(run: RunRecord) -> Path:
    if not run.snapshot_path:
        raise CaseError(f"run {run.id} has no snapshot_path")
    snapshot = Path(run.snapshot_path).resolve()
    if not snapshot.is_dir():
        raise CaseError(f"snapshot directory not found for run {run.id}: {snapshot}")
    return snapshot


def _default_case_id(run: RunRecord) -> str:
    skill = (run.skill_id or "unknown").replace("_", "-")
    suffix = run.id.removeprefix("r_")[:12]
    return f"{skill}-{suffix}"


def _expected_category(run: RunRecord) -> Category:
    if run.category in _ALLOWED_CATEGORIES:
        return cast(Category, run.category)
    return "unknown"


def _expected_action(run: RunRecord) -> EvalExpectedAction:
    if run.predicted_action in _ACTION_MAP:
        return _ACTION_MAP[run.predicted_action]
    for action in run.actions:
        if action in _ACTION_MAP:
            return _ACTION_MAP[action]
    return "none"


__all__ = ["import_run"]
