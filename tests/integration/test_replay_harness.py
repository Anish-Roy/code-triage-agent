from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from code_triage_agent.background.replay import run_replay
from code_triage_agent.evals.cases import CaseError, load_case_set
from code_triage_agent.settings import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FROZEN = PROJECT_ROOT / "evals" / "frozen"
SKILLS = {"triage-pr-failure", "find-culprit", "triage-nightly", "propose-fix"}


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        config_dir=PROJECT_ROOT / "config",
        database_path=tmp_path / "var" / "triage.db",
        snapshot_root=tmp_path / "snapshots",
        event_root=tmp_path / "events",
        eval_frozen_dir=FROZEN,
        eval_reports_dir=tmp_path / "reports",
        eval_cache_dir=tmp_path / "cache",
    )


def test_frozen_set_meets_the_phase_3_shape() -> None:
    cases = [case for _, case in load_case_set(FROZEN)]

    assert len(cases) >= 30
    per_skill = Counter(case.expected_skill for case in cases)
    assert set(per_skill) == SKILLS
    assert all(count >= 6 for count in per_skill.values())
    assert sum(1 for case in cases if case.hard_boundary) >= 10
    assert sum(1 for case in cases if "injection" in case.tags) >= 3
    assert sum(1 for case in cases if case.reviewer) >= 10
    assert sum(1 for case in cases if "smoke" in case.tags) >= 5
    for case in cases:
        assert case.labeler
        assert case.snapshot_path == "snapshot"


def test_replay_writes_json_and_markdown_reports(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    outcome = run_replay(
        set_dir=FROZEN,
        settings=settings,
        case_ids=[
            "pr-real-bug-login",
            "culprit-dependency-pin",
            "nightly-infra-network",
            "fix-dependency-import",
        ],
        report_dir=tmp_path / "reports",
        project_root=PROJECT_ROOT,
    )

    report = outcome.report
    assert report.metrics.total_cases == 4
    assert report.metrics.correct_cases == 4
    assert report.metrics.accuracy == 1.0
    assert report.metrics.confusion_matrix
    assert set(report.metrics.per_skill_accuracy) == SKILLS
    assert report.gates is not None and report.gates.passed
    assert outcome.json_path.is_file()
    assert outcome.markdown_path.is_file()
    payload = json.loads(outcome.json_path.read_text(encoding="utf-8"))
    assert payload["set_name"] == "frozen"
    assert len(payload["cases"]) == 4
    markdown = outcome.markdown_path.read_text(encoding="utf-8")
    assert "## Confusion matrix" in markdown
    assert "Composite score" in markdown


def test_smoke_selection_is_small_and_covers_every_skill(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    outcome = run_replay(
        set_dir=FROZEN,
        settings=settings,
        smoke=True,
        report_dir=tmp_path / "reports",
        project_root=PROJECT_ROOT,
    )

    assert 1 <= outcome.report.metrics.total_cases <= settings.eval_smoke_cases
    assert outcome.report.metrics.accuracy == 1.0


def test_replay_refuses_to_write_inside_the_frozen_set(tmp_path: Path) -> None:
    with pytest.raises(CaseError, match="frozen eval set"):
        run_replay(
            set_dir=FROZEN,
            settings=_settings(tmp_path),
            case_ids=["pr-real-bug-login"],
            report_dir=FROZEN / "reports",
            project_root=PROJECT_ROOT,
        )


def test_replay_rejects_unknown_case_ids(tmp_path: Path) -> None:
    with pytest.raises(CaseError, match="unknown case id"):
        run_replay(
            set_dir=FROZEN,
            settings=_settings(tmp_path),
            case_ids=["does-not-exist"],
            report_dir=tmp_path / "reports",
            project_root=PROJECT_ROOT,
        )


def test_live_run_requires_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CTA_LLM_API_KEY", raising=False)
    with pytest.raises(CaseError, match="CTA_LLM_API_KEY"):
        run_replay(
            set_dir=FROZEN,
            settings=_settings(tmp_path),
            case_ids=["pr-real-bug-login"],
            allow_live=True,
            report_dir=tmp_path / "reports",
            project_root=PROJECT_ROOT,
        )
