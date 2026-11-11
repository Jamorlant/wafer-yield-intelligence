"""Configuration loading.

The YAML file is the single source of truth for data locations, split ratios,
model hyper-parameters and monitoring thresholds. Nothing is hard-coded in the
pipeline modules.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from src.paths import REPO_ROOT, resolve


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    config_path = Path(path) if path else REPO_ROOT / "configs" / "config.yaml"
    with open(config_path, encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle)
    cfg["_config_path"] = str(config_path)
    return cfg


def path_of(cfg: dict[str, Any], key: str) -> Path:
    """Return an absolute path for a key under the ``paths`` section."""
    return resolve(cfg["paths"][key])


def config_fingerprint(cfg: dict[str, Any]) -> str:
    """Stable hash of the configuration (without the injected private key)."""
    payload = {k: v for k, v in cfg.items() if not k.startswith("_")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]
