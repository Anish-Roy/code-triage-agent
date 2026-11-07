from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

from code_triage_agent.db import (
    Database,
    claim_next_event,
    create_run,
    finish_run,
    get_run,
    get_signature,
    insert_event,
    list_events,
    list_steps,
    mark_event_done,
    mark_event_failed,
    record_step,
    upsert_signature,
)
from code_triage_agent.models import EventRecord

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def _event(database: Database, delivery_id: str = "delivery-1", attempt: int = 1) -> EventRecord:
    with database.connection() as connection:
        event = insert_event(
            connection,
            delivery_id=delivery_id,
            repo="acme/widgets",
            workflow_run_id=9001,
            run_attempt=attempt,
            conclusion="failure",
            payload_path=f"var/events/{delivery_id}/payload.json",
            now=NOW,
        )
    assert event is not None
    return event


def test_migration_creates_schema(tmp_path: Path) -> None:
    database = Database(tmp_path / "triage.db")

    assert database.migrate() == 1
    assert database.migrate() == 1

    with database.connection() as connection:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {"events", "runs", "steps", "signatures", "outcomes", "proposals"} <= tables


def test_insert_event_dedupes_both_keys(database: Database) -> None:
    first = _event(database)

    with database.connection() as connection:
        duplicate_delivery = insert_event(
            connection,
            delivery_id="delivery-1",
            repo="acme/widgets",
            workflow_run_id=9999,
            run_attempt=1,
            conclusion="failure",
            payload_path="elsewhere.json",
            now=NOW,
        )
        duplicate_run = insert_event(
            connection,
            delivery_id="delivery-2",
            repo="acme/widgets",
            workflow_run_id=9001,
            run_attempt=1,
            conclusion="failure",
            payload_path="elsewhere.json",
            now=NOW,
        )

    assert first.id > 0
    assert duplicate_delivery is None
    assert duplicate_run is None


def test_claim_is_atomic_across_connections(database: Database) -> None:
    _event(database)

    def claim(barrier: Barrier) -> EventRecord | None:
        barrier.wait()
        with database.connection() as connection:
            return claim_next_event(connection, now=NOW)

    barrier = Barrier(8)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: claim(barrier), range(8)))

    claimed = [event for event in results if event is not None]
    assert len(claimed) == 1
    assert claimed[0].attempts == 1


def test_claim_skips_events_that_are_not_due(database: Database) -> None:
    _event(database)
    with database.connection() as connection:
        assert claim_next_event(connection, now=NOW) is not None
        mark_event_failed(
            connection,
            1,
            error="boom",
            now=NOW,
            max_attempts=5,
            base_backoff_seconds=60,
        )
        assert claim_next_event(connection, now=NOW + timedelta(seconds=30)) is None
        assert claim_next_event(connection, now=NOW + timedelta(seconds=61)) is not None


def test_failed_events_back_off_then_die(database: Database) -> None:
    _event(database)
    with database.connection() as connection:
        claim_next_event(connection, now=NOW)
        failed = mark_event_failed(
            connection,
            connection.execute("SELECT id FROM events").fetchone()[0],
            error="boom",
            now=NOW,
            max_attempts=2,
            base_backoff_seconds=10,
        )
        assert failed is not None
        assert failed.status == "failed"
        assert failed.next_attempt_at == NOW + timedelta(seconds=10)

    with database.connection() as connection:
        claim_next_event(connection, now=NOW + timedelta(seconds=11))
        dead = mark_event_failed(
            connection,
            connection.execute("SELECT id FROM events").fetchone()[0],
            error="still broken",
            now=NOW + timedelta(seconds=11),
            max_attempts=2,
        )
        assert dead is not None
        assert dead.status == "dead"
        assert dead.next_attempt_at is None
        assert dead.error == "still broken"
        assert list_events(connection, status="dead")[0].id == dead.id


def test_claim_reclaims_stale_processing_rows(database: Database) -> None:
    _event(database)
    with database.connection() as connection:
        first = claim_next_event(connection, now=NOW, lease_seconds=600)
        assert first is not None
        assert first.status == "processing"
        assert first.attempts == 1

    with database.connection() as connection:
        assert claim_next_event(connection, now=NOW + timedelta(seconds=599)) is None
        recovered = claim_next_event(connection, now=NOW + timedelta(seconds=601))
        assert recovered is not None
        assert recovered.attempts == 2


def test_event_lifecycle_done(database: Database) -> None:
    event = _event(database)
    with database.connection() as connection:
        claim_next_event(connection, now=NOW)
        mark_event_done(connection, event.id, now=NOW)
        row = connection.execute("SELECT * FROM events WHERE id = ?", (event.id,)).fetchone()

    assert row is not None
    assert row["status"] == "done"
    assert row["claimed_at"] is None


def test_run_and_step_roundtrip(database: Database) -> None:
    event = _event(database)
    with database.connection() as connection:
        run = create_run(
            connection,
            event_id=event.id,
            trigger_id="pr_failure",
            config_sha="a" * 64,
            now=NOW,
        )
        record_step(
            connection,
            run_id=run.id,
            seq=0,
            tool="git_diff",
            args={"base": "main"},
            result={"lines": 3},
            latency_ms=12,
            cost_usd=0.01,
            now=NOW,
        )
        finished = finish_run(
            connection,
            run.id,
            status="collected",
            signature="sig_v1:abc",
            snapshot_path="var/snapshots/x",
            actions=["comment_on_pr"],
            now=NOW,
        )
        steps = list_steps(connection, run.id)
        reloaded = get_run(connection, run.id)

    assert run.id.startswith("r_")
    assert finished is not None
    assert finished.status == "collected"
    assert finished.signature == "sig_v1:abc"
    assert finished.actions == ["comment_on_pr"]
    assert reloaded is not None and reloaded.snapshot_path == "var/snapshots/x"
    assert len(steps) == 1
    assert steps[0].tool == "git_diff"
    assert steps[0].args == {"base": "main"}
    assert steps[0].result == {"lines": 3}


def test_signature_upsert_increments_count_and_keeps_exemplar(database: Database) -> None:
    with database.connection() as connection:
        first = upsert_signature(
            connection,
            signature="sig_v1:abc",
            exemplar="first exemplar",
            job_name="test",
            step_name="pytest",
            repo="acme/widgets",
            now=NOW,
        )
        second = upsert_signature(
            connection,
            signature="sig_v1:abc",
            exemplar="second exemplar",
            job_name="test",
            step_name="pytest",
            repo="acme/widgets",
            now=NOW + timedelta(hours=1),
        )
        loaded = get_signature(connection, "sig_v1:abc")

    assert first.count == 1
    assert second.count == 2
    assert loaded is not None
    assert loaded.exemplar == "first exemplar"
    assert loaded.first_seen == NOW
    assert loaded.last_seen == NOW + timedelta(hours=1)
