from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from code_triage_agent.cli import main

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = PROJECT_ROOT / "tests" / "fixtures" / "snapshots" / "pr-failure"
GOLDEN = PROJECT_ROOT / "tests" / "fixtures" / "golden" / "triage_pr_failure.json"


def _stable(data: dict[str, Any]) -> dict[str, Any]:
    """Drop fields that legitimately change between runs."""
    return {
        "status": data["status"],
        "trigger_id": data["trigger_id"],
        "skill_id": data["skill_id"],
        "decision": data["decision"],
        "actions": data["actions"],
        "comment": None
        if data["comment"] is None
        else {
            key: data["comment"][key] for key in ("posted", "comment_id", "skipped_reason", "body")
        },
        "error": data["error"],
        "cost_usd": data["cost_usd"],
        "usage": data["usage"],
        "steps": [
            {key: step[key] for key in ("kind", "tool", "args", "result", "is_error", "cost_usd")}
            for step in data["steps"]
        ],
    }


def test_triage_cli_matches_golden_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("CTA_CONFIG_DIR", str(PROJECT_ROOT / "config"))
    monkeypatch.setenv("CTA_DATABASE_PATH", str(tmp_path / "var" / "triage.db"))
    monkeypatch.setenv("CTA_SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
    monkeypatch.setenv("CTA_EVENT_ROOT", str(tmp_path / "events"))

    exit_code = main(["triage", "--snapshot", str(SNAPSHOT)])

    assert exit_code == 0
    data = json.loads(capsys.readouterr().out)
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert _stable(data) == expected


def test_triage_cli_requires_a_replay_script(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "metadata.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CTA_CONFIG_DIR", str(PROJECT_ROOT / "config"))

    exit_code = main(["triage", "--snapshot", str(snapshot)])

    assert exit_code == 1
    assert "replay script not found" in capsys.readouterr().err


def test_triage_cli_can_force_a_skill(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("CTA_CONFIG_DIR", str(PROJECT_ROOT / "config"))
    monkeypatch.setenv("CTA_DATABASE_PATH", str(tmp_path / "var" / "triage.db"))

    exit_code = main(["triage", "--snapshot", str(SNAPSHOT), "--skill", "propose-fix"])

    assert exit_code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["skill_id"] == "propose-fix"
    assert data["status"] == "succeeded"
