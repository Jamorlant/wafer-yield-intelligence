"""Static dashboard generator.

Outputs a self-contained ``dashboard/index.html`` plus ``dashboard/data/dashboard.json``
and a vendored ECharts build, so the demo works offline with no extra service.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from src.config import path_of
from src.db import connect, read_df
from src.ingest.wm811k import load_representative_maps
from src.paths import ensure_parent

log = logging.getLogger(__name__)


def _decode_map(item: dict) -> dict:
    cells: list[int] = []
    for value, count in item["rle"]:
        cells.extend([int(value)] * int(count))
    rows, cols = int(item["rows"]), int(item["cols"])
    if len(cells) != rows * cols:
        raise ValueError(f"bad RLE for class {item.get('class')}: {len(cells)} != {rows * cols}")
    return {
        "class": item["class"],
        "rows": rows,
        "cols": cols,
        "density": round(float(item.get("defect_density", 0)), 4),
        "grid": [cells[i * cols : (i + 1) * cols] for i in range(rows)],
    }


def _risk_histogram(frame: pd.DataFrame) -> list[dict]:
    bins = [0, 0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 0.9, 1.01]
    labels = ["0-5%", "5-10%", "10-20%", "20-30%", "30-50%", "50-70%", "70-90%", "90-100%"]
    cut = pd.cut(frame["fail_probability"], bins=bins, labels=labels, right=False)
    counts = cut.value_counts().reindex(labels, fill_value=0)
    return [{"bin": label, "count": int(count)} for label, count in counts.items()]


def build_dashboard(cfg: dict) -> dict:
    artifacts = path_of(cfg, "artifacts")
    dashboard_dir = path_of(cfg, "dashboard")
    metrics = json.loads((artifacts / "metrics.json").read_text(encoding="utf-8"))
    defect = json.loads((artifacts / "defect_metrics.json").read_text(encoding="utf-8"))
    quality = json.loads((artifacts / "quality_report.json").read_text(encoding="utf-8"))
    attributions = json.loads((artifacts / "attributions.json").read_text(encoding="utf-8"))
    maps = [_decode_map(item) for item in load_representative_maps(path_of(cfg, "raw_dir"))]

    with connect(path_of(cfg, "warehouse")) as conn:
        trend = read_df(conn, "SELECT * FROM ads_yield_trend_daily ORDER BY stat_date")
        test_risk = read_df(conn, "SELECT * FROM ads_wafer_risk WHERE split = 'test' ORDER BY fail_probability DESC")
        drift = read_df(conn, "SELECT * FROM ads_sensor_drift")
        defect_dist = read_df(conn, "SELECT * FROM dws_defect_class_dist ORDER BY label_code")

    top_sensors = [row["feature"] for row in metrics["top_features"] if str(row["feature"]).startswith("S")][:6]
    drift_series = []
    for sensor in top_sensors:
        subset = drift.loc[drift["sensor_id"] == sensor].sort_values("stat_date")
        drift_series.append(
            {
                "sensor": sensor,
                "series": [
                    [row.stat_date, None if pd.isna(row.z_score) else round(float(row.z_score), 3)]
                    for row in subset.itertuples()
                ],
            }
        )

    alarms = [
        {
            "wafer_id": row.wafer_id,
            "stat_date": row.stat_date,
            "probability": round(float(row.fail_probability), 4),
            "risk_band": row.risk_band,
            "actual": "fail" if int(row.is_fail) == 1 else "pass",
            "top_driver": row.top_driver_1,
            "contribution": round(float(row.contribution_1), 3),
        }
        for row in test_risk.head(15).itertuples()
    ]

    selected = metrics["selected_model"]
    test = metrics["models"][selected]["test"]
    defect_selected = defect["selected_model"]
    defect_test = defect["models"][defect_selected]

    gain_total = sum(float(row["lgbm_gain"]) for row in metrics["top_features"]) or 1.0
    importance = [
        {
            "feature": row["feature"],
            "gain_share": round(float(row["lgbm_gain"]) / gain_total, 4),
            "effect_mean_abs": round(float(row["effect_mean_abs"]), 4),
        }
        for row in metrics["top_features"][:10]
    ]

    data = {
        "meta": {
            "title": "Wafer Yield Intelligence",
            "subtitle": "半导体良率预测与晶圆缺陷识别 · SECOM + WM-811K",
            "generated_at": datetime.now(UTC).astimezone().strftime("%Y-%m-%d %H:%M"),
            "sources": [
                "SECOM (UCI id 179, CC BY 4.0)",
                "WM-811K derived 26-feature subset (CC0)",
            ],
        },
        "kpi": {
            "test_auc": test["auc"],
            "test_auc_ci_low": test["auc_ci95"][0],
            "test_auc_ci_high": test["auc_ci95"][1],
            "backtest_auc_mean": metrics["backtest"]["summary"][selected]["auc_mean"],
            "backtest_auc_std": metrics["backtest"]["summary"][selected]["auc_std"],
            "test_pr_auc": test["pr_auc"],
            "test_recall": test["recall"],
            "test_alerts": test["alerts"],
            "test_fails": test["actual_fail"],
            "test_n": test["n"],
            "target_fpr": metrics["target_fpr"],
            "defect_macro_f1": defect_test["macro_f1"],
            "defect_accuracy": defect_test["accuracy"],
            "quality_status": quality["overall_status"],
            "quality_checks": len(quality["checks"]),
            "fail_rate": metrics["dataset"]["fail_rate"],
        },
        "metrics": metrics,
        "defect": defect,
        "quality": quality,
        "trend": [
            {
                "date": row.stat_date,
                "yield_pct": round(float(row.actual_yield) * 100, 2),
                "risk_pct": None if pd.isna(row.avg_risk) else round(float(row.avg_risk) * 100, 2),
                "alerts": None if pd.isna(row.high_risk_cnt) else int(row.high_risk_cnt),
            }
            for row in trend.itertuples()
        ],
        "risk_hist": _risk_histogram(test_risk),
        "alarms": alarms,
        "attributions": attributions,
        "drift": drift_series,
        "defect_dist": [
            {"label": row.label_name, "count": int(row.cnt), "share": round(float(row.share), 4)}
            for row in defect_dist.itertuples()
        ],
        "importance": importance,
        "maps": maps,
        "business": test["business"],
    }

    ensure_parent(dashboard_dir / "data" / "dashboard.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    template = (Path(__file__).parent / "template.html").read_text(encoding="utf-8")
    html = template.replace("__DASHBOARD_DATA__", json.dumps(data, ensure_ascii=False))
    ensure_parent(dashboard_dir / "index.html").write_text(html, encoding="utf-8")
    log.info("dashboard written to %s", dashboard_dir / "index.html")
    return data
