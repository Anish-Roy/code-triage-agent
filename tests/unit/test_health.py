from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from code_triage_agent.server.app import create_app
from code_triage_agent.settings import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _settings(tmp_path: Path, config_dir: Path | None = None) -> Settings:
    return Settings(
        _env_file=None,
        config_dir=config_dir or PROJECT_ROOT / "config",
        database_path=tmp_path / "var" / "triage.db",
        snapshot_root=tmp_path / "snapshots",
        event_root=tmp_path / "events",
    )


def test_health_is_ok(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))

    response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["config"]["triggers"] == 3
    assert body["checks"]["config"]["skills"] == 4
    assert body["checks"]["database"]["ok"] is True
    assert (tmp_path / "var" / "triage.db").exists()


def test_health_reports_broken_config(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path, config_dir=tmp_path / "missing")))

    response = client.get("/healthz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
    assert body["checks"]["config"]["ok"] is False
    assert "config directory not found" in body["checks"]["config"]["error"]
