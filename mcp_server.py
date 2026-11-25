"""Standalone stdio MCP entrypoint for AnqiCMS site-group management."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from core.runtime_paths import app_dir
from core.sitegroup_mcp_server import SiteGroupMcpServer


def resolve_config_path(config_arg: str | None = None) -> Path:
    """Resolve ``config.ini`` path from CLI argument or workspace/executable dir."""
    if config_arg:
        return Path(config_arg).resolve()
    local = app_dir() / "config.ini"
    if local.exists():
        return local.resolve()
    return Path("config.ini").resolve()


def _configure_stdio_utf8() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AnqiCMS 站群管理工具 MCP Stdio 服务端",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件路径（默认自动查找 config.ini）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    _configure_stdio_utf8()
    config_path = resolve_config_path(args.config)
    try:
        with SiteGroupMcpServer(config_path) as server:
            server.serve_stdio()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
