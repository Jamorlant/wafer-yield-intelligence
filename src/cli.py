"""Command-line entry point.

Examples
--------
python -m src.cli all          # download -> warehouse -> quality -> models -> dashboard
python -m src.cli warehouse    # rebuild layers (idempotent)
python -m src.cli train        # yield model only
python -m src.cli defect       # 9-class defect model only
python -m src.cli serve        # FastAPI on http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from src.config import load_config, path_of
from src.logging_utils import setup_logging

log = logging.getLogger(__name__)


def _quality_gate(cfg: dict) -> bool:
    from src.quality.checks import run_quality_checks

    report = run_quality_checks(cfg)
    for check in report["checks"]:
        log.info("[%s] %s: %s", check["status"].upper(), check["check_name"], check["observed"])
    return report["overall_status"] != "fail"


def cmd_download(cfg: dict, args) -> int:
    from src.ingest.download import download_all

    assets = download_all(cfg, force=args.force)
    for asset in assets:
        log.info("%s (%s bytes) sha256=%s", asset["asset_name"], f"{asset['bytes']:,}", asset["sha256"][:12])
    return 0


def cmd_warehouse(cfg: dict, args) -> int:
    from src.warehouse.build import build_warehouse

    summary = build_warehouse(cfg, resume=args.resume)
    log.info("warehouse summary: %s", json.dumps(summary, ensure_ascii=False))
    return 0


def cmd_quality(cfg: dict, args) -> int:
    return 0 if _quality_gate(cfg) else 1


def cmd_train(cfg: dict, args) -> int:
    from src.modeling.train import train_yield_model

    metrics = train_yield_model(cfg)
    selected = metrics["selected_model"]
    test = metrics["models"][selected]["test"]
    log.info("test AUC=%.4f PR-AUC=%.4f recall=%.3f alerts=%d", test["auc"], test["pr_auc"], test["recall"], test["alerts"])
    return 0


def cmd_defect(cfg: dict, args) -> int:
    from src.modeling.defect import train_defect_model

    metrics = train_defect_model(cfg)
    selected = metrics["selected_model"]
    log.info(
        "defect macro-F1=%.4f accuracy=%.4f",
        metrics["models"][selected]["macro_f1"],
        metrics["models"][selected]["accuracy"],
    )
    return 0


def cmd_dashboard(cfg: dict, args) -> int:
    from src.dashboard.build import build_dashboard

    build_dashboard(cfg)
    log.info("open %s in a browser", path_of(cfg, "dashboard") / "index.html")
    return 0


def cmd_serve(cfg: dict, args) -> int:
    import uvicorn

    uvicorn.run("src.serving.app:app", host=args.host, port=args.port, reload=False)
    return 0


def cmd_all(cfg: dict, args) -> int:
    for step in (cmd_download, cmd_warehouse, cmd_quality, cmd_train, cmd_defect, cmd_dashboard):
        code = step(cfg, args)
        if code != 0:
            log.error("pipeline stopped at %s", step.__name__)
            return code
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.cli", description="Wafer Yield Intelligence pipeline")
    parser.add_argument("--config", default=None, help="alternative configs/*.yaml path")
    parser.add_argument("--log-level", default=None, help="override project.log_level")
    sub = parser.add_subparsers(dest="command", required=True)

    download = sub.add_parser("download", help="fetch and verify raw datasets")
    download.add_argument("--force", action="store_true", help="re-download even when the hash matches")
    download.set_defaults(func=cmd_download)

    warehouse = sub.add_parser("warehouse", help="build ODS -> DWD -> DWS -> ADS")
    warehouse.add_argument("--no-resume", dest="resume", action="store_false", help="always start a fresh run")
    warehouse.set_defaults(func=cmd_warehouse, resume=True)

    quality = sub.add_parser("quality", help="run the data-quality gate")
    quality.set_defaults(func=cmd_quality)

    train = sub.add_parser("train", help="train the yield model")
    train.set_defaults(func=cmd_train)

    defect = sub.add_parser("defect", help="train the 9-class defect model")
    defect.set_defaults(func=cmd_defect)

    dashboard = sub.add_parser("dashboard", help="render the static dashboard")
    dashboard.set_defaults(func=cmd_dashboard)

    serve = sub.add_parser("serve", help="start the FastAPI service")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)

    run_all = sub.add_parser("all", help="run every stage end to end")
    run_all.set_defaults(func=cmd_all, force=False, resume=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(args.log_level or cfg["project"]["log_level"], path_of(cfg, "logs") / "pipeline.log")
    log.info("command=%s config=%s", args.command, cfg["_config_path"])
    return int(args.func(cfg, args))


if __name__ == "__main__":
    sys.exit(main())
