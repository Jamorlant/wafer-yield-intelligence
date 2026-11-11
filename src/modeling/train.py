"""Yield model training: LightGBM vs logistic regression on a time-ordered split.

Design decisions
----------------
* The split is chronological (70/15/15). Random splitting would leak future
  process drift into training and inflate AUC.
* Models are compared with a rolling-origin backtest over the development period
  (train + validation). With ~9 failures in a 235-wafer holdout, a single split
  cannot rank two models reliably.
* The operating threshold is chosen from pooled out-of-fold scores at a fixed 5%
  false-positive budget, then frozen and applied to the untouched test period.
* The winner is refit on train + validation before the holdout is scored once.
* Attribution is exact for both candidates: TreeSHAP (``pred_contrib``) for
  LightGBM, ``coefficient x z-score`` for the standardised logistic model.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from src.config import path_of
from src.db import connect, read_df
from src.modeling.backtest import rolling_origin, summarize
from src.modeling.metrics import (
    binary_report,
    bootstrap_ci,
    business_value,
    recall_at_fpr,
    safe_auc,
    safe_pr_auc,
)
from src.paths import ensure_parent

log = logging.getLogger(__name__)

META_COLUMNS = ["wafer_id", "measurement_ts", "stat_date", "split", "is_fail"]


def _subsample_curve(x: np.ndarray, y: np.ndarray, points: int = 60) -> list[list[float]]:
    if len(x) <= points:
        idx = np.arange(len(x))
    else:
        idx = np.unique(np.linspace(0, len(x) - 1, points).round().astype(int))
    return [[round(float(x[i]), 4), round(float(y[i]), 4)] for i in idx]


def _evaluate(y_true: np.ndarray, scores: np.ndarray, threshold: float, cfg: dict) -> dict:
    report = binary_report(y_true, scores, threshold)
    report["auc"] = safe_auc(y_true, scores)
    report["pr_auc"] = safe_pr_auc(y_true, scores)
    report["auc_ci95"] = list(bootstrap_ci(y_true, scores, safe_auc))
    report["pr_auc_ci95"] = list(bootstrap_ci(y_true, scores, safe_pr_auc))
    report["business"] = business_value(
        report,
        wafer_value_cny=cfg["secom"]["business"]["wafer_value_cny"],
        fn_cost_ratio=cfg["secom"]["business"]["fn_cost_ratio"],
        false_alarm_cost_cny=cfg["secom"]["business"]["false_alarm_cost_cny"],
    )
    return report


def _lgb_factory(params: dict):
    def factory():
        return lgb.LGBMClassifier(**params)

    return factory


def _logistic_factory(params: dict, seed: int):
    def factory():
        return Pipeline(
            [
                ("imputer", SimpleImputer(strategy="median")),
                ("scaler", StandardScaler()),
                (
                    "model",
                    LogisticRegression(
                        C=params["C"],
                        max_iter=params["max_iter"],
                        class_weight="balanced",
                        random_state=seed,
                    ),
                ),
            ]
        )

    return factory


def _contributions(model_type: str, model, X: np.ndarray, stats: dict) -> np.ndarray:
    """Per-row log-odds contribution, shape (n_rows, n_features)."""
    if model_type == "lightgbm":
        return model.booster_.predict(X, pred_contrib=True)[:, :-1]
    filled = np.nan_to_num(X, nan=0.0)
    z = (filled - stats["means"]) / stats["scales"]
    return z * stats["coefficients"]


def _logistic_stats(model: Pipeline) -> dict:
    imputer: SimpleImputer = model.named_steps["imputer"]
    scaler: StandardScaler = model.named_steps["scaler"]
    lr: LogisticRegression = model.named_steps["model"]
    return {
        "medians": imputer.statistics_.astype(float),
        "means": scaler.mean_.astype(float),
        "scales": scaler.scale_.astype(float),
        "coefficients": lr.coef_[0].astype(float),
        "intercept": float(lr.intercept_[0]),
    }


def train_yield_model(cfg: dict) -> dict:
    db_path = path_of(cfg, "warehouse")
    artifacts = path_of(cfg, "artifacts")
    model_dir = artifacts / "models"
    ensure_parent(model_dir / "placeholder")

    with connect(db_path) as conn:
        manifest = read_df(conn, "SELECT * FROM ads_training_manifest ORDER BY created_at DESC LIMIT 1").iloc[0]
        features = json.loads(manifest["feature_names"])
        frame = read_df(conn, "SELECT * FROM ads_yield_feature_matrix ORDER BY measurement_ts")
        risk_cfg = cfg["secom"]["risk_bands"]
        split_cfg = cfg["secom"]["split"]

    X = frame[features].astype(float).to_numpy()
    y = frame["is_fail"].to_numpy(dtype=int)
    split = frame["split"].astype(str)
    train_mask = (split == "train").to_numpy()
    valid_mask = (split == "valid").to_numpy()
    dev_mask = train_mask | valid_mask
    test_mask = (split == "test").to_numpy()

    log.info(
        "train=%d valid=%d test=%d features=%d",
        train_mask.sum(),
        valid_mask.sum(),
        test_mask.sum(),
        len(features),
    )

    seed = cfg["project"]["seed"]
    factories = {
        "lightgbm": _lgb_factory(cfg["secom"]["lgbm"]),
        "logistic_regression": _logistic_factory(cfg["secom"]["logistic"], seed),
    }

    # --- rolling-origin comparison on the development period ----------------
    backtest_records, oof = rolling_origin(
        X[dev_mask],
        y[dev_mask],
        factories,
        initial_fraction=cfg["secom"]["backtest"]["initial_fraction"],
        folds=cfg["secom"]["backtest"]["folds"],
        target_fpr=cfg["secom"]["target_fpr"],
    )
    backtest_summary = summarize(backtest_records, list(factories))
    for name, summary in backtest_summary.items():
        log.info(
            "backtest %s auc=%.3f+-%.3f pr_auc=%.3f recall@fpr=%.3f",
            name,
            summary["auc_mean"],
            summary["auc_std"],
            summary["pr_auc_mean"],
            summary["recall_mean"],
        )

    selected = max(backtest_summary, key=lambda name: (backtest_summary[name]["auc_mean"], backtest_summary[name]["pr_auc_mean"]))
    log.info("selected %s by rolling-origin mean AUC", selected)

    # --- frozen threshold from pooled out-of-fold scores --------------------
    target_fpr = cfg["secom"]["target_fpr"]
    oof_scores = oof[selected]
    # Early rows precede the first backtest cut-off, so they have no OOF score.
    oof_mask = ~np.isnan(oof_scores)
    threshold = recall_at_fpr(y[dev_mask][oof_mask], oof_scores[oof_mask], target_fpr)["threshold"]
    log.info("threshold %.4f from pooled OOF at %.1f%% FPR", threshold, target_fpr * 100)

    # --- refit on train + validation, score holdout once --------------------
    fitted: dict[str, object] = {}
    for name, factory in factories.items():
        fitted[name] = factory().fit(X[dev_mask], y[dev_mask])

    prob_test = {name: model.predict_proba(X[test_mask])[:, 1] for name, model in fitted.items()}
    models: dict[str, dict] = {}
    for name in fitted:
        models[name] = {
            "backtest": {
                "fold_metrics": [row for row in backtest_records if row["model"] == name],
                **backtest_summary[name],
            },
            "test": _evaluate(y[test_mask], prob_test[name], threshold, cfg),
        }

    test = models[selected]["test"]
    log.info(
        "selected=%s test auc=%.3f pr_auc=%.3f recall@fpr=%.3f alerts=%d",
        selected,
        test["auc"],
        test["pr_auc"],
        test["recall"],
        test["alerts"],
    )

    # --- attribution --------------------------------------------------------
    X_test = X[test_mask]
    y_test = y[test_mask]
    selected_model = fitted[selected]
    if selected == "lightgbm":
        contrib_test = _contributions("lightgbm", selected_model, X_test, {})
        lgb_gain = selected_model.booster_.feature_importance(importance_type="gain")
        model_type = "lightgbm"
        model_file = "yield_lgbm.txt"
        selected_model.booster_.save_model(str(model_dir / model_file))
        logistic_stats = {}
    else:
        logistic_stats = _logistic_stats(selected_model)
        contrib_test = _contributions("logistic_regression", selected_model, X_test, logistic_stats)
        lgb_gain = fitted["lightgbm"].booster_.feature_importance(importance_type="gain")
        model_type = "logistic_regression"
        model_file = "yield_logistic.pkl"
        joblib.dump(selected_model, model_dir / model_file)

    lgb_model = fitted["lightgbm"]
    lgb_model.booster_.save_model(str(model_dir / "yield_lgbm.txt"))
    joblib.dump(fitted["logistic_regression"], model_dir / "yield_logistic.pkl")
    effect_mean_abs = np.abs(contrib_test).mean(axis=0)

    perm = permutation_importance(
        selected_model, X_test, y_test, scoring="roc_auc", n_repeats=10, random_state=seed, n_jobs=1
    )
    importance = pd.DataFrame(
        {
            "feature": features,
            "lgbm_gain": lgb_gain,
            "permutation_mean": perm.importances_mean,
            "permutation_std": perm.importances_std,
            "effect_mean_abs": effect_mean_abs,
        }
    ).sort_values("effect_mean_abs", ascending=False)
    importance.to_csv(artifacts / "feature_importance.csv", index=False)

    # --- persist model metadata --------------------------------------------
    def stats_dict(values: np.ndarray) -> dict[str, float]:
        return {feature: float(value) for feature, value in zip(features, values, strict=True)}

    metadata = {
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "selected_model": selected,
        "model_type": model_type,
        "model_file": model_file,
        "threshold": threshold,
        "threshold_source": f"pooled out-of-fold scores at {target_fpr:.0%} FPR",
        "target_fpr": target_fpr,
        "features": features,
        "feature_medians": stats_dict(
            logistic_stats["medians"] if logistic_stats else np.nanmedian(X[dev_mask], axis=0)
        ),
        "risk_bands": risk_cfg,
        "split": {
            "train": int(train_mask.sum()),
            "valid": int(valid_mask.sum()),
            "test": int(test_mask.sum()),
            "train_start": str(frame.loc[train_mask, "measurement_ts"].min()),
            "train_end": str(frame.loc[train_mask, "measurement_ts"].max()),
            "test_start": str(frame.loc[test_mask, "measurement_ts"].min()),
            "test_end": str(frame.loc[test_mask, "measurement_ts"].max()),
        },
        "backtest": {name: backtest_summary[name] for name in backtest_summary},
        "metrics_path": str(artifacts / "metrics.json"),
    }
    if logistic_stats:
        metadata["feature_means"] = stats_dict(logistic_stats["means"])
        metadata["feature_scales"] = stats_dict(logistic_stats["scales"])
        metadata["coefficients"] = stats_dict(logistic_stats["coefficients"])
        metadata["intercept"] = logistic_stats["intercept"]
    ensure_parent(model_dir / "model_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # --- operational scoring for every wafer --------------------------------
    prob_all = selected_model.predict_proba(X)[:, 1]
    if selected == "logistic_regression":
        contrib_all = _contributions("logistic_regression", selected_model, X, logistic_stats)
    else:
        contrib_all = _contributions("lightgbm", selected_model, X, {})
    top_idx = np.argsort(-np.abs(contrib_all), axis=1)[:, :3]

    risk_rows = pd.DataFrame(
        {
            "wafer_id": frame["wafer_id"],
            "measurement_ts": frame["measurement_ts"],
            "stat_date": frame["stat_date"],
            "split": split,
            "is_fail": y,
            "fail_probability": prob_all,
            "risk_band": [
                "high" if p >= risk_cfg["high"] else "medium" if p >= risk_cfg["medium"] else "low" for p in prob_all
            ],
            "alert_flag": (prob_all >= threshold).astype(int),
        }
    )
    for rank in range(3):
        idx = top_idx[:, rank]
        risk_rows[f"top_driver_{rank + 1}"] = [features[i] for i in idx]
        risk_rows[f"contribution_{rank + 1}"] = contrib_all[np.arange(len(idx)), idx]

    predictions = frame[META_COLUMNS].copy()
    predictions["prob_lightgbm"] = lgb_model.predict_proba(X)[:, 1]
    predictions["prob_logistic"] = fitted["logistic_regression"].predict_proba(X)[:, 1]
    predictions["alert"] = risk_rows["alert_flag"]
    predictions.to_csv(artifacts / "predictions.csv", index=False)

    # Per-wafer explanations for the highest-risk holdout wafers.
    test_positions = np.where(test_mask)[0]
    top_test = test_positions[np.argsort(-prob_all[test_positions])[:12]]
    attributions = []
    for position in top_test:
        row_contrib = contrib_all[position]
        order = np.argsort(-np.abs(row_contrib))[:10]
        attributions.append(
            {
                "wafer_id": str(frame.iloc[position]["wafer_id"]),
                "stat_date": str(frame.iloc[position]["stat_date"]),
                "is_fail": int(y[position]),
                "probability": round(float(prob_all[position]), 4),
                "method": "TreeSHAP" if selected == "lightgbm" else "standardised coefficient",
                "drivers": [
                    {
                        "feature": features[index],
                        "contribution": round(float(row_contrib[index]), 4),
                        "value": round(float(X[position, index]), 4),
                    }
                    for index in order
                ],
            }
        )
    ensure_parent(artifacts / "attributions.json").write_text(
        json.dumps(attributions, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # --- metrics report -----------------------------------------------------
    from sklearn.metrics import precision_recall_curve, roc_curve

    fpr, tpr, _ = roc_curve(y_test, prob_test[selected])
    precision, recall, _ = precision_recall_curve(y_test, prob_test[selected])
    metrics = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dataset": {
            "name": "SECOM",
            "wafers": int(len(frame)),
            "features": int(len(features)),
            "fail_rate": round(float(y.mean()), 4),
            "source": "UCI Machine Learning Repository (id 179), CC BY 4.0",
        },
        "split": {
            "strategy": "chronological",
            "fractions": split_cfg,
            **metadata["split"],
        },
        "target_fpr": target_fpr,
        "selected_model": selected,
        "selection_rule": "highest mean AUC in 5-fold rolling-origin backtest",
        "threshold": threshold,
        "threshold_source": metadata["threshold_source"],
        "models": models,
        "backtest": {
            "strategy": "expanding window, chronological",
            "initial_fraction": cfg["secom"]["backtest"]["initial_fraction"],
            "folds": cfg["secom"]["backtest"]["folds"],
            "summary": backtest_summary,
        },
        "top_features": json.loads(importance.head(15).round(5).to_json(orient="records")),
        "curves": {
            "roc": _subsample_curve(fpr, tpr),
            "pr": _subsample_curve(recall, precision),
        },
        "scenario_note": "Business value is a scenario estimate with explicit assumptions, not a booked saving.",
    }
    ensure_parent(artifacts / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # --- write ADS results --------------------------------------------------
    with connect(db_path) as conn:
        conn.execute("DELETE FROM ads_wafer_risk")
        risk_rows.to_sql("ads_wafer_risk", conn, if_exists="append", index=False, chunksize=200)

        conn.execute("DELETE FROM ads_yield_trend_daily")
        out_of_sample = risk_rows[risk_rows["split"] == "test"]
        trend = (
            frame.assign(is_fail=y)
            .groupby("stat_date")
            .agg(wafer_cnt=("wafer_id", "count"), fail_cnt=("is_fail", "sum"))
            .reset_index()
        )
        trend["actual_yield"] = 1 - trend["fail_cnt"] / trend["wafer_cnt"]
        risk_by_date = out_of_sample.groupby("stat_date").agg(
            avg_risk=("fail_probability", "mean"), high_risk_cnt=("alert_flag", "sum")
        )
        trend = trend.merge(risk_by_date, left_on="stat_date", right_index=True, how="left")
        trend.to_sql("ads_yield_trend_daily", conn, if_exists="append", index=False)

    log.info("metrics written to %s", artifacts / "metrics.json")
    return metrics
