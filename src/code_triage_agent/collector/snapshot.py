"""Write and read the per-run input snapshot.

Everything needed to replay a triage without calling GitHub lives here:

    var/snapshots/<run_id>/
        metadata.json
        logs.txt
        diff.patch
        memory.json
        signature.txt
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from code_triage_agent.config.schema import MemoryNote
from code_triage_agent.models import SnapshotMetadata

_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class SnapshotError(RuntimeError):
    """Raised when a snapshot cannot be written or read safely."""


@dataclass(frozen=True)
class SnapshotPaths:
    directory: Path
    metadata: Path
    logs: Path
    diff: Path
    memory: Path
    signature: Path


@dataclass(frozen=True)
class LoadedSnapshot:
    directory: Path
    metadata: SnapshotMetadata
    logs: str
    diff: str
    memory: list[MemoryNote]
    signature: str


def write_snapshot(
    root: Path,
    run_id: str,
    *,
    metadata: SnapshotMetadata,
    logs: str,
    diff: str | None,
    memory_notes: Sequence[MemoryNote],
    signature: str,
) -> SnapshotPaths:
    if not _RUN_ID.fullmatch(run_id):
        raise SnapshotError(f"unsafe run id: {run_id!r}")

    directory = root / run_id
    directory.mkdir(parents=True, exist_ok=True)

    paths = SnapshotPaths(
        directory=directory,
        metadata=directory / "metadata.json",
        logs=directory / "logs.txt",
        diff=directory / "diff.patch",
        memory=directory / "memory.json",
        signature=directory / "signature.txt",
    )

    metadata_json = json.dumps(metadata.model_dump(mode="json"), indent=2, sort_keys=True)
    memory_json = json.dumps(
        [note.model_dump(mode="json") for note in memory_notes],
        indent=2,
        sort_keys=True,
    )

    _atomic_write(paths.metadata, (metadata_json + "\n").encode("utf-8"))
    _atomic_write(paths.logs, logs.encode("utf-8"))
    _atomic_write(paths.diff, (diff or "").encode("utf-8"))
    _atomic_write(paths.memory, (memory_json + "\n").encode("utf-8"))
    _atomic_write(paths.signature, (signature + "\n").encode("utf-8"))
    return paths


def load_snapshot(directory: Path) -> LoadedSnapshot:
    directory = directory.resolve()
    metadata_path = directory / "metadata.json"
    if not metadata_path.is_file():
        raise SnapshotError(f"snapshot metadata not found: {metadata_path}")

    metadata = SnapshotMetadata.model_validate_json(metadata_path.read_text(encoding="utf-8"))
    logs = _read_optional(directory / "logs.txt")
    diff = _read_optional(directory / "diff.patch")
    signature = _read_optional(directory / "signature.txt").strip()

    memory: list[MemoryNote] = []
    memory_path = directory / "memory.json"
    if memory_path.is_file():
        raw = json.loads(memory_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise SnapshotError(f"snapshot memory must be a list: {memory_path}")
        memory = [MemoryNote.model_validate(item) for item in raw]

    return LoadedSnapshot(
        directory=directory,
        metadata=metadata,
        logs=logs,
        diff=diff,
        memory=memory,
        signature=signature,
    )


def _read_optional(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
