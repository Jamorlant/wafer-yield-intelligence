import pandas as pd
import pytest

from src.quality.rules import label_domain_violations, missing_rate_summary, wm811k_validity


def test_label_domain_violations():
    labels = pd.Series([-1, 1, -1, 0, 2])
    assert label_domain_violations(labels) == 2


def test_wm811k_validity_reasons():
    frame = pd.DataFrame(
        {
            "rows": [30, 0, 30, 30, 30],
            "cols": [30, 30, 30, 30, 30],
            "total_die": [900, 900, 0, 900, 900],
            "fail_die": [10, 10, 10, -1, 901],
            "defect_density": [0.01, 0.01, 0.01, 0.01, 0.01],
        }
    )
    valid, reason = wm811k_validity(frame)

    assert valid.tolist() == [1, 0, 0, 0, 0]
    assert reason.tolist() == ["", "bad_shape", "no_die", "negative_fail", "fail_gt_total"]


def test_wm811k_density_out_of_range():
    frame = pd.DataFrame(
        {"rows": [30], "cols": [30], "total_die": [900], "fail_die": [10], "defect_density": [1.2]}
    )
    valid, reason = wm811k_validity(frame)
    assert valid.tolist() == [0]
    assert reason.tolist() == ["density_out_of_range"]


def test_missing_rate_summary():
    profile = pd.DataFrame(
        {
            "missing_rate": [0.01, 0.5],
            "is_constant": [0, 1],
            "is_usable": [1, 0],
        }
    )
    summary = missing_rate_summary(profile, max_missing_rate=0.4)
    assert summary["max_missing_rate"] == pytest.approx(0.5)
    assert summary["high_missing_cnt"] == 1
    assert summary["constant_cnt"] == 1
    assert summary["usable_cnt"] == 1
