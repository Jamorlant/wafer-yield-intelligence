"""Static dashboard generator (MES BI first, SECOM as technical appendix).

The main narrative is the synthetic MES demo: overview -> drill-down -> SPC
alerts. The SECOM yield model and the WM-811K defect model stay available in
the appendix for technical questions.

Output: a self-contained ``dashboard/index.html`` plus
``dashboard/data/dashboard.json`` and the vendored ECharts build, so the demo
runs offline with no service.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from src.config import path_of
from src.db import connect, read_df
from src.ingest.wm811k import load_representative_maps
from src.paths import ensure_parent

log = logging.getLogger(__name__)


def _decode_map(item: dict) -> dict:
    cells: list[int] = []
    for value, count in item["rle"]:
        cells.extend([int(value)] * int(count))
    rows, cols = int(item["rows"]), int(item["cols"])
    if len(cells) != rows * cols:
        raise ValueError(f"bad RLE for class {item.get('class')}: {len(cells)} != {rows * cols}")
    return {
        "class": item["class"],
        "rows": rows,
        "cols": cols,
        "density": round(float(item.get("defect_density", 0)), 4),
        "grid": [cells[i * cols : (i + 1) * cols] for i in range(rows)],
    }


def _risk_histogram(frame: pd.DataFrame) -> list[dict]:
    bins = [0, 0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 0.9, 1.01]
    labels = ["0-5%", "5-10%", "10-20%", "20-30%", "30-50%", "50-70%", "70-90%", "90-100%"]
    cut = pd.cut(frame["fail_probability"], bins=bins, labels=labels, right=False)
    counts = cut.value_counts().reindex(labels, fill_value=0)
    return [{"bin": label, "count": int(count)} for label, count in counts.items()]


def _round(value, digits=4):
    if value is None or pd.isna(value):
        return None
    return round(float(value), digits)


def _mes_payload(conn, cfg: dict) -> dict:
    mes = cfg["mes"]
    target = float(mes.get("target_yield", 0.945))
    kpi = read_df(conn, "SELECT * FROM ads_mes_kpi_daily ORDER BY line_code, stat_date")
    alerts = read_df(conn, "SELECT * FROM ads_mes_alert ORDER BY alert_ts DESC")
    data_end = kpi["stat_date"].max()
    # Snapshot the board on the day the incident fired, not on the last day of
    # the extract: a BI screen is read when the alert lands, before recovery.
    snapshot = data_end
    if not alerts.empty:
        last_alert_date = str(alerts["alert_ts"].str[:10].max())
        if 0 <= (pd.Timestamp(data_end) - pd.Timestamp(last_alert_date)).days <= 3:
            snapshot = last_alert_date
    kpi = kpi[kpi["stat_date"] <= snapshot]
    last_date = snapshot
    recent_start = (pd.Timestamp(last_date) - pd.Timedelta(days=6)).strftime("%Y-%m-%d")

    line_names = {line["code"]: line["name"] for line in mes["lines"]}
    today = kpi[kpi["stat_date"] == last_date]
    week = kpi[kpi["stat_date"] >= recent_start]
    total_input = today["input_qty"].sum()
    total_good = today["good_qty"].sum()
    overall_yield = float(total_good / total_input) if total_input else 0.0
    prev_7 = week[week["stat_date"] < last_date]
    overall_7d = float(prev_7["good_qty"].sum() / prev_7["input_qty"].sum()) if len(prev_7) else overall_yield

    lines = []
    for line in mes["lines"]:
        code = line["code"]
        subset = kpi[kpi["line_code"] == code].sort_values("stat_date")
        current = subset[subset["stat_date"] == last_date]
        series = [[row.stat_date, _round(row.yield_final * 100, 2)] for row in subset.tail(30).itertuples()]
        row = current.iloc[0] if not current.empty else None
        lines.append(
            {
                "code": code,
                "name": line_names.get(code, code),
                "yield_pct": _round(row.yield_final * 100, 2) if row is not None else None,
                "yield_7d_pct": _round(row.yield_7d_avg * 100, 2) if row is not None else None,
                "delta_pp": _round(row.delta_pp, 2) if row is not None else None,
                "alert_cnt": int(row.alert_cnt) if row is not None else 0,
                "status": row.status if row is not None else "normal",
                "series": series,
            }
        )

    alerts = read_df(conn, "SELECT * FROM ads_mes_alert ORDER BY alert_ts DESC")
    suspects = read_df(
        conn,
        """
        SELECT s.*, a.param_name, a.rule_code, a.alert_ts, a.equipment_id
        FROM ads_mes_suspect_lot s
        JOIN ads_mes_alert a ON a.alert_id = s.alert_id
        ORDER BY a.alert_ts DESC, s.defect_qty DESC
        """,
    )
    scenario_rows = read_df(conn, "SELECT * FROM ads_mes_scenario_check ORDER BY scenario_id")
    scenario_meta = read_df(conn, "SELECT * FROM ods_mes_scenario ORDER BY scenario_id")

    # ---- SPC charts: every alerted series + one healthy reference series
    charts = []
    seen = set()
    for alert in alerts.sort_values("alert_ts").itertuples():
        key = (alert.line_code, alert.process_code, alert.equipment_id, int(alert.param_index))
        if key in seen:
            continue
        seen.add(key)
        series = read_df(
            conn,
            """
            SELECT stat_hour, mean_value, z_score, base_mean, ucl, lcl, scenario_id
            FROM ads_mes_spc
            WHERE line_code=? AND process_code=? AND equipment_id=? AND param_index=?
            ORDER BY stat_hour
            """,
            (alert.line_code, alert.process_code, alert.equipment_id, int(alert.param_index)),
        )
        series = series[series["stat_hour"].str[:10] <= last_date]
        anchor = pd.Timestamp(alert.alert_ts)
        lo = (anchor - pd.Timedelta(days=7)).strftime("%Y-%m-%dT%H")
        hi = (anchor + pd.Timedelta(days=2)).strftime("%Y-%m-%dT%H")
        series = series[(series["stat_hour"] >= lo) & (series["stat_hour"] <= hi)]
        charts.append(
            {
                "key": f"{alert.line_code}|{alert.process_code}|{alert.equipment_id}|{alert.param_index}",
                "line_code": alert.line_code,
                "process_code": alert.process_code,
                "equipment_id": alert.equipment_id,
                "param_name": alert.param_name,
                "unit": alert.unit,
                "rule_code": alert.rule_code,
                "alert_ts": alert.alert_ts,
                "points": [
                    [row.stat_hour, _round(row.mean_value, 3), _round(row.z_score, 2), _round(row.ucl, 3), _round(row.lcl, 3)]
                    for row in series.itertuples()
                ],
            }
        )
    reference = read_df(
        conn,
        """
        SELECT stat_hour, mean_value, z_score, ucl, lcl
        FROM ads_mes_spc
        WHERE line_code='LINE-A' AND process_code='WIRE_BOND' AND equipment_id='EQ-WB-01' AND param_index=4
        ORDER BY stat_hour
        """,
    )
    ref_start = (pd.Timestamp(last_date) - pd.Timedelta(days=6)).strftime("%Y-%m-%d")
    reference = reference[(reference["stat_hour"].str[:10] >= ref_start) & (reference["stat_hour"].str[:10] <= last_date)]
    if not reference.empty:
        charts.append(
            {
                "key": "reference",
                "line_code": "LINE-A",
                "process_code": "WIRE_BOND",
                "equipment_id": "EQ-WB-01",
                "param_name": "弧高",
                "unit": "um",
                "rule_code": "NORMAL",
                "alert_ts": None,
                "points": [
                    [row.stat_hour, _round(row.mean_value, 3), _round(row.z_score, 2), _round(row.ucl, 3), _round(row.lcl, 3)]
                    for row in reference.itertuples()
                ],
            }
        )

    # ---- scenario chain (the demo's causal story), S1 in detail
    chain = None
    if not scenario_meta.empty:
        scenario = scenario_meta.iloc[0]
        start = pd.Timestamp(scenario["start_ts"])
        end = start + pd.Timedelta(hours=12)
        param_series = read_df(
            conn,
            """
            SELECT stat_hour, mean_value, z_score, ucl, lcl
            FROM ads_mes_spc
            WHERE line_code=? AND process_code=? AND equipment_id=? AND param_index=?
            ORDER BY stat_hour
            """,
            (
                scenario["line_code"],
                scenario["process_code"],
                scenario["equipment_id"],
                int(
                    read_df(
                        conn,
                        "SELECT param_index FROM dwd_mes_param_spec WHERE process_code=? AND param_code=?",
                        (scenario["process_code"], scenario["param_code"]),
                    ).iloc[0]["param_index"]
                ),
            ),
        )
        step = read_df(
            conn,
            """
            SELECT stat_hour, SUM(input_qty) AS input_qty, SUM(defect_qty) AS defect_qty
            FROM dwd_mes_process
            WHERE line_code=? AND process_code=? AND equipment_id=?
            GROUP BY stat_hour ORDER BY stat_hour
            """,
            (scenario["line_code"], scenario["process_code"], scenario["equipment_id"]),
        )
        final = read_df(
            conn,
            """
            SELECT substr(t.process_ts, 1, 13) AS stat_hour, AVG(l.yield_final) AS yield_final
            FROM dwd_mes_lot_route l
            JOIN dwd_mes_process d ON d.lot_id = l.lot_id AND d.process_code = ?
            JOIN dwd_mes_process t ON t.lot_id = l.lot_id AND t.process_code = 'TEST'
            WHERE d.line_code = ? AND d.equipment_id = ?
            GROUP BY substr(t.process_ts, 1, 13)
            """,
            (scenario["process_code"], scenario["line_code"], scenario["equipment_id"]),
        )
        window = [start.strftime("%Y-%m-%dT%H"), end.strftime("%Y-%m-%dT%H")]
        param_series = param_series[(param_series["stat_hour"] >= window[0]) & (param_series["stat_hour"] < window[1])]
        step = step[(step["stat_hour"] >= window[0]) & (step["stat_hour"] < window[1])]
        final = final[(final["stat_hour"] >= window[0]) & (final["stat_hour"] < window[1])]
        step_yield = step.assign(step_yield=1.0 - step["defect_qty"] / step["input_qty"])
        merged = (
            param_series.merge(step_yield[["stat_hour", "step_yield"]], on="stat_hour", how="outer")
            .merge(final, on="stat_hour", how="outer")
            .sort_values("stat_hour")
        )
        chain = {
            "scenario_id": scenario["scenario_id"],
            "title": scenario["title"],
            "line_code": scenario["line_code"],
            "process_code": scenario["process_code"],
            "equipment_id": scenario["equipment_id"],
            "param_name": scenario["param_name"],
            "unit": scenario["unit"],
            "start_ts": scenario["start_ts"],
            "points": [
                [
                    row.stat_hour,
                    _round(row.mean_value, 3),
                    _round(row.z_score, 2),
                    _round(row.ucl, 3),
                    _round(row.lcl, 3),
                    _round(row.step_yield, 4),
                    _round(row.yield_final, 4),
                ]
                for row in merged.itertuples()
            ],
        }

    # ---- drill-down tables (last 14 days at the deepest grain)
    drill_start = (pd.Timestamp(last_date) - pd.Timedelta(days=13)).strftime("%Y-%m-%d")
    drill = read_df(
        conn,
        """
        SELECT line_code, process_code, equipment_id, shift_code, stat_date,
               lot_cnt, input_qty, defect_qty, step_yield
        FROM dws_mes_process_daily
        WHERE stat_date >= ? AND stat_date <= ?
        ORDER BY stat_date
        """,
        (drill_start, last_date),
    )
    process_daily = read_df(
        conn,
        """
        SELECT line_code, process_code, stat_date, SUM(input_qty) AS input_qty,
               SUM(defect_qty) AS defect_qty
        FROM dwd_mes_process
        WHERE stat_date BETWEEN ? AND ?
        GROUP BY line_code, process_code, stat_date
        ORDER BY stat_date
        """,
        (drill_start, last_date),
    )
    pareto = read_df(
        conn,
        """
        SELECT line_code, process_code, equipment_id, shift_code, defect_code, SUM(defect_qty) AS defect_qty
        FROM dws_mes_defect_pareto
        WHERE stat_date >= ? AND stat_date <= ?
        GROUP BY line_code, process_code, equipment_id, shift_code, defect_code
        """,
        (drill_start, last_date),
    )
    product_daily = read_df(
        conn,
        """
        SELECT line_code, product_code, stat_date, input_qty, good_qty, yield_final
        FROM dws_mes_product_daily
        WHERE stat_date BETWEEN ? AND ?
        ORDER BY stat_date
        """,
        (drill_start, last_date),
    )

    quality = json.loads((path_of(cfg, "artifacts") / "quality_report.json").read_text(encoding="utf-8"))
    generation = json.loads((path_of(cfg, "artifacts") / "mes_generation.json").read_text(encoding="utf-8"))

    top_loss = (
        read_df(
            conn,
            """
            SELECT process_code, defect_code, SUM(defect_qty) AS defect_qty
            FROM ads_mes_loss_pareto
            WHERE stat_date >= ?
            GROUP BY process_code, defect_code
            ORDER BY defect_qty DESC LIMIT 8
            """,
            (recent_start,),
        )
        .assign(share=lambda frame: frame["defect_qty"] / frame["defect_qty"].sum())
        .to_dict("records")
    )

    return {
        "meta": {
            "title": "Wafer Yield Intelligence",
            "subtitle": "封装测试产线良率与设备异常 BI · 合成 MES 演示",
            "data_mode": generation["stats"].get("source", "synthetic"),
            "disclaimer": "本看板数据为 100% 合成数据，仅用于演示。真实产线数据受保密协议（NDA）限制无法获取；"
            "字段结构与 MES/EAP 导出一致，接入真实数据只替换数据源层（docs/mes_schema.md）。",
            "generated_at": datetime.now(UTC).astimezone().strftime("%Y-%m-%d %H:%M"),
            "as_of": last_date,
            "data_end": data_end,
            "target_yield": target,
            "seed": generation["stats"].get("seed"),
            "rows": generation["stats"].get("process_rows"),
            "lots": generation["stats"].get("lots"),
        },
        "overview": {
            "kpis": {
                "yield_pct": _round(overall_yield * 100, 2),
                "yield_target_pct": _round(target * 100, 2),
                "delta_7d_pp": _round((overall_yield - overall_7d) * 100, 2),
                "alert_cnt_7d": int(alerts[alerts["alert_ts"] >= recent_start].shape[0]) if not alerts.empty else 0,
                "suspect_lot_cnt": int(suspects["lot_id"].nunique()) if not suspects.empty else 0,
                "input_qty": int(total_input),
                "line_cnt": len(mes["lines"]),
            },
            "lines": lines,
            "top_loss": top_loss,
            "quality": {
                "status": quality["overall_status"],
                "checks": len(quality["checks"]),
                "pass": sum(1 for check in quality["checks"] if check["status"] == "pass"),
                "warn": sum(1 for check in quality["checks"] if check["status"] == "warn"),
                "fail": sum(1 for check in quality["checks"] if check["status"] == "fail"),
                "rows": quality["checks"],
            },
        },
        "drill": {
            "processes": [
                {"code": process["code"], "name": process["name"], "prefix": process["equipment_prefix"]}
                for process in mes["processes"]
            ],
            "lines": [{"code": line["code"], "name": line["name"]} for line in mes["lines"]],
            "shifts": [{"code": "DAY", "name": "白班"}, {"code": "SWING", "name": "中班"}, {"code": "NIGHT", "name": "夜班"}],
            "window": {"start": drill_start, "end": last_date},
            "step_rows": drill[
                ["line_code", "process_code", "equipment_id", "shift_code", "stat_date", "lot_cnt", "input_qty", "defect_qty", "step_yield"]
            ].round({"step_yield": 6}).values.tolist(),
            "process_rows": process_daily.round({"input_qty": 0}).values.tolist(),
            "product_rows": product_daily[["line_code", "product_code", "stat_date", "input_qty", "good_qty", "yield_final"]]
            .round({"yield_final": 6})
            .values.tolist(),
            "pareto_rows": pareto.values.tolist(),
            "chain": chain,
        },
        "alerts": {
            "rows": [
                {
                    "alert_id": row.alert_id,
                    "alert_ts": row.alert_ts,
                    "line_code": row.line_code,
                    "process_code": row.process_code,
                    "equipment_id": row.equipment_id,
                    "param_code": row.param_code,
                    "param_name": row.param_name,
                    "unit": row.unit,
                    "rule_code": row.rule_code,
                    "severity": row.severity,
                    "z_score": _round(row.z_score, 2),
                    "observed_value": _round(row.observed_value, 3),
                    "base_mean": _round(row.base_mean, 3),
                    "ucl": _round(row.ucl, 3),
                    "lcl": _round(row.lcl, 3),
                    "evidence": row.evidence,
                    "suggested_action": row.suggested_action,
                    "owner_role": row.owner_role,
                    "scenario_id": row.scenario_id,
                }
                for row in alerts.itertuples()
            ],
            "suspects": [
                {
                    "alert_id": row.alert_id,
                    "lot_id": row.lot_id,
                    "product_code": row.product_code,
                    "process_ts": row.process_ts,
                    "shift_code": row.shift_code,
                    "input_qty": int(row.input_qty),
                    "defect_qty": int(row.defect_qty),
                    "step_yield": _round(row.step_yield, 4),
                    "equipment_id": row.equipment_id,
                    "suggested_action": row.suggested_action,
                }
                for row in suspects.itertuples()
            ],
            "scenarios": [
                {
                    "scenario_id": row.scenario_id,
                    "expected_rule": row.expected_rule,
                    "detected_rule": row.detected_rule,
                    "detected_ts": row.detected_ts,
                    "detection_lag_hours": row.detection_lag_hours,
                    "first_breach_ts": row.first_breach_ts,
                    "first_step_drop_ts": row.first_step_drop_ts,
                    "step_drop_pp": row.step_drop_pp,
                    "final_drop_pp": row.final_drop_pp,
                    "verdict": row.verdict,
                    "detail": json.loads(row.detail),
                }
                for row in scenario_rows.itertuples()
            ],
            "spc": charts,
        },
        "appendix_secom": _secom_payload(cfg),
    }


def _secom_payload(cfg: dict) -> dict:
    artifacts = path_of(cfg, "artifacts")
    metrics = json.loads((artifacts / "metrics.json").read_text(encoding="utf-8"))
    defect = json.loads((artifacts / "defect_metrics.json").read_text(encoding="utf-8"))
    quality = json.loads((artifacts / "quality_report.json").read_text(encoding="utf-8"))
    attributions = json.loads((artifacts / "attributions.json").read_text(encoding="utf-8"))
    maps = [_decode_map(item) for item in load_representative_maps(path_of(cfg, "raw_dir"))]

    with connect(path_of(cfg, "warehouse")) as conn:
        trend = read_df(conn, "SELECT * FROM ads_yield_trend_daily ORDER BY stat_date")
        test_risk = read_df(conn, "SELECT * FROM ads_wafer_risk WHERE split = 'test' ORDER BY fail_probability DESC")
        drift = read_df(conn, "SELECT * FROM ads_sensor_drift")

    top_sensors = [row["feature"] for row in metrics["top_features"] if str(row["feature"]).startswith("S")][:4]
    drift_series = []
    for sensor in top_sensors:
        subset = drift.loc[drift["sensor_id"] == sensor].sort_values("stat_date")
        drift_series.append(
            {
                "sensor": sensor,
                "series": [
                    [row.stat_date, None if pd.isna(row.z_score) else round(float(row.z_score), 3)]
                    for row in subset.itertuples()
                ],
            }
        )

    alarms = [
        {
            "wafer_id": row.wafer_id,
            "stat_date": row.stat_date,
            "probability": round(float(row.fail_probability), 4),
            "risk_band": row.risk_band,
            "actual": "fail" if int(row.is_fail) == 1 else "pass",
            "top_driver": row.top_driver_1,
        }
        for row in test_risk.head(10).itertuples()
    ]

    selected = metrics["selected_model"]
    test = metrics["models"][selected]["test"]
    defect_selected = defect["selected_model"]
    defect_test = defect["models"][defect_selected]
    gain_total = sum(float(row["lgbm_gain"]) for row in metrics["top_features"]) or 1.0

    return {
        "kpi": {
            "test_auc": test["auc"],
            "test_auc_ci_low": test["auc_ci95"][0],
            "test_auc_ci_high": test["auc_ci95"][1],
            "backtest_auc_mean": metrics["backtest"]["summary"][selected]["auc_mean"],
            "backtest_auc_std": metrics["backtest"]["summary"][selected]["auc_std"],
            "test_pr_auc": test["pr_auc"],
            "test_recall": test["recall"],
            "test_alerts": test["alerts"],
            "test_fails": test["actual_fail"],
            "test_n": test["n"],
            "defect_macro_f1": defect_test["macro_f1"],
            "defect_accuracy": defect_test["accuracy"],
            "quality_status": quality["overall_status"],
            "quality_checks": len(quality["checks"]),
            "fail_rate": metrics["dataset"]["fail_rate"],
            "business": test["business"],
        },
        "metrics": metrics,
        "defect": defect,
        "quality_checks": quality["checks"],
        "trend": [
            {
                "date": row.stat_date,
                "yield_pct": round(float(row.actual_yield) * 100, 2),
                "risk_pct": None if pd.isna(row.avg_risk) else round(float(row.avg_risk) * 100, 2),
            }
            for row in trend.itertuples()
        ],
        "risk_hist": _risk_histogram(test_risk),
        "alarms": alarms,
        "attributions": attributions,
        "drift": drift_series,
        "importance": [
            {
                "feature": row["feature"],
                "gain_share": round(float(row["lgbm_gain"]) / gain_total, 4),
                "effect_mean_abs": round(float(row["effect_mean_abs"]), 4),
            }
            for row in metrics["top_features"][:10]
        ],
        "maps": maps,
    }


def build_dashboard(cfg: dict) -> dict:
    dashboard_dir = path_of(cfg, "dashboard")
    with connect(path_of(cfg, "warehouse")) as conn:
        data = _mes_payload(conn, cfg)
        data["appendix_secom"] = _secom_payload(cfg)

    ensure_parent(dashboard_dir / "data" / "dashboard.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    template = (Path(__file__).parent / "template.html").read_text(encoding="utf-8")
    html = template.replace("__DASHBOARD_DATA__", json.dumps(data, ensure_ascii=False))
    ensure_parent(dashboard_dir / "index.html").write_text(html, encoding="utf-8")
    log.info("dashboard written to %s", dashboard_dir / "index.html")
    return data
