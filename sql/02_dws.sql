-- =============================================================================
-- DWS layer - Data Warehouse Service (subject-oriented aggregates)
-- -----------------------------------------------------------------------------
-- Two grains are served here:
--   * wafer grain  -> dws_wafer_summary (row-level quality signals)
--   * sensor x day -> dws_sensor_daily  (drift monitoring / SPC baseline)
-- =============================================================================

DROP TABLE IF EXISTS dws_wafer_summary;
CREATE TABLE dws_wafer_summary (
    wafer_id       TEXT PRIMARY KEY,
    measurement_ts TEXT NOT NULL,
    stat_date      TEXT NOT NULL,
    is_fail        INTEGER NOT NULL,
    sensor_cnt     INTEGER NOT NULL,
    missing_cnt    INTEGER NOT NULL,
    imputed_cnt    INTEGER NOT NULL,
    outlier_cnt    INTEGER NOT NULL,
    missing_rate   REAL NOT NULL,
    abnormal_rate  REAL NOT NULL
);

DROP TABLE IF EXISTS dws_sensor_daily;
CREATE TABLE dws_sensor_daily (
    sensor_id   TEXT NOT NULL,
    stat_date   TEXT NOT NULL,
    wafer_cnt   INTEGER NOT NULL,
    mean_value  REAL,
    std_value   REAL,
    p05         REAL,
    p95         REAL,
    missing_rate REAL,
    PRIMARY KEY (sensor_id, stat_date)
);

DROP TABLE IF EXISTS dws_defect_class_dist;
CREATE TABLE dws_defect_class_dist (
    label_name TEXT PRIMARY KEY,
    label_code INTEGER NOT NULL,
    cnt        INTEGER NOT NULL,
    share      REAL NOT NULL
);

DROP TABLE IF EXISTS dws_wm811k_split;
CREATE TABLE dws_wm811k_split (
    split_original TEXT NOT NULL,
    label_name     TEXT NOT NULL,
    cnt            INTEGER NOT NULL,
    PRIMARY KEY (split_original, label_name)
);
