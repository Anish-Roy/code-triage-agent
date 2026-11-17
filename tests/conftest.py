"""Shared pytest fixtures and helpers."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from code_triage_agent.collector.snapshot import LoadedSnapshot, load_snapshot, write_snapshot
from code_triage_agent.config.loader import LoadedConfig, load_config
from code_triage_agent.config.schema import MemoryNote
from code_triage_agent.db import Database, insert_event
from code_triage_agent.github.webhook import write_event_payload
from code_triage_agent.models import EventRecord, SnapshotMetadata
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


@pytest.fixture
def make_snapshot(tmp_path: Path) -> Callable[..., LoadedSnapshot]:
    """Write a snapshot in the collector's on-disk format and load it back."""

    def _make(
        *,
        run_id: str = "r_test",
        source: str = "pull_request",
        conclusion: str = "failure",
        pr_number: int | None = 7,
        signature: str = "sig_v1:test",
        logs: str = "E   AssertionError: expected 200 got 500\n",
        diff: str = "",
        memory: Sequence[MemoryNote] = (),
        metadata: dict[str, Any] | None = None,
        extra_files: dict[str, str] | None = None,
    ) -> LoadedSnapshot:
        values: dict[str, Any] = {
            "run_id": run_id,
            "event_id": 1,
            "delivery_id": "delivery-test",
            "repo": "acme/widgets",
            "workflow_run_id": 9001,
            "run_attempt": 1,
            "workflow_name": "CI",
            "source": source,
            "conclusion": conclusion,
            "head_sha": "abc1234",
            "base_sha": "def5678",
            "pr_number": pr_number,
            "signature": signature,
            "config_sha": "config-sha",
            "collected_at": datetime(2026, 11, 5, 10, 0, tzinfo=UTC),
        }
        if metadata:
            values.update(metadata)
        meta = SnapshotMetadata.model_validate(values)
        root = tmp_path / "snapshots"
        paths = write_snapshot(
            root,
            run_id,
            metadata=meta,
            logs=logs,
            diff=diff,
            memory_notes=memory,
            signature=signature,
        )
        for name, content in (extra_files or {}).items():
            target = paths.directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return load_snapshot(paths.directory)

    return _make
