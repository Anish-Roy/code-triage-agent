"""Load and validate ``case.json`` records.

``case.json`` is authoritative. The ``evals`` SQLite table is only an index, so
a stale index can never change what a report evaluates.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import ValidationError

from code_triage_agent.models import EvalCase

CASE_FILE = "case.json"
SNAPSHOT_DIR = "snapshot"
FROZEN_SET = "frozen"
_CASE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class CaseError(RuntimeError):
    """Raised when a case is missing, malformed, or unsafe."""


def validate_case_id(case_id: str) -> str:
    """Reject path traversal and IDs that cannot be a directory name."""
    if not _CASE_ID.fullmatch(case_id):
        raise CaseError(f"invalid case_id {case_id!r}: expected [a-z0-9][a-z0-9._-]{{0,63}}")
    if case_id in {".", ".."}:
        raise CaseError(f"invalid case_id {case_id!r}")
    return case_id


def load_case(case_dir: Path, *, project_root: Path | None = None) -> EvalCase:
    case_dir = case_dir.resolve()
    path = case_dir / CASE_FILE
    if not path.is_file():
        raise CaseError(f"case file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CaseError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise CaseError(f"{path}: case.json must be a JSON object")
    try:
        case = EvalCase.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise CaseError(f"{path}: {details}") from exc

    validate_case_id(case.case_id)
    if case.case_id != case_dir.name:
        raise CaseError(
            f"{path}: case_id {case.case_id!r} must match directory name {case_dir.name!r}"
        )
    return case


def case_snapshot_dir(case_dir: Path, case: EvalCase, *, project_root: Path | None = None) -> Path:
    """Resolve ``snapshot_path`` without trusting it to escape the case."""
    case_dir = case_dir.resolve()
    raw = Path(case.snapshot_path)
    candidates: list[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.append(case_dir / raw)
        if project_root is not None:
            candidates.append(project_root / raw)
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_dir():
            return resolved
    raise CaseError(
        f"snapshot not found for case {case.case_id!r}: tried "
        + ", ".join(str(item) for item in candidates)
    )


def list_case_dirs(set_dir: Path) -> list[Path]:
    set_dir = set_dir.resolve()
    if not set_dir.is_dir():
        raise CaseError(f"eval set directory not found: {set_dir}")
    return sorted(
        (path for path in set_dir.iterdir() if path.is_dir() and (path / CASE_FILE).is_file()),
        key=lambda path: path.name,
    )


def load_case_set(
    set_dir: Path, *, project_root: Path | None = None
) -> list[tuple[Path, EvalCase]]:
    return [(path, load_case(path, project_root=project_root)) for path in list_case_dirs(set_dir)]


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def assert_not_frozen(path: Path, frozen_dir: Path) -> None:
    if is_within(path, frozen_dir):
        raise CaseError(
            f"refusing to write inside the frozen eval set: {path} (frozen root {frozen_dir})"
        )


def write_case(case_dir: Path, case: EvalCase, *, overwrite: bool = False) -> Path:
    """Write ``case.json``, refusing to overwrite an existing case by default."""
    validate_case_id(case.case_id)
    if case.case_id != case_dir.name:
        raise CaseError(f"case_id {case.case_id!r} must match directory name {case_dir.name!r}")
    case_dir.mkdir(parents=True, exist_ok=True)
    path = case_dir / CASE_FILE
    if path.exists() and not overwrite:
        raise CaseError(f"refusing to overwrite existing case: {path}")
    path.write_text(
        json.dumps(case.model_dump(mode="json", exclude_none=True), indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return path


__all__ = [
    "CASE_FILE",
    "FROZEN_SET",
    "SNAPSHOT_DIR",
    "CaseError",
    "assert_not_frozen",
    "case_snapshot_dir",
    "is_within",
    "list_case_dirs",
    "load_case",
    "load_case_set",
    "validate_case_id",
    "write_case",
]
