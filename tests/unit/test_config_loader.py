from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from code_triage_agent.config import ConfigError, load_config

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROJECT_CONFIG = PROJECT_ROOT / "config"

VALID_TRIGGERS = """\
- id: demo
  when:
    event: workflow_run
    conclusion: [failure]
    source: pull_request
  skill: demo
  allowed_actions: [comment_on_pr]
"""

VALID_SKILL = """\
---
id: demo
version: 1
allowed_tools: [read_file, comment_on_pr]
output_schema: submit_triage
---
# Goal

Test skill.
"""


def _write_config(
    root: Path,
    *,
    triggers: str = VALID_TRIGGERS,
    skill: str = VALID_SKILL,
    memory: str = "",
) -> Path:
    (root / "skills").mkdir(parents=True)
    (root / "memory").mkdir()
    (root / "triggers.yaml").write_text(triggers, encoding="utf-8")
    (root / "skills" / "demo.md").write_text(skill, encoding="utf-8")
    (root / "memory" / "known_failures.jsonl").write_text(memory, encoding="utf-8")
    (root / "memory" / "repo_notes.md").write_text("# Notes\n", encoding="utf-8")
    return root


def test_loads_project_config() -> None:
    config = load_config(PROJECT_CONFIG)

    assert [trigger.id for trigger in config.triggers] == [
        "pr_failure",
        "main_failure",
        "nightly_failure",
    ]
    assert set(config.skills) == {
        "triage-pr-failure",
        "find-culprit",
        "triage-nightly",
        "propose-fix",
    }
    assert config.memory_notes == ()
    assert len(config.config_sha) == 64


def test_loads_valid_minimal_config(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path / "config"))

    assert len(config.triggers) == 1
    assert config.skills["demo"].version == 1


def test_config_sha_is_stable_and_changes_with_content(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree(PROJECT_CONFIG, config_dir)

    first = load_config(config_dir).config_sha
    second = load_config(config_dir).config_sha
    assert first == second

    notes = config_dir / "memory" / "repo_notes.md"
    notes.write_text(notes.read_text(encoding="utf-8") + "\n- New note\n", encoding="utf-8")
    assert load_config(config_dir).config_sha != first


def test_rejects_missing_config_dir(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config directory not found"):
        load_config(tmp_path / "does-not-exist")


def test_rejects_unknown_skill(tmp_path: Path) -> None:
    triggers = VALID_TRIGGERS.replace("skill: demo", "skill: missing")
    config_dir = _write_config(tmp_path / "config", triggers=triggers)

    with pytest.raises(ConfigError, match="references missing skill"):
        load_config(config_dir)


def test_rejects_unknown_action(tmp_path: Path) -> None:
    triggers = VALID_TRIGGERS.replace(
        "allowed_actions: [comment_on_pr]", "allowed_actions: [delete_repo]"
    )
    config_dir = _write_config(tmp_path / "config", triggers=triggers)

    with pytest.raises(ConfigError, match="unknown action"):
        load_config(config_dir)


def test_rejects_unknown_tool(tmp_path: Path) -> None:
    skill = VALID_SKILL.replace(
        "allowed_tools: [read_file, comment_on_pr]", "allowed_tools: [shell]"
    )
    config_dir = _write_config(tmp_path / "config", skill=skill)

    with pytest.raises(ConfigError, match="unknown tools"):
        load_config(config_dir)


def test_rejects_duplicate_trigger(tmp_path: Path) -> None:
    triggers = VALID_TRIGGERS + VALID_TRIGGERS
    config_dir = _write_config(tmp_path / "config", triggers=triggers)

    with pytest.raises(ConfigError, match="duplicate trigger id"):
        load_config(config_dir)


def test_rejects_malformed_memory(tmp_path: Path) -> None:
    config_dir = _write_config(tmp_path / "config", memory="{not json}\n")

    with pytest.raises(ConfigError, match=r"known_failures\.jsonl"):
        load_config(config_dir)


def test_rejects_skill_id_filename_mismatch(tmp_path: Path) -> None:
    skill = VALID_SKILL.replace("id: demo", "id: other")
    config_dir = _write_config(tmp_path / "config", skill=skill)

    with pytest.raises(ConfigError, match="must match filename"):
        load_config(config_dir)
