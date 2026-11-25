"""Unified CLI and desktop launcher for the AnQiCMS Web Console."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import threading
import webbrowser
from collections.abc import Callable, Sequence
from pathlib import Path

from core.app_info import APP_NAME, APP_VERSION
from core.runtime_paths import app_dir, bundled_dir
from web.server import WebConsoleServer

_DEFAULT_CONFIG_NAME = "config.ini"


def _configure_stdio_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def resolve_runtime_config_path(
    config_arg: str | Path | None = None,
    default_name: str = _DEFAULT_CONFIG_NAME,
) -> Path:
    """Resolve ``config.ini`` path from CLI argument or exe/workspace directory."""
    if config_arg is not None and str(config_arg).strip():
        return Path(str(config_arg).strip()).resolve()

    if getattr(sys, "frozen", False):
        external = app_dir() / default_name
        if external.exists():
            return external
        bundled = bundled_dir() / default_name
        if bundled.exists():
            external.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(bundled, external)
        return external

    local = app_dir() / default_name
    if local.exists():
        return local
    return Path(default_name).resolve()


def build_parser() -> argparse.ArgumentParser:
    """Create CLI argument parser for the Web Console launcher."""
    parser = argparse.ArgumentParser(
        description=f"{APP_NAME} Web 控制台启动器",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="监听地址（默认 127.0.0.1；VPS/挂机宝远程访问可设为 0.0.0.0）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="监听端口（默认 0 自动分配空闲端口，例如可指定 18900）",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件路径（默认自动查找程序或工作目录下的 config.ini）",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="无头服务模式：仅启动 Web 与 MCP 服务，不自动打开浏览器窗口",
    )
    parser.add_argument(
        "--mode",
        choices=("app", "browser"),
        default="app",
        help="窗口打开模式：app（优先 Edge/Chrome 独立应用窗口）或 browser（默认浏览器标签页）",
    )
    parser.add_argument(
        "--tk",
        action="store_true",
        help="启动旧版 Tkinter 桌面界面",
    )
    parser.add_argument(
        "--mcp-stdio",
        action="store_true",
        help="以 MCP Stdio 服务端模式运行（供 Cursor / Claude Desktop 等本地拉起）",
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the launcher."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.port < 0 or args.port > 65535:
        parser.error("--port 必须在 0 到 65535 之间")
    return args


def _candidate_browser_paths() -> list[Path]:
    """Return common Edge/Chrome executable locations across platforms."""
    candidates: list[Path] = []
    if sys.platform.startswith("win"):
        roots = [
            os.environ.get("PROGRAMFILES", ""),
            os.environ.get("PROGRAMFILES(X86)", ""),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        rel_paths = (
            Path("Microsoft") / "Edge" / "Application" / "msedge.exe",
            Path("Google") / "Chrome" / "Application" / "chrome.exe",
            Path("Chromium") / "Application" / "chrome.exe",
        )
        for root in roots:
            if not root:
                continue
            base = Path(root)
            for rel in rel_paths:
                candidates.append(base / rel)
    elif sys.platform == "darwin":
        candidates.extend(
            [
                Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
            ]
        )
    else:
        candidates.extend(
            [
                Path("/usr/bin/microsoft-edge"),
                Path("/usr/bin/google-chrome"),
                Path("/usr/bin/chromium"),
                Path("/usr/bin/chromium-browser"),
            ]
        )
    return candidates


def find_app_browser() -> str | None:
    """Find an installed Edge/Chrome/Chromium executable supporting ``--app=``."""
    for name in ("msedge", "chrome", "google-chrome", "chromium", "chromium-browser"):
        resolved = shutil.which(name)
        if resolved:
            return resolved
    for path in _candidate_browser_paths():
        if path.is_file():
            return str(path)
    return None


def open_console_window(url: str, mode: str = "app") -> bool:
    """Open the Web Console in standalone app mode or a standard browser tab."""
    if mode == "app":
        browser_exe = find_app_browser()
        if browser_exe:
            try:
                subprocess.Popen(
                    [browser_exe, f"--app={url}"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            except OSError:
                pass
    return bool(webbrowser.open_new_tab(url))


def _format_startup_banner(
    server: WebConsoleServer,
    config_path: Path,
    *,
    headless: bool,
) -> str:
    local_url = server.url
    mcp_url = f"{local_url}/mcp"
    lines = [
        "=" * 60,
        f"{APP_NAME} v{APP_VERSION} - Web 站群控制台",
        f"控制台地址 : {local_url}",
        f"MCP 接口   : {mcp_url}",
        f"监听绑定   : {server.host}:{server.port}",
        f"配置文件   : {config_path}",
    ]
    if server.host in {"0.0.0.0", "::"}:
        lines.append(f"远程访问   : http://<服务器IP>:{server.port}（请确认防火墙已放行端口）")
    if headless:
        lines.append("运行模式   : 无头服务模式（--no-browser）")
    lines.append("退出方式   : 按 Ctrl+C 或直接关闭此窗口即可安全退出服务")
    lines.append("=" * 60)
    return "\n".join(lines)


def _install_signal_stop(stop_event: threading.Event) -> list[tuple[int, object]]:
    """Register SIGTERM handler on main thread so service shuts down cleanly."""
    if threading.current_thread() is not threading.main_thread():
        return []
    previous: list[tuple[int, object]] = []
    sigterm = getattr(signal, "SIGTERM", None)
    if sigterm is None:
        return previous
    try:
        old = signal.getsignal(sigterm)
        signal.signal(sigterm, lambda _signum, _frame: stop_event.set())
        previous.append((sigterm, old))
    except (OSError, ValueError):
        pass
    return previous


def run_web_console(
    config_path: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
    mode: str = "app",
    stop_event: threading.Event | None = None,
    print_fn: Callable[[str], None] = print,
    server_factory: Callable[..., WebConsoleServer] = WebConsoleServer,
) -> int:
    """Start ``WebConsoleServer``, print banner, optionally open UI, and wait until exit."""
    resolved_config = Path(config_path).resolve()
    server = server_factory(resolved_config, host=host, port=port)
    wait_event = stop_event if stop_event is not None else threading.Event()
    saved_signals: list[tuple[int, object]] = []

    try:
        server.start()
        print_fn(_format_startup_banner(server, resolved_config, headless=not open_browser))
        if open_browser:
            open_console_window(server.url, mode=mode)
        saved_signals = _install_signal_stop(wait_event)
        while not wait_event.is_set():
            wait_event.wait(timeout=0.5)
    except KeyboardInterrupt:
        print_fn("\n正在停止 Web 控制台与本地代理服务...")
    finally:
        for signum, prev_handler in saved_signals:
            try:
                signal.signal(signum, prev_handler)  # type: ignore[arg-type]
            except (OSError, ValueError):
                pass
        server.stop()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint supporting Web Console, legacy Tkinter UI, and MCP stdio."""
    _configure_stdio_utf8()
    args = parse_args(argv)

    if args.tk:
        import app as legacy_app

        legacy_app.main()
        return 0

    config_path = resolve_runtime_config_path(args.config)

    if args.mcp_stdio:
        import mcp_server

        return int(mcp_server.main(["--config", str(config_path)]))

    return run_web_console(
        config_path,
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
        mode=args.mode,
    )


if __name__ == "__main__":
    raise SystemExit(main())
