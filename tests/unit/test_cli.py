from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from code_triage_agent.cli import _resolve_event, main
from code_triage_agent.db import Database

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "github"


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


def _payload_args(**overrides: object) -> Namespace:
    values: dict[str, object] = {
        "event_id": None,
        "payload": FIXTURES / "workflow_run_event.json",
        "delivery_id": "delivery-cli",
        "repo": "acme/widgets",
        "run_id": 9001,
        "attempt": None,
    }
    values.update(overrides)
    return Namespace(**values)


def test_resolve_event_imports_a_payload(tmp_path: Path) -> None:
    from code_triage_agent.settings import Settings

    settings = Settings(
        _env_file=None,
        config_dir=PROJECT_ROOT / "config",
        database_path=tmp_path / "var" / "triage.db",
        snapshot_root=tmp_path / "snapshots",
        event_root=tmp_path / "events",
    )
    settings.ensure_runtime_dirs()
    database = Database(settings.database_path)
    database.migrate()

    event = _resolve_event(database, settings, _payload_args())

    assert event is not None
    assert event.repo == "acme/widgets"
    assert event.workflow_run_id == 9001
    assert event.run_attempt == 1
    assert Path(event.payload_path).is_file()

    same_run = _resolve_event(database, settings, _payload_args(delivery_id="delivery-cli-2"))
    assert same_run is not None
    assert same_run.id == event.id


def test_resolve_event_rejects_mismatched_run_id(tmp_path: Path) -> None:
    from code_triage_agent.settings import Settings

    settings = Settings(
        _env_file=None,
        config_dir=PROJECT_ROOT / "config",
        database_path=tmp_path / "var" / "triage.db",
        snapshot_root=tmp_path / "snapshots",
        event_root=tmp_path / "events",
    )
    settings.ensure_runtime_dirs()
    database = Database(settings.database_path)
    database.migrate()

    with pytest.raises(ValueError, match="does not match"):
        _resolve_event(database, settings, _payload_args(run_id=123))
