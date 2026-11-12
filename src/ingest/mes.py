"""Load the MES extract.

Two providers, selected by ``mes.source``:

``synthetic``
    The deterministic generator in :mod:`src.synthetic.mes`. Used for the demo;
    it is labelled as synthetic in the dashboard, README and docs.

``export``
    A real MES/EAP export dropped into ``mes.export_dir`` as ``lots.csv`` and
    ``process.csv``. The column contract is identical to the synthetic output
    (see ``docs/mes_schema.md``), so nothing downstream changes: same DWD
    validation, same SPC rules, same dashboard.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from src.paths import resolve

log = logging.getLogger(__name__)

LOT_COLUMNS = ["lot_id", "line_code", "product_code", "start_shift", "lot_start_ts", "lot_size", "good_qty", "yield_final"]
PROCESS_COLUMNS = [
    "lot_id",
    "line_code",
    "product_code",
    "process_seq",
    "process_code",
    "equipment_id",
    "process_ts",
    "shift_code",
    "input_qty",
    "output_qty",
    "defect_qty",
    "defect_code",
    "param_1",
    "param_2",
    "param_3",
    "param_4",
    "param_5",
]


def param_spec(cfg: dict) -> pd.DataFrame:
    rows = []
    for pi, proc in enumerate(cfg["mes"]["processes"]):
        for qi, param in enumerate(proc["params"]):
            rows.append(
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
    return pd.DataFrame(rows)


def _read_export(cfg: dict) -> dict:
    export_dir = resolve(cfg["mes"]["export_dir"])
    lots_path = Path(export_dir) / "lots.csv"
    process_path = Path(export_dir) / "process.csv"
    if not lots_path.exists() or not process_path.exists():
        raise FileNotFoundError(
            f"mes.source=export but {lots_path} / {process_path} is missing. "
            "See docs/mes_schema.md for the export contract."
        )
    lots = pd.read_csv(lots_path, dtype={"lot_id": str, "product_code": str, "line_code": str})
    process = pd.read_csv(process_path, dtype={"lot_id": str})
    missing_lots = [c for c in LOT_COLUMNS if c not in lots.columns]
    missing_process = [c for c in PROCESS_COLUMNS if c not in process.columns]
    if missing_lots or missing_process:
        raise ValueError(f"MES export schema mismatch: lots missing {missing_lots}, process missing {missing_process}")
    process["scenario_id"] = ""
    stats = {
        "seed": None,
        "source": "mes_export",
        "days": int(pd.to_datetime(lots["lot_start_ts"]).dt.date.nunique()),
        "lots": int(len(lots)),
        "process_rows": int(len(process)),
        "mean_final_yield": round(float(lots["yield_final"].mean()), 6),
        "digest": "",
        "generated_at": None,
    }
    return {
        "lots": lots[LOT_COLUMNS],
        "process": process[PROCESS_COLUMNS + ["scenario_id"]],
        "param_spec": param_spec(cfg),
        "scenarios": [],
        "stats": stats,
    }


def load_mes_source(cfg: dict) -> dict:
    source = cfg["mes"].get("source", "synthetic")
    if source == "export":
        log.info("loading MES export from %s", resolve(cfg["mes"]["export_dir"]))
        return _read_export(cfg)
    from src.synthetic.mes import generate_mes

    return generate_mes(cfg)
