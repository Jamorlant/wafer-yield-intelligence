"""Filesystem helpers: every path is resolved relative to the repository root."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def resolve(path: str | Path) -> Path:
    """Resolve *path* against the repository root and create the parent dir."""
    target = (REPO_ROOT / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    return target


def ensure_parent(path: str | Path) -> Path:
    target = resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target
