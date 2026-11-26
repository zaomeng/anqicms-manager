"""Local Web Console HTTP server and REST/MCP API backend (pure stdlib HTTP + JSON)."""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import json
import logging
import math
import mimetypes
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from core.admin_jump_service import AdminJumpService
from core.admin_proxy import AdminProxyServer
from core.anqicms_mcp_client import AnqiMcpClient, probe_sites_batch
from core.app_info import APP_NAME, APP_VERSION
from core.archive_service import ArchiveService
from core.category_keywords import CategoryKeywordsError, normalize_category_keywords
from core.category_service import CategoryCache, CategoryService, CategoryServiceError
from core.config_service import (
    SITE_GROUP_MAX_LENGTH,
    ConfigLoadError,
    load_config,
    save_config,
)
from core.content_service import parse_image_groups_content
from core.friendlink_ledger import FriendLinkLedger, ledger_path
from core.friendlink_service import (
    FriendLinkError,
    FriendLinkService,
    export_sites_html_links,
    parse_batch_link_input,
)
from core.gallery_service import (
    Gallery,
    GalleryError,
    GalleryService,
    validate_gallery_content,
)
from core.integration_settings import (
    AdminJumpSettings,
    FriendLinkSettings,
    IntegrationSettings,
    McpSettings,
    UpgradeSettings,
    load_integration_settings,
    save_integration_settings,
    settings_path,
)
from core.mcp_contracts import McpClientFactory
from core.publisher import Publisher
from core.redaction import SecretRedactor, redact_text
from core.runtime_paths import app_dir, bundled_dir, resolve_external_or_bundled
import core.schedule_service as _schedule_service
from core.schedule_service import is_valid_publish_time, normalize_publish_time
from core.site_credentials import (
    admin_url,
    display_name,
    effective_mcp_token,
    import_token,
    mask_secret,
    site_origin_key,
)
from core.site_factory import (
    DEFAULT_ADMIN_PATH,
    build_import_endpoint,
    derive_site_paths,
    is_valid_site_url,
    normalize_admin_path,
    normalize_site_url,
    parse_publish_endpoint,
    redact_sensitive_url,
    validate_site_key,
)
import core.site_onboarding as _site_onboarding
from core.site_onboarding import (
    SiteDiscoveryResult,
    auto_discover_sites,
    parse_bulk_new_sites,
)

try:
    from core.site_onboarding import parse_smart_site_import
except ImportError:
    parse_smart_site_import = None  # type: ignore[assignment]
from core.site_service import SiteService
from core.sitegroup_db import SiteGroupDB, db_path
from core.startup_service import StartupPreferenceError, StartupService
from core.state_store import StateFileError, atomic_write_text, get_published_count
from core.task_manager import TaskManager
from core.upgrade_service import UpgradeService
from models.entities import PublishResult, SiteConfig, SiteStatusSummary
from models.sitegroup import (
    UPGRADE_ALREADY_LATEST,
    UPGRADE_UPGRADED,
    CrossLinkItem,
    CrossLinkPlan,
    FriendLink,
    FriendLinkSpec,
    LedgerPlanSummary,
    LinkOpResult,
    McpProbeResult,
    SiteVersionInfo,
    UpgradeProgress,
    UpgradeResult,
)
from web.job_runner import JobRecord, JobRunner

_LOGGER = logging.getLogger("anqicms.web")
_MAX_REQUEST_BYTES = 10 * 1024 * 1024
_CROSSLINK_PREVIEW_TTL_S = 900.0
_PUBLISH_LOG_MAX_LINES = 500

_STATIC_MIME_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}

