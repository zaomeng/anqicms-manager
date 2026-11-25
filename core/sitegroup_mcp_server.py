"""MCP (Model Context Protocol) JSON-RPC 2.0 server for AnqiCMS site-group management.

Supports both:
- Direct JSON-RPC dispatch via :meth:`SiteGroupMcpServer.handle_jsonrpc` (for
  mounting at ``POST /mcp`` in the web dashboard server).
- Newline-delimited stdio transport via :meth:`SiteGroupMcpServer.serve_stdio`
  (for standalone CLI usage via ``mcp_server.py``).

Pure Python stdlib + existing ``core``/``models`` modules; zero Tk and zero
extra third-party dependencies.
"""

from __future__ import annotations

import inspect
import json
import sys
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from core import config_service as default_config_service
from core.admin_jump_service import AdminJumpService
from core.anqicms_mcp_client import AnqiMcpClient, probe_site
from core.app_info import APP_NAME, APP_VERSION
from core.friendlink_ledger import FriendLinkLedger, ledger_path
from core.friendlink_service import (
    FriendLinkService,
    export_sites_html_links,
    link_key,
    parse_batch_link_input,
)
from core.integration_settings import (
    IntegrationSettings,
    load_integration_settings,
    settings_path,
)
from core.mcp_contracts import McpClientFactory
from core.redaction import SecretRedactor, redact_text
from core.runtime_paths import app_dir
from core.site_credentials import (
    admin_url,
    display_name,
    effective_mcp_token,
    has_admin_credentials,
    mask_secret,
)
from core.site_factory import ensure_site_files
from core.site_onboarding import (
    SiteDiscoveryResult,
    auto_discover_sites,
    parse_bulk_new_sites,
)
from core.sitegroup_db import SiteGroupDB, db_path
from core.upgrade_service import UpgradeService
from models.entities import SiteConfig
from models.sitegroup import (
    CROSSLINK_HUB,
    CROSSLINK_MESH,
    CROSSLINK_RING,
    JUMP_TIER_BRIDGE,
    JUMP_TIER_DIRECT,
    JUMP_TIER_TICKET,
    LINK_CANCELLED,
    LINK_CREATED,
    LINK_DELETED,
    LINK_FAILED,
    LINK_NOT_FOUND,
    LINK_PLANNED,
    LINK_SKIPPED_EXISTS,
    LINK_SKIPPED_SELF,
    UPGRADE_ALREADY_LATEST,
    UPGRADE_CANCELLED,
    UPGRADE_FAILED,
    UPGRADE_SKIPPED,
    UPGRADE_TIMEOUT,
    UPGRADE_UPGRADED,
    CrossLinkItem,
    LinkOpResult,
    McpProbeResult,
    SiteVersionInfo,
    UpgradeResult,
)

__all__ = [
    "DEFAULT_PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "SiteGroupMcpServer",
]

SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = (
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

_CROSSLINK_MODES: tuple[str, ...] = (
    CROSSLINK_MESH,
    CROSSLINK_HUB,
    CROSSLINK_RING,
    "random_k",
    "group_cross",
)

_JSONRPC_PARSE_ERROR = -32700
_JSONRPC_INVALID_REQUEST = -32600
_JSONRPC_METHOD_NOT_FOUND = -32601
_JSONRPC_INVALID_PARAMS = -32602


class _ToolInputError(ValueError):
    """Raised when tool arguments fail validation."""


@dataclass(frozen=True, slots=True)
class _ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any]
    handler: Callable[[dict[str, Any]], dict[str, Any]]


def _resolve_default_config_path() -> Path:
    local = app_dir() / "config.ini"
    if local.exists():
        return local.resolve()
    return Path("config.ini").resolve()


def _is_valid_jsonrpc_id(value: Any) -> bool:
    return value is None or (isinstance(value, (str, int, float)) and not isinstance(value, bool))


def _jsonrpc_result(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(
    req_id: Any,
    code: int,
    message: str,
    data: Any = None,
) -> dict[str, Any]:
    error_obj: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error_obj["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": error_obj}


def _tool_response(
    text: str,
    structured: dict[str, Any],
    *,
    is_error: bool = False,
) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": structured,
        "isError": is_error,
    }


def _tool_error(
    message: str,
    *,
    extra: dict[str, Any] | None = None,
    redactor: SecretRedactor | None = None,
) -> dict[str, Any]:
    safe_message = redactor(message) if redactor is not None else redact_text(message)
    payload: dict[str, Any] = {"ok": False, "error": safe_message}
    if extra:
        for key, value in extra.items():
            if isinstance(value, str):
                payload[key] = redactor(value) if redactor is not None else redact_text(value)
            else:
                payload[key] = value
    return _tool_response(safe_message, payload, is_error=True)


def _opt_str(args: Mapping[str, Any], key: str, default: str = "", *, strip: bool = True) -> str:
    if key not in args or args[key] is None:
        return default
    value = args[key]
    if not isinstance(value, str):
        raise _ToolInputError(f"参数 {key} 必须是字符串")
    return value.strip() if strip else value


def _req_str(
    args: Mapping[str, Any],
    key: str,
    *,
    allow_empty: bool = False,
    strip: bool = True,
) -> str:
    if key not in args or args[key] is None:
        raise _ToolInputError(f"缺少必填参数：{key}")
    value = args[key]
    if not isinstance(value, str):
        raise _ToolInputError(f"参数 {key} 必须是字符串")
    cleaned = value.strip() if strip else value
    if not allow_empty and not cleaned.strip():
        raise _ToolInputError(f"参数 {key} 不能为空")
    return cleaned


def _opt_bool(args: Mapping[str, Any], key: str, default: bool = False) -> bool:
    if key not in args or args[key] is None:
        return default
    value = args[key]
    if not isinstance(value, bool):
        raise _ToolInputError(f"参数 {key} 必须是布尔值")
    return value


