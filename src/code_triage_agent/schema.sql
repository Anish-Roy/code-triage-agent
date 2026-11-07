-- Schema version 1.
--
-- Applied by code_triage_agent.db.Database.migrate() when PRAGMA user_version
-- is below 1. Later schema changes belong in src/code_triage_agent/migrations/
-- as NNNN_description.sql files.

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id     TEXT NOT NULL UNIQUE,
    repo            TEXT NOT NULL,
    workflow_run_id INTEGER NOT NULL,
    run_attempt     INTEGER NOT NULL,
    conclusion      TEXT NOT NULL,
    event_name      TEXT NOT NULL DEFAULT 'workflow_run',
    payload_path    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    claimed_at      TEXT,
    next_attempt_at TEXT,
    received_at     TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (repo, workflow_run_id, run_attempt)
);

CREATE INDEX IF NOT EXISTS idx_events_claimable
    ON events (status, next_attempt_at, id);

CREATE TABLE IF NOT EXISTS runs (
    id             TEXT PRIMARY KEY,
    event_id       INTEGER NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    trigger_id     TEXT,
    skill_id       TEXT,
    skill_version  TEXT,
    signature      TEXT,
    snapshot_path  TEXT,
    category       TEXT,
    confidence     REAL,
    summary        TEXT,
    actions_json   TEXT NOT NULL DEFAULT '[]',
    cost_usd       REAL NOT NULL DEFAULT 0,
    status         TEXT NOT NULL,
    config_sha     TEXT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_signature ON runs (signature);
CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs (started_at);

CREATE TABLE IF NOT EXISTS steps (
    run_id     TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    tool       TEXT NOT NULL,
    args_json  TEXT NOT NULL DEFAULT '{}',
    result_json TEXT,
    is_error   INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    cost_usd   REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE IF NOT EXISTS outcomes (
    run_id        TEXT PRIMARY KEY REFERENCES runs (id) ON DELETE CASCADE,
    label         TEXT NOT NULL,
    signals_json  TEXT NOT NULL DEFAULT '{}',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    collected_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outcomes_label ON outcomes (label);

CREATE TABLE IF NOT EXISTS signatures (
    signature  TEXT PRIMARY KEY,
    exemplar   TEXT NOT NULL,
    job_name   TEXT NOT NULL,
    step_name  TEXT NOT NULL,
    repo       TEXT,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    count      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS proposals (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    kind                  TEXT NOT NULL,
    base_sha              TEXT NOT NULL,
    patch                 TEXT NOT NULL,
    supporting_runs_json  TEXT NOT NULL DEFAULT '[]',
    eval_before_json      TEXT NOT NULL DEFAULT '{}',
    eval_after_json       TEXT NOT NULL DEFAULT '{}',
    status                TEXT NOT NULL,
    reason                TEXT,
    pr_url                TEXT,
    created_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config_versions (
    sha        TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    source     TEXT NOT NULL,
    note       TEXT
);

CREATE TABLE IF NOT EXISTS evals (
    case_id           TEXT PRIMARY KEY,
    set_name          TEXT NOT NULL,
    snapshot_path     TEXT NOT NULL,
    expected_category TEXT,
    expected_action   TEXT,
    notes             TEXT
);

CREATE INDEX IF NOT EXISTS idx_evals_set_name ON evals (set_name);
