-- =============================================================================
-- MES ADS layer - dashboard-ready tables.
-- -----------------------------------------------------------------------------
-- ads_mes_kpi_daily      line x day KPI with a 7-day rolling baseline
--                        (window function, no look-ahead in the comparison)
-- ads_mes_spc            hourly parameter series with frozen control limits
-- ads_mes_alert          alerts raised by the SPC rules
-- ads_mes_suspect_lot    lots processed by an alerted tool before the alert
-- ads_mes_scenario_check injected-anomaly vs detected-alert verification
-- ads_mes_loss_pareto    loss contribution ranking per day
-- =============================================================================

DROP TABLE IF EXISTS ads_mes_kpi_daily;
CREATE TABLE ads_mes_kpi_daily (
    line_code    TEXT NOT NULL,
    stat_date    TEXT NOT NULL,
    lot_cnt      INTEGER NOT NULL,
    input_qty    INTEGER NOT NULL,
    good_qty     INTEGER NOT NULL,
    yield_final  REAL NOT NULL,
    yield_7d_avg REAL,
    delta_pp     REAL,
    alert_cnt    INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'normal',   -- normal / watch / alert
    PRIMARY KEY (line_code, stat_date)
);

DROP TABLE IF EXISTS ads_mes_spc;
CREATE TABLE ads_mes_spc (
    line_code    TEXT NOT NULL,
    process_code TEXT NOT NULL,
    equipment_id TEXT NOT NULL,
    param_index  INTEGER NOT NULL,
    param_code   TEXT NOT NULL,
    param_name   TEXT NOT NULL,
    unit         TEXT NOT NULL,
    stat_hour    TEXT NOT NULL,
    sample_cnt   INTEGER NOT NULL,
    mean_value   REAL,
    base_mean    REAL,
    base_sd      REAL,
    z_score      REAL,
    ucl          REAL,
    lcl          REAL,
    point_breach INTEGER NOT NULL,
    scenario_id  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (line_code, process_code, equipment_id, param_index, stat_hour)
);

DROP TABLE IF EXISTS ads_mes_alert;
CREATE TABLE ads_mes_alert (
    alert_id      TEXT PRIMARY KEY,
    alert_ts      TEXT NOT NULL,
    line_code     TEXT NOT NULL,
    process_code  TEXT NOT NULL,
    equipment_id  TEXT NOT NULL,
    param_index   INTEGER NOT NULL,
    param_code    TEXT NOT NULL,
    param_name    TEXT NOT NULL,
    unit          TEXT NOT NULL,
    rule_code     TEXT NOT NULL,
    severity      TEXT NOT NULL,
    z_score       REAL,
    observed_value REAL,
    base_mean     REAL,
    ucl           REAL,
    lcl           REAL,
    evidence      TEXT NOT NULL,
    suggested_action TEXT NOT NULL,
    owner_role    TEXT NOT NULL,
    scenario_id   TEXT NOT NULL DEFAULT ''
);

DROP TABLE IF EXISTS ads_mes_suspect_lot;
CREATE TABLE ads_mes_suspect_lot (
    alert_id     TEXT NOT NULL,
    lot_id       TEXT NOT NULL,
    product_code TEXT NOT NULL,
    process_ts   TEXT NOT NULL,
    shift_code   TEXT NOT NULL,
    input_qty    INTEGER NOT NULL,
    defect_qty   INTEGER NOT NULL,
    step_yield   REAL NOT NULL,
    suggested_action TEXT NOT NULL,
    PRIMARY KEY (alert_id, lot_id)
);

DROP TABLE IF EXISTS ads_mes_scenario_check;
CREATE TABLE ads_mes_scenario_check (
    scenario_id        TEXT PRIMARY KEY,
    expected_rule      TEXT NOT NULL,
    detected_rule      TEXT,
    detected_ts        TEXT,
    detection_lag_hours REAL,
    first_breach_ts    TEXT,
    first_step_drop_ts TEXT,
    step_yield_base    REAL,
    step_yield_window  REAL,
    step_drop_pp       REAL,
    final_yield_base   REAL,
    final_yield_window REAL,
    final_drop_pp      REAL,
    verdict            TEXT NOT NULL,
    detail             TEXT NOT NULL
);

DROP TABLE IF EXISTS ads_mes_loss_pareto;
CREATE TABLE ads_mes_loss_pareto (
    stat_date    TEXT NOT NULL,
    process_code TEXT NOT NULL,
    defect_code  TEXT NOT NULL,
    defect_qty   INTEGER NOT NULL,
    loss_rank    INTEGER NOT NULL,
    PRIMARY KEY (stat_date, process_code, defect_code)
);

INSERT INTO ads_mes_kpi_daily
SELECT
    line_code,
    stat_date,
    lot_cnt,
    input_qty,
    good_qty,
    yield_final,
    AVG(yield_final) OVER (
        PARTITION BY line_code ORDER BY stat_date ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING
    ) AS yield_7d_avg,
    (yield_final - AVG(yield_final) OVER (
        PARTITION BY line_code ORDER BY stat_date ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING
    )) * 100 AS delta_pp,
    0,
    'normal'
FROM dws_mes_line_daily;

INSERT INTO ads_mes_loss_pareto
SELECT
    stat_date,
    process_code,
    defect_code,
    SUM(defect_qty) AS defect_qty,
    RANK() OVER (PARTITION BY stat_date ORDER BY SUM(defect_qty) DESC) AS loss_rank
FROM dws_mes_defect_pareto
GROUP BY stat_date, process_code, defect_code;
