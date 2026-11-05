"""Load and validate the ``config/`` directory.

Validation is intentionally strict: the webhook server should refuse to start
(or report unhealthy) rather than run with a trigger pointing at a missing skill.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from code_triage_agent.config.schema import (
    KNOWN_ACTIONS,
    KNOWN_TOOLS,
    MemoryNote,
    SkillMeta,
    TriggerConfig,
)

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


class ConfigError(Exception):
    """Raised when the configuration directory is invalid."""


@dataclass(frozen=True)
class LoadedConfig:
    config_dir: Path
    triggers: tuple[TriggerConfig, ...]
    skills: Mapping[str, SkillMeta]
    memory_notes: tuple[MemoryNote, ...]
    config_sha: str


def load_config(config_dir: Path) -> LoadedConfig:
    config_dir = config_dir.resolve()
    if not config_dir.is_dir():
        raise ConfigError(f"config directory not found: {config_dir}")

    errors: list[str] = []
    triggers = _load_triggers(config_dir / "triggers.yaml", errors)
    skills = _load_skills(config_dir / "skills", errors)
    memory_notes = _load_memory(config_dir / "memory", errors)

    for trigger in triggers:
        if trigger.skill not in skills:
            errors.append(
                f"triggers.yaml: trigger {trigger.id!r} references missing skill {trigger.skill!r}"
            )
        for action in trigger.allowed_actions:
            if action not in KNOWN_ACTIONS:
                errors.append(
                    f"triggers.yaml: trigger {trigger.id!r} has unknown action {action!r}"
                )

    if errors:
        raise ConfigError("\n".join(errors))

    return LoadedConfig(
        config_dir=config_dir,
        triggers=tuple(triggers),
        skills=skills,
        memory_notes=tuple(memory_notes),
        config_sha=_hash_config(config_dir),
    )


def _load_triggers(path: Path, errors: list[str]) -> list[TriggerConfig]:
    if not path.is_file():
        errors.append(f"missing {path.name}")
        return []

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        errors.append(f"{path.name}: invalid YAML: {exc}")
        return []

    if not isinstance(raw, list):
        errors.append(f"{path.name}: expected a list of triggers")
        return []

    triggers: list[TriggerConfig] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        try:
            trigger = TriggerConfig.model_validate(item)
        except ValidationError as exc:
            errors.append(f"{path.name}[{index}]: {_format_validation_error(exc)}")
            continue
        if trigger.id in seen:
            errors.append(f"{path.name}: duplicate trigger id {trigger.id!r}")
            continue
        seen.add(trigger.id)
        triggers.append(trigger)
    return triggers


def _load_skills(skills_dir: Path, errors: list[str]) -> dict[str, SkillMeta]:
    if not skills_dir.is_dir():
        errors.append("missing skills/ directory")
        return {}

    skills: dict[str, SkillMeta] = {}
    for path in sorted(skills_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        match = _FRONTMATTER.match(text)
        if match is None:
            errors.append(f"skills/{path.name}: missing YAML frontmatter")
            continue

        try:
            frontmatter = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError as exc:
            errors.append(f"skills/{path.name}: invalid frontmatter: {exc}")
            continue

        if not isinstance(frontmatter, dict):
            errors.append(f"skills/{path.name}: frontmatter must be a mapping")
            continue

        body = match.group(2).strip()
        if not body:
            errors.append(f"skills/{path.name}: skill body is empty")
            continue

        try:
            skill = SkillMeta.model_validate({**frontmatter, "body": body})
        except ValidationError as exc:
            errors.append(f"skills/{path.name}: {_format_validation_error(exc)}")
            continue

        if skill.id != path.stem:
            errors.append(f"skills/{path.name}: id {skill.id!r} must match filename {path.stem!r}")
            continue
        unknown_tools = sorted(set(skill.allowed_tools) - KNOWN_TOOLS)
        if unknown_tools:
            errors.append(f"skills/{path.name}: unknown tools {unknown_tools}")
            continue
        if skill.id in skills:
            errors.append(f"skills/{path.name}: duplicate skill id {skill.id!r}")
            continue
        skills[skill.id] = skill

    if not skills and not errors:
        errors.append("skills/: no skill files found")
    return skills


def _load_memory(memory_dir: Path, errors: list[str]) -> list[MemoryNote]:
    if not memory_dir.is_dir():
        errors.append("missing memory/ directory")
        return []

    notes: list[MemoryNote] = []
    seen: set[str] = set()
    known_failures = memory_dir / "known_failures.jsonl"
    if not known_failures.is_file():
        errors.append("missing memory/known_failures.jsonl")
    else:
        for line_number, line in enumerate(
            known_failures.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                note = MemoryNote.model_validate(json.loads(line))
            except (json.JSONDecodeError, ValidationError) as exc:
                errors.append(
                    f"memory/known_failures.jsonl:{line_number}: {_format_validation_error(exc)}"
                )
                continue
            if note.signature in seen:
                errors.append(
                    f"memory/known_failures.jsonl:{line_number}: duplicate signature "
                    f"{note.signature!r}"
                )
                continue
            seen.add(note.signature)
            notes.append(note)

    if not (memory_dir / "repo_notes.md").is_file():
        errors.append("missing memory/repo_notes.md")

    return notes


def _format_validation_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
    return str(exc)


def _hash_config(config_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(config_dir.rglob("*")):
        if not path.is_file():
            continue
        digest.update(path.relative_to(config_dir).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()
