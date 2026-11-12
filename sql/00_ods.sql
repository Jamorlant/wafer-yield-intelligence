-- =============================================================================
-- ODS layer - Operational Data Store (raw, immutable landing zone)
-- -----------------------------------------------------------------------------
-- Design notes:
--   * ODS keeps the source grain and source values. Only type normalisation is
--     allowed here (e.g. timestamps to ISO-8601), never business logic.
--   * SECOM is stored in long format (one row per wafer x sensor) because a
--     590-column wide table is unreadable and unmaintainable in SQLite.
--   * A NULL raw_value means "missing in source", which is different from a
--     real measured zero. This distinction drives the quality report.
-- =============================================================================

DROP TABLE IF EXISTS ods_secom_sensor_value;  -- child first: FK -> ods_secom_wafer
DROP TABLE IF EXISTS ods_secom_wafer;
CREATE TABLE ods_secom_wafer (
    wafer_id        TEXT PRIMARY KEY,          -- synthetic key: W00001 .. W01567
    measurement_ts  TEXT NOT NULL,             -- ISO-8601, parsed from dd/mm/yyyy
    label_raw       INTEGER NOT NULL,          -- source label: -1 = pass, 1 = fail
    load_batch_id   TEXT NOT NULL,             -- ingestion batch for lineage
    ingested_at     TEXT NOT NULL
);

CREATE TABLE ods_secom_sensor_value (
    wafer_id  TEXT NOT NULL,
    sensor_id TEXT NOT NULL,                   -- S001 .. S590 (source column order)
    raw_value REAL,                            -- NULL = missing in source
    PRIMARY KEY (wafer_id, sensor_id),
    FOREIGN KEY (wafer_id) REFERENCES ods_secom_wafer (wafer_id)
);
CREATE INDEX idx_ods_sensor_value_sensor ON ods_secom_sensor_value (sensor_id);

DROP TABLE IF EXISTS ods_wm811k_wafer;
CREATE TABLE ods_wm811k_wafer (
    source_index        INTEGER PRIMARY KEY,   -- original LSWMD.pkl row index
    label_code          INTEGER NOT NULL,      -- 0 .. 8
    label_name          TEXT NOT NULL,         -- none / Center / ... / Near-full
    split_original      TEXT NOT NULL,         -- Training / Test (official split)
    rows                INTEGER,
    cols                INTEGER,
    total_grid          REAL,
    total_die           REAL,
    fail_die            REAL,
    wafer_yield         REAL,
    defect_density      REAL,
    center_defect_ratio REAL,
    mid_defect_ratio    REAL,
    edge_defect_ratio   REAL,
    edge_to_center_ratio REAL,
    quadrant_imbalance  REAL,
    left_defect_ratio   REAL,
    right_defect_ratio  REAL,
    top_defect_ratio    REAL,
    bottom_defect_ratio REAL,
    defect_centroid_x   REAL,
    defect_centroid_y   REAL,
    defect_spread_x     REAL,
    defect_spread_y     REAL,
    radial_mean         REAL,
    radial_std          REAL,
    cluster_count       REAL,
    largest_cluster_ratio REAL,
    die_size            REAL,
    wafer_index         REAL
);

DROP TABLE IF EXISTS ods_asset;
CREATE TABLE ods_asset (
    asset_name    TEXT PRIMARY KEY,
    source_url    TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    bytes         INTEGER NOT NULL,
    downloaded_at TEXT NOT NULL
);
