-- =============================================================================
-- ADS layer - Application Data Service (model-ready and dashboard-ready)
-- -----------------------------------------------------------------------------
-- ads_yield_feature_matrix is created dynamically by
-- src/warehouse/build.py because its columns are the surviving sensor ids
-- (S001 .. S590). The generated DDL follows this template:
--
--   CREATE TABLE ads_yield_feature_matrix (
--       wafer_id        TEXT PRIMARY KEY,
--       measurement_ts  TEXT NOT NULL,
--       stat_date       TEXT NOT NULL,
--       split           TEXT NOT NULL,   -- train / valid / test (time ordered)
--       is_fail         INTEGER NOT NULL,
--       z_mean          REAL,            -- derived wafer-level signals
--       z_std           REAL,
--       z_max_abs       REAL,
--       abnormal_cnt    INTEGER,
--       <S001> REAL, <S002> REAL, ...    -- one column per usable sensor
--   );
-- =============================================================================

DROP TABLE IF EXISTS ads_training_manifest;
CREATE TABLE ads_training_manifest (
    run_id        TEXT PRIMARY KEY,
    dataset       TEXT NOT NULL,
    row_cnt       INTEGER NOT NULL,
    feature_cnt   INTEGER NOT NULL,
    train_cnt     INTEGER NOT NULL,
    valid_cnt     INTEGER NOT NULL,
    test_cnt      INTEGER NOT NULL,
    train_end     TEXT NOT NULL,
    valid_end     TEXT NOT NULL,
    feature_names TEXT NOT NULL,               -- JSON array
    created_at    TEXT NOT NULL
);

DROP TABLE IF EXISTS ads_wafer_risk;
CREATE TABLE ads_wafer_risk (
    wafer_id        TEXT PRIMARY KEY,
    measurement_ts  TEXT NOT NULL,
    stat_date       TEXT NOT NULL,
    split           TEXT NOT NULL,
    is_fail         INTEGER NOT NULL,
    fail_probability REAL NOT NULL,
    risk_band       TEXT NOT NULL,
    alert_flag      INTEGER NOT NULL,
    top_driver_1    TEXT,
    contribution_1  REAL,
    top_driver_2    TEXT,
    contribution_2  REAL,
    top_driver_3    TEXT,
    contribution_3  REAL
);
CREATE INDEX idx_ads_risk_date ON ads_wafer_risk (stat_date, fail_probability);

DROP TABLE IF EXISTS ads_yield_trend_daily;
CREATE TABLE ads_yield_trend_daily (
    stat_date     TEXT PRIMARY KEY,
    wafer_cnt     INTEGER NOT NULL,
    fail_cnt      INTEGER NOT NULL,
    actual_yield  REAL NOT NULL,
    avg_risk      REAL,
    high_risk_cnt INTEGER
);

DROP TABLE IF EXISTS ads_sensor_drift;
CREATE TABLE ads_sensor_drift (
    sensor_id     TEXT NOT NULL,
    stat_date     TEXT NOT NULL,
    mean_value    REAL,
    z_score       REAL,
    control_upper REAL,
    control_lower REAL,
    is_drift      INTEGER NOT NULL,
    PRIMARY KEY (sensor_id, stat_date)
);

DROP TABLE IF EXISTS ads_defect_class_perf;
CREATE TABLE ads_defect_class_perf (
    label_name TEXT PRIMARY KEY,
    precision  REAL NOT NULL,
    recall     REAL NOT NULL,
    f1         REAL NOT NULL,
    support    INTEGER NOT NULL
);

DROP TABLE IF EXISTS ads_defect_confusion;
CREATE TABLE ads_defect_confusion (
    actual_label    TEXT NOT NULL,
    predicted_label TEXT NOT NULL,
    cnt             INTEGER NOT NULL,
    PRIMARY KEY (actual_label, predicted_label)
);
