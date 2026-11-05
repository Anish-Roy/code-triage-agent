from __future__ import annotations

import os
from pathlib import Path

import pytest

from code_triage_agent.settings import Settings


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("CTA_"):
            monkeypatch.delenv(key)


def test_defaults(clean_env: None) -> None:
    settings = Settings(_env_file=None)

    assert settings.dry_run is True
    assert settings.replay_mode is False
    assert settings.auto_merge_memory is False
    assert settings.max_steps == 12
    assert settings.max_cost_usd == 0.50
    assert settings.log_level == "INFO"
    assert settings.webhook_configured() is False
    assert settings.github_app_configured() is False


def test_env_prefix_and_coercion(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CTA_MAX_STEPS", "5")
    monkeypatch.setenv("CTA_DRY_RUN", "false")
    monkeypatch.setenv("CTA_LOG_LEVEL", "debug")

    settings = Settings(_env_file=None)

    assert settings.max_steps == 5
    assert settings.dry_run is False
    assert settings.log_level == "DEBUG"


def test_private_key_expands_literal_newlines(clean_env: None) -> None:
    settings = Settings(_env_file=None, github_app_private_key="line1\\nline2")

    assert settings.github_app_private_key is not None
    assert settings.github_app_private_key.get_secret_value() == "line1\nline2"


def test_ensure_runtime_dirs(clean_env: None, tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "db" / "triage.db",
        snapshot_root=tmp_path / "snapshots",
        event_root=tmp_path / "events",
    )

    settings.ensure_runtime_dirs()

    assert settings.database_path.parent.is_dir()
    assert settings.snapshot_root.is_dir()
    assert settings.event_root.is_dir()


def test_github_configured(clean_env: None) -> None:
    settings = Settings(
        _env_file=None,
        github_app_id=1,
        github_app_private_key="key",
        github_installation_id=2,
        github_webhook_secret="secret",
    )

    assert settings.webhook_configured() is True
    assert settings.github_app_configured() is True
