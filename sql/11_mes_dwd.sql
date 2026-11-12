-- =============================================================================
-- MES DWD layer - conformed, validated, event grain preserved.
-- -----------------------------------------------------------------------------
-- Rules applied (codes in src/warehouse/mes.py):
--   1. Quantity conservation: input = output + defect, and the output of step
--      k must equal the input of step k+1. Violations are flagged, not dropped.
--   2. step_yield is derived from quantities, never from the synthetic yield
--      column, so the KPI is reproducible from the raw counts alone.
--   3. Lot route is rebuilt from the process steps (first input / last output)
--      and compared with the lot header; mismatches become invalid rows.
-- =============================================================================

DROP TABLE IF EXISTS dwd_mes_process;
CREATE TABLE dwd_mes_process (
    lot_id        TEXT NOT NULL,
    line_code     TEXT NOT NULL,
    product_code  TEXT NOT NULL,
    process_seq   INTEGER NOT NULL,
    process_code  TEXT NOT NULL,
    equipment_id  TEXT NOT NULL,
    process_ts    TEXT NOT NULL,
    stat_date     TEXT NOT NULL,
    stat_hour     TEXT NOT NULL,           -- YYYY-MM-DDTHH
    shift_code    TEXT NOT NULL,
    input_qty     INTEGER NOT NULL,
    output_qty    INTEGER NOT NULL,
    defect_qty    INTEGER NOT NULL,
    defect_code   TEXT NOT NULL,
    step_yield    REAL NOT NULL,
    is_qty_consistent INTEGER NOT NULL,
    scenario_id   TEXT NOT NULL DEFAULT '',
    param_1       REAL,
    param_2       REAL,
    param_3       REAL,
    param_4       REAL,
    param_5       REAL,
    PRIMARY KEY (lot_id, process_seq)
);
CREATE INDEX idx_dwd_mes_process_date ON dwd_mes_process (stat_date);
CREATE INDEX idx_dwd_mes_process_equip ON dwd_mes_process (equipment_id, process_code, stat_hour);

DROP TABLE IF EXISTS dwd_mes_lot_route;
CREATE TABLE dwd_mes_lot_route (
    lot_id       TEXT PRIMARY KEY,
    line_code    TEXT NOT NULL,
    product_code TEXT NOT NULL,
    start_shift  TEXT NOT NULL,
    lot_start_ts TEXT NOT NULL,
    stat_date    TEXT NOT NULL,
    input_qty    INTEGER NOT NULL,
    good_qty     INTEGER NOT NULL,
    scrap_qty    INTEGER NOT NULL,
    yield_final  REAL NOT NULL,
    header_yield REAL NOT NULL,
    is_qty_consistent INTEGER NOT NULL
);
CREATE INDEX idx_dwd_mes_lot_route_date ON dwd_mes_lot_route (stat_date, line_code);

DROP TABLE IF EXISTS dwd_mes_param_spec;
CREATE TABLE dwd_mes_param_spec (
    process_code TEXT NOT NULL,
    process_name TEXT NOT NULL,
    process_seq  INTEGER NOT NULL,
    param_index  INTEGER NOT NULL,
    param_code   TEXT NOT NULL,
    param_name   TEXT NOT NULL,
    unit         TEXT NOT NULL,
    base_value   REAL NOT NULL,
    sd_value     REAL NOT NULL,
    is_key       INTEGER NOT NULL,
    owner_role   TEXT NOT NULL,
    PRIMARY KEY (process_code, param_index)
);

INSERT INTO dwd_mes_process
SELECT
    p.lot_id,
    p.line_code,
    p.product_code,
    p.process_seq,
    p.process_code,
    p.equipment_id,
    p.process_ts,
    substr(p.process_ts, 1, 10)                    AS stat_date,
    substr(p.process_ts, 1, 13)                    AS stat_hour,
    p.shift_code,
    p.input_qty,
    p.output_qty,
    p.defect_qty,
    p.defect_code,
    1.0 * p.output_qty / NULLIF(p.input_qty, 0)    AS step_yield,
    CASE WHEN p.input_qty = p.output_qty + p.defect_qty THEN 1 ELSE 0 END AS is_qty_consistent,
    p.scenario_id,
    p.param_1, p.param_2, p.param_3, p.param_4, p.param_5
FROM ods_mes_process p;

INSERT INTO dwd_mes_lot_route
SELECT
    l.lot_id,
    l.line_code,
    l.product_code,
    l.start_shift,
    l.lot_start_ts,
    substr(l.lot_start_ts, 1, 10) AS stat_date,
    first_step.input_qty,
    last_step.output_qty,
    first_step.input_qty - last_step.output_qty AS scrap_qty,
    1.0 * last_step.output_qty / NULLIF(first_step.input_qty, 0) AS yield_final,
    l.yield_final AS header_yield,
    CASE WHEN last_step.output_qty = l.good_qty THEN 1 ELSE 0 END AS is_qty_consistent
FROM ods_mes_lot l
JOIN ods_mes_process first_step
  ON first_step.lot_id = l.lot_id AND first_step.process_seq = 1
JOIN ods_mes_process last_step
  ON last_step.lot_id = l.lot_id
 AND last_step.process_seq = (SELECT MAX(process_seq) FROM ods_mes_process x WHERE x.lot_id = l.lot_id);
