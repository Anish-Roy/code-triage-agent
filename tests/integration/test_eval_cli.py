from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from code_triage_agent.cli import main

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FROZEN = PROJECT_ROOT / "evals" / "frozen"
BASELINE = PROJECT_ROOT / "evals" / "baselines" / "frozen.json"


@pytest.fixture(autouse=True)
def _paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CTA_CONFIG_DIR", str(PROJECT_ROOT / "config"))
    monkeypatch.setenv("CTA_DATABASE_PATH", str(tmp_path / "var" / "triage.db"))
    monkeypatch.setenv("CTA_SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
    monkeypatch.setenv("CTA_EVENT_ROOT", str(tmp_path / "events"))
    monkeypatch.setenv("CTA_EVAL_FROZEN_DIR", str(FROZEN))
    monkeypatch.setenv("CTA_EVAL_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("CTA_EVAL_CACHE_DIR", str(tmp_path / "cache"))


def test_eval_run_report_and_gates_through_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reports = tmp_path / "reports"
    exit_code = main(
        [
            "eval",
            "run",
            "--set",
            "frozen",
            "--case",
            "pr-real-bug-login",
            "--baseline",
            str(BASELINE),
            "--report-dir",
            str(reports),
            "--json",
        ]
    )
    assert exit_code == 0
    assert '"accuracy": 1.0' in capsys.readouterr().out

    report = next(reports.glob("*.json"))
    assert main(["eval", "report", "--report", str(report), "--json"]) == 0
    assert '"set_name": "frozen"' in capsys.readouterr().out

    assert main(["eval", "gates", "--report", str(report), "--baseline", str(BASELINE)]) == 0
    assert "passed: yes" in capsys.readouterr().out


def test_eval_gates_returns_nonzero_on_accuracy_regression(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from code_triage_agent.models import ReplayMetrics, ReplayReport

    def _write(path: Path, *, accuracy: float) -> None:
        report = ReplayReport(
            run_at=datetime(2026, 11, 19, 12, 0, tzinfo=UTC),
            config_sha="config",
            set_name="frozen",
            model="fake-llm",
            metrics=ReplayMetrics(
                total_cases=1,
                correct_cases=1 if accuracy else 0,
                accuracy=accuracy,
            ),
            budget_usd=15.0,
        )
        path.write_text(report.model_dump_json(), encoding="utf-8")

    current = tmp_path / "current.json"
    baseline = tmp_path / "baseline.json"
    _write(current, accuracy=0.0)
    _write(baseline, accuracy=1.0)

    exit_code = main(["eval", "gates", "--report", str(current), "--baseline", str(baseline)])

    assert exit_code == 1
    assert "accuracy regressed" in capsys.readouterr().out


def test_outcomes_collect_without_credentials_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in (
        "CTA_GITHUB_APP_ID",
        "CTA_GITHUB_APP_PRIVATE_KEY",
        "CTA_GITHUB_INSTALLATION_ID",
    ):
        monkeypatch.delenv(name, raising=False)

    exit_code = main(["outcomes", "collect", "--limit", "1"])

    assert exit_code == 1
    assert "GitHub App credentials are required" in capsys.readouterr().err
