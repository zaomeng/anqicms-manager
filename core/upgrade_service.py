"""Batch version detection and staged upgrade service for independent AnqiCMS sites.

This module talks to sites through the ``McpClientFactory`` / ``McpClientLike``
protocol defined in :mod:`core.mcp_contracts` (never importing a concrete MCP
client at module import time), with optional configurable REST fallback paths
(disabled/empty by default).
"""

from __future__ import annotations

import importlib
import inspect
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable

import requests

from core.integration_settings import IntegrationSettings
from core.mcp_contracts import (
    APPROVAL_HINT,
    AUTH_HINT,
    REDIRECT_HINT,
    SCOPE_HINT,
    SSL_HINT,
    McpApprovalRequiredError,
    McpAuthError,
    McpClientFactory,
    McpClientLike,
    McpError,
    McpProtocolError,
    McpScopeError,
    McpToolError,
    McpUnavailableError,
    classify_scope_error,
    detect_approval_required,
    is_upgrade_candidate_tool,
    tool_action_enum,
)
from core.redaction import SecretRedactor
from core.site_credentials import effective_mcp_token
from models.entities import SiteConfig
from models.sitegroup import (
    UPGRADE_ALREADY_LATEST,
    UPGRADE_AUTH_FAILED,
    UPGRADE_CANCELLED,
    UPGRADE_FAILED,
    UPGRADE_NO_TOOL,
    UPGRADE_SCOPE_DISABLED,
    UPGRADE_SKIPPED,
    UPGRADE_TIMEOUT,
    UPGRADE_UNREACHABLE,
    UPGRADE_UPGRADED,
    McpToolInfo,
    McpToolResult,
    SiteVersionInfo,
    UpgradeProgress,
    UpgradeResult,
)

__all__ = [
    "compare_versions",
    "parse_version_payload",
    "scope_hint",
    "UpgradeService",
]

_MIN_CONCURRENCY = 1
_MAX_CONCURRENCY_CAP = 5
_DEFAULT_CHECK_CONCURRENCY = 10
_MAX_CHECK_CONCURRENCY_CAP = 20
_SUCCESS_JSON_CODES = {0, "0", 200, "200"}
_SUCCESS_STATUSES = frozenset({UPGRADE_UPGRADED, UPGRADE_ALREADY_LATEST})

_NO_UPGRADE_TOOL_HINT = (
    "该站未通过 MCP 暴露升级工具：请在后台 AI 统一配置→接口开放范围 勾选系统升级"
)
_NO_CHECK_TOOL_HINT = (
    "该站未通过 MCP 暴露版本检测工具：请在后台 设置→AI 统一配置→接口开放范围 检查接口权限"
)
_UNREACHABLE_HINT = "请检查站点网址、网络连通性及 HTTPS 证书配置"

_SCOPE_HINTS: dict[str, str] = {
    "upgrade": "请登录该站后台：设置→AI 统一配置→接口开放范围，勾选「系统升级」后重试",
    "friendlink": "请登录该站后台：设置→AI 统一配置→接口开放范围，勾选「友情链接/内容管理」或在 设置→安全设置 开启 API 后重试",
    "jump": "请登录该站后台：设置→AI 统一配置→接口开放范围，勾选「多站点/单点登录」后重试",
}

_VERSION_CORE_RE = re.compile(
    r"^[vV]?(\d+(?:\.\d+)+)(?:-([0-9A-Za-z.\-]+))?(?:\+[0-9A-Za-z.\-]+)?$"
)
_TEXT_VERSION_RE = re.compile(
    r"(?<![0-9A-Za-z.])(v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.\-]+)?)(?![0-9A-Za-z]|\.\d)",
    re.IGNORECASE,
)
_CURRENT_LABEL_RE = re.compile(
    r"(?:当前版本|本地版本|当前|current[_\s-]*version|local[_\s-]*version)"
    r"[^0-9vV\n]{0,12}(v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.\-]+)?)",
    re.IGNORECASE,
)
_LATEST_LABEL_RE = re.compile(
    r"(?:最新版本|新版本|目标版本|最新|latest[_\s-]*version|new[_\s-]*version|latest)"
    r"[^0-9vV\n]{0,12}(v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.\-]+)?)",
    re.IGNORECASE,
)
_ALREADY_LATEST_TEXT_RE = re.compile(
    r"(?:已是最新|无需升级|没有新版本|already\s+(?:up\s+to\s+date|latest)|up\s+to\s+date)",
    re.IGNORECASE,
)
_PRERELEASE_CHUNK_RE = re.compile(r"\d+|[A-Za-z]+")

_CURRENT_VERSION_KEYS = (
    "current_version",
    "currentversion",
    "version",
    "local_version",
    "localversion",
    "app_version",
    "appversion",
    "system_version",
    "systemversion",
    "now_version",
    "nowversion",
    "old_version",
    "oldversion",
)
_LATEST_VERSION_KEYS = (
    "latest_version",
    "latestversion",
    "latest",
    "new_version",
    "newversion",
    "remote_version",
    "remoteversion",
    "target_version",
    "targetversion",
    "available_version",
    "availableversion",
)
_AVAILABLE_KEYS = (
    "need_upgrade",
    "needupgrade",
    "has_update",
    "hasupdate",
    "upgradable",
    "upgrade_available",
    "upgradeavailable",
    "can_upgrade",
    "canupgrade",
    "has_new_version",
    "hasnewversion",
    "is_updatable",
)
_NESTED_PAYLOAD_KEYS = ("data", "result", "system", "info", "version_info", "versionInfo")

_VERSION_ARG_NAMES = frozenset(
    {"version", "target_version", "targetversion", "to_version", "toversion"}
)
_CONFIRM_ARG_NAMES = frozenset(
    {"confirm", "confirmed", "approve", "approved", "force"}
)


def scope_hint(kind: str) -> str:
    """Return a Chinese remediation hint for a closed MCP scope category."""
    if not isinstance(kind, str):
        return SCOPE_HINT
    return _SCOPE_HINTS.get(kind.strip().casefold(), SCOPE_HINT)


def _parse_version(raw: str) -> tuple[tuple[int, ...], str] | None:
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    match = _VERSION_CORE_RE.match(cleaned)
    if not match:
        return None
    nums = tuple(int(part) for part in match.group(1).split("."))
    prerelease = match.group(2) or ""
    return nums, prerelease


def _prerelease_key(prerelease: str) -> tuple[tuple[int, int | str], ...]:
    tokens: list[tuple[int, int | str]] = []
    for part in re.split(r"[.\-]", prerelease):
        if not part:
            continue
        for chunk in _PRERELEASE_CHUNK_RE.findall(part):
            if chunk.isdigit():
                tokens.append((0, int(chunk)))
            else:
                tokens.append((1, chunk.casefold()))
    return tuple(tokens)


