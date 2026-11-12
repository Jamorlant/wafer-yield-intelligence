"""Guard the committed dashboard payload: synthetic label, 3 screens, MATCH chains."""

from __future__ import annotations

import json
from pathlib import Path

DATA = json.loads(
    (Path(__file__).resolve().parents[1] / "dashboard" / "data" / "dashboard.json").read_text(encoding="utf-8")
)


def test_synthetic_disclaimer_is_not_buried():
    assert DATA["meta"]["data_mode"] == "synthetic"
    disclaimer = DATA["meta"]["disclaimer"]
    assert "合成" in disclaimer
    assert "NDA" in disclaimer
    assert "docs/mes_schema.md" in disclaimer


def test_three_screen_sections_exist():
    for key in ("overview", "drill", "alerts", "appendix_secom"):
        assert key in DATA
    assert DATA["overview"]["lines"]
    assert DATA["overview"]["kpis"]["yield_pct"] is not None
    assert DATA["drill"]["step_rows"]
    assert DATA["drill"]["chain"]["points"]
    assert DATA["alerts"]["spc"]


def test_injected_scenarios_match_detected_alerts():
    scenarios = DATA["alerts"]["scenarios"]
    assert len(scenarios) >= 2
    for scenario in scenarios:
        assert scenario["verdict"] == "MATCH"
        assert scenario["detected_rule"] == scenario["expected_rule"]
        assert scenario["step_drop_pp"] < 0
        assert scenario["final_drop_pp"] < 0
        chain = scenario["detail"]["chain"]
        assert chain["drift_start"] <= chain["first_param_breach"] <= chain["first_final_yield_drop"]

    traced = {row["scenario_id"] for row in DATA["alerts"]["rows"]}
    assert "" not in traced, "every alert must be traceable to an injected scenario"


def test_secom_appendix_keeps_real_metrics():
    kpi = DATA["appendix_secom"]["kpi"]
    assert 0.5 < kpi["test_auc"] < 0.75
    assert kpi["test_n"] > 0
    assert DATA["appendix_secom"]["metrics"]["curves"]["roc"]