_SITEGROUP_TEXT_FIELDS: tuple[tuple[str, str], ...] = (
    ("site_title", "站点名称"),
    ("site_group", "站点分组"),
    ("mcp_token", "MCP Token"),
    ("admin_username", "后台账号"),
    ("admin_password", "后台密码"),
    ("admin_path", "后台路径"),
)
_BULK_TEXT_KEYS: tuple[str, ...] = (
    "site_group",
    "mcp_token",
    "admin_username",
    "admin_password",
    "site_title",
    "admin_path",
)
_POSITIVE_SETTING_FIELDS = frozenset(
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


class ApiError(RuntimeError):
    """Structured HTTP API error converted to a JSON error envelope."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        hint: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = int(status_code)
        self.hint = hint


class ApiBadRequestError(ApiError):
    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message, status_code=400, hint=hint)


class ApiNotFoundError(ApiError):
    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message, status_code=404, hint=hint)


class ApiConflictError(ApiError):
    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message, status_code=409, hint=hint)


class ApiUnavailableError(ApiError):
    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message, status_code=503, hint=hint)


class ConfigService:
    """Thread-safe wrapper around ``core.config_service.load_config`` and ``save_config``."""

    def __init__(
        self,
        config_path: str | Path,
        *,
        lock: threading.RLock | None = None,
    ) -> None:
        self.config_path = Path(config_path).resolve()
        self.lock = lock or threading.RLock()

    def load(self) -> list[SiteConfig]:
        with self.lock:
            return load_config(self.config_path)

    def save(self, sites: list[SiteConfig]) -> None:
        with self.lock:
            save_config(self.config_path, sites)


def resolve_static_dir(static_dir: str | Path | None = None) -> Path:
    """Resolve ``web/static`` cleanly for both source checkouts and PyInstaller bundles."""
    if static_dir is not None:
        return Path(static_dir).resolve()
    if getattr(sys, "frozen", False):
        candidate = resolve_external_or_bundled("web/static")
        if candidate.exists():
            return candidate
        return bundled_dir() / "web" / "static"
    local = Path(__file__).resolve().parent / "static"
    if local.exists():
        return local
    return resolve_external_or_bundled("web/static")


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _opt_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _safe_json_list(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return list(raw)
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        decoded = json.loads(raw)
    except ValueError:
        return []
    return decoded if isinstance(decoded, list) else []


def _safe_json_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        decoded = json.loads(raw)
    except ValueError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _call_with_supported_kwargs(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return func(*args, **kwargs)
    params = sig.parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return func(*args, **kwargs)
    filtered = {k: v for k, v in kwargs.items() if k in params}
    return func(*args, **filtered)


def _scheme_host_port_key(url: str) -> tuple[str, str, int | None] | None:
    text = (url or "").strip()
    if "://" not in text:
        return None
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.casefold()
    host = (parsed.hostname or "").strip(".").casefold()
    if not scheme or not host:
        return None
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    if host.startswith("www."):
        host = host[4:]
    if port is None:
        port = 443 if scheme == "https" else 80 if scheme == "http" else None
    return (scheme, host, port)


def _match_site_by_identifier(
    identifier: str,
    sites: Sequence[SiteConfig],
) -> tuple[SiteConfig | None, str]:
    key = (identifier or "").strip()
    if not key:
        return None, "站点标识为空"

    exact = [site for site in sites if site.name == key]
    if len(exact) == 1:
        return exact[0], ""

    folded = key.casefold()
    ci_matches = [site for site in sites if site.name.casefold() == folded]
    if len(ci_matches) == 1:
        return ci_matches[0], ""
    if len(ci_matches) > 1:
        return None, f"站点名“{key}”存在多个匹配"

    scheme_key = _scheme_host_port_key(key)
    if scheme_key is not None:
        scheme_matches = [
            site
            for site in sites
            if _scheme_host_port_key(getattr(site, "url", "")) == scheme_key
        ]
        if len(scheme_matches) == 1:
            return scheme_matches[0], ""

    origin_key = site_origin_key(key)
    if origin_key:
        origin_matches = [
            site
            for site in sites
            if site_origin_key(getattr(site, "url", "")) == origin_key
        ]
        if len(origin_matches) == 1:
            return origin_matches[0], ""
        if len(origin_matches) > 1:
            return None, f"域名“{origin_key}”匹配到多个站点，请改用精确站点标识"

    return None, "未找到匹配站点"


def _split_bulk_line(line: str) -> list[str]:
    if "\t" in line:
        parts = line.split("\t")
    elif "|" in line:
        parts = line.split("|")
    elif "," in line:
        parts = line.split(",")
    else:
        parts = [line]
    return [part.strip() for part in parts]


def _validate_sitegroup_updates(fields_map: Mapping[str, Any]) -> dict[str, str]:
    label_map = dict(_SITEGROUP_TEXT_FIELDS)
    cleaned: dict[str, str] = {}
    for key, label in _SITEGROUP_TEXT_FIELDS:
        if key not in fields_map:
            continue
        raw_val = fields_map.get(key)
        text = str(raw_val if raw_val is not None else "").strip()
        if not text:
            continue
        if _has_control_chars(text):
            raise ApiBadRequestError(f"{label}不能包含换行或控制字符")
        if key == "site_group" and len(text) > SITE_GROUP_MAX_LENGTH:
            raise ApiBadRequestError(f"{label}不能超过 {SITE_GROUP_MAX_LENGTH} 个字符")
        if key == "admin_path":
            try:
                text = normalize_admin_path(text)
            except ValueError as exc:
                raise ApiBadRequestError(str(exc)) from exc
        cleaned[key] = text
    return cleaned


def _format_masked_updates(updates: Mapping[str, Any]) -> str:
    label_map = dict(_SITEGROUP_TEXT_FIELDS)
    label_map.update(
        {
            "fid": "默认栏目 ID",
            "daily_publish_limit": "每日发布上限",
            "interval_time": "发布间隔",
            "enabled": "启用状态",
        }
    )
    tokens: list[str] = []
    for key, value in updates.items():
        label = label_map.get(key, key)
        if key in {"mcp_token", "admin_password"}:
            tokens.append(f"{label}={mask_secret(str(value))}")
        else:
            tokens.append(f"{label}={value}")
    return "，".join(tokens)


def _coerce_setting_value(name: str, value: Any, default: Any) -> Any:
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ApiBadRequestError(f"设置项 {name} 必须是布尔值")
        return value
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ApiBadRequestError(f"设置项 {name} 必须是整数")
        if isinstance(value, float):
            if not math.isfinite(value) or value != int(value):
                raise ApiBadRequestError(f"设置项 {name} 必须是整数")
            value = int(value)
        if name in _POSITIVE_SETTING_FIELDS and value <= 0:
            raise ApiBadRequestError(f"设置项 {name} 必须大于 0")
        if value < 0:
            raise ApiBadRequestError(f"设置项 {name} 不能为负数")
        return value
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ApiBadRequestError(f"设置项 {name} 必须是数字")
        number = float(value)
        if not math.isfinite(number):
            raise ApiBadRequestError(f"设置项 {name} 必须是有限数字")
        if name in _POSITIVE_SETTING_FIELDS and number <= 0:
            raise ApiBadRequestError(f"设置项 {name} 必须大于 0")
        if number < 0:
            raise ApiBadRequestError(f"设置项 {name} 不能为负数")
        return number
    if isinstance(default, str):
        if not isinstance(value, str):
            raise ApiBadRequestError(f"设置项 {name} 必须是字符串")
        if name == "endpoint_path" and (not value.strip() or not value.startswith("/")):
            raise ApiBadRequestError("mcp.endpoint_path 必须以 / 开头且不能为空")
        if name == "protocol_version" and not value.strip():
            raise ApiBadRequestError("mcp.protocol_version 不能为空")
        return value
    if isinstance(default, list):
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ApiBadRequestError(f"设置项 {name} 必须是字符串数组")
        return list(value)
    return default


def _merge_integration_settings(
    current: IntegrationSettings,
    payload: Mapping[str, Any],
) -> IntegrationSettings:
    if not isinstance(payload, Mapping):
        raise ApiBadRequestError("设置请求体必须是 JSON 对象")

    section_classes = {
        "mcp": McpSettings,
        "friendlink": FriendLinkSettings,
        "admin_jump": AdminJumpSettings,
        "upgrade": UpgradeSettings,
    }
    built_sections: dict[str, Any] = {}
    for section_name, section_cls in section_classes.items():
        cur_section = getattr(current, section_name)
        raw_section = payload.get(section_name)
        if raw_section is None:
            built_sections[section_name] = replace(cur_section)
            continue
        if not isinstance(raw_section, Mapping):
            raise ApiBadRequestError(f"设置节 {section_name} 必须是对象")
        defaults = section_cls()
        kwargs: dict[str, Any] = {}
        for item in dataclasses.fields(section_cls):
            cur_val = getattr(cur_section, item.name)
            if item.name in raw_section:
                def_val = getattr(defaults, item.name)
                kwargs[item.name] = _coerce_setting_value(
                    f"{section_name}.{item.name}",
                    raw_section[item.name],
                    def_val,
                )
            else:
                kwargs[item.name] = list(cur_val) if isinstance(cur_val, list) else cur_val
        built_sections[section_name] = section_cls(**kwargs)

    return IntegrationSettings(**built_sections)


def _serialize_site(
    site: SiteConfig,
    *,
    cached_status: Mapping[str, Any] | None = None,
    summary: SiteStatusSummary | None = None,
    reveal_secrets: bool = False,
) -> dict[str, Any]:
    effective_token = effective_mcp_token(site)
    has_url = bool((site.url or "").strip())
    computed_admin_url = ""
    if has_url:
        try:
            computed_admin_url = admin_url(site)
        except ValueError:
            computed_admin_url = ""

    data: dict[str, Any] = {
        "name": site.name,
        "site_id": site.site_id,
        "site_title": site.site_title,
        "display_name": display_name(site),
        "site_group": site.site_group,
        "url": site.url,
        "jiekou_masked": redact_sensitive_url(site.jiekou),
        "has_import_token": bool(import_token(site)),
        "fid": site.fid,
        "category_name": site.category_name,
        "category_keywords": {str(k): list(v) for k, v in (site.category_keywords or {}).items()},
        "publish_dir": str(site.publish_dir),
        "daily_publish_filename": str(site.daily_publish_filename),
        "publish_time": site.publish_time,
        "publish_times": (
            list(site.publish_times)
            if site.publish_times
            else ([site.publish_time] if site.publish_time else [])
        ),
        "interval_time": site.interval_time,
        "daily_publish_limit": site.daily_publish_limit,
        "enabled": bool(site.enabled),
        "image_file": str(site.image_file) if site.image_file is not None else "",
        "gallery_id": site.gallery_id,
        "article_warning_threshold": site.article_warning_threshold,
        "activation_date": site.activation_date,
        "admin_path": site.admin_path or DEFAULT_ADMIN_PATH,
        "admin_url": computed_admin_url,
        "admin_username": site.admin_username,
        "has_mcp_token": bool(effective_token),
        "has_explicit_mcp_token": bool((site.mcp_token or "").strip()),
        "has_admin_password": bool((site.admin_password or "").strip()),
        "mcp_token_masked": mask_secret(effective_token),
        "admin_password_masked": mask_secret(site.admin_password),
        "version": "",
        "current_version": "",
        "latest_version": "",
        "update_available": None,
        "mcp_ok": None,
        "mcp_tools": [],
        "link_count": None,
        "broken_link_count": None,
        "last_checked_at": "",
    }

    if cached_status is not None:
        current_ver = str(cached_status.get("current_version") or "")
        data.update(
            {
                "version": current_ver,
                "current_version": current_ver,
                "latest_version": str(cached_status.get("latest_version") or ""),
                "update_available": _opt_bool(cached_status.get("update_available")),
                "mcp_ok": _opt_bool(cached_status.get("mcp_ok")),
                "mcp_tools": _safe_json_list(cached_status.get("mcp_tools_json")),
                "link_count": cached_status.get("link_count"),
                "broken_link_count": cached_status.get("broken_link_count"),
                "last_checked_at": str(cached_status.get("last_checked_at") or ""),
            }
        )

    if summary is not None:
        data.update(
            {
                "published_today": summary.published_today,
                "pending_count": summary.pending_count,
                "article_count": summary.article_count,
                "remaining_quota": summary.remaining_quota,
                "publish_status": summary.status,
                "publish_error": summary.last_error,
            }
        )
    if reveal_secrets:
        data.update(
            {
                "jiekou": site.jiekou or "",
                "mcp_token": site.mcp_token or "",
                "admin_password": site.admin_password or "",
            }
        )
    return data


def _serialize_gallery(gallery: Gallery) -> dict[str, Any]:
    """Serialize a managed ``Gallery`` including parsed group and image counts."""
    try:
        content = gallery.file_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        content = ""
    groups = parse_image_groups_content(content)
    image_count = sum(len(urls) for urls in groups.values())
    return {
        "id": gallery.gallery_id,
        "name": gallery.name,
        "path": str(gallery.file_path),
        "group_count": len(groups),
        "image_count": image_count,
        "content": content,
    }


def _parse_publish_times_input(raw_value: Any) -> list[str]:
    """Parse comma/newline string or list of ``HH:MM:SS`` into normalized unique times."""
    if isinstance(raw_value, str):
        tokens = [
            part.strip()
            for part in re.split(r"[\r\n,，]+", raw_value)
            if part.strip()
        ]
    elif isinstance(raw_value, (list, tuple)):
        tokens = []
        for item in raw_value:
            if not isinstance(item, str):
                raise ApiBadRequestError("publish_times 中的时间必须是字符串")
            stripped = item.strip()
            if stripped:
                tokens.append(stripped)
    else:
        raise ApiBadRequestError("publish_times 必须是字符串或数组")

    normalizer = getattr(_schedule_service, "normalize_publish_times", None)
    if callable(normalizer):
        try:
            normalized = list(normalizer(tokens))
        except ValueError as exc:
            raise ApiBadRequestError(f"发布时间无效: {exc}") from exc
    else:
        normalized = []
        for token in tokens:
            if not is_valid_publish_time(token):
                raise ApiBadRequestError(f"发布时间无效: {token}")
            normalized.append(normalize_publish_time(token))
    return list(dict.fromkeys(normalized))


def _resolve_site_publish_endpoint(
    site: SiteConfig,
    payload: Mapping[str, Any] | None = None,
) -> str:
    """Resolve full ``http(s)://.../archive?token=...`` endpoint for category fetching."""
    body = payload if isinstance(payload, Mapping) else {}
    raw_full = str(body.get("full_endpoint") or "").strip()
    if raw_full:
        return raw_full

    raw_url = str(body.get("url") if "url" in body else site.url or "").strip()
    if "jiekou" in body and str(body.get("jiekou") or "").strip():
        raw_jiekou = str(body["jiekou"]).strip()
    elif "api_key" in body and str(body.get("api_key") or "").strip():
        raw_jiekou = build_import_endpoint(str(body["api_key"]).strip())
    else:
        raw_jiekou = str(site.jiekou or "").strip()

    if not raw_jiekou:
        return ""
    if raw_jiekou.casefold().startswith(("http://", "https://")):
        return raw_jiekou
    if not raw_url:
        return ""
    normalized_base = normalize_site_url(raw_url)
    if raw_jiekou.startswith("/"):
        return f"{normalized_base.rstrip('/')}{raw_jiekou}"
    return f"{normalized_base.rstrip('/')}/{raw_jiekou}"


def _serialize_friendlink(link: FriendLink) -> dict[str, Any]:
    return {
        "id": link.id,
        "title": link.title,
        "link": link.link,
        "nofollow": bool(link.nofollow),
        "back_link": link.back_link,
        "my_title": link.my_title,
        "my_link": link.my_link,
        "contact": link.contact,
        "remark": link.remark,
        "status": link.status,
        "status_text": link.status_text,
    }


def _serialize_spec(spec: FriendLinkSpec) -> dict[str, Any]:
    return {
        "title": spec.title,
        "link": spec.link,
        "nofollow": bool(spec.nofollow),
        "back_link": spec.back_link,
        "my_title": spec.my_title,
        "my_link": spec.my_link,
        "contact": spec.contact,
        "remark": spec.remark,
    }


def _serialize_link_op(op: LinkOpResult) -> dict[str, Any]:
    return {
        "site_name": op.site_name,
        "site_id": op.site_id,
        "action": op.action,
        "link": op.link,
        "title": op.title,
        "status": op.status,
        "message": op.message,
    }


def _serialize_crosslink_item(item: CrossLinkItem) -> dict[str, Any]:
    return {
        "source_site_id": item.source_site_id,
        "source_name": item.source_name,
        "target_site_id": item.target_site_id,
        "target_name": item.target_name,
        "title": item.spec.title,
        "link": item.spec.link,
        "nofollow": bool(item.spec.nofollow),
        "my_title": item.spec.my_title,
        "my_link": item.spec.my_link,
        "remark": item.spec.remark,
        "state": item.state,
        "message": item.message,
    }


def _serialize_ledger_summary(summary: LedgerPlanSummary) -> dict[str, Any]:
    return {
        "plan_id": summary.plan_id,
        "mode": summary.mode,
        "created_at": summary.created_at,
        "site_names": list(summary.site_names),
        "created_count": summary.created_count,
        "rolled_back_count": summary.rolled_back_count,
    }


def _serialize_version_info(info: SiteVersionInfo) -> dict[str, Any]:
    return {
        "site_name": info.site_name,
        "site_id": info.site_id,
        "reachable": bool(info.reachable),
        "current_version": info.current_version,
        "latest_version": info.latest_version,
        "upgrade_available": info.upgrade_available,
        "source": info.source,
        "check_tool": info.check_tool,
        "upgrade_tool": info.upgrade_tool,
        "message": info.message,
        "hint": info.hint,
    }


def _serialize_upgrade_result(result: UpgradeResult) -> dict[str, Any]:
    return {
        "site_name": result.site_name,
        "site_id": result.site_id,
        "status": result.status,
        "from_version": result.from_version,
        "to_version": result.to_version,
        "message": result.message,
        "hint": result.hint,
        "duration_s": round(float(result.duration_s), 3),
    }


def _serialize_probe_result(result: McpProbeResult) -> dict[str, Any]:
    return {
        "site_name": result.site_name,
        "ok": bool(result.ok),
        "server_name": result.server_name,
        "server_version": result.server_version,
        "protocol_version": result.protocol_version,
        "tool_count": result.tool_count,
        "tools": [t.name for t in result.tools],
        "friendlink_tools": list(result.friendlink_tools),
        "upgrade_tools": list(result.upgrade_tools),
        "jump_tools": list(result.jump_tools),
        "message": result.message,
        "hint": result.hint,
    }


def _serialize_discovery_result(
    discovery: SiteDiscoveryResult,
    probe: McpProbeResult | None = None,
) -> dict[str, Any]:
    mcp_ok = probe.ok if probe is not None else discovery.mcp_ok
    mcp_tools = [t.name for t in probe.tools] if probe is not None else list(discovery.mcp_tools)
    current_version = str(
        getattr(discovery, "current_version", "")
        or (probe.server_version if probe is not None else "")
        or ""
    ).strip()
    raw_fid = getattr(discovery, "default_fid", 0)
    try:
        default_fid = int(raw_fid or 0)
    except (TypeError, ValueError):
        default_fid = 0
    default_category_name = str(getattr(discovery, "default_category_name", "") or "").strip()
    return {
        "site_name": discovery.site_name,
        "site_url": discovery.site_url,
        "reachable": bool(discovery.reachable),
        "http_status": discovery.http_status,
        "site_title": discovery.site_title,
        "discovered_title": discovery.discovered_title,
        "link_count": discovery.link_count,
        "mcp_ok": mcp_ok,
        "mcp_tools": mcp_tools,
        "current_version": current_version,
        "default_fid": default_fid,
        "default_category_name": default_category_name,
        "message": discovery.message,
        "mcp_message": probe.message if probe is not None else "",
    }


def _count_by_status(items: Iterable[LinkOpResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    return counts


@dataclass(slots=True)
class _CachedCrosslinkPreview:
    plan: CrossLinkPlan
    sites: list[SiteConfig]
    existing_by_site: dict[str, list[FriendLink]]
    expires_at: float


class WebConsoleApp:
    """Application controller holding shared site-group services for the HTTP console."""

    def __init__(
        self,
        config_path: str | Path = "config.ini",
        *,
        config_service: ConfigService | None = None,
        site_service: SiteService | None = None,
        settings: IntegrationSettings | None = None,
        db: SiteGroupDB | None = None,
        friendlink_ledger: FriendLinkLedger | None = None,
        friendlink_service: FriendLinkService | None = None,
        upgrade_service: UpgradeService | None = None,
        admin_proxy: AdminProxyServer | None = None,
        admin_jump_service: AdminJumpService | None = None,
        job_runner: JobRunner | None = None,
        task_manager: TaskManager | None = None,
        archive_service: ArchiveService | None = None,
        gallery_service: GalleryService | None = None,
        category_service: CategoryService | None = None,
        category_cache: CategoryCache | None = None,
        startup_service: StartupService | None = None,
        mcp_client_factory: McpClientFactory | None = None,
        probe_fn: Callable[..., list[McpProbeResult]] | None = None,
        discover_fn: Callable[..., list[SiteDiscoveryResult]] | None = None,
        update_check_fn: Callable[..., Any] | None = None,
        update_download_fn: Callable[..., Any] | None = None,
        static_dir: str | Path | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.config_path = Path(config_path).resolve()
        self.host = host
        self.configured_port = int(port)
        self.bound_port = 0
        self.static_dir = resolve_static_dir(static_dir)

        self.config_lock = threading.RLock()
        self.config_service = config_service or ConfigService(
            self.config_path, lock=self.config_lock
        )
        self.settings_file = settings_path(self.config_path)
        self.settings = (
            settings
            if settings is not None
            else load_integration_settings(self.settings_file)
        )

        self.archive_service = archive_service or ArchiveService(
            archive_root=self.config_path.parent / "归档",
            cleanup_state_file=self.config_path.parent / "设置" / "archive_cleanup_state.txt",
            identity_map_file=self.config_path.parent / "设置" / "archive_site_ids.json",
        )
        self.gallery_service = gallery_service or GalleryService(
            self.config_path.parent / "设置"
        )
        self.category_service = category_service or CategoryService()
        self.category_cache = category_cache or CategoryCache(
            self.config_path.parent / "设置" / "category_cache.json"
        )
        self.startup_service = startup_service or StartupService(
            preference_path=self.config_path.parent / "设置" / "startup_preference.json",
        )
        self.site_service = site_service or SiteService(archive_service=self.archive_service)

        self._owns_db = db is None
        self.db = db or SiteGroupDB(db_path(self.config_path))
        self.friendlink_ledger = friendlink_ledger or FriendLinkLedger(
            ledger_path(self.config_path)
        )

        self._custom_mcp_client_factory = mcp_client_factory
        self._owns_proxy = admin_proxy is None
        self.admin_proxy = admin_proxy or AdminProxyServer(
            verify_ssl=self.settings.mcp.verify_ssl
        )

        self.friendlink_service = friendlink_service or FriendLinkService(
            self.settings,
            mcp_client_factory=self.mcp_client_factory,
        )
        self.upgrade_service = upgrade_service or UpgradeService(
            self.settings,
            mcp_client_factory=self.mcp_client_factory,
        )
        self.admin_jump_service = admin_jump_service or AdminJumpService(
            self.settings,
            mcp_client_factory=self.mcp_client_factory,
            proxy=self.admin_proxy,
            use_local_proxy=True,
        )

        self._owns_job_runner = job_runner is None
        self.job_runner = job_runner or JobRunner(redactor=self.redact_text)
        self.task_manager = task_manager

        self._probe_fn = probe_fn
        self._discover_fn = discover_fn
        self._update_check_fn = update_check_fn
        self._update_download_fn = update_download_fn
        self._server_stop_fn: Callable[[], None] | None = None
        self._exit_fn: Callable[[int], None] = os._exit

        self._crosslink_lock = threading.Lock()
        self._crosslink_cache: dict[str, _CachedCrosslinkPreview] = {}

        self._mcp_lock = threading.Lock()
        self._mcp_server: Any = None

        self._publish_lock = threading.Lock()
        self._publish_logs: deque[str] = deque(maxlen=_PUBLISH_LOG_MAX_LINES)
        self._publish_stats: dict[str, Any] = {
            "total": 0,
            "success": 0,
            "failed": 0,
            "started_at": "",
            "finished_at": "",
        }
        self._redactor_lock = threading.Lock()
        self._cached_redactor: SecretRedactor | None = None
        self._shutdown_done = False

    def mcp_client_factory(self, site: SiteConfig):
        """Create an MCP client bound to current ``self.settings``."""
        if self._custom_mcp_client_factory is not None:
            return self._custom_mcp_client_factory(site)
        return AnqiMcpClient.for_site(site, self.settings)

    def _refresh_redactor(self, sites: Sequence[SiteConfig]) -> SecretRedactor:
        redactor = SecretRedactor.from_sites(sites)
        with self._redactor_lock:
            self._cached_redactor = redactor
        return redactor

    def load_sites(self) -> list[SiteConfig]:
        """Load current sites from ``ConfigService``."""
        sites = self.config_service.load()
        self._refresh_redactor(sites)
        return sites

    def load_sites_safe(self) -> list[SiteConfig]:
        try:
            return self.load_sites()
        except Exception:
            return []

    def redact_text(self, text: str) -> str:
        """Scrub all configured site credentials and common secret patterns from ``text``."""
        with self._redactor_lock:
            redactor = self._cached_redactor
        if redactor is None:
            redactor = self._refresh_redactor(self.load_sites_safe())
        return redactor(str(text or ""))

    def reload_settings(self, new_settings: IntegrationSettings) -> None:
        """Hot-reload ``self.settings`` and rebuild dependent services immediately."""
        self.settings = new_settings
        if hasattr(self.admin_proxy, "_verify_ssl"):
            self.admin_proxy._verify_ssl = bool(new_settings.mcp.verify_ssl)
        old_jump = self.admin_jump_service
        old_bridge = getattr(old_jump, "_bridge", None)
        if old_bridge is not None and hasattr(old_bridge, "stop"):
            try:
                old_bridge.stop()
            except Exception:
                pass
        self.friendlink_service = FriendLinkService(
            self.settings,
            mcp_client_factory=self.mcp_client_factory,
        )
        self.upgrade_service = UpgradeService(
            self.settings,
            mcp_client_factory=self.mcp_client_factory,
        )
        self.admin_jump_service = AdminJumpService(
            self.settings,
            mcp_client_factory=self.mcp_client_factory,
            proxy=self.admin_proxy,
            use_local_proxy=True,
        )
        with self._mcp_lock:
            self._mcp_server = None

    def shutdown(self) -> None:
        """Release background workers, proxy, jump service, and DB resources."""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        if self._owns_job_runner:
            try:
                self.job_runner.shutdown(wait=True, cancel_running=True)
            except Exception:
                pass
        if self.task_manager is not None:
            try:
                self.task_manager.stop()
            except Exception:
                pass
        try:
            self.admin_jump_service.shutdown()
        except Exception:
            pass
        if self._owns_proxy:
            try:
                self.admin_proxy.stop()
            except Exception:
                pass
        if self._owns_db:
            try:
                self.db.close()
            except Exception:
                pass

    def __enter__(self) -> WebConsoleApp:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown()

    # ------------------------------------------------------------------ helpers

    def _find_site(self, site_name: str, sites: Sequence[SiteConfig] | None = None) -> SiteConfig:
        target = str(site_name or "").strip()
        if not target:
            raise ApiBadRequestError("站点名称不能为空")
        pool = list(sites) if sites is not None else self.load_sites()
        for site in pool:
            if site.name == target:
                return site
        folded = target.casefold()
        ci_matches = [site for site in pool if site.name.casefold() == folded]
        if len(ci_matches) == 1:
            return ci_matches[0]
        raise ApiNotFoundError(f"站点不存在: {target}")

    def _select_sites(
        self,
        site_names: Sequence[str] | None,
        *,
        require_non_empty: bool = False,
        sites: Sequence[SiteConfig] | None = None,
    ) -> list[SiteConfig]:
        pool = list(sites) if sites is not None else self.load_sites()
        if not site_names:
            if require_non_empty:
                raise ApiBadRequestError("请至少选择一个站点")
            return pool
        if not isinstance(site_names, Sequence) or isinstance(site_names, (str, bytes)):
            raise ApiBadRequestError("site_names 必须是字符串数组")
        by_exact = {site.name: site for site in pool}
        by_folded = {site.name.casefold(): site for site in pool}
        by_id = {site.site_id: site for site in pool if site.site_id}
        selected: list[SiteConfig] = []
        seen: set[str] = set()
        missing: list[str] = []
        for raw_name in site_names:
            key = str(raw_name or "").strip()
            if not key:
                continue
            match = by_exact.get(key) or by_folded.get(key.casefold()) or by_id.get(key)
            if match is None:
                missing.append(key)
                continue
            if match.name not in seen:
                seen.add(match.name)
                selected.append(match)
        if missing:
            raise ApiNotFoundError(f"站点不存在: {', '.join(missing)}")
        if require_non_empty and not selected:
            raise ApiBadRequestError("请至少选择一个站点")
        return selected

    def _safe_summarize_site(self, site: SiteConfig) -> SiteStatusSummary:
        try:
            return self.site_service.summarize_site(site)
        except Exception as exc:  # noqa: BLE001
            published_today = 0
            try:
                published_today = get_published_count(site.daily_publish_filename)
            except (StateFileError, OSError):
                published_today = 0
            return SiteStatusSummary(
                site_name=site.name,
                pending_count=0,
                article_count=0,
                published_today=published_today,
                daily_limit=site.daily_publish_limit,
                remaining_quota=max(site.daily_publish_limit - published_today, 0),
                status="error",
                last_error=self.redact_text(str(exc)),
            )

    def _record_op_safe(
        self,
        category: str,
        action: str,
        *,
        site_name: str = "",
        status: str = "",
        summary: str = "",
        detail: Any = None,
    ) -> None:
        try:
            self.db.record_operation(
                category,
                action,
                site_name=site_name,
                status=status,
                summary=self.redact_text(summary),
                detail=detail,
            )
        except Exception:  # noqa: BLE001
            pass

    def _upsert_status_safe(self, site_name: str, **kwargs: Any) -> None:
        try:
            self.db.upsert_site_status(site_name, **kwargs)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ health & dashboard

    def get_health(self) -> dict[str, Any]:
        app_info = self._load_app_info()
        proxy_port = 0
        try:
            proxy_port = int(self.admin_proxy.port)
        except Exception:
            proxy_port = 0
        return {
            "app_name": app_info["app_name"],
            "version": app_info["app_version"],
            "github_repo": app_info["github_repo"],
            "frozen": bool(getattr(sys, "frozen", False)),
            "port": int(self.bound_port),
            "proxy_port": proxy_port,
            "site_count": len(self.load_sites_safe()),
        }

    # ------------------------------------------------------------------ app self-update

    @staticmethod
    def _load_app_info() -> dict[str, str]:
        try:
            import core.app_info as app_info_mod

            return {
                "app_name": str(getattr(app_info_mod, "APP_NAME", APP_NAME) or APP_NAME),
                "app_version": str(getattr(app_info_mod, "APP_VERSION", APP_VERSION) or APP_VERSION),
                "github_repo": str(
                    getattr(app_info_mod, "GITHUB_REPO", "zaomeng/anqicms-manager")
                    or "zaomeng/anqicms-manager"
                ),
            }
        except ImportError:
            return {
                "app_name": APP_NAME,
                "app_version": APP_VERSION,
                "github_repo": "zaomeng/anqicms-manager",
            }

    @staticmethod
    def _load_self_updater() -> Any:
        try:
            return importlib.import_module("core.self_updater")
        except ImportError as exc:
            raise ApiUnavailableError("程序自动更新模块暂不可用") from exc

    @staticmethod
    def _serialize_update_info(info: Any) -> dict[str, Any]:
        if dataclasses.is_dataclass(info):
            return asdict(info)
        if isinstance(info, Mapping):
            return dict(info)
        return dict(vars(info))

    def check_app_update(self) -> dict[str, Any]:
        app_info = self._load_app_info()
        try:
            if self._update_check_fn is not None:
                info = _call_with_supported_kwargs(
                    self._update_check_fn,
                    current_version=app_info["app_version"],
                    repo=app_info["github_repo"],
                )
            else:
                updater = self._load_self_updater()
                info = _call_with_supported_kwargs(
                    updater.check_app_update,
                    current_version=app_info["app_version"],
                    repo=app_info["github_repo"],
                )
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"检查程序更新失败: {exc}", status_code=502) from exc

        data = self._serialize_update_info(info)
        data.setdefault("current_version", app_info["app_version"])
        data.setdefault("github_repo", app_info["github_repo"])
        self._record_op_safe(
            "settings",
            "app_update_check",
            status="ok",
            summary=f"检查程序更新：当前 {data.get('current_version')}，最新 {data.get('latest_version') or '-'}",
        )
        return data

    @staticmethod
    def _invoke_download_update(
        download_fn: Callable[..., Any],
        info: Any,
        info_dict: Mapping[str, Any],
        dest_dir: Path,
        progress_cb: Callable[..., None],
    ) -> Path:
        try:
            params = inspect.signature(download_fn).parameters
        except (TypeError, ValueError):
            params = {}
        first_name = next(iter(params), "")
        if not params or first_name in {"info", "update_info", "release"} or (
            "download_url" not in params and len(params) >= 1
        ):
            result = _call_with_supported_kwargs(
                download_fn,
                info,
                dest_dir=dest_dir,
                target_dir=dest_dir,
                progress=progress_cb,
                progress_callback=progress_cb,
            )
        else:
            result = _call_with_supported_kwargs(
                download_fn,
                download_url=info_dict.get("download_url", ""),
                sha256_url=info_dict.get("sha256_url", ""),
                expected_sha256=info_dict.get("sha256", "") or info_dict.get("expected_sha256", ""),
                asset_name=info_dict.get("asset_name", ""),
                dest_dir=dest_dir,
                target_dir=dest_dir,
                progress=progress_cb,
                progress_callback=progress_cb,
            )
        return Path(result)

    def submit_app_update_apply(self, payload: Mapping[str, Any] | None = None) -> JobRecord:
        body = payload if isinstance(payload, Mapping) else {}
        app_info = self._load_app_info()

        def _job(update_progress: Callable[..., None], cancel_event: threading.Event) -> dict[str, Any]:
            update_progress(0, 3, "正在检查 GitHub 最新版本...")
            if self._update_check_fn is not None:
                info = _call_with_supported_kwargs(
                    self._update_check_fn,
                    current_version=app_info["app_version"],
                    repo=app_info["github_repo"],
                )
            else:
                updater = self._load_self_updater()
                info = _call_with_supported_kwargs(
                    updater.check_app_update,
                    current_version=app_info["app_version"],
                    repo=app_info["github_repo"],
                )
            info_dict = self._serialize_update_info(info)
            current_ver = str(info_dict.get("current_version") or app_info["app_version"])
            latest_ver = str(info_dict.get("latest_version") or "")
            release_url = str(info_dict.get("release_url") or f"https://github.com/{app_info['github_repo']}/releases")
            download_url = str(info_dict.get("download_url") or "")

            if not info_dict.get("update_available") and not bool(body.get("force", False)):
                update_progress(3, 3, "当前已是最新版本")
                return {
                    "applied": False,
                    "update_available": False,
                    "source_mode": not bool(getattr(sys, "frozen", False)),
                    "current_version": current_ver,
                    "latest_version": latest_ver or current_ver,
                    "release_url": release_url,
                    "download_url": download_url,
                    "message": str(info_dict.get("message") or f"当前已是最新版本 v{current_ver}"),
                }

            if not getattr(sys, "frozen", False):
                update_progress(3, 3, "当前为源码运行模式，跳过 EXE 自动替换")
                msg = "当前为源码运行模式，请运行 git pull 或前往 GitHub Releases 下载新版 EXE"
                self._record_op_safe(
                    "settings",
                    "app_update_apply",
                    status="ok",
                    summary=f"源码模式检查更新 {current_ver} -> {latest_ver}",
                )
                return {
                    "applied": False,
                    "source_mode": True,
                    "update_available": bool(info_dict.get("update_available")),
                    "current_version": current_ver,
                    "latest_version": latest_ver,
                    "release_url": release_url,
                    "download_url": download_url,
                    "message": msg,
                }

            if cancel_event.is_set():
                return {"applied": False, "cancelled": True, "message": "已取消更新"}

            updater = self._load_self_updater()
            download_fn = self._update_download_fn or updater.download_and_verify_update
            update_dir = self.config_path.parent / "设置" / "updates"
            update_dir.mkdir(parents=True, exist_ok=True)

            update_progress(1, 3, f"正在下载并校验更新包 {latest_ver or ''}...")

            def _dl_progress(downloaded: int, total: int = 0) -> None:
                if total > 0:
                    pct_mb = downloaded / (1024 * 1024)
                    tot_mb = total / (1024 * 1024)
                    update_progress(1, 3, f"下载中 {pct_mb:.1f} / {tot_mb:.1f} MB...")

            downloaded_path = self._invoke_download_update(
                download_fn,
                info,
                info_dict,
                update_dir,
                _dl_progress,
            )

            update_progress(2, 3, "SHA256 校验通过，正在准备自动替换脚本...")
            current_exe = Path(sys.executable).resolve()
            script_path = _call_with_supported_kwargs(
                updater.create_windows_replace_script,
                downloaded_path,
                current_exe,
                pid=os.getpid(),
                restart=True,
            )
            _call_with_supported_kwargs(updater.launch_replace_script, script_path)

            update_progress(3, 3, "替换脚本已就绪，即将自动重启程序...")
            self._record_op_safe(
                "settings",
                "app_update_apply",
                status="ok",
                summary=f"应用自动更新 {current_ver} -> {latest_ver}，即将重启",
            )

            def _delayed_exit() -> None:
                try:
                    if callable(self._server_stop_fn):
                        self._server_stop_fn()
                    else:
                        self.shutdown()
                except Exception:
                    pass
                self._exit_fn(0)

            timer = threading.Timer(1.5, _delayed_exit)
            timer.daemon = True
            timer.start()

            return {
                "applied": True,
                "restarting": True,
                "source_mode": False,
                "current_version": current_ver,
                "latest_version": latest_ver,
                "release_url": release_url,
                "download_url": download_url,
                "downloaded_path": str(downloaded_path),
                "script_path": str(script_path),
                "message": f"已完成下载与 SHA256 校验（{latest_ver}），服务将在 1.5 秒后退出并自动完成替换重启",
            }

        return self.job_runner.submit(
            "app_update_apply",
            "下载并更新程序",
            _job,
            total=3,
        )

    def get_dashboard(self) -> dict[str, Any]:
        sites = self.load_sites()
        cached_map = {
            row["site_name"]: row
            for row in self.db.list_site_statuses()
            if row.get("site_name")
        }

        enriched_sites: list[dict[str, Any]] = []
        group_counts: dict[str, int] = {}
        enabled_count = 0
        mcp_ok_count = 0
        mcp_fail_count = 0
        updates_available = 0
        total_links = 0
        broken_links = 0
        published_today_total = 0
        pending_total = 0

        for site in sites:
            summary = self._safe_summarize_site(site)
            cached = cached_map.get(site.name)
            serialized = _serialize_site(site, cached_status=cached, summary=summary)
            enriched_sites.append(serialized)

            if site.enabled:
                enabled_count += 1
            grp = (site.site_group or "").strip()
            if grp:
                group_counts[grp] = group_counts.get(grp, 0) + 1

            mcp_ok = serialized.get("mcp_ok")
            if mcp_ok is True:
                mcp_ok_count += 1
            elif mcp_ok is False:
                mcp_fail_count += 1

            if serialized.get("update_available") is True:
                updates_available += 1
            if isinstance(serialized.get("link_count"), int):
                total_links += serialized["link_count"]
            if isinstance(serialized.get("broken_link_count"), int):
                broken_links += serialized["broken_link_count"]

            published_today_total += summary.published_today
            pending_total += summary.pending_count

        active_jobs = sum(
            1
            for job in self.job_runner.list_jobs(limit=100)
            if job.status in {"pending", "running"}
        )

        kpis = {
            "total_sites": len(sites),
            "enabled_sites": enabled_count,
            "mcp_ok_sites": mcp_ok_count,
            "mcp_failed_sites": mcp_fail_count,
            "updates_available": updates_available,
            "total_links": total_links,
            "broken_links": broken_links,
            "published_today": published_today_total,
            "pending_articles": pending_total,
            "active_jobs": active_jobs,
            "publish_running": self._is_publish_running(),
        }
        return {
            "kpis": kpis,
            "sites": enriched_sites,
            "groups": sorted(group_counts),
            "group_counts": group_counts,
        }

    # ------------------------------------------------------------------ site CRUD & bulk

    def list_sites(
        self,
        *,
        group: str = "",
        query: str = "",
        enabled: bool | None = None,
        reveal_secrets: bool = False,
    ) -> dict[str, Any]:
        sites = self.load_sites()
        cached_map = {
            row["site_name"]: row
            for row in self.db.list_site_statuses()
            if row.get("site_name")
        }
        groups = sorted({(s.site_group or "").strip() for s in sites if (s.site_group or "").strip()})

        q_lower = query.strip().casefold()
        group_filter = group.strip()
        items: list[dict[str, Any]] = []
        for site in sites:
            if group_filter and (site.site_group or "").strip() != group_filter:
                continue
            if enabled is not None and bool(site.enabled) != enabled:
                continue
            if q_lower:
                haystack = " ".join(
                    (
                        site.name,
                        site.site_title,
                        site.url,
                        site.site_group,
                    )
                ).casefold()
                if q_lower not in haystack:
                    continue
            items.append(
                _serialize_site(
                    site,
                    cached_status=cached_map.get(site.name),
                    reveal_secrets=reveal_secrets,
                )
            )
        return {"sites": items, "groups": groups, "total": len(items)}

    def upsert_site(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ApiBadRequestError("请求体必须是 JSON 对象")
        raw_name = str(payload.get("name") or "").strip()
        try:
            name = validate_site_key(raw_name)
        except ValueError as exc:
            raise ApiBadRequestError(str(exc)) from exc

        with self.config_lock:
            sites = self.load_sites()
            existing_index = next(
                (idx for idx, s in enumerate(sites) if s.name == name),
                None,
            )
            if existing_index is None:
                ci_matches = [
                    idx for idx, s in enumerate(sites) if s.name.casefold() == name.casefold()
                ]
                if len(ci_matches) == 1:
                    existing_index = ci_matches[0]

            is_new = existing_index is None
            base_dir = self.config_path.parent

            if is_new:
                paths = derive_site_paths(name, base_dir)
                base_site = SiteConfig(
                    name=name,
                    fid=0,
                    url="",
                    jiekou="",
                    publish_dir=paths["publish_dir"],
                    daily_publish_filename=paths["daily_publish_filename"],
                    publish_time="06:00:00",
                    publish_times=["06:00:00"],
                    interval_time=60,
                    daily_publish_limit=60,
                    enabled=False,
                    image_file=None,
                    site_id=str(uuid.uuid4()),
                    activation_date=(date.today() + timedelta(days=1)).isoformat(),
                    admin_path=DEFAULT_ADMIN_PATH,
                )
            else:
                assert existing_index is not None
                base_site = sites[existing_index]

            updates: dict[str, Any] = {}

            # Endpoint & URL parsing
            full_endpoint = payload.get("full_endpoint")
            if isinstance(full_endpoint, str) and full_endpoint.strip():
                try:
                    parsed = parse_publish_endpoint(full_endpoint.strip())
                except ValueError as exc:
                    raise ApiBadRequestError(str(exc)) from exc
                updates["url"] = parsed.base_url
                updates["jiekou"] = parsed.publish_endpoint
            else:
                if "url" in payload:
                    raw_url = str(payload.get("url") or "").strip()
                    normalized_url = normalize_site_url(raw_url)
                    if normalized_url and not is_valid_site_url(normalized_url):
                        raise ApiBadRequestError("站点网址无效")
                    updates["url"] = normalized_url
                if "jiekou" in payload:
                    updates["jiekou"] = str(payload.get("jiekou") or "").strip()
                elif "api_key" in payload and str(payload.get("api_key") or "").strip():
                    updates["jiekou"] = build_import_endpoint(str(payload["api_key"]).strip())

            # Numeric & boolean fields
            for int_field, label in (
                ("fid", "栏目 ID"),
                ("interval_time", "发布间隔"),
                ("daily_publish_limit", "每日发布上限"),
                ("article_warning_threshold", "库存预警阈值"),
            ):
                if int_field in payload and payload[int_field] is not None and payload[int_field] != "":
                    raw_val = payload[int_field]
                    if isinstance(raw_val, bool):
                        raise ApiBadRequestError(f"{label}必须是整数")
                    try:
                        if isinstance(raw_val, float) and not raw_val.is_integer():
                            raise ValueError("non-integer float")
                        val = int(raw_val)
                    except (TypeError, ValueError) as exc:
                        raise ApiBadRequestError(f"{label}必须是整数") from exc
                    if val < 0:
                        raise ApiBadRequestError(f"{label}不能为负数")
                    updates[int_field] = val

            if "enabled" in payload:
                updates["enabled"] = bool(payload["enabled"])

            if "category_name" in payload:
                updates["category_name"] = str(payload.get("category_name") or "").strip()
            if "activation_date" in payload and str(payload.get("activation_date") or "").strip():
                updates["activation_date"] = str(payload["activation_date"]).strip()

            if "publish_dir" in payload:
                raw_pub_dir = payload.get("publish_dir")
                if raw_pub_dir is not None and not isinstance(raw_pub_dir, str):
                    raise ApiBadRequestError("文章目录必须是字符串")
                pub_dir_text = str(raw_pub_dir or "").strip()
                if pub_dir_text:
                    if _has_control_chars(pub_dir_text):
                        raise ApiBadRequestError("文章目录不能包含控制字符")
                    raw_dir = Path(pub_dir_text)
                    updates["publish_dir"] = (
                        raw_dir if raw_dir.is_absolute() else (base_dir / raw_dir).resolve()
                    )
            if "daily_publish_filename" in payload and str(payload.get("daily_publish_filename") or "").strip():
                raw_daily = Path(str(payload["daily_publish_filename"]).strip())
                updates["daily_publish_filename"] = (
                    raw_daily if raw_daily.is_absolute() else (base_dir / raw_daily).resolve()
                )
            if "image_file" in payload:
                raw_img = str(payload.get("image_file") or "").strip()
                if not raw_img:
                    updates["image_file"] = None
                    if "gallery_id" not in payload:
                        updates["gallery_id"] = ""
                else:
                    img_path = Path(raw_img)
                    updates["image_file"] = img_path if img_path.is_absolute() else (base_dir / img_path).resolve()

            if "gallery_id" in payload:
                raw_gid = str(payload.get("gallery_id") or "").strip()
                if raw_gid:
                    try:
                        gallery = self.gallery_service.require_gallery(raw_gid)
                    except GalleryError as exc:
                        raise ApiBadRequestError(str(exc)) from exc
                    updates["gallery_id"] = gallery.gallery_id
                    updates["image_file"] = gallery.file_path
                else:
                    updates["gallery_id"] = ""
                    if "image_file" not in payload or not str(payload.get("image_file") or "").strip():
                        updates["image_file"] = None

            if "publish_times" in payload and payload["publish_times"] is not None:
                norm_times = _parse_publish_times_input(payload["publish_times"])
                if norm_times:
                    updates["publish_times"] = norm_times
                    updates["publish_time"] = norm_times[0]
                else:
                    raw_pt = str(payload.get("publish_time") or "").strip()
                    if raw_pt:
                        if not is_valid_publish_time(raw_pt):
                            raise ApiBadRequestError(f"发布时间无效: {raw_pt}")
                        norm_pt = normalize_publish_time(raw_pt)
                        updates["publish_time"] = norm_pt
                        updates["publish_times"] = [norm_pt]
                    else:
                        updates["publish_times"] = []
                        updates["publish_time"] = ""
            elif "publish_time" in payload:
                raw_pt = str(payload.get("publish_time") or "").strip()
                if raw_pt:
                    if not is_valid_publish_time(raw_pt):
                        raise ApiBadRequestError(f"发布时间无效: {raw_pt}")
                    norm_pt = normalize_publish_time(raw_pt)
                    updates["publish_time"] = norm_pt
                    updates["publish_times"] = [norm_pt]
                else:
                    updates["publish_time"] = ""
                    updates["publish_times"] = []

            if "category_keywords" in payload and payload["category_keywords"] is not None:
                try:
                    updates["category_keywords"] = normalize_category_keywords(
                        payload["category_keywords"]
                    )
                except CategoryKeywordsError as exc:
                    raise ApiBadRequestError(f"栏目关键词无效: {exc}") from exc

            # Site-group & credentials fields
            for key, label in _SITEGROUP_TEXT_FIELDS:
                if key == "admin_path":
                    if key in payload:
                        try:
                            updates["admin_path"] = normalize_admin_path(
                                str(payload.get("admin_path") or "")
                            )
                        except ValueError as exc:
                            raise ApiBadRequestError(str(exc)) from exc
                    continue

                if key in payload:
                    val = str(payload.get(key) or "").strip()
                    if _has_control_chars(val):
                        raise ApiBadRequestError(f"{label}不能包含换行或控制字符")
                    if key == "site_group" and len(val) > SITE_GROUP_MAX_LENGTH:
                        raise ApiBadRequestError(f"{label}不能超过 {SITE_GROUP_MAX_LENGTH} 个字符")
                    if key in {"mcp_token", "admin_password"} and not val and not is_new:
                        clear_flag = bool(payload.get(f"clear_{key}", False))
                        if clear_flag:
                            updates[key] = ""
                    else:
                        updates[key] = val
                elif not is_new and bool(payload.get(f"clear_{key}", False)):
                    updates[key] = ""

            candidate = replace(base_site, **updates)
            new_sites = (
                [*sites, candidate]
                if is_new
                else [candidate if idx == existing_index else s for idx, s in enumerate(sites)]
            )
            try:
                self.config_service.save(new_sites)
            except ConfigLoadError as exc:
                raise ApiBadRequestError(str(exc)) from exc

            saved_sites = self.load_sites()
            saved_site = next((s for s in saved_sites if s.name == candidate.name), candidate)

        self._record_op_safe(
            "site",
            "create" if is_new else "update",
            site_name=saved_site.name,
            status="ok",
            summary=f"{'新增' if is_new else '更新'}站点 {saved_site.name}",
        )
        return {
            "created": is_new,
            "site": _serialize_site(saved_site, cached_status=self.db.get_site_status(saved_site.name)),
        }

    def delete_site(self, site_name: str) -> dict[str, Any]:
        with self.config_lock:
            sites = self.load_sites()
            target = self._find_site(site_name, sites)
            remaining = [s for s in sites if s.name != target.name]
            self.config_service.save(remaining)
            self._refresh_redactor(remaining)
        self.db.delete_site_status(target.name)
        self._record_op_safe(
            "site",
            "delete",
            site_name=target.name,
            status="ok",
            summary=f"删除站点 {target.name}",
        )
        return {"deleted": target.name}

    def bulk_update_sites(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ApiBadRequestError("请求体必须是 JSON 对象")
        mode = str(payload.get("mode") or "assign").strip().casefold()
        dry_run = bool(payload.get("dry_run", False))

        with self.config_lock:
            sites = self.load_sites()
            if mode == "assign":
                return self._bulk_update_assign(sites, payload, dry_run=dry_run)
            if mode == "text":
                return self._bulk_update_text(sites, payload, dry_run=dry_run)
            raise ApiBadRequestError(f"不支持的批量更新模式: {mode}")

    def _bulk_update_assign(
        self,
        sites: list[SiteConfig],
        payload: Mapping[str, Any],
        *,
        dry_run: bool,
    ) -> dict[str, Any]:
        target_sites = self._select_sites(payload.get("site_names"), require_non_empty=True, sites=sites)
        raw_fields = payload.get("fields")
        if not isinstance(raw_fields, Mapping):
            raise ApiBadRequestError("fields 必须是对象")

        updates: dict[str, Any] = dict(_validate_sitegroup_updates(raw_fields))
        for int_key, label in (
            ("fid", "栏目 ID"),
            ("daily_publish_limit", "每日发布上限"),
            ("interval_time", "发布间隔"),
        ):
            if int_key in raw_fields and raw_fields[int_key] not in (None, ""):
                try:
                    val = int(raw_fields[int_key])
                except (TypeError, ValueError) as exc:
                    raise ApiBadRequestError(f"{label}必须是整数") from exc
                if val < 0:
                    raise ApiBadRequestError(f"{label}不能为负数")
                updates[int_key] = val
        if "enabled" in raw_fields and raw_fields["enabled"] is not None:
            updates["enabled"] = bool(raw_fields["enabled"])

        if not updates:
            raise ApiBadRequestError("请至少填写一个需要更新的字段")

        target_names = {s.name for s in target_sites}
        updated_sites = [
            replace(site, **updates) if site.name in target_names else site
            for site in sites
        ]
        updated_names = [s.name for s in target_sites]

        if not dry_run:
            try:
                self.config_service.save(updated_sites)
            except ConfigLoadError as exc:
                raise ApiBadRequestError(str(exc)) from exc
            self._refresh_redactor(updated_sites)
            self._record_op_safe(
                "site",
                "bulk_update_assign",
                status="ok",
                summary=f"批量更新 {len(updated_names)} 个站点（{_format_masked_updates(updates)}）",
                detail={"sites": updated_names, "fields": sorted(updates.keys())},
            )

        return {
            "mode": "assign",
            "dry_run": dry_run,
            "updated": updated_names,
            "count": len(updated_names),
            "fields": sorted(updates.keys()),
        }

    def _bulk_update_text(
        self,
        sites: list[SiteConfig],
        payload: Mapping[str, Any],
        *,
        dry_run: bool,
    ) -> dict[str, Any]:
        raw_text = payload.get("text")
        if not isinstance(raw_text, str):
            raise ApiBadRequestError("text 必须是字符串")

        cleaned_text = raw_text.lstrip("\ufeff")
        records: list[dict[str, Any]] = []
        aggregated: dict[str, dict[str, str]] = {}

        for line_no, raw_line in enumerate(cleaned_text.splitlines(), start=1):
            line = raw_line.rstrip("\r\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            parts = _split_bulk_line(line)
            if (
                line_no == 1
                and parts
                and parts[0].casefold() in {"站点标识", "站点名", "name", "site_name", "site"}
            ):
                continue

            if len(parts) > 7 and any(parts[7:]):
                records.append(
                    {
                        "line": line_no,
                        "identifier": parts[0],
                        "matched_site": "",
                        "status": "error",
                        "fields": [],
                        "message": "列数过多（格式：站点标识|分组|MCP_Token|后台账号|后台密码|站点名称[|后台路径]）",
                    }
                )
                continue
            parts = parts[:7]

            identifier = parts[0]
            site, match_error = _match_site_by_identifier(identifier, sites)
            if site is None:
                records.append(
                    {
                        "line": line_no,
                        "identifier": identifier,
                        "matched_site": "",
                        "status": "unmatched",
                        "fields": [],
                        "message": match_error,
                    }
                )
                continue

            raw_fields = {
                key: (parts[idx] if idx < len(parts) else "")
                for idx, key in enumerate(_BULK_TEXT_KEYS, start=1)
            }
            try:
                updates = _validate_sitegroup_updates(raw_fields)
            except ApiBadRequestError as exc:
                records.append(
                    {
                        "line": line_no,
                        "identifier": identifier,
                        "matched_site": site.name,
                        "status": "error",
                        "fields": [],
                        "message": exc.message,
                    }
                )
                continue

            if not updates:
                records.append(
                    {
                        "line": line_no,
                        "identifier": identifier,
                        "matched_site": site.name,
                        "status": "unchanged",
                        "fields": [],
                        "message": "未填写需要更新的字段（跳过）",
                    }
                )
                continue

            aggregated.setdefault(site.name, {}).update(updates)
            records.append(
                {
                    "line": line_no,
                    "identifier": identifier,
                    "matched_site": site.name,
                    "status": "matched",
                    "fields": sorted(updates.keys()),
                    "message": _format_masked_updates(updates),
                }
            )

        updated_names = [s.name for s in sites if s.name in aggregated]
        if not dry_run and aggregated:
            updated_sites = [
                replace(site, **aggregated[site.name]) if site.name in aggregated else site
                for site in sites
            ]
            try:
                self.config_service.save(updated_sites)
            except ConfigLoadError as exc:
                raise ApiBadRequestError(str(exc)) from exc
            self._refresh_redactor(updated_sites)
            self._record_op_safe(
                "site",
                "bulk_update_text",
                status="ok",
                summary=f"文本批量更新 {len(updated_names)} 个站点",
                detail={"sites": updated_names},
            )

        return {
            "mode": "text",
            "dry_run": dry_run,
            "items": records,
            "updated": updated_names,
            "matched_count": sum(1 for r in records if r["status"] == "matched"),
            "unmatched_count": sum(1 for r in records if r["status"] == "unmatched"),
            "error_count": sum(1 for r in records if r["status"] == "error"),
        }

    def bulk_import_sites(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ApiBadRequestError("请求体必须是 JSON 对象")
        raw_text = payload.get("text")
        if not isinstance(raw_text, str):
            raise ApiBadRequestError("text 必须是字符串")
        dry_run = bool(payload.get("dry_run", True))
        raw_default_group = payload.get("default_group")
        default_group = str(raw_default_group if raw_default_group is not None else "").strip()
        if _has_control_chars(default_group):
            raise ApiBadRequestError("默认分组不能包含换行或控制字符")
        if len(default_group) > SITE_GROUP_MAX_LENGTH:
            raise ApiBadRequestError(f"默认分组不能超过 {SITE_GROUP_MAX_LENGTH} 个字符")

        smart_parser = parse_smart_site_import or getattr(
            _site_onboarding, "parse_smart_site_import", None
        )

        with self.config_lock:
            existing = self.load_sites()
            if callable(smart_parser):
                parsed = _call_with_supported_kwargs(
                    smart_parser,
                    raw_text,
                    existing,
                    self.config_path,
                    default_group=default_group,
                )
                new_sites = list(getattr(parsed, "new_sites", ()) or ())
                updated_sites = list(getattr(parsed, "updated_sites", ()) or ())
                warnings = list(getattr(parsed, "warnings", ()) or ())
                merged_count = int(getattr(parsed, "merged_count", 0) or 0)
            else:
                new_sites, warnings = parse_bulk_new_sites(raw_text, existing, self.config_path)
                if default_group:
                    new_sites = [
                        replace(site, site_group=default_group)
                        if not (site.site_group or "").strip()
                        else site
                        for site in new_sites
                    ]
                updated_sites = []
                merged_count = 0

            if not dry_run and (new_sites or updated_sites):
                updated_by_name = {site.name: site for site in updated_sites}
                saved_list = [updated_by_name.get(site.name, site) for site in existing] + new_sites
                try:
                    self.config_service.save(saved_list)
                except ConfigLoadError as exc:
                    raise ApiBadRequestError(str(exc)) from exc
                self._refresh_redactor(saved_list)
                self._record_op_safe(
                    "site",
                    "bulk_import",
                    status="ok",
                    summary=f"批量导入：新增 {len(new_sites)} 个，补全 {len(updated_sites)} 个站点",
                    detail={
                        "sites": [s.name for s in new_sites],
                        "updated_sites": [s.name for s in updated_sites],
                        "merged_count": merged_count,
                    },
                )

        serialized_new = [_serialize_site(site) for site in new_sites]
        serialized_updated = [_serialize_site(site) for site in updated_sites]
        return {
            "dry_run": dry_run,
            "imported_count": len(new_sites),
            "updated_count": len(updated_sites),
            "merged_count": merged_count,
            "new_sites": serialized_new,
            "updated_sites": serialized_updated,
            "sites": serialized_new,
            "warnings": warnings,
        }

    # ------------------------------------------------------------------ discover & MCP probe

    def submit_discover_job(self, payload: Mapping[str, Any]) -> JobRecord:
        sites = self._select_sites(payload.get("site_names"))
        probe_mcp = bool(payload.get("probe_mcp", True))
        if "backfill_titles" in payload:
            backfill_titles = bool(payload.get("backfill_titles", True))
        else:
            backfill_titles = bool(payload.get("backfill_title", True))
        max_concurrency = max(1, min(20, int(payload.get("max_concurrency", 6) or 6)))
        total_steps = len(sites) * (2 if probe_mcp else 1)

        def _job(update_progress: Callable[..., None], cancel_event: threading.Event) -> dict[str, Any]:
            discover_fn = self._discover_fn or auto_discover_sites
            probe_fn = self._probe_fn or probe_sites_batch

            done_counter = 0
            counter_lock = threading.Lock()

            def _on_discover(done: int, _total: int, res: SiteDiscoveryResult) -> None:
                nonlocal done_counter
                with counter_lock:
                    done_counter += 1
                    cur = done_counter
                update_progress(
                    cur,
                    total_steps,
                    f"首页探测: {res.site_name} ({res.message})",
                    item=_serialize_discovery_result(res),
                )

            discover_results = _call_with_supported_kwargs(
                discover_fn,
                sites,
                max_concurrency=max_concurrency,
                probe_fn=None,
                settings=self.settings,
                mcp_client_factory=self.mcp_client_factory,
                verify_ssl=self.settings.mcp.verify_ssl,
                update_site=False,
                progress=_on_discover,
                cancel_event=cancel_event,
            )

            probe_map: dict[str, McpProbeResult] = {}
            if probe_mcp and not cancel_event.is_set():
                def _on_probe(done: int, _total: int, res: McpProbeResult) -> None:
                    nonlocal done_counter
                    with counter_lock:
                        done_counter += 1
                        cur = done_counter
                    update_progress(
                        cur,
                        total_steps,
                        f"MCP 探测: {res.site_name} ({res.message})",
                        item=_serialize_probe_result(res),
                    )

                probe_results = _call_with_supported_kwargs(
                    probe_fn,
                    sites,
                    self.settings,
                    max_workers=max_concurrency,
                    progress=_on_probe,
                    cancel_event=cancel_event,
                )
                probe_map = {p.site_name: p for p in probe_results}

            discovered_titles: dict[str, str] = {}
            discovered_fids: dict[str, tuple[int, str]] = {}
            items_out: list[dict[str, Any]] = []
            for d in discover_results:
                probe = probe_map.get(d.site_name)
                serialized_item = _serialize_discovery_result(d, probe)
                db_kwargs: dict[str, Any] = {
                    "site_url": d.site_url,
                    "raw": serialized_item,
                }
                effective_title = (d.site_title or d.discovered_title or "").strip()
                if effective_title:
                    db_kwargs["site_title"] = effective_title
                if d.link_count is not None:
                    db_kwargs["link_count"] = d.link_count
                current_ver = str(
                    getattr(d, "current_version", "")
                    or (probe.server_version if probe is not None else "")
                    or ""
                ).strip()
                if current_ver:
                    db_kwargs["current_version"] = current_ver
                if probe is not None:
                    db_kwargs["mcp_ok"] = probe.ok
                    db_kwargs["mcp_tools"] = [t.name for t in probe.tools]
                elif d.mcp_ok is not None:
                    db_kwargs["mcp_ok"] = d.mcp_ok
                    if d.mcp_tools:
                        db_kwargs["mcp_tools"] = list(d.mcp_tools)
                self._upsert_status_safe(d.site_name, **db_kwargs)

                clean_discovered = (d.discovered_title or "").strip()
                if clean_discovered and not _has_control_chars(clean_discovered):
                    discovered_titles[d.site_name] = clean_discovered

                raw_fid = getattr(d, "default_fid", 0)
                try:
                    default_fid = int(raw_fid or 0)
                except (TypeError, ValueError):
                    default_fid = 0
                if default_fid > 0:
                    raw_cat = str(getattr(d, "default_category_name", "") or "").strip()
                    clean_cat = "" if _has_control_chars(raw_cat) else raw_cat
                    discovered_fids[d.site_name] = (default_fid, clean_cat)

                items_out.append(serialized_item)

            titles_backfilled: list[str] = []
            fids_backfilled: list[str] = []
            if backfill_titles and (discovered_titles or discovered_fids) and not cancel_event.is_set():
                with self.config_lock:
                    latest_sites = self.load_sites()
                    changed = False
                    new_sites: list[SiteConfig] = []
                    for site in latest_sites:
                        site_updates: dict[str, Any] = {}
                        new_title = discovered_titles.get(site.name)
                        if new_title and not (site.site_title or "").strip():
                            site_updates["site_title"] = new_title
                            titles_backfilled.append(site.name)
                        fid_info = discovered_fids.get(site.name)
                        if fid_info is not None and int(getattr(site, "fid", 0) or 0) == 0:
                            new_fid, new_cat_name = fid_info
                            if new_fid > 0:
                                site_updates["fid"] = new_fid
                                if new_cat_name and not (site.category_name or "").strip():
                                    site_updates["category_name"] = new_cat_name
                                fids_backfilled.append(site.name)
                        if site_updates:
                            new_sites.append(replace(site, **site_updates))
                            changed = True
                        else:
                            new_sites.append(site)
                    if changed:
                        self.config_service.save(new_sites)
                        self._refresh_redactor(new_sites)

            reachable_count = sum(1 for item in items_out if item["reachable"])
            mcp_ok_count = sum(1 for item in items_out if item["mcp_ok"] is True)
            self._record_op_safe(
                "site",
                "discover",
                status="ok",
                summary=f"探测 {len(items_out)} 站：首页可达 {reachable_count}，MCP 正常 {mcp_ok_count}",
                detail={
                    "titles_backfilled": titles_backfilled,
                    "fids_backfilled": fids_backfilled,
                },
            )
            return {
                "items": items_out,
                "total": len(items_out),
                "reachable": reachable_count,
                "mcp_ok": mcp_ok_count,
                "titles_backfilled": titles_backfilled,
                "fids_backfilled": fids_backfilled,
            }

        return self.job_runner.submit("discover", "探测站点与 MCP", _job, total=total_steps)

    def submit_probe_mcp_job(self, payload: Mapping[str, Any]) -> JobRecord:
        sites = self._select_sites(payload.get("site_names"))
        max_workers = max(1, min(20, int(payload.get("max_workers", 10) or 10)))
        site_by_name = {s.name: s for s in sites}

        def _job(update_progress: Callable[..., None], cancel_event: threading.Event) -> dict[str, Any]:
            probe_fn = self._probe_fn or probe_sites_batch

            def _on_probe(done: int, total: int, res: McpProbeResult) -> None:
                update_progress(
                    done,
                    total,
                    f"MCP 探测: {res.site_name} ({res.message})",
                    item=_serialize_probe_result(res),
                )

            results = _call_with_supported_kwargs(
                probe_fn,
                sites,
                self.settings,
                max_workers=max_workers,
                progress=_on_probe,
                cancel_event=cancel_event,
            )
            serialized: list[dict[str, Any]] = []
            for res in results:
                site = site_by_name.get(res.site_name)
                item_dict = _serialize_probe_result(res)
                serialized.append(item_dict)
                self._upsert_status_safe(
                    res.site_name,
                    site_url=site.url if site is not None else "",
                    mcp_ok=res.ok,
                    mcp_tools=[t.name for t in res.tools],
                    raw=item_dict,
                )

            ok_count = sum(1 for r in results if r.ok)
            self._record_op_safe(
                "site",
                "probe_mcp",
                status="ok",
                summary=f"MCP 探测 {len(results)} 站，成功 {ok_count} 站",
            )
            return {
                "items": serialized,
                "total": len(serialized),
                "ok_count": ok_count,
            }

        return self.job_runner.submit("probe_mcp", "批量探测 MCP", _job, total=len(sites))

    # ------------------------------------------------------------------ friendlinks

    def get_site_friendlinks(self, site_name: str) -> dict[str, Any]:
        site = self._find_site(site_name)
        try:
            links = self.friendlink_service.list_links(site)
        except FriendLinkError as exc:
            raise ApiError(
                self.redact_text(exc.message or str(exc)),
                status_code=502,
                hint=self.redact_text(exc.hint),
            ) from exc

        self._upsert_status_safe(site.name, site_url=site.url, link_count=len(links))
        return {
            "site_name": site.name,
            "link_count": len(links),
            "links": [_serialize_friendlink(item) for item in links],
        }

    def parse_friendlinks_input(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        raw_text = payload.get("text")
        if not isinstance(raw_text, str):
            raise ApiBadRequestError("text 必须是字符串")
        default_nofollow = bool(
            payload.get("default_nofollow", self.settings.friendlink.default_nofollow)
        )
        specs, errors = parse_batch_link_input(raw_text, default_nofollow=default_nofollow)
        return {
            "specs": [_serialize_spec(spec) for spec in specs],
            "errors": errors,
            "count": len(specs),
        }

    def _parse_specs_from_payload(self, payload: Mapping[str, Any]) -> tuple[list[FriendLinkSpec], list[str]]:
        default_nofollow = bool(
            payload.get("nofollow", self.settings.friendlink.default_nofollow)
        )
        default_remark = str(payload.get("remark") or "").strip()
        raw_input = (
            payload.get("text_or_specs")
            if "text_or_specs" in payload
            else payload.get("specs")
            if "specs" in payload
            else payload.get("text")
        )

        parse_errors: list[str] = []
        specs: list[FriendLinkSpec] = []
        if isinstance(raw_input, str):
            specs, parse_errors = parse_batch_link_input(
                raw_input, default_nofollow=default_nofollow
            )
        elif isinstance(raw_input, list):
            for idx, item in enumerate(raw_input, start=1):
                if isinstance(item, str):
                    sub_specs, sub_errs = parse_batch_link_input(
                        item, default_nofollow=default_nofollow
                    )
                    specs.extend(sub_specs)
                    parse_errors.extend(sub_errs)
                elif isinstance(item, Mapping):
                    link_val = str(item.get("link") or item.get("url") or "").strip()
                    title_val = str(item.get("title") or "").strip()
                    if not link_val:
                        parse_errors.append(f"第{idx}项：缺少 link")
                        continue
                    specs.append(
                        FriendLinkSpec(
                            title=title_val or link_val,
                            link=link_val,
                            nofollow=bool(item.get("nofollow", default_nofollow)),
                            back_link=str(item.get("back_link") or "").strip(),
                            my_title=str(item.get("my_title") or "").strip(),
                            my_link=str(item.get("my_link") or "").strip(),
                            contact=str(item.get("contact") or "").strip(),
                            remark=str(item.get("remark") or default_remark).strip(),
                        )
                    )
                else:
                    parse_errors.append(f"第{idx}项：格式无效")
        else:
            raise ApiBadRequestError("请提供 text_or_specs、specs 或 text")

        if default_remark:
            specs = [
                spec if spec.remark else replace(spec, remark=default_remark)
                for spec in specs
            ]
        if not specs:
            raise ApiBadRequestError(parse_errors[0] if parse_errors else "未解析到有效友链")
        return specs, parse_errors

    def submit_friendlink_batch_add(self, payload: Mapping[str, Any]) -> JobRecord:
        sites = self._select_sites(payload.get("site_names"), require_non_empty=True)
        specs, parse_errors = self._parse_specs_from_payload(payload)
        skip_existing = bool(payload.get("skip_existing", True))
        total_ops = len(sites) * len(specs)

        def _job(update_progress: Callable[..., None], cancel_event: threading.Event) -> dict[str, Any]:
            done = 0
            lock = threading.Lock()

            def _on_op(res: LinkOpResult) -> None:
                nonlocal done
                with lock:
                    done += 1
                    cur = done
                update_progress(
                    cur,
                    total_ops,
                    f"{res.site_name} -> {res.link}: {res.status}",
                    item=_serialize_link_op(res),
                )

            results = self.friendlink_service.batch_add(
                sites,
                specs,
                skip_existing=skip_existing,
                progress=_on_op,
                cancel_event=cancel_event,
            )
            counts = _count_by_status(results)
            self._record_op_safe(
                "friendlink",
                "batch_add",
                status="ok",
                summary=f"批量上链 {len(sites)} 站 × {len(specs)} 条：{counts}",
                detail={"counts": counts, "sites": [s.name for s in sites]},
            )
            return {
                "results": [_serialize_link_op(r) for r in results],
                "counts": counts,
                "parse_errors": parse_errors,
            }

        return self.job_runner.submit(
            "friendlink_batch_add",
            f"批量添加友链 ({len(sites)} 站)",
            _job,
            total=total_ops,
        )

    def submit_friendlink_batch_remove(self, payload: Mapping[str, Any]) -> JobRecord:
        sites = self._select_sites(payload.get("site_names"), require_non_empty=True)
        raw_input = (
            payload.get("text_or_urls")
            if "text_or_urls" in payload
            else payload.get("urls")
            if "urls" in payload
            else payload.get("text")
        )
        urls: list[str] = []
        if isinstance(raw_input, str):
            specs, errors = parse_batch_link_input(raw_input)
            urls = [s.link for s in specs]
            if not urls and errors:
                raise ApiBadRequestError(errors[0])
        elif isinstance(raw_input, list):
            for item in raw_input:
                if isinstance(item, str) and item.strip():
                    urls.append(item.strip())
                elif isinstance(item, Mapping):
                    link_val = str(item.get("link") or item.get("url") or "").strip()
                    if link_val:
                        urls.append(link_val)
        else:
            raise ApiBadRequestError("请提供 text_or_urls、urls 或 text")

        if not urls:
            raise ApiBadRequestError("请提供至少一个待删除的友链网址")

        total_ops = len(sites) * len(urls)

        def _job(update_progress: Callable[..., None], cancel_event: threading.Event) -> dict[str, Any]:
            done = 0
            lock = threading.Lock()

            def _on_op(res: LinkOpResult) -> None:
                nonlocal done
                with lock:
                    done += 1
                    cur = done
                update_progress(
                    cur,
                    total_ops,
                    f"{res.site_name} -/-> {res.link}: {res.status}",
                    item=_serialize_link_op(res),
                )

            results = self.friendlink_service.batch_remove(
                sites,
                urls,
                progress=_on_op,
                cancel_event=cancel_event,
            )
            counts = _count_by_status(results)
            self._record_op_safe(
                "friendlink",
                "batch_remove",
                status="ok",
                summary=f"批量下链 {len(sites)} 站 × {len(urls)} 条：{counts}",
                detail={"counts": counts, "sites": [s.name for s in sites]},
            )
            return {
                "results": [_serialize_link_op(r) for r in results],
                "counts": counts,
            }

        return self.job_runner.submit(
            "friendlink_batch_remove",
            f"批量删除友链 ({len(sites)} 站)",
            _job,
            total=total_ops,
        )

    def submit_friendlink_search(self, payload: Mapping[str, Any]) -> JobRecord:
        sites = self._select_sites(payload.get("site_names"))
        query = str(payload.get("query") or "").strip()
        match_root_domain = bool(payload.get("match_root_domain", True))

        def _job(update_progress: Callable[..., None], cancel_event: threading.Event) -> dict[str, Any]:
            done = 0
            lock = threading.Lock()

            def _on_progress(entry: dict[str, Any]) -> None:
                nonlocal done
                with lock:
                    done += 1
                    cur = done
                sname = entry.get("site_name", "")
                msg = (
                    f"{sname}: 错误 {entry['error']}"
                    if entry.get("error")
                    else f"{sname}: 共 {entry.get('total', 0)} 条，命中 {entry.get('matched', 0)} 条"
                )
                update_progress(cur, len(sites), msg, item=entry)

            result = self.friendlink_service.search_links_across_sites(
                sites,
                query=query,
                match_root_domain=match_root_domain,
                progress=_on_progress,
                cancel_event=cancel_event,
            )
            for sname, count in (result.get("site_counts") or {}).items():
                self._upsert_status_safe(sname, link_count=count)
            return result

        return self.job_runner.submit(
            "friendlink_search",
            "全站友链搜索",
            _job,
            total=len(sites),
        )

    def submit_friendlink_audit(self, payload: Mapping[str, Any]) -> JobRecord:
        sites = self._select_sites(payload.get("site_names"))
        raw_whitelist = payload.get("whitelist_domains", ())
        if isinstance(raw_whitelist, str):
            whitelist = [
                part.strip()
                for part in re.split(r"[\s,;，；]+", raw_whitelist)
                if part.strip()
            ]
        elif isinstance(raw_whitelist, list):
            whitelist = [str(item).strip() for item in raw_whitelist if str(item).strip()]
        else:
            whitelist = []

        def _job(update_progress: Callable[..., None], cancel_event: threading.Event) -> dict[str, Any]:
            done = 0
            lock = threading.Lock()

            def _on_item(entry: dict[str, Any]) -> None:
                nonlocal done
                with lock:
                    done += 1
                    cur = done
                update_progress(
                    cur,
                    max(cur, len(sites)),
                    f"{entry.get('site_name', '')}: {entry.get('link', '')} -> {entry.get('status', '')}",
                    item=entry,
                )

            report = self.friendlink_service.batch_check_backlinks(
                sites,
                whitelist_domains=whitelist,
                progress=_on_item,
                cancel_event=cancel_event,
            )

            per_site_total: dict[str, int] = {}
            per_site_broken: dict[str, int] = {}
            for item in report.get("items", ()):
                sname = str(item.get("site_name") or "")
                st = str(item.get("status") or "")
                per_site_total[sname] = per_site_total.get(sname, 0) + 1
                if st in {"missing", "unreachable"}:
                    per_site_broken[sname] = per_site_broken.get(sname, 0) + 1
                try:
                    self.db.record_backlink_audit(
                        site_name=sname,
                        target_url=str(item.get("link") or ""),
                        target_title=str(item.get("title") or ""),
                        status=st,
                        http_status=item.get("http_status"),
                        message=str(item.get("message") or ""),
                    )
                except Exception:  # noqa: BLE001
                    pass

            site_errors = report.get("site_errors") or {}
            for site in sites:
                if site.name in site_errors:
                    continue
                self._upsert_status_safe(
                    site.name,
                    site_url=site.url,
                    link_count=per_site_total.get(site.name, 0),
                    broken_link_count=per_site_broken.get(site.name, 0),
                )

            summary = report.get("summary") or {}
            self._record_op_safe(
                "friendlink",
                "audit",
                status="ok",
                summary=f"反链审计 {len(sites)} 站：共 {summary.get('total', 0)} 条，异常 {(summary.get('missing', 0) + summary.get('unreachable', 0))} 条",
                detail={"summary": summary, "site_errors": site_errors},
            )
            return report

        return self.job_runner.submit(
            "friendlink_audit",
            f"反链健康审计 ({len(sites)} 站)",
            _job,
            total=len(sites),
        )

    @staticmethod
    def _resolve_group_selector_arg(selector: Any, sites: Sequence[SiteConfig]) -> Any:
        if selector is None or isinstance(selector, str):
            return selector
        if isinstance(selector, list):
            known_groups = {(s.site_group or "").strip() for s in sites if (s.site_group or "").strip()}
            by_name = {s.name: s for s in sites}
            by_id = {s.site_id: s for s in sites if s.site_id}
            resolved: list[Any] = []
            for item in selector:
                key = str(item or "").strip()
                if not key:
                    continue
                if key in known_groups:
                    resolved.append(key)
                elif key in by_name:
                    resolved.append(by_name[key])
                elif key in by_id:
                    resolved.append(by_id[key])
                else:
                    resolved.append(key)
            return resolved
        return selector

    def preview_crosslink(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        mode = str(payload.get("mode") or "mesh").strip()
        sites = self._select_sites(payload.get("site_names"), require_non_empty=True)
        hub_site = str(payload.get("hub_site") or payload.get("hub_site_id") or "").strip()
        bidirectional = bool(payload.get("bidirectional", True))
        nofollow = bool(payload.get("nofollow", self.settings.friendlink.default_nofollow))
        remark = str(payload.get("remark") or "")
        avoid_reciprocal = bool(payload.get("avoid_reciprocal", True))
        avoid_same_root_domain = bool(payload.get("avoid_same_root_domain", True))

        raw_k = payload.get("k")
        k_val: int | None = None
        if raw_k is not None and raw_k != "":
            try:
                k_val = int(raw_k)
            except (TypeError, ValueError) as exc:
                raise ApiBadRequestError("k 必须是整数") from exc

        raw_seed = payload.get("seed")
        seed_val: int | None = None
        if raw_seed is not None and raw_seed != "":
            try:
                seed_val = int(raw_seed)
            except (TypeError, ValueError) as exc:
                raise ApiBadRequestError("seed 必须是整数") from exc

        group_a = self._resolve_group_selector_arg(payload.get("group_a"), sites)
        group_b = self._resolve_group_selector_arg(payload.get("group_b"), sites)

        try:
            raw_plan = self.friendlink_service.plan_crosslinks(
                sites,
                mode,
                hub_site_id=hub_site,
                bidirectional=bidirectional,
                nofollow=nofollow,
                remark=remark,
                k=k_val,
                seed=seed_val,
                avoid_reciprocal=avoid_reciprocal,
                avoid_same_root_domain=avoid_same_root_domain,
                group_a=group_a,
                group_b=group_b,
            )
        except ValueError as exc:
            raise ApiBadRequestError(str(exc)) from exc

        existing_by_site: dict[str, list[FriendLink]] = {}
        previewed = self.friendlink_service.preview_plan(
            raw_plan,
            sites,
            existing_out=existing_by_site,
        )
        fingerprint = previewed.fingerprint()
        now_mono = time.monotonic()

        with self._crosslink_lock:
            expired_keys = [
                key
                for key, entry in self._crosslink_cache.items()
                if entry.expires_at <= now_mono
            ]
            for key in expired_keys:
                self._crosslink_cache.pop(key, None)
            self._crosslink_cache[fingerprint] = _CachedCrosslinkPreview(
                plan=previewed,
                sites=sites,
                existing_by_site=existing_by_site,
                expires_at=now_mono + _CROSSLINK_PREVIEW_TTL_S,
            )

        return {
            "plan_id": previewed.plan_id,
            "mode": previewed.mode,
            "created_at": previewed.created_at,
            "hub_site_id": previewed.hub_site_id,
            "bidirectional": previewed.bidirectional,
            "fingerprint": fingerprint,
            "counts": previewed.counts,
            "items": [_serialize_crosslink_item(item) for item in previewed.items],
        }

    def submit_crosslink_execute(self, payload: Mapping[str, Any]) -> JobRecord:
        fingerprint = str(payload.get("fingerprint") or "").strip()
        if not fingerprint:
            raise ApiBadRequestError("缺少预览指纹 fingerprint，请先预览互链计划")

        now_mono = time.monotonic()
        with self._crosslink_lock:
            cached = self._crosslink_cache.get(fingerprint)
            if cached is None or cached.expires_at <= now_mono:
                self._crosslink_cache.pop(fingerprint, None)
                raise ApiConflictError("互链预览已过期或不存在，请重新生成预览")

            requested_plan_id = str(payload.get("plan_id") or "").strip()
            if requested_plan_id and requested_plan_id != cached.plan.plan_id:
                raise ApiConflictError("互链计划 ID 与当前预览不匹配，请重新生成预览")
            if cached.plan.fingerprint() != fingerprint:
                raise ApiConflictError("互链计划指纹校验失败，请重新生成预览")

            # Pop so cached existing_by_site is consumed once by execution.
            self._crosslink_cache.pop(fingerprint, None)

        plan = cached.plan
        target_sites = cached.sites
        cached_existing = cached.existing_by_site
        total_items = len(plan.items)

        def _job(update_progress: Callable[..., None], cancel_event: threading.Event) -> dict[str, Any]:
            done = 0
            lock = threading.Lock()

            def _on_op(res: LinkOpResult) -> None:
                nonlocal done
                with lock:
                    done += 1
                    cur = done
                update_progress(
                    cur,
                    total_items,
                    f"{res.site_name} -> {res.link}: {res.status}",
                    item=_serialize_link_op(res),
                )

            report: CrossLinkReport = self.friendlink_service.execute_plan(
                plan,
                target_sites,
                ledger=self.friendlink_ledger,
                existing_by_site=cached_existing,
                progress=_on_op,
                cancel_event=cancel_event,
            )
            counts = _count_by_status(report.results)
            self._record_op_safe(
                "friendlink",
                "crosslink_execute",
                status="ok",
                summary=f"执行互链计划 {report.plan.plan_id} ({report.plan.mode})：{counts}",
                detail={"plan_id": report.plan.plan_id, "mode": report.plan.mode, "counts": counts},
            )
            return {
                "plan_id": report.plan.plan_id,
                "mode": report.plan.mode,
                "counts": report.plan.counts,
                "items": [_serialize_crosslink_item(item) for item in report.plan.items],
                "results": [_serialize_link_op(res) for res in report.results],
                "result_counts": counts,
            }

        return self.job_runner.submit(
            "crosslink_execute",
            f"执行站群互链 ({plan.mode})",
            _job,
            total=total_items,
        )

    def list_crosslink_history(self) -> dict[str, Any]:
        plans = self.friendlink_ledger.list_plans()
        return {"plans": [_serialize_ledger_summary(plan) for plan in plans]}

    def submit_crosslink_rollback(self, payload: Mapping[str, Any]) -> JobRecord:
        plan_id = str(payload.get("plan_id") or "").strip()
        if not plan_id:
            raise ApiBadRequestError("plan_id 不能为空")
        entries = self.friendlink_ledger.created_links(plan_id)
        if not entries:
            raise ApiNotFoundError(f"未找到可回滚的互链记录: {plan_id}")
        sites = self.load_sites()

        def _job(update_progress: Callable[..., None], cancel_event: threading.Event) -> dict[str, Any]:
            done = 0
            lock = threading.Lock()

            def _on_op(res: LinkOpResult) -> None:
                nonlocal done
                with lock:
                    done += 1
                    cur = done
                update_progress(
                    cur,
                    len(entries),
                    f"回滚 {res.site_name} -> {res.link}: {res.status}",
                    item=_serialize_link_op(res),
                )

            results = self.friendlink_service.rollback_plan(
                plan_id,
                sites,
                ledger=self.friendlink_ledger,
                progress=_on_op,
                cancel_event=cancel_event,
            )
            counts = _count_by_status(results)
            self._record_op_safe(
                "friendlink",
                "crosslink_rollback",
                status="ok",
                summary=f"回滚互链计划 {plan_id}：{counts}",
                detail={"plan_id": plan_id, "counts": counts},
            )
            return {
                "plan_id": plan_id,
                "results": [_serialize_link_op(r) for r in results],
                "counts": counts,
            }

        return self.job_runner.submit(
            "crosslink_rollback",
            f"回滚互链计划 {plan_id}",
            _job,
            total=len(entries),
        )

    def export_friendlinks_html(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        sites = self._select_sites(payload.get("site_names"))
        include_nofollow = bool(
            payload.get("include_nofollow", self.settings.friendlink.default_nofollow)
        )
        html_text = export_sites_html_links(sites, include_nofollow=include_nofollow)
        line_count = len([line for line in html_text.splitlines() if line.strip()])
        return {"html": html_text, "count": line_count}

    # ------------------------------------------------------------------ upgrade

    def submit_upgrade_check(self, payload: Mapping[str, Any]) -> JobRecord:
        sites = self._select_sites(payload.get("site_names"))
        raw_workers = payload.get("max_workers")
        max_workers = int(raw_workers) if raw_workers not in (None, "") else None
        site_by_name = {s.name: s for s in sites}

        def _job(update_progress: Callable[..., None], cancel_event: threading.Event) -> dict[str, Any]:
            done = 0
            lock = threading.Lock()

            def _on_progress(prog: UpgradeProgress) -> None:
                nonlocal done
                with lock:
                    if prog.stage == "done":
                        done += 1
                    cur = done
                update_progress(cur, len(sites), f"{prog.site_name}: {prog.message}")

            infos = self.upgrade_service.check_versions(
                sites,
                progress=_on_progress,
                cancel_event=cancel_event,
                max_workers=max_workers,
            )
            serialized: list[dict[str, Any]] = []
            for info in infos:
                item = _serialize_version_info(info)
                serialized.append(item)
                site = site_by_name.get(info.site_name)
                db_kwargs: dict[str, Any] = {
                    "site_url": site.url if site is not None else "",
                    "raw": item,
                }
                if info.current_version:
                    db_kwargs["current_version"] = info.current_version
                if info.latest_version:
                    db_kwargs["latest_version"] = info.latest_version
                if info.upgrade_available is not None:
                    db_kwargs["update_available"] = info.upgrade_available
                if info.source == "mcp" and info.reachable:
                    db_kwargs["mcp_ok"] = True
                elif not info.reachable:
                    db_kwargs["mcp_ok"] = False
                self._upsert_status_safe(info.site_name, **db_kwargs)

            updates_count = sum(1 for info in infos if info.upgrade_available is True)
            self._record_op_safe(
                "upgrade",
                "check_versions",
                status="ok",
                summary=f"检测 {len(infos)} 站版本，可升级 {updates_count} 站",
            )
            return {
                "items": serialized,
                "total": len(serialized),
                "updates_available": updates_count,
            }

        return self.job_runner.submit(
            "upgrade_check",
            f"批量检测版本 ({len(sites)} 站)",
            _job,
            total=len(sites),
        )

    def submit_upgrade_run(self, payload: Mapping[str, Any]) -> JobRecord:
        if payload.get("confirmed") is not True:
            raise ApiBadRequestError("批量升级必须明确确认 (confirmed: true)")
        sites = self._select_sites(payload.get("site_names"), require_non_empty=True)
        canary = bool(payload.get("canary", payload.get("canary_first", True)))
        stop_on_failure = bool(payload.get("stop_on_failure", True))
        raw_conc = payload.get("concurrency", payload.get("max_workers"))
        concurrency: int | None = None
        if raw_conc not in (None, ""):
            try:
                concurrency = max(1, min(5, int(raw_conc)))
            except (TypeError, ValueError) as exc:
                raise ApiBadRequestError("concurrency 必须是 1-5 的整数") from exc

        def _job(update_progress: Callable[..., None], cancel_event: threading.Event) -> dict[str, Any]:
            done = 0
            lock = threading.Lock()

            def _on_progress(prog: UpgradeProgress) -> None:
                with lock:
                    cur = done
                update_progress(cur, len(sites), f"{prog.site_name} [{prog.stage}]: {prog.message}")

            def _on_result(res: UpgradeResult) -> None:
                nonlocal done
                with lock:
                    done += 1
                    cur = done
                item_dict = _serialize_upgrade_result(res)
                update_progress(
                    cur,
                    len(sites),
                    f"{res.site_name} -> {res.status}: {res.message}",
                    item=item_dict,
                )
                if res.status == UPGRADE_UPGRADED:
                    db_kwargs: dict[str, Any] = {"update_available": False, "raw": item_dict}
                    if res.to_version:
                        db_kwargs["current_version"] = res.to_version
                        db_kwargs["latest_version"] = res.to_version
                    self._upsert_status_safe(res.site_name, **db_kwargs)
                elif res.status == UPGRADE_ALREADY_LATEST:
                    ver = res.to_version or res.from_version
                    db_kwargs = {"update_available": False, "raw": item_dict}
                    if ver:
                        db_kwargs["current_version"] = ver
                        db_kwargs["latest_version"] = ver
                    self._upsert_status_safe(res.site_name, **db_kwargs)

                self._record_op_safe(
                    "upgrade",
                    "upgrade_site",
                    site_name=res.site_name,
                    status=res.status,
                    summary=res.message,
                    detail=item_dict,
                )

            results = self.upgrade_service.upgrade_sites(
                sites,
                confirmed=True,
                max_workers=concurrency,
                canary_first=canary,
                stop_on_failure=stop_on_failure,
                progress=_on_progress,
                result_callback=_on_result,
                cancel_event=cancel_event,
            )
            counts: dict[str, int] = {}
            for res in results:
                counts[res.status] = counts.get(res.status, 0) + 1
            return {
                "items": [_serialize_upgrade_result(r) for r in results],
                "total": len(results),
                "counts": counts,
            }

        return self.job_runner.submit(
            "upgrade_run",
            f"批量升级 ({len(sites)} 站)",
            _job,
            total=len(sites),
        )

    # ------------------------------------------------------------------ admin jump

    def resolve_admin_jump(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        site_name = str(payload.get("site_name") or "").strip()
        if not site_name:
            raise ApiBadRequestError("site_name 不能为空")
        open_browser = bool(payload.get("open_browser", False))
        site = self._find_site(site_name)

        if not self.admin_proxy.is_running:
            self.admin_proxy.start()

        try:
            plan = self.admin_jump_service.resolve(site)
        except Exception as exc:  # noqa: BLE001
            raise ApiBadRequestError(self.redact_text(str(exc)) or "无法解析后台跳转地址") from exc

        opened = False
        if open_browser:
            opener = getattr(self.admin_jump_service, "_opener", None)
            if callable(opener):
                try:
                    opened = opener(plan.target_url) is not False
                except Exception:  # noqa: BLE001
                    opened = False

        reason = "；".join(plan.fallback_reasons)
        self._record_op_safe(
            "admin",
            "jump",
            site_name=site.name,
            status="ok",
            summary=f"后台跳转 tier={plan.tier}",
            detail={"tier": plan.tier, "opened": opened},
        )
        return {
            "site_name": plan.site_name,
            "url": plan.target_url,
            "display_url": plan.display_url,
            "tier": plan.tier,
            "reason": reason,
            "fallback_reasons": list(plan.fallback_reasons),
            "opened": opened,
        }

    # ------------------------------------------------------------------ publish

    def _is_publish_running(self) -> bool:
        tm = self.task_manager
        return bool(tm is not None and tm.worker is not None and tm.worker.is_alive())

    def _append_publish_log(self, line: str) -> None:
        safe = self.redact_text(line)
        with self._publish_lock:
            self._publish_logs.append(safe)

    def get_publish_status(self) -> dict[str, Any]:
        sites = self.load_sites()
        summaries = [self._safe_summarize_site(site) for site in sites]
        tm = self.task_manager
        diagnostics = {}
        if tm is not None:
            diagnostics = {
                name: {
                    "code": diag.code,
                    "message": self.redact_text(diag.message),
                    "severity": diag.severity,
                }
                for name, diag in tm.last_build_diagnostics.items()
            }
        with self._publish_lock:
            stats = dict(self._publish_stats)
        return {
            "running": self._is_publish_running(),
            "paused": bool(tm is not None and not tm.pause_event.is_set()),
            "stopping": bool(tm is not None and tm.stop_event.is_set()),
            "stats": stats,
            "diagnostics": diagnostics,
            "sites": [
                {
                    "site_name": s.site_name,
                    "pending_count": s.pending_count,
                    "article_count": s.article_count,
                    "published_today": s.published_today,
                    "daily_limit": s.daily_limit,
                    "remaining_quota": s.remaining_quota,
                    "status": s.status,
                    "last_error": s.last_error,
                }
                for s in summaries
            ],
        }

    def start_publish(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self._is_publish_running():
            raise ApiConflictError("已有发布任务正在执行")

        if self.task_manager is None:
            publisher = Publisher(archive_service=self.archive_service)
            self.task_manager = TaskManager(
                publisher,
                max_concurrent_sites=3,
                gallery_service=self.gallery_service,
            )

        sites = self.load_sites()
        raw_names = payload.get("site_names")
        selected_sites = self._select_sites(raw_names, sites=sites)
        selected_set = {s.name for s in selected_sites} if raw_names else None
        allow_disabled = bool(payload.get("allow_disabled", bool(raw_names)))

        articles_by_site: dict[str, list[Any]] = {}
        for site in selected_sites:
            try:
                articles_by_site[site.name] = self.site_service.list_articles(site)
            except Exception:  # noqa: BLE001
                articles_by_site[site.name] = []

        tasks = self.task_manager.build_tasks(
            sites,
            articles_by_site,
            selected_sites=selected_set,
            allow_disabled=allow_disabled,
        )
        diagnostics = {
            name: {"code": d.code, "message": self.redact_text(d.message), "severity": d.severity}
            for name, d in self.task_manager.last_build_diagnostics.items()
        }
        if not tasks:
            return {
                "started": False,
                "task_count": 0,
                "sites": [],
                "diagnostics": diagnostics,
                "message": "没有可执行发布任务",
            }

        with self._publish_lock:
            self._publish_stats = {
                "total": len(tasks),
                "success": 0,
                "failed": 0,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "finished_at": "",
            }

        def _on_log(msg: str) -> None:
            self._append_publish_log(msg)

        def _on_result(res: PublishResult) -> bool:
            with self._publish_lock:
                if res.success:
                    self._publish_stats["success"] += 1
                else:
                    self._publish_stats["failed"] += 1
                done = self._publish_stats["success"] + self._publish_stats["failed"]
                if done >= self._publish_stats["total"]:
                    self._publish_stats["finished_at"] = datetime.now().isoformat(timespec="seconds")
            self._append_publish_log(f"{res.site_name} -> {res.title}: {res.message}")
            return True

        def _on_site_complete(site: SiteConfig, _results: list[PublishResult], reason: str) -> None:
            self._append_publish_log(f"{site.name} 队列完成 ({reason})")
            with self._publish_lock:
                if not self._is_publish_running():
                    self._publish_stats["finished_at"] = datetime.now().isoformat(timespec="seconds")

        self.task_manager.run_tasks(
            tasks,
            on_log=_on_log,
            on_result=_on_result,
            on_site_complete=_on_site_complete,
        )
        task_sites = sorted({site.name for site, _ in tasks})
        self._record_op_safe(
            "publish",
            "start",
            status="ok",
            summary=f"启动发布：{len(task_sites)} 站，{len(tasks)} 篇文章",
        )
        return {
            "started": True,
            "task_count": len(tasks),
            "sites": task_sites,
            "diagnostics": diagnostics,
        }

    def stop_publish(self) -> dict[str, Any]:
        was_running = self._is_publish_running()
        if self.task_manager is not None:
            self.task_manager.stop()
        self._append_publish_log("已请求停止发布任务")
        return {"stopped": True, "was_running": was_running}

    def get_publish_logs(self, *, lines: int = 200) -> dict[str, Any]:
        limit = max(1, min(2000, int(lines)))
        log_path = self.config_path.parent / "fabu.log"
        file_lines: list[str] = []
        exists = log_path.is_file()
        if exists:
            try:
                raw_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                file_lines = [self.redact_text(line) for line in raw_lines[-limit:]]
            except OSError:
                file_lines = []

        with self._publish_lock:
            mem_lines = list(self._publish_logs)[-limit:]

        combined = file_lines if file_lines else mem_lines
        return {
            "lines": combined[-limit:],
            "log_file": str(log_path),
            "exists": exists,
        }

    def get_publish_pending(self, site_name: str, *, limit: int = 50) -> dict[str, Any]:
        """Scan pending ``.txt`` articles in ``site.publish_dir`` via ``SiteService``."""
        site = self._find_site(site_name)
        cap = max(1, min(200, int(limit)))
        try:
            articles = self.site_service.list_articles(site)
        except (StateFileError, OSError) as exc:
            raise ApiBadRequestError(self.redact_text(str(exc)) or "扫描待发文章失败") from exc

        pending_articles = [article for article in articles if article.status == "pending"]
        error_count = sum(1 for article in articles if article.status == "error")
        return {
            "site_name": site.name,
            "publish_dir": str(site.publish_dir),
            "default_fid": site.fid,
            "count": len(pending_articles),
            "total": len(pending_articles),
            "error_count": error_count,
            "items": [
                {
                    "filename": article.path.name,
                    "title": article.title,
                    "category_id": article.category_id,
                    "keywords": article.keywords,
                }
                for article in pending_articles[:cap]
            ],
        }

    # ------------------------------------------------------------------ categories, galleries & startup

    def fetch_site_categories(
        self,
        site_name: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fetch live hierarchical categories for ``site_name`` via ``CategoryService``."""
        site = self._find_site(site_name)
        full_endpoint = _resolve_site_publish_endpoint(site, payload)
        if not full_endpoint:
            raise ApiBadRequestError("站点缺少网址或发布接口，无法获取栏目")

        try:
            categories = self.category_service.fetch(full_endpoint)
        except ValueError as exc:
            raise ApiBadRequestError(
                self.redact_text(redact_sensitive_url(str(exc))) or "发布接口格式无效"
            ) from exc
        except CategoryServiceError as exc:
            raise ApiError(
                self.redact_text(redact_sensitive_url(str(exc))) or "获取栏目失败",
                status_code=502,
            ) from exc

        try:
            self.category_cache.put(full_endpoint, categories)
        except (OSError, ValueError):
            pass

        return {
            "site_name": site.name,
            "categories": [
                {
                    "id": category.id,
                    "parent_id": category.parent_id,
                    "title": category.title,
                }
                for category in categories
            ],
        }

    def list_galleries(self) -> dict[str, Any]:
        """Return all managed image galleries via ``GalleryService``."""
        try:
            galleries = self.gallery_service.list_galleries()
        except GalleryError as exc:
            raise ApiBadRequestError(str(exc)) from exc
        items = [_serialize_gallery(gallery) for gallery in galleries]
        return {"galleries": items, "total": len(items)}

    def get_gallery(self, gallery_id: str) -> dict[str, Any]:
        """Return a single managed image gallery by ``gallery_id``."""
        target_id = str(gallery_id or "").strip()
        if not target_id:
            raise ApiBadRequestError("图库 ID 不能为空")
        try:
            gallery = self.gallery_service.get_gallery(target_id)
        except GalleryError as exc:
            raise ApiBadRequestError(str(exc)) from exc
        if gallery is None:
            raise ApiNotFoundError(f"图库不存在: {target_id}")
        return _serialize_gallery(gallery)

    def save_gallery(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Create or update a managed image gallery via ``GalleryService``."""
        if not isinstance(payload, Mapping):
            raise ApiBadRequestError("请求体必须是 JSON 对象")

        gallery_id = str(payload.get("id") or "").strip()
        has_name = "name" in payload
        has_content = "content" in payload and payload.get("content") is not None

        with self.config_lock:
            if not gallery_id:
                raw_name = payload.get("name")
                raw_content = payload.get("content")
                if not isinstance(raw_content, str):
                    raise ApiBadRequestError("图库内容必须是文本")
                try:
                    with tempfile.TemporaryDirectory() as tmpdir:
                        src_path = Path(tmpdir) / "gallery.txt"
                        src_path.write_bytes(raw_content.encode("utf-8"))
                        gallery = self.gallery_service.create_gallery(raw_name, src_path)
                except GalleryError as exc:
                    raise ApiBadRequestError(str(exc)) from exc
                created = True
            else:
                try:
                    existing = self.gallery_service.get_gallery(gallery_id)
                except GalleryError as exc:
                    raise ApiBadRequestError(str(exc)) from exc
                if existing is None:
                    raise ApiNotFoundError(f"图库不存在: {gallery_id}")

                if has_content:
                    raw_content = payload["content"]
                    if not isinstance(raw_content, str):
                        raise ApiBadRequestError("图库内容必须是文本")
                    validation = validate_gallery_content(raw_content)
                    if not validation.valid:
                        raise ApiBadRequestError(validation.message)

                gallery = existing
                if has_name:
                    raw_name = payload["name"]
                    try:
                        gallery = self.gallery_service.rename_gallery(gallery_id, raw_name)
                    except GalleryError as exc:
                        raise ApiBadRequestError(str(exc)) from exc

                if has_content:
                    try:
                        atomic_write_text(gallery.file_path, payload["content"])
                    except OSError as exc:
                        raise ApiError(f"保存图库文件失败: {exc}", status_code=500) from exc
                created = False

        serialized = _serialize_gallery(gallery)
        self._record_op_safe(
            "gallery",
            "create" if created else "update",
            status="ok",
            summary=f"{'新增' if created else '更新'}图库 {gallery.name}",
            detail={"gallery_id": gallery.gallery_id, "image_count": serialized["image_count"]},
        )
        return {"created": created, "gallery": serialized}

    def delete_gallery(self, gallery_id: str) -> dict[str, Any]:
        """Delete an image gallery unless bound by one or more configured sites."""
        target_id = str(gallery_id or "").strip()
        if not target_id:
            raise ApiBadRequestError("图库 ID 不能为空")

        with self.config_lock:
            sites = self.load_sites()
            bound_sites = [
                site.name
                for site in sites
                if (site.gallery_id or "").strip() == target_id
            ]
            if bound_sites:
                raise ApiBadRequestError(
                    f"图库正在被以下站点使用，不能删除: {', '.join(bound_sites)}"
                )
            try:
                existing = self.gallery_service.get_gallery(target_id)
            except GalleryError as exc:
                raise ApiBadRequestError(str(exc)) from exc
            if existing is None:
                raise ApiNotFoundError(f"图库不存在: {target_id}")
            try:
                self.gallery_service.delete_gallery(
                    target_id,
                    referenced_gallery_ids=frozenset(
                        site.gallery_id for site in sites if site.gallery_id
                    ),
                )
            except GalleryError as exc:
                raise ApiBadRequestError(str(exc)) from exc

        self._record_op_safe(
            "gallery",
            "delete",
            status="ok",
            summary=f"删除图库 {existing.name}",
            detail={"gallery_id": target_id},
        )
        return {"deleted": target_id}

    def get_startup_status(self) -> dict[str, Any]:
        """Return Windows auto-start support and current state via ``StartupService``."""
        supported = bool(self.startup_service.supported)
        enabled = False
        if supported:
            try:
                enabled = bool(self.startup_service.is_enabled())
            except (OSError, StartupPreferenceError) as exc:
                raise ApiError(f"读取自启动状态失败: {exc}", status_code=500) from exc
        return {"supported": supported, "enabled": enabled}

    def set_startup_enabled(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Enable or disable Windows login auto-start via ``StartupService``."""
        if not isinstance(payload, Mapping):
            raise ApiBadRequestError("请求体必须是 JSON 对象")
        raw_enabled = payload.get("enabled")
        if not isinstance(raw_enabled, bool):
            raise ApiBadRequestError("enabled 必须是布尔值")

        try:
            changed = self.startup_service.set_enabled(raw_enabled)
            supported = bool(self.startup_service.supported)
            enabled = bool(self.startup_service.is_enabled()) if supported else False
        except (OSError, StartupPreferenceError) as exc:
            raise ApiError(f"设置开机自启动失败: {exc}", status_code=500) from exc

        self._record_op_safe(
            "settings",
            "startup_toggle",
            status="ok",
            summary=f"{'开启' if enabled else '关闭'}开机自启动",
            detail={"supported": supported, "enabled": enabled, "changed": bool(changed)},
        )
        return {
            "supported": supported,
            "enabled": enabled,
            "changed": bool(changed),
        }

    # ------------------------------------------------------------------ settings & MCP

    def get_settings_data(self) -> dict[str, Any]:
        data = asdict(self.settings)
        data["settings_path"] = str(self.settings_file)
        return data

    def update_settings(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        new_settings = _merge_integration_settings(self.settings, payload)
        save_integration_settings(self.settings_file, new_settings)
        self.reload_settings(new_settings)
        self._record_op_safe(
            "settings",
            "update",
            status="ok",
            summary="更新集成高级设置并热重载服务",
        )
        return self.get_settings_data()

    def get_mcp_config(self) -> dict[str, Any]:
        display_host = "127.0.0.1" if self.host in {"0.0.0.0", "::", ""} else self.host
        port = self.bound_port or self.configured_port
        http_url = f"http://{display_host}:{port}/mcp"
        mcp_script = str((app_dir() / "mcp_server.py").resolve())
        config_str = str(self.config_path)

        if getattr(sys, "frozen", False):
            stdio_cmd = sys.executable
            stdio_args = ["--mcp-stdio", "--config", config_str]
        else:
            stdio_cmd = sys.executable
            stdio_args = [mcp_script, "--config", config_str]

        streamable_http = {
            "mcpServers": {
                "anqicms-sitegroup": {
                    "type": "http",
                    "url": http_url,
                }
            }
        }
        stdio = {
            "mcpServers": {
                "anqicms-sitegroup": {
                    "command": stdio_cmd,
                    "args": stdio_args,
                }
            }
        }
        return {
            "http_url": http_url,
            "stdio_command": stdio_cmd,
            "stdio_args": stdio_args,
            "mcp_script": mcp_script,
            "config_path": config_str,
            "streamable_http": streamable_http,
            "stdio": stdio,
            "streamable_http_json": json.dumps(streamable_http, ensure_ascii=False, indent=2),
            "stdio_json": json.dumps(stdio, ensure_ascii=False, indent=2),
        }

    def _get_or_create_mcp_server(self) -> Any:
        with self._mcp_lock:
            if self._mcp_server is not None:
                return self._mcp_server
            try:
                from core.sitegroup_mcp_server import SiteGroupMcpServer
            except ImportError as exc:
                raise ApiUnavailableError("SiteGroupMcpServer 模块暂不可用") from exc

            canonical_services: dict[str, Any] = {
                "config_path": self.config_path,
                "settings": self.settings,
                "db": self.db,
                "site_service": self.site_service,
                "friendlink_service": self.friendlink_service,
                "friendlink_ledger": self.friendlink_ledger,
                "upgrade_service": self.upgrade_service,
                "admin_jump_service": self.admin_jump_service,
                "admin_proxy": self.admin_proxy,
                "mcp_client_factory": self.mcp_client_factory,
            }
            aliases: dict[str, Any] = {
                **canonical_services,
                "config_service": self.config_service,
                "integration_settings": self.settings,
                "sitegroup_db": self.db,
                "ledger": self.friendlink_ledger,
                "jump_service": self.admin_jump_service,
                "proxy": self.admin_proxy,
                "sites_provider": self.load_sites,
                "task_manager": self.task_manager,
            }

            try:
                sig = inspect.signature(SiteGroupMcpServer.__init__)
                params = sig.parameters
                has_var_kw = any(
                    p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
                )
                kwargs: dict[str, Any] = {}
                for param_name, param in params.items():
                    if param_name == "self":
                        continue
                    if param.kind in (
                        inspect.Parameter.VAR_POSITIONAL,
                        inspect.Parameter.VAR_KEYWORD,
                    ):
                        continue
                    if param_name in aliases:
                        kwargs[param_name] = aliases[param_name]
                if has_var_kw:
                    for key, val in canonical_services.items():
                        kwargs.setdefault(key, val)
                instance = SiteGroupMcpServer(**kwargs)
            except (TypeError, ValueError):
                instance = SiteGroupMcpServer(self.config_path)

            self._mcp_server = instance
            return instance

    def handle_mcp_jsonrpc(self, payload: Any) -> Any:
        server = self._get_or_create_mcp_server()
        return server.handle_jsonrpc(payload)

    # ------------------------------------------------------------------ jobs & audit logs

    def list_jobs(self, *, limit: int = 50) -> dict[str, Any]:
        cap = max(1, min(500, int(limit)))
        return {"jobs": [job.to_dict() for job in self.job_runner.list_jobs(limit=cap)]}

    def get_job(self, job_id: str) -> dict[str, Any]:
        job = self.job_runner.get(job_id)
        if job is None:
            raise ApiNotFoundError(f"任务不存在: {job_id}")
        return job.to_dict()

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        if not self.job_runner.cancel(job_id):
            raise ApiNotFoundError(f"任务不存在: {job_id}")
        job = self.job_runner.get(job_id)
        return {
            "cancelled": True,
            "job": job.to_dict() if job is not None else {"job_id": job_id},
        }

    def get_audit_logs(
        self,
        *,
        limit: int = 100,
        category: str = "",
        site_name: str = "",
        status: str = "",
    ) -> dict[str, Any]:
        cap = max(1, min(500, int(limit)))
        operations = []
        for row in self.db.list_operations(
            category=category or None,
            limit=cap,
            site_name=site_name or None,
        ):
            item = dict(row)
            item["summary"] = self.redact_text(str(item.get("summary") or ""))
            item["detail"] = _safe_json_dict(item.get("detail_json"))
            operations.append(item)

        audits = []
        for row in self.db.list_backlink_audits(
            site_name=site_name or None,
            status=status or None,
            limit=cap,
        ):
            item = dict(row)
            item["message"] = self.redact_text(str(item.get("message") or ""))
            audits.append(item)

        return {
            "operations": operations,
            "backlink_audits": audits,
        }


class _ConsoleHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass,
        *,
        reuse_address: bool = False,
    ) -> None:
        self.allow_reuse_address = reuse_address
        super().__init__(server_address, RequestHandlerClass)


class WebConsoleServer:
    """Lifecycle-managed local HTTP server exposing the Web Console & MCP endpoint."""

    def __init__(
        self,
        app_or_config_path: WebConsoleApp | str | Path = "config.ini",
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        app: WebConsoleApp | None = None,
        **app_kwargs: Any,
    ) -> None:
        if app is not None:
            self.app = app
        elif isinstance(app_or_config_path, WebConsoleApp):
            self.app = app_or_config_path
        else:
            self.app = WebConsoleApp(
                app_or_config_path,
                host=host,
                port=port,
                **app_kwargs,
            )
        if getattr(self.app, "_server_stop_fn", None) is None:
            self.app._server_stop_fn = self.stop
        self._host = host if app is None and not isinstance(app_or_config_path, WebConsoleApp) else (
            app_kwargs.get("host") or self.app.host or host
        )
        self._configured_port = int(
            port if app is None and not isinstance(app_or_config_path, WebConsoleApp) else (
                port or self.app.configured_port
            )
        )
        self._lock = threading.RLock()
        self._server: _ConsoleHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._port: int = 0

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        with self._lock:
            return self._port

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._server is not None

    @property
    def url(self) -> str:
        port = self.port or self._configured_port
        display_host = "127.0.0.1" if self._host in {"0.0.0.0", "::", ""} else self._host
        return f"http://{display_host}:{port}" if port else ""

    def start(self) -> WebConsoleServer:
        """Start the HTTP server on a background daemon thread (idempotent)."""
        with self._lock:
            if self._server is not None:
                return self
            handler_cls = self._build_handler_class()
            server = _ConsoleHTTPServer(
                (self._host, self._configured_port),
                handler_cls,
                reuse_address=(self._configured_port != 0),
            )
            bound_port = int(server.server_address[1])
            thread = threading.Thread(
                target=server.serve_forever,
                name=f"web-console-{bound_port}",
                daemon=True,
            )
            self._server = server
            self._thread = thread
            self._port = bound_port
            self.app.host = self._host
            self.app.bound_port = bound_port
            thread.start()
            return self

    def stop(self) -> None:
        """Stop the HTTP server and shut down ``self.app`` resources."""
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
            self._port = 0
            self.app.bound_port = 0
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        self.app.shutdown()

    def __enter__(self) -> WebConsoleServer:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def _build_handler_class(self):
        app = self.app

        class _ConsoleRequestHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                _LOGGER.debug(format, *args)

            def do_OPTIONS(self) -> None:  # noqa: N802
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.send_header("Allow", "GET, POST, PUT, DELETE, OPTIONS")
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                self._dispatch("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._dispatch("POST")

            def do_PUT(self) -> None:  # noqa: N802
                self._dispatch("PUT")

            def do_DELETE(self) -> None:  # noqa: N802
                self._dispatch("DELETE")

            def _read_body_bytes(self) -> bytes:
                raw_len = self.headers.get("Content-Length")
                if raw_len is None or not raw_len.strip():
                    return b""
                try:
                    length = int(raw_len.strip())
                except ValueError as exc:
                    raise ApiBadRequestError("无效的 Content-Length") from exc
                if length < 0:
                    raise ApiBadRequestError("无效的 Content-Length")
                if length > _MAX_REQUEST_BYTES:
                    raise ApiError("请求体过大", status_code=413)
                if length == 0:
                    return b""
                return self.rfile.read(length)

            def _read_json_body(self, *, allow_empty: bool = True) -> dict[str, Any]:
                raw = self._read_body_bytes()
                if not raw or not raw.strip():
                    if allow_empty:
                        return {}
                    raise ApiBadRequestError("请求体不能为空")
                try:
                    decoded = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    raise ApiBadRequestError("请求体不是有效 UTF-8 JSON") from exc
                if not isinstance(decoded, dict):
                    raise ApiBadRequestError("请求体必须是 JSON 对象")
                return decoded

            def _send_bytes(
                self,
                status_code: int,
                body: bytes,
                *,
                content_type: str,
                cache_control: str = "no-store",
                extra_headers: Mapping[str, str] | None = None,
            ) -> None:
                try:
                    self.send_response(status_code)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", cache_control)
                    if extra_headers:
                        for header_name, header_value in extra_headers.items():
                            self.send_header(header_name, header_value)
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.end_headers()
                    if body:
                        self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def _send_json(self, status_code: int, payload: Any) -> None:
                body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
                self._send_bytes(
                    status_code,
                    body,
                    content_type="application/json; charset=utf-8",
                    cache_control="no-store",
                )

            def _send_ok(self, data: Any, *, status_code: int = 200) -> None:
                self._send_json(status_code, {"ok": True, "data": data})

            def _send_error_json(
                self,
                status_code: int,
                message: str,
                *,
                hint: str = "",
            ) -> None:
                body: dict[str, Any] = {
                    "ok": False,
                    "error": app.redact_text(message),
                }
                if hint:
                    body["hint"] = app.redact_text(hint)
                self._send_json(status_code, body)

            def _serve_static_file(self, rel_path: str) -> None:
                clean_rel = rel_path.lstrip("/")
                if not clean_rel or "\\" in clean_rel or "\x00" in clean_rel:
                    self._send_error_json(404, "静态资源不存在")
                    return
                static_root = app.static_dir.resolve()
                target = (static_root / clean_rel).resolve()
                try:
                    target.relative_to(static_root)
                except ValueError:
                    self._send_error_json(403, "禁止访问静态目录外的文件")
                    return
                if not target.is_file():
                    self._send_error_json(404, "静态资源不存在")
                    return

                suffix = target.suffix.lower()
                mime = _STATIC_MIME_TYPES.get(suffix)
                if mime is None:
                    guessed, _ = mimetypes.guess_type(str(target))
                    mime = guessed or "application/octet-stream"
                    if mime.startswith("text/") and "charset=" not in mime:
                        mime = f"{mime}; charset=utf-8"

                try:
                    data = target.read_bytes()
                except OSError as exc:
                    self._send_error_json(500, f"读取静态资源失败: {exc}")
                    return
                self._send_bytes(
                    200,
                    data,
                    content_type=mime,
                    cache_control="no-store, no-cache, must-revalidate, max-age=0",
                    extra_headers={"Pragma": "no-cache"},
                )

            def _handle_mcp(self, method: str) -> None:
                if method != "POST":
                    self._send_bytes(
                        405,
                        b'{"jsonrpc":"2.0","error":{"code":-32000,"message":"Method Not Allowed"}}',
                        content_type="application/json; charset=utf-8",
                    )
                    return
                try:
                    raw = self._read_body_bytes()
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError, ApiError):
                    self._send_json(
                        400,
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {"code": -32700, "message": "Parse error"},
                        },
                    )
                    return

                try:
                    result = app.handle_mcp_jsonrpc(payload)
                except ApiError as exc:
                    self._send_json(
                        exc.status_code,
                        {
                            "jsonrpc": "2.0",
                            "id": payload.get("id") if isinstance(payload, dict) else None,
                            "error": {"code": -32603, "message": app.redact_text(exc.message)},
                        },
                    )
                    return
                except Exception as exc:  # noqa: BLE001
                    self._send_json(
                        500,
                        {
                            "jsonrpc": "2.0",
                            "id": payload.get("id") if isinstance(payload, dict) else None,
                            "error": {"code": -32603, "message": app.redact_text(str(exc))},
                        },
                    )
                    return

                if result is None:
                    self._send_bytes(
                        202,
                        b"",
                        content_type="application/json; charset=utf-8",
                    )
                    return
                self._send_json(200, result)

            def _dispatch(self, method: str) -> None:
                parsed = urlsplit(self.path)
                path = parsed.path or "/"
                query = parse_qs(parsed.query, keep_blank_values=False)

                if path in {"/mcp", "/mcp/"}:
                    self._handle_mcp(method)
                    return

                if method == "GET":
                    if path in {"/", "/index.html"}:
                        self._serve_static_file("index.html")
                        return
                    if path.startswith("/static/"):
                        self._serve_static_file(unquote(path[len("/static/"):]))
                        return
                    if path == "/favicon.ico":
                        ico_path = app.static_dir / "favicon.ico"
                        if ico_path.is_file():
                            self._serve_static_file("favicon.ico")
                        else:
                            self._send_bytes(204, b"", content_type="image/x-icon")
                        return

                try:
                    data = self._route_api(method, path, query)
                except ApiError as exc:
                    self._send_error_json(exc.status_code, exc.message, hint=exc.hint)
                except (ConfigLoadError, CategoryKeywordsError, ValueError) as exc:
                    self._send_error_json(400, str(exc))
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.exception("Unhandled API error on %s %s", method, path)
                    self._send_error_json(500, f"服务器内部错误: {exc}")
                else:
                    self._send_ok(data)

            @staticmethod
            def _q_str(query: dict[str, list[str]], key: str, default: str = "") -> str:
                values = query.get(key)
                return values[0].strip() if values else default

            @staticmethod
            def _q_int(query: dict[str, list[str]], key: str, default: int) -> int:
                raw = query.get(key)
                if not raw:
                    return default
                try:
                    return int(raw[0].strip())
                except ValueError as exc:
                    raise ApiBadRequestError(f"查询参数 {key} 必须是整数") from exc

            def _route_api(
                self,
                method: str,
                path: str,
                query: dict[str, list[str]],
            ) -> Any:
                # Health & Dashboard
                if path == "/api/health":
                    if method != "GET":
                        raise ApiError("不支持的请求方法", status_code=405)
                    return app.get_health()
                if path == "/api/dashboard":
                    if method != "GET":
                        raise ApiError("不支持的请求方法", status_code=405)
                    return app.get_dashboard()

                # Sites collection & actions
                if path == "/api/sites":
                    if method == "GET":
                        enabled_raw = self._q_str(query, "enabled").casefold()
                        enabled_filter: bool | None = None
                        if enabled_raw in {"1", "true", "yes"}:
                            enabled_filter = True
                        elif enabled_raw in {"0", "false", "no"}:
                            enabled_filter = False
                        reveal_secrets = self._q_str(query, "reveal").casefold() in {"1", "true", "yes"}
                        return app.list_sites(
                            group=self._q_str(query, "group"),
                            query=self._q_str(query, "q"),
                            enabled=enabled_filter,
                            reveal_secrets=reveal_secrets,
                        )
                    if method == "POST":
                        return app.upsert_site(self._read_json_body(allow_empty=False))
                    raise ApiError("不支持的请求方法", status_code=405)

                if path == "/api/sites/bulk-update":
                    if method != "POST":
                        raise ApiError("不支持的请求方法", status_code=405)
                    return app.bulk_update_sites(self._read_json_body(allow_empty=False))

                if path == "/api/sites/bulk-import":
                    if method != "POST":
                        raise ApiError("不支持的请求方法", status_code=405)
                    return app.bulk_import_sites(self._read_json_body(allow_empty=False))

                if path == "/api/sites/discover":
                    if method != "POST":
                        raise ApiError("不支持的请求方法", status_code=405)
                    job = app.submit_discover_job(self._read_json_body(allow_empty=True))
                    return {"job_id": job.job_id, "job": job.to_dict()}

                if path == "/api/sites/probe-mcp":
                    if method != "POST":
                        raise ApiError("不支持的请求方法", status_code=405)
                    job = app.submit_probe_mcp_job(self._read_json_body(allow_empty=True))
                    return {"job_id": job.job_id, "job": job.to_dict()}

                if path.startswith("/api/sites/"):
                    raw_suffix = path[len("/api/sites/"):].strip("/")
                    if not raw_suffix:
                        raise ApiNotFoundError("API 路径不存在")
                    segments = raw_suffix.split("/")
                    if len(segments) == 2 and segments[1] == "categories":
                        site_name = unquote(segments[0]).strip()
                        if not site_name:
                            raise ApiNotFoundError("API 路径不存在")
                        if method != "POST":
                            raise ApiError("不支持的请求方法", status_code=405)
                        return app.fetch_site_categories(
                            site_name,
                            self._read_json_body(allow_empty=True),
                        )
                    if len(segments) != 1:
                        raise ApiNotFoundError("API 路径不存在")
                    site_name = unquote(segments[0]).strip()
                    if not site_name:
                        raise ApiNotFoundError("API 路径不存在")
                    if method == "DELETE":
                        return app.delete_site(site_name)
                    if method == "GET":
                        site = app._find_site(site_name)
                        reveal_secrets = self._q_str(query, "reveal").casefold() in {"1", "true", "yes"}
                        return {
                            "site": _serialize_site(
                                site,
                                cached_status=app.db.get_site_status(site.name),
                                reveal_secrets=reveal_secrets,
                            )
                        }
                    raise ApiError("不支持的请求方法", status_code=405)

                # Image galleries
                if path == "/api/galleries":
                    if method == "GET":
                        return app.list_galleries()
                    if method == "POST":
                        return app.save_gallery(self._read_json_body(allow_empty=False))
                    raise ApiError("不支持的请求方法", status_code=405)

                if path.startswith("/api/galleries/"):
                    raw_gid = path[len("/api/galleries/"):].strip("/")
                    if not raw_gid or "/" in raw_gid:
                        raise ApiNotFoundError("API 路径不存在")
                    gallery_id = unquote(raw_gid).strip()
                    if not gallery_id:
                        raise ApiNotFoundError("API 路径不存在")
                    if method == "DELETE":
                        return app.delete_gallery(gallery_id)
                    if method == "GET":
                        return {"gallery": app.get_gallery(gallery_id)}
                    raise ApiError("不支持的请求方法", status_code=405)

                # Friendlinks
                if path.startswith("/api/friendlinks/site/"):
                    if method != "GET":
                        raise ApiError("不支持的请求方法", status_code=405)
                    site_name = unquote(path[len("/api/friendlinks/site/"):]).strip("/")
                    if not site_name or "/" in site_name:
                        raise ApiNotFoundError("API 路径不存在")
                    return app.get_site_friendlinks(site_name)

                if path == "/api/friendlinks/parse":
                    if method != "POST":
                        raise ApiError("不支持的请求方法", status_code=405)
                    return app.parse_friendlinks_input(self._read_json_body(allow_empty=True))

                if path == "/api/friendlinks/batch-add":
                    if method != "POST":
                        raise ApiError("不支持的请求方法", status_code=405)
                    job = app.submit_friendlink_batch_add(self._read_json_body(allow_empty=False))
                    return {"job_id": job.job_id, "job": job.to_dict()}

                if path == "/api/friendlinks/batch-remove":
                    if method != "POST":
                        raise ApiError("不支持的请求方法", status_code=405)
                    job = app.submit_friendlink_batch_remove(self._read_json_body(allow_empty=False))
                    return {"job_id": job.job_id, "job": job.to_dict()}

                if path == "/api/friendlinks/search":
                    if method != "POST":
                        raise ApiError("不支持的请求方法", status_code=405)
                    job = app.submit_friendlink_search(self._read_json_body(allow_empty=True))
                    return {"job_id": job.job_id, "job": job.to_dict()}

                if path == "/api/friendlinks/audit":
                    if method != "POST":
                        raise ApiError("不支持的请求方法", status_code=405)
                    job = app.submit_friendlink_audit(self._read_json_body(allow_empty=True))
                    return {"job_id": job.job_id, "job": job.to_dict()}

                if path == "/api/friendlinks/crosslink/preview":
                    if method != "POST":
                        raise ApiError("不支持的请求方法", status_code=405)
                    return app.preview_crosslink(self._read_json_body(allow_empty=False))

                if path == "/api/friendlinks/crosslink/execute":
                    if method != "POST":
                        raise ApiError("不支持的请求方法", status_code=405)
                    job = app.submit_crosslink_execute(self._read_json_body(allow_empty=False))
                    return {"job_id": job.job_id, "job": job.to_dict()}

                if path == "/api/friendlinks/crosslink/history":
                    if method != "GET":
                        raise ApiError("不支持的请求方法", status_code=405)
                    return app.list_crosslink_history()

                if path == "/api/friendlinks/crosslink/rollback":
                    if method != "POST":
                        raise ApiError("不支持的请求方法", status_code=405)
                    job = app.submit_crosslink_rollback(self._read_json_body(allow_empty=False))
                    return {"job_id": job.job_id, "job": job.to_dict()}

                if path == "/api/friendlinks/export-html":
                    if method != "POST":
                        raise ApiError("不支持的请求方法", status_code=405)
                    return app.export_friendlinks_html(self._read_json_body(allow_empty=True))

                # Upgrade
                if path == "/api/upgrade/check":
                    if method != "POST":
                        raise ApiError("不支持的请求方法", status_code=405)
                    job = app.submit_upgrade_check(self._read_json_body(allow_empty=True))
                    return {"job_id": job.job_id, "job": job.to_dict()}

                if path == "/api/upgrade/run":
                    if method != "POST":
                        raise ApiError("不支持的请求方法", status_code=405)
                    job = app.submit_upgrade_run(self._read_json_body(allow_empty=False))
                    return {"job_id": job.job_id, "job": job.to_dict()}

                # Admin jump
                if path == "/api/admin/jump":
                    if method != "POST":
                        raise ApiError("不支持的请求方法", status_code=405)
                    return app.resolve_admin_jump(self._read_json_body(allow_empty=False))

                # Publish
                if path == "/api/publish/status":
                    if method != "GET":
                        raise ApiError("不支持的请求方法", status_code=405)
                    return app.get_publish_status()

                if path == "/api/publish/start":
                    if method != "POST":
                        raise ApiError("不支持的请求方法", status_code=405)
                    return app.start_publish(self._read_json_body(allow_empty=True))

                if path == "/api/publish/stop":
                    if method != "POST":
                        raise ApiError("不支持的请求方法", status_code=405)
                    return app.stop_publish()

                if path == "/api/publish/logs":
                    if method != "GET":
                        raise ApiError("不支持的请求方法", status_code=405)
                    return app.get_publish_logs(lines=self._q_int(query, "lines", 200))

                if path.startswith("/api/publish/pending/"):
                    raw_name = path[len("/api/publish/pending/"):].strip("/")
                    if not raw_name or "/" in raw_name:
                        raise ApiNotFoundError("API 路径不存在")
                    site_name = unquote(raw_name).strip()
                    if not site_name:
                        raise ApiNotFoundError("API 路径不存在")
                    if method != "GET":
                        raise ApiError("不支持的请求方法", status_code=405)
                    return app.get_publish_pending(
                        site_name,
                        limit=self._q_int(query, "limit", 50),
                    )

                # System startup toggle
                if path == "/api/system/startup":
                    if method == "GET":
                        return app.get_startup_status()
                    if method == "POST":
                        return app.set_startup_enabled(self._read_json_body(allow_empty=False))
                    raise ApiError("不支持的请求方法", status_code=405)

                # Settings & MCP config
                if path == "/api/settings":
                    if method == "GET":
                        return app.get_settings_data()
                    if method == "PUT":
                        return app.update_settings(self._read_json_body(allow_empty=False))
                    raise ApiError("不支持的请求方法", status_code=405)

                if path == "/api/mcp-config":
                    if method != "GET":
                        raise ApiError("不支持的请求方法", status_code=405)
                    return app.get_mcp_config()

                # App self-update
                if path == "/api/app/update/check":
                    if method != "GET":
                        raise ApiError("不支持的请求方法", status_code=405)
                    return app.check_app_update()

                if path == "/api/app/update/apply":
                    if method != "POST":
                        raise ApiError("不支持的请求方法", status_code=405)
                    job = app.submit_app_update_apply(self._read_json_body(allow_empty=True))
                    return {"job_id": job.job_id, "job": job.to_dict()}

                # Jobs
                if path == "/api/jobs":
                    if method != "GET":
                        raise ApiError("不支持的请求方法", status_code=405)
                    return app.list_jobs(limit=self._q_int(query, "limit", 50))

                if path.startswith("/api/jobs/"):
                    suffix = path[len("/api/jobs/"):].strip("/")
                    if suffix.endswith("/cancel"):
                        if method != "POST":
                            raise ApiError("不支持的请求方法", status_code=405)
                        job_id = unquote(suffix[:-len("/cancel")]).strip("/")
                        if not job_id or "/" in job_id:
                            raise ApiNotFoundError("API 路径不存在")
                        return app.cancel_job(job_id)
                    if "/" in suffix or not suffix:
                        raise ApiNotFoundError("API 路径不存在")
                    if method != "GET":
                        raise ApiError("不支持的请求方法", status_code=405)
                    return app.get_job(unquote(suffix))

                # Audit logs
                if path == "/api/audit-logs":
                    if method != "GET":
                        raise ApiError("不支持的请求方法", status_code=405)
                    return app.get_audit_logs(
                        limit=self._q_int(query, "limit", 100),
                        category=self._q_str(query, "category"),
                        site_name=self._q_str(query, "site_name"),
                        status=self._q_str(query, "status"),
                    )

                raise ApiNotFoundError("API 路径不存在")

        return _ConsoleRequestHandler
