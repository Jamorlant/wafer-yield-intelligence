import pytest
from fastapi.testclient import TestClient

from src.serving.app import app


@pytest.fixture()
def client():
    return TestClient(app)


def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert set(payload) >= {"yield_model", "defect_model", "warehouse"}


def test_predict_endpoint_contract(client):
    response = client.post("/predict", json={"features": {}})
    # 503 before training, 200 after training; both are valid contracts.
    assert response.status_code in (200, 503)
    if response.status_code == 200:
        payload = response.json()
        assert 0.0 <= payload["failure_probability"] <= 1.0
        assert payload["risk_band"] in {"low", "medium", "high"}
        assert isinstance(payload["top_drivers"], list)


def test_predict_rejects_non_numeric(client):
    response = client.post("/predict", json={"features": {"S001": "not-a-number"}})
    assert response.status_code == 422


def test_unknown_wafer_returns_404(client):
    response = client.get("/wafer/NOT-A-WAFER")
    assert response.status_code in (404, 503)  # warehouse may not exist in CI
