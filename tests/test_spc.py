"""Unit tests for the SPC rules: one incident, one alert, no silent misses."""

import numpy as np
import pandas as pd

from src.quality.spc import RULE_HIGH, RULE_RUN, baseline_limits, detect_alerts, first_breach


def test_r1_needs_three_consecutive_points():
    z = np.array([0.1, 3.4, 3.2, 3.6, 0.2])
    alerts = detect_alerts(z, sigma=3.0, sustained=3)
    assert alerts[0]["rule"] == RULE_HIGH
    assert alerts[0]["index"] == 3, "alert fires on the third breach, not the first"


def test_r2_catches_slow_drift_before_any_point_breaches():
    z = np.array([2.1] * 8)
    alerts = detect_alerts(z, sigma=3.0, sustained=3, run_length=7, run_mean_sigma=2.0)
    assert [a["rule"] for a in alerts] == [RULE_RUN]
    assert alerts[0]["index"] == 6
    assert alerts[0]["z"] == 2.1


def test_r2_ignores_runs_that_straddle_the_centre_line():
    z = np.array([2.5, 2.5, 2.5, -2.5, 2.5, 2.5, 2.5])
    assert detect_alerts(z, sigma=3.0, run_length=7, run_mean_sigma=2.0) == []


def test_normal_noise_raises_nothing():
    rng = np.random.default_rng(0)
    z = rng.normal(0, 0.6, 720)
    assert detect_alerts(z, sigma=3.0, sustained=3, run_length=7, run_mean_sigma=2.0) == []


def test_first_breach_returns_none_when_in_control():
    assert first_breach(np.array([0.1, -0.2, 0.3])) is None
    assert first_breach(np.array([0.1, -3.4, 0.3])) == 1


def _hourly_frame(hours: int) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "line_code": ["LINE-A"] * hours,
            "stat_hour": [f"2026-09-{i // 24 + 1:02d}T{i % 24:02d}" for i in range(hours)],
            "mean_value": np.linspace(8.5, 8.6, hours),
            "std_value": np.full(hours, 0.05),
            "sample_cnt": np.full(hours, 4),
        }
    )


def test_baseline_limits_use_hourly_mean_spread():
    limits = baseline_limits(_hourly_frame(48), ["line_code"])
    assert len(limits) == 1
    row = limits.iloc[0]
    assert row["hours"] == 48
    assert np.isclose(row["base_mean"], 8.55, atol=1e-6)
    assert row["base_sd"] > 0


def test_baseline_needs_24_hours():
    assert baseline_limits(_hourly_frame(23), ["line_code"]).empty
