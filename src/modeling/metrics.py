"""Model evaluation metrics tailored to manufacturing.

Why not accuracy: with a ~6.6% failure rate, predicting "pass" for everything
already scores 93.4%. Manufacturing teams care about *missed failures* under a
bounded false-alarm load, so we report AUC, PR-AUC and recall at a fixed FPR.
"""

from __future__ import annotations

import numpy as np
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score, roc_curve


def safe_auc(y_true: np.ndarray, scores: np.ndarray) -> float:
    if len(np.unique(y_true)) < 2:
        return float("nan")
    return float(roc_auc_score(y_true, scores))


def safe_pr_auc(y_true: np.ndarray, scores: np.ndarray) -> float:
    if len(np.unique(y_true)) < 2:
        return float("nan")
    return float(average_precision_score(y_true, scores))


def recall_at_fpr(y_true: np.ndarray, scores: np.ndarray, target_fpr: float = 0.05) -> dict:
    """Pick the largest threshold whose FPR stays within the budget."""
    fpr, tpr, thresholds = roc_curve(y_true, scores)
    allowed = np.where(fpr <= target_fpr)[0]
    if len(allowed) == 0:
        return {"threshold": float("inf"), "recall": 0.0, "fpr": 0.0, "precision": 0.0}
    idx = allowed[-1]
    threshold = float(thresholds[idx])
    predicted = scores >= threshold
    tp = int(((y_true == 1) & predicted).sum())
    fp = int(((y_true == 0) & predicted).sum())
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    return {
        "threshold": threshold,
        "recall": float(tpr[idx]),
        "fpr": float(fpr[idx]),
        "precision": float(precision),
    }


def confusion_counts(y_true: np.ndarray, scores: np.ndarray, threshold: float) -> dict:
    predicted = scores >= threshold
    tp = int(((y_true == 1) & predicted).sum())
    fp = int(((y_true == 0) & predicted).sum())
    tn = int(((y_true == 0) & ~predicted).sum())
    fn = int(((y_true == 1) & ~predicted).sum())
    return {"tp": tp, "fp": fp, "tn": tn, "fn": fn}


def binary_report(y_true: np.ndarray, scores: np.ndarray, threshold: float) -> dict:
    counts = confusion_counts(y_true, scores, threshold)
    total = sum(counts.values())
    tp, fp, tn, fn = counts["tp"], counts["fp"], counts["tn"], counts["fn"]
    return {
        **counts,
        "n": total,
        "actual_fail": int((y_true == 1).sum()),
        "alerts": tp + fp,
        "recall": tp / (tp + fn) if (tp + fn) else 0.0,
        "precision": tp / (tp + fp) if (tp + fp) else 0.0,
        "fpr": fp / (fp + tn) if (fp + tn) else 0.0,
        "specificity": tn / (tn + fp) if (tn + fp) else 0.0,
        "brier": float(brier_score_loss(y_true, scores)),
        "threshold": float(threshold),
    }


def business_value(report: dict, *, wafer_value_cny: float, fn_cost_ratio: float, false_alarm_cost_cny: float) -> dict:
    """Scenario value estimate; assumptions are explicit and editable in config."""
    escape_cost = wafer_value_cny * fn_cost_ratio
    avoided_escapes = report["tp"]
    extra_alarms = report["fp"]
    gross_saving = avoided_escapes * escape_cost
    alarm_cost = extra_alarms * false_alarm_cost_cny
    return {
        "assumption_wafer_value_cny": wafer_value_cny,
        "assumption_escape_multiplier": fn_cost_ratio,
        "assumption_false_alarm_cost_cny": false_alarm_cost_cny,
        "avoided_escapes": avoided_escapes,
        "missed_failures": report["fn"],
        "false_alarms": extra_alarms,
        "gross_saving_cny": gross_saving,
        "screening_cost_cny": alarm_cost,
        "net_value_cny": gross_saving - alarm_cost,
    }


def bootstrap_ci(
    y_true: np.ndarray,
    scores: np.ndarray,
    metric_fn,
    *,
    n_boot: int = 1000,
    seed: int = 42,
    alpha: float = 0.05,
) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    values: list[float] = []
    n = len(y_true)
    for _ in range(n_boot):
        idx = rng.integers(0, n, n)
        if len(np.unique(y_true[idx])) < 2:
            continue
        value = metric_fn(y_true[idx], scores[idx])
        if np.isfinite(value):
            values.append(value)
    if not values:
        return float("nan"), float("nan")
    return float(np.quantile(values, alpha / 2)), float(np.quantile(values, 1 - alpha / 2))
