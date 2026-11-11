"""Pure validation rules shared by the pipeline and the unit tests."""

from __future__ import annotations

import numpy as np
import pandas as pd


def label_domain_violations(labels: pd.Series, allowed: tuple[int, ...] = (-1, 1)) -> int:
    """Count labels outside the documented domain."""
    return int((~labels.isin(allowed)).sum())


def wm811k_validity(frame: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Physical consistency rules for a wafer-map feature row.

    Returns ``(is_valid, invalid_reason)`` as aligned Series.
    """
    reason = np.select(
        [
            (frame["rows"] <= 0) | (frame["cols"] <= 0),
            frame["total_die"] <= 0,
            frame["fail_die"] < 0,
            frame["fail_die"] > frame["total_die"],
            (frame["defect_density"] < 0) | (frame["defect_density"] > 1),
        ],
        ["bad_shape", "no_die", "negative_fail", "fail_gt_total", "density_out_of_range"],
        default="",
    )
    reason = pd.Series(reason, index=frame.index, dtype="object")
    return (reason == "").astype(int), reason


def missing_rate_summary(profile: pd.DataFrame, max_missing_rate: float = 0.40) -> dict:
    """Summarise completeness flags produced by ``sensor_profile``."""
    return {
        "max_missing_rate": float(profile["missing_rate"].max()) if len(profile) else 0.0,
        "high_missing_cnt": int((profile["missing_rate"] > max_missing_rate).sum()),
        "constant_cnt": int(profile["is_constant"].sum()),
        "usable_cnt": int(profile["is_usable"].sum()),
    }
