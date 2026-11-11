import numpy as np
import pytest

from src.modeling.metrics import (
    binary_report,
    bootstrap_ci,
    business_value,
    recall_at_fpr,
    safe_auc,
    safe_pr_auc,
)


@pytest.fixture()
def scores():
    rng = np.random.default_rng(7)
    y = np.array([0] * 90 + [1] * 10)
    s = np.concatenate([rng.uniform(0, 0.35, 90), rng.uniform(0.45, 1.0, 10)])
    return y, s


def test_recall_at_fpr_respects_budget(scores):
    y, s = scores
    result = recall_at_fpr(y, s, target_fpr=0.05)
    report = binary_report(y, s, result["threshold"])

    assert report["fpr"] <= 0.05 + 1e-9
    assert report["recall"] > 0.5
    assert report["alerts"] == report["tp"] + report["fp"]


def test_safe_metrics_on_single_class():
    y = np.zeros(10, dtype=int)
    s = np.linspace(0, 1, 10)
    assert np.isnan(safe_auc(y, s))
    assert np.isnan(safe_pr_auc(y, s))


def test_business_value_arithmetic():
    report = {"tp": 5, "fp": 2, "fn": 5}
    value = business_value(report, wafer_value_cny=3000, fn_cost_ratio=10, false_alarm_cost_cny=300)

    assert value["avoided_escapes"] == 5
    assert value["gross_saving_cny"] == 150_000
    assert value["screening_cost_cny"] == 600
    assert value["net_value_cny"] == 149_400


def test_bootstrap_ci_is_ordered(scores):
    y, s = scores
    low, high = bootstrap_ci(y, s, safe_auc, n_boot=200, seed=1)
    assert low <= high
    assert 0.5 <= low <= 1.0
