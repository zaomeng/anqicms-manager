from __future__ import annotations

import sys
from pathlib import Path


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def bundled_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS")).resolve()
    return app_dir()


def resolve_external_or_bundled(relative_path: str) -> Path:
    external = app_dir() / relative_path
    if external.exists():
        return external
    bundled = bundled_dir() / relative_path
    return bundled
