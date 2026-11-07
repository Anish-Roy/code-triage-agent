from __future__ import annotations

from datetime import UTC, datetime, timedelta

from code_triage_agent.db import Database, claim_next_event, get_event
from code_triage_agent.jobs.worker import Worker
from code_triage_agent.models import EventRecord

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _event(database: Database, make_event) -> EventRecord:  # type: ignore[no-untyped-def]
    return make_event()


def test_successful_processor_marks_event_done(database: Database, make_event) -> None:  # type: ignore[no-untyped-def]
    event = _event(database, make_event)
    processed: list[int] = []
    worker = Worker(
        database=database,
        processor=lambda item: processed.append(item.id),
        clock=lambda: NOW,
    )

    assert worker.run_once() is True
    assert processed == [event.id]

    with database.connection() as connection:
        stored = get_event(connection, event.id)
    assert stored is not None
    assert stored.status == "done"
    assert stored.attempts == 1

    assert worker.run_once() is False


def test_failing_processor_retries_then_dead_letters(database: Database, make_event) -> None:  # type: ignore[no-untyped-def]
    _event(database, make_event)
    clock = Clock(NOW)

    def boom(_: EventRecord) -> None:
        raise RuntimeError("collector exploded")

    worker = Worker(
        database=database,
        processor=boom,
        clock=clock,
        max_attempts=2,
        base_backoff_seconds=10,
    )

    assert worker.run_once() is True
    with database.connection() as connection:
        first = get_event(connection, 1)
    assert first is not None
    assert first.status == "failed"
    assert first.attempts == 1
    assert first.error == "RuntimeError: collector exploded"
    assert first.next_attempt_at == NOW + timedelta(seconds=10)

    assert worker.run_once() is False  # not due yet

    clock.now = NOW + timedelta(seconds=11)
    assert worker.run_once() is True
    with database.connection() as connection:
        second = get_event(connection, 1)
    assert second is not None
    assert second.status == "dead"
    assert second.attempts == 2


def test_worker_recovers_a_stale_claim(database: Database, make_event) -> None:  # type: ignore[no-untyped-def]
    event = _event(database, make_event)
    with database.connection() as connection:
        claimed = claim_next_event(connection, now=NOW, lease_seconds=600)
    assert claimed is not None
    assert claimed.status == "processing"

    processed: list[int] = []
    worker = Worker(
        database=database,
        processor=lambda item: processed.append(item.id),
        clock=lambda: NOW + timedelta(seconds=601),
        lease_seconds=600,
    )

    assert worker.run_once() is True
    assert processed == [event.id]
    with database.connection() as connection:
        stored = get_event(connection, event.id)
    assert stored is not None
    assert stored.status == "done"
    assert stored.attempts == 2
