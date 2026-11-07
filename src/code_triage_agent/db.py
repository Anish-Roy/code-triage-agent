"""SQLite persistence: connection helpers, migrations, and repositories.

V1 intentionally uses plain ``sqlite3`` (no ORM). Timestamps are stored as
ISO-8601 UTC strings, which sort correctly and stay debuggable with the
``sqlite3`` CLI.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from code_triage_agent.models import (
    EventRecord,
    RunRecord,
    SignatureRecord,
    StepRecord,
)

SCHEMA_VERSION = 1
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_MIGRATIONS_DIR = Path(__file__).with_name("migrations")
_migration_lock = threading.Lock()


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_iso(value: datetime) -> str:
    """Serialize a datetime as a stable, lexicographically sortable UTC string."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    # sqlite3.Row iterates values, so we must ask for column names explicitly.
    return {key: row[key] for key in row.keys()}  # noqa: SIM118


class Database:
    """Owns the database file and its migrations.

    Connections are cheap and short-lived; open one per unit of work. WAL mode
    lets the read-only health check run while the worker writes.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    def migrate(self) -> int:
        """Apply pending migrations and return the resulting ``user_version``."""
        with _migration_lock, self.connection() as connection:
            current = _user_version(connection)
            for version, path in _migrations():
                if version <= current:
                    continue
                connection.executescript(path.read_text(encoding="utf-8"))
                connection.execute(f"PRAGMA user_version = {version}")
                current = version
            return current


def _user_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def _migrations() -> list[tuple[int, Path]]:
    migrations: list[tuple[int, Path]] = [(1, _SCHEMA_PATH)]
    if _MIGRATIONS_DIR.is_dir():
        for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            prefix = path.name.split("_", 1)[0]
            if not prefix.isdigit():
                raise RuntimeError(f"migration filename must start with a number: {path.name}")
            migrations.append((int(prefix), path))
    migrations.sort(key=lambda item: item[0])
    return migrations


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a ``BEGIN IMMEDIATE`` transaction (a write lock from the start)."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def insert_event(
    connection: sqlite3.Connection,
    *,
    delivery_id: str,
    repo: str,
    workflow_run_id: int,
    run_attempt: int,
    conclusion: str,
    payload_path: str,
    event_name: str = "workflow_run",
    now: datetime | None = None,
) -> EventRecord | None:
    """Insert a new event, returning ``None`` when the dedupe key already exists."""
    timestamp = to_iso(now or utcnow())
    try:
        cursor = connection.execute(
            """
            INSERT INTO events (
                delivery_id, repo, workflow_run_id, run_attempt, conclusion,
                event_name, payload_path, status, attempts, received_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
            """,
            (
                delivery_id,
                repo,
                workflow_run_id,
                run_attempt,
                conclusion,
                event_name,
                payload_path,
                timestamp,
                timestamp,
            ),
        )
    except sqlite3.IntegrityError:
        return None
    event_id = cursor.lastrowid
    if event_id is None:
        return None
    return get_event(connection, event_id)


def get_event(connection: sqlite3.Connection, event_id: int) -> EventRecord | None:
    row = connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return _event_from_row(row) if row is not None else None


def get_event_by_dedupe(
    connection: sqlite3.Connection,
    *,
    repo: str,
    workflow_run_id: int,
    run_attempt: int,
) -> EventRecord | None:
    row = connection.execute(
        """
        SELECT * FROM events
        WHERE repo = ? AND workflow_run_id = ? AND run_attempt = ?
        """,
        (repo, workflow_run_id, run_attempt),
    ).fetchone()
    return _event_from_row(row) if row is not None else None


def list_events(
    connection: sqlite3.Connection,
    *,
    status: str | None = None,
    limit: int = 100,
) -> list[EventRecord]:
    if status is None:
        rows = connection.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT * FROM events WHERE status = ? ORDER BY id DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    return [_event_from_row(row) for row in rows]


def claim_next_event(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
    lease_seconds: int = 600,
) -> EventRecord | None:
    """Atomically claim one due event.

    A ``processing`` row older than ``lease_seconds`` is treated as abandoned
    (worker crash) and reclaimed. The ``attempts`` counter increments on claim.
    """
    timestamp = now or utcnow()
    iso_now = to_iso(timestamp)
    lease_cutoff = to_iso(timestamp - timedelta(seconds=lease_seconds))

    with transaction(connection):
        row = connection.execute(
            """
            SELECT * FROM events
            WHERE (
                    status IN ('pending', 'failed')
                    AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                  )
               OR (status = 'processing' AND claimed_at IS NOT NULL AND claimed_at <= ?)
            ORDER BY id
            LIMIT 1
            """,
            (iso_now, lease_cutoff),
        ).fetchone()
        if row is None:
            return None

        event_id = int(row["id"])
        connection.execute(
            """
            UPDATE events
            SET status = 'processing',
                attempts = attempts + 1,
                claimed_at = ?,
                updated_at = ?,
                error = NULL
            WHERE id = ?
            """,
            (iso_now, iso_now, event_id),
        )
        updated = connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()

    return _event_from_row(updated) if updated is not None else None


def mark_event_done(
    connection: sqlite3.Connection,
    event_id: int,
    *,
    now: datetime | None = None,
) -> None:
    timestamp = to_iso(now or utcnow())
    connection.execute(
        """
        UPDATE events
        SET status = 'done', error = NULL, next_attempt_at = NULL,
            claimed_at = NULL, updated_at = ?
        WHERE id = ?
        """,
        (timestamp, event_id),
    )


def mark_event_failed(
    connection: sqlite3.Connection,
    event_id: int,
    *,
    error: str,
    now: datetime | None = None,
    max_attempts: int = 5,
    base_backoff_seconds: int = 60,
    max_backoff_seconds: int = 3600,
) -> EventRecord | None:
    """Record a failure and schedule a retry, or move the event to ``dead``."""
    timestamp = now or utcnow()
    row = connection.execute("SELECT attempts FROM events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        return None
    attempts = int(row["attempts"])

    if attempts >= max_attempts:
        connection.execute(
            """
            UPDATE events
            SET status = 'dead', error = ?, next_attempt_at = NULL,
                claimed_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (error[:4000], to_iso(timestamp), event_id),
        )
    else:
        backoff = min(base_backoff_seconds * (2 ** max(attempts - 1, 0)), max_backoff_seconds)
        connection.execute(
            """
            UPDATE events
            SET status = 'failed', error = ?, next_attempt_at = ?,
                claimed_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (
                error[:4000],
                to_iso(timestamp + timedelta(seconds=backoff)),
                to_iso(timestamp),
                event_id,
            ),
        )
    return get_event(connection, event_id)


def _event_from_row(row: sqlite3.Row) -> EventRecord:
    data = _row_dict(row)
    return EventRecord.model_validate(
        {
            **data,
            "claimed_at": parse_iso(cast(str | None, data.get("claimed_at"))),
            "next_attempt_at": parse_iso(cast(str | None, data.get("next_attempt_at"))),
        }
    )


# ---------------------------------------------------------------------------
# Runs and steps
# ---------------------------------------------------------------------------


def new_run_id() -> str:
    return f"r_{uuid.uuid4().hex}"


def create_run(
    connection: sqlite3.Connection,
    *,
    event_id: int,
    run_id: str | None = None,
    trigger_id: str | None = None,
    skill_id: str | None = None,
    skill_version: str | None = None,
    signature: str | None = None,
    snapshot_path: str | None = None,
    config_sha: str | None = None,
    status: str = "running",
    now: datetime | None = None,
) -> RunRecord:
    identifier = run_id or new_run_id()
    connection.execute(
        """
        INSERT INTO runs (
            id, event_id, trigger_id, skill_id, skill_version, signature,
            snapshot_path, status, config_sha, started_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            identifier,
            event_id,
            trigger_id,
            skill_id,
            skill_version,
            signature,
            snapshot_path,
            status,
            config_sha,
            to_iso(now or utcnow()),
        ),
    )
    run = get_run(connection, identifier)
    if run is None:  # pragma: no cover - guarded by the insert above
        raise RuntimeError(f"run {identifier} disappeared after insert")
    return run