def compare_versions(a: str, b: str) -> int:
    """Compare two version strings like ``'v3.6.7'`` and ``'3.6.7-beta'``.

    Returns ``-1`` if ``a < b``, ``1`` if ``a > b``, and ``0`` if equal or if
    either string cannot be parsed as a version.
    """
    parsed_a = _parse_version(a)
    parsed_b = _parse_version(b)
    if parsed_a is None or parsed_b is None:
        return 0

    nums_a, pre_a = parsed_a
    nums_b, pre_b = parsed_b
    length = max(len(nums_a), len(nums_b))
    padded_a = nums_a + (0,) * (length - len(nums_a))
    padded_b = nums_b + (0,) * (length - len(nums_b))
    if padded_a < padded_b:
        return -1
    if padded_a > padded_b:
        return 1

    if pre_a == pre_b:
        return 0
    if pre_a and not pre_b:
        return -1
    if not pre_a and pre_b:
        return 1

    key_a = _prerelease_key(pre_a)
    key_b = _prerelease_key(pre_b)
    if key_a < key_b:
        return -1
    if key_a > key_b:
        return 1
    return 0


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().casefold()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n"}:
            return False
    return None


def _collect_mappings(root: Any) -> list[dict[str, Any]]:
    """Collect the top-level dict and any well-known nested payload dicts."""
    mappings: list[dict[str, Any]] = []
    if not isinstance(root, dict):
        return mappings
    queue: list[dict[str, Any]] = [root]
    seen: set[int] = set()
    while queue:
        current = queue.pop(0)
        obj_id = id(current)
        if obj_id in seen:
            continue
        seen.add(obj_id)
        mappings.append(current)
        for nested_key in _NESTED_PAYLOAD_KEYS:
            nested = current.get(nested_key)
            if isinstance(nested, dict):
                queue.append(nested)
    return mappings


def _lookup_str(mappings: list[dict[str, Any]], keys: tuple[str, ...]) -> str:
    for mapping in mappings:
        lowered_map = {str(k).casefold(): v for k, v in mapping.items()}
        for key in keys:
            value = lowered_map.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _lookup_bool(mappings: list[dict[str, Any]], keys: tuple[str, ...]) -> bool | None:
    for mapping in mappings:
        lowered_map = {str(k).casefold(): v for k, v in mapping.items()}
        for key in keys:
            if key in lowered_map:
                coerced = _coerce_bool(lowered_map[key])
                if coerced is not None:
                    return coerced
    return None


def parse_version_payload(result: McpToolResult) -> tuple[str, str, bool | None]:
    """Extract ``(current_version, latest_version, upgrade_available)``.

    Inspects ``result.structured`` first (falling back to JSON-decoding
    ``result.text``), and finally extracts version numbers from ``result.text``
    via regex.
    """
    text = result.text if isinstance(getattr(result, "text", ""), str) else ""
    structured = getattr(result, "structured", None)
    if structured is None and text.strip().startswith("{"):
        try:
            decoded = json.loads(text)
            if isinstance(decoded, dict):
                structured = decoded
        except ValueError:
            structured = None

    mappings = _collect_mappings(structured)
    current = _lookup_str(mappings, _CURRENT_VERSION_KEYS)
    latest = _lookup_str(mappings, _LATEST_VERSION_KEYS)
    available = _lookup_bool(mappings, _AVAILABLE_KEYS)

    if text and (not current or not latest):
        if not current:
            match = _CURRENT_LABEL_RE.search(text)
            if match:
                current = match.group(1).strip()
        if not latest:
            match = _LATEST_LABEL_RE.search(text)
            if match:
                latest = match.group(1).strip()

    if text and (not current or not latest):
        found = [m.group(1).strip() for m in _TEXT_VERSION_RE.finditer(text)]
        if not current and found:
            current = found[0]
            remaining = found[1:]
        else:
            remaining = [ver for ver in found if ver != current]
        if not latest and remaining:
            latest = remaining[0]

    if available is None:
        if current and latest and _parse_version(current) and _parse_version(latest):
            available = compare_versions(current, latest) < 0
        elif text and _ALREADY_LATEST_TEXT_RE.search(text):
            available = False

    return current, latest, available


def _site_name(site: Any) -> str:
    value = getattr(site, "name", "")
    return value if isinstance(value, str) else str(value or "")


def _site_id(site: Any) -> str:
    value = getattr(site, "site_id", "")
    return value if isinstance(value, str) else str(value or "")


def _site_url(site: Any) -> str:
    value = getattr(site, "url", "")
    return value.strip().rstrip("/") if isinstance(value, str) else ""


def _find_action_prop_name(properties: dict[str, Any]) -> str:
    for prop_name in properties:
        if isinstance(prop_name, str) and prop_name.casefold() == "action":
            return prop_name
    return ""


def _select_action_value(
    tool: McpToolInfo,
    preferred_exact: tuple[str, ...],
    contains_markers: tuple[str, ...] = (),
) -> tuple[str, str] | None:
    schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return None
    action_prop = _find_action_prop_name(properties)
    if not action_prop:
        return None
    actions = tool_action_enum(tool)
    if not actions:
        return None
    for wanted in preferred_exact:
        wanted_folded = wanted.casefold()
        match = next((a for a in actions if a.casefold() == wanted_folded), None)
        if match is not None:
            return action_prop, match
    for marker in contains_markers:
        marker_folded = marker.casefold()
        match = next((a for a in actions if marker_folded in a.casefold()), None)
        if match is not None:
            return action_prop, match
    return None


def _build_check_arguments(tool: McpToolInfo) -> dict[str, Any]:
    selected = _select_action_value(
        tool,
        ("check", "info", "check_update", "upgrade_check", "site_version", "version", "status"),
        ("check", "info", "version"),
    )
    if selected is None:
        return {}
    prop_name, value = selected
    return {prop_name: value}


def _build_upgrade_arguments(
    tool: McpToolInfo,
    target_version: str,
    *,
    confirmed: bool,
) -> dict[str, Any]:
    schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    arguments: dict[str, Any] = {}
    selected_action = _select_action_value(
        tool,
        ("upgrade", "system_upgrade", "update_system", "do_upgrade"),
        ("upgrade", "update_system", "system_update", "升级"),
    )
    if selected_action is not None:
        action_prop, action_val = selected_action
        prop_schema = properties.get(action_prop)
        prop_type = prop_schema.get("type") if isinstance(prop_schema, dict) else None
        if prop_type in (None, "string"):
            arguments[action_prop] = action_val
    for prop_name, prop_schema in properties.items():
        if not isinstance(prop_name, str):
            continue
        lowered = prop_name.casefold()
        prop_type = prop_schema.get("type") if isinstance(prop_schema, dict) else None
        if lowered in _VERSION_ARG_NAMES and target_version:
            if prop_type in (None, "string"):
                arguments[prop_name] = target_version
        elif confirmed and lowered in _CONFIRM_ARG_NAMES:
            if prop_type in (None, "boolean"):
                arguments[prop_name] = True
    return arguments


def _is_restart_interruption(exc: Exception) -> bool:
    """Return True if ``exc`` likely reflects the site dropping connections while restarting."""
    if isinstance(exc, requests.exceptions.SSLError):
        return False
    if isinstance(
        exc,
        (
            requests.ReadTimeout,
            requests.ConnectionError,
            TimeoutError,
            ConnectionError,
        ),
    ):
        return True
    if isinstance(exc, McpUnavailableError):
        cause = exc.__cause__
        if isinstance(cause, requests.exceptions.SSLError):
            return False
        text = f"{exc.message} {exc.hint}".casefold()
        if "ssl" in text or "证书" in text or "重定向" in text or "redirect" in text:
            return False
        if exc.http_status in (None, 502, 503, 504):
            return True
    return False


