from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from code_triage_agent.collector.snapshot import SnapshotError, load_snapshot, write_snapshot
from code_triage_agent.config.schema import MemoryNote
from code_triage_agent.models import FailedJobInfo, FailedStepInfo, JobLogTrim, SnapshotMetadata

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def _metadata(run_id: str = "r_test") -> SnapshotMetadata:
    return SnapshotMetadata(
        run_id=run_id,
        event_id=1,
        delivery_id="delivery-1",
        repo="acme/widgets",
        workflow_run_id=9001,
        run_attempt=1,
        workflow_name="CI",
        source="pull_request",
        head_sha="abc",
        base_sha="def",
        pr_number=42,
        signature="sig_v1:abc",
        config_sha="a" * 64,
        failed_jobs=[
            FailedJobInfo(
                job_id=5001,
                name="test",
                conclusion="failure",
                failed_steps=[FailedStepInfo(number=2, name="Run pytest", conclusion="failure")],
            )
        ],
        trim=[JobLogTrim(job_id=5001, job_name="test", step_name="Run pytest", kept_lines=4)],
        total_bytes=120,
        redaction_counts={"github_token": 1},
        memory_matches=1,
        diff_available=True,
        collected_at=NOW,
    )


def _note() -> MemoryNote:
    return MemoryNote(
        signature="sig_v1:abc",
        repo="acme/widgets",
        note="Integration timeouts are infra, not flaky.",
        confidence="high",
        evidence_runs=["r_123"],
        created_at=NOW,
        source="human",
    )


def test_write_and_load_roundtrip(tmp_path: Path) -> None:
    paths = write_snapshot(
        tmp_path,
        "r_test",
        metadata=_metadata(),
        logs="##[error]boom\n",
        diff="diff --git a/x b/x\n",
        memory_notes=[_note()],
        signature="sig_v1:abc",
    )

    assert paths.directory == tmp_path / "r_test"
    assert {path.name for path in paths.directory.iterdir()} == {
        "metadata.json",
        "logs.txt",
        "diff.patch",
        "memory.json",
        "signature.txt",
    }

    loaded = load_snapshot(paths.directory)
    assert loaded.metadata.run_id == "r_test"
    assert loaded.metadata.pr_number == 42
    assert loaded.metadata.failed_jobs[0].failed_steps[0].name == "Run pytest"
    assert loaded.metadata.collected_at == NOW
    assert loaded.logs.startswith("##[error]")
    assert loaded.diff.startswith("diff --git")
    assert loaded.signature == "sig_v1:abc"
    assert len(loaded.memory) == 1
    assert loaded.memory[0].note.startswith("Integration timeouts")


def test_missing_diff_is_written_empty(tmp_path: Path) -> None:
    paths = write_snapshot(
        tmp_path,
        "r_nodiff",
        metadata=_metadata("r_nodiff"),
        logs="",
        diff=None,
        memory_notes=[],
        signature="sig_v1:none",
    )

    assert paths.diff.read_text(encoding="utf-8") == ""
    assert load_snapshot(paths.directory).diff == ""


@pytest.mark.parametrize("run_id", ["../escape", "a/b", "with space", ""])
def test_unsafe_run_id_is_rejected(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(SnapshotError, match="unsafe run id"):
        write_snapshot(
            tmp_path,
            run_id,
            metadata=_metadata(run_id or "x"),
            logs="",
            diff=None,
            memory_notes=[],
            signature="sig",
        )


def test_load_missing_snapshot_raises(tmp_path: Path) -> None:
    with pytest.raises(SnapshotError, match="metadata not found"):
        load_snapshot(tmp_path / "does-not-exist")
