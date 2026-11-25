"""Streamable HTTP JSON-RPC 2.0 client for AnqiCMS official MCP endpoint.

Every ``AnqiMcpClient`` instance is stateful (session id, negotiated protocol
version, cached tool list, incrementing request id) and is **not** thread-safe:
create one instance per worker thread / site task.
"""

from __future__ import annotations

import inspect
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable

import requests

from core.app_info import APP_NAME, APP_VERSION
from core.integration_settings import IntegrationSettings, McpSettings
from core.mcp_contracts import (
    APPROVAL_HINT,
    REDIRECT_HINT,
    SSL_HINT,
    McpApprovalRequiredError,
    McpAuthError,
    McpError,
    McpProtocolError,
    McpScopeError,
    McpToolError,
    McpUnavailableError,
    classify_scope_error,
    detect_approval_details,
    is_jump_candidate_tool,
    is_upgrade_candidate_tool,
    score_tools,
    tool_action_enum,
)
from core.redaction import SecretRedactor, redact_text
from core.site_credentials import effective_mcp_token
from models.sitegroup import McpProbeResult, McpToolInfo, McpToolResult

MAX_TOOL_PAGES = 20
_ERROR_SNIPPET_LIMIT = 200

_INTENT_CATALOG_EXPLICIT_NAMES: tuple[str, ...] = (
    "mcp_list_intents",
    "list_intents",
    "intent_catalog",
    "get_intents",
    "intents_list",
    "discover_intents",
    "intent_search",
    "search_intents",
)
_INTENT_CATALOG_KEYWORDS: tuple[str, ...] = (
    "mcp_list_intents",
    "intent_search",
    "search_intents",
    "list_intents",
    "intent_catalog",
    "意图目录",
    "意图搜索",
    "搜索意图",
    "意图列表",
)
_INTENT_EXECUTE_EXPLICIT_NAMES: tuple[str, ...] = (
    "execute_intent",
    "call_intent",
    "run_intent",
    "invoke_intent",
    "intent_execute",
    "intent_call",
    "intent_run",
)
_INTENT_EXECUTE_KEYWORDS: tuple[str, ...] = (
    "execute_intent",
    "call_intent",
    "run_intent",
    "invoke_intent",
    "执行意图",
)
_CONFIRM_TOOL_EXPLICIT_NAMES: tuple[str, ...] = (
    "tool_confirm_batch",
    "confirm_batch",
    "tool_confirm",
    "confirm_tool",
)
_CATALOG_LIST_KEYS: tuple[str, ...] = (
    "intents",
    "tools",
    "items",
    "list",
    "catalog",
    "results",
    "entries",
    "records",
)
_CATALOG_NESTED_KEYS: tuple[str, ...] = ("data", "result", "payload")
_CATALOG_QUERY_PROPS: tuple[str, ...] = ("query", "keyword", "keywords", "q", "search", "term", "pattern")
_EXECUTE_NAME_PROPS: tuple[str, ...] = (
    "intent",
    "intent_name",
    "intentname",
    "name",
    "tool",
    "tool_name",
    "toolname",
    "id",
    "action",
)
_EXECUTE_ARGS_PROPS: tuple[str, ...] = (
    "arguments",
    "args",
    "params",
    "parameters",
    "input",
    "payload",
    "data",
)
_CONFIRM_TOKEN_PROPS: tuple[str, ...] = (
    "confirm_token",
    "confirmtoken",
    "approval_token",
    "approvaltoken",
    "approval_id",
    "approvalid",
    "confirm_id",
    "confirmid",
    "confirmation_token",
    "batch_token",
    "token",
    "id",
)
_CONFIRM_BATCH_LIST_PROPS: tuple[str, ...] = (
    "tokens",
    "confirm_tokens",
    "approval_tokens",
    "approval_ids",
    "confirm_ids",
    "ids",
)
_CONFIRM_BOOL_PROPS: tuple[str, ...] = ("confirm", "confirmed", "approve", "approved")
_META_SEARCH_HINTS = ("intent", "意图", "catalog", "目录")

FRIENDLINK_TOOL_KEYWORDS: tuple[str, ...] = (
    "friendlink",
    "friend_link",
    "友链",
    "友情链接",
)
_STRICT_FRIENDLINK_MARKERS: tuple[str, ...] = (
    "friendlink",
    "friend_link",
    "友链",
)

_AUTH_RPC_CODES = frozenset({401, 403, -32001, -32003})
_AUTH_ERROR_KEYWORDS: tuple[str, ...] = (
    "unauthorized",
    "unauthenticated",
    "invalid token",
    "token invalid",
    "token expired",
    "invalid bearer",
    "access denied",
    "未授权",
    "认证失败",
    "鉴权失败",
    "token无效",
    "token 无效",
    "token已过期",
    "token 已过期",
)


def _is_auth_rpc_error(code: int | None, message: str) -> bool:
    if code in _AUTH_RPC_CODES:
        return True
    lowered = (message or "").casefold()
    return any(keyword in lowered for keyword in _AUTH_ERROR_KEYWORDS)


