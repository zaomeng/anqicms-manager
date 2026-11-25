from __future__ import annotations

import sys
import shutil
from pathlib import Path

from core.runtime_paths import app_dir, bundled_dir
from ui.main_window import run_app


def resolve_runtime_config_path(default_name: str = "config.ini") -> Path:
    if getattr(sys, "frozen", False):
        external = app_dir() / default_name
        if external.exists():
            return external
        bundled = bundled_dir() / default_name
        if bundled.exists():
            external.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(bundled, external)
            return external
        return external
    else:
        local = app_dir() / default_name
        if local.exists():
            return local
    return Path(default_name).resolve()


def main() -> None:
    run_app(resolve_runtime_config_path())


if __name__ == "__main__":
    main()
