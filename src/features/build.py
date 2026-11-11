"""Pure feature-engineering functions (unit-testable, no database access)."""

from __future__ import annotations

import numpy as np
import pandas as pd

TRAIN_SPLIT = "train"
VALID_SPLIT = "valid"
TEST_SPLIT = "test"


def sensor_profile(
    wide: pd.DataFrame,
    train_mask: pd.Series,
    *,
    max_missing_rate: float = 0.40,
    min_unique_values: int = 2,
) -> pd.DataFrame:
    """Profile sensor columns.

    All distribution statistics are computed on the **training period only** so
    that imputation and winsorising never use future information.
    """
    train = wide.loc[train_mask]
    profile = pd.DataFrame(
        {
            "total_cnt": len(wide),
            "missing_cnt": wide.isna().sum(),
            "missing_rate": wide.isna().mean(),
            "unique_cnt": wide.nunique(dropna=True),
            "mean_value": train.mean(),
            "std_value": train.std(),
            "median_value": train.median(),
            "min_value": train.min(),
            "max_value": train.max(),
            "p01": train.quantile(0.01),
            "p99": train.quantile(0.99),
        }
    )
    profile.index.name = "sensor_id"
    profile["is_constant"] = (profile["unique_cnt"] < min_unique_values).astype(int)
    profile["is_high_missing"] = (profile["missing_rate"] > max_missing_rate).astype(int)
    profile["is_usable"] = (
        (profile["is_constant"] == 0) & (profile["is_high_missing"] == 0) & (profile["std_value"].fillna(0) > 0)
    ).astype(int)
    return profile.reset_index()


def clean_sensor_matrix(
    wide: pd.DataFrame,
    profile: pd.DataFrame,
    *,
    outlier_z: float = 6.0,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Median-impute and winsorise sensor values.

    Returns ``(clean, imputed, outlier)`` where the latter two are boolean
    frames with the same shape as ``wide``.
    """
    prof = profile.set_index("sensor_id").reindex(wide.columns)
    medians = prof["median_value"].fillna(0.0)
    mean = prof["mean_value"].fillna(0.0)
    std = prof["std_value"].replace(0, np.nan).fillna(1.0)
    lower = prof["p01"].fillna(wide.min())
    upper = prof["p99"].fillna(wide.max())
    usable = prof["is_usable"].fillna(0).astype(bool)

    imputed = wide.isna()
    filled = wide.fillna(medians)

    z = (filled - mean) / std
    outlier = z.abs() > outlier_z

    clean = filled.copy()
    usable_cols = usable[usable].index.tolist()
    if usable_cols:
        clean[usable_cols] = filled[usable_cols].clip(lower=lower[usable_cols], upper=upper[usable_cols], axis=1)
    return clean, imputed, outlier


def add_derived_features(clean: pd.DataFrame, profile: pd.DataFrame) -> pd.DataFrame:
    """Wafer-level signals derived from standardised sensor readings."""
    prof = profile.set_index("sensor_id").reindex(clean.columns)
    usable = prof["is_usable"].fillna(0).astype(bool)
    cols = usable[usable].index.tolist()
    if not cols:
        return pd.DataFrame(
            {"z_mean": 0.0, "z_std": 0.0, "z_max_abs": 0.0, "abnormal_cnt": 0}, index=clean.index
        )
    mean = prof.loc[cols, "mean_value"].fillna(0.0)
    std = prof.loc[cols, "std_value"].replace(0, np.nan).fillna(1.0)
    z = (clean[cols] - mean) / std
    abs_z = z.abs()
    return pd.DataFrame(
        {
            "z_mean": z.mean(axis=1),
            "z_std": z.std(axis=1).fillna(0.0),
            "z_max_abs": abs_z.max(axis=1),
            "abnormal_cnt": (abs_z > 3.0).sum(axis=1).astype(int),
        },
        index=clean.index,
    )


def time_split_boundaries(ts: pd.Series, fractions: dict[str, float]) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Chronological cut points. Ties stay on the same side of a boundary."""
    train_end = ts.quantile(fractions["train"])
    valid_end = ts.quantile(fractions["train"] + fractions["valid"])
    return pd.Timestamp(train_end), pd.Timestamp(valid_end)


def assign_time_split(ts: pd.Series, fractions: dict[str, float]) -> pd.Series:
    """Assign train/valid/test by time; never random.

    The split respects causality: every test wafer is later than every valid
    wafer, and every valid wafer is later than every training wafer.
    """
    train_end, valid_end = time_split_boundaries(ts, fractions)
    split = pd.Series(TEST_SPLIT, index=ts.index, dtype="object")
    split.loc[ts <= valid_end] = VALID_SPLIT
    split.loc[ts <= train_end] = TRAIN_SPLIT
    return split


def risk_band(probability: float, high: float, medium: float) -> str:
    if probability >= high:
        return "high"
    if probability >= medium:
        return "medium"
    return "low"