def finish_run(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    status: str,
    signature: str | None = None,
    snapshot_path: str | None = None,
    category: str | None = None,
    confidence: float | None = None,
    summary: str | None = None,
    actions: Sequence[str] | None = None,
    cost_usd: float | None = None,
    trigger_id: str | None = None,
    skill_id: str | None = None,
    skill_version: str | None = None,
    config_sha: str | None = None,
    now: datetime | None = None,
) -> RunRecord | None:
    connection.execute(
        """
        UPDATE runs
        SET status = ?,
            signature = COALESCE(?, signature),
            snapshot_path = COALESCE(?, snapshot_path),
            category = COALESCE(?, category),
            confidence = COALESCE(?, confidence),
            summary = COALESCE(?, summary),
            actions_json = COALESCE(?, actions_json),
            cost_usd = COALESCE(?, cost_usd),
            trigger_id = COALESCE(?, trigger_id),
            skill_id = COALESCE(?, skill_id),
            skill_version = COALESCE(?, skill_version),
            config_sha = COALESCE(?, config_sha),
            finished_at = ?
        WHERE id = ?
        """,
        (
            status,
            signature,
            snapshot_path,
            category,
            confidence,
            summary,
            json.dumps(list(actions)) if actions is not None else None,
            cost_usd,
            trigger_id,
            skill_id,
            skill_version,
            config_sha,
            to_iso(now or utcnow()),
            run_id,
        ),
    )
    return get_run(connection, run_id)


