"""Statistical process control rules (pure functions, unit-tested).

The hourly mean of a parameter is standardised against a baseline frozen on the
first ``baseline_days`` days:

    z = (hourly_mean - baseline_mean) / baseline_sd

where ``baseline_sd`` is the standard deviation of the **hourly means** in the
frozen baseline window (not the pooled within-hour sigma divided by sqrt(n)).

Two rules raise an alert:

``R1_3SIGMA`` (high)
    ``sustained_points`` consecutive hours with |z| >= sigma. A single point is
    only a warning flag; three in a row is a real shift.

``R2_SEVEN_POINT`` (medium)
    ``run_length`` consecutive hours on the same side of the baseline mean with
    a run mean of at least ``run_mean_sigma``. Catches slow drifts before any
    single point crosses 3 sigma.

Every rule reports its first trigger only, so one incident yields one alert.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

RULE_HIGH = "R1_3SIGMA"
RULE_RUN = "R2_SEVEN_POINT"


def baseline_limits(baseline: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    """Frozen control limits per series, computed on the plotted hourly mean.

    Limits are the mean +/- k * sd of the *hourly means* observed in the
    baseline window. This is the classic Shewhart chart on the plotted
    statistic; the pooled within-hour sigma is kept as a diagnostic only, so a
    noisy hour with two samples cannot by itself shrink the limits.
    """
    frame = baseline.copy()
    frame["_sum"] = frame["mean_value"] * frame["sample_cnt"]
    frame["_var"] = (frame["std_value"].fillna(0.0) ** 2) * (frame["sample_cnt"] - 1).clip(lower=0)
    grouped = frame.groupby(group_cols, observed=True).agg(
        n=("sample_cnt", "sum"),
        total=("_sum", "sum"),
        var_sum=("_var", "sum"),
        hours=("stat_hour", "count"),
        sd_means=("mean_value", "std"),
    )
    grouped = grouped[grouped["hours"] >= 24]
    grouped["base_mean"] = grouped["total"] / grouped["n"]
    grouped["within_sd"] = np.sqrt(grouped["var_sum"] / (grouped["n"] - 1).clip(lower=1))
    grouped["base_sd"] = grouped["sd_means"].replace(0.0, np.nan)
    return grouped[["base_mean", "base_sd", "within_sd", "hours"]].reset_index()


def standardise(hourly: pd.DataFrame, limits: pd.DataFrame, group_cols: list[str], sigma: float = 3.0) -> pd.DataFrame:
    """Attach limits, z-scores and control bounds to an hourly series."""
    frame = hourly.merge(limits, on=group_cols, how="inner")
    frame["base_sd"] = frame["base_sd"].replace(0.0, np.nan)
    frame["z_score"] = (frame["mean_value"] - frame["base_mean"]) / frame["base_sd"]
    frame["ucl"] = frame["base_mean"] + sigma * frame["base_sd"]
    frame["lcl"] = frame["base_mean"] - sigma * frame["base_sd"]
    frame["point_breach"] = (frame["z_score"].abs() >= sigma).astype(int)
    return frame


def detect_alerts(
    z: np.ndarray,
    sigma: float = 3.0,
    sustained: int = 3,
    run_length: int = 7,
    run_mean_sigma: float = 2.0,
) -> list[dict]:
    """Return the first trigger of each rule for one series (index into ``z``)."""
    values = np.asarray(z, dtype=float)
    values = np.where(np.isfinite(values), values, 0.0)
    alerts: list[dict] = []

    streak = 0
    for i, value in enumerate(values):
        streak = streak + 1 if abs(value) >= sigma else 0
        if streak >= max(sustained, 1) and not any(a["rule"] == RULE_HIGH for a in alerts):
            alerts.append({"index": i, "rule": RULE_HIGH, "severity": "high", "z": float(value), "value": float(values[i])})
            break

    for i in range(len(values) - run_length + 1):
        window = values[i : i + run_length]
        if (np.all(window >= 0) or np.all(window <= 0)) and abs(float(window.mean())) >= run_mean_sigma:
            alerts.append(
                {
                    "index": i + run_length - 1,
                    "rule": RULE_RUN,
                    "severity": "medium",
                    "z": float(window.mean()),
                    "value": float(values[i + run_length - 1]),
                }
            )
            break

    return alerts


def first_breach(z: np.ndarray, sigma: float = 3.0) -> int | None:
    """Index of the first single point outside the control limits."""
    values = np.asarray(z, dtype=float)
    hits = np.where(np.abs(values) >= sigma)[0]
    return int(hits[0]) if len(hits) else None
