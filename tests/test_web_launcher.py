from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

import web_app


def test_parse_args_defaults_and_custom_options() -> None:
    defaults = web_app.parse_args([])
    assert defaults.host == "127.0.0.1"
    assert defaults.port == 0
    assert defaults.config is None
    assert defaults.no_browser is False
    assert defaults.mode == "app"
    assert defaults.tk is False
    assert defaults.mcp_stdio is False

    custom = web_app.parse_args(
        [
            "--host",
            "0.0.0.0",
            "--port",
            "18900",
            "--config",
            "custom.ini",
            "--no-browser",
            "--mode",
            "browser",
        ]
    )
    assert custom.host == "0.0.0.0"
    assert custom.port == 18900
    assert custom.config == "custom.ini"
    assert custom.no_browser is True
    assert custom.mode == "browser"

    tk_args = web_app.parse_args(["--tk"])
    assert tk_args.tk is True

    mcp_args = web_app.parse_args(["--mcp-stdio"])
    assert mcp_args.mcp_stdio is True

    with pytest.raises(SystemExit):
        web_app.parse_args(["--port", "70000"])


def test_resolve_runtime_config_path_prefers_explicit_and_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit = tmp_path / "explicit.ini"
    assert web_app.resolve_runtime_config_path(str(explicit)) == explicit.resolve()

    local_cfg = tmp_path / "config.ini"
    local_cfg.write_text("", encoding="utf-8")
    monkeypatch.setattr(web_app, "app_dir", lambda: tmp_path)
    monkeypatch.delattr(web_app.sys, "frozen", raising=False)
    assert web_app.resolve_runtime_config_path() == local_cfg


def test_find_app_browser_uses_which_and_candidate_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    edge_path = tmp_path / "msedge.exe"
    edge_path.write_text("stub", encoding="utf-8")

    monkeypatch.setattr(
        web_app.shutil,
        "which",
        lambda name: str(edge_path) if name == "msedge" else None,
    )
    assert web_app.find_app_browser() == str(edge_path)

    monkeypatch.setattr(web_app.shutil, "which", lambda _name: None)
    chrome_path = tmp_path / "chrome.exe"
    chrome_path.write_text("stub", encoding="utf-8")
    monkeypatch.setattr(
        web_app,
        "_candidate_browser_paths",
        lambda: [tmp_path / "missing.exe", chrome_path],
    )
    assert web_app.find_app_browser() == str(chrome_path)

    monkeypatch.setattr(web_app, "_candidate_browser_paths", lambda: [tmp_path / "missing.exe"])
    assert web_app.find_app_browser() is None


def test_open_console_window_app_mode_and_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    popen_calls: list[list[str]] = []
    tab_calls: list[str] = []

    monkeypatch.setattr(web_app, "find_app_browser", lambda: r"C:\Edge\msedge.exe")
    monkeypatch.setattr(
        web_app.subprocess,
        "Popen",
        lambda cmd, **_kwargs: popen_calls.append(cmd) or SimpleNamespace(pid=123),
    )
    monkeypatch.setattr(
        web_app.webbrowser,
        "open_new_tab",
        lambda url: tab_calls.append(url) or True,
    )

    assert web_app.open_console_window("http://127.0.0.1:18900", mode="app") is True
    assert popen_calls == [[r"C:\Edge\msedge.exe", "--app=http://127.0.0.1:18900"]]
    assert tab_calls == []

    # browser mode skips --app browser and opens a standard tab
    assert web_app.open_console_window("http://127.0.0.1:18900", mode="browser") is True
    assert tab_calls == ["http://127.0.0.1:18900"]

    # app mode falls back to webbrowser when Popen fails
    tab_calls.clear()

    def _raise_popen(*_args, **_kwargs):
        raise OSError("cannot launch")

    monkeypatch.setattr(web_app.subprocess, "Popen", _raise_popen)
    assert web_app.open_console_window("http://127.0.0.1:18901", mode="app") is True
    assert tab_calls == ["http://127.0.0.1:18901"]

    # app mode falls back to webbrowser when no Edge/Chrome executable is found
    tab_calls.clear()
    monkeypatch.setattr(web_app, "find_app_browser", lambda: None)
    assert web_app.open_console_window("http://127.0.0.1:18902", mode="app") is True
    assert tab_calls == ["http://127.0.0.1:18902"]


def test_run_web_console_headless_starts_serves_and_stops_cleanly(tmp_path: Path) -> None:
    config_file = tmp_path / "config.ini"
    config_file.write_text("", encoding="utf-8")

    stop_event = threading.Event()
    printed: list[str] = []
    created_servers: list[web_app.WebConsoleServer] = []
    health_holder: dict[str, object] = {}

    def _server_factory(cfg: Path, *, host: str, port: int) -> web_app.WebConsoleServer:
        server = web_app.WebConsoleServer(cfg, host=host, port=port)
        created_servers.append(server)
        return server

    def _probe_and_stop() -> None:
        while not created_servers or not created_servers[0].is_running:
            if stop_event.wait(0.02):
                return
        server = created_servers[0]
        try:
            with urllib.request.urlopen(f"{server.url}/api/health", timeout=3.0) as resp:
                health_holder["status"] = resp.status
                health_holder["body"] = json.loads(resp.read().decode("utf-8"))
        finally:
            stop_event.set()

    worker = threading.Thread(target=_probe_and_stop, daemon=True)
    worker.start()

    exit_code = web_app.run_web_console(
        config_file,
        host="127.0.0.1",
        port=0,
        open_browser=False,
        stop_event=stop_event,
        print_fn=printed.append,
        server_factory=_server_factory,
    )
    worker.join(timeout=5.0)

    assert exit_code == 0
    assert len(created_servers) == 1
    assert not created_servers[0].is_running
    assert health_holder["status"] == 200
    assert health_holder["body"]["ok"] is True
    assert printed
    assert "/mcp" in printed[0]
    assert str(config_file.resolve()) in printed[0]


def test_main_dispatches_tk_and_mcp_stdio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tk_called: list[bool] = []
    monkeypatch.setitem(
        web_app.sys.modules,
        "app",
        SimpleNamespace(main=lambda: tk_called.append(True)),
    )
    assert web_app.main(["--tk"]) == 0
    assert tk_called == [True]

    mcp_calls: list[list[str]] = []
    monkeypatch.setitem(
        web_app.sys.modules,
        "mcp_server",
        SimpleNamespace(main=lambda argv: mcp_calls.append(list(argv)) or 0),
    )
    cfg = tmp_path / "cfg.ini"
    assert web_app.main(["--mcp-stdio", "--config", str(cfg)]) == 0
    assert mcp_calls == [["--config", str(cfg.resolve())]]