def get_run(connection: sqlite3.Connection, run_id: str) -> RunRecord | None:
    row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return _run_from_row(row) if row is not None else None


def list_runs(
    connection: sqlite3.Connection,
    *,
    event_id: int | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[RunRecord]:
    clauses: list[str] = []
    parameters: list[Any] = []
    if event_id is not None:
        clauses.append("event_id = ?")
        parameters.append(event_id)
    if status is not None:
        clauses.append("status = ?")
        parameters.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    parameters.append(limit)
    rows = connection.execute(
        f"SELECT * FROM runs {where} ORDER BY started_at DESC LIMIT ?", parameters
    ).fetchall()
    return [_run_from_row(row) for row in rows]


def record_step(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    seq: int,
    tool: str,
    args: dict[str, Any] | None = None,
    result: Any = None,
    is_error: bool = False,
    latency_ms: int | None = None,
    cost_usd: float = 0.0,
    now: datetime | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO steps (
            run_id, seq, tool, args_json, result_json, is_error,
            latency_ms, cost_usd, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            seq,
            tool,
            json.dumps(args or {}, default=str),
            json.dumps(result, default=str),
            1 if is_error else 0,
            latency_ms,
            cost_usd,
            to_iso(now or utcnow()),
        ),
    )


def list_steps(connection: sqlite3.Connection, run_id: str) -> list[StepRecord]:
    rows = connection.execute(
        "SELECT * FROM steps WHERE run_id = ? ORDER BY seq", (run_id,)
    ).fetchall()
    return [_step_from_row(row) for row in rows]


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    data = _row_dict(row)
    actions = json.loads(cast(str, data.pop("actions_json") or "[]"))
    return RunRecord.model_validate(
        {
            **data,
            "actions": actions,
            "finished_at": parse_iso(cast(str | None, data.get("finished_at"))),
        }
    )


def _step_from_row(row: sqlite3.Row) -> StepRecord:
    data = _row_dict(row)
    args = json.loads(cast(str, data.pop("args_json") or "{}"))
    result_raw = data.pop("result_json")
    result = json.loads(result_raw) if result_raw is not None else None
    return StepRecord.model_validate(
        {
            **data,
            "args": args,
            "result": result,
            "is_error": bool(data.get("is_error")),
        }
    )


# ---------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------


def upsert_signature(
    connection: sqlite3.Connection,
    *,
    signature: str,
    exemplar: str,
    job_name: str,
    step_name: str,
    repo: str | None = None,
    now: datetime | None = None,
) -> SignatureRecord:
    timestamp = to_iso(now or utcnow())
    connection.execute(
        """
        INSERT INTO signatures (
            signature, exemplar, job_name, step_name, repo,
            first_seen, last_seen, count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT (signature) DO UPDATE SET
            last_seen = excluded.last_seen,
            count = signatures.count + 1,
            repo = COALESCE(signatures.repo, excluded.repo)
        """,
        (signature, exemplar, job_name, step_name, repo, timestamp, timestamp),
    )
    row = connection.execute(
        "SELECT * FROM signatures WHERE signature = ?", (signature,)
    ).fetchone()
    if row is None:  # pragma: no cover - guarded by the upsert above
        raise RuntimeError(f"signature {signature} disappeared after upsert")
    return _signature_from_row(row)


def get_signature(connection: sqlite3.Connection, signature: str) -> SignatureRecord | None:
    row = connection.execute(
        "SELECT * FROM signatures WHERE signature = ?", (signature,)
    ).fetchone()
    return _signature_from_row(row) if row is not None else None


def _signature_from_row(row: sqlite3.Row) -> SignatureRecord:
    data = _row_dict(row)
    return SignatureRecord.model_validate(
        {
            **data,
            "first_seen": parse_iso(cast(str, data["first_seen"])),
            "last_seen": parse_iso(cast(str, data["last_seen"])),
        }
    )
