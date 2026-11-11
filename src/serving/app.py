"""FastAPI model service.

Endpoints
---------
GET  /health          liveness + artifact availability
GET  /model/info      metrics and split metadata for both models
POST /predict         yield failure probability for one wafer + top drivers
POST /defect/predict  9-class defect pattern probabilities
GET  /wafer/{wafer_id} stored risk score from the ADS layer
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from src.config import load_config, path_of
from src.db import connect, read_df
from src.features.build import risk_band

log = logging.getLogger(__name__)


class YieldRequest(BaseModel):
    features: dict[str, float] = Field(
        default_factory=dict,
        description="Sensor id -> value. Missing sensors are filled with training medians.",
    )


class Driver(BaseModel):
    feature: str
    contribution: float


class YieldResponse(BaseModel):
    failure_probability: float
    risk_band: str
    threshold: float
    alert: bool
    top_drivers: list[Driver]


class DefectRequest(BaseModel):
    features: dict[str, float]


class DefectResponse(BaseModel):
    predicted_class: str
    probabilities: dict[str, float]


@lru_cache(maxsize=1)
def _yield_bundle():
    cfg = load_config()
    artifacts = path_of(cfg, "artifacts")
    metadata_path = artifacts / "models" / "model_metadata.json"
    if not metadata_path.exists():
        raise FileNotFoundError("yield model metadata is missing")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    model_path = artifacts / "models" / metadata["model_file"]
    if not model_path.exists():
        raise FileNotFoundError("yield model artifacts are missing")
    if metadata["model_type"] == "lightgbm":
        model = lgb.Booster(model_file=str(model_path))
    else:
        model = joblib.load(model_path)
    return cfg, model, metadata


@lru_cache(maxsize=1)
def _defect_bundle():
    cfg = load_config()
    artifacts = path_of(cfg, "artifacts")
    model_path = artifacts / "models" / "defect_lgbm.txt"
    metadata_path = artifacts / "models" / "defect_metadata.json"
    if not model_path.exists() or not metadata_path.exists():
        raise FileNotFoundError("defect model artifacts are missing")
    booster = lgb.Booster(model_file=str(model_path))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    return cfg, booster, metadata


def _vector(features: dict[str, float], names: list[str], medians: dict[str, float]) -> np.ndarray:
    return np.array([float(features.get(name, medians.get(name, 0.0))) for name in names], dtype=float)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Wafer Yield Intelligence API",
        version="0.1.0",
        description="Yield failure prediction, defect pattern classification and wafer risk lookup.",
    )

    @app.get("/health")
    def health() -> dict:
        cfg = load_config()
        artifacts = path_of(cfg, "artifacts")
        metadata_path = artifacts / "models" / "model_metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
        return {
            "status": "ok",
            "yield_model": bool(metadata) and (artifacts / "models" / metadata["model_file"]).exists(),
            "defect_model": (artifacts / "models" / "defect_lgbm.txt").exists(),
            "warehouse": path_of(cfg, "warehouse").exists(),
        }

    @app.get("/model/info")
    def model_info() -> dict:
        cfg = load_config()
        artifacts = path_of(cfg, "artifacts")
        payload: dict = {"config": str(cfg["_config_path"])}
        for key, filename in [("yield", "metrics.json"), ("defect", "defect_metrics.json")]:
            path = artifacts / filename
            if not path.exists():
                raise HTTPException(status_code=503, detail=f"{filename} missing; run training first")
            metrics = json.loads(path.read_text(encoding="utf-8"))
            if key == "yield":
                payload[key] = {
                    "dataset": metrics["dataset"],
                    "split": metrics["split"],
                    "selected_model": metrics["selected_model"],
                    "threshold": metrics["threshold"],
                    "test": metrics["models"][metrics["selected_model"]]["test"],
                }
            else:
                payload[key] = {
                    "dataset": metrics["dataset"],
                    "split": metrics["split"],
                    "selected_model": metrics["selected_model"],
                    "test": {
                        name: metrics["models"][metrics["selected_model"]][name]
                        for name in ("accuracy", "balanced_accuracy", "macro_f1", "weighted_f1")
                    },
                }
        return payload

    @app.post("/predict", response_model=YieldResponse)
    def predict(request: YieldRequest) -> YieldResponse:
        try:
            cfg, model, metadata = _yield_bundle()
        except (OSError, FileNotFoundError) as exc:
            raise HTTPException(status_code=503, detail="yield model not trained yet; run `python -m src.cli train`") from exc

        names = metadata["features"]
        vector = _vector(request.features, names, metadata["feature_medians"])
        if metadata["model_type"] == "lightgbm":
            contribution = model.predict(vector.reshape(1, -1), pred_contrib=True)[0]
            logit = float(contribution.sum())
            probability = float(1 / (1 + np.exp(-logit)))
            drivers = contribution[:-1]
        else:
            probability = float(model.predict_proba(vector.reshape(1, -1))[0, 1])
            means = np.array([metadata["feature_means"][name] for name in names])
            scales = np.array([metadata["feature_scales"][name] for name in names])
            coefficients = np.array([metadata["coefficients"][name] for name in names])
            drivers = coefficients * (vector - means) / scales
        order = np.argsort(-np.abs(drivers))[:5]
        threshold = float(metadata["threshold"])
        bands = metadata["risk_bands"]
        return YieldResponse(
            failure_probability=round(probability, 4),
            risk_band=risk_band(probability, bands["high"], bands["medium"]),
            threshold=threshold,
            alert=probability >= threshold,
            top_drivers=[
                Driver(feature=names[index], contribution=round(float(drivers[index]), 4)) for index in order
            ],
        )

    @app.post("/defect/predict", response_model=DefectResponse)
    def predict_defect(request: DefectRequest) -> DefectResponse:
        try:
            cfg, booster, metadata = _defect_bundle()
        except (OSError, FileNotFoundError) as exc:
            raise HTTPException(status_code=503, detail="defect model not trained yet; run `python -m src.cli defect`") from exc

        names = metadata["features"]
        vector = _vector(request.features, names, metadata["feature_medians"])
        probabilities = booster.predict(vector.reshape(1, -1))[0]
        classes = metadata["classes"]
        best = int(np.argmax(probabilities))
        return DefectResponse(
            predicted_class=classes[best],
            probabilities={name: round(float(prob), 4) for name, prob in zip(classes, probabilities, strict=True)},
        )

    @app.get("/wafer/{wafer_id}")
    def wafer_risk(wafer_id: str) -> dict:
        cfg = load_config()
        try:
            with connect(path_of(cfg, "warehouse")) as conn:
                frame = read_df(conn, "SELECT * FROM ads_wafer_risk WHERE wafer_id = ?", (wafer_id,))
        except Exception as exc:  # noqa: BLE001 - warehouse may not be built yet
            raise HTTPException(status_code=503, detail="warehouse not ready; run `python -m src.cli all`") from exc
        if frame.empty:
            raise HTTPException(status_code=404, detail=f"wafer {wafer_id} not found")
        record = frame.iloc[0].to_dict()
        return {key: (None if pd.isna(value) else value) for key, value in record.items()}

    return app


app = create_app()
