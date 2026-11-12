"""The synthetic MES generator must be reproducible and physically consistent."""

from __future__ import annotations

import copy

import numpy as np

from src.config import load_config
from src.synthetic.mes import generate_mes


def _cfg(days: int = 4, start: str = "2026-11-08", lots_per_shift: int = 10) -> dict:
    cfg = copy.deepcopy(load_config())
    cfg["mes"]["days"] = days
    cfg["mes"]["start_date"] = start
    cfg["mes"]["lots_per_shift"] = lots_per_shift
    # keep only S1 so the small window never excludes a configured scenario
    cfg["mes"]["scenarios"] = [s for s in cfg["mes"]["scenarios"] if s["id"] == "S1"]
    return cfg


def test_expected_row_counts():
    cfg = _cfg(days=4, lots_per_shift=10)
    data = generate_mes(cfg)
    mes = cfg["mes"]
    expected_lots = len(mes["lines"]) * 4 * 3 * 10
    assert data["stats"]["lots"] == expected_lots
    assert data["stats"]["process_rows"] == expected_lots * len(mes["processes"])
    assert len(data["process"]) == expected_lots * len(mes["processes"])


def test_quantity_conservation_and_route_continuity():
    data = generate_mes(_cfg())
    process = data["process"]
    assert (process["input_qty"] == process["output_qty"] + process["defect_qty"]).all()
    assert (process["defect_qty"] >= 0).all()
    assert (process["output_qty"] > 0).all()

    output = process.pivot(index="lot_id", columns="process_seq", values="output_qty")
    inputs = process.pivot(index="lot_id", columns="process_seq", values="input_qty")
    for seq in range(1, inputs.columns.max()):
        assert (output[seq] == inputs[seq + 1]).all()

    lot = data["lots"].set_index("lot_id")
    last = process[process["process_seq"] == process["process_seq"].max()].set_index("lot_id")
    assert (last["output_qty"] == lot["good_qty"]).all()
    # final yield is rolled throughput: last output / first input (lot_size)
    assert np.allclose(last["output_qty"] / lot["lot_size"], lot["yield_final"])


def test_same_seed_is_byte_reproducible():
    first = generate_mes(_cfg())
    second = generate_mes(_cfg())
    assert first["stats"]["digest"] == second["stats"]["digest"]
    assert first["lots"]["good_qty"].equals(second["lots"]["good_qty"])
    assert first["process"][["input_qty", "output_qty", "param_1"]].equals(
        second["process"][["input_qty", "output_qty", "param_1"]]
    )


def test_scenario_rows_are_stamped_with_ground_truth():
    # 2026-11-08 + 4 days covers S1 (2026-11-09 00:00 -> 2026-11-11 00:00).
    data = generate_mes(_cfg(days=4, start="2026-11-08", lots_per_shift=20))
    s1 = [s for s in data["scenarios"] if s["scenario_id"] == "S1"]
    assert s1, "S1 should be inside the configured window"
    process = data["process"]
    stamped = process[process["scenario_id"] == "S1"]
    assert len(stamped) == s1[0]["step_row_cnt"]
    assert (stamped["process_code"] == "DIE_ATTACH").all()
    assert (stamped["equipment_id"] == "EQ-DA-02").all()
    assert (stamped["line_code"] == "LINE-B").all()
