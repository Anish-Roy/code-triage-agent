from __future__ import annotations

from pathlib import Path

import pytest

from code_triage_agent.cli import main

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_validate_config_succeeds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CTA_CONFIG_DIR", str(PROJECT_ROOT / "config"))

    exit_code = main(["validate-config"])

    assert exit_code == 0
    assert capsys.readouterr().out.startswith("OK: 3 triggers, 4 skills")


def test_validate_config_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("CTA_CONFIG_DIR", str(tmp_path / "missing"))

    exit_code = main(["validate-config"])

    assert exit_code == 1
    assert capsys.readouterr().err.startswith("INVALID:")


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main([])

    assert exit_code == 2
    assert "validate-config" in capsys.readouterr().out