def _close_quietly(resource: Any) -> None:
    close_fn = getattr(resource, "close", None)
    if callable(close_fn):
        try:
            close_fn()
        except Exception:
            pass


def _join_site_path(base_url: str, path: str) -> str:
    cleaned = path.strip()
    if not cleaned.startswith("/"):
        cleaned = "/" + cleaned
    return f"{base_url.rstrip('/')}{cleaned}"


def _supports_auto_confirm(client: McpClientLike) -> bool:
    call_fn = getattr(client, "call_tool", None)
    if not callable(call_fn):
        return False
    try:
        sig = inspect.signature(call_fn)
    except (TypeError, ValueError):
        return False
    return "auto_confirm" in sig.parameters or any(
        param.kind == inspect.Parameter.VAR_KEYWORD
        for param in sig.parameters.values()
    )


def _approval_failure_hint(
    *,
    confirm_tool: str = "",
    exc_hint: str = "",
) -> str:
    if exc_hint:
        return exc_hint
    if confirm_tool:
        return (
            f"站点 MCP 返回了待确认状态（需要 {confirm_tool}），未能自动完成确认；"
            "请检查接口权限或登录站点后台手动确认升级"
        )
    return APPROVAL_HINT


class UpgradeService:
    """Batch version checks and staged upgrades across independent AnqiCMS sites."""

    def __init__(
        self,
        settings: IntegrationSettings | None = None,
        *,
        mcp_client_factory: McpClientFactory | None = None,
        session_factory: Callable[[], requests.Session] = requests.Session,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings or IntegrationSettings()
        self._mcp_client_factory = mcp_client_factory
        self._session_factory = session_factory
        self._sleep = sleep
        self._monotonic = monotonic

    def _clamp_concurrency(
        self,
        requested: int | None,
        item_count: int,
        *,
        cap: int = _MAX_CONCURRENCY_CAP,
        default: int | None = None,
    ) -> int:
        fallback = self._settings.upgrade.max_concurrency if default is None else default
        raw = fallback if requested is None else requested
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = fallback
        bounded = max(_MIN_CONCURRENCY, min(cap, value))
        if item_count <= 0:
            return _MIN_CONCURRENCY
        return min(bounded, item_count)

    def _open_mcp_client(self, site: SiteConfig) -> McpClientLike:
        if self._mcp_client_factory is not None:
            return self._mcp_client_factory(site)
        try:
            module = importlib.import_module("core.anqicms_mcp_client")
            client_cls = getattr(module, "AnqiMcpClient")
        except (ImportError, AttributeError) as exc:
            raise McpUnavailableError(
                "未配置 MCP 客户端",
                hint="请检查 MCP 客户端模块或传入 mcp_client_factory",
            ) from exc
        return client_cls.for_site(site, settings=self._settings)

    def _find_check_tools(self, client: McpClientLike) -> list[McpToolInfo]:
        up = self._settings.upgrade
        explicit_folded = {
            name.strip().casefold()
            for name in up.check_tool_names
            if isinstance(name, str) and name.strip()
        }
        candidates = list(
            client.find_tools(
                up.check_keywords,
                explicit_names=up.check_tool_names,
            )
        )
        seen = {t.name for t in candidates}
        # Multi-action upgrade tools like siteops_upgrade (action enum: ["check", "info", "upgrade"])
        # also provide update-check actions even if their description omits "版本"/"检查更新".
        upgrade_candidates = client.find_tools(
            up.upgrade_keywords,
            explicit_names=up.upgrade_tool_names,
        )
        for tool in upgrade_candidates:
            if tool.name in seen:
                continue
            actions = {a.casefold() for a in tool_action_enum(tool)}
            if actions & {"check", "info", "check_update", "upgrade_check"}:
                candidates.append(tool)
                seen.add(tool.name)

        filtered: list[McpToolInfo] = []
        for tool in candidates:
            if tool.name.casefold() in explicit_folded:
                filtered.append(tool)
                continue
            if tool.name.casefold() == "system_config":
                continue
            filtered.append(tool)

        def _priority(tool: McpToolInfo) -> tuple[int, int]:
            folded = tool.name.casefold()
            if folded in explicit_folded:
                return (0, 0)
            actions = {a.casefold() for a in tool_action_enum(tool)}
            if actions & {"check", "check_update", "upgrade_check"}:
                return (1, 0)
            if "check" in folded or "检查" in (tool.description or ""):
                return (1, 1)
            return (2, 0)

        filtered.sort(key=_priority)
        return filtered

    def _find_upgrade_tools(self, client: McpClientLike) -> list[McpToolInfo]:
        up = self._settings.upgrade
        candidates = client.find_tools(
            up.upgrade_keywords,
            explicit_names=up.upgrade_tool_names,
        )
        return [
            tool
            for tool in candidates
            if is_upgrade_candidate_tool(
                tool,
                explicit_names=up.upgrade_tool_names,
                include_check_tools=False,
            )
        ]

    @staticmethod
    def _has_disabled_upgrade_intent(client: McpClientLike) -> bool:
        has_disabled_fn = getattr(client, "has_disabled_intent", None)
        if not callable(has_disabled_fn):
            return False
        try:
            return bool(has_disabled_fn(("siteops_upgrade", "upgrade", "升级")))
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _read_local_version(client: McpClientLike, init_result: Any) -> tuple[str, str]:
        """Try ``system_config(action='site_version')`` then ``serverInfo.version``."""
        try:
            tools = list(client.list_tools())
        except Exception:  # noqa: BLE001
            tools = []

        # Prefer system_config when present, or any tool declaring a site_version action.
        version_tools: list[tuple[int, McpToolInfo, str, str]] = []
        for tool in tools:
            selected = _select_action_value(tool, ("site_version",))
            if selected is None:
                continue
            prop_name, action_val = selected
            rank = 0 if tool.name.casefold() == "system_config" else 1
            version_tools.append((rank, tool, prop_name, action_val))
        version_tools.sort(key=lambda item: item[0])

        for _, tool, prop_name, action_val in version_tools:
            try:
                res = client.call_tool(tool.name, {prop_name: action_val})
            except Exception:  # noqa: BLE001
                continue
            if not getattr(res, "is_error", False):
                ver, _, _ = parse_version_payload(res)
                if ver:
                    return ver, tool.name

        server_info = getattr(client, "server_info", None)
        if not isinstance(server_info, dict) or not server_info:
            server_info = init_result.get("serverInfo") if isinstance(init_result, dict) else {}
        if isinstance(server_info, dict):
            raw_ver = server_info.get("version")
            if isinstance(raw_ver, str) and raw_ver.strip():
                return raw_ver.strip(), ""
        return "", ""

    def _rest_headers_and_params(self, site: SiteConfig) -> tuple[dict[str, str], dict[str, str]]:
        token = effective_mcp_token(site)
        headers: dict[str, str] = {"Accept": "application/json"}
        params: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
            params["token"] = token
        return headers, params

    def _check_version_rest(
        self,
        site: SiteConfig,
        redactor: SecretRedactor,
    ) -> SiteVersionInfo | None:
        paths = [p for p in self._settings.upgrade.rest_version_paths if isinstance(p, str) and p.strip()]
        if not paths:
            return None
        base_url = _site_url(site)
        if not base_url:
            return None

        headers, params = self._rest_headers_and_params(site)
        timeout = (self._settings.mcp.connect_timeout, self._settings.mcp.read_timeout)
        verify = self._settings.mcp.verify_ssl
        last_auth_error: SiteVersionInfo | None = None

        session = self._session_factory()
        try:
            for path in paths:
                url = _join_site_path(base_url, path)
                try:
                    response = session.get(
                        url,
                        headers=headers,
                        params=params or None,
                        timeout=timeout,
                        verify=verify,
                        allow_redirects=False,
                    )
                except requests.exceptions.SSLError:
                    continue
                except requests.RequestException:
                    continue

                if response.status_code in (401, 403):
                    last_auth_error = SiteVersionInfo(
                        site_name=_site_name(site),
                        site_id=_site_id(site),
                        reachable=False,
                        source="none",
                        message=f"接口鉴权失败（HTTP {response.status_code}）",
                        hint=AUTH_HINT,
                    )
                    continue
                if 300 <= response.status_code < 400:
                    continue
                if not (200 <= response.status_code < 300):
                    continue

                try:
                    payload = response.json()
                except ValueError:
                    payload = None

                if isinstance(payload, dict) and "code" in payload:
                    if payload.get("code") not in _SUCCESS_JSON_CODES:
                        continue

                tool_result = McpToolResult(
                    is_error=False,
                    text=response.text or "",
                    structured=payload if isinstance(payload, dict) else None,
                )
                current, latest, available = parse_version_payload(tool_result)
                if current or latest or available is not None:
                    return SiteVersionInfo(
                        site_name=_site_name(site),
                        site_id=_site_id(site),
                        reachable=True,
                        current_version=current,
                        latest_version=latest,
                        upgrade_available=available,
                        source="rest",
                        message="通过备用接口检测成功",
                    )
        finally:
            _close_quietly(session)

        return last_auth_error

    def _trigger_upgrade_rest(
        self,
        site: SiteConfig,
        desired_version: str,
        redactor: SecretRedactor,
    ) -> tuple[str, str, str]:
        """Attempt REST upgrade fallback.

        Returns ``(outcome, message, hint)`` where outcome is one of
        ``"triggered"``, ``"auth_failed"``, ``"unreachable"``, ``"failed"``.
        """
        paths = [p for p in self._settings.upgrade.rest_upgrade_paths if isinstance(p, str) and p.strip()]
        if not paths:
            return "unreachable", "", ""

        base_url = _site_url(site)
        headers, params = self._rest_headers_and_params(site)
        timeout = (
            self._settings.mcp.connect_timeout,
            self._settings.upgrade.upgrade_call_timeout_s,
        )
        verify = self._settings.mcp.verify_ssl
        body: dict[str, Any] = {"confirm": True}
        if desired_version:
            body["version"] = desired_version

        last_status = "unreachable"
        last_message = "备用升级接口不可用"
        last_hint = _UNREACHABLE_HINT

        session = self._session_factory()
        try:
            for path in paths:
                url = _join_site_path(base_url, path)
                try:
                    response = session.post(
                        url,
                        headers=headers,
                        params=params or None,
                        json=body,
                        timeout=timeout,
                        verify=verify,
                        allow_redirects=False,
                    )
                except requests.exceptions.SSLError as exc:
                    last_status = "unreachable"
                    last_message = redactor(str(exc)) or "证书校验失败"
                    last_hint = SSL_HINT
                    continue
                except (requests.ReadTimeout, requests.ConnectionError):
                    return "triggered", "升级请求期间连接断开，正在等待站点重启", ""
                except requests.RequestException as exc:
                    last_status = "unreachable"
                    last_message = redactor(str(exc)) or "升级请求失败"
                    last_hint = _UNREACHABLE_HINT
                    continue

                if response.status_code in (401, 403):
                    return "auth_failed", f"备用升级接口鉴权失败（HTTP {response.status_code}）", AUTH_HINT
                if 300 <= response.status_code < 400:
                    last_status = "unreachable"
                    last_message = f"接口发生重定向（HTTP {response.status_code}）"
                    last_hint = REDIRECT_HINT
                    continue
                if response.status_code in (502, 503, 504):
                    return "triggered", f"站点返回 HTTP {response.status_code}，可能正在重启", ""
                if not (200 <= response.status_code < 300):
                    last_status = "failed"
                    last_message = f"备用升级接口返回 HTTP {response.status_code}"
                    last_hint = ""
                    continue

                try:
                    payload = response.json()
                except ValueError:
                    payload = None

                if isinstance(payload, dict) and "code" in payload:
                    if payload.get("code") not in _SUCCESS_JSON_CODES:
                        msg = redactor(str(payload.get("msg") or payload.get("message") or "备用升级接口返回失败"))
                        if classify_scope_error(msg):
                            return "scope_disabled", msg, scope_hint("upgrade")
                        return "failed", msg, ""

                return "triggered", "已通过备用接口触发升级", ""
        finally:
            _close_quietly(session)

        return last_status, last_message, last_hint

    def check_version(self, site: SiteConfig) -> SiteVersionInfo:
        """Detect a single site's current and latest version. Never raises."""
        name = _site_name(site)
        sid = _site_id(site)
        base_url = _site_url(site)
        if not base_url:
            return SiteVersionInfo(
                site_name=name,
                site_id=sid,
                reachable=False,
                source="none",
                message="缺少站点网址",
                hint="请先在站点配置中填写站点网址",
            )

        redactor = SecretRedactor.from_sites([site])
        client: McpClientLike | None = None
        check_tool_name = ""
        upgrade_tool_name = ""
        try:
            client = self._open_mcp_client(site)
            init_result = client.initialize()
            check_tools = self._find_check_tools(client)
            upgrade_tools = self._find_upgrade_tools(client)
            upgrade_tool_name = upgrade_tools[0].name if upgrade_tools else ""

            if not check_tools:
                rest_info = self._check_version_rest(site, redactor)
                if rest_info is not None and rest_info.reachable:
                    return SiteVersionInfo(
                        site_name=name,
                        site_id=sid,
                        reachable=True,
                        current_version=rest_info.current_version,
                        latest_version=rest_info.latest_version,
                        upgrade_available=rest_info.upgrade_available,
                        source="rest",
                        check_tool="",
                        upgrade_tool=upgrade_tool_name,
                        message=rest_info.message,
                        hint="" if upgrade_tool_name else (_NO_UPGRADE_TOOL_HINT if rest_info.upgrade_available else ""),
                    )

                local_version, local_tool_name = self._read_local_version(client, init_result)
                has_disabled_upgrade = self._has_disabled_upgrade_intent(client)
                if has_disabled_upgrade:
                    ver_prefix = f"当前版本 {local_version}" if local_version else "当前版本未知"
                    return SiteVersionInfo(
                        site_name=name,
                        site_id=sid,
                        reachable=True,
                        current_version=local_version,
                        latest_version="",
                        upgrade_available=None,
                        source="mcp",
                        check_tool="",
                        upgrade_tool=upgrade_tool_name,
                        message=(
                            f"{ver_prefix}（未开启 siteops_upgrade 意图，"
                            "请在后台「设置 → AI 统一配置」开启后即可检测更新与一键升级）"
                        ),
                        hint=scope_hint("upgrade"),
                    )
                if local_version:
                    return SiteVersionInfo(
                        site_name=name,
                        site_id=sid,
                        reachable=True,
                        current_version=local_version,
                        latest_version="",
                        upgrade_available=None,
                        source="mcp",
                        check_tool=local_tool_name,
                        upgrade_tool=upgrade_tool_name,
                        message=f"当前版本 {local_version}（站点未暴露版本更新检测工具）",
                        hint=_NO_CHECK_TOOL_HINT,
                    )
                return SiteVersionInfo(
                    site_name=name,
                    site_id=sid,
                    reachable=True,
                    source="mcp",
                    check_tool="",
                    upgrade_tool=upgrade_tool_name,
                    message="MCP 已连接，但未找到版本检测工具（未暴露版本检测接口）",
                    hint=_NO_CHECK_TOOL_HINT,
                )

            check_tool = check_tools[0]
            check_tool_name = check_tool.name
            check_args = _build_check_arguments(check_tool)
            result = client.call_tool(check_tool_name, check_args)
            if result.is_error:
                err_text = redactor(result.text) or "版本检测工具返回错误"
                if classify_scope_error(result.text):
                    return SiteVersionInfo(
                        site_name=name,
                        site_id=sid,
                        reachable=True,
                        source="mcp",
                        check_tool=check_tool_name,
                        upgrade_tool=upgrade_tool_name,
                        message=err_text,
                        hint=scope_hint("upgrade"),
                    )
                return SiteVersionInfo(
                    site_name=name,
                    site_id=sid,
                    reachable=True,
                    source="mcp",
                    check_tool=check_tool_name,
                    upgrade_tool=upgrade_tool_name,
                    message=err_text,
                )

            current, latest, available = parse_version_payload(result)
            if not current and (latest or available is not None):
                current, _ = self._read_local_version(client, init_result)
            hint = ""
            if available is True and not upgrade_tool_name:
                hint = _NO_UPGRADE_TOOL_HINT
            return SiteVersionInfo(
                site_name=name,
                site_id=sid,
                reachable=True,
                current_version=current,
                latest_version=latest,
                upgrade_available=available,
                source="mcp",
                check_tool=check_tool_name,
                upgrade_tool=upgrade_tool_name,
                message="检测成功" if (current or latest or available is not None) else "未解析到版本号",
                hint=hint,
            )
        except McpAuthError as exc:
            return SiteVersionInfo(
                site_name=name,
                site_id=sid,
                reachable=False,
                source="none",
                message=redactor(str(exc)) or "MCP 鉴权失败",
                hint=redactor(exc.hint) if exc.hint else AUTH_HINT,
            )
        except McpScopeError as exc:
            return SiteVersionInfo(
                site_name=name,
                site_id=sid,
                reachable=True,
                source="mcp",
                check_tool=check_tool_name,
                upgrade_tool=upgrade_tool_name,
                message=redactor(str(exc)) or "接口开放范围未勾选",
                hint=scope_hint("upgrade"),
            )
        except McpToolError as exc:
            raw_text = str(exc)
            if exc.result and exc.result.text:
                raw_text = f"{raw_text} {exc.result.text}"
            if classify_scope_error(raw_text):
                return SiteVersionInfo(
                    site_name=name,
                    site_id=sid,
                    reachable=True,
                    source="mcp",
                    check_tool=check_tool_name,
                    upgrade_tool=upgrade_tool_name,
                    message=redactor(str(exc)) or "接口开放范围未勾选",
                    hint=scope_hint("upgrade"),
                )
            return SiteVersionInfo(
                site_name=name,
                site_id=sid,
                reachable=True,
                source="mcp",
                check_tool=check_tool_name,
                upgrade_tool=upgrade_tool_name,
                message=redactor(str(exc)) or "版本检测工具调用失败",
                hint=redactor(exc.hint) if exc.hint else "",
            )
        except (McpUnavailableError, McpProtocolError) as exc:
            rest_info = self._check_version_rest(site, redactor)
            if rest_info is not None:
                return rest_info
            default_hint = _UNREACHABLE_HINT if isinstance(exc, McpUnavailableError) else ""
            return SiteVersionInfo(
                site_name=name,
                site_id=sid,
                reachable=False,
                source="none",
                message=redactor(str(exc)) or "站点不可达",
                hint=redactor(exc.hint) if exc.hint else default_hint,
            )
        except McpError as exc:
            return SiteVersionInfo(
                site_name=name,
                site_id=sid,
                reachable=False,
                source="none",
                message=redactor(str(exc)) or "MCP 请求失败",
                hint=redactor(exc.hint) if exc.hint else "",
            )
        except Exception as exc:
            return SiteVersionInfo(
                site_name=name,
                site_id=sid,
                reachable=False,
                source="none",
                message=redactor(str(exc)) or "检测失败",
            )
        finally:
            if client is not None:
                _close_quietly(client)

    def check_versions(
        self,
        sites: Iterable[SiteConfig],
        *,
        progress: Callable[[UpgradeProgress], None] | None = None,
        cancel_event: threading.Event | None = None,
        max_workers: int | None = None,
    ) -> list[SiteVersionInfo]:
        """Detect versions across sites concurrently, preserving input order."""
        site_list = list(sites)
        if not site_list:
            return []

        workers = self._clamp_concurrency(
            max_workers,
            len(site_list),
            cap=_MAX_CHECK_CONCURRENCY_CAP,
            default=_DEFAULT_CHECK_CONCURRENCY,
        )
        results: list[SiteVersionInfo | None] = [None] * len(site_list)

        def _worker(index: int, site: SiteConfig) -> None:
            name = _site_name(site)
            if cancel_event is not None and cancel_event.is_set():
                results[index] = SiteVersionInfo(
                    site_name=name,
                    site_id=_site_id(site),
                    reachable=False,
                    source="none",
                    message="已取消",
                )
                if progress is not None:
                    progress(UpgradeProgress(site_name=name, stage="done", message="已取消"))
                return

            if progress is not None:
                progress(UpgradeProgress(site_name=name, stage="checking", message="正在检测版本..."))
            info = self.check_version(site)
            results[index] = info
            if progress is not None:
                progress(UpgradeProgress(site_name=name, stage="done", message=info.message))

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(_worker, idx, site)
                for idx, site in enumerate(site_list)
            ]
            for future in futures:
                future.result()

        return [item for item in results if item is not None]

    def _probe_http_home(self, site: SiteConfig) -> bool:
        base_url = _site_url(site)
        if not base_url:
            return False
        timeout = (self._settings.mcp.connect_timeout, self._settings.mcp.read_timeout)
        verify = self._settings.mcp.verify_ssl
        session = self._session_factory()
        try:
            response = session.get(
                f"{base_url}/",
                timeout=timeout,
                verify=verify,
                allow_redirects=False,
            )
            return 200 <= response.status_code < 400
        except requests.RequestException:
            return False
        finally:
            _close_quietly(session)

    def wait_until_healthy(
        self,
        site: SiteConfig,
        *,
        previous_version: str,
        cancel_event: threading.Event | None = None,
        progress: Callable[[UpgradeProgress], None] | None = None,
    ) -> tuple[bool, str]:
        """Poll ``GET site_url/`` (2xx/3xx) and fresh version check until healthy or timeout."""
        name = _site_name(site)
        timeout_s = max(0.0, float(self._settings.upgrade.health_timeout_s))
        interval_s = max(0.01, float(self._settings.upgrade.health_interval_s))

        start = self._monotonic()
        elapsed = 0.0
        attempt = 0

        while True:
            if cancel_event is not None and cancel_event.is_set():
                return False, ""

            attempt += 1
            if progress is not None:
                progress(
                    UpgradeProgress(
                        site_name=name,
                        stage="verifying",
                        message=f"正在检测站点恢复状态（第 {attempt} 次）...",
                    )
                )

            if self._probe_http_home(site):
                info = self.check_version(site)
                if info.reachable:
                    return True, info.current_version

            now = self._monotonic()
            elapsed = max(elapsed, now - start)
            remaining = timeout_s - elapsed
            if remaining <= 0:
                return False, ""

            step = min(interval_s, remaining)
            before = self._monotonic()
            self._sleep(step)
            after = self._monotonic()
            if after <= before:
                elapsed += step
            else:
                elapsed = max(elapsed, after - start)

    def upgrade_site(
        self,
        site: SiteConfig,
        *,
        confirmed: bool,
        target_version: str = "",
        progress: Callable[[UpgradeProgress], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> UpgradeResult:
        """Upgrade a single site after explicit user confirmation."""
        if confirmed is not True:
            raise ValueError("升级需要用户确认")

        name = _site_name(site)
        sid = _site_id(site)
        started = self._monotonic()
        redactor = SecretRedactor.from_sites([site])

        def _finish(
            status: str,
            *,
            from_version: str = "",
            to_version: str = "",
            message: str = "",
            hint: str = "",
        ) -> UpgradeResult:
            duration_s = max(0.0, self._monotonic() - started)
            safe_msg = redactor(message)
            safe_hint = redactor(hint)
            if progress is not None:
                progress(UpgradeProgress(site_name=name, stage="done", message=safe_msg))
            return UpgradeResult(
                site_name=name,
                site_id=sid,
                status=status,
                from_version=from_version,
                to_version=to_version,
                message=safe_msg,
                hint=safe_hint,
                duration_s=duration_s,
            )

        if cancel_event is not None and cancel_event.is_set():
            return _finish(UPGRADE_CANCELLED, message="已取消")

        base_url = _site_url(site)
        if not base_url:
            return _finish(
                UPGRADE_UNREACHABLE,
                message="缺少站点网址",
                hint="请先在站点配置中填写站点网址",
            )

        current_version = ""
        latest_version = ""
        upgrade_available: bool | None = None
        desired_version = target_version.strip()

        if progress is not None:
            progress(UpgradeProgress(site_name=name, stage="connecting", message="正在连接站点..."))

        client: McpClientLike | None = None
        init_result: Any = None
        used_rest_fallback = False

        try:
            client = self._open_mcp_client(site)
            init_result = client.initialize()
        except McpAuthError as exc:
            _close_quietly(client)
            return _finish(
                UPGRADE_AUTH_FAILED,
                message=str(exc) or "MCP 鉴权失败",
                hint=exc.hint or AUTH_HINT,
            )
        except (McpUnavailableError, McpProtocolError) as exc:
            _close_quietly(client)
            client = None
            if not self._settings.upgrade.rest_upgrade_paths:
                default_hint = _UNREACHABLE_HINT if isinstance(exc, McpUnavailableError) else ""
                return _finish(
                    UPGRADE_UNREACHABLE,
                    message=str(exc) or "站点不可达",
                    hint=exc.hint or default_hint,
                )
            used_rest_fallback = True
        except McpError as exc:
            _close_quietly(client)
            return _finish(
                UPGRADE_FAILED,
                message=str(exc) or "MCP 连接失败",
                hint=exc.hint,
            )
        except Exception as exc:
            _close_quietly(client)
            return _finish(
                UPGRADE_UNREACHABLE,
                message=str(exc) or "连接站点失败",
                hint=_UNREACHABLE_HINT,
            )

        if used_rest_fallback:
            if cancel_event is not None and cancel_event.is_set():
                return _finish(UPGRADE_CANCELLED, message="已取消")
            if progress is not None:
                progress(UpgradeProgress(site_name=name, stage="checking", message="正在通过备用接口检测版本..."))
            rest_info = self._check_version_rest(site, redactor)
            if rest_info is not None:
                if not rest_info.reachable and rest_info.hint == AUTH_HINT:
                    return _finish(UPGRADE_AUTH_FAILED, message=rest_info.message, hint=rest_info.hint)
                current_version = rest_info.current_version
                latest_version = rest_info.latest_version
                upgrade_available = rest_info.upgrade_available
                if not desired_version:
                    desired_version = latest_version
            if self._is_already_latest(current_version, latest_version, upgrade_available, target_version.strip()):
                return _finish(
                    UPGRADE_ALREADY_LATEST,
                    from_version=current_version,
                    to_version=current_version or latest_version,
                    message="已是最新版本，无需升级",
                )
            if cancel_event is not None and cancel_event.is_set():
                return _finish(UPGRADE_CANCELLED, from_version=current_version, message="已取消")
            if progress is not None:
                progress(UpgradeProgress(site_name=name, stage="upgrading", message="正在通过备用接口触发升级..."))
            outcome, msg, hint = self._trigger_upgrade_rest(site, desired_version, redactor)
            if outcome == "auth_failed":
                return _finish(UPGRADE_AUTH_FAILED, from_version=current_version, message=msg, hint=hint)
            if outcome == "scope_disabled":
                return _finish(UPGRADE_SCOPE_DISABLED, from_version=current_version, message=msg, hint=hint)
            if outcome == "unreachable":
                return _finish(UPGRADE_UNREACHABLE, from_version=current_version, message=msg, hint=hint)
            if outcome != "triggered":
                return _finish(UPGRADE_FAILED, from_version=current_version, message=msg, hint=hint)
        else:
            assert client is not None
            try:
                if cancel_event is not None and cancel_event.is_set():
                    return _finish(UPGRADE_CANCELLED, message="已取消")

                if progress is not None:
                    progress(UpgradeProgress(site_name=name, stage="checking", message="正在检查当前版本..."))

                check_tools = self._find_check_tools(client)
                upgrade_tools = self._find_upgrade_tools(client)

                if check_tools:
                    check_tool = check_tools[0]
                    check_args = _build_check_arguments(check_tool)
                    check_result = client.call_tool(check_tool.name, check_args)
                    if check_result.is_error:
                        if classify_scope_error(check_result.text):
                            return _finish(
                                UPGRADE_SCOPE_DISABLED,
                                message=check_result.text or "接口开放范围未勾选",
                                hint=scope_hint("upgrade"),
                            )
                        return _finish(
                            UPGRADE_FAILED,
                            message=check_result.text or "版本检测工具返回错误",
                        )
                    current_version, latest_version, upgrade_available = parse_version_payload(check_result)
                    if not current_version and (latest_version or upgrade_available is not None):
                        current_version, _ = self._read_local_version(client, init_result)
                    if not desired_version:
                        desired_version = latest_version
                else:
                    current_version, _ = self._read_local_version(client, init_result)

                if self._is_already_latest(current_version, latest_version, upgrade_available, target_version.strip()):
                    return _finish(
                        UPGRADE_ALREADY_LATEST,
                        from_version=current_version,
                        to_version=current_version or latest_version,
                        message="已是最新版本，无需升级",
                    )

                if not upgrade_tools:
                    if self._has_disabled_upgrade_intent(client):
                        return _finish(
                            UPGRADE_SCOPE_DISABLED,
                            from_version=current_version,
                            message="站点未开启 siteops_upgrade 意图（系统升级未在接口开放范围中开启）",
                            hint="请登录该站后台：设置 → AI 统一配置 → 接口开放范围/开放意图，开启「siteops_upgrade / 系统升级」后重试",
                        )
                    return _finish(
                        UPGRADE_NO_TOOL,
                        from_version=current_version,
                        message="该站未通过 MCP 暴露升级工具",
                        hint=_NO_UPGRADE_TOOL_HINT,
                    )

                if cancel_event is not None and cancel_event.is_set():
                    return _finish(UPGRADE_CANCELLED, from_version=current_version, message="已取消")

                upgrade_tool = upgrade_tools[0]
                if progress is not None:
                    progress(
                        UpgradeProgress(
                            site_name=name,
                            stage="upgrading",
                            message=f"正在执行升级（{upgrade_tool.name}）...",
                        )
                    )
                arguments = _build_upgrade_arguments(
                    upgrade_tool,
                    desired_version,
                    confirmed=True,
                )
                call_kwargs: dict[str, Any] = {
                    "timeout": self._settings.upgrade.upgrade_call_timeout_s,
                }
                if _supports_auto_confirm(client):
                    call_kwargs["auto_confirm"] = True
                try:
                    upgrade_result = client.call_tool(
                        upgrade_tool.name,
                        arguments,
                        **call_kwargs,
                    )
                except McpApprovalRequiredError as exc:
                    return _finish(
                        UPGRADE_FAILED,
                        from_version=current_version,
                        message=str(exc) or "升级操作需要二次确认，未能自动确认",
                        hint=_approval_failure_hint(
                            confirm_tool=exc.confirm_tool,
                            exc_hint=exc.hint,
                        ),
                    )
                except McpScopeError as exc:
                    return _finish(
                        UPGRADE_SCOPE_DISABLED,
                        from_version=current_version,
                        message=str(exc) or "系统升级接口未开放",
                        hint=scope_hint("upgrade"),
                    )
                except McpAuthError as exc:
                    return _finish(
                        UPGRADE_AUTH_FAILED,
                        from_version=current_version,
                        message=str(exc) or "MCP 鉴权失败",
                        hint=exc.hint or AUTH_HINT,
                    )
                except McpToolError as exc:
                    err_text = str(exc)
                    if exc.result and exc.result.text:
                        err_text = f"{err_text} {exc.result.text}"
                    if classify_scope_error(err_text):
                        return _finish(
                            UPGRADE_SCOPE_DISABLED,
                            from_version=current_version,
                            message=str(exc) or "系统升级接口未开放",
                            hint=scope_hint("upgrade"),
                        )
                    return _finish(
                        UPGRADE_FAILED,
                        from_version=current_version,
                        message=str(exc) or "升级工具执行失败",
                        hint=exc.hint,
                    )
                except Exception as exc:
                    if not _is_restart_interruption(exc):
                        hint = exc.hint if isinstance(exc, McpError) else ""
                        return _finish(
                            UPGRADE_FAILED,
                            from_version=current_version,
                            message=str(exc) or "升级请求失败",
                            hint=hint,
                        )
                    # Timeout/connection drop during upgrade call likely means restart started.
                else:
                    approval_req, _, approval_confirm_tool = detect_approval_required(upgrade_result)
                    if approval_req:
                        return _finish(
                            UPGRADE_FAILED,
                            from_version=current_version,
                            message=upgrade_result.text.strip() or "升级操作需要二次确认，未能自动确认",
                            hint=_approval_failure_hint(confirm_tool=approval_confirm_tool),
                        )
                    if upgrade_result.is_error:
                        if classify_scope_error(upgrade_result.text):
                            return _finish(
                                UPGRADE_SCOPE_DISABLED,
                                from_version=current_version,
                                message=upgrade_result.text or "系统升级接口未开放",
                                hint=scope_hint("upgrade"),
                            )
                        return _finish(
                            UPGRADE_FAILED,
                            from_version=current_version,
                            message=upgrade_result.text or "升级工具返回错误",
                        )
            except McpApprovalRequiredError as exc:
                return _finish(
                    UPGRADE_FAILED,
                    from_version=current_version,
                    message=str(exc) or "操作需要二次确认，未能自动确认",
                    hint=_approval_failure_hint(
                        confirm_tool=exc.confirm_tool,
                        exc_hint=exc.hint,
                    ),
                )
            except McpAuthError as exc:
                return _finish(
                    UPGRADE_AUTH_FAILED,
                    from_version=current_version,
                    message=str(exc) or "MCP 鉴权失败",
                    hint=exc.hint or AUTH_HINT,
                )
            except McpScopeError as exc:
                return _finish(
                    UPGRADE_SCOPE_DISABLED,
                    from_version=current_version,
                    message=str(exc) or "接口开放范围未勾选",
                    hint=scope_hint("upgrade"),
                )
            except McpToolError as exc:
                err_text = str(exc)
                if exc.result and exc.result.text:
                    err_text = f"{err_text} {exc.result.text}"
                if classify_scope_error(err_text):
                    return _finish(
                        UPGRADE_SCOPE_DISABLED,
                        from_version=current_version,
                        message=str(exc) or "接口开放范围未勾选",
                        hint=scope_hint("upgrade"),
                    )
                return _finish(
                    UPGRADE_FAILED,
                    from_version=current_version,
                    message=str(exc) or "工具调用失败",
                    hint=exc.hint,
                )
            except McpUnavailableError as exc:
                return _finish(
                    UPGRADE_UNREACHABLE,
                    from_version=current_version,
                    message=str(exc) or "站点不可达",
                    hint=exc.hint or _UNREACHABLE_HINT,
                )
            except Exception as exc:
                return _finish(
                    UPGRADE_FAILED,
                    from_version=current_version,
                    message=str(exc) or "升级过程发生异常",
                )
            finally:
                _close_quietly(client)

        # Stage: restarting
        if cancel_event is not None and cancel_event.is_set():
            return _finish(
                UPGRADE_CANCELLED,
                from_version=current_version,
                message="已取消（升级指令已发送，站点可能仍在重启）",
            )

        restart_wait_s = max(0.0, float(self._settings.upgrade.restart_wait_s))
        if progress is not None:
            progress(
                UpgradeProgress(
                    site_name=name,
                    stage="restarting",
                    message=f"等待站点重启（{int(restart_wait_s)} 秒）...",
                )
            )
        if restart_wait_s > 0:
            self._sleep(restart_wait_s)

        if cancel_event is not None and cancel_event.is_set():
            return _finish(
                UPGRADE_CANCELLED,
                from_version=current_version,
                message="已取消（站点可能仍在重启）",
            )

        # Stage: verifying
        healthy, verified_version = self.wait_until_healthy(
            site,
            previous_version=current_version,
            cancel_event=cancel_event,
            progress=progress,
        )

        if cancel_event is not None and cancel_event.is_set() and not healthy:
            return _finish(
                UPGRADE_CANCELLED,
                from_version=current_version,
                message="已取消等待站点恢复",
            )

        if not healthy:
            timeout_int = int(round(self._settings.upgrade.health_timeout_s))
            return _finish(
                UPGRADE_TIMEOUT,
                from_version=current_version,
                message=f"等待站点恢复超时（{timeout_int} 秒）",
                hint=f"站点未在 {timeout_int} 秒内恢复，请检查服务器进程/守护",
            )

        if self._verify_version_upgraded(current_version, verified_version, desired_version):
            to_ver = verified_version or desired_version
            msg = (
                f"升级成功：{current_version} → {to_ver}"
                if current_version and to_ver
                else (f"升级成功（当前版本：{to_ver}）" if to_ver else "升级完成，站点已恢复响应")
            )
            return _finish(
                UPGRADE_UPGRADED,
                from_version=current_version,
                to_version=to_ver,
                message=msg,
            )

        return _finish(
            UPGRADE_FAILED,
            from_version=current_version,
            to_version=verified_version,
            message="升级后版本未变化",
            hint="站点已恢复访问但版本号未更新，请登录后台查看升级日志",
        )

    @staticmethod
    def _is_already_latest(
        current_version: str,
        latest_version: str,
        upgrade_available: bool | None,
        explicit_target: str,
    ) -> bool:
        if explicit_target:
            if (
                current_version
                and _parse_version(current_version) is not None
                and _parse_version(explicit_target) is not None
                and compare_versions(current_version, explicit_target) >= 0
            ):
                return True
            return False

        if (
            current_version
            and latest_version
            and _parse_version(current_version) is not None
            and _parse_version(latest_version) is not None
        ):
            return compare_versions(current_version, latest_version) >= 0
        return upgrade_available is False

    @staticmethod
    def _verify_version_upgraded(
        previous_version: str,
        verified_version: str,
        desired_version: str,
    ) -> bool:
        if previous_version and verified_version:
            if _parse_version(previous_version) is not None and _parse_version(verified_version) is not None:
                if compare_versions(previous_version, verified_version) < 0:
                    return True
                return False
            return verified_version != previous_version

        if verified_version:
            if desired_version and _parse_version(desired_version) is not None and _parse_version(verified_version) is not None:
                return compare_versions(verified_version, desired_version) >= 0
            return True

        # No version could be read after restart: only treat as success when we had no baseline either.
        return not previous_version

    def upgrade_sites(
        self,
        sites: Iterable[SiteConfig],
        *,
        confirmed: bool,
        max_workers: int | None = None,
        canary_first: bool = True,
        stop_on_failure: bool = True,
        progress: Callable[[UpgradeProgress], None] | None = None,
        result_callback: Callable[[UpgradeResult], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> list[UpgradeResult]:
        """Run staged upgrades across sites with canary, concurrency cap 1–5 and stop-on-failure."""
        if confirmed is not True:
            raise ValueError("升级需要用户确认")

        site_list = list(sites)
        if not site_list:
            return []

        results: list[UpgradeResult | None] = [None] * len(site_list)
        callback_lock = threading.Lock()

        def _record(index: int, result: UpgradeResult) -> None:
            results[index] = result
            if result_callback is not None:
                with callback_lock:
                    result_callback(result)

        def _mark_remaining(
            indices: Iterable[int],
            status: str,
            message: str,
            hint: str = "",
        ) -> None:
            for idx in indices:
                site = site_list[idx]
                name = _site_name(site)
                if progress is not None:
                    progress(UpgradeProgress(site_name=name, stage="done", message=message))
                _record(
                    idx,
                    UpgradeResult(
                        site_name=name,
                        site_id=_site_id(site),
                        status=status,
                        message=message,
                        hint=hint,
                    ),
                )

        remaining_indices = list(range(len(site_list)))

        if canary_first:
            canary_site = site_list[0]
            canary_result = self.upgrade_site(
                canary_site,
                confirmed=True,
                progress=progress,
                cancel_event=cancel_event,
            )
            _record(0, canary_result)
            remaining_indices = remaining_indices[1:]

            if not remaining_indices:
                return [canary_result]

            if cancel_event is not None and cancel_event.is_set():
                _mark_remaining(remaining_indices, UPGRADE_CANCELLED, "已取消")
                return [r for r in results if r is not None]

            if stop_on_failure and canary_result.status not in _SUCCESS_STATUSES:
                skip_msg = f"首个验证站点 {_site_name(canary_site)} 升级未成功，已跳过"
                _mark_remaining(
                    remaining_indices,
                    UPGRADE_SKIPPED,
                    skip_msg,
                    hint="请先处理首个站点的问题后再重试批量升级",
                )
                return [r for r in results if r is not None]

        workers = self._clamp_concurrency(max_workers, len(remaining_indices))
        failure_stop = threading.Event()
        failure_lock = threading.Lock()
        failed_site_holder: list[str] = []

        def _run_one(index: int) -> None:
            site = site_list[index]
            name = _site_name(site)

            if cancel_event is not None and cancel_event.is_set():
                if progress is not None:
                    progress(UpgradeProgress(site_name=name, stage="done", message="已取消"))
                _record(
                    index,
                    UpgradeResult(
                        site_name=name,
                        site_id=_site_id(site),
                        status=UPGRADE_CANCELLED,
                        message="已取消",
                    ),
                )
                return

            if stop_on_failure and failure_stop.is_set():
                with failure_lock:
                    culprit = failed_site_holder[0] if failed_site_holder else "前序站点"
                skip_msg = f"站点 {culprit} 升级未成功，已跳过"
                if progress is not None:
                    progress(UpgradeProgress(site_name=name, stage="done", message=skip_msg))
                _record(
                    index,
                    UpgradeResult(
                        site_name=name,
                        site_id=_site_id(site),
                        status=UPGRADE_SKIPPED,
                        message=skip_msg,
                        hint="已开启“失败即停止后续”，请先排查失败站点",
                    ),
                )
                return

            result = self.upgrade_site(
                site,
                confirmed=True,
                progress=progress,
                cancel_event=cancel_event,
            )
            if (
                stop_on_failure
                and result.status not in _SUCCESS_STATUSES
                and result.status != UPGRADE_CANCELLED
            ):
                with failure_lock:
                    if not failed_site_holder:
                        failed_site_holder.append(name)
                failure_stop.set()
            _record(index, result)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_run_one, idx) for idx in remaining_indices]
            for future in futures:
                future.result()

        return [r for r in results if r is not None]
