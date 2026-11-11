"""Idempotent dataset downloader.

Policy
------
* Raw data is never committed. ``python -m src.cli download`` fetches it.
* Every download is verified against a SHA-256 recorded in configs/config.yaml.
* Existing files with a matching hash are reused, so re-running is cheap and
  safe (idempotency). ``--force`` re-downloads.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from src.config import path_of

log = logging.getLogger(__name__)

USER_AGENT = "wafer-yield-intelligence/0.1 (+https://github.com)"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    headers = {"User-Agent": USER_AGENT}
    if "api.github.com" in url:
        # Ask the contents API for raw bytes instead of its JSON envelope.
        headers["Accept"] = "application/vnd.github.raw"
    request = urllib.request.Request(url, headers=headers)
    log.info("downloading %s", url)
    with urllib.request.urlopen(request, timeout=120) as response, open(tmp, "wb") as handle:
        shutil.copyfileobj(response, handle)
    tmp.replace(target)


def _ensure(url: str, target: Path, expected: str, force: bool) -> dict:
    expected = expected.upper()
    if target.exists() and not force:
        actual = sha256_of(target)
        if actual == expected:
            log.info("reuse %s (sha256 ok)", target.name)
            return _asset(target, url, actual)
        log.warning("hash mismatch for %s, re-downloading", target.name)
    _download(url, target)
    actual = sha256_of(target)
    if actual != expected:
        raise RuntimeError(f"sha256 mismatch for {target.name}: {actual} != {expected}")
    return _asset(target, url, actual)


def _asset(path: Path, source_url: str, digest: str) -> dict:
    return {
        "asset_name": path.name,
        "source_url": source_url,
        "sha256": digest,
        "bytes": path.stat().st_size,
        "downloaded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "path": str(path),
    }


def download_all(cfg: dict, force: bool = False) -> list[dict]:
    raw_dir = path_of(cfg, "raw_dir")
    raw_dir.mkdir(parents=True, exist_ok=True)
    assets: list[dict] = []

    # --- SECOM (UCI, public download, no login) -----------------------------
    secom_cfg = cfg["secom"]
    archive = raw_dir / "secom.zip"
    assets.append(_ensure(secom_cfg["download_url"], archive, secom_cfg["archive_sha256"], force))

    extract_dir = raw_dir / "secom"
    expected_files = [extract_dir / "secom.data", extract_dir / "secom_labels.data"]
    if force or not all(path.exists() for path in expected_files):
        log.info("extracting %s", archive.name)
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(extract_dir)
    if not all(path.exists() for path in expected_files):
        raise RuntimeError("SECOM archive extracted without secom.data / secom_labels.data")

    # --- WM-811K derived features (CC0 source, public GitHub mirror) --------
    wm_cfg = cfg["wm811k"]
    npz_path = raw_dir / "wm811k_labeled.npz"
    assets.append(_ensure(wm_cfg["download_url"], npz_path, wm_cfg["npz_sha256"], force))

    representative_path = raw_dir / "representative_wafers.json"
    assets.append(
        _ensure(
            wm_cfg["representative_url"],
            representative_path,
            wm_cfg["representative_sha256"],
            force,
        )
    )
    return assets
