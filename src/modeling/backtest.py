"""Rolling-origin backtest for model selection.

With only 1,567 wafers the single 15% holdout contains ~9 failures, so one split
cannot rank models reliably. We therefore compare candidates with an expanding
window over the development period (all rows before the holdout): each fold
trains on everything up to a cut-off and scores the next block, exactly how the
model would be retrained and deployed week after week.

The holdout period stays untouched until the final report.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import numpy as np

from src.modeling.metrics import recall_at_fpr, safe_auc, safe_pr_auc

log = logging.getLogger(__name__)


def rolling_origin(
    X: np.ndarray,
    y: np.ndarray,
    model_factories: dict[str, Callable],
    *,
    initial_fraction: float = 0.55,
    folds: int = 5,
    target_fpr: float = 0.05,
    inner_valid_fraction: float = 0.15,
) -> tuple[list[dict], dict[str, np.ndarray]]:
    """Return (fold records, out-of-fold scores per model).

    ``X``/``y`` must be chronologically ordered and contain only the development
    period. The threshold for each fold's recall is picked on the tail of that
    fold's training window, mirroring the frozen-threshold policy.
    """
    n = len(y)
    if folds < 2 or n < 50:
        raise ValueError("backtest needs at least 2 folds and 50 rows")
    start = int(n * initial_fraction)
    block = max(1, (n - start) // folds)

    oof = {name: np.full(n, np.nan) for name in model_factories}
    records: list[dict] = []
    for fold in range(folds):
        lo = start + fold * block
        hi = n if fold == folds - 1 else lo + block
        inner_cut = int(lo * (1 - inner_valid_fraction))
        for name, factory in model_factories.items():
            inner = factory().fit(X[:inner_cut], y[:inner_cut])
            inner_scores = inner.predict_proba(X[inner_cut:lo])[:, 1]
            threshold = recall_at_fpr(y[inner_cut:lo], inner_scores, target_fpr)["threshold"]

            model = factory().fit(X[:lo], y[:lo])
            scores = model.predict_proba(X[lo:hi])[:, 1]
            oof[name][lo:hi] = scores
            records.append(
                {
                    "fold": fold + 1,
                    "model": name,
                    "train_rows": int(lo),
                    "test_rows": int(hi - lo),
                    "test_fails": int(y[lo:hi].sum()),
                    "auc": safe_auc(y[lo:hi], scores),
                    "pr_auc": safe_pr_auc(y[lo:hi], scores),
                    "recall_at_target_fpr": float((scores[y[lo:hi] == 1] >= threshold).mean())
                    if y[lo:hi].sum()
                    else float("nan"),
                    "alerts": int((scores >= threshold).sum()),
                    "threshold": threshold,
                }
            )
        log.info("backtest fold %d/%d done (train=%d test=%d)", fold + 1, folds, lo, hi - lo)
    return records, oof


def summarize(records: list[dict], models: list[str]) -> dict[str, dict]:
    summary: dict[str, dict] = {}
    for name in models:
        rows = [row for row in records if row["model"] == name]
        aucs = np.array([row["auc"] for row in rows], dtype=float)
        prs = np.array([row["pr_auc"] for row in rows], dtype=float)
        recalls = np.array([row["recall_at_target_fpr"] for row in rows], dtype=float)
        summary[name] = {
            "auc_mean": float(np.nanmean(aucs)),
            "auc_std": float(np.nanstd(aucs, ddof=1)) if len(aucs) > 1 else 0.0,
            "pr_auc_mean": float(np.nanmean(prs)),
            "pr_auc_std": float(np.nanstd(prs, ddof=1)) if len(prs) > 1 else 0.0,
            "recall_mean": float(np.nanmean(recalls)),
            "recall_std": float(np.nanstd(recalls, ddof=1)) if len(recalls) > 1 else 0.0,
            "folds": len(rows),
        }
    return summary
