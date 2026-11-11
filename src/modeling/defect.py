"""WM-811K 9-class wafer defect pattern classifier.

The derived feature table has no timestamps, so the official Training/Test
split shipped with WM-811K is respected instead of inventing a temporal one.
Both models see exactly the same rows; the test split is never used for fitting
or for choosing hyper-parameters.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    classification_report,
    confusion_matrix,
    f1_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from src.config import path_of
from src.db import connect, read_df
from src.ingest.wm811k import FEATURES
from src.paths import ensure_parent

log = logging.getLogger(__name__)


def _class_weights(y: np.ndarray, n_classes: int) -> np.ndarray:
    counts = np.bincount(y, minlength=n_classes).astype(float)
    per_class = np.where(counts > 0, len(y) / (n_classes * np.maximum(counts, 1)), 0.0)
    return per_class[y]


def train_defect_model(cfg: dict) -> dict:
    db_path = path_of(cfg, "warehouse")
    artifacts = path_of(cfg, "artifacts")
    class_names = cfg["wm811k"]["class_names"]

    with connect(db_path) as conn:
        frame = read_df(conn, "SELECT * FROM dwd_wm811k_wafer WHERE is_valid = 1 ORDER BY source_index")

    # total_grid is dropped in DWD as a redundant rows*cols column.
    features = [column for column in FEATURES if column in frame.columns]
    X = frame[features].astype(float)
    y = frame["label_code"].to_numpy(dtype=int)
    train_mask = (frame["split_original"] == "Training").to_numpy()
    test_mask = (frame["split_original"] == "Test").to_numpy()
    log.info("defect train=%d test=%d classes=%d", train_mask.sum(), test_mask.sum(), len(class_names))

    lgbm = lgb.LGBMClassifier(**cfg["wm811k"]["lgbm"])
    lgbm.fit(X.loc[train_mask], y[train_mask], sample_weight=_class_weights(y[train_mask], len(class_names)))

    logistic = Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            (
                "model",
                LogisticRegression(
                    C=cfg["wm811k"]["logistic"]["C"],
                    max_iter=cfg["wm811k"]["logistic"]["max_iter"],
                    class_weight="balanced",
                    random_state=cfg["project"]["seed"],
                ),
            ),
        ]
    )
    logistic.fit(X.loc[train_mask], y[train_mask])

    reports: dict[str, dict] = {}
    predictions: dict[str, np.ndarray] = {}
    for name, model in {"lightgbm": lgbm, "logistic_regression": logistic}.items():
        pred = model.predict(X.loc[test_mask])
        predictions[name] = pred
        report = classification_report(
            y[test_mask], pred, labels=list(range(len(class_names))), target_names=class_names, output_dict=True, zero_division=0
        )
        reports[name] = {
            "accuracy": float(accuracy_score(y[test_mask], pred)),
            "balanced_accuracy": float(balanced_accuracy_score(y[test_mask], pred)),
            "macro_f1": float(f1_score(y[test_mask], pred, average="macro")),
            "weighted_f1": float(f1_score(y[test_mask], pred, average="weighted")),
            "per_class": {
                name_class: {
                    "precision": float(report[name_class]["precision"]),
                    "recall": float(report[name_class]["recall"]),
                    "f1": float(report[name_class]["f1-score"]),
                    "support": int(report[name_class]["support"]),
                }
                for name_class in class_names
            },
        }

    selected = "lightgbm"
    selected_pred = predictions[selected]
    matrix = confusion_matrix(y[test_mask], selected_pred, labels=list(range(len(class_names))))

    importance = pd.DataFrame(
        {
            "feature": features,
            "gain": lgbm.booster_.feature_importance(importance_type="gain"),
            "split": lgbm.booster_.feature_importance(importance_type="split"),
        }
    ).sort_values("gain", ascending=False)
    importance.to_csv(artifacts / "defect_feature_importance.csv", index=False)

    metadata = {
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": selected,
        "classes": class_names,
        "features": features,
        "feature_medians": {column: float(X[column].median()) for column in features},
        "split": {"train": int(train_mask.sum()), "test": int(test_mask.sum()), "strategy": "official WM-811K split"},
    }
    ensure_parent(artifacts / "models" / "defect_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lgbm.booster_.save_model(str(artifacts / "models" / "defect_lgbm.txt"))

    class_distribution = (
        frame.groupby(["label_name", "split_original"]).size().unstack(fill_value=0).reindex(class_names).fillna(0)
    )
    metrics = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dataset": {
            "name": "WM-811K (derived 26-feature subset)",
            "rows": int(len(frame)),
            "classes": len(class_names),
            "source": "CC0 WM-811K via public GitHub mirror; see docs/data_dictionary.md",
        },
        "split": metadata["split"],
        "models": reports,
        "selected_model": selected,
        "confusion_matrix": matrix.tolist(),
        "class_order": class_names,
        "class_distribution": {
            label: {split_name: int(value) for split_name, value in row.items()} for label, row in class_distribution.iterrows()
        },
        "top_features": json.loads(importance.head(12).round(5).to_json(orient="records")),
    }
    ensure_parent(artifacts / "defect_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    with connect(db_path) as conn:
        conn.execute("DELETE FROM ads_defect_class_perf")
        perf_rows = [
            {
                "label_name": label,
                "precision": reports[selected]["per_class"][label]["precision"],
                "recall": reports[selected]["per_class"][label]["recall"],
                "f1": reports[selected]["per_class"][label]["f1"],
                "support": reports[selected]["per_class"][label]["support"],
            }
            for label in class_names
        ]
        pd.DataFrame(perf_rows).to_sql("ads_defect_class_perf", conn, if_exists="append", index=False)

        conn.execute("DELETE FROM ads_defect_confusion")
        confusion_rows = [
            {"actual_label": class_names[i], "predicted_label": class_names[j], "cnt": int(matrix[i, j])}
            for i in range(len(class_names))
            for j in range(len(class_names))
        ]
        pd.DataFrame(confusion_rows).to_sql("ads_defect_confusion", conn, if_exists="append", index=False)

    log.info(
        "defect model macro_f1=%.3f balanced_acc=%.3f",
        reports[selected]["macro_f1"],
        reports[selected]["balanced_accuracy"],
    )
    return metrics
