from pathlib import Path

from core.instance_lock import ConfigInstanceLock


def test_same_config_allows_only_one_instance_and_can_be_reacquired_after_release(
    tmp_path
):
    config_path = tmp_path / "config.ini"
    first = ConfigInstanceLock(config_path)
    second = ConfigInstanceLock(config_path)

    assert first.acquire() is True
    try:
        assert second.acquire() is False
    finally:
        first.release()

    assert second.acquire() is True
    second.release()


def test_different_config_files_can_be_locked_at_the_same_time(tmp_path):
    first = ConfigInstanceLock(tmp_path / "one" / "config.ini")
    second = ConfigInstanceLock(tmp_path / "two" / "config.ini")

    assert first.acquire() is True
    try:
        assert second.acquire() is True
    finally:
        second.release()
        first.release()


def test_run_app_holds_config_lock_until_mainloop_returns(tmp_path, monkeypatch):
    import ui.main_window as main_window

    config_path = tmp_path / "config.ini"
    events = []

    class TrackingLock:
        def __init__(self, path):
            assert Path(path).resolve() == config_path.resolve()
            events.append("lock-created")

        def __enter__(self):
            events.append("lock-acquired")
            return self

        def __exit__(self, exc_type, exc, traceback):
            events.append("lock-released")

    class FakeRoot:
        def protocol(self, name, callback):
            assert name == "WM_DELETE_WINDOW"
            assert callable(callback)
            events.append("protocol")

        def mainloop(self):
            assert "lock-acquired" in events
            assert "lock-released" not in events
            events.append("mainloop")

    class FakeWindow:
        def __init__(self, root, config_path):
            assert isinstance(root, FakeRoot)
            assert Path(config_path).resolve() == config_path_outer.resolve()
            events.append("window")

        def on_close(self):
            pass

    config_path_outer = config_path
    monkeypatch.setattr(main_window, "ConfigInstanceLock", TrackingLock)
    monkeypatch.setattr(main_window.tk, "Tk", FakeRoot)
    monkeypatch.setattr(main_window, "MainWindow", FakeWindow)

    main_window.run_app(config_path)

    assert events == [
        "lock-created",
        "lock-acquired",
        "window",
        "protocol",
        "mainloop",
        "lock-released",
    ]
