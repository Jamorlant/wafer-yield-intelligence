"""SECOM ingestion: raw files -> ODS tables (long format)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from src.config import path_of
from src.db import write_df

log = logging.getLogger(__name__)

LABEL_PASS = -1
LABEL_FAIL = 1


def sensor_name(index: int) -> str:
    return f"S{index + 1:03d}"


def load_raw(raw_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read ``secom.data`` and ``secom_labels.data`` without changing semantics.

    Returns
    -------
    features : DataFrame (n_wafers x 590), columns S001..S590, NaN = missing
    labels   : DataFrame with ``label_raw`` and parsed ``measurement_ts``
    """
    secom_dir = raw_dir / "secom"
    features = pd.read_csv(secom_dir / "secom.data", sep=r"\s+", header=None, na_values="NaN")
    labels = pd.read_csv(
        secom_dir / "secom_labels.data",
        sep=r"\s+",
        header=None,
        names=["label_raw", "measurement_ts"],
        quotechar='"',
    )
    if features.shape[0] != labels.shape[0]:
        raise ValueError(f"row mismatch: features={features.shape[0]} labels={labels.shape[0]}")

    features.columns = [sensor_name(i) for i in range(features.shape[1])]
    labels["measurement_ts"] = pd.to_datetime(labels["measurement_ts"], format="%d/%m/%Y %H:%M:%S")
    labels["wafer_id"] = [f"W{i + 1:05d}" for i in range(len(labels))]
    return features, labels


def build_ods(conn, cfg: dict, batch_id: str | None = None) -> dict[str, int]:
    raw_dir = path_of(cfg, "raw_dir")
    features, labels = load_raw(raw_dir)

    expected_rows = cfg["secom"]["expected_rows"]
    expected_sensors = cfg["secom"]["expected_sensors"]
    if features.shape != (expected_rows, expected_sensors):
        raise ValueError(f"unexpected SECOM shape {features.shape}, expected {(expected_rows, expected_sensors)}")

    batch_id = batch_id or f"secom-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    now = datetime.now(UTC).isoformat(timespec="seconds")

    wafers = pd.DataFrame(
        {
            "wafer_id": labels["wafer_id"],
            "measurement_ts": labels["measurement_ts"].dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "label_raw": labels["label_raw"].astype(int),
            "load_batch_id": batch_id,
            "ingested_at": now,
        }
    )
    write_df(conn, wafers, "ods_secom_wafer", if_exists="append")

    melted = (
        features.assign(wafer_id=labels["wafer_id"].to_numpy())
        .melt(id_vars="wafer_id", var_name="sensor_id", value_name="raw_value")
        .loc[:, ["wafer_id", "sensor_id", "raw_value"]]
    )
    log.info("writing %s sensor values to ODS", f"{len(melted):,}")
    write_df(conn, melted, "ods_secom_sensor_value", if_exists="append")

    return {"ods_secom_wafer": len(wafers), "ods_secom_sensor_value": len(melted)}