def _parse_tool_info(raw: Any) -> McpToolInfo | None:
    if isinstance(raw, str):
        cleaned = raw.strip()
        return McpToolInfo(name=cleaned) if cleaned else None
    if not isinstance(raw, dict):
        return None
    name = ""
    for key in ("name", "intent", "intent_name", "intentName", "tool", "tool_name", "toolName", "id"):
        val = raw.get(key)
        if isinstance(val, str) and val.strip():
            name = val.strip()
            break
    if not name:
        return None
    desc_text = ""
    for key in ("description", "desc", "summary", "title", "label"):
        val = raw.get(key)
        if isinstance(val, str) and val:
            desc_text = val
            break
    title_val = raw.get("title")
    if isinstance(title_val, str) and title_val.strip():
        title_clean = title_val.strip()
        if desc_text and title_clean.casefold() not in desc_text.casefold():
            desc_text = f"{title_clean}：{desc_text}"
        elif not desc_text:
            desc_text = title_clean
    schema: dict[str, Any] = {}
    for key in ("inputSchema", "input_schema", "schema", "parameters", "params_schema", "paramsSchema"):
        candidate = raw.get(key)
        if isinstance(candidate, dict):
            schema = candidate
            break
    annotations = raw.get("annotations")
    ann_dict = annotations if isinstance(annotations, dict) else {}
    return McpToolInfo(
        name=name,
        description=desc_text,
        input_schema=dict(schema),
        annotations=dict(ann_dict),
    )


def _extract_catalog_items(result: McpToolResult) -> list[McpToolInfo]:
    payload: Any = result.structured
    if payload is None and isinstance(result.text, str) and result.text.strip():
        try:
            payload = json.loads(result.text)
        except (ValueError, TypeError):
            payload = None

    raw_items: list[Any] = []
    if isinstance(payload, list):
        raw_items = payload
    elif isinstance(payload, dict):
        queue: list[dict[str, Any]] = [payload]
        seen_ids: set[int] = set()
        while queue and not raw_items:
            current = queue.pop(0)
            if id(current) in seen_ids:
                continue
            seen_ids.add(id(current))
            lowered = {str(k).casefold(): v for k, v in current.items()}
            for key in _CATALOG_LIST_KEYS:
                candidate = lowered.get(key)
                if isinstance(candidate, list):
                    raw_items = candidate
                    break
            if raw_items:
                break
            for nested_key in _CATALOG_NESTED_KEYS:
                nested = lowered.get(nested_key)
                if isinstance(nested, dict):
                    queue.append(nested)
                elif isinstance(nested, list):
                    raw_items = nested
                    break

    parsed_tools: list[McpToolInfo] = []
    seen_names: set[str] = set()
    for item in raw_items:
        parsed = _parse_tool_info(item)
        if parsed is not None and parsed.name not in seen_names:
            seen_names.add(parsed.name)
            parsed_tools.append(parsed)
    return parsed_tools


def _decode_response_text(response: requests.Response) -> str:
    content = getattr(response, "content", None)
    if isinstance(content, (bytes, bytearray)):
        return bytes(content).decode("utf-8", errors="replace")
    text = getattr(response, "text", "")
    return text if isinstance(text, str) else ""


def _close_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _extract_tool_result(result: dict[str, Any]) -> McpToolResult:
    is_error = bool(result.get("isError", False))
    content = result.get("content")
    text_parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                part = item.get("text")
                if isinstance(part, str):
                    text_parts.append(part)
    text = "\n".join(text_parts)

    structured: object | None = None
    if "structuredContent" in result and result["structuredContent"] is not None:
        structured = result["structuredContent"]
    elif text.strip():
        try:
            structured = json.loads(text)
        except (ValueError, TypeError):
            structured = None

    return McpToolResult(
        is_error=is_error,
        text=text,
        structured=structured,
        raw=dict(result),
    )