def _opt_int(
    args: Mapping[str, Any],
    key: str,
    default: int | None = None,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    if key not in args or args[key] is None:
        return default
    value = args[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise _ToolInputError(f"参数 {key} 必须是整数")
    if minimum is not None and value < minimum:
        raise _ToolInputError(f"参数 {key} 不能小于 {minimum}")
    if maximum is not None and value > maximum:
        raise _ToolInputError(f"参数 {key} 不能大于 {maximum}")
    return value


def _opt_str_list(args: Mapping[str, Any], key: str) -> list[str] | None:
    if key not in args or args[key] is None:
        return None
    value = args[key]
    if not isinstance(value, list):
        raise _ToolInputError(f"参数 {key} 必须是字符串数组")
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise _ToolInputError(f"参数 {key} 的元素必须都是字符串")
        text = item.strip()
        if text:
            cleaned.append(text)
    return cleaned


def _accepts_arg_count(func: Callable[..., Any]) -> tuple[bool, int]:
    """Return ``(has_varargs, max_positional_args)`` for dispatching injected methods."""
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return True, 0
    positional = 0
    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_POSITIONAL:
            return True, positional
        if param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional += 1
    return False, positional


def _select_sites(
    all_sites: Sequence[SiteConfig],
    *,
    site_names: list[str] | None = None,
    group: str = "",
    search: str = "",
    enabled_only: bool = False,
) -> list[SiteConfig]:
    if site_names is not None:
        if not site_names:
            raise _ToolInputError("参数 site_names 不能为空数组；如需操作全部站点请省略该参数")
        by_exact = {site.name: site for site in all_sites}
        by_folded = {site.name.casefold(): site for site in all_sites}
        selected: list[SiteConfig] = []
        seen: set[str] = set()
        missing: list[str] = []
        for raw_name in site_names:
            site = by_exact.get(raw_name) or by_folded.get(raw_name.casefold())
            if site is None:
                missing.append(raw_name)
                continue
            if site.name not in seen:
                seen.add(site.name)
                selected.append(site)
        if missing:
            raise _ToolInputError(f"未找到指定站点：{', '.join(missing)}")
    else:
        selected = list(all_sites)

    if group:
        selected = [
            site
            for site in selected
            if (getattr(site, "site_group", "") or "").strip() == group
        ]
    if enabled_only:
        selected = [site for site in selected if bool(getattr(site, "enabled", False))]
    if search:
        needle = search.casefold()
        selected = [
            site
            for site in selected
            if any(
                needle in (getattr(site, attr, "") or "").casefold()
                for attr in ("name", "site_title", "url", "site_group", "category_name")
            )
        ]
    return selected


def _parse_tools_json(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw if item]
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
        except ValueError:
            return []
        if isinstance(decoded, list):
            return [str(item) for item in decoded if item]
    return []


def _extract_remove_urls(links_text: str) -> tuple[list[str], list[str]]:
    """Extract target link URLs from HTML ``<a>`` tags, delimited lines, or bare URLs."""
    specs, errors = parse_batch_link_input(links_text)
    urls = [spec.link for spec in specs]
    seen_keys = {link_key(u) or u.casefold() for u in urls}

    if specs or not errors:
        return urls, errors

    # Allow bare host/domain tokens when batch removal input omits http://
    remaining_errors: list[str] = []
    normalized_text = links_text.replace("\r\n", "\n").replace("\r", "\n")
    for line_no, raw_line in enumerate(normalized_text.split("\n"), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not any(ch.isspace() for ch in line) and link_key(line):
            key = link_key(line)
            if key not in seen_keys:
                seen_keys.add(key)
                urls.append(line)
        else:
            err = f"第{line_no}行：网址无效"
            if err in errors:
                remaining_errors.append(err)

    return urls, remaining_errors if remaining_errors else ([] if urls else errors)


def _summarize_op_results(results: Sequence[LinkOpResult]) -> dict[str, int]:
    counts: dict[str, int] = {
        "total": len(results),
        LINK_CREATED: 0,
        LINK_DELETED: 0,
        LINK_SKIPPED_EXISTS: 0,
        LINK_SKIPPED_SELF: 0,
        LINK_NOT_FOUND: 0,
        LINK_FAILED: 0,
        LINK_CANCELLED: 0,
    }
    for res in results:
        counts[res.status] = counts.get(res.status, 0) + 1
    return {k: v for k, v in counts.items() if k == "total" or v > 0}


def _serialize_op_result(res: LinkOpResult, redactor: SecretRedactor) -> dict[str, Any]:
    return {
        "site_name": res.site_name,
        "site_id": res.site_id,
        "action": res.action,
        "link": redactor(res.link),
        "title": res.title,
        "status": res.status,
        "message": redactor(res.message),
    }


def _serialize_crosslink_item(item: CrossLinkItem, redactor: SecretRedactor) -> dict[str, Any]:
    return {
        "source_site": item.source_name,
        "source_site_id": item.source_site_id,
        "target_site": item.target_name,
        "target_site_id": item.target_site_id,
        "title": item.spec.title,
        "link": redactor(item.spec.link),
        "nofollow": bool(item.spec.nofollow),
        "remark": item.spec.remark,
        "state": item.state,
        "message": redactor(item.message),
    }


def _serialize_version_info(info: SiteVersionInfo, redactor: SecretRedactor) -> dict[str, Any]:
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
        "message": redactor(info.message),
        "hint": redactor(info.hint),
    }


def _serialize_upgrade_result(res: UpgradeResult, redactor: SecretRedactor) -> dict[str, Any]:
    return {
        "site_name": res.site_name,
        "site_id": res.site_id,
        "status": res.status,
        "from_version": res.from_version,
        "to_version": res.to_version,
        "message": redactor(res.message),
        "hint": redactor(res.hint),
        "duration_s": round(float(res.duration_s), 3),
    }


def _default_jump_reason(tier: str) -> str:
    if tier == JUMP_TIER_TICKET:
        return "已通过 MCP 一次性登录票据生成跳转地址"
    if tier == JUMP_TIER_BRIDGE:
        return "已通过后台静默登录生成本地桥接跳转地址"
    if tier == JUMP_TIER_DIRECT:
        return "直接访问站点后台登录页"
    return ""


class SiteGroupMcpServer:
    """MCP JSON-RPC 2.0 server exposing AnqiCMS site-group tools."""

    def __init__(
        self,
        config_path: Path | str | None = None,
        *,
        config_service=None,
        settings: IntegrationSettings | None = None,
        db: SiteGroupDB | None = None,
        friendlink_service: FriendLinkService | None = None,
        ledger: FriendLinkLedger | None = None,
        upgrade_service: UpgradeService | None = None,
        jump_service: AdminJumpService | None = None,
        proxy=None,
        mcp_client_factory: McpClientFactory | None = None,
    ) -> None:
        self.config_path = (
            Path(config_path).resolve()
            if config_path is not None
            else _resolve_default_config_path()
        )
        self._config_service = config_service
        self._settings = settings
        self._db = db
        self._owns_db = False
        self._friendlink_service = friendlink_service
        self._ledger = ledger
        self._upgrade_service = upgrade_service
        self._jump_service = jump_service
        self._owns_jump_service = False
        self._proxy = proxy
        self._mcp_client_factory = mcp_client_factory

        self._init_lock = threading.RLock()
        self._client_initialized = False
        self._tools = self._build_tool_specs()
        self._tools_by_name = {spec.name: spec for spec in self._tools}

    # ------------------------------------------------------------------ #
    # Lifecycle & service resolution
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        """Release owned SQLite and loopback bridge resources (idempotent)."""
        with self._init_lock:
            db = self._db if self._owns_db else None
            jump = self._jump_service if self._owns_jump_service else None
            if self._owns_db:
                self._db = None
                self._owns_db = False
            if self._owns_jump_service:
                self._jump_service = None
                self._owns_jump_service = False

        if jump is not None:
            try:
                jump.shutdown()
            except Exception:
                pass
        if db is not None:
            try:
                db.close()
            except Exception:
                pass

    def __enter__(self) -> SiteGroupMcpServer:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _get_settings(self) -> IntegrationSettings:
        with self._init_lock:
            if self._settings is None:
                self._settings = load_integration_settings(settings_path(self.config_path))
            return self._settings

    def _get_mcp_client_factory(self) -> McpClientFactory:
        with self._init_lock:
            if self._mcp_client_factory is None:
                cfg = self._get_settings()
                self._mcp_client_factory = lambda site: AnqiMcpClient.for_site(site, settings=cfg)
            return self._mcp_client_factory

    def _get_db(self) -> SiteGroupDB:
        with self._init_lock:
            if self._db is None:
                self._db = SiteGroupDB(db_path(self.config_path))
                self._owns_db = True
            return self._db

    def _get_ledger(self) -> FriendLinkLedger:
        with self._init_lock:
            if self._ledger is None:
                self._ledger = FriendLinkLedger(ledger_path(self.config_path))
            return self._ledger

    def _get_friendlink_service(self) -> FriendLinkService:
        with self._init_lock:
            if self._friendlink_service is None:
                self._friendlink_service = FriendLinkService(
                    self._get_settings(),
                    mcp_client_factory=self._get_mcp_client_factory(),
                )
            return self._friendlink_service

    def _get_upgrade_service(self) -> UpgradeService:
        with self._init_lock:
            if self._upgrade_service is None:
                self._upgrade_service = UpgradeService(
                    self._get_settings(),
                    mcp_client_factory=self._get_mcp_client_factory(),
                )
            return self._upgrade_service

    def _get_jump_service(self) -> AdminJumpService:
        with self._init_lock:
            if self._jump_service is None:
                self._jump_service = AdminJumpService(
                    self._get_settings(),
                    mcp_client_factory=self._get_mcp_client_factory(),
                    proxy=self._proxy,
                    use_local_proxy=False,
                )
                self._owns_jump_service = True
            return self._jump_service

    def _load_sites(self) -> list[SiteConfig]:
        if self._config_service is None:
            return list(default_config_service.load_config(self.config_path))
        for attr in ("load_config", "load", "list_sites", "get_sites"):
            fn = getattr(self._config_service, attr, None)
            if callable(fn):
                has_varargs, pos_count = _accepts_arg_count(fn)
                if pos_count >= 1 or has_varargs:
                    return list(fn(self.config_path))
                return list(fn())
        raise RuntimeError("注入的 config_service 缺少 load_config/load 方法")

    def _save_sites(self, sites: list[SiteConfig]) -> None:
        if self._config_service is None:
            default_config_service.save_config(self.config_path, sites)
            return
        for attr in ("save_config", "save", "write_config"):
            fn = getattr(self._config_service, attr, None)
            if callable(fn):
                has_varargs, pos_count = _accepts_arg_count(fn)
                if pos_count >= 2 or has_varargs:
                    fn(self.config_path, sites)
                    return
                fn(sites)
                return
        raise RuntimeError("注入的 config_service 缺少 save_config/save 方法")

    def _safe_record_operation(
        self,
        category: str,
        action: str,
        *,
        site_name: str = "",
        status: str = "ok",
        summary: str = "",
        detail: Any = None,
        redactor: SecretRedactor | None = None,
    ) -> None:
        try:
            db = self._get_db()
            safe_summary = redactor(summary) if redactor is not None else redact_text(summary)
            db.record_operation(
                category,
                action,
                site_name=site_name,
                status=status,
                summary=safe_summary,
                detail=detail,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # JSON-RPC 2.0 & stdio transport
    # ------------------------------------------------------------------ #
    def list_tools(self) -> list[dict[str, Any]]:
        """Return MCP ``tools/list`` definitions."""
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "inputSchema": spec.input_schema,
                "annotations": spec.annotations,
            }
            for spec in self._tools
        ]

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a registered tool directly and return an MCP ``tools/call`` result."""
        spec = self._tools_by_name.get(name)
        if spec is None:
            return _tool_error(f"未知工具：{name}")
        args = dict(arguments) if isinstance(arguments, Mapping) else {}
        return self._dispatch_tool(spec, args)

    def handle_jsonrpc(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Handle a single JSON-RPC 2.0 message; return ``None`` for notifications."""
        if not isinstance(payload, dict):
            return _jsonrpc_error(None, _JSONRPC_INVALID_REQUEST, "Invalid Request: payload must be a JSON object")

        has_id = "id" in payload
        raw_id = payload.get("id")
        req_id = raw_id if _is_valid_jsonrpc_id(raw_id) else None
        method = payload.get("method")

        if payload.get("jsonrpc") != "2.0":
            if not has_id and isinstance(method, str) and method.startswith("notifications/"):
                return None
            return _jsonrpc_error(req_id, _JSONRPC_INVALID_REQUEST, "Invalid Request: jsonrpc must be '2.0'")

        if not isinstance(method, str) or not method.strip():
            return _jsonrpc_error(req_id, _JSONRPC_INVALID_REQUEST, "Invalid Request: missing method")

        method = method.strip()

        # JSON-RPC 2.0 notifications have no "id" and must never receive a response.
        if not has_id or (method.startswith("notifications/") and raw_id is None):
            if method == "notifications/initialized":
                self._client_initialized = True
            return None

        if method == "initialize":
            params = payload.get("params")
            if params is not None and not isinstance(params, dict):
                return _jsonrpc_error(req_id, _JSONRPC_INVALID_PARAMS, "Invalid params: params must be an object")
            requested_version = (params or {}).get("protocolVersion") if isinstance(params, dict) else None
            if isinstance(requested_version, str) and requested_version.strip() in SUPPORTED_PROTOCOL_VERSIONS:
                protocol_version = requested_version.strip()
            else:
                protocol_version = DEFAULT_PROTOCOL_VERSION
            return _jsonrpc_result(
                req_id,
                {
                    "protocolVersion": protocol_version,
                    "capabilities": {
                        "tools": {"listChanged": False},
                    },
                    "serverInfo": {
                        "name": APP_NAME,
                        "version": APP_VERSION,
                    },
                    "instructions": (
                        "AnqiCMS 站群管理工具 MCP 服务端：支持站点查询、跨站友链检索与批量上下链、"
                        "反链审计、站群互链规划/执行/回滚、版本检测与金丝雀升级、后台跳转地址解析及批量建站。"
                    ),
                },
            )

        if method == "ping":
            return _jsonrpc_result(req_id, {})

        if method == "tools/list":
            params = payload.get("params")
            if params is not None and not isinstance(params, dict):
                return _jsonrpc_error(req_id, _JSONRPC_INVALID_PARAMS, "Invalid params: params must be an object")
            return _jsonrpc_result(req_id, {"tools": self.list_tools()})

        if method == "tools/call":
            params = payload.get("params")
            if not isinstance(params, dict):
                return _jsonrpc_error(req_id, _JSONRPC_INVALID_PARAMS, "Invalid params: params must be an object")
            tool_name = params.get("name")
            if not isinstance(tool_name, str) or not tool_name.strip():
                return _jsonrpc_error(req_id, _JSONRPC_INVALID_PARAMS, "Invalid params: missing tool name")
            tool_name = tool_name.strip()
            raw_args = params.get("arguments")
            if raw_args is None:
                arguments: dict[str, Any] = {}
            elif isinstance(raw_args, dict):
                arguments = raw_args
            else:
                return _jsonrpc_error(req_id, _JSONRPC_INVALID_PARAMS, "Invalid params: arguments must be an object")
            spec = self._tools_by_name.get(tool_name)
            if spec is None:
                return _jsonrpc_error(req_id, _JSONRPC_INVALID_PARAMS, f"未知工具：{tool_name}")
            return _jsonrpc_result(req_id, self._dispatch_tool(spec, arguments))

        return _jsonrpc_error(req_id, _JSONRPC_METHOD_NOT_FOUND, f"未知方法：{method}")

    def serve_stdio(
        self,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
    ) -> None:
        """Run newline-delimited JSON-RPC 2.0 loop over ``stdin``/``stdout``."""
        in_stream = sys.stdin if stdin is None else stdin
        out_stream = sys.stdout if stdout is None else stdout

        while True:
            raw_line = in_stream.readline()
            if not raw_line:
                break
            line = raw_line.strip()
            if not line:
                continue

            try:
                decoded = json.loads(line)
            except ValueError as exc:
                self._write_stdio_message(
                    out_stream,
                    _jsonrpc_error(None, _JSONRPC_PARSE_ERROR, f"Parse error: {exc}"),
                )
                continue

            if isinstance(decoded, list):
                if not decoded:
                    self._write_stdio_message(
                        out_stream,
                        _jsonrpc_error(None, _JSONRPC_INVALID_REQUEST, "Invalid Request: empty batch"),
                    )
                    continue
                batch_responses = []
                for item in decoded:
                    resp = self.handle_jsonrpc(item)
                    if resp is not None:
                        batch_responses.append(resp)
                if batch_responses:
                    self._write_stdio_message(out_stream, batch_responses)
                continue

            response = self.handle_jsonrpc(decoded)
            if response is not None:
                self._write_stdio_message(out_stream, response)

    @staticmethod
    def _write_stdio_message(out_stream: TextIO, payload: Any) -> None:
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        out_stream.write(serialized + "\n")
        out_stream.flush()

    def _dispatch_tool(self, spec: _ToolSpec, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return spec.handler(arguments)
        except _ToolInputError as exc:
            return _tool_error(str(exc))
        except Exception as exc:  # noqa: BLE001
            try:
                redactor = SecretRedactor.from_sites(self._load_sites())
            except Exception:
                redactor = SecretRedactor()
            msg = redactor(f"执行工具 {spec.name} 失败：{exc}")
            print(f"[sitegroup-mcp] {msg}", file=sys.stderr)
            return _tool_error(msg, redactor=redactor)

    # ------------------------------------------------------------------ #
    # Tool definitions (12 tools)
    # ------------------------------------------------------------------ #
    def _build_tool_specs(self) -> tuple[_ToolSpec, ...]:
        return (
            _ToolSpec(
                name="list_sites",
                description="列出已配置的站群站点，可按分组、关键词或启用状态筛选，并合并 SiteGroupDB 缓存的版本、MCP 和友链统计信息。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "group": {"type": "string", "description": "按站点分组名过滤（精确匹配）。"},
                        "search": {"type": "string", "description": "按站点名、显示名、网址、分组或栏目名模糊搜索。"},
                        "enabled_only": {"type": "boolean", "description": "是否仅返回已启用发布任务的站点。", "default": False},
                    },
                    "additionalProperties": False,
                },
                annotations={"readOnlyHint": True, "destructiveHint": False},
                handler=self._tool_list_sites,
            ),
            _ToolSpec(
                name="search_links",
                description="跨站点实时并发检索友链，支持按根域名、链接 URL、标题或备注匹配。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "检索关键词或域名/网址；传空字符串可拉取全部友链。"},
                        "site_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "限定检索的站点名称列表；省略则检索全部站点。",
                        },
                        "group": {"type": "string", "description": "限定检索的站点分组名称。"},
                        "match_root_domain": {"type": "boolean", "description": "是否启用根域名等价匹配。", "default": True},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                annotations={"readOnlyHint": True, "destructiveHint": False},
                handler=self._tool_search_links,
            ),
            _ToolSpec(
                name="batch_add_links",
                description="向多个站点批量添加友链（支持 HTML <a> 代码、标题|URL 或多行 URL），自动去重并跳过本站域名。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "links_text": {"type": "string", "description": "友链内容，支持 HTML <a> 标签或每行 标题|URL / URL。"},
                        "site_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "目标站点名称列表；省略则作用于全部（或指定分组）站点。",
                        },
                        "group": {"type": "string", "description": "目标站点分组名称。"},
                        "skip_existing": {"type": "boolean", "description": "是否跳过站点中已存在的友链。", "default": True},
                        "nofollow": {"type": "boolean", "description": "默认是否为新增友链添加 nofollow 属性。", "default": False},
                    },
                    "required": ["links_text"],
                    "additionalProperties": False,
                },
                annotations={"readOnlyHint": False, "destructiveHint": False},
                handler=self._tool_batch_add_links,
            ),
            _ToolSpec(
                name="batch_remove_links",
                description="从多个站点批量下链（删除指定友链），支持 HTML <a> 代码或 URL 列表。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "links_text": {"type": "string", "description": "待删除的友链文本，支持 HTML <a> 代码或每行 URL / 标题|URL。"},
                        "site_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "目标站点名称列表；省略则作用于全部（或指定分组）站点。",
                        },
                        "group": {"type": "string", "description": "目标站点分组名称。"},
                    },
                    "required": ["links_text"],
                    "additionalProperties": False,
                },
                annotations={"readOnlyHint": False, "destructiveHint": True},
                handler=self._tool_batch_remove_links,
            ),
            _ToolSpec(
                name="audit_backlinks",
                description="并发审计各站点友链的对方反链存活情况并写入 SiteGroupDB 历史记录；仅当 remove_missing=true 且 confirmed=true 时才删除缺失反链的友链。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "site_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "待审计的站点名称列表；省略则审计全部（或指定分组）站点。",
                        },
                        "group": {"type": "string", "description": "待审计的站点分组名称。"},
                        "whitelist_domains": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "白名单域名列表，命中白名单的友链将跳过反链检测且不会被自动删除。",
                        },
                        "remove_missing": {"type": "boolean", "description": "是否删除确认缺失我方反链（status=missing）的友链。", "default": False},
                        "confirmed": {"type": "boolean", "description": "当 remove_missing=true 时，必须传 confirmed=true 才会真正执行删链。", "default": False},
                    },
                    "additionalProperties": False,
                },
                annotations={"readOnlyHint": False, "destructiveHint": True},
                handler=self._tool_audit_backlinks,
            ),
            _ToolSpec(
                name="crosslink_plan",
                description="规划或执行站群互链（mesh/hub/ring/random_k/group_cross）。默认 dry_run=true 仅预览；仅当 dry_run=false 且 confirmed=true 时才真正写入友链并记入回滚台账。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "mode": {
                            "type": "string",
                            "enum": list(_CROSSLINK_MODES),
                            "description": "互链拓扑模式：mesh（全互链）、hub（中心主站）、ring（环形）、random_k（随机 K 链）、group_cross（跨分组互链）。",
                        },
                        "site_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "参与互链的站点名称列表。",
                        },
                        "group": {"type": "string", "description": "按分组筛选参与互链的站点。"},
                        "hub_site": {"type": "string", "description": "当 mode=hub 时指定中心主站名称或 site_id。"},
                        "group_a": {"type": "string", "description": "当 mode=group_cross 时指定分组 A 名称。"},
                        "group_b": {"type": "string", "description": "当 mode=group_cross 时指定分组 B 名称。"},
                        "k": {"type": "integer", "minimum": 1, "description": "random_k 或 group_cross 模式下每个站点的出链数量上限。"},
                        "bidirectional": {"type": "boolean", "description": "是否双向互链。", "default": True},
                        "nofollow": {"type": "boolean", "description": "是否添加 nofollow 属性。", "default": False},
                        "avoid_reciprocal": {"type": "boolean", "description": "random_k 模式下是否尽量避免互换对链（A↔B）。", "default": True},
                        "avoid_same_root_domain": {"type": "boolean", "description": "是否避开同根域名站点互相加链。", "default": True},
                        "dry_run": {"type": "boolean", "description": "是否为预览模式（默认 true，不实际写入）。", "default": True},
                        "confirmed": {"type": "boolean", "description": "当 dry_run=false 时必须传 confirmed=true 才会写入。", "default": False},
                    },
                    "required": ["mode"],
                    "additionalProperties": False,
                },
                annotations={"readOnlyHint": False, "destructiveHint": False},
                handler=self._tool_crosslink_plan,
            ),
            _ToolSpec(
                name="rollback_crosslink",
                description="按互链台账 plan_id 精确回滚删除该次计划新创建的友链，必须设置 confirmed=true。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "plan_id": {"type": "string", "description": "互链台账中的 plan_id。"},
                        "confirmed": {"type": "boolean", "description": "高危确认开关，必须为 true 才会执行回滚删除。", "default": False},
                    },
                    "required": ["plan_id"],
                    "additionalProperties": False,
                },
                annotations={"readOnlyHint": False, "destructiveHint": True},
                handler=self._tool_rollback_crosslink,
            ),
            _ToolSpec(
                name="export_html_links",
                description="导出选定站点的标准 HTML <a> 友情链接代码（多标题池取首条）。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "site_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "要导出的站点名称列表；省略则导出全部（或指定分组）站点。",
                        },
                        "group": {"type": "string", "description": "要导出的站点分组名称。"},
                        "include_nofollow": {"type": "boolean", "description": "是否在 <a> 标签中包含 rel=\"nofollow\"。", "default": False},
                    },
                    "additionalProperties": False,
                },
                annotations={"readOnlyHint": True, "destructiveHint": False},
                handler=self._tool_export_html_links,
            ),
            _ToolSpec(
                name="check_versions",
                description="并发检测站点的当前版本、最新版本与可升级状态，并将结果更新到 SiteGroupDB 缓存。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "site_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "待检测的站点名称列表；省略则检测全部（或指定分组）站点。",
                        },
                        "group": {"type": "string", "description": "待检测的站点分组名称。"},
                    },
                    "additionalProperties": False,
                },
                annotations={"readOnlyHint": True, "destructiveHint": False},
                handler=self._tool_check_versions,
            ),
            _ToolSpec(
                name="upgrade_sites",
                description="对选定站点执行分阶段系统升级（支持金丝雀先升级第 1 站、失败即停、并发 1-5），属于高危操作，必须设置 confirmed=true。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "site_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "待升级的站点名称列表。",
                        },
                        "group": {"type": "string", "description": "待升级的站点分组名称。"},
                        "confirmed": {"type": "boolean", "description": "高危确认开关，必须显式设为 true 才会执行升级。", "default": False},
                        "canary": {"type": "boolean", "description": "是否启用金丝雀策略（先升级第 1 站并等待健康恢复）。", "default": True},
                        "concurrency": {"type": "integer", "minimum": 1, "maximum": 5, "description": "后续批量升级并发数（1-5）。", "default": 2},
                        "stop_on_failure": {"type": "boolean", "description": "遇到失败站点时是否停止后续升级。", "default": True},
                    },
                    "additionalProperties": False,
                },
                annotations={"readOnlyHint": False, "destructiveHint": True},
                handler=self._tool_upgrade_sites,
            ),
            _ToolSpec(
                name="get_admin_jump_url",
                description="解析指定站点的后台跳转 URL（按 ticket -> bridge -> direct 分级回退），仅返回脱敏后的地址与分级信息，不会在服务端打开浏览器。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "site_name": {"type": "string", "description": "目标站点标识名称。"},
                    },
                    "required": ["site_name"],
                    "additionalProperties": False,
                },
                annotations={"readOnlyHint": True, "destructiveHint": False},
                handler=self._tool_get_admin_jump_url,
            ),
            _ToolSpec(
                name="import_sites",
                description="解析批量建站文本（支持 站点名|网址或导入接口|分组|MCP_Token|后台账号|后台密码|站点名称 或单列域名/URL），默认仅预览，save=true 时持久化保存到配置文件，可选 auto_discover 探测站点。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "多行批量建站文本（支持 |、Tab 或逗号分隔）。"},
                        "save": {"type": "boolean", "description": "是否保存到配置文件（默认 false 仅预览）。", "default": False},
                        "auto_discover": {"type": "boolean", "description": "是否并发探测首页标题、友链数与 MCP 状态。", "default": False},
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
                annotations={"readOnlyHint": False, "destructiveHint": False},
                handler=self._tool_import_sites,
            ),
        )

    # ------------------------------------------------------------------ #
    # Tool implementations
    # ------------------------------------------------------------------ #
    def _tool_list_sites(self, args: dict[str, Any]) -> dict[str, Any]:
        group = _opt_str(args, "group", "")
        search = _opt_str(args, "search", "")
        enabled_only = _opt_bool(args, "enabled_only", False)

        all_sites = self._load_sites()
        selected = _select_sites(
            all_sites,
            group=group,
            search=search,
            enabled_only=enabled_only,
        )

        cached_by_name: dict[str, dict[str, Any]] = {}
        try:
            for row in self._get_db().list_site_statuses():
                name = str(row.get("site_name") or "").strip()
                if name:
                    cached_by_name[name] = row
        except Exception:
            cached_by_name = {}

        groups = sorted(
            {
                (getattr(site, "site_group", "") or "").strip()
                for site in all_sites
                if (getattr(site, "site_group", "") or "").strip()
            }
        )

        items: list[dict[str, Any]] = []
        lines: list[str] = []
        for site in selected:
            cached = cached_by_name.get(site.name) or {}
            token = effective_mcp_token(site)
            update_raw = cached.get("update_available")
            mcp_ok_raw = cached.get("mcp_ok")
            item = {
                "name": site.name,
                "site_id": site.site_id,
                "site_title": site.site_title,
                "display_name": display_name(site),
                "site_group": site.site_group,
                "url": site.url,
                "admin_url": admin_url(site) if site.url else "",
                "admin_path": site.admin_path,
                "enabled": bool(site.enabled),
                "fid": site.fid,
                "category_name": site.category_name,
                "has_mcp_token": bool(token),
                "mcp_token_masked": mask_secret(token),
                "has_admin_credentials": has_admin_credentials(site),
                "current_version": str(cached.get("current_version") or ""),
                "latest_version": str(cached.get("latest_version") or ""),
                "update_available": None if update_raw is None else bool(update_raw),
                "mcp_ok": None if mcp_ok_raw is None else bool(mcp_ok_raw),
                "mcp_tools": _parse_tools_json(cached.get("mcp_tools_json")),
                "link_count": cached.get("link_count"),
                "broken_link_count": cached.get("broken_link_count"),
                "last_checked_at": str(cached.get("last_checked_at") or ""),
            }
            items.append(item)

            parts = [f"- {site.name}"]
            if site.site_title:
                parts.append(f"({site.site_title})")
            if site.site_group:
                parts.append(f"[{site.site_group}]")
            parts.append(site.url or "未配置网址")
            parts.append("已启用" if site.enabled else "未启用")
            if item["current_version"]:
                ver_text = item["current_version"]
                if item["latest_version"] and item["update_available"]:
                    ver_text += f" -> {item['latest_version']}(可升级)"
                parts.append(f"版本:{ver_text}")
            if item["mcp_ok"] is not None:
                parts.append(f"MCP:{'正常' if item['mcp_ok'] else '异常'}")
            if item["link_count"] is not None:
                link_txt = f"友链:{item['link_count']}"
                if item["broken_link_count"]:
                    link_txt += f"(异常:{item['broken_link_count']})"
                parts.append(link_txt)
            lines.append(" | ".join(parts))

        header = f"共匹配 {len(items)} 个站点（总计 {len(all_sites)} 个）"
        if groups:
            header += f"，分组：{', '.join(groups)}"
        text = header + ("\n" + "\n".join(lines) if lines else "")
        return _tool_response(
            text,
            {
                "ok": True,
                "total": len(items),
                "total_configured": len(all_sites),
                "groups": groups,
                "sites": items,
            },
        )

    def _tool_search_links(self, args: dict[str, Any]) -> dict[str, Any]:
        query = _req_str(args, "query", allow_empty=True, strip=True)
        site_names = _opt_str_list(args, "site_names")
        group = _opt_str(args, "group", "")
        match_root_domain = _opt_bool(args, "match_root_domain", True)

        all_sites = self._load_sites()
        target_sites = _select_sites(all_sites, site_names=site_names, group=group)
        if not target_sites:
            return _tool_error("未匹配到目标站点")

        redactor = SecretRedactor.from_sites(target_sites)
        result = self._get_friendlink_service().search_links_across_sites(
            target_sites,
            query=query,
            match_root_domain=match_root_domain,
        )

        raw_matches = result.get("matches") or []
        site_counts = dict(result.get("site_counts") or {})
        raw_errors = dict(result.get("site_errors") or {})
        total_links = int(result.get("total_links") or 0)

        matches = []
        for item in raw_matches:
            entry = dict(item)
            if "link" in entry:
                entry["link"] = redactor(str(entry["link"]))
            if "back_link" in entry:
                entry["back_link"] = redactor(str(entry["back_link"]))
            matches.append(entry)

        site_errors = {str(k): redactor(str(v)) for k, v in raw_errors.items()}

        site_by_name = {site.name: site for site in target_sites}
        for sname, count in site_counts.items():
            site = site_by_name.get(sname)
            if site is not None:
                try:
                    self._get_db().upsert_site_status(
                        sname,
                        site_url=site.url,
                        site_title=site.site_title,
                        link_count=count,
                    )
                except Exception:
                    pass

        lines = [
            f"检索完成：关键词={query!r}，扫描 {len(target_sites)} 个站点（共 {total_links} 条友链），匹配 {len(matches)} 条"
        ]
        for match in matches[:50]:
            lines.append(
                f"- [{match.get('site_name', '')}] {match.get('title', '')} -> {match.get('link', '')}"
            )
        if len(matches) > 50:
            lines.append(f"... 其余 {len(matches) - 50} 条见 structuredContent.matches")
        if site_errors:
            lines.append(f"失败站点（{len(site_errors)}）：" + "；".join(f"{k}: {v}" for k, v in site_errors.items()))

        return _tool_response(
            "\n".join(lines),
            {
                "ok": True,
                "query": query,
                "match_root_domain": match_root_domain,
                "site_count": len(target_sites),
                "total_links": total_links,
                "matched_count": len(matches),
                "matches": matches,
                "site_counts": site_counts,
                "site_errors": site_errors,
            },
        )

    def _tool_batch_add_links(self, args: dict[str, Any]) -> dict[str, Any]:
        links_text = _req_str(args, "links_text", allow_empty=False, strip=False)
        site_names = _opt_str_list(args, "site_names")
        group = _opt_str(args, "group", "")
        skip_existing = _opt_bool(args, "skip_existing", True)
        nofollow = _opt_bool(args, "nofollow", False)

        specs, parse_errors = parse_batch_link_input(links_text, default_nofollow=nofollow)
        if not specs:
            detail = "；".join(parse_errors) if parse_errors else "未识别到任何有效的友链 URL"
            return _tool_error(f"解析友链失败：{detail}", extra={"parse_errors": parse_errors})

        all_sites = self._load_sites()
        target_sites = _select_sites(all_sites, site_names=site_names, group=group)
        if not target_sites:
            return _tool_error("未匹配到目标站点")

        redactor = SecretRedactor.from_sites(target_sites)
        results = self._get_friendlink_service().batch_add(
            target_sites,
            specs,
            skip_existing=skip_existing,
        )
        summary = _summarize_op_results(results)
        self._safe_record_operation(
            "friendlink",
            "batch_add",
            site_name=",".join(s.name for s in target_sites[:5]),
            status="ok" if summary.get(LINK_FAILED, 0) == 0 else "partial",
            summary=f"批量上链 {len(specs)} 条 x {len(target_sites)} 站：新增 {summary.get(LINK_CREATED, 0)}，已存在跳过 {summary.get(LINK_SKIPPED_EXISTS, 0)}，失败 {summary.get(LINK_FAILED, 0)}",
            detail=summary,
            redactor=redactor,
        )

        serialized = [_serialize_op_result(res, redactor) for res in results]
        lines = [
            f"批量上链完成：解析 {len(specs)} 条友链，作用于 {len(target_sites)} 个站点 | "
            f"新增 {summary.get(LINK_CREATED, 0)} / 已存在跳过 {summary.get(LINK_SKIPPED_EXISTS, 0)} / "
            f"跳过本站 {summary.get(LINK_SKIPPED_SELF, 0)} / 失败 {summary.get(LINK_FAILED, 0)}"
        ]
        for entry in serialized[:40]:
            lines.append(
                f"- [{entry['site_name']}] {entry['status']}: {entry['title']} ({entry['link']}) {entry['message']}".strip()
            )
        if parse_errors:
            lines.append("解析警告：" + "；".join(parse_errors))

        return _tool_response(
            "\n".join(lines),
            {
                "ok": True,
                "parsed_count": len(specs),
                "parse_errors": parse_errors,
                "site_count": len(target_sites),
                "summary": summary,
                "results": serialized,
            },
        )

    def _tool_batch_remove_links(self, args: dict[str, Any]) -> dict[str, Any]:
        links_text = _req_str(args, "links_text", allow_empty=False, strip=False)
        site_names = _opt_str_list(args, "site_names")
        group = _opt_str(args, "group", "")

        urls, parse_errors = _extract_remove_urls(links_text)
        if not urls:
            detail = "；".join(parse_errors) if parse_errors else "未识别到任何有效的友链 URL"
            return _tool_error(f"解析待删除友链失败：{detail}", extra={"parse_errors": parse_errors})

        all_sites = self._load_sites()
        target_sites = _select_sites(all_sites, site_names=site_names, group=group)
        if not target_sites:
            return _tool_error("未匹配到目标站点")

        redactor = SecretRedactor.from_sites(target_sites)
        results = self._get_friendlink_service().batch_remove(target_sites, urls)
        summary = _summarize_op_results(results)
        self._safe_record_operation(
            "friendlink",
            "batch_remove",
            site_name=",".join(s.name for s in target_sites[:5]),
            status="ok" if summary.get(LINK_FAILED, 0) == 0 else "partial",
            summary=f"批量下链 {len(urls)} 条 x {len(target_sites)} 站：已删除 {summary.get(LINK_DELETED, 0)}，未找到 {summary.get(LINK_NOT_FOUND, 0)}，失败 {summary.get(LINK_FAILED, 0)}",
            detail=summary,
            redactor=redactor,
        )

        serialized = [_serialize_op_result(res, redactor) for res in results]
        lines = [
            f"批量下链完成：目标 {len(urls)} 条，作用于 {len(target_sites)} 个站点 | "
            f"已删除 {summary.get(LINK_DELETED, 0)} / 未找到 {summary.get(LINK_NOT_FOUND, 0)} / "
            f"失败 {summary.get(LINK_FAILED, 0)}"
        ]
        for entry in serialized[:40]:
            lines.append(
                f"- [{entry['site_name']}] {entry['status']}: {entry['link']} {entry['message']}".strip()
            )
        if parse_errors:
            lines.append("解析警告：" + "；".join(parse_errors))

        return _tool_response(
            "\n".join(lines),
            {
                "ok": True,
                "target_urls": urls,
                "parse_errors": parse_errors,
                "site_count": len(target_sites),
                "summary": summary,
                "results": serialized,
            },
        )

    def _tool_audit_backlinks(self, args: dict[str, Any]) -> dict[str, Any]:
        site_names = _opt_str_list(args, "site_names")
        group = _opt_str(args, "group", "")
        whitelist_domains = _opt_str_list(args, "whitelist_domains") or []
        remove_missing = _opt_bool(args, "remove_missing", False)
        confirmed = _opt_bool(args, "confirmed", False)

        all_sites = self._load_sites()
        target_sites = _select_sites(all_sites, site_names=site_names, group=group)
        if not target_sites:
            return _tool_error("未匹配到目标站点")

        redactor = SecretRedactor.from_sites(target_sites)
        fl_service = self._get_friendlink_service()
        audit = fl_service.batch_check_backlinks(
            target_sites,
            whitelist_domains=whitelist_domains,
        )

        raw_items = list(audit.get("items") or [])
        summary = dict(audit.get("summary") or {})
        raw_errors = dict(audit.get("site_errors") or {})
        site_errors = {str(k): redactor(str(v)) for k, v in raw_errors.items()}

        items: list[dict[str, Any]] = []
        per_site_total: dict[str, int] = {}
        per_site_broken: dict[str, int] = {}
        missing_by_site: dict[str, list[str]] = {}
        db = self._get_db()

        for raw_item in raw_items:
            entry = dict(raw_item)
            raw_link = str(entry.get("link") or "")
            entry["link"] = redactor(raw_link)
            entry["message"] = redactor(str(entry.get("message") or ""))
            items.append(entry)

            sname = str(entry.get("site_name") or "")
            status = str(entry.get("status") or "")
            if sname:
                per_site_total[sname] = per_site_total.get(sname, 0) + 1
                if status in {"missing", "unreachable"}:
                    per_site_broken[sname] = per_site_broken.get(sname, 0) + 1
                if status == "missing" and raw_link:
                    missing_by_site.setdefault(sname, []).append(raw_link)
                try:
                    db.record_backlink_audit(
                        site_name=sname,
                        target_url=entry["link"],
                        target_title=str(entry.get("title") or ""),
                        status=status,
                        http_status=entry.get("http_status"),
                        message=entry["message"],
                    )
                except Exception:
                    pass

        site_by_name = {site.name: site for site in target_sites}
        for sname, total_cnt in per_site_total.items():
            site = site_by_name.get(sname)
            if site is not None:
                try:
                    db.upsert_site_status(
                        sname,
                        site_url=site.url,
                        site_title=site.site_title,
                        link_count=total_cnt,
                        broken_link_count=per_site_broken.get(sname, 0),
                    )
                except Exception:
                    pass

        self._safe_record_operation(
            "friendlink",
            "audit_backlinks",
            site_name=",".join(s.name for s in target_sites[:5]),
            status="ok" if not site_errors else "partial",
            summary=f"反链审计 {len(target_sites)} 站：共 {summary.get('total', 0)} 条，正常 {summary.get('ok', 0)}，缺失 {summary.get('missing', 0)}，不可达 {summary.get('unreachable', 0)}",
            detail=summary,
            redactor=redactor,
        )

        removal_summary: dict[str, Any] | None = None
        removal_results: list[dict[str, Any]] = []
        confirmation_required = False
        missing_count = sum(len(v) for v in missing_by_site.values())

        if remove_missing and missing_count > 0:
            if not confirmed:
                confirmation_required = True
                removal_summary = {
                    "executed": False,
                    "confirmation_required": True,
                    "pending_missing_count": missing_count,
                    "deleted": 0,
                    "failed": 0,
                }
            else:
                all_remove_results: list[LinkOpResult] = []
                for sname, urls in missing_by_site.items():
                    site = site_by_name.get(sname)
                    if site is None or not urls:
                        continue
                    all_remove_results.extend(fl_service.batch_remove([site], urls))
                removal_summary = {
                    "executed": True,
                    "confirmation_required": False,
                    **_summarize_op_results(all_remove_results),
                }
                removal_results = [_serialize_op_result(res, redactor) for res in all_remove_results]
                self._safe_record_operation(
                    "friendlink",
                    "remove_missing_backlinks",
                    site_name=",".join(missing_by_site.keys()),
                    status="ok" if removal_summary.get(LINK_FAILED, 0) == 0 else "partial",
                    summary=f"清理缺失反链：删除 {removal_summary.get(LINK_DELETED, 0)}，失败 {removal_summary.get(LINK_FAILED, 0)}",
                    detail=removal_summary,
                    redactor=redactor,
                )
        elif remove_missing:
            removal_summary = {
                "executed": bool(confirmed),
                "confirmation_required": False,
                "total": 0,
                "deleted": 0,
            }

        lines = [
            f"反链审计完成（{len(target_sites)} 个站点）：共 {summary.get('total', 0)} 条 | "
            f"正常 {summary.get('ok', 0)} / nofollow {summary.get('nofollow', 0)} / "
            f"缺失反链 {summary.get('missing', 0)} / 不可达 {summary.get('unreachable', 0)} / "
            f"白名单 {summary.get('whitelisted', 0)}"
        ]
        for entry in items:
            if entry.get("status") in {"missing", "unreachable", "nofollow"}:
                lines.append(
                    f"- [{entry.get('site_name', '')}] {entry.get('status')}: {entry.get('title', '')} ({entry.get('link', '')}) - {entry.get('message', '')}"
                )
        if confirmation_required:
            lines.append(
                f"提示：检测到 {missing_count} 条缺失反链，因未设置 confirmed=true 未执行删除；如需清理请设置 remove_missing=true 且 confirmed=true。"
            )
        elif removal_summary and removal_summary.get("executed"):
            lines.append(
                f"缺失反链清理结果：已删除 {removal_summary.get(LINK_DELETED, 0)}，失败 {removal_summary.get(LINK_FAILED, 0)}"
            )
        if site_errors:
            lines.append("站点错误：" + "；".join(f"{k}: {v}" for k, v in site_errors.items()))

        return _tool_response(
            "\n".join(lines),
            {
                "ok": True,
                "site_count": len(target_sites),
                "summary": summary,
                "items": items,
                "site_errors": site_errors,
                "confirmation_required": confirmation_required,
                "removal_summary": removal_summary,
                "removal_results": removal_results,
            },
        )

    def _tool_crosslink_plan(self, args: dict[str, Any]) -> dict[str, Any]:
        mode = _req_str(args, "mode")
        if mode not in _CROSSLINK_MODES:
            raise _ToolInputError(f"不支持的互链模式：{mode}（可选：{', '.join(_CROSSLINK_MODES)}）")

        site_names = _opt_str_list(args, "site_names")
        group = _opt_str(args, "group", "")
        hub_site = _opt_str(args, "hub_site", "")
        group_a = _opt_str(args, "group_a", "")
        group_b = _opt_str(args, "group_b", "")
        k = _opt_int(args, "k", None, minimum=1)
        bidirectional = _opt_bool(args, "bidirectional", True)
        nofollow = _opt_bool(args, "nofollow", False)
        avoid_reciprocal = _opt_bool(args, "avoid_reciprocal", True)
        avoid_same_root_domain = _opt_bool(args, "avoid_same_root_domain", True)
        dry_run = _opt_bool(args, "dry_run", True)
        confirmed = _opt_bool(args, "confirmed", False)

        if not dry_run and not confirmed:
            return _tool_error(
                "执行站群互链写入必须显式设置 confirmed=true；若仅预览请保持 dry_run=true。",
                extra={"confirmation_required": True, "mode": mode, "dry_run": False},
            )

        all_sites = self._load_sites()
        target_sites = _select_sites(all_sites, site_names=site_names, group=group)
        redactor = SecretRedactor.from_sites(target_sites)
        fl_service = self._get_friendlink_service()

        try:
            plan = fl_service.plan_crosslinks(
                target_sites,
                mode,
                hub_site_id=hub_site,
                bidirectional=bidirectional,
                nofollow=nofollow,
                k=k,
                avoid_reciprocal=avoid_reciprocal,
                avoid_same_root_domain=avoid_same_root_domain,
                group_a=group_a or None,
                group_b=group_b or None,
            )
        except ValueError as exc:
            return _tool_error(str(exc), redactor=redactor)

        if dry_run:
            previewed = fl_service.preview_plan(plan, target_sites)
            items = [_serialize_crosslink_item(item, redactor) for item in previewed.items]
            counts = dict(previewed.counts)
            lines = [
                f"互链预览（mode={previewed.mode}, plan_id={previewed.plan_id}）：共 {len(items)} 条计划 | "
                f"待创建 {counts.get(LINK_PLANNED, 0)} / 已存在 {counts.get(LINK_SKIPPED_EXISTS, 0)} / "
                f"跳过本站 {counts.get(LINK_SKIPPED_SELF, 0)} / 失败 {counts.get(LINK_FAILED, 0)}"
            ]
            for item in items[:40]:
                lines.append(
                    f"- {item['source_site']} -> {item['target_site']} ({item['title']}): {item['state']} {item['message']}".strip()
                )
            lines.append("提示：当前为 dry_run 预览，未实际写入；如需执行请传入 dry_run=false 且 confirmed=true。")
            return _tool_response(
                "\n".join(lines),
                {
                    "ok": True,
                    "dry_run": True,
                    "executed": False,
                    "plan_id": previewed.plan_id,
                    "mode": previewed.mode,
                    "hub_site_id": previewed.hub_site_id,
                    "bidirectional": previewed.bidirectional,
                    "counts": counts,
                    "total_items": len(items),
                    "items": items,
                },
            )

        ledger = self._get_ledger()
        report = fl_service.execute_plan(plan, target_sites, ledger=ledger)
        items = [_serialize_crosslink_item(item, redactor) for item in report.plan.items]
        results = [_serialize_op_result(res, redactor) for res in report.results]
        counts = dict(report.plan.counts)

        self._safe_record_operation(
            "friendlink",
            "crosslink_execute",
            site_name=",".join(s.name for s in target_sites[:5]),
            status="ok" if counts.get(LINK_FAILED, 0) == 0 else "partial",
            summary=f"执行互链 {report.plan.mode} ({report.plan.plan_id})：创建 {counts.get(LINK_CREATED, 0)}，已存在 {counts.get(LINK_SKIPPED_EXISTS, 0)}，失败 {counts.get(LINK_FAILED, 0)}",
            detail={"plan_id": report.plan.plan_id, "mode": report.plan.mode, "counts": counts},
            redactor=redactor,
        )

        lines = [
            f"互链已执行（mode={report.plan.mode}, plan_id={report.plan.plan_id}）：共 {len(items)} 条 | "
            f"新增 {counts.get(LINK_CREATED, 0)} / 已存在 {counts.get(LINK_SKIPPED_EXISTS, 0)} / "
            f"失败 {counts.get(LINK_FAILED, 0)}（已记入台账，可通过 rollback_crosslink 回滚）"
        ]
        for entry in results[:40]:
            lines.append(
                f"- [{entry['site_name']}] {entry['status']}: {entry['title']} ({entry['link']}) {entry['message']}".strip()
            )
        return _tool_response(
            "\n".join(lines),
            {
                "ok": True,
                "dry_run": False,
                "executed": True,
                "plan_id": report.plan.plan_id,
                "mode": report.plan.mode,
                "hub_site_id": report.plan.hub_site_id,
                "bidirectional": report.plan.bidirectional,
                "counts": counts,
                "total_items": len(items),
                "items": items,
                "results": results,
            },
        )

    def _tool_rollback_crosslink(self, args: dict[str, Any]) -> dict[str, Any]:
        plan_id = _req_str(args, "plan_id")
        confirmed = _opt_bool(args, "confirmed", False)

        ledger = self._get_ledger()
        pending_entries = ledger.created_links(plan_id)

        if not confirmed:
            return _tool_error(
                f"回滚互链计划 {plan_id} 将从目标站点删除 {len(pending_entries)} 条已创建友链，必须设置 confirmed=true 确认后方可执行。",
                extra={
                    "confirmation_required": True,
                    "plan_id": plan_id,
                    "pending_count": len(pending_entries),
                },
            )

        if not pending_entries:
            return _tool_response(
                f"未找到计划 {plan_id} 下可回滚的已创建友链（可能不存在或已全部回滚）。",
                {
                    "ok": True,
                    "plan_id": plan_id,
                    "total": 0,
                    "summary": {"total": 0},
                    "results": [],
                },
            )

        all_sites = self._load_sites()
        redactor = SecretRedactor.from_sites(all_sites)
        results = self._get_friendlink_service().rollback_plan(
            plan_id,
            all_sites,
            ledger=ledger,
        )
        summary = _summarize_op_results(results)
        self._safe_record_operation(
            "friendlink",
            "rollback_crosslink",
            status="ok" if summary.get(LINK_FAILED, 0) == 0 else "partial",
            summary=f"回滚互链计划 {plan_id}：已删除 {summary.get(LINK_DELETED, 0)}，未找到 {summary.get(LINK_NOT_FOUND, 0)}，失败 {summary.get(LINK_FAILED, 0)}",
            detail={"plan_id": plan_id, "summary": summary},
            redactor=redactor,
        )

        serialized = [_serialize_op_result(res, redactor) for res in results]
        lines = [
            f"回滚完成（plan_id={plan_id}）：共处理 {len(serialized)} 条 | "
            f"已删除 {summary.get(LINK_DELETED, 0)} / 未找到 {summary.get(LINK_NOT_FOUND, 0)} / "
            f"失败 {summary.get(LINK_FAILED, 0)}"
        ]
        for entry in serialized[:40]:
            lines.append(
                f"- [{entry['site_name']}] {entry['status']}: {entry['link']} {entry['message']}".strip()
            )
        return _tool_response(
            "\n".join(lines),
            {
                "ok": True,
                "plan_id": plan_id,
                "total": len(serialized),
                "summary": summary,
                "results": serialized,
            },
        )

    def _tool_export_html_links(self, args: dict[str, Any]) -> dict[str, Any]:
        site_names = _opt_str_list(args, "site_names")
        group = _opt_str(args, "group", "")
        include_nofollow = _opt_bool(args, "include_nofollow", False)

        all_sites = self._load_sites()
        target_sites = _select_sites(all_sites, site_names=site_names, group=group)
        if not target_sites:
            return _tool_error("未匹配到目标站点")

        html_text = export_sites_html_links(
            target_sites,
            include_nofollow=include_nofollow,
        )
        exported_lines = [line for line in html_text.splitlines() if line.strip()]
        site_items = [
            {
                "name": site.name,
                "display_name": display_name(site),
                "url": site.url,
                "site_group": site.site_group,
            }
            for site in target_sites
            if site.url
        ]
        text = f"已导出 {len(exported_lines)} 条 HTML 友链代码：\n{html_text}" if exported_lines else "所选站点均未配置有效网址，未生成 HTML 友链。"
        return _tool_response(
            text,
            {
                "ok": True,
                "count": len(exported_lines),
                "include_nofollow": include_nofollow,
                "html": html_text,
                "sites": site_items,
            },
        )

    def _tool_check_versions(self, args: dict[str, Any]) -> dict[str, Any]:
        site_names = _opt_str_list(args, "site_names")
        group = _opt_str(args, "group", "")

        all_sites = self._load_sites()
        target_sites = _select_sites(all_sites, site_names=site_names, group=group)
        if not target_sites:
            return _tool_error("未匹配到目标站点")

        redactor = SecretRedactor.from_sites(target_sites)
        infos = self._get_upgrade_service().check_versions(target_sites)
        site_by_name = {site.name: site for site in target_sites}
        db = self._get_db()

        serialized: list[dict[str, Any]] = []
        lines: list[str] = []
        reachable_count = 0
        upgrade_count = 0
        latest_count = 0

        for info in infos:
            if info.reachable:
                reachable_count += 1
            if info.upgrade_available is True:
                upgrade_count += 1
            elif info.upgrade_available is False:
                latest_count += 1

            site = site_by_name.get(info.site_name)
            try:
                existing_row = db.get_site_status(info.site_name) or {}
                existing_tools = _parse_tools_json(existing_row.get("mcp_tools_json"))
                discovered_tools = [t for t in (info.check_tool, info.upgrade_tool) if t]
                merged_tools = list(existing_tools)
                for tool_name in discovered_tools:
                    if tool_name not in merged_tools:
                        merged_tools.append(tool_name)

                upsert_kwargs: dict[str, Any] = {
                    "site_url": getattr(site, "url", "") if site is not None else "",
                    "site_title": getattr(site, "site_title", "") if site is not None else "",
                    "current_version": info.current_version,
                    "latest_version": info.latest_version,
                    "update_available": info.upgrade_available,
                }
                if info.source == "mcp":
                    upsert_kwargs["mcp_ok"] = True
                elif not info.reachable:
                    upsert_kwargs["mcp_ok"] = False
                if merged_tools:
                    upsert_kwargs["mcp_tools"] = merged_tools
                db.upsert_site_status(info.site_name, **upsert_kwargs)
            except Exception:
                pass

            entry = _serialize_version_info(info, redactor)
            serialized.append(entry)
            ver_part = entry["current_version"] or "未知"
            if entry["latest_version"]:
                ver_part += f" -> {entry['latest_version']}"
            status_tag = (
                "可升级"
                if entry["upgrade_available"] is True
                else ("已是最新" if entry["upgrade_available"] is False else ("可达" if entry["reachable"] else "不可达"))
            )
            lines.append(
                f"- [{entry['site_name']}] {status_tag} | {ver_part} | {entry['message']}"
            )

        summary = {
            "total": len(serialized),
            "reachable": reachable_count,
            "unreachable": len(serialized) - reachable_count,
            "upgrade_available": upgrade_count,
            "up_to_date": latest_count,
        }
        self._safe_record_operation(
            "upgrade",
            "check_versions",
            site_name=",".join(s.name for s in target_sites[:5]),
            status="ok",
            summary=f"版本检测 {len(serialized)} 站：可升级 {upgrade_count}，已是最新 {latest_count}，不可达 {summary['unreachable']}",
            detail=summary,
            redactor=redactor,
        )

        header = (
            f"版本检测完成（{len(serialized)} 个站点）：可升级 {upgrade_count} / "
            f"已是最新 {latest_count} / 不可达 {summary['unreachable']}"
        )
        return _tool_response(
            header + ("\n" + "\n".join(lines) if lines else ""),
            {
                "ok": True,
                "summary": summary,
                "sites": serialized,
            },
        )

    def _tool_upgrade_sites(self, args: dict[str, Any]) -> dict[str, Any]:
        site_names = _opt_str_list(args, "site_names")
        group = _opt_str(args, "group", "")
        confirmed = _opt_bool(args, "confirmed", False)
        canary = _opt_bool(args, "canary", True)
        concurrency = _opt_int(args, "concurrency", 2, minimum=1, maximum=5)
        assert concurrency is not None
        stop_on_failure = _opt_bool(args, "stop_on_failure", True)

        if confirmed is not True:
            return _tool_error(
                "站点系统升级属于高危操作（可能触发站点重启），必须显式设置 confirmed=true 确认后方可执行。",
                extra={"confirmation_required": True},
            )

        all_sites = self._load_sites()
        target_sites = _select_sites(all_sites, site_names=site_names, group=group)
        if not target_sites:
            return _tool_error("未匹配到目标站点")

        redactor = SecretRedactor.from_sites(target_sites)
        results = self._get_upgrade_service().upgrade_sites(
            target_sites,
            confirmed=True,
            max_workers=concurrency,
            canary_first=canary,
            stop_on_failure=stop_on_failure,
        )

        site_by_name = {site.name: site for site in target_sites}
        db = self._get_db()
        counts: dict[str, int] = {"total": len(results)}
        serialized: list[dict[str, Any]] = []
        lines: list[str] = []

        for res in results:
            counts[res.status] = counts.get(res.status, 0) + 1
            site = site_by_name.get(res.site_name)
            if res.status in {UPGRADE_UPGRADED, UPGRADE_ALREADY_LATEST}:
                ver = res.to_version or res.from_version
                try:
                    db.upsert_site_status(
                        res.site_name,
                        site_url=getattr(site, "url", "") if site is not None else "",
                        site_title=getattr(site, "site_title", "") if site is not None else "",
                        current_version=ver,
                        latest_version=ver,
                        update_available=False,
                    )
                except Exception:
                    pass
            entry = _serialize_upgrade_result(res, redactor)
            serialized.append(entry)
            lines.append(
                f"- [{entry['site_name']}] {entry['status']}: {entry['from_version']} -> {entry['to_version']} | {entry['message']}"
            )

        self._safe_record_operation(
            "upgrade",
            "upgrade_sites",
            site_name=",".join(s.name for s in target_sites[:5]),
            status="ok" if counts.get(UPGRADE_FAILED, 0) == 0 and counts.get(UPGRADE_TIMEOUT, 0) == 0 else "partial",
            summary=f"批量升级 {len(results)} 站：成功 {counts.get(UPGRADE_UPGRADED, 0)}，最新 {counts.get(UPGRADE_ALREADY_LATEST, 0)}，失败 {counts.get(UPGRADE_FAILED, 0)}，跳过 {counts.get(UPGRADE_SKIPPED, 0)}",
            detail=counts,
            redactor=redactor,
        )

        header = (
            f"批量升级完成（{len(results)} 个站点，canary={canary}, concurrency={concurrency}, stop_on_failure={stop_on_failure}）："
            f"成功 {counts.get(UPGRADE_UPGRADED, 0)} / 已最新 {counts.get(UPGRADE_ALREADY_LATEST, 0)} / "
            f"失败 {counts.get(UPGRADE_FAILED, 0)} / 跳过 {counts.get(UPGRADE_SKIPPED, 0)}"
        )
        return _tool_response(
            header + ("\n" + "\n".join(lines) if lines else ""),
            {
                "ok": True,
                "canary": canary,
                "concurrency": concurrency,
                "stop_on_failure": stop_on_failure,
                "summary": counts,
                "results": serialized,
            },
        )

    def _tool_get_admin_jump_url(self, args: dict[str, Any]) -> dict[str, Any]:
        site_name = _req_str(args, "site_name")
        all_sites = self._load_sites()
        matched = _select_sites(all_sites, site_names=[site_name])
        site = matched[0]
        redactor = SecretRedactor.from_sites([site])

        try:
            plan = self._get_jump_service().resolve(site)
        except Exception as exc:  # noqa: BLE001
            return _tool_error(f"无法解析后台跳转地址：{exc}", redactor=redactor)

        safe_url = redactor(plan.target_url)
        safe_display = redactor(plan.display_url)
        safe_reasons = [redactor(reason) for reason in plan.fallback_reasons if reason]
        reason_text = "；".join(safe_reasons) if safe_reasons else _default_jump_reason(plan.tier)

        self._safe_record_operation(
            "admin_jump",
            "resolve_url",
            site_name=site.name,
            status=plan.tier,
            summary=reason_text,
            detail={"tier": plan.tier, "display_url": safe_display},
            redactor=redactor,
        )

        text = (
            f"站点 {site.name} 后台跳转地址（tier={plan.tier}）：{safe_url}\n"
            f"说明：{reason_text}"
        )
        return _tool_response(
            text,
            {
                "ok": True,
                "site_name": site.name,
                "url": safe_url,
                "display_url": safe_display,
                "tier": plan.tier,
                "reason": reason_text,
                "fallback_reasons": safe_reasons,
            },
        )

    def _tool_import_sites(self, args: dict[str, Any]) -> dict[str, Any]:
        text = _req_str(args, "text", allow_empty=False, strip=False)
        save = _opt_bool(args, "save", False)
        auto_discover = _opt_bool(args, "auto_discover", False)

        existing_sites = self._load_sites()
        new_sites, raw_warnings = parse_bulk_new_sites(text, existing_sites, self.config_path)
        redactor = SecretRedactor.from_sites([*existing_sites, *new_sites])

        discoveries_by_name: dict[str, SiteDiscoveryResult] = {}
        if auto_discover and new_sites:
            settings = self._get_settings()

            def _probe_fn(site: SiteConfig) -> McpProbeResult | None:
                if not effective_mcp_token(site):
                    return None
                if self._mcp_client_factory is not None:
                    client = self._mcp_client_factory(site)
                    try:
                        client.initialize()
                        tools = client.list_tools()
                        return McpProbeResult(
                            site_name=site.name,
                            ok=True,
                            tool_count=len(tools),
                            tools=tuple(tools),
                        )
                    finally:
                        close_fn = getattr(client, "close", None)
                        if callable(close_fn):
                            try:
                                close_fn()
                            except Exception:
                                pass
                return probe_site(site, settings)

            discoveries = auto_discover_sites(
                new_sites,
                probe_fn=_probe_fn,
                verify_ssl=settings.mcp.verify_ssl,
                update_site=True,
            )
            discoveries_by_name = {res.site_name: res for res in discoveries}

        saved = False
        if save and new_sites:
            self._save_sites([*existing_sites, *new_sites])
            if self._config_service is None:
                for site in new_sites:
                    ensure_site_files(site)
            saved = True
            self._safe_record_operation(
                "onboarding",
                "import_sites",
                site_name=",".join(s.name for s in new_sites[:5]),
                status="saved",
                summary=f"批量导入并保存 {len(new_sites)} 个新站点（跳过/警告 {len(raw_warnings)} 条）",
                detail={"count": len(new_sites), "names": [s.name for s in new_sites]},
                redactor=redactor,
            )
            if discoveries_by_name:
                db = self._get_db()
                for site in new_sites:
                    disc = discoveries_by_name.get(site.name)
                    if disc is not None:
                        try:
                            db.upsert_site_status(
                                site.name,
                                site_url=site.url,
                                site_title=site.site_title,
                                link_count=disc.link_count,
                                mcp_ok=disc.mcp_ok,
                                mcp_tools=disc.mcp_tools,
                            )
                        except Exception:
                            pass

        serialized_sites: list[dict[str, Any]] = []
        lines: list[str] = []
        for site in new_sites:
            token = effective_mcp_token(site)
            disc = discoveries_by_name.get(site.name)
            item: dict[str, Any] = {
                "name": site.name,
                "site_id": site.site_id,
                "url": site.url,
                "site_title": site.site_title,
                "site_group": site.site_group,
                "has_import_endpoint": bool(site.jiekou),
                "has_mcp_token": bool(token),
                "mcp_token_masked": mask_secret(token),
                "has_admin_credentials": has_admin_credentials(site),
                "enabled": bool(site.enabled),
            }
            if disc is not None:
                item["discovery"] = {
                    "reachable": disc.reachable,
                    "http_status": disc.http_status,
                    "discovered_title": disc.discovered_title,
                    "link_count": disc.link_count,
                    "mcp_ok": disc.mcp_ok,
                    "mcp_tools": list(disc.mcp_tools),
                    "message": redactor(disc.message),
                }
            serialized_sites.append(item)

            desc_parts = [f"- {site.name}", site.url]
            if site.site_group:
                desc_parts.append(f"[{site.site_group}]")
            if site.site_title:
                desc_parts.append(site.site_title)
            if disc is not None:
                desc_parts.append("可达" if disc.reachable else "不可达")
                if disc.link_count is not None:
                    desc_parts.append(f"友链:{disc.link_count}")
            lines.append(" | ".join(desc_parts))

        warnings = [
            {str(k): redactor(str(v)) for k, v in warning.items()}
            for warning in raw_warnings
        ]
        mode_label = "已保存" if saved else ("预览（未保存）" if not save else "无新增站点可保存")
        header = f"批量导入（{mode_label}）：解析新增 {len(serialized_sites)} 个站点，跳过/警告 {len(warnings)} 条"
        if warnings:
            lines.extend(
                f"! 第{w.get('line', '?')}行：{w.get('reason', '')}" for w in warnings[:20]
            )

        return _tool_response(
            header + ("\n" + "\n".join(lines) if lines else ""),
            {
                "ok": True,
                "saved": saved,
                "save_requested": save,
                "auto_discover": auto_discover,
                "parsed_count": len(serialized_sites),
                "warning_count": len(warnings),
                "sites": serialized_sites,
                "warnings": warnings,
            },
        )
