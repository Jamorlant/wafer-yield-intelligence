"""MES warehouse: ODS -> DWD -> DWS -> ADS, plus scenario verification.

The layer boundaries mirror the SECOM pipeline (see ``src/warehouse/build.py``):
ODS keeps the raw extract, DWD conforms and validates, DWS aggregates to the
grains the business asks for, ADS is dashboard-ready and frozen.

Two things are deliberate:

* quantities are the only source of truth for yield - ``step_yield`` and
  ``yield_final`` are recomputed from integer counts, never copied;
* every injected anomaly is verified against the SPC output
  (``ads_mes_scenario_check``). A mismatch fails the quality gate, so the demo
  can never claim a causal chain the data does not support.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

import numpy as np
import pandas as pd

from src.config import path_of
from src.db import bulk_insert, read_df, run_sql_file
from src.ingest.mes import load_mes_source, param_spec
from src.quality import spc

log = logging.getLogger(__name__)

LOT_COLUMNS = ["lot_id", "line_code", "product_code", "start_shift", "lot_start_ts", "lot_size", "good_qty", "yield_final"]
PROCESS_COLUMNS = [
    "lot_id",
    "line_code",
    "product_code",
    "process_seq",
    "process_code",
    "equipment_id",
    "process_ts",
    "shift_code",
    "input_qty",
    "output_qty",
    "defect_qty",
    "defect_code",
    "scenario_id",
    "param_1",
    "param_2",
    "param_3",
    "param_4",
    "param_5",
]
SCENARIO_COLUMNS = [
    "scenario_id",
    "title",
    "line_code",
    "process_code",
    "equipment_id",
    "param_code",
    "param_name",
    "unit",
    "start_ts",
    "end_ts",
    "ramp_hours",
    "magnitude_sigma",
    "magnitude_value",
    "yield_penalty_pp",
    "yield_lag_hours",
    "defect_code",
    "owner_role",
    "expected_rule",
    "step_row_cnt",
    "lot_cnt",
]

ACTION_BY_PROCESS = {
    "GRIND": "核对砂轮磨损与冷却液温度，抽检厚度；必要时更换砂轮",
    "DICE": "检查划片刀磨损与切割道宽度，抽检崩边与裂纹",
    "DIE_ATTACH": "核对键合温度与贴装偏移，复测空洞率；必要时停线校准",
    "WIRE_BOND": "检查键合力与弧高，抽检拉力与线弧形态",
    "MOLD": "校验模压压力与保压曲线，抽检塑封空洞率",
    "TEST": "校验测试接触电阻与重测率，检查测试座与探针",
}


def _write_json(path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)


# --------------------------------------------------------------------------- #
# ODS
# --------------------------------------------------------------------------- #
def build_mes_ods(conn, cfg: dict) -> int:
    run_sql_file(conn, "sql/10_mes_ods.sql")
    data = load_mes_source(cfg)
    bulk_insert(conn, data["lots"], "ods_mes_lot", LOT_COLUMNS)
    bulk_insert(conn, data["process"], "ods_mes_process", PROCESS_COLUMNS)
    if data["scenarios"]:
        bulk_insert(conn, pd.DataFrame(data["scenarios"]), "ods_mes_scenario", SCENARIO_COLUMNS)
    stats = data["stats"]
    conn.execute(
        "INSERT OR REPLACE INTO ods_mes_asset VALUES (?,?,?,?,?,?)",
        (
            "mes_extract",
            cfg["mes"].get("source", "synthetic"),
            stats.get("seed"),
            int(len(data["process"])),
            stats.get("digest", ""),
            stats.get("generated_at") or datetime.now().isoformat(timespec="seconds"),
        ),
    )
    _write_json(
        path_of(cfg, "artifacts") / "mes_generation.json",
        {
            "disclaimer": "100% synthetic demo data. Real fab data is under NDA. "
            "Set mes.source=export to read a real MES export instead (docs/mes_schema.md).",
            "stats": stats,
            "scenarios": data["scenarios"],
        },
    )
    log.info("mes ods: %s lots, %s process rows", f"{len(data['lots']):,}", f"{len(data['process']):,}")
    return len(data["process"])


# --------------------------------------------------------------------------- #
# DWD
# --------------------------------------------------------------------------- #
def build_mes_dwd(conn, cfg: dict) -> int:
    run_sql_file(conn, "sql/11_mes_dwd.sql")
    spec = param_spec(cfg)
    bulk_insert(
        conn,
        spec,
        "dwd_mes_param_spec",
        [
            "process_code",
            "process_name",
            "process_seq",
            "param_index",
            "param_code",
            "param_name",
            "unit",
            "base_value",
            "sd_value",
            "is_key",
            "owner_role",
        ],
    )
    total = conn.execute("SELECT COUNT(*) FROM dwd_mes_process").fetchone()[0]
    inconsistent = conn.execute("SELECT COUNT(*) FROM dwd_mes_process WHERE is_qty_consistent = 0").fetchone()[0]
    log.info("mes dwd: %s rows, %s quantity inconsistencies", f"{total:,}", inconsistent)
    return int(total)


# --------------------------------------------------------------------------- #
# DWS
# --------------------------------------------------------------------------- #
def build_mes_dws(conn, cfg: dict) -> int:
    run_sql_file(conn, "sql/12_mes_dws.sql")
    key_specs = read_df(conn, "SELECT * FROM dwd_mes_param_spec WHERE is_key = 1 ORDER BY process_seq, param_index")
    param_cols = ", ".join(f"param_{i}" for i in range(1, 6))
    proc = read_df(
        conn,
        f"""
        SELECT line_code, product_code, process_code, equipment_id, shift_code,
               stat_date, stat_hour, input_qty, defect_qty, defect_code, {param_cols}
        FROM dwd_mes_process
        """,
    )

    # step x tool x shift x day
    daily = (
        proc.groupby(["line_code", "process_code", "equipment_id", "shift_code", "stat_date"], observed=True)
        .agg(lot_cnt=("input_qty", "size"), input_qty=("input_qty", "sum"), defect_qty=("defect_qty", "sum"))
        .reset_index()
    )
    daily["step_yield"] = 1.0 - daily["defect_qty"] / daily["input_qty"]
    bulk_insert(
        conn,
        daily,
        "dws_mes_process_daily",
        ["line_code", "process_code", "equipment_id", "shift_code", "stat_date", "lot_cnt", "input_qty", "defect_qty", "step_yield"],
    )

    # parameter x tool x hour (SPC input, key parameters only)
    hourly_parts = []
    for spec in key_specs.itertuples():
        column = f"param_{spec.param_index}"
        subset = proc.loc[proc["process_code"] == spec.process_code, ["line_code", "equipment_id", "stat_hour", column]]
        subset = subset.dropna(subset=[column])
        agg = (
            subset.groupby(["line_code", "equipment_id", "stat_hour"], observed=True)[column]
            .agg(sample_cnt="size", mean_value="mean", std_value="std")
            .reset_index()
            .assign(process_code=spec.process_code, param_index=spec.param_index)
        )
        hourly_parts.append(agg)
    hourly = pd.concat(hourly_parts, ignore_index=True)
    bulk_insert(
        conn,
        hourly,
        "dws_mes_param_hourly",
        ["line_code", "process_code", "equipment_id", "param_index", "stat_hour", "sample_cnt", "mean_value", "std_value"],
    )

    # defect pareto, last 14 days only (the demo window)
    last_date = proc["stat_date"].max()
    window_start = (pd.Timestamp(last_date) - pd.Timedelta(days=13)).strftime("%Y-%m-%d")
    recent = proc[proc["stat_date"] >= window_start]
    pareto = (
        recent.groupby(["stat_date", "line_code", "process_code", "equipment_id", "shift_code", "defect_code"], observed=True)
        .agg(defect_qty=("defect_qty", "sum"))
        .reset_index()
    )
    bulk_insert(
        conn,
        pareto,
        "dws_mes_defect_pareto",
        ["stat_date", "line_code", "process_code", "equipment_id", "shift_code", "defect_code", "defect_qty"],
    )
    log.info("mes dws: %s daily rows, %s hourly rows, %s pareto rows", f"{len(daily):,}", f"{len(hourly):,}", f"{len(pareto):,}")
    return int(len(daily) + len(hourly) + len(pareto))


# --------------------------------------------------------------------------- #
# ADS helpers
# --------------------------------------------------------------------------- #
def _scenario_windows(cfg: dict) -> pd.DataFrame:
    rows = []
    for scenario in cfg["mes"].get("scenarios", []):
        rows.append(
            {
                "scenario_id": scenario["id"],
                "line_code": scenario["line"],
                "process_code": scenario["process"],
                "equipment_id": scenario["equipment"],
                "window_start": pd.Timestamp(scenario["start_ts"]).strftime("%Y-%m-%dT%H"),
                "window_end": (pd.Timestamp(scenario["start_ts"]) + pd.Timedelta(hours=scenario["duration_hours"]))
                .strftime("%Y-%m-%dT%H"),
            }
        )
    return pd.DataFrame(rows)


def _compute_spc(conn, cfg: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    spc_cfg = cfg["mes"]["spc"]
    start = pd.Timestamp(cfg["mes"]["start_date"])
    baseline_end = (start + pd.Timedelta(days=int(spc_cfg["baseline_days"]))).strftime("%Y-%m-%dT%H")

    hourly = read_df(
        conn,
        """
        SELECT h.line_code, h.process_code, h.equipment_id, h.param_index, h.stat_hour,
               h.sample_cnt, h.mean_value, h.std_value,
               s.param_code, s.param_name, s.unit, s.owner_role
        FROM dws_mes_param_hourly h
        JOIN dwd_mes_param_spec s
          ON s.process_code = h.process_code AND s.param_index = h.param_index
        """,
    )
    min_samples = int(spc_cfg["min_samples_per_hour"])
    hourly = hourly[hourly["sample_cnt"] >= min_samples].copy()

    group_cols = ["line_code", "process_code", "equipment_id", "param_index"]
    limits = spc.baseline_limits(hourly[hourly["stat_hour"] < baseline_end], group_cols)
    scored = spc.standardise(hourly, limits, group_cols, sigma=float(spc_cfg["sigma_limit"]))
    scored = scored[scored["stat_hour"] >= baseline_end]

    windows = _scenario_windows(cfg)
    if not windows.empty:
        scored = scored.merge(
            windows[["scenario_id", "line_code", "process_code", "equipment_id", "window_start", "window_end"]],
            on=["line_code", "process_code", "equipment_id"],
            how="left",
        )
        inside = (scored["stat_hour"] >= scored["window_start"]) & (scored["stat_hour"] < scored["window_end"])
        scored["scenario_id"] = np.where(inside, scored["scenario_id"], "")
        scored = scored.drop(columns=["window_start", "window_end"])
    else:
        scored["scenario_id"] = ""

    alerts = []
    for keys, series in scored.groupby(group_cols, observed=True, sort=False):
        series = series.sort_values("stat_hour")
        hits = spc.detect_alerts(
            series["z_score"].to_numpy(),
            sigma=float(spc_cfg["sigma_limit"]),
            sustained=int(spc_cfg["sustained_points"]),
            run_length=int(spc_cfg["run_length"]),
            run_mean_sigma=float(spc_cfg["run_mean_sigma"]),
        )
        for hit in hits:
            row = series.iloc[hit["index"]]
            alerts.append(
                {
                    "alert_ts": row["stat_hour"],
                    "line_code": keys[0],
                    "process_code": keys[1],
                    "equipment_id": keys[2],
                    "param_index": int(keys[3]),
                    "param_code": row["param_code"],
                    "param_name": row["param_name"],
                    "unit": row["unit"],
                    "rule_code": hit["rule"],
                    "severity": hit["severity"],
                    "z_score": round(float(hit["z"]), 2),
                    "observed_value": float(row["mean_value"]),
                    "base_mean": float(row["base_mean"]),
                    "ucl": float(row["ucl"]),
                    "lcl": float(row["lcl"]),
                    "owner_role": row["owner_role"],
                    "scenario_id": row["scenario_id"],
                    "sustained": int(spc_cfg["sustained_points"]),
                }
            )

    alert_frame = pd.DataFrame(alerts)
    if not alert_frame.empty:
        alert_frame = alert_frame.sort_values(["alert_ts", "line_code", "equipment_id"]).reset_index(drop=True)
        alert_frame["alert_id"] = [f"ALT-{i + 1:03d}" for i in range(len(alert_frame))]
    return scored, alert_frame


def _alert_evidence(row) -> tuple[str, str]:
    if row["rule_code"] == spc.RULE_HIGH:
        evidence = (
            f"{row['param_name']} 连续 {row['sustained']} 小时超出 ±3σ：最新小时均值 "
            f"{row['observed_value']:.2f}{row['unit']}，基线 {row['base_mean']:.2f}{row['unit']}，"
            f"z={row['z_score']:+.1f}（UCL {row['ucl']:.2f} / LCL {row['lcl']:.2f}）"
        )
    else:
        evidence = (
            f"{row['param_name']} 连续 7 小时位于基线一侧且均值偏移 {abs(row['z_score']):.1f}σ："
            f"小时均值 {row['observed_value']:.2f}{row['unit']}，基线 {row['base_mean']:.2f}{row['unit']}"
        )
    action = ACTION_BY_PROCESS.get(row["process_code"], "复核该工序参数与设备状态") + f"（{row['equipment_id']}）"
    return evidence, action


def _suspect_lots(conn, alert: pd.Series, lookback_hours: int) -> pd.DataFrame:
    start = (pd.Timestamp(alert["alert_ts"]) - pd.Timedelta(hours=lookback_hours)).strftime("%Y-%m-%dT%H:%M:%S")
    end = (pd.Timestamp(alert["alert_ts"]) + pd.Timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    frame = read_df(
        conn,
        """
        SELECT lot_id, product_code, process_ts, shift_code, input_qty, defect_qty, step_yield
        FROM dwd_mes_process
        WHERE equipment_id = ? AND process_code = ? AND process_ts >= ? AND process_ts <= ?
        ORDER BY defect_qty DESC, process_ts DESC
        LIMIT 20
        """,
        (alert["equipment_id"], alert["process_code"], start, end),
    )
    frame.insert(0, "alert_id", alert["alert_id"])
    frame["suggested_action"] = ACTION_BY_PROCESS.get(alert["process_code"], "复核参数") + "，复测后放行"
    return frame


def _hourly_step_yield(conn, line_code: str, process_code: str, equipment_id: str) -> pd.DataFrame:
    return read_df(
        conn,
        """
        SELECT stat_hour, SUM(input_qty) AS input_qty, SUM(defect_qty) AS defect_qty,
               COUNT(*) AS lot_cnt, 1.0 - 1.0 * SUM(defect_qty) / SUM(input_qty) AS step_yield
        FROM dwd_mes_process
        WHERE line_code = ? AND process_code = ? AND equipment_id = ?
        GROUP BY stat_hour
        ORDER BY stat_hour
        """,
        (line_code, process_code, equipment_id),
    )


def _hourly_final_yield(conn, line_code: str, process_code: str, equipment_id: str) -> pd.DataFrame:
    """Final yield of lots that ran through the alerted tool, bucketed by TEST hour."""
    return read_df(
        conn,
        """
        SELECT substr(t.process_ts, 1, 13) AS stat_hour,
               AVG(l.yield_final) AS yield_final,
               COUNT(*) AS lot_cnt
        FROM dwd_mes_lot_route l
        JOIN dwd_mes_process d
          ON d.lot_id = l.lot_id AND d.process_code = ?
        JOIN dwd_mes_process t
          ON t.lot_id = l.lot_id AND t.process_code = 'TEST'
        WHERE d.line_code = ? AND d.equipment_id = ?
        GROUP BY substr(t.process_ts, 1, 13)
        ORDER BY stat_hour
        """,
        (process_code, line_code, equipment_id),
    )


def _shift_of(ts: str) -> str:
    """Map an ISO hour to the 8-hour shift that owns it (DAY/SWING/NIGHT)."""
    hour = int(str(ts)[11:13])
    if 8 <= hour < 16:
        return "DAY"
    if 16 <= hour < 24:
        return "SWING"
    return "NIGHT"


def _verify_scenarios(conn, cfg: dict, alerts: pd.DataFrame) -> list[dict]:
    scenarios = read_df(conn, "SELECT * FROM ods_mes_scenario ORDER BY scenario_id")
    spec = read_df(conn, "SELECT process_code, param_code, param_index FROM dwd_mes_param_spec")
    scenarios = scenarios.merge(spec, on=["process_code", "param_code"], how="left")
    results = []
    for scenario in scenarios.itertuples():
        start = pd.Timestamp(scenario.start_ts)
        end = pd.Timestamp(scenario.end_ts)
        baseline_start = (start - pd.Timedelta(days=14)).strftime("%Y-%m-%d")

        matched = alerts[alerts["scenario_id"] == scenario.scenario_id] if not alerts.empty else pd.DataFrame()
        detected_rule = None
        detected_ts = None
        detection_lag = None
        if not matched.empty:
            first = matched.sort_values("alert_ts").iloc[0]
            detected_rule = first["rule_code"]
            detected_ts = first["alert_ts"]
            detection_lag = round((pd.Timestamp(detected_ts) - start).total_seconds() / 3600, 1)

        series = _hourly_step_yield(conn, scenario.line_code, scenario.process_code, scenario.equipment_id)
        series["hour_ts"] = pd.to_datetime(series["stat_hour"], format="%Y-%m-%dT%H")
        base_mask = (series["hour_ts"] >= baseline_start) & (series["hour_ts"] < start)
        # averages use the peak of the drift (last 24 h); the first drop is
        # searched over the whole window so the causal chain keeps its timing
        peak_start = max(start, end - pd.Timedelta(hours=24))
        window_mask = (series["hour_ts"] >= peak_start) & (series["hour_ts"] < end)
        observed_mask = (series["hour_ts"] >= start) & (series["hour_ts"] < end)
        base = series[base_mask]
        window = series[window_mask]
        observed = series[observed_mask]
        base_yield = float(base["step_yield"].mean()) if not base.empty else float("nan")
        window_yield = float(window["step_yield"].mean()) if not window.empty else float("nan")
        step_drop = round((window_yield - base_yield) * 100, 2) if base_yield == base_yield else None
        first_drop_ts = None
        if not base.empty and not observed.empty:
            sigma = float(base["step_yield"].std(ddof=0) or 0.0)
            if sigma > 0:
                z = (observed["step_yield"] - base_yield) / sigma
                hits = observed.loc[z <= -3.0, "stat_hour"]
                first_drop_ts = str(hits.iloc[0]) if not hits.empty else None

        final_series = _hourly_final_yield(conn, scenario.line_code, scenario.process_code, scenario.equipment_id)
        final_series["hour_ts"] = pd.to_datetime(final_series["stat_hour"], format="%Y-%m-%dT%H")
        base_final = final_series[(final_series["hour_ts"] >= baseline_start) & (final_series["hour_ts"] < start)]
        window_final = final_series[(final_series["hour_ts"] >= peak_start) & (final_series["hour_ts"] < end)]
        observed_final = final_series[(final_series["hour_ts"] >= start) & (final_series["hour_ts"] < end)]
        final_base = float(base_final["yield_final"].mean()) if not base_final.empty else float("nan")
        final_window = float(window_final["yield_final"].mean()) if not window_final.empty else float("nan")
        final_drop = round((final_window - final_base) * 100, 2) if final_base == final_base else None
        first_final_drop = None
        if not base_final.empty and not observed_final.empty:
            final_sigma = float(base_final["yield_final"].std(ddof=0) or 0.0)
            if final_sigma > 0:
                final_z = (observed_final["yield_final"] - final_base) / final_sigma
                final_hits = observed_final.loc[final_z <= -3.0, "stat_hour"]
                first_final_drop = str(final_hits.iloc[0]) if not final_hits.empty else None

        first_breach = None
        breach_rows = read_df(
            conn,
            """
            SELECT stat_hour, z_score FROM ads_mes_spc
            WHERE line_code = ? AND process_code = ? AND equipment_id = ? AND param_index = ?
              AND stat_hour >= ? AND ABS(z_score) >= 3.0
            ORDER BY stat_hour LIMIT 1
            """,
            (
                scenario.line_code,
                scenario.process_code,
                scenario.equipment_id,
                int(scenario.param_index),
                start.strftime("%Y-%m-%dT%H"),
            ),
        )
        if not breach_rows.empty:
            first_breach = str(breach_rows.iloc[0]["stat_hour"])

        lag_limit = max(24.0, float(scenario.ramp_hours) + 12.0)
        checks = {
            "alert": detected_rule is not None,
            "rule": detected_rule == scenario.expected_rule,
            "step_drop": step_drop is not None and step_drop <= -0.8,
            "final_drop": final_drop is not None and final_drop <= -0.5,
            "directed": detection_lag is not None and 0 <= detection_lag <= lag_limit,
        }
        verdict = "MATCH" if all(checks.values()) else "MISMATCH"
        detail = {
            "title": scenario.title,
            "checks": checks,
            "chain": {
                "drift_start": scenario.start_ts,
                "first_param_breach": first_breach,
                "first_shift": _shift_of(first_drop_ts or first_breach or scenario.start_ts),
                "first_alert": detected_ts,
                "first_step_yield_drop": first_drop_ts,
                "first_final_yield_drop": first_final_drop,
            },
            "lag_hours": {
                "drift_to_param_breach": (
                    round((pd.Timestamp(first_breach) - start).total_seconds() / 3600, 1)
                    if first_breach
                    else None
                ),
                "drift_to_alert": detection_lag,
                "drift_to_step_yield_drop": (
                    round((pd.Timestamp(first_drop_ts) - start).total_seconds() / 3600, 1)
                    if first_drop_ts
                    else None
                ),
                "drift_to_final_yield_drop": (
                    round((pd.Timestamp(first_final_drop) - start).total_seconds() / 3600, 1)
                    if first_final_drop
                    else None
                ),
            },
        }
        results.append(
            {
                "scenario_id": scenario.scenario_id,
                "expected_rule": scenario.expected_rule,
                "detected_rule": detected_rule,
                "detected_ts": detected_ts,
                "detection_lag_hours": detection_lag,
                "first_breach_ts": first_breach,
                "first_step_drop_ts": first_drop_ts,
                "step_yield_base": round(base_yield, 5) if base_yield == base_yield else None,
                "step_yield_window": round(window_yield, 5) if window_yield == window_yield else None,
                "step_drop_pp": step_drop,
                "final_yield_base": round(final_base, 5) if final_base == final_base else None,
                "final_yield_window": round(final_window, 5) if final_window == final_window else None,
                "final_drop_pp": final_drop,
                "verdict": verdict,
                "detail": json.dumps(detail, ensure_ascii=False),
            }
        )
    return results


# --------------------------------------------------------------------------- #
# ADS
# --------------------------------------------------------------------------- #
def build_mes_ads(conn, cfg: dict) -> int:
    run_sql_file(conn, "sql/13_mes_ads.sql")
    scored, alerts = _compute_spc(conn, cfg)

    # keep 30 days of the scored series for the dashboard
    start = pd.Timestamp(cfg["mes"]["start_date"])
    data_end = start + pd.Timedelta(days=int(cfg["mes"]["days"]) - 1)
    window_start = (data_end - pd.Timedelta(days=29)).strftime("%Y-%m-%dT%H")
    recent = scored[scored["stat_hour"] >= window_start].copy()
    bulk_insert(
        conn,
        recent,
        "ads_mes_spc",
        [
            "line_code",
            "process_code",
            "equipment_id",
            "param_index",
            "param_code",
            "param_name",
            "unit",
            "stat_hour",
            "sample_cnt",
            "mean_value",
            "base_mean",
            "base_sd",
            "z_score",
            "ucl",
            "lcl",
            "point_breach",
            "scenario_id",
        ],
    )

    alert_rows = []
    suspect_frames = []
    if not alerts.empty:
        lookback = int(cfg["mes"]["suspect"]["lookback_hours"])
        for alert in alerts.to_dict("records"):
            evidence, action = _alert_evidence(alert)
            alert.update({"evidence": evidence, "suggested_action": action})
            alert_rows.append(alert)
            suspect_frames.append(_suspect_lots(conn, pd.Series(alert), lookback))
        alert_frame = pd.DataFrame(alert_rows)
        bulk_insert(
            conn,
            alert_frame,
            "ads_mes_alert",
            [
                "alert_id",
                "alert_ts",
                "line_code",
                "process_code",
                "equipment_id",
                "param_index",
                "param_code",
                "param_name",
                "unit",
                "rule_code",
                "severity",
                "z_score",
                "observed_value",
                "base_mean",
                "ucl",
                "lcl",
                "evidence",
                "suggested_action",
                "owner_role",
                "scenario_id",
            ],
        )
        suspects = pd.concat(suspect_frames, ignore_index=True)
        bulk_insert(
            conn,
            suspects,
            "ads_mes_suspect_lot",
            ["alert_id", "lot_id", "product_code", "process_ts", "shift_code", "input_qty", "defect_qty", "step_yield", "suggested_action"],
        )

    checks = _verify_scenarios(conn, cfg, alerts)
    if checks:
        bulk_insert(
            conn,
            pd.DataFrame(checks),
            "ads_mes_scenario_check",
            [
                "scenario_id",
                "expected_rule",
                "detected_rule",
                "detected_ts",
                "detection_lag_hours",
                "first_breach_ts",
                "first_step_drop_ts",
                "step_yield_base",
                "step_yield_window",
                "step_drop_pp",
                "final_yield_base",
                "final_yield_window",
                "final_drop_pp",
                "verdict",
                "detail",
            ],
        )

    # status / alert count per line-day
    if not alerts.empty:
        alert_days = alerts.assign(stat_date=alerts["alert_ts"].str[:10]).groupby(["line_code", "stat_date"]).size()
        for (line_code, stat_date), count in alert_days.items():
            conn.execute(
                "UPDATE ads_mes_kpi_daily SET alert_cnt = ? WHERE line_code = ? AND stat_date = ?",
                (int(count), line_code, stat_date),
            )
    conn.execute(
        """
        UPDATE ads_mes_kpi_daily
        SET status = CASE
            WHEN alert_cnt > 0 OR delta_pp <= -0.5 THEN 'alert'
            WHEN delta_pp <= -0.2 THEN 'watch'
            ELSE 'normal' END
        """
    )
    conn.commit()

    _write_json(
        path_of(cfg, "artifacts") / "mes_scenarios.json",
        {
            "disclaimer": "Synthetic demo data. Each scenario below proves that the injected "
            "anomaly and the dashboard alert line up.",
            "scenarios": checks,
        },
    )
    _write_json(path_of(cfg, "artifacts") / "mes_alerts.json", alert_rows)
    log.info("mes ads: %s spc rows, %s alerts, %s scenario checks", f"{len(recent):,}", len(alert_rows), len(checks))
    return int(len(recent) + len(alert_rows) + len(checks))