class AnqiMcpClient:
    """MCP Streamable HTTP client implementing ``McpClientLike``."""

    def __init__(
        self,
        site_url: str,
        token: str,
        *,
        settings: McpSettings | None = None,
        session: requests.Session | None = None,
        client_name: str = APP_NAME,
        client_version: str = APP_VERSION,
    ) -> None:
        self._site_url = str(site_url or "").strip()
        self._token = str(token or "").strip()
        self._settings = settings if settings is not None else McpSettings()
        if session is None:
            self._session: requests.Session = requests.Session()
            self._owns_session = True
        else:
            self._session = session
            self._owns_session = False
        self._client_name = client_name
        self._client_version = client_version

        self.server_info: dict[str, Any] = {}
        self.protocol_version: str = ""
        self.session_id: str | None = None

        self._initialized: bool = False
        self._init_result: dict[str, Any] | None = None
        self._tools_cache: list[McpToolInfo] | None = None
        self._intents_cache: list[McpToolInfo] | None = None
        self._virtual_intents_by_name: dict[str, McpToolInfo] = {}
        self._catalog_tool: McpToolInfo | None = None
        self._intent_execute_tool: McpToolInfo | None = None
        self._next_id: int = 0
        self._closed: bool = False

    @classmethod
    def for_site(
        cls,
        site,
        settings: IntegrationSettings | None = None,
        session: requests.Session | None = None,
    ) -> "AnqiMcpClient":
        site_url = str(getattr(site, "url", "") or "").strip()
        if not site_url:
            raise McpUnavailableError("站点网址未配置")
        token = effective_mcp_token(site)
        if not token:
            raise McpAuthError("未配置 MCP Token，且发布接口中没有 token")
        if isinstance(settings, IntegrationSettings):
            mcp_settings = settings.mcp
        elif isinstance(settings, McpSettings):
            mcp_settings = settings
        else:
            mcp_settings = McpSettings()
        return cls(site_url, token, settings=mcp_settings, session=session)

    def __repr__(self) -> str:
        return (
            f"AnqiMcpClient(endpoint_url={self.endpoint_url!r}, "
            f"protocol_version={self.protocol_version!r}, "
            f"initialized={self._initialized!r})"
        )

    @property
    def endpoint_url(self) -> str:
        path = (self._settings.endpoint_path or "/api/mcp").strip()
        if not path.startswith("/"):
            path = "/" + path
        return f"{self._site_url.rstrip('/')}{path}"

    def _redact(self, text: str) -> str:
        secrets = (self._token,) if self._token else ()
        return redact_text(text, secrets)

    def _next_request_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _timeout_tuple(self, read_timeout: float | None = None) -> tuple[float, float]:
        connect = float(self._settings.connect_timeout)
        read = float(read_timeout if read_timeout is not None else self._settings.read_timeout)
        return (connect, read)

    def _build_headers(self, *, include_protocol: bool = True) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if include_protocol and self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _ensure_ready_for_transport(self) -> None:
        if not self._site_url:
            raise McpUnavailableError("站点网址未配置")
        if not self._token:
            raise McpAuthError("未配置 MCP Token，且发布接口中没有 token")

    def _post(
        self,
        payload: dict[str, Any],
        *,
        include_protocol: bool = True,
        timeout: float | None = None,
    ) -> requests.Response:
        self._ensure_ready_for_transport()
        try:
            return self._session.post(
                self.endpoint_url,
                json=payload,
                headers=self._build_headers(include_protocol=include_protocol),
                timeout=self._timeout_tuple(timeout),
                verify=self._settings.verify_ssl,
                allow_redirects=False,
                stream=True,
            )
        except requests.exceptions.SSLError as exc:
            raise McpUnavailableError(
                self._redact(f"MCP SSL 证书校验失败：{exc}"),
                hint=SSL_HINT,
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise McpUnavailableError(
                self._redact(f"MCP 请求超时：{exc}"),
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise McpUnavailableError(
                self._redact(f"无法连接 MCP 服务：{exc}"),
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise McpUnavailableError(
                self._redact(f"MCP 网络请求失败：{exc}"),
            ) from exc

    def _snippet(self, response: requests.Response) -> str:
        raw = _decode_response_text(response).strip()
        if not raw:
            return ""
        compact = " ".join(raw.split())
        if len(compact) > _ERROR_SNIPPET_LIMIT:
            compact = compact[:_ERROR_SNIPPET_LIMIT] + "…"
        return f"：{self._redact(compact)}"

    def _check_http_status(self, response: requests.Response, *, allowed_statuses: set[int] | None = None) -> None:
        status = int(response.status_code)
        if allowed_statuses is not None:
            if status in allowed_statuses:
                return
        elif 200 <= status < 300:
            return

        if status in (401, 403):
            raise McpAuthError(
                self._redact(f"MCP 认证失败（HTTP {status}）{self._snippet(response)}"),
                http_status=status,
            )
        if 300 <= status < 400:
            location = response.headers.get("Location", "")
            loc_suffix = f" → {self._redact(location)}" if location else ""
            raise McpUnavailableError(
                self._redact(f"MCP 接口发生重定向（HTTP {status}{loc_suffix}）"),
                http_status=status,
                hint=REDIRECT_HINT,
            )
        if status == 404:
            raise McpUnavailableError(
                self._redact(f"MCP 接口不存在或未开启（HTTP 404）{self._snippet(response)}"),
                http_status=404,
            )
        raise McpUnavailableError(
            self._redact(f"MCP 接口不可用（HTTP {status}）{self._snippet(response)}"),
            http_status=status,
        )

    def _parse_json_response(self, response: requests.Response, expected_id: int) -> dict[str, Any]:
        body = _decode_response_text(response)
        try:
            parsed = json.loads(body)
        except (ValueError, TypeError) as exc:
            raise McpProtocolError(
                self._redact(f"MCP 响应不是合法 JSON：{exc}"),
                http_status=int(response.status_code),
            ) from exc

        if isinstance(parsed, list):
            for entry in parsed:
                if isinstance(entry, dict) and entry.get("id") == expected_id and "method" not in entry:
                    return entry
            raise McpProtocolError(
                f"MCP 批量响应未包含匹配 ID({expected_id}) 的结果",
                http_status=int(response.status_code),
            )

        if not isinstance(parsed, dict):
            raise McpProtocolError(
                "MCP JSON 响应必须是对象",
                http_status=int(response.status_code),
            )

        resp_id = parsed.get("id")
        if resp_id is not None and resp_id != expected_id:
            raise McpProtocolError(
                f"MCP 响应 ID 不匹配：期望 {expected_id}，收到 {resp_id}",
                http_status=int(response.status_code),
            )
        if resp_id is None and "error" not in parsed:
            raise McpProtocolError(
                f"MCP 响应缺少匹配的 ID（期望 {expected_id}）",
                http_status=int(response.status_code),
            )
        return parsed

    def _parse_sse_response(self, response: requests.Response, expected_id: int) -> dict[str, Any]:
        data_lines: list[str] = []
        mismatched_id: Any = None

        def _consume_event(lines: list[str]) -> dict[str, Any] | None:
            nonlocal mismatched_id
            payload_text = "\n".join(lines).strip()
            if not payload_text:
                return None
            try:
                parsed = json.loads(payload_text)
            except (ValueError, TypeError) as exc:
                raise McpProtocolError(
                    self._redact(f"MCP SSE 事件包含无效 JSON：{exc}"),
                    http_status=int(response.status_code),
                ) from exc

            entries = parsed if isinstance(parsed, list) else [parsed]
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                # Ignore server notifications (no id) and server-to-client requests (has method).
                if "method" in entry:
                    continue
                if "id" not in entry or entry.get("id") is None:
                    if "error" in entry:
                        return entry
                    continue
                if entry.get("id") == expected_id:
                    return entry
                mismatched_id = entry.get("id")
            return None

        for raw_line in response.iter_lines(decode_unicode=False):
            if isinstance(raw_line, (bytes, bytearray)):
                line = bytes(raw_line).decode("utf-8", errors="replace")
            else:
                line = str(raw_line)
            if line.endswith("\r"):
                line = line[:-1]

            if line == "":
                if data_lines:
                    matched = _consume_event(data_lines)
                    data_lines = []
                    if matched is not None:
                        return matched
                continue

            if line.startswith(":"):
                continue

            field_name, sep, value = line.partition(":")
            if sep and field_name == "data":
                if value.startswith(" "):
                    value = value[1:]
                data_lines.append(value)

        if data_lines:
            matched = _consume_event(data_lines)
            if matched is not None:
                return matched

        if mismatched_id is not None:
            raise McpProtocolError(
                f"MCP SSE 响应 ID 不匹配：期望 {expected_id}，收到 {mismatched_id}",
                http_status=int(response.status_code),
            )
        raise McpProtocolError(
            "MCP SSE 流未返回匹配请求的响应",
            http_status=int(response.status_code),
        )

    def _decode_rpc_message(self, response: requests.Response, expected_id: int) -> dict[str, Any]:
        content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().casefold()
        if content_type == "text/event-stream":
            return self._parse_sse_response(response, expected_id)
        if content_type in ("application/json", "application/problem+json", ""):
            return self._parse_json_response(response, expected_id)
        raise McpProtocolError(
            self._redact(f"MCP 接口返回了不支持的 Content-Type：{content_type}"),
            http_status=int(response.status_code),
        )

    def _raise_rpc_error(self, error_obj: Any, *, is_tool_call: bool) -> None:
        if isinstance(error_obj, dict):
            raw_code = error_obj.get("code")
            code = raw_code if isinstance(raw_code, int) and not isinstance(raw_code, bool) else None
            raw_msg = str(error_obj.get("message") or "未知 JSON-RPC 错误")
            raw_data = error_obj.get("data")
            extra = ""
            if raw_data is not None and raw_data != "":
                extra = f"（{raw_data}）"
            combined = f"{raw_msg}{extra}"
        else:
            code = None
            combined = str(error_obj or "未知 JSON-RPC 错误")

        redacted = self._redact(combined)
        if is_tool_call:
            if classify_scope_error(combined):
                raise McpScopeError(redacted, code=code)
            raise McpToolError(redacted, code=code)
        if _is_auth_rpc_error(code, combined):
            raise McpAuthError(redacted, code=code)
        raise McpProtocolError(redacted, code=code)

    def _rpc_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        include_protocol: bool = True,
        timeout: float | None = None,
        is_tool_call: bool = False,
        allow_session_retry: bool = True,
        capture_session_id: bool = False,
    ) -> Any:
        req_id = self._next_request_id()
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        had_session = bool(self.session_id)
        response = self._post(payload, include_protocol=include_protocol, timeout=timeout)
        try:
            if int(response.status_code) == 404 and had_session and allow_session_retry:
                _close_response(response)
                response = None  # type: ignore[assignment]
                self._reset_session_state()
                self.initialize()
                return self._rpc_request(
                    method,
                    params,
                    include_protocol=include_protocol,
                    timeout=timeout,
                    is_tool_call=is_tool_call,
                    allow_session_retry=False,
                    capture_session_id=capture_session_id,
                )

            self._check_http_status(response)

            if capture_session_id:
                header_sid = response.headers.get("Mcp-Session-Id")
                if isinstance(header_sid, str) and header_sid.strip():
                    self.session_id = header_sid.strip()

            message = self._decode_rpc_message(response, req_id)
        finally:
            if response is not None:
                _close_response(response)

        if "error" in message and message["error"] is not None:
            self._raise_rpc_error(message["error"], is_tool_call=is_tool_call)

        if "result" not in message:
            raise McpProtocolError(f"MCP 响应缺少 result 字段（method={method}）")

        return message["result"]

    def _send_initialized_notification(self) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        }
        response = self._post(payload, include_protocol=True)
        try:
            self._check_http_status(response, allowed_statuses={200, 202, 204})
        finally:
            _close_response(response)

    def _reset_session_state(self) -> None:
        self.session_id = None
        self.protocol_version = ""
        self._initialized = False
        self._init_result = None

    def _protocol_candidates(self) -> list[str]:
        candidates: list[str] = []
        raw_versions = [self._settings.protocol_version, *self._settings.fallback_protocol_versions]
        for ver in raw_versions:
            if isinstance(ver, str) and ver.strip() and ver.strip() not in candidates:
                candidates.append(ver.strip())
        if not candidates:
            candidates.append("2025-06-18")
        return candidates

    def initialize(self) -> dict[str, Any]:
        """Perform MCP initialize handshake + ``notifications/initialized`` (idempotent)."""
        if self._initialized and self._init_result is not None:
            return dict(self._init_result)

        candidates = self._protocol_candidates()
        last_error: McpError | None = None

        for idx, candidate in enumerate(candidates):
            self._reset_session_state()
            params = {
                "protocolVersion": candidate,
                "capabilities": {},
                "clientInfo": {
                    "name": self._client_name,
                    "version": self._client_version,
                },
            }
            try:
                result = self._rpc_request(
                    "initialize",
                    params,
                    include_protocol=False,
                    allow_session_retry=False,
                    capture_session_id=True,
                )
            except McpProtocolError as exc:
                last_error = exc
                # Retry next fallback version when server rejects protocol params via JSON-RPC error.
                if idx + 1 < len(candidates) and exc.code is not None:
                    continue
                raise
            except McpUnavailableError as exc:
                last_error = exc
                # Some servers reject unsupported protocolVersion with HTTP 400.
                if idx + 1 < len(candidates) and exc.http_status == 400:
                    continue
                raise

            if not isinstance(result, dict):
                raise McpProtocolError("MCP initialize 返回的 result 必须是对象")

            negotiated = result.get("protocolVersion")
            self.protocol_version = (
                negotiated.strip()
                if isinstance(negotiated, str) and negotiated.strip()
                else candidate
            )
            server_info = result.get("serverInfo")
            self.server_info = dict(server_info) if isinstance(server_info, dict) else {}

            self._send_initialized_notification()
            self._initialized = True
            self._init_result = dict(result)
            return dict(self._init_result)

        assert last_error is not None
        raise last_error

    def list_tools(self, *, refresh: bool = False) -> list[McpToolInfo]:
        """List tools exposed by the server, following ``nextCursor`` up to 20 pages."""
        if not refresh and self._tools_cache is not None:
            return list(self._tools_cache)

        if not self._initialized:
            self.initialize()

        tools: list[McpToolInfo] = []
        seen_names: set[str] = set()
        seen_cursors: set[str] = set()
        cursor: str | None = None

        for _ in range(MAX_TOOL_PAGES):
            params: dict[str, Any] = {"cursor": cursor} if cursor else {}
            result = self._rpc_request("tools/list", params)
            if not isinstance(result, dict):
                raise McpProtocolError("MCP tools/list 返回的 result 必须是对象")
            raw_tools = result.get("tools")
            if not isinstance(raw_tools, list):
                raise McpProtocolError("MCP tools/list 响应缺少 tools 列表")
            for item in raw_tools:
                parsed = _parse_tool_info(item)
                if parsed is not None and parsed.name not in seen_names:
                    seen_names.add(parsed.name)
                    tools.append(parsed)

            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            if next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        self._tools_cache = list(tools)
        if refresh:
            self._intents_cache = None
            self._virtual_intents_by_name = {}
        self._refresh_meta_tools(self._tools_cache)
        return list(tools)

    def _refresh_meta_tools(self, tools: list[McpToolInfo]) -> None:
        exec_matches = score_tools(
            tools,
            _INTENT_EXECUTE_KEYWORDS,
            _INTENT_EXECUTE_EXPLICIT_NAMES,
        )
        execute_tool: McpToolInfo | None = None
        for candidate in exec_matches:
            folded = candidate.name.casefold()
            if folded in _INTENT_CATALOG_EXPLICIT_NAMES:
                continue
            execute_tool = candidate
            break
        self._intent_execute_tool = execute_tool

        catalog_pool = [
            t for t in tools if execute_tool is None or t.name != execute_tool.name
        ]
        catalog_matches = score_tools(
            catalog_pool,
            _INTENT_CATALOG_KEYWORDS,
            _INTENT_CATALOG_EXPLICIT_NAMES,
        )
        self._catalog_tool = catalog_matches[0] if catalog_matches else None

    @staticmethod
    def _build_catalog_args_candidates(catalog_tool: McpToolInfo) -> list[dict[str, Any]]:
        schema = catalog_tool.input_schema if isinstance(catalog_tool.input_schema, dict) else {}
        properties = schema.get("properties")
        props = properties if isinstance(properties, dict) else {}
        required_raw = schema.get("required")
        required = {str(r).casefold() for r in required_raw} if isinstance(required_raw, list) else set()

        query_prop: str | None = None
        for wanted in _CATALOG_QUERY_PROPS:
            for prop_name in props:
                if str(prop_name).casefold() == wanted:
                    query_prop = str(prop_name)
                    break
            if query_prop is not None:
                break

        candidates: list[dict[str, Any]] = []
        is_search_like = "search" in catalog_tool.name.casefold()
        if query_prop is not None and (query_prop.casefold() in required or is_search_like):
            candidates.append({query_prop: ""})
            candidates.append({query_prop: "*"})
            candidates.append({})
        else:
            candidates.append({})
            if query_prop is not None:
                candidates.append({query_prop: ""})
                candidates.append({query_prop: "*"})

        unique_candidates: list[dict[str, Any]] = []
        for cand in candidates:
            if cand not in unique_candidates:
                unique_candidates.append(cand)
        return unique_candidates

    def discover_intents(self, refresh: bool = False) -> list[McpToolInfo]:
        """Inspect ``list_tools()`` for an Intent Catalog meta-tool and cache virtual intents."""
        if not refresh and self._intents_cache is not None:
            return list(self._intents_cache)

        tools = self.list_tools(refresh=refresh)
        self._refresh_meta_tools(tools)

        if self._catalog_tool is None:
            self._intents_cache = []
            self._virtual_intents_by_name = {}
            return []

        discovered: list[McpToolInfo] = []
        for args in self._build_catalog_args_candidates(self._catalog_tool):
            try:
                res = self._invoke_tool_once(
                    self._catalog_tool.name,
                    args,
                    auto_confirm=False,
                )
            except (McpScopeError, McpApprovalRequiredError):
                break
            except McpToolError:
                continue
            discovered = _extract_catalog_items(res)
            break

        self._intents_cache = list(discovered)
        self._virtual_intents_by_name = {tool.name: tool for tool in discovered}
        return list(discovered)

    def list_disabled_intents(self, refresh: bool = False) -> list[McpToolInfo]:
        """Return catalog-only intents from ``mcp_list_intents`` not exposed in ``tools/list``.

        When an ``_intent_execute_tool`` (e.g. ``execute_intent``) exists, catalog
        items are executable virtual intents, so this returns ``[]``.
        """
        flat_tools = self.list_tools(refresh=refresh)
        if self._catalog_tool is None or self._intent_execute_tool is not None:
            return []
        catalog_items = self.discover_intents(refresh=False)
        exposed_folded = {tool.name.casefold() for tool in flat_tools}
        return [
            item
            for item in catalog_items
            if item.name.casefold() not in exposed_folded
        ]

    def has_disabled_intent(
        self,
        name_or_keywords: str | Iterable[str],
        *,
        refresh: bool = False,
    ) -> bool:
        """Return True if any unexposed catalog intent matches ``name_or_keywords``."""
        if isinstance(name_or_keywords, str):
            stripped = name_or_keywords.strip().casefold()
            needles = [stripped] if stripped else []
        else:
            needles = [
                item.strip().casefold()
                for item in (name_or_keywords or ())
                if isinstance(item, str) and item.strip()
            ]
        if not needles:
            return False
        disabled = self.list_disabled_intents(refresh=refresh)
        if not disabled:
            return False
        for intent in disabled:
            haystack = f"{intent.name}\n{intent.description}".casefold()
            if any(needle in haystack for needle in needles):
                return True
        return False

    def _is_meta_search_request(
        self,
        keywords: Iterable[str],
        explicit_names: Iterable[str],
    ) -> bool:
        for item in (*keywords, *explicit_names):
            if isinstance(item, str) and item.strip():
                folded = item.strip().casefold()
                if any(hint in folded for hint in _META_SEARCH_HINTS):
                    return True
        return False

    def find_tools(
        self,
        keywords: Iterable[str],
        *,
        explicit_names: Iterable[str] = (),
    ) -> list[McpToolInfo]:
        """Rank executable direct tools (and virtual intents when executable) via ``score_tools``."""
        flat_tools = self.list_tools()
        virtual_intents = (
            self.discover_intents()
            if (self._catalog_tool is not None and self._intent_execute_tool is not None)
            else []
        )

        explicit_set = {
            name.strip().casefold()
            for name in (explicit_names or ())
            if isinstance(name, str) and name.strip()
        }
        meta_names: set[str] = set()
        if not self._is_meta_search_request(keywords, explicit_names):
            if self._catalog_tool is not None and self._catalog_tool.name.casefold() not in explicit_set:
                meta_names.add(self._catalog_tool.name)
            if self._intent_execute_tool is not None and self._intent_execute_tool.name.casefold() not in explicit_set:
                meta_names.add(self._intent_execute_tool.name)

        searchable_flat = [tool for tool in flat_tools if tool.name not in meta_names]
        combined = [*searchable_flat, *virtual_intents]
        return score_tools(combined, keywords, explicit_names)

    def _lookup_virtual_intent(self, name: str) -> McpToolInfo | None:
        if name in self._virtual_intents_by_name:
            return self._virtual_intents_by_name[name]
        folded = name.casefold()
        for intent_name, info in self._virtual_intents_by_name.items():
            if intent_name.casefold() == folded:
                return info
        return None

    def _build_execute_intent_arguments(
        self,
        execute_tool: McpToolInfo,
        intent_name: str,
        arguments: dict | None,
    ) -> dict[str, Any]:
        payload_args = dict(arguments) if arguments is not None else {}
        schema = execute_tool.input_schema if isinstance(execute_tool.input_schema, dict) else {}
        properties = schema.get("properties")
        props = properties if isinstance(properties, dict) else {}

        name_key = "intent"
        for wanted in _EXECUTE_NAME_PROPS:
            matched = next((str(k) for k in props if str(k).casefold() == wanted), None)
            if matched is not None:
                name_key = matched
                break

        args_key = "arguments"
        for wanted in _EXECUTE_ARGS_PROPS:
            matched = next((str(k) for k in props if str(k).casefold() == wanted), None)
            if matched is not None:
                args_key = matched
                break

        return {
            name_key: intent_name,
            args_key: payload_args,
        }

    def _resolve_confirm_tool(self, returned_confirm_tool: str) -> McpToolInfo | None:
        if self._tools_cache is None:
            self.list_tools()
        if (
            self._intents_cache is None
            and self._catalog_tool is not None
            and self._intent_execute_tool is not None
        ):
            self.discover_intents()

        all_tools: list[McpToolInfo] = list(self._tools_cache or ())
        if self._intent_execute_tool is not None:
            seen = {t.name for t in all_tools}
            for intent_tool in (self._intents_cache or ()):
                if intent_tool.name not in seen:
                    all_tools.append(intent_tool)
                    seen.add(intent_tool.name)

        if returned_confirm_tool:
            folded = returned_confirm_tool.strip().casefold()
            exact = next((t for t in all_tools if t.name == returned_confirm_tool.strip()), None)
            if exact is not None:
                return exact
            ci_match = next((t for t in all_tools if t.name.casefold() == folded), None)
            if ci_match is not None:
                return ci_match

        for candidate_name in _CONFIRM_TOOL_EXPLICIT_NAMES:
            match = next((t for t in all_tools if t.name.casefold() == candidate_name), None)
            if match is not None:
                return match
        return None

    @staticmethod
    def _build_confirm_arguments(
        confirm_tool: McpToolInfo,
        token: str,
        token_key: str,
    ) -> dict[str, Any]:
        schema = confirm_tool.input_schema if isinstance(confirm_tool.input_schema, dict) else {}
        properties = schema.get("properties")
        props = properties if isinstance(properties, dict) else {}

        args: dict[str, Any] = {}
        if props:
            token_assigned = False
            if token:
                for wanted in _CONFIRM_TOKEN_PROPS:
                    for prop_name, prop_schema in props.items():
                        if str(prop_name).casefold() == wanted:
                            prop_type = prop_schema.get("type") if isinstance(prop_schema, dict) else None
                            if prop_type in (None, "string"):
                                args[str(prop_name)] = token
                                token_assigned = True
                                break
                    if token_assigned:
                        break
                if not token_assigned:
                    for wanted in _CONFIRM_BATCH_LIST_PROPS:
                        for prop_name, prop_schema in props.items():
                            if str(prop_name).casefold() == wanted:
                                prop_type = prop_schema.get("type") if isinstance(prop_schema, dict) else None
                                if prop_type in (None, "array"):
                                    args[str(prop_name)] = [token]
                                    token_assigned = True
                                    break
                        if token_assigned:
                            break
            for prop_name, prop_schema in props.items():
                folded = str(prop_name).casefold()
                prop_type = prop_schema.get("type") if isinstance(prop_schema, dict) else None
                if folded in _CONFIRM_BOOL_PROPS and prop_type in (None, "boolean"):
                    args[str(prop_name)] = True
                elif folded in {"decision", "action"} and prop_type in (None, "string"):
                    args[str(prop_name)] = "approve"
            if token and not token_assigned:
                args[token_key or "confirm_token"] = token
            return args

        if token:
            return {token_key or "confirm_token": token}
        return {"confirm": True}

    def _resolve_rpc_target(self, name: str, arguments: dict | None) -> tuple[str, dict[str, Any]]:
        direct_names = {t.name for t in self._tools_cache} if self._tools_cache is not None else set()
        if name not in direct_names:
            if (
                self._tools_cache is not None
                and self._intents_cache is None
                and self._catalog_tool is not None
                and self._intent_execute_tool is not None
            ):
                self.discover_intents()
            virtual = self._lookup_virtual_intent(name)
            if virtual is not None and self._intent_execute_tool is not None:
                return (
                    self._intent_execute_tool.name,
                    self._build_execute_intent_arguments(
                        self._intent_execute_tool,
                        virtual.name,
                        arguments,
                    ),
                )
        return name, dict(arguments) if arguments is not None else {}

    def _invoke_tool_once(
        self,
        name: str,
        arguments: dict | None = None,
        *,
        timeout: float | None = None,
        auto_confirm: bool = False,
    ) -> McpToolResult:
        if not self._initialized:
            self.initialize()

        rpc_name, rpc_args = self._resolve_rpc_target(name, arguments)
        params = {
            "name": rpc_name,
            "arguments": rpc_args,
        }
        result = self._rpc_request(
            "tools/call",
            params,
            timeout=timeout,
            is_tool_call=True,
        )
        if not isinstance(result, dict):
            raise McpProtocolError(f"MCP tools/call({name}) 返回的 result 必须是对象")

        tool_result = _extract_tool_result(result)
        required, token, returned_confirm_tool, token_key = detect_approval_details(tool_result)
        if required:
            if auto_confirm:
                confirm_tool_info = self._resolve_confirm_tool(returned_confirm_tool)
                if confirm_tool_info is not None:
                    confirm_args = self._build_confirm_arguments(
                        confirm_tool_info,
                        token,
                        token_key,
                    )
                    return self._invoke_tool_once(
                        confirm_tool_info.name,
                        confirm_args,
                        timeout=timeout,
                        auto_confirm=False,
                    )
            raw_msg = tool_result.text.strip() or f"工具 {name} 需要二次确认后执行"
            raise McpApprovalRequiredError(
                self._redact(raw_msg),
                approval_token=token,
                confirm_tool=returned_confirm_tool,
                payload=tool_result.structured if tool_result.structured is not None else tool_result.raw,
                result=tool_result,
                hint=APPROVAL_HINT,
            )

        inner_error = (
            isinstance(tool_result.structured, dict)
            and (
                tool_result.structured.get("isError") is True
                or tool_result.structured.get("is_error") is True
            )
        )
        if tool_result.is_error or inner_error:
            raw_error_text = tool_result.text.strip()
            if not raw_error_text and isinstance(tool_result.structured, dict):
                raw_error_text = str(
                    tool_result.structured.get("error")
                    or tool_result.structured.get("message")
                    or tool_result.structured.get("msg")
                    or ""
                ).strip()
            raw_error_text = raw_error_text or f"工具 {name} 执行失败"
            redacted_msg = self._redact(raw_error_text)
            if classify_scope_error(raw_error_text):
                raise McpScopeError(redacted_msg, result=tool_result)
            raise McpToolError(redacted_msg, result=tool_result)

        return tool_result

    def call_tool(
        self,
        name: str,
        arguments: dict | None = None,
        *,
        timeout: float | None = None,
        auto_confirm: bool = False,
    ) -> McpToolResult:
        """Invoke a direct tool or discovered virtual intent; handle turn-level approval."""
        return self._invoke_tool_once(
            name,
            arguments,
            timeout=timeout,
            auto_confirm=auto_confirm,
        )

    def ping(self) -> bool:
        """Send JSON-RPC ``ping``; returns ``True`` on valid reply, ``False`` on MCP error."""
        try:
            if not self._initialized:
                self.initialize()
            self._rpc_request("ping", {})
            return True
        except McpError:
            return False

    def close(self) -> None:
        """Best-effort HTTP DELETE to terminate session, then close owned session."""
        if self._closed:
            return
        self._closed = True

        session_id = self.session_id
        self.session_id = None
        if session_id and self._site_url and self._token:
            headers = {
                "Authorization": f"Bearer {self._token}",
                "Mcp-Session-Id": session_id,
            }
            if self.protocol_version:
                headers["MCP-Protocol-Version"] = self.protocol_version
            response = None
            try:
                response = self._session.delete(
                    self.endpoint_url,
                    headers=headers,
                    timeout=(min(float(self._settings.connect_timeout), 2.0), min(float(self._settings.read_timeout), 5.0)),
                    verify=self._settings.verify_ssl,
                    allow_redirects=False,
                )
            except Exception:
                pass
            finally:
                if response is not None:
                    _close_response(response)

        if self._owns_session:
            try:
                self._session.close()
            except Exception:
                pass

    def __enter__(self) -> "AnqiMcpClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def probe_site(
    site,
    settings: IntegrationSettings | None = None,
    *,
    session_factory: Callable[[], requests.Session] = requests.Session,
) -> McpProbeResult:
    """Probe a site's MCP endpoint and classify discovered tools. Never raises."""
    site_name = str(getattr(site, "name", "") or "")
    effective_settings = settings if isinstance(settings, IntegrationSettings) else IntegrationSettings()
    redactor = SecretRedactor.from_sites([site])

    session: requests.Session | None = None
    client: AnqiMcpClient | None = None
    try:
        session = session_factory()
        client = AnqiMcpClient.for_site(site, effective_settings, session=session)
        client.initialize()
        tools = client.list_tools(refresh=True)
        has_execute_tool = client._intent_execute_tool is not None
        virtual_intents = client.discover_intents() if has_execute_tool else []
        all_tools: list[McpToolInfo] = []
        seen_names: set[str] = set()
        for tool in (*tools, *virtual_intents):
            if tool.name not in seen_names:
                seen_names.add(tool.name)
                all_tools.append(tool)

        disabled_intents: list[McpToolInfo] = []
        if client._catalog_tool is not None and not has_execute_tool:
            try:
                disabled_intents = client.list_disabled_intents()
            except McpError:
                disabled_intents = []

        fl_candidates = client.find_tools(FRIENDLINK_TOOL_KEYWORDS)
        strict_friendlink = [
            tool
            for tool in fl_candidates
            if any(
                marker in tool.name.casefold()
                or any(marker in action.casefold() for action in tool_action_enum(tool))
                for marker in _STRICT_FRIENDLINK_MARKERS
            )
        ]
        friendlink_tools = tuple(
            tool.name for tool in (strict_friendlink if strict_friendlink else fl_candidates)
        )

        upgrade_keywords = (
            *effective_settings.upgrade.check_keywords,
            *effective_settings.upgrade.upgrade_keywords,
        )
        upgrade_explicit = (
            *effective_settings.upgrade.check_tool_names,
            *effective_settings.upgrade.upgrade_tool_names,
        )
        upgrade_tools = tuple(
            tool.name
            for tool in client.find_tools(upgrade_keywords, explicit_names=upgrade_explicit)
            if is_upgrade_candidate_tool(
                tool,
                explicit_names=upgrade_explicit,
                include_check_tools=True,
            )
        )
        jump_explicit = effective_settings.admin_jump.ticket_tool_names
        jump_tools = tuple(
            tool.name
            for tool in client.find_tools(
                effective_settings.admin_jump.ticket_keywords,
                explicit_names=jump_explicit,
            )
            if is_jump_candidate_tool(tool, explicit_names=jump_explicit)
        )

        server_name = str(client.server_info.get("name") or "")
        server_version = str(client.server_info.get("version") or "")
        message = (
            f"连接成功，发现 {len(all_tools)} 个可用工具（另有 {len(disabled_intents)} 个未开放意图）"
            if disabled_intents
            else f"连接成功，发现 {len(all_tools)} 个工具"
        )
        return McpProbeResult(
            site_name=site_name,
            ok=True,
            server_name=server_name,
            server_version=server_version,
            protocol_version=client.protocol_version,
            tool_count=len(all_tools),
            tools=tuple(all_tools),
            friendlink_tools=friendlink_tools,
            upgrade_tools=upgrade_tools,
            jump_tools=jump_tools,
            message=message,
            hint="",
        )
    except McpError as exc:
        return McpProbeResult(
            site_name=site_name,
            ok=False,
            message=redactor(str(exc)),
            hint=exc.hint,
        )
    except Exception as exc:
        return McpProbeResult(
            site_name=site_name,
            ok=False,
            message=redactor(f"MCP 探测失败：{exc}"),
            hint="",
        )
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


