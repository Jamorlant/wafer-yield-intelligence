-- =============================================================================
-- DWD layer - Data Warehouse Detail (cleaned, conformed, still event grain)
-- -----------------------------------------------------------------------------
-- Cleaning rules applied here (see src/warehouse/build.py for the code):
--   1. Labels are restricted to the documented domain {-1, 1}; duplicates are
--      rejected instead of silently aggregated.
--   2. Timestamps are ISO-8601 and carry a stat_date for daily aggregation.
--   3. Per-sensor missing rate / variance is profiled on the TRAINING period
--      only, so imputation statistics never look into the future.
--   4. Missing sensor values are median-imputed (is_imputed = 1) and values
--      beyond +/- 6 sigma of the training period are winsorised
--      (is_outlier = 1). The original value is not lost: ODS keeps it.
-- =============================================================================

DROP TABLE IF EXISTS dwd_sensor_value;  -- child first (FK -> dwd_wafer_label)
DROP TABLE IF EXISTS dwd_wafer_label;
CREATE TABLE dwd_wafer_label (
    wafer_id       TEXT PRIMARY KEY,
    measurement_ts TEXT NOT NULL,
    stat_date      TEXT NOT NULL,
    is_fail        INTEGER NOT NULL CHECK (is_fail IN (0, 1)),
    load_batch_id  TEXT NOT NULL
);
CREATE INDEX idx_dwd_label_date ON dwd_wafer_label (stat_date);

DROP TABLE IF EXISTS dwd_sensor_profile;
CREATE TABLE dwd_sensor_profile (
    sensor_id       TEXT PRIMARY KEY,
    total_cnt       INTEGER NOT NULL,
    missing_cnt     INTEGER NOT NULL,
    missing_rate    REAL NOT NULL,
    unique_cnt      INTEGER NOT NULL,
    mean_value      REAL,
    std_value       REAL,
    median_value    REAL,
    min_value       REAL,
    max_value       REAL,
    p01             REAL,
    p99             REAL,
    -- flags consumed by feature building and the dashboard
    is_constant     INTEGER NOT NULL,
    is_high_missing INTEGER NOT NULL,
    is_usable       INTEGER NOT NULL
);

CREATE TABLE dwd_sensor_value (
    wafer_id   TEXT NOT NULL,
    sensor_id  TEXT NOT NULL,
    value_clean REAL,                         -- imputed + winsorised value
    is_imputed INTEGER NOT NULL DEFAULT 0,
    is_outlier INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (wafer_id, sensor_id),
    FOREIGN KEY (wafer_id) REFERENCES dwd_wafer_label (wafer_id)
);
CREATE INDEX idx_dwd_sensor_value_sensor ON dwd_sensor_value (sensor_id);
CREATE INDEX idx_dwd_sensor_value_wafer ON dwd_sensor_value (wafer_id);

DROP TABLE IF EXISTS dwd_wm811k_wafer;
CREATE TABLE dwd_wm811k_wafer AS
SELECT
    source_index,
    label_code,
    label_name,
    split_original,
    rows,
    cols,
    total_die,
    fail_die,
    wafer_yield,
    defect_density,
    center_defect_ratio,
    mid_defect_ratio,
    edge_defect_ratio,
    edge_to_center_ratio,
    quadrant_imbalance,
    left_defect_ratio,
    right_defect_ratio,
    top_defect_ratio,
    bottom_defect_ratio,
    defect_centroid_x,
    defect_centroid_y,
    defect_spread_x,
    defect_spread_y,
    radial_mean,
    radial_std,
    cluster_count,
    largest_cluster_ratio,
    die_size,
    wafer_index,
    1 AS is_valid,
    '' AS invalid_reason
FROM ods_wm811k_wafer
WHERE 0;  -- populated by src/warehouse/build.py after row-level validation
