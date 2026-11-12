"""Warehouse builder: ODS -> DWD -> DWS -> ADS.

Idempotency
-----------
Every layer rebuilds from its upstream layer, so re-running the whole pipeline
produces the same database. A ``meta_pipeline_run`` row records the run; a
``meta_pipeline_step`` row records progress. With ``resume=True`` a partially
finished run with the same data fingerprint continues from the failed step
instead of redoing successful work.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from src.config import config_fingerprint, path_of
from src.db import connect, read_df, run_sql_file, write_df
from src.features.build import (
    add_derived_features,
    assign_time_split,
    clean_sensor_matrix,
    sensor_profile,
    time_split_boundaries,
)
from src.ingest import secom, wm811k
from src.paths import REPO_ROOT
from src.quality.rules import wm811k_validity

log = logging.getLogger(__name__)

STEPS = ("ods", "dwd", "dws", "ads", "mes_ods", "mes_dwd", "mes_dws", "mes_ads")
DERIVED_FEATURES = ["z_mean", "z_std", "z_max_abs", "abnormal_cnt"]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result.stdout.strip() or "unknown"
    except OSError:
        return "unknown"


def data_fingerprint(cfg: dict) -> str:
    parts = [
        config_fingerprint(cfg),
        cfg["secom"]["archive_sha256"].upper(),
        cfg["wm811k"]["npz_sha256"].upper(),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# run bookkeeping
# --------------------------------------------------------------------------- #
def _prepare_run(conn, fingerprint: str, resume: bool) -> str:
    if resume:
        row = conn.execute(
            """
            SELECT run_id FROM meta_pipeline_run
            WHERE fingerprint = ? AND status != 'success'
            ORDER BY started_at DESC LIMIT 1
            """,
            (fingerprint,),
        ).fetchone()
        if row:
            log.info("resuming run %s (fingerprint %s)", row["run_id"], fingerprint)
            return row["run_id"]
    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    conn.execute(
        "INSERT INTO meta_pipeline_run (run_id, fingerprint, status, started_at, git_commit) VALUES (?,?,?,?,?)",
        (run_id, fingerprint, "running", _now(), _git_commit()),
    )
    return run_id


def _step_success(conn, run_id: str, step: str) -> bool:
    row = conn.execute(
        "SELECT status FROM meta_pipeline_step WHERE run_id = ? AND step = ?", (run_id, step)
    ).fetchone()
    return bool(row and row["status"] == "success")


def _step_start(conn, run_id: str, step: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta_pipeline_step (run_id, step, status, started_at) VALUES (?,?,?,?)",
        (run_id, step, "running", _now()),
    )


def _step_finish(conn, run_id: str, step: str, status: str, row_cnt: int, message: str = "") -> None:
    conn.execute(
        """
        UPDATE meta_pipeline_step
        SET status = ?, row_cnt = ?, message = ?, finished_at = ?
        WHERE run_id = ? AND step = ?
        """,
        (status, row_cnt, message, _now(), run_id, step),
    )
    # Persist each step so a crash later does not discard completed work.
    conn.commit()


# --------------------------------------------------------------------------- #
# ODS
# --------------------------------------------------------------------------- #
def _build_ods(conn, cfg: dict) -> int:
    run_sql_file(conn, "sql/00_ods.sql")
    counts = secom.build_ods(conn, cfg)
    counts.update(wm811k.build_ods(conn, cfg))

    raw_dir = path_of(cfg, "raw_dir")
    assets = [
        ("secom.zip", cfg["secom"]["download_url"], cfg["secom"]["archive_sha256"], raw_dir / "secom.zip"),
        ("wm811k_labeled.npz", cfg["wm811k"]["download_url"], cfg["wm811k"]["npz_sha256"], raw_dir / "wm811k_labeled.npz"),
        (
            "representative_wafers.json",
            cfg["wm811k"]["representative_url"],
            cfg["wm811k"]["representative_sha256"],
            raw_dir / "representative_wafers.json",
        ),
    ]
    now = _now()
    for name, url, digest, path in assets:
        conn.execute(
            "INSERT OR REPLACE INTO ods_asset VALUES (?,?,?,?,?)",
            (name, url, digest.upper(), path.stat().st_size if path.exists() else 0, now),
        )
    return sum(counts.values())


# --------------------------------------------------------------------------- #
# DWD
# --------------------------------------------------------------------------- #
def _load_wide(conn) -> tuple[pd.DataFrame, pd.DataFrame]:
    labels = read_df(conn, "SELECT wafer_id, measurement_ts, label_raw FROM ods_secom_wafer")
    labels["measurement_ts"] = pd.to_datetime(labels["measurement_ts"])
    labels = labels.sort_values("measurement_ts").reset_index(drop=True)

    values = read_df(conn, "SELECT wafer_id, sensor_id, raw_value FROM ods_secom_sensor_value")
    wide = values.pivot(index="wafer_id", columns="sensor_id", values="raw_value")
    wide = wide.reindex(sorted(wide.columns), axis=1).reindex(labels["wafer_id"])
    return wide, labels


def _build_dwd(conn, cfg: dict) -> int:
    run_sql_file(conn, "sql/01_dwd.sql")
    wide, labels = _load_wide(conn)

    split_cfg = cfg["secom"]["split"]
    train_end, _ = time_split_boundaries(labels["measurement_ts"], split_cfg)
    train_mask = pd.Series(
        (labels["measurement_ts"] <= train_end).to_numpy(), index=labels["wafer_id"].to_numpy(), name="is_train"
    )
    log.info("DWD profile window: %s .. %s (%d wafers)", labels.measurement_ts.min(), train_end, int(train_mask.sum()))

    quality = cfg["secom"]["quality"]
    profile = sensor_profile(
        wide,
        train_mask,
        max_missing_rate=quality["max_missing_rate"],
        min_unique_values=quality["min_unique_values"],
    )
    write_df(conn, profile, "dwd_sensor_profile", if_exists="append")

    clean, imputed, outlier = clean_sensor_matrix(wide, profile, outlier_z=quality["outlier_z"])
    flags = imputed.astype("int8") + 2 * outlier.astype("int8")
    long = clean.reset_index().melt(id_vars="wafer_id", var_name="sensor_id", value_name="value_clean")
    flag_long = flags.reset_index().melt(id_vars="wafer_id", var_name="sensor_id", value_name="flag")
    long = long.merge(flag_long, on=["wafer_id", "sensor_id"], how="left")
    flag_codes = long.pop("flag")
    long["is_imputed"] = (flag_codes % 2).astype(int)
    long["is_outlier"] = (flag_codes // 2).astype(int)

    dwd_labels = pd.DataFrame(
        {
            "wafer_id": labels["wafer_id"],
            "measurement_ts": labels["measurement_ts"].dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "stat_date": labels["measurement_ts"].dt.strftime("%Y-%m-%d"),
            "is_fail": (labels["label_raw"] == secom.LABEL_FAIL).astype(int),
            "load_batch_id": "ods-secom",
        }
    )
    # Parent rows must exist before the foreign-keyed sensor facts.
    write_df(conn, dwd_labels, "dwd_wafer_label", if_exists="append")
    write_df(conn, long[["wafer_id", "sensor_id", "value_clean", "is_imputed", "is_outlier"]], "dwd_sensor_value")


    # --- WM-811K row-level validation --------------------------------------
    wm = read_df(conn, "SELECT * FROM ods_wm811k_wafer")
    wm["is_valid"], wm["invalid_reason"] = wm811k_validity(wm)
    columns = [
        "source_index",
        "label_code",
        "label_name",
        "split_original",
        "rows",
        "cols",
        "total_die",
        "fail_die",
        "wafer_yield",
        "defect_density",
        "center_defect_ratio",
        "mid_defect_ratio",
        "edge_defect_ratio",
        "edge_to_center_ratio",
        "quadrant_imbalance",
        "left_defect_ratio",
        "right_defect_ratio",
        "top_defect_ratio",
        "bottom_defect_ratio",
        "defect_centroid_x",
        "defect_centroid_y",
        "defect_spread_x",
        "defect_spread_y",
        "radial_mean",
        "radial_std",
        "cluster_count",
        "largest_cluster_ratio",
        "die_size",
        "wafer_index",
        "is_valid",
        "invalid_reason",
    ]
    write_df(conn, wm[columns], "dwd_wm811k_wafer", if_exists="append")
    return len(long) + len(dwd_labels) + len(wm)


# --------------------------------------------------------------------------- #
# DWS
# --------------------------------------------------------------------------- #
def _build_dws(conn, cfg: dict) -> int:
    run_sql_file(conn, "sql/02_dws.sql")

    conn.execute(
        """
        INSERT INTO dws_wafer_summary
        SELECT
            l.wafer_id,
            l.measurement_ts,
            l.stat_date,
            l.is_fail,
            COUNT(v.sensor_id),
            SUM(CASE WHEN v.is_imputed = 1 THEN 1 ELSE 0 END),
            SUM(v.is_imputed),
            SUM(v.is_outlier),
            AVG(v.is_imputed),
            AVG(v.is_outlier)
        FROM dwd_wafer_label l
        JOIN dwd_sensor_value v ON v.wafer_id = l.wafer_id
        GROUP BY l.wafer_id, l.measurement_ts, l.stat_date, l.is_fail
        """
    )

    usable = read_df(conn, "SELECT sensor_id FROM dwd_sensor_profile WHERE is_usable = 1")["sensor_id"].tolist()
    placeholders = ",".join("?" for _ in usable)
    values = read_df(
        conn,
        f"""
        SELECT v.sensor_id, l.stat_date, v.value_clean, v.is_imputed
        FROM dwd_sensor_value v
        JOIN dwd_wafer_label l ON l.wafer_id = v.wafer_id
        WHERE v.sensor_id IN ({placeholders})
        """,
        tuple(usable),
    )
    grouped = values.groupby(["sensor_id", "stat_date"])
    daily = grouped.agg(
        wafer_cnt=("value_clean", "count"),
        mean_value=("value_clean", "mean"),
        std_value=("value_clean", "std"),
        missing_rate=("is_imputed", "mean"),
    ).reset_index()
    quantiles = grouped["value_clean"].quantile([0.05, 0.95]).unstack()
    quantiles.columns = ["p05", "p95"]
    daily = daily.merge(quantiles.reset_index(), on=["sensor_id", "stat_date"], how="left")
    write_df(conn, daily, "dws_sensor_daily", if_exists="append")

    conn.execute(
        """
        INSERT INTO dws_defect_class_dist
        SELECT label_name, MIN(label_code), COUNT(*),
               COUNT(*) * 1.0 / (SELECT COUNT(*) FROM ods_wm811k_wafer)
        FROM ods_wm811k_wafer
        GROUP BY label_name
        """
    )
    conn.execute(
        """
        INSERT INTO dws_wm811k_split
        SELECT split_original, label_name, COUNT(*)
        FROM ods_wm811k_wafer
        GROUP BY split_original, label_name
        """
    )
    return int(len(daily) + len(values))


# --------------------------------------------------------------------------- #
# ADS
# --------------------------------------------------------------------------- #
def _create_feature_table(conn, feature_columns: list[str]) -> None:
    definitions = "\n    , ".join(f'"{column}" REAL' for column in feature_columns)
    ddl = f"""
    -- Generated by src/warehouse/build.py: one row per wafer, one column per
    -- usable sensor plus derived wafer-level signals. Sensor ids are quoted
    -- identifiers so they survive schema changes.
    DROP TABLE IF EXISTS ads_yield_feature_matrix;
    CREATE TABLE ads_yield_feature_matrix (
        wafer_id        TEXT PRIMARY KEY,
        measurement_ts  TEXT NOT NULL,
        stat_date       TEXT NOT NULL,
        split           TEXT NOT NULL,
        is_fail         INTEGER NOT NULL,
        {definitions}
    );
    """
    conn.executescript(ddl)


def _build_ads(conn, cfg: dict) -> int:
    run_sql_file(conn, "sql/03_ads.sql")
    labels = read_df(conn, "SELECT wafer_id, measurement_ts, stat_date, is_fail FROM dwd_wafer_label")
    profile = read_df(conn, "SELECT * FROM dwd_sensor_profile")
    usable = profile.loc[profile["is_usable"] == 1, "sensor_id"].tolist()

    placeholders = ",".join("?" for _ in usable)
    values = read_df(
        conn,
        f"SELECT wafer_id, sensor_id, value_clean FROM dwd_sensor_value WHERE sensor_id IN ({placeholders})",
        tuple(usable),
    )
    wide = values.pivot(index="wafer_id", columns="sensor_id", values="value_clean").reindex(columns=usable)
    wide = wide.reindex(labels["wafer_id"])

    derived = add_derived_features(wide, profile)
    split = assign_time_split(pd.to_datetime(labels["measurement_ts"]), cfg["secom"]["split"])

    matrix = pd.concat(
        [
            labels[["wafer_id", "measurement_ts", "stat_date", "is_fail"]],
            split.rename("split"),
            derived.reset_index(drop=True),
            wide.reset_index(drop=True),
        ],
        axis=1,
    )

    feature_columns = DERIVED_FEATURES + usable
    _create_feature_table(conn, feature_columns)
    write_df(conn, matrix, "ads_yield_feature_matrix", if_exists="append", chunksize=50)

    train_end, valid_end = time_split_boundaries(pd.to_datetime(labels["measurement_ts"]), cfg["secom"]["split"])
    conn.execute(
        "INSERT OR REPLACE INTO ads_training_manifest VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            _latest_run_id(conn),
            "secom",
            len(matrix),
            len(feature_columns),
            int((split == "train").sum()),
            int((split == "valid").sum()),
            int((split == "test").sum()),
            str(train_end),
            str(valid_end),
            json.dumps(feature_columns),
            _now(),
        ),
    )

    # Actual yield trend; risk columns are filled after model training.
    trend = (
        labels.assign(is_fail=labels["is_fail"].astype(int))
        .groupby("stat_date")
        .agg(wafer_cnt=("wafer_id", "count"), fail_cnt=("is_fail", "sum"))
        .reset_index()
    )
    trend["actual_yield"] = 1 - trend["fail_cnt"] / trend["wafer_cnt"]
    trend["avg_risk"] = np.nan
    trend["high_risk_cnt"] = np.nan
    write_df(
        conn,
        trend[["stat_date", "wafer_cnt", "fail_cnt", "actual_yield", "avg_risk", "high_risk_cnt"]],
        "ads_yield_trend_daily",
        if_exists="append",
    )

    # Sensor drift vs training baseline (SPC-style 3-sigma limits).
    daily = read_df(conn, "SELECT * FROM dws_sensor_daily")
    baseline = profile.set_index("sensor_id")[["mean_value", "std_value"]].rename(
        columns={"mean_value": "base_mean", "std_value": "base_std"}
    )
    drift = daily.merge(baseline, left_on="sensor_id", right_index=True, how="inner")
    drift["base_std"] = drift["base_std"].replace(0, np.nan)
    drift["z_score"] = (drift["mean_value"] - drift["base_mean"]) / drift["base_std"]
    drift["control_upper"] = drift["base_mean"] + 3 * drift["base_std"]
    drift["control_lower"] = drift["base_mean"] - 3 * drift["base_std"]
    drift["is_drift"] = (drift["z_score"].abs() >= 3).astype(int)
    write_df(
        conn,
        drift[["sensor_id", "stat_date", "mean_value", "z_score", "control_upper", "control_lower", "is_drift"]],
        "ads_sensor_drift",
        if_exists="append",
    )
    return len(matrix) + len(trend) + len(drift)


def _build_mes_ods(conn, cfg: dict) -> int:
    from src.warehouse.mes import build_mes_ods

    return build_mes_ods(conn, cfg)


def _build_mes_dwd(conn, cfg: dict) -> int:
    from src.warehouse.mes import build_mes_dwd

    return build_mes_dwd(conn, cfg)


def _build_mes_dws(conn, cfg: dict) -> int:
    from src.warehouse.mes import build_mes_dws

    return build_mes_dws(conn, cfg)


def _build_mes_ads(conn, cfg: dict) -> int:
    from src.warehouse.mes import build_mes_ads

    return build_mes_ads(conn, cfg)


def _latest_run_id(conn) -> str:
    row = conn.execute("SELECT run_id FROM meta_pipeline_run ORDER BY started_at DESC LIMIT 1").fetchone()
    return row["run_id"] if row else "unknown"


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #
def build_warehouse(cfg: dict, *, resume: bool = True) -> dict:
    db_path = path_of(cfg, "warehouse")
    fingerprint = data_fingerprint(cfg)
    builders = {
        "ods": _build_ods,
        "dwd": _build_dwd,
        "dws": _build_dws,
        "ads": _build_ads,
        "mes_ods": _build_mes_ods,
        "mes_dwd": _build_mes_dwd,
        "mes_dws": _build_mes_dws,
        "mes_ads": _build_mes_ads,
    }
    summary: dict[str, int] = {}

    with connect(db_path) as conn:
        run_sql_file(conn, "sql/99_meta.sql")
        run_id = _prepare_run(conn, fingerprint, resume)
        for step in STEPS:
            if resume and _step_success(conn, run_id, step):
                log.info("step %s already succeeded in run %s, skipping", step, run_id)
                summary[step] = 0
                continue
            _step_start(conn, run_id, step)
            try:
                rows = builders[step](conn, cfg)
            except Exception as exc:  # noqa: BLE001 - fail the run but keep context
                _step_finish(conn, run_id, step, "failed", 0, str(exc))
                conn.execute(
                    "UPDATE meta_pipeline_run SET status = 'failed', finished_at = ? WHERE run_id = ?",
                    (_now(), run_id),
                )
                conn.commit()
                log.exception("step %s failed", step)
                raise
            _step_finish(conn, run_id, step, "success", rows)
            summary[step] = rows
            log.info("step %s done: %s rows", step, f"{rows:,}")

        conn.execute(
            "UPDATE meta_pipeline_run SET status = 'success', finished_at = ? WHERE run_id = ?",
            (_now(), run_id),
        )
    summary["run_id"] = run_id
    summary["db_path"] = str(Path(db_path))
    return summary