def _emit_probe_progress(
    progress: Callable[..., None],
    site: Any,
    result: McpProbeResult,
    completed: int,
    total: int,
) -> None:
    try:
        sig = inspect.signature(progress)
    except (TypeError, ValueError):
        progress(result)
        return
    params = list(sig.parameters.values())
    if any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params):
        progress(result)
        return
    positional_count = sum(
        1
        for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    )
    if positional_count == 2:
        progress(site, result)
    elif positional_count >= 3:
        progress(completed, total, result)
    else:
        progress(result)


def probe_sites_batch(
    sites: Iterable[Any],
    settings: IntegrationSettings | None = None,
    *,
    max_workers: int = 10,
    session_factory: Callable[[], requests.Session] = requests.Session,
    progress: Callable[..., None] | None = None,
    cancel_event: threading.Event | None = None,
) -> list[McpProbeResult]:
    """Probe multiple sites concurrently via MCP, preserving input order."""
    site_list = list(sites)
    if not site_list:
        return []

    try:
        requested = int(max_workers)
    except (TypeError, ValueError):
        requested = 10
    workers = max(1, min(requested, len(site_list)))

    total = len(site_list)
    results: list[McpProbeResult | None] = [None] * total
    progress_lock = threading.Lock()
    completed = 0

    def _worker(index: int, site: Any) -> None:
        nonlocal completed
        site_name = str(getattr(site, "name", "") or "")
        if cancel_event is not None and cancel_event.is_set():
            res = McpProbeResult(
                site_name=site_name,
                ok=False,
                message="已取消",
            )
        else:
            res = probe_site(site, settings, session_factory=session_factory)
        results[index] = res
        if progress is not None:
            with progress_lock:
                completed += 1
                done_count = completed
            _emit_probe_progress(progress, site, res, done_count, total)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(_worker, idx, site)
            for idx, site in enumerate(site_list)
        ]
        for future in futures:
            future.result()

    return [item for item in results if item is not None]
