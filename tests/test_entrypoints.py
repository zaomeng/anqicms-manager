import importlib
import sys


def test_importing_app_does_not_launch_ui():
    module = importlib.import_module("app")
    assert hasattr(module, "main")


def test_importing_legacy_entrypoint_is_safe():
    module = importlib.import_module("anqicms多线程网站发布1")
    assert hasattr(module, "main")


def test_frozen_runtime_config_is_copied_next_to_exe(tmp_path, monkeypatch):
    app = importlib.import_module("app")
    exe_dir = tmp_path / "exe"
    bundle_dir = tmp_path / "bundle"
    exe_dir.mkdir()
    bundle_dir.mkdir()
    (bundle_dir / "config.ini").write_text("[demo]\n", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(app, "app_dir", lambda: exe_dir)
    monkeypatch.setattr(app, "bundled_dir", lambda: bundle_dir)

    result = app.resolve_runtime_config_path()

    assert result == exe_dir / "config.ini"
    assert result.read_text(encoding="utf-8") == "[demo]\n"


def test_main_starts_run_app_with_the_resolved_runtime_config(tmp_path, monkeypatch):
    app = importlib.import_module("app")
    config_path = tmp_path / "profile" / "config.ini"
    observed = []
    monkeypatch.setattr(app, "resolve_runtime_config_path", lambda: config_path)
    monkeypatch.setattr(app, "run_app", observed.append)

    app.main()

    assert observed == [config_path]
