"""Deterministic synthetic MES data generator.

Grain
-----
One row per ``lot x process step``. With the shipped configuration that is
3 lines x 90 days x 3 shifts x 100 lots x 6 steps = **486,000** step records
(81,000 lots).

Reproducibility
---------------
Everything is drawn from ``numpy.random.default_rng(mes.seed)`` and every
timestamp is derived from ``mes.start_date``; re-running the pipeline yields
byte-identical quantities and parameters (verified by a digest in
``artifacts/mes_generation.json`` and by ``tests/test_synthetic.py``).

Anomaly scenarios
-----------------
Each entry in ``mes.scenarios`` injects a parameter drift on one
(line, process, equipment) and a matching yield penalty. The affected rows are
stamped with ``scenario_id`` in ODS/DWD, which is the ground truth used by the
quality gate to prove that SPC alerts and injected anomalies line up 1:1.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

SHIFT_START_HOUR = {"DAY": 8, "SWING": 16, "NIGHT": 0}
SHIFT_ORDER = ("DAY", "SWING", "NIGHT")


def _shift_code(hours: np.ndarray) -> np.ndarray:
    """Map an hour of day to the 8-hour shift that owns it."""
    codes = np.where(hours < 8, "NIGHT", np.where(hours < 16, "DAY", "SWING"))
    return codes


def _equipment_index(process_idx: np.ndarray, product_idx: np.ndarray) -> np.ndarray:
    """Tool assignment.

    DIE_ATTACH is recipe-locked per product (one tool per product family),
    the other steps rotate tools by product/step. This keeps the drill-down
    honest: a tool-level anomaly is not identical to a product-level anomaly.
    """
    return np.where(process_idx == 2, product_idx % 3, (product_idx + process_idx) % 3)


def _scenario_mask(
    line_code: np.ndarray,
    process_code: np.ndarray,
    equipment_id: np.ndarray,
    process_min: np.ndarray,
    scenario: dict,
) -> np.ndarray:
    start = int(np.datetime64(scenario["start_ts"], "m").astype("int64"))
    end = start + int(scenario["duration_hours"] * 60)
    return (
        (line_code == scenario["line"])
        & (process_code == scenario["process"])
        & (equipment_id == scenario["equipment"])
        & (process_min >= start)
        & (process_min < end)
    )


def _ramp_fraction(minutes_from_start: np.ndarray, ramp_hours: float, lag_hours: float) -> np.ndarray:
    """Linear drift fraction in [0, 1] with a lag between cause and effect."""
    ramp = max(float(ramp_hours), 1e-6)
    return np.clip((minutes_from_start - lag_hours * 60) / (ramp * 60), 0.0, 1.0)


def _digest(frame: pd.DataFrame, columns: list[str]) -> str:
    rounded = frame[columns].astype("float64").round(6).to_numpy()
    return hashlib.sha256(np.ascontiguousarray(rounded).tobytes()).hexdigest()[:16]


def generate_mes(cfg: dict) -> dict:
    """Generate lots, process steps and the scenario manifest."""
    mes = cfg["mes"]
    seed = int(mes["seed"])
    rng = np.random.default_rng(seed)

    lines = mes["lines"]
    products = mes["products"]
    processes = mes["processes"]
    n_lines, n_products, n_proc = len(lines), len(products), len(processes)
    days = int(mes["days"])
    lots_per_shift = int(mes["lots_per_shift"])
    lot_size = int(mes["lot_size"])
    shift_hours = int(mes["shift_hours"])
    cycle_min = float(mes["process_cycle_hours"]) * 60
    start_day = np.datetime64(mes["start_date"], "D")
    base_min = int(start_day.astype("datetime64[m]").astype("int64"))

    n_per_line_day = len(SHIFT_ORDER) * lots_per_shift
    n_lots = n_lines * days * n_per_line_day

    # ---------------------------------------------------------------- indexes
    idx = np.arange(n_lots)
    within = idx % lots_per_shift
    block = idx // lots_per_shift
    shift_idx = block % len(SHIFT_ORDER)
    day_idx = (block // len(SHIFT_ORDER)) % days
    line_idx = block // (len(SHIFT_ORDER) * days)

    line_code = np.array([lines[i]["code"] for i in range(n_lines)], dtype=object)[line_idx]
    shift_code = np.array(SHIFT_ORDER, dtype=object)[shift_idx]

    mix = np.array([float(p["mix"]) for p in products])
    mix = mix / mix.sum()
    product_idx = rng.choice(n_products, size=n_lots, p=mix)
    product_code = np.array([p["code"] for p in products], dtype=object)[product_idx]

    shift_start_hour = np.array([SHIFT_START_HOUR[s] for s in SHIFT_ORDER])[shift_idx]
    spacing = shift_hours * 60 / lots_per_shift
    jitter = rng.integers(0, 3, size=n_lots)  # minutes
    lot_start_min = base_min + day_idx * 1440 + shift_start_hour * 60 + (within * spacing).astype(int) + jitter

    lot_day = pd.to_datetime(lot_start_min, unit="m").strftime("%Y%m%d")
    lot_id = np.array(
        [
            f"{line_code[i][-1]}{lot_day[i]}{shift_code[i][0]}{within[i]:03d}"
            for i in range(n_lots)
        ],
        dtype=object,
    )

    # ------------------------------------------------------------- parameters
    process_codes = [p["code"] for p in processes]
    param_base = np.zeros((n_proc, 5))
    param_sd = np.zeros((n_proc, 5))
    param_spec_rows = []
    for pi, proc in enumerate(processes):
        for qi, param in enumerate(proc["params"]):
            param_base[pi, qi] = float(param["base"])
            param_sd[pi, qi] = float(param["sd"])
            param_spec_rows.append(
                {
                    "process_code": proc["code"],
                    "process_name": proc["name"],
                    "process_seq": pi + 1,
                    "param_index": qi + 1,
                    "param_code": param["code"],
                    "param_name": param["name"],
                    "unit": param["unit"],
                    "base_value": float(param["base"]),
                    "sd_value": float(param["sd"]),
                    "is_key": 1 if param.get("key") else 0,
                    "owner_role": proc["owner"],
                }
            )

    # process timestamps: lot start + step index * cycle + small queue jitter
    step_min = (np.arange(1, n_proc + 1) * cycle_min).astype(int)
    proc_min = lot_start_min[:, None] + step_min[None, :] + rng.integers(0, 30, size=(n_lots, n_proc))
    proc_dt = pd.to_datetime(proc_min.ravel(), unit="m")
    proc_hours = ((proc_min.ravel() % 1440) // 60).astype(int)

    proc_code_flat = np.tile(np.array(process_codes, dtype=object), n_lots)
    line_flat = np.repeat(line_code, n_proc)
    product_flat = np.repeat(product_code, n_proc)
    product_idx_flat = np.repeat(product_idx, n_proc)
    process_idx = np.tile(np.arange(n_proc), n_lots)
    equipment_idx = _equipment_index(process_idx, product_idx_flat)
    equipment_id = np.array(
        [f"{processes[p]['equipment_prefix']}-{e + 1:02d}" for p, e in zip(process_idx, equipment_idx, strict=True)],
        dtype=object,
    )
    process_shift = _shift_code(proc_hours)

    # parameters: base + iid noise (+ injected drift)
    noise = rng.normal(0.0, 1.0, size=(n_lots * n_proc, 5))
    params = param_base[process_idx] + noise * param_sd[process_idx]
    scenario_id = np.full(n_lots * n_proc, "", dtype=object)

    # ------------------------------------------------------------ step yields
    line_bias = np.array([float(line["yield_bias"]) for line in lines])[line_idx]
    product_bias = np.array([float(p.get("yield_bias", 0.0)) for p in products])[product_idx]
    seasonal = 0.0018 * np.sin(2 * np.pi * day_idx / 30.0)
    lot_noise = rng.normal(0.0, 0.0008, size=n_lots)

    base_yield = np.array([float(p["base_yield"]) for p in processes])
    step_yield = (
        base_yield[None, :]
        + line_bias[:, None] / n_proc
        + product_bias[:, None] * 0.5
        + seasonal[:, None]
        + lot_noise[:, None]
        + rng.normal(0.0, 0.0012, size=(n_lots, n_proc))
    )

    scenario_rows = []
    for scenario in mes.get("scenarios", []):
        mask_flat = _scenario_mask(line_flat, proc_code_flat, equipment_id, proc_min.ravel(), scenario)
        if not mask_flat.any():
            raise ValueError(f"scenario {scenario['id']} matched no rows; check line/process/equipment config")
        offset = int(np.datetime64(scenario["start_ts"], "m").astype("int64"))
        minutes = proc_min.ravel()[mask_flat] - offset

        pi = process_codes.index(scenario["process"])
        qi = [p["code"] for p in processes[pi]["params"]].index(scenario["param"])

        drift_fraction = _ramp_fraction(minutes, float(scenario["ramp_hours"]), 0.0)
        params[mask_flat, qi] += drift_fraction * float(scenario["magnitude_sigma"]) * param_sd[pi, qi]

        # Yield effect starts `yield_lag_hours` after the drift begins.
        penalty_fraction = _ramp_fraction(minutes, float(scenario["ramp_hours"]), float(scenario["yield_lag_hours"]))
        penalty = penalty_fraction * float(scenario["yield_penalty_pp"]) / 100.0

        lot_rows = mask_flat.reshape(n_lots, n_proc)[:, pi]
        step_yield[np.ix_(lot_rows, [pi])] -= penalty.reshape(-1, 1)
        scenario_id[mask_flat] = scenario["id"]

        start_dt = pd.Timestamp(scenario["start_ts"])
        scenario_rows.append(
            {
                "scenario_id": scenario["id"],
                "title": scenario["title"],
                "line_code": scenario["line"],
                "process_code": scenario["process"],
                "equipment_id": scenario["equipment"],
                "param_code": scenario["param"],
                "param_name": processes[pi]["params"][qi]["name"],
                "unit": processes[pi]["params"][qi]["unit"],
                "start_ts": start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                "end_ts": (start_dt + timedelta(hours=int(scenario["duration_hours"]))).strftime("%Y-%m-%dT%H:%M:%S"),
                "ramp_hours": float(scenario["ramp_hours"]),
                "magnitude_sigma": float(scenario["magnitude_sigma"]),
                "magnitude_value": round(float(scenario["magnitude_sigma"]) * float(param_sd[pi, qi]), 4),
                "yield_penalty_pp": float(scenario["yield_penalty_pp"]),
                "yield_lag_hours": float(scenario["yield_lag_hours"]),
                "defect_code": scenario["defect_code"],
                "owner_role": scenario["owner"],
                "expected_rule": scenario["expected_rule"],
                "step_row_cnt": int(mask_flat.sum()),
                "lot_cnt": int(lot_rows.sum()),
            }
        )

    step_yield = np.clip(step_yield, 0.85, 0.9995)

    # ------------------------------------------------- quantities (binomial)
    input_qty = np.zeros((n_lots, n_proc), dtype=np.int64)
    output_qty = np.zeros((n_lots, n_proc), dtype=np.int64)
    defect_qty = np.zeros((n_lots, n_proc), dtype=np.int64)
    input_qty[:, 0] = lot_size
    for k in range(n_proc):
        defect_qty[:, k] = rng.binomial(input_qty[:, k], 1.0 - step_yield[:, k])
        output_qty[:, k] = input_qty[:, k] - defect_qty[:, k]
        if k + 1 < n_proc:
            input_qty[:, k + 1] = output_qty[:, k]

    # defect codes: dominant code per step, overridden by the scenario code
    default_code = np.array([p["defect_code"] for p in processes], dtype=object)
    defect_code = np.tile(default_code, n_lots)
    for scenario in scenario_rows:
        mask = scenario_id == scenario["scenario_id"]
        defect_code[mask] = scenario["defect_code"]

    # ------------------------------------------------------------- dataframes
    lots = pd.DataFrame(
        {
            "lot_id": lot_id,
            "line_code": line_code,
            "product_code": product_code,
            "start_shift": shift_code,
            "lot_start_ts": pd.to_datetime(lot_start_min, unit="m").strftime("%Y-%m-%dT%H:%M:%S"),
            "lot_size": lot_size,
            "good_qty": output_qty[:, -1],
            "yield_final": output_qty[:, -1] / lot_size,
            "scenario_id": np.where(
                (scenario_id.reshape(n_lots, n_proc) != "").any(axis=1),
                scenario_id.reshape(n_lots, n_proc).max(axis=1),
                "",
            ),
        }
    )

    process = pd.DataFrame(
        {
            "lot_id": np.repeat(lot_id, n_proc),
            "line_code": line_flat,
            "product_code": product_flat,
            "process_seq": process_idx + 1,
            "process_code": proc_code_flat,
            "equipment_id": equipment_id,
            "process_ts": proc_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "shift_code": process_shift,
            "input_qty": input_qty.ravel(),
            "output_qty": output_qty.ravel(),
            "defect_qty": defect_qty.ravel(),
            "defect_code": defect_code,
            "scenario_id": scenario_id,
        }
    )
    for qi in range(5):
        process[f"param_{qi + 1}"] = np.round(params[:, qi], 4)

    params_spec = pd.DataFrame(param_spec_rows)
    stats = {
        "seed": seed,
        "start_date": mes["start_date"],
        "days": days,
        "lots": int(n_lots),
        "process_rows": int(n_lots * n_proc),
        "lines": n_lines,
        "products": n_products,
        "processes": n_proc,
        "mean_final_yield": round(float(lots["yield_final"].mean()), 6),
        "digest": _digest(process, ["input_qty", "output_qty", "defect_qty", "param_1", "param_2"]),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    return {"lots": lots, "process": process, "param_spec": params_spec, "scenarios": scenario_rows, "stats": stats}
