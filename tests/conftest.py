"""Shared pytest fixtures and helpers."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from code_triage_agent.config.loader import LoadedConfig, load_config
from code_triage_agent.db import Database, insert_event
from code_triage_agent.github.webhook import write_event_payload
from code_triage_agent.models import EventRecord
from code_triage_agent.settings import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
GITHUB_FIXTURES = FIXTURES / "github"
LOG_FIXTURES = FIXTURES / "logs"


@pytest.fixture
def project_config() -> LoadedConfig:
    return load_config(PROJECT_ROOT / "config")


@pytest.fixture
def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "var" / "triage.db")
    db.migrate()
    return db


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        config_dir=PROJECT_ROOT / "config",
        database_path=tmp_path / "var" / "triage.db",
        snapshot_root=tmp_path / "snapshots",
        event_root=tmp_path / "events",
    )


@pytest.fixture
def make_event(database: Database, settings: Settings) -> Callable[..., EventRecord]:
    def _make(
        *,
        payload: dict[str, Any] | None = None,
        delivery_id: str = "delivery-1",
        repo: str = "acme/widgets",
        workflow_run_id: int = 9001,
        run_attempt: int = 1,
        conclusion: str = "failure",
    ) -> EventRecord:
        body = payload if payload is not None else _default_payload()
        body_bytes = json.dumps(body).encode("utf-8")
        payload_path = write_event_payload(settings.event_root, delivery_id, body_bytes)
        with database.connection() as connection:
            event = insert_event(
                connection,
                delivery_id=delivery_id,
                repo=repo,
                workflow_run_id=workflow_run_id,
                run_attempt=run_attempt,
                conclusion=conclusion,
                payload_path=str(payload_path),
            )
        if event is None:
            raise AssertionError("could not insert fixture event")
        return event

    return _make


def _default_payload() -> dict[str, Any]:
    return json.loads((GITHUB_FIXTURES / "workflow_run_event.json").read_text(encoding="utf-8"))
