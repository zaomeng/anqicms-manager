import json
import subprocess

import pytest

from core.startup_service import (
    DEFAULT_VALUE_NAME,
    RUN_KEY,
    StartupPreferenceError,
    StartupService,
)


class FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeRegistry:
    HKEY_CURRENT_USER = "HKCU"
    KEY_QUERY_VALUE = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values = {}
        self.calls = []

    def CreateKeyEx(self, root, path, reserved, access):
        self.calls.append(("create", root, path, reserved, access))
        return FakeKey()

    def OpenKey(self, root, path, reserved, access):
        self.calls.append(("open", root, path, reserved, access))
        if not self.values:
            raise FileNotFoundError
        return FakeKey()

    def SetValueEx(self, _key, name, reserved, value_type, value):
        self.calls.append(("set", name, reserved, value_type, value))
        self.values[name] = value

    def QueryValueEx(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError
        return self.values[name], self.REG_SZ

    def DeleteValue(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError
        del self.values[name]


def test_frozen_command_is_quoted_executable_only(tmp_path):
    executable = tmp_path / "程序 目录" / "AnQiCMS Publisher.exe"
    service = StartupService(
        platform="win32",
        registry=FakeRegistry(),
        executable=executable,
        frozen=True,
    )

    assert service.build_command() == subprocess.list2cmdline([str(executable.resolve())])


def test_source_command_uses_pythonw_and_app_script(tmp_path):
    python = tmp_path / "Python 312" / "python.exe"
    script = tmp_path / "项目 目录" / "app.py"
    service = StartupService(
        platform="win32",
        registry=FakeRegistry(),
        executable=python,
        app_script=script,
        frozen=False,
    )

    assert service.build_command() == subprocess.list2cmdline(
        [str(python.resolve().with_name("pythonw.exe")), str(script.resolve())]
    )


def test_enable_registers_current_user_command_and_is_idempotent(tmp_path):
    registry = FakeRegistry()
    executable = tmp_path / "app.exe"
    service = StartupService(
        platform="win32",
        registry=registry,
        executable=executable,
        preference_path=tmp_path / "preference.json",
        frozen=True,
    )

    assert service.enable() is True
    assert service.enable() is True
    assert registry.values[DEFAULT_VALUE_NAME] == service.build_command()
    assert service.registered_command() == service.build_command()
    assert service.is_enabled() is True
    assert ("create", "HKCU", RUN_KEY, 0, registry.KEY_SET_VALUE) in registry.calls


def test_old_command_is_not_considered_enabled(tmp_path):
    registry = FakeRegistry()
    registry.values[DEFAULT_VALUE_NAME] = '"C:\\Old\\app.exe"'
    service = StartupService(
        platform="win32",
        registry=registry,
        executable=tmp_path / "new.exe",
        frozen=True,
    )

    assert service.registered_command() == '"C:\\Old\\app.exe"'
    assert service.is_enabled() is False


def test_disable_removes_value_and_missing_value_is_safe(tmp_path):
    registry = FakeRegistry()
    service = StartupService(
        platform="win32",
        registry=registry,
        executable=tmp_path / "app.exe",
        preference_path=tmp_path / "preference.json",
        frozen=True,
    )

    assert service.disable() is False
    service.enable()
    assert service.disable() is True
    assert service.disable() is False
    assert service.is_enabled() is False


def test_non_windows_environment_is_safe(tmp_path):
    service = StartupService(
        platform="linux",
        executable=tmp_path / "python",
        app_script=tmp_path / "app.py",
        frozen=False,
    )

    assert service.supported is False
    assert service.registered_command() is None
    assert service.is_enabled() is False
    assert service.enable() is False
    assert service.disable() is False
    assert service.set_enabled(True) is False


def test_custom_value_name_is_used(tmp_path):
    registry = FakeRegistry()
    service = StartupService(
        platform="win32",
        registry=registry,
        executable=tmp_path / "app.exe",
        preference_path=tmp_path / "preference.json",
        frozen=True,
        value_name="CustomPublisher",
    )

    service.set_enabled(True)

    assert registry.values == {"CustomPublisher": service.build_command()}
    assert service.preference() is True


def test_missing_preference_enables_once_and_persists_true(tmp_path):
    registry = FakeRegistry()
    preference_path = tmp_path / "设置" / "startup_preference.json"
    service = StartupService(
        platform="win32",
        registry=registry,
        executable=tmp_path / "app.exe",
        preference_path=preference_path,
        frozen=True,
    )

    assert service.preference() is None
    assert service.ensure_default_enabled() is True
    assert service.preference() is True
    assert json.loads(preference_path.read_text(encoding="utf-8")) == {"enabled": True}
    assert service.ensure_default_enabled() is True
    assert [call[0] for call in registry.calls].count("set") == 1
    assert not list(preference_path.parent.glob("*.tmp"))


def test_explicit_disable_persists_false_and_missing_run_value_stays_disabled(tmp_path):
    registry = FakeRegistry()
    preference_path = tmp_path / "preference.json"
    service = StartupService(
        platform="win32",
        registry=registry,
        executable=tmp_path / "app.exe",
        preference_path=preference_path,
        frozen=True,
    )
    service.ensure_default_enabled()

    assert service.set_enabled(False) is True
    assert service.preference() is False
    assert registry.values == {}
    assert service.ensure_default_enabled() is False
    assert registry.values == {}


def test_existing_true_preference_does_not_recreate_missing_run_value(tmp_path):
    registry = FakeRegistry()
    preference_path = tmp_path / "preference.json"
    service = StartupService(
        platform="win32",
        registry=registry,
        executable=tmp_path / "app.exe",
        preference_path=preference_path,
        frozen=True,
    )
    service.ensure_default_enabled()
    registry.values.clear()
    registry.calls.clear()

    assert service.ensure_default_enabled() is False
    assert service.preference() is True
    assert registry.values == {}
    assert not any(call[0] == "set" for call in registry.calls)


def test_disable_without_run_value_still_records_explicit_false(tmp_path):
    preference_path = tmp_path / "preference.json"
    service = StartupService(
        platform="win32",
        registry=FakeRegistry(),
        executable=tmp_path / "app.exe",
        preference_path=preference_path,
        frozen=True,
    )

    assert service.set_enabled(False) is False
    assert service.preference() is False
    assert json.loads(preference_path.read_text(encoding="utf-8")) == {"enabled": False}


def test_existing_own_command_migrates_to_true_preference_without_rewriting_registry(
    tmp_path,
):
    registry = FakeRegistry()
    preference_path = tmp_path / "preference.json"
    service = StartupService(
        platform="win32",
        registry=registry,
        executable=tmp_path / "app.exe",
        preference_path=preference_path,
        frozen=True,
    )
    registry.values[DEFAULT_VALUE_NAME] = service.build_command()

    assert service.ensure_default_enabled() is True
    assert service.preference() is True
    assert not any(call[0] == "set" for call in registry.calls)


def test_foreign_command_is_not_overwritten_or_claimed_as_preference(tmp_path):
    registry = FakeRegistry()
    registry.values[DEFAULT_VALUE_NAME] = '"C:\\Other\\publisher.exe"'
    preference_path = tmp_path / "preference.json"
    service = StartupService(
        platform="win32",
        registry=registry,
        executable=tmp_path / "app.exe",
        preference_path=preference_path,
        frozen=True,
    )

    assert service.ensure_default_enabled() is False
    assert registry.values[DEFAULT_VALUE_NAME] == '"C:\\Other\\publisher.exe"'
    assert service.preference() is None
    assert not preference_path.exists()


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", '{"enabled":1}', '{"enabled":null}'],
)
def test_damaged_preference_does_not_trigger_default_enable(tmp_path, content):
    registry = FakeRegistry()
    preference_path = tmp_path / "preference.json"
    preference_path.write_text(content, encoding="utf-8")
    service = StartupService(
        platform="win32",
        registry=registry,
        executable=tmp_path / "app.exe",
        preference_path=preference_path,
        frozen=True,
    )

    with pytest.raises(StartupPreferenceError):
        service.ensure_default_enabled()

    assert registry.values == {}


def test_enable_rolls_back_new_registry_value_when_preference_write_fails(
    tmp_path, monkeypatch
):
    registry = FakeRegistry()
    service = StartupService(
        platform="win32",
        registry=registry,
        executable=tmp_path / "app.exe",
        preference_path=tmp_path / "preference.json",
        frozen=True,
    )
    monkeypatch.setattr(
        "core.startup_service.atomic_write_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(StartupPreferenceError):
        service.set_enabled(True)

    assert registry.values == {}
    assert not service.preference_path.exists()


def test_enable_restores_foreign_registry_value_when_preference_write_fails(
    tmp_path, monkeypatch
):
    registry = FakeRegistry()
    foreign_command = '"C:\\Other\\publisher.exe"'
    registry.values[DEFAULT_VALUE_NAME] = foreign_command
    service = StartupService(
        platform="win32",
        registry=registry,
        executable=tmp_path / "app.exe",
        preference_path=tmp_path / "preference.json",
        frozen=True,
    )
    monkeypatch.setattr(
        "core.startup_service.atomic_write_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(StartupPreferenceError):
        service.set_enabled(True)

    assert registry.values[DEFAULT_VALUE_NAME] == foreign_command


def test_disable_restores_original_registry_value_when_preference_write_fails(
    tmp_path, monkeypatch
):
    registry = FakeRegistry()
    service = StartupService(
        platform="win32",
        registry=registry,
        executable=tmp_path / "app.exe",
        preference_path=tmp_path / "preference.json",
        frozen=True,
    )
    registry.values[DEFAULT_VALUE_NAME] = service.build_command()
    monkeypatch.setattr(
        "core.startup_service.atomic_write_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(StartupPreferenceError):
        service.set_enabled(False)

    assert registry.values[DEFAULT_VALUE_NAME] == service.build_command()


def test_first_run_default_rolls_back_registry_when_preference_write_fails(
    tmp_path, monkeypatch
):
    registry = FakeRegistry()
    service = StartupService(
        platform="win32",
        registry=registry,
        executable=tmp_path / "app.exe",
        preference_path=tmp_path / "preference.json",
        frozen=True,
    )
    monkeypatch.setattr(
        "core.startup_service.atomic_write_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(StartupPreferenceError):
        service.ensure_default_enabled()

    assert registry.values == {}
