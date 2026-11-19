from __future__ import annotations

from pathlib import Path

import pytest

from code_triage_agent.evals.cases import (
    CaseError,
    assert_not_frozen,
    case_snapshot_dir,
    load_case,
    load_case_set,
    validate_case_id,
    write_case,
)
from code_triage_agent.models import EvalCase


def _case(case_id: str = "pr-flaky-001") -> EvalCase:
    return EvalCase(
        case_id=case_id,
        snapshot_path="snapshot",
        expected_category="flaky_test",
        expected_action="comment",
        labeler="alice",
        notes="passed on rerun with the same signature",
        tags=["smoke"],
    )


@pytest.mark.parametrize(
    "value",
    ["../escape", "a/b", "/absolute", "", "UPPER", "..", ".", "a" * 65],
)
def test_validate_case_id_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(CaseError):
        validate_case_id(value)


def test_validate_case_id_accepts_normal_values() -> None:
    assert validate_case_id("pr-flaky-001") == "pr-flaky-001"
    assert validate_case_id("case_v1.2") == "case_v1.2"


def test_write_and_load_case_roundtrip(tmp_path: Path) -> None:
    case_dir = tmp_path / "pr-flaky-001"
    (case_dir / "snapshot").mkdir(parents=True)

    path = write_case(case_dir, _case())
    loaded = load_case(case_dir)

    assert path == case_dir / "case.json"
    assert loaded == _case()
    assert case_snapshot_dir(case_dir, loaded) == (case_dir / "snapshot").resolve()


def test_write_case_refuses_to_overwrite(tmp_path: Path) -> None:
    case_dir = tmp_path / "pr-flaky-001"
    write_case(case_dir, _case())

    with pytest.raises(CaseError, match="overwrite"):
        write_case(case_dir, _case())


def test_load_case_requires_matching_directory_name(tmp_path: Path) -> None:
    case_dir = tmp_path / "wrong-name"
    case_dir.mkdir()
    (case_dir / "case.json").write_text(
        '{"case_id":"pr-flaky-001","snapshot_path":"snapshot","expected_category":"flaky_test"}',
        encoding="utf-8",
    )

    with pytest.raises(CaseError, match="must match directory name"):
        load_case(case_dir)


def test_load_case_rejects_unknown_fields_and_categories(tmp_path: Path) -> None:
    case_dir = tmp_path / "pr-flaky-001"
    case_dir.mkdir()
    (case_dir / "case.json").write_text(
        '{"case_id":"pr-flaky-001","snapshot_path":"snapshot",'
        '"expected_category":"not_a_category","typo":true}',
        encoding="utf-8",
    )

    with pytest.raises(CaseError, match="expected_category"):
        load_case(case_dir)


def test_load_case_set_is_sorted_and_skips_non_cases(tmp_path: Path) -> None:
    for case_id in ("case-b", "case-a"):
        case_dir = tmp_path / case_id
        (case_dir / "snapshot").mkdir(parents=True)
        write_case(case_dir, _case(case_id))
    (tmp_path / "not-a-case").mkdir()

    loaded = load_case_set(tmp_path)

    assert [case.case_id for _, case in loaded] == ["case-a", "case-b"]


def test_frozen_guard_only_blocks_paths_inside_the_frozen_root(tmp_path: Path) -> None:
    frozen = tmp_path / "evals" / "frozen"
    reports = tmp_path / "evals" / "reports"
    frozen.mkdir(parents=True)
    reports.mkdir(parents=True)

    assert_not_frozen(reports, frozen)
    with pytest.raises(CaseError, match="frozen eval set"):
        assert_not_frozen(frozen / "reports", frozen)
    with pytest.raises(CaseError, match="frozen eval set"):
        assert_not_frozen(frozen, frozen)


def test_missing_snapshot_is_reported(tmp_path: Path) -> None:
    case_dir = tmp_path / "pr-flaky-001"
    write_case(case_dir, _case())

    with pytest.raises(CaseError, match="snapshot not found"):
        case_snapshot_dir(case_dir, _case())
