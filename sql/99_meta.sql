-- Pipeline metadata: idempotency + resumability + quality audit trail.
-- These tables are append-only across runs, hence IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS meta_pipeline_run (
    run_id       TEXT PRIMARY KEY,
    fingerprint  TEXT NOT NULL,
    status       TEXT NOT NULL,               -- running / success / failed
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    git_commit   TEXT
);

CREATE TABLE IF NOT EXISTS meta_pipeline_step (
    run_id      TEXT NOT NULL,
    step        TEXT NOT NULL,
    status      TEXT NOT NULL,                -- running / success / failed
    row_cnt     INTEGER,
    message     TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    PRIMARY KEY (run_id, step)
);

CREATE TABLE IF NOT EXISTS meta_quality_check (
    run_id     TEXT NOT NULL,
    check_name TEXT NOT NULL,
    status     TEXT NOT NULL,                 -- pass / warn / fail
    observed   TEXT,
    expected   TEXT,
    details    TEXT,
    checked_at TEXT NOT NULL,
    PRIMARY KEY (run_id, check_name)
);
