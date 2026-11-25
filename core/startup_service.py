from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from core.runtime_paths import app_dir
from core.state_store import atomic_write_text


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
DEFAULT_VALUE_NAME = "AnQiCMSPublisher"
DEFAULT_PREFERENCE_PATH = Path("设置") / "startup_preference.json"


class StartupPreferenceError(OSError):
    pass


class StartupService:
    def __init__(
        self,
        *,
        registry=None,
        platform: str | None = None,
        executable: str | Path | None = None,
        app_script: str | Path | None = None,
        preference_path: str | Path | None = None,
        frozen: bool | None = None,
        value_name: str = DEFAULT_VALUE_NAME,
    ) -> None:
        self.platform = platform if platform is not None else sys.platform
        self.executable = Path(executable or sys.executable).resolve()
        self.app_script = Path(app_script or app_dir() / "app.py").resolve()
        self.preference_path = Path(
            preference_path or app_dir() / DEFAULT_PREFERENCE_PATH
        ).resolve()
        self.frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
        self.value_name = value_name
        self.registry = registry if registry is not None else self._load_registry()

    @property
    def supported(self) -> bool:
        return self.platform.startswith("win") and self.registry is not None

    def build_command(self) -> str:
        if self.frozen:
            arguments = [str(self.executable)]
        else:
            arguments = [str(self._pythonw_executable()), str(self.app_script)]
        return subprocess.list2cmdline(arguments)

    def registered_command(self) -> str | None:
        if not self.supported:
            return None
        try:
            with self.registry.OpenKey(
                self.registry.HKEY_CURRENT_USER,
                RUN_KEY,
                0,
                self.registry.KEY_QUERY_VALUE,
            ) as key:
                value, _value_type = self.registry.QueryValueEx(key, self.value_name)
        except FileNotFoundError:
            return None
        return value if isinstance(value, str) else None

    def is_enabled(self) -> bool:
        return self.registered_command() == self.build_command()

    def preference(self) -> bool | None:
        """Return the explicit preference, or None before the first decision."""
        try:
            content = self.preference_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StartupPreferenceError(
                f"无法读取登录自启动偏好: {self.preference_path.name}"
            ) from exc
        try:
            payload = json.loads(content)
        except (TypeError, ValueError) as exc:
            raise StartupPreferenceError(
                f"登录自启动偏好已损坏: {self.preference_path.name}"
            ) from exc
        enabled = payload.get("enabled") if isinstance(payload, dict) else None
        if not isinstance(enabled, bool):
            raise StartupPreferenceError(
                f"登录自启动偏好格式错误: {self.preference_path.name}"
            )
        return enabled

    def ensure_default_enabled(self) -> bool:
        """Apply the first-run default once without replacing a foreign command."""
        if not self.supported:
            return False
        preference = self.preference()
        if preference is not None:
            return self.is_enabled()
        registered = self.registered_command()
        expected = self.build_command()
        if registered is not None and registered != expected:
            return False
        if registered is None:
            return self.set_enabled(True)
        self._save_preference(True)
        return True

    def enable(self) -> bool:
        if not self.supported:
            return False
        with self.registry.CreateKeyEx(
            self.registry.HKEY_CURRENT_USER,
            RUN_KEY,
            0,
            self.registry.KEY_SET_VALUE,
        ) as key:
            self.registry.SetValueEx(
                key,
                self.value_name,
                0,
                self.registry.REG_SZ,
                self.build_command(),
            )
        return True

    def disable(self) -> bool:
        if not self.supported:
            return False
        try:
            with self.registry.OpenKey(
                self.registry.HKEY_CURRENT_USER,
                RUN_KEY,
                0,
                self.registry.KEY_SET_VALUE,
            ) as key:
                self.registry.DeleteValue(key, self.value_name)
        except FileNotFoundError:
            return False
        return True

    def set_enabled(self, enabled: bool) -> bool:
        if not self.supported:
            return False
        previous_command = self.registered_command()
        changed = self.enable() if enabled else self.disable()
        try:
            self._save_preference(enabled)
        except StartupPreferenceError:
            self._restore_registered_command(previous_command)
            raise
        return changed

    def _restore_registered_command(self, command: str | None) -> None:
        if command is None:
            self.disable()
            return
        with self.registry.CreateKeyEx(
            self.registry.HKEY_CURRENT_USER,
            RUN_KEY,
            0,
            self.registry.KEY_SET_VALUE,
        ) as key:
            self.registry.SetValueEx(
                key,
                self.value_name,
                0,
                self.registry.REG_SZ,
                command,
            )

    def _save_preference(self, enabled: bool) -> None:
        payload = json.dumps(
            {"enabled": enabled},
            ensure_ascii=True,
            separators=(",", ":"),
        )
        try:
            atomic_write_text(self.preference_path, payload + "\n")
        except OSError as exc:
            raise StartupPreferenceError(
                f"无法保存登录自启动偏好: {self.preference_path.name}"
            ) from exc

    def _pythonw_executable(self) -> Path:
        if self.executable.name.casefold() in {"python.exe", "pythonw.exe"}:
            return self.executable.with_name("pythonw.exe")
        return self.executable

    def _load_registry(self):
        if not self.platform.startswith("win"):
            return None
        try:
            import winreg
        except ImportError:
            return None
        return winreg
