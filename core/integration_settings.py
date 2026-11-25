"""Optional ``设置/integration_settings.json`` for the site-group services.

Loading never raises: a missing/corrupt file yields defaults, unknown keys are
ignored and every field with a wrong type or out-of-range value falls back to
its own default.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path

from core.state_store import atomic_write_text

SETTINGS_FILENAME = "integration_settings.json"
SETTINGS_DIRNAME = "设置"

# Numeric fields that must be strictly positive (everything else may be 0).
_POSITIVE_FIELDS = frozenset(
    {
        "connect_timeout",
        "read_timeout",
        "max_concurrency",
        "request_timeout",
        "check_timeout",
        "batch_confirm_threshold",
        "batch_max",
        "bridge_ttl_s",
        "upgrade_call_timeout_s",
        "health_timeout_s",
        "health_interval_s",
    }
)


def _str_list(values: list[str]):
    return field(default_factory=lambda: list(values))


@dataclass(slots=True)
class McpSettings:
    endpoint_path: str = "/api/mcp"
    protocol_version: str = "2025-06-18"
    fallback_protocol_versions: list[str] = _str_list(["2025-03-26", "2024-11-05"])
    connect_timeout: float = 5.0
    read_timeout: float = 20.0
    verify_ssl: bool = True


@dataclass(slots=True)
class FriendLinkSettings:
    max_concurrency: int = 6
    request_timeout: float = 15.0
    check_timeout: float = 8.0
    per_request_delay_s: float = 0.2
    default_nofollow: bool = False
    default_remark: str = "站群互链"


@dataclass(slots=True)
class AdminJumpSettings:
    enable_ticket: bool = True
    ticket_tool_names: list[str] = _str_list([])
    ticket_keywords: list[str] = _str_list(
        ["login_ticket", "ticket", "jump", "switch_site", "admin_login", "登录", "跳转"]
    )
    ticket_url_template: str = ""  # e.g. "{admin_url}?ticket={ticket}"
    enable_silent_login: bool = True
    login_api_paths: list[str] = _str_list(["api/login", "api/user/login"])
    token_handoff_url_template: str = ""
    autopost_login: bool = False
    batch_confirm_threshold: int = 5
    batch_max: int = 30
    batch_stagger_ms: int = 400
    bridge_ttl_s: float = 60.0


@dataclass(slots=True)
class UpgradeSettings:
    check_tool_names: list[str] = _str_list([])
    upgrade_tool_names: list[str] = _str_list([])
    check_keywords: list[str] = _str_list(
        ["check_update", "upgrade_check", "version", "system_info", "检查更新", "版本"]
    )
    upgrade_keywords: list[str] = _str_list(
        ["system_upgrade", "upgrade", "update_system", "升级"]
    )
    rest_version_paths: list[str] = _str_list([])
    rest_upgrade_paths: list[str] = _str_list([])
    max_concurrency: int = 3
    upgrade_call_timeout_s: float = 180.0
    restart_wait_s: float = 8.0
    health_timeout_s: float = 240.0
    health_interval_s: float = 5.0


@dataclass(slots=True)
class IntegrationSettings:
    mcp: McpSettings = field(default_factory=McpSettings)
    friendlink: FriendLinkSettings = field(default_factory=FriendLinkSettings)
    admin_jump: AdminJumpSettings = field(default_factory=AdminJumpSettings)
    upgrade: UpgradeSettings = field(default_factory=UpgradeSettings)


def settings_path(config_path) -> Path:
    return Path(config_path).parent / SETTINGS_DIRNAME / SETTINGS_FILENAME


def _coerce(name: str, value: object, default: object) -> object:
    """Return ``value`` if it fits the type of ``default`` else ``default``."""
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        if isinstance(value, float):
            if not math.isfinite(value) or value != int(value):
                return default
            value = int(value)
        minimum_ok = value > 0 if name in _POSITIVE_FIELDS else value >= 0
        return value if minimum_ok else default
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        number = float(value)
        if not math.isfinite(number):
            return default
        minimum_ok = number > 0 if name in _POSITIVE_FIELDS else number >= 0
        return number if minimum_ok else default
    if isinstance(default, str):
        if not isinstance(value, str):
            return default
        if name == "endpoint_path" and not value.startswith("/"):
            return default
        if name in {"endpoint_path", "protocol_version"} and not value.strip():
            return default
        return value
    if isinstance(default, list):
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return list(value)
        return list(default)
    return default


def _build_section(cls, raw: object):
    section = cls()
    if not isinstance(raw, dict):
        return section
    for item in fields(cls):
        if item.name not in raw:
            continue
        default = getattr(section, item.name)
        setattr(section, item.name, _coerce(item.name, raw[item.name], default))
    return section


def load_integration_settings(path) -> IntegrationSettings:
    """Load settings; never raises."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
        data = json.loads(text)
    except (OSError, ValueError, TypeError):
        return IntegrationSettings()
    if not isinstance(data, dict):
        return IntegrationSettings()
    settings = IntegrationSettings()
    for item in fields(IntegrationSettings):
        section_type = type(getattr(settings, item.name))
        if is_dataclass(section_type):
            setattr(settings, item.name, _build_section(section_type, data.get(item.name)))
    return settings


def save_integration_settings(path, settings: IntegrationSettings) -> None:
    content = json.dumps(asdict(settings), ensure_ascii=False, indent=2)
    atomic_write_text(path, content + "\n")
