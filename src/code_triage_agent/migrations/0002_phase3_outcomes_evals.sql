-- Schema version 2: Phase 3 outcomes and evals.
--
-- Additive only. The released schema.sql stays untouched; every change here is
-- replayed by code_triage_agent.db.Database.migrate() for existing databases.

-- Make the accepted decision and the recommended action queryable without
-- parsing the submit_triage step JSON on every report.
ALTER TABLE runs ADD COLUMN predicted_action TEXT;
ALTER TABLE runs ADD COLUMN decision_json TEXT;
ALTER TABLE runs ADD COLUMN outcome TEXT;
ALTER TABLE runs ADD COLUMN outcome_checked_at TEXT;

CREATE INDEX IF NOT EXISTS idx_runs_outcome ON runs (outcome);
CREATE INDEX IF NOT EXISTS idx_runs_predicted_action ON runs (predicted_action);

-- An outcome row is only written once a usable signal exists. Polling
-- bookkeeping therefore lives in its own table so a run can be checked without
-- inventing a label.
CREATE TABLE IF NOT EXISTS outcome_checks (
    run_id           TEXT PRIMARY KEY REFERENCES runs (id) ON DELETE CASCADE,
    attempts         INTEGER NOT NULL DEFAULT 0,
    first_checked_at TEXT,
    last_checked_at  TEXT,
    next_check_at    TEXT,
    terminal         INTEGER NOT NULL DEFAULT 0,
    error            TEXT
);

CREATE INDEX IF NOT EXISTS idx_outcome_checks_due
    ON outcome_checks (terminal, next_check_at);

ALTER TABLE outcomes ADD COLUMN updated_at TEXT;
ALTER TABLE outcomes ADD COLUMN terminal INTEGER NOT NULL DEFAULT 0;

-- case.json remains authoritative, but the table is a useful index for reports
-- and for checking whether an imported case already exists.
ALTER TABLE evals ADD COLUMN labeler TEXT;
ALTER TABLE evals ADD COLUMN reviewer TEXT;
ALTER TABLE evals ADD COLUMN created_at TEXT;
ALTER TABLE evals ADD COLUMN updated_at TEXT;
