-- =============================================================================
-- MES ODS layer - raw synthetic MES extract (same shape as a real MES/EAP
-- export). 100% SYNTHETIC: see mes.scenarios in configs/config.yaml and
-- artifacts/mes_generation.json. Real fab data is under NDA.
-- -----------------------------------------------------------------------------
-- Grain:
--   ods_mes_lot      one row per production lot
--   ods_mes_process  one row per lot x process step (486,000 rows shipped)
--   ods_mes_scenario ground truth of injected anomalies (never cleaned away)
-- =============================================================================

DROP TABLE IF EXISTS ods_mes_process;  -- child first (FK -> ods_mes_lot)
DROP TABLE IF EXISTS ods_mes_lot;
CREATE TABLE ods_mes_lot (
    lot_id        TEXT PRIMARY KEY,        -- e.g. B20261109N001
    line_code     TEXT NOT NULL,
    product_code  TEXT NOT NULL,
    start_shift   TEXT NOT NULL,           -- DAY / SWING / NIGHT
    lot_start_ts  TEXT NOT NULL,           -- ISO-8601
    lot_size      INTEGER NOT NULL,
    good_qty      INTEGER NOT NULL,
    yield_final   REAL NOT NULL
);

CREATE TABLE ods_mes_process (
    lot_id        TEXT NOT NULL,
    line_code     TEXT NOT NULL,
    product_code  TEXT NOT NULL,
    process_seq   INTEGER NOT NULL,
    process_code  TEXT NOT NULL,
    equipment_id  TEXT NOT NULL,
    process_ts    TEXT NOT NULL,
    shift_code    TEXT NOT NULL,
    input_qty     INTEGER NOT NULL,
    output_qty    INTEGER NOT NULL,
    defect_qty    INTEGER NOT NULL,
    defect_code   TEXT NOT NULL,
    scenario_id   TEXT NOT NULL DEFAULT '', -- ground truth, empty = normal
    param_1       REAL,
    param_2       REAL,
    param_3       REAL,
    param_4       REAL,
    param_5       REAL,
    PRIMARY KEY (lot_id, process_seq)
);
CREATE INDEX idx_ods_mes_process_ts ON ods_mes_process (process_ts);
CREATE INDEX idx_ods_mes_process_equip ON ods_mes_process (equipment_id, process_ts);

DROP TABLE IF EXISTS ods_mes_scenario;
CREATE TABLE ods_mes_scenario (
    scenario_id    TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    line_code      TEXT NOT NULL,
    process_code   TEXT NOT NULL,
    equipment_id   TEXT NOT NULL,
    param_code     TEXT NOT NULL,
    param_name     TEXT NOT NULL,
    unit           TEXT NOT NULL,
    start_ts       TEXT NOT NULL,
    end_ts         TEXT NOT NULL,
    ramp_hours     REAL NOT NULL,
    magnitude_sigma REAL NOT NULL,
    magnitude_value REAL NOT NULL,
    yield_penalty_pp REAL NOT NULL,
    yield_lag_hours REAL NOT NULL,
    defect_code    TEXT NOT NULL,
    owner_role     TEXT NOT NULL,
    expected_rule  TEXT NOT NULL,
    step_row_cnt   INTEGER NOT NULL,
    lot_cnt        INTEGER NOT NULL
);

DROP TABLE IF EXISTS ods_mes_asset;
CREATE TABLE ods_mes_asset (
    asset_name  TEXT PRIMARY KEY,
    source      TEXT NOT NULL,             -- synthetic | mes_export
    seed        INTEGER,
    row_cnt     INTEGER NOT NULL,
    digest      TEXT NOT NULL,
    generated_at TEXT NOT NULL
);
