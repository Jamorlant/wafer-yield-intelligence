"""WM-811K derived-feature ingestion (9-class wafer defect patterns)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from src.config import path_of
from src.db import write_df

log = logging.getLogger(__name__)

# 26 coordinate features extracted from every wafer map in LSWMD.pkl.
# Provenance: CC0 WM-811K dataset -> derived subset published in
# https://github.com/ililllilll/WM811K (sha256 pinned in configs/config.yaml).
FEATURES = [
    "rows",
    "cols",
    "total_grid",
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
]

# Column names expected by sql/00_ods.sql (dieSize / waferIndex were renamed).
ODS_RENAME = {"die_size": "die_size", "wafer_index": "wafer_index"}


def load_labeled(raw_dir: Path, class_names: list[str]) -> pd.DataFrame:
    npz_path = raw_dir / "wm811k_labeled.npz"
    data = np.load(npz_path, allow_pickle=True)
    frame = pd.DataFrame(data["X"], columns=FEATURES)
    frame.insert(0, "source_index", np.asarray(data["source_index"], dtype=np.int64))
    frame["label_code"] = np.asarray(data["y"], dtype=int)
    frame["label_name"] = frame["label_code"].map(lambda code: class_names[int(code)])
    frame["split_original"] = np.where(np.asarray(data["train_test"]) == 0, "Training", "Test")
    return frame


def build_ods(conn, cfg: dict) -> dict[str, int]:
    raw_dir = path_of(cfg, "raw_dir")
    class_names = cfg["wm811k"]["class_names"]
    frame = load_labeled(raw_dir, class_names)

    expected = {0: "none", 1: "Center", 2: "Donut", 3: "Edge-Loc", 4: "Edge-Ring", 5: "Loc", 6: "Random", 7: "Scratch", 8: "Near-full"}
    observed = frame.groupby("label_code")["label_name"].first().to_dict()
    if observed != expected:
        raise ValueError(f"unexpected WM-811K class mapping: {observed}")

    ods_columns = [
        "source_index",
        "label_code",
        "label_name",
        "split_original",
        *FEATURES,
    ]
    write_df(conn, frame[ods_columns], "ods_wm811k_wafer", if_exists="append")
    return {"ods_wm811k_wafer": len(frame)}


def load_representative_maps(raw_dir: Path) -> list[dict]:
    """Nine RLE-encoded wafer maps used only for dashboard rendering."""
    with open(raw_dir / "representative_wafers.json", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload["wafers"]
