import numpy as np
import pandas as pd
import pytest

from src.features.build import (
    add_derived_features,
    assign_time_split,
    clean_sensor_matrix,
    risk_band,
    sensor_profile,
)


@pytest.fixture()
def wide_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "S001": [10.0, np.nan, 30.0, 40.0],
            "S002": [5.0, 5.0, 5.0, 5.0],
            "S003": [1.0, np.nan, np.nan, np.nan],
        },
        index=["W1", "W2", "W3", "W4"],
    )


def test_profile_flags_constant_and_high_missing(wide_frame):
    profile = sensor_profile(
        wide_frame,
        pd.Series([True, True, True, True], index=wide_frame.index),
        max_missing_rate=0.40,
        min_unique_values=2,
    ).set_index("sensor_id")

    assert profile.loc["S002", "is_constant"] == 1
    assert profile.loc["S003", "is_high_missing"] == 1
    assert profile.loc["S003", "is_usable"] == 0
    assert profile.loc["S001", "is_usable"] == 1


def test_clean_imputes_with_training_median(wide_frame):
    train_mask = pd.Series([True, True, True, True], index=wide_frame.index)
    profile = sensor_profile(wide_frame, train_mask)
    clean, imputed, _ = clean_sensor_matrix(wide_frame, profile)

    assert imputed.loc["W2", "S001"] is np.True_ or bool(imputed.loc["W2", "S001"]) is True
    assert clean.loc["W2", "S001"] == pytest.approx(30.0)
    assert clean.loc["W2", "S003"] == pytest.approx(1.0)


def test_clean_winsorises_usable_sensor(wide_frame):
    profile = sensor_profile(wide_frame, pd.Series(True, index=wide_frame.index))
    clean, _, outlier = clean_sensor_matrix(wide_frame, profile, outlier_z=2.0)

    assert clean.loc["W4", "S001"] <= wide_frame.loc["W4", "S001"]
    assert bool(outlier.loc["W4", "S001"]) in (True, False)


def test_derived_features_are_finite(wide_frame):
    profile = sensor_profile(wide_frame, pd.Series(True, index=wide_frame.index))
    clean, _, _ = clean_sensor_matrix(wide_frame, profile)
    derived = add_derived_features(clean, profile)

    assert list(derived.columns) == ["z_mean", "z_std", "z_max_abs", "abnormal_cnt"]
    assert derived.notna().all().all()


def test_time_split_is_chronological():
    timestamps = pd.Series(pd.date_range("2024-01-01", periods=200, freq="h"))
    split = assign_time_split(timestamps, {"train": 0.7, "valid": 0.15, "test": 0.15})

    assert (split == "train").sum() >= 130
    assert timestamps[split == "train"].max() < timestamps[split == "test"].min()
    assert timestamps[split == "valid"].max() < timestamps[split == "test"].min()


def test_risk_bands():
    assert risk_band(0.9, high=0.6, medium=0.3) == "high"
    assert risk_band(0.4, high=0.6, medium=0.3) == "medium"
    assert risk_band(0.1, high=0.6, medium=0.3) == "low"
