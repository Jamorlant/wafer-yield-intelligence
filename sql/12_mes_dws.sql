-- =============================================================================
-- MES DWS layer - reusable aggregates at the grains the business actually
-- asks for: line x day, line x step x tool x shift x day, product x day,
-- tool x parameter x hour (SPC input) and defect pareto.
-- =============================================================================

DROP TABLE IF EXISTS dws_mes_line_daily;
CREATE TABLE dws_mes_line_daily (
    line_code   TEXT NOT NULL,
    stat_date   TEXT NOT NULL,
    lot_cnt     INTEGER NOT NULL,
    input_qty   INTEGER NOT NULL,
    good_qty    INTEGER NOT NULL,
    scrap_qty   INTEGER NOT NULL,
    yield_final REAL NOT NULL,
    PRIMARY KEY (line_code, stat_date)
);

DROP TABLE IF EXISTS dws_mes_product_daily;
CREATE TABLE dws_mes_product_daily (
    line_code    TEXT NOT NULL,
    product_code TEXT NOT NULL,
    stat_date    TEXT NOT NULL,
    lot_cnt      INTEGER NOT NULL,
    input_qty    INTEGER NOT NULL,
    good_qty     INTEGER NOT NULL,
    yield_final  REAL NOT NULL,
    PRIMARY KEY (line_code, product_code, stat_date)
);

DROP TABLE IF EXISTS dws_mes_process_daily;
CREATE TABLE dws_mes_process_daily (
    line_code    TEXT NOT NULL,
    process_code TEXT NOT NULL,
    equipment_id TEXT NOT NULL,
    shift_code   TEXT NOT NULL,
    stat_date    TEXT NOT NULL,
    lot_cnt      INTEGER NOT NULL,
    input_qty    INTEGER NOT NULL,
    defect_qty   INTEGER NOT NULL,
    step_yield   REAL NOT NULL,
    PRIMARY KEY (line_code, process_code, equipment_id, shift_code, stat_date)
);

DROP TABLE IF EXISTS dws_mes_param_hourly;
CREATE TABLE dws_mes_param_hourly (
    line_code    TEXT NOT NULL,
    process_code TEXT NOT NULL,
    equipment_id TEXT NOT NULL,
    param_index  INTEGER NOT NULL,
    stat_hour    TEXT NOT NULL,
    sample_cnt   INTEGER NOT NULL,
    mean_value   REAL NOT NULL,
    std_value    REAL,
    PRIMARY KEY (line_code, process_code, equipment_id, param_index, stat_hour)
);

DROP TABLE IF EXISTS dws_mes_defect_pareto;
CREATE TABLE dws_mes_defect_pareto (
    stat_date    TEXT NOT NULL,
    line_code    TEXT NOT NULL,
    process_code TEXT NOT NULL,
    equipment_id TEXT NOT NULL,
    shift_code   TEXT NOT NULL,
    defect_code  TEXT NOT NULL,
    defect_qty   INTEGER NOT NULL,
    PRIMARY KEY (stat_date, line_code, process_code, equipment_id, shift_code, defect_code)
);

INSERT INTO dws_mes_line_daily
SELECT line_code, stat_date, COUNT(*), SUM(input_qty), SUM(good_qty), SUM(scrap_qty),
       1.0 * SUM(good_qty) / SUM(input_qty)
FROM dwd_mes_lot_route
GROUP BY line_code, stat_date;

INSERT INTO dws_mes_product_daily
SELECT line_code, product_code, stat_date, COUNT(*), SUM(input_qty), SUM(good_qty),
       1.0 * SUM(good_qty) / SUM(input_qty)
FROM dwd_mes_lot_route
GROUP BY line_code, product_code, stat_date;
