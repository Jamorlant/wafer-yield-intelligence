"""Data-quality gate for the warehouse and the model-ready table.

Checks are grouped into four families:
  completeness  - row counts, missing rates
  validity      - label domain, timestamp parsing, finite values, class count
  uniqueness    - duplicate wafer ids
  consistency   - layer-to-layer row parity and strict temporal split order

``fail`` blocks the pipeline; ``warn`` is reported but does not block.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import numpy as np

from src.config import path_of
from src.db import connect, read_df
from src.paths import ensure_parent

log = logging.getLogger(__name__)


def _check(name: str, status: str, observed, expected, details: str = "") -> dict:
    return {
        "check_name": name,
        "status": status,
        "observed": str(observed),
        "expected": str(expected),
        "details": details,
    }


def _latest_run_id(conn) -> str:
    row = conn.execute("SELECT run_id FROM meta_pipeline_run ORDER BY started_at DESC LIMIT 1").fetchone()
    return row["run_id"] if row else "manual"


def run_quality_checks(cfg: dict) -> dict:
    checks: list[dict] = []
    db_path = path_of(cfg, "warehouse")

    with connect(db_path) as conn:
        run_id = _latest_run_id(conn)
        expected_rows = cfg["secom"]["expected_rows"]
        expected_sensors = cfg["secom"]["expected_sensors"]

        wafer_cnt = conn.execute("SELECT COUNT(*) AS c FROM ods_secom_wafer").fetchone()["c"]
        checks.append(
            _check(
                "secom_wafer_row_count",
                "pass" if wafer_cnt == expected_rows else "fail",
                wafer_cnt,
                expected_rows,
                "UCI SECOM ships 1,567 wafers",
            )
        )

        sensor_cnt = conn.execute("SELECT COUNT(DISTINCT sensor_id) AS c FROM ods_secom_sensor_value").fetchone()["c"]
        checks.append(
            _check(
                "secom_sensor_column_count",
                "pass" if sensor_cnt == expected_sensors else "fail",
                sensor_cnt,
                expected_sensors,
            )
        )

        dup = conn.execute(
            "SELECT COUNT(*) - COUNT(DISTINCT wafer_id) AS c FROM ods_secom_wafer"
        ).fetchone()["c"]
        checks.append(_check("duplicate_wafer_id", "pass" if dup == 0 else "fail", dup, 0))

        bad_labels = conn.execute("SELECT COUNT(*) AS c FROM ods_secom_wafer WHERE label_raw NOT IN (-1, 1)").fetchone()["c"]
        checks.append(_check("label_domain", "pass" if bad_labels == 0 else "fail", bad_labels, 0, "allowed: -1 pass, 1 fail"))

        bad_ts = conn.execute(
            "SELECT COUNT(*) AS c FROM ods_secom_wafer WHERE datetime(measurement_ts) IS NULL"
        ).fetchone()["c"]
        checks.append(_check("timestamp_parse", "pass" if bad_ts == 0 else "fail", bad_ts, 0))

        fail_cnt = conn.execute("SELECT SUM(label_raw = 1) AS c FROM ods_secom_wafer").fetchone()["c"] or 0
        fail_rate = fail_cnt / wafer_cnt if wafer_cnt else 0.0
        checks.append(
            _check(
                "label_fail_rate",
                "pass" if 0.03 <= fail_rate <= 0.12 else "warn",
                round(fail_rate, 4),
                "0.03 - 0.12",
                "known class imbalance (~6.6% failures); drives PR-AUC/recall@FPR reporting",
            )
        )

        profile = read_df(conn, "SELECT * FROM dwd_sensor_profile")
        max_missing = float(profile["missing_rate"].max())
        checks.append(
            _check("sensor_max_missing_rate", "pass" if max_missing <= 0.95 else "warn", round(max_missing, 4), "<= 0.95")
        )
        checks.append(
            _check(
                "constant_sensor_count",
                "pass",
                int(profile["is_constant"].sum()),
                "documented, excluded from model",
                "constant columns carry no signal but stay in DWD for auditability",
            )
        )
        checks.append(
            _check(
                "high_missing_sensor_count",
                "pass" if int(profile["is_high_missing"].sum()) <= 50 else "warn",
                int(profile["is_high_missing"].sum()),
                "<= 50",
                f"threshold = {cfg['secom']['quality']['max_missing_rate']:.0%}",
            )
        )
        checks.append(
            _check(
                "usable_sensor_count",
                "pass" if 200 <= int(profile["is_usable"].sum()) <= 590 else "warn",
                int(profile["is_usable"].sum()),
                "200 - 590",
            )
        )

        raw = read_df(conn, "SELECT raw_value FROM ods_secom_sensor_value WHERE raw_value IS NOT NULL")
        nonfinite = int((~np.isfinite(raw["raw_value"].to_numpy())).sum())
        checks.append(_check("nonfinite_sensor_values", "pass" if nonfinite == 0 else "fail", nonfinite, 0))

        ods_values = conn.execute("SELECT COUNT(*) AS c FROM ods_secom_sensor_value").fetchone()["c"]
        dwd_values = conn.execute("SELECT COUNT(*) AS c FROM dwd_sensor_value").fetchone()["c"]
        checks.append(
            _check(
                "layer_row_parity",
                "pass" if ods_values == dwd_values else "fail",
                f"ods={ods_values:,} dwd={dwd_values:,}",
                "equal",
            )
        )

        wm_cnt = conn.execute("SELECT COUNT(*) AS c FROM ods_wm811k_wafer").fetchone()["c"]
        checks.append(_check("wm811k_row_count", "pass" if wm_cnt == 172950 else "fail", wm_cnt, 172950))
        wm_classes = conn.execute("SELECT COUNT(DISTINCT label_name) AS c FROM ods_wm811k_wafer").fetchone()["c"]
        checks.append(_check("wm811k_class_count", "pass" if wm_classes == 9 else "fail", wm_classes, 9))
        wm_invalid = conn.execute("SELECT COUNT(*) AS c FROM dwd_wm811k_wafer WHERE is_valid = 0").fetchone()["c"]
        checks.append(
            _check("wm811k_invalid_rows", "pass" if wm_invalid == 0 else "warn", wm_invalid, 0, "physical-rule violations")
        )

        split_df = read_df(conn, "SELECT split, measurement_ts FROM ads_yield_feature_matrix")
        split_df["measurement_ts"] = split_df["measurement_ts"].astype(str)
        train_max = split_df.loc[split_df["split"] == "train", "measurement_ts"].max()
        test_min = split_df.loc[split_df["split"] == "test", "measurement_ts"].min()
        ordered = bool(train_max < test_min)
        checks.append(
            _check(
                "temporal_split_order",
                "pass" if ordered else "fail",
                f"train_max={train_max} < test_min={test_min}",
                "strictly time ordered",
                "prevents future leakage in model evaluation",
            )
        )

        split_counts = split_df["split"].value_counts().to_dict()
        checks.append(
            _check(
                "split_sizes",
                "pass" if min(split_counts.values()) >= 100 else "warn",
                json.dumps({k: int(v) for k, v in sorted(split_counts.items())}),
                "each split >= 100",
            )
        )

        # ------------------------------------------------------------ MES demo
        mes = cfg["mes"]
        expected_lots = (
            len(mes["lines"]) * int(mes["days"]) * 3 * int(mes["lots_per_shift"])
        )
        expected_steps = expected_lots * len(mes["processes"])
        lot_cnt = conn.execute("SELECT COUNT(*) AS c FROM ods_mes_lot").fetchone()["c"]
        step_cnt = conn.execute("SELECT COUNT(*) AS c FROM dwd_mes_process").fetchone()["c"]
        checks.append(_check("mes_lot_row_count", "pass" if lot_cnt == expected_lots else "fail", lot_cnt, expected_lots))
        checks.append(
            _check(
                "mes_step_row_count",
                "pass" if step_cnt == expected_steps else "fail",
                step_cnt,
                expected_steps,
                f"{len(mes['lines'])} lines x {mes['days']} days x 3 shifts x {mes['lots_per_shift']} lots x {len(mes['processes'])} steps",
            )
        )

        bad_qty = conn.execute("SELECT COUNT(*) AS c FROM dwd_mes_process WHERE is_qty_consistent = 0").fetchone()["c"]
        checks.append(
            _check(
                "mes_quantity_conservation",
                "pass" if bad_qty == 0 else "fail",
                bad_qty,
                0,
                "input = output + defect at every step; output(k) = input(k+1)",
            )
        )

        bad_route = conn.execute(
            "SELECT COUNT(*) AS c FROM dwd_mes_lot_route WHERE is_qty_consistent = 0"
        ).fetchone()["c"]
        checks.append(
            _check(
                "mes_route_yield_parity",
                "pass" if bad_route == 0 else "fail",
                bad_route,
                0,
                "lot-level yield recomputed from step 1 input and last step output equals the lot header",
            )
        )

        dirty_yield = conn.execute(
            "SELECT COUNT(*) AS c FROM dwd_mes_process WHERE step_yield <= 0.80 OR step_yield > 1.0"
        ).fetchone()["c"]
        checks.append(_check("mes_step_yield_domain", "pass" if dirty_yield == 0 else "fail", dirty_yield, 0))

        days_covered = conn.execute(
            "SELECT COUNT(DISTINCT stat_date) AS c FROM dwd_mes_lot_route"
        ).fetchone()["c"]
        checks.append(
            _check(
                "mes_time_continuity",
                "pass" if days_covered == int(mes["days"]) else "fail",
                days_covered,
                int(mes["days"]),
                "one row per production day, no gaps",
            )
        )

        kpi_drift = conn.execute(
            """
            SELECT COUNT(*) AS c FROM (
                SELECT k.line_code, k.stat_date, k.yield_final, d.yield_final AS dws_yield
                FROM ads_mes_kpi_daily k
                JOIN dws_mes_line_daily d
                  ON d.line_code = k.line_code AND d.stat_date = k.stat_date
                WHERE ABS(k.yield_final - d.yield_final) > 1e-9
            )
            """
        ).fetchone()["c"]
        checks.append(
            _check("mes_kpi_caliber_consistency", "pass" if kpi_drift == 0 else "fail", kpi_drift, 0, "ADS yield = sum(good)/sum(input)")
        )

        scenario_cnt = conn.execute("SELECT COUNT(*) AS c FROM ods_mes_scenario").fetchone()["c"]
        verified = conn.execute(
            "SELECT COUNT(*) AS c FROM ads_mes_scenario_check WHERE verdict = 'MATCH'"
        ).fetchone()["c"]
        checks.append(
            _check(
                "mes_scenario_detected",
                "pass" if verified == scenario_cnt else "fail",
                f"{verified}/{scenario_cnt}",
                "all injected scenarios detected",
                "each injected drift must produce the expected SPC rule, a step-yield drop and a final-yield drop",
            )
        )

        untraced = conn.execute("SELECT COUNT(*) AS c FROM ads_mes_alert WHERE scenario_id = ''").fetchone()["c"]
        checks.append(
            _check(
                "mes_alert_traceability",
                "pass" if untraced == 0 else "warn",
                untraced,
                "0 in synthetic demo",
                "every alert in this synthetic dataset maps 1:1 to an injected scenario; "
                "on a real MES export orphan alerts are expected and become investigation work",
            )
        )

        labelled = conn.execute("SELECT source FROM ods_mes_asset WHERE asset_name = 'mes_extract'").fetchone()
        checks.append(
            _check(
                "mes_source_labelled",
                "pass" if labelled is not None else "fail",
                labelled["source"] if labelled else "missing",
                "synthetic | export",
                "the dashboard banner and README must state the data source",
            )
        )

        statuses = {check["status"] for check in checks}
        overall = "fail" if "fail" in statuses else "warn" if "warn" in statuses else "pass"

        report = {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "run_id": run_id,
            "overall_status": overall,
            "summary": {
                "wafers": int(wafer_cnt),
                "sensors": int(sensor_cnt),
                "fail_rate": round(float(fail_rate), 4),
                "usable_sensors": int(profile["is_usable"].sum()),
                "max_sensor_missing_rate": round(max_missing, 4),
                "split": {k: int(v) for k, v in split_counts.items()},
                "mes_lots": int(lot_cnt),
                "mes_step_rows": int(step_cnt),
                "mes_scenarios_verified": f"{verified}/{scenario_cnt}",
                "mes_untraced_alerts": int(untraced),
            },
            "checks": checks,
        }

        for check in checks:
            conn.execute(
                "INSERT OR REPLACE INTO meta_quality_check VALUES (?,?,?,?,?,?,?)",
                (
                    run_id,
                    check["check_name"],
                    check["status"],
                    check["observed"],
                    check["expected"],
                    check["details"],
                    report["generated_at"],
                ),
            )

    output = ensure_parent(path_of(cfg, "artifacts") / "quality_report.json")
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("quality report: %s (%s)", output, overall)
    return report
