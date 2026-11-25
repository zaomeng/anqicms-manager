"""Contracts shared by the MCP client and every service that consumes it.

Wave-2 services (friend links, admin jump, upgrade) code against
``McpClientLike`` / ``McpClientFactory`` and the exception hierarchy below, so
they can be developed and tested with fakes, independent of the real client.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Iterable, Protocol, runtime_checkable

from models.entities import SiteConfig
from models.sitegroup import McpToolInfo, McpToolResult

AUTH_HINT = "MCP Token 无效或未开启：后台 设置→安全设置 开启 API，设置→AI 统一配置 生成 Token"
SCOPE_HINT = "请登录该站后台：设置→AI 统一配置→接口开放范围，勾选对应高危域（系统升级/多站点）后重试"
REDIRECT_HINT = "接口发生重定向，请把站点网址改为 https://… 形式（含正确的 http/https）"
SSL_HINT = "证书校验失败：请检查站点证书，或在 integration_settings.json 中关闭 mcp.verify_ssl"
APPROVAL_HINT = (
    "该操作触发了服务端二次确认：请通过确认工具（如 tool_confirm_batch）提交确认令牌，"
    "或启用 auto_confirm=True 后重试；若站点未开放确认工具，请登录站点后台手动操作"
)

SCOPE_KEYWORDS = (
    "开放范围",
    "未开放",
    "未开启",
    "无权",
    "权限",
    "高危",
    "scope",
    "permission",
    "forbidden",
    "not allowed",
    "disabled",
)


class McpError(Exception):
    """Base class; ``str(exc)`` is the (already redacted) message."""

    default_hint = ""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        http_status: int | None = None,
        hint: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status
        self.hint = hint or self.default_hint


class McpAuthError(McpError):
    """401/403 or missing token."""

    default_hint = AUTH_HINT


class McpUnavailableError(McpError):
    """Connect/timeout/SSL error, 404/405 or 3xx."""


class McpProtocolError(McpError):
    """Invalid JSON/SSE, id mismatch or missing result."""


class McpToolError(McpError):
    """``isError`` result or JSON-RPC error on ``tools/call``."""

    def __init__(
        self,
        message: str,
        *,
        result: McpToolResult | None = None,
        code: int | None = None,
        http_status: int | None = None,
        hint: str = "",
    ) -> None:
        super().__init__(message, code=code, http_status=http_status, hint=hint)
        self.result = result


class McpScopeError(McpToolError):
    """The tool exists but its (high-risk) scope is not opened on the site."""

    default_hint = SCOPE_HINT


class McpApprovalRequiredError(McpToolError):
    """Raised when an AnqiCMS v3.6.5+ tool call requires turn-level approval."""

    default_hint = APPROVAL_HINT

    def __init__(
        self,
        message: str,
        *,
        approval_token: str = "",
        confirm_tool: str = "",
        payload: Any = None,
        result: McpToolResult | None = None,
        code: int | None = None,
        http_status: int | None = None,
        hint: str = "",
    ) -> None:
        super().__init__(
            message,
            result=result,
            code=code,
            http_status=http_status,
            hint=hint,
        )
        self.approval_token = str(approval_token or "")
        self.confirm_tool = str(confirm_tool or "")
        self.payload = payload


_APPROVAL_FLAG_KEYS = frozenset(
    {
        "pendingapproval",
        "requiresconfirmation",
        "requireconfirmation",
        "requiresconfirm",
        "needconfirm",
        "needsconfirmation",
        "confirmationrequired",
        "approvalrequired",
        "needsapproval",
    }
)
_APPROVAL_STATUS_PENDING = frozenset(
    {
        "pendingapproval",
        "requiresconfirmation",
        "requireconfirmation",
        "awaitingapproval",
        "confirmationrequired",
        "approvalrequired",
    }
)
_APPROVAL_STATUS_COMPLETED = frozenset(
    {
        "confirmed",
        "approved",
        "completed",
        "done",
        "succeeded",
        "success",
    }
)
_APPROVAL_TOKEN_KEYS: tuple[str, ...] = (
    "confirmtoken",
    "approvaltoken",
    "approvalid",
    "confirmid",
    "confirmationtoken",
    "batchtoken",
    "turntoken",
)
_CONFIRM_TOOL_KEYS = frozenset(
    {
        "confirmtool",
        "confirmationtool",
        "approvaltool",
        "nexttool",
    }
)
_ACTION_OR_TOOL_KEYS = frozenset({"tool", "action", "nextaction", "nextstep"})
_CONFIRM_BATCH_TOOL_VALUES = frozenset({"toolconfirmbatch", "confirmbatch"})

_TEXT_TOKEN_RE = re.compile(
    r"(confirm_token|approval_token|approval_id|confirm_id|confirmation_token|"
    r"confirmToken|approvalToken|approvalId|confirmId)\s*[:=]\s*[\"']?([A-Za-z0-9._:/-]+)[\"']?",
)
_TEXT_CONFIRM_TOOL_RE = re.compile(
    r"(?:confirm_tool|approval_tool|confirmTool|approvalTool)\s*[:=]\s*[\"']?([A-Za-z0-9_.-]+)[\"']?",
)
_TEXT_TOOL_CONFIRM_BATCH_RE = re.compile(r"\b(tool_confirm_batch)\b", re.IGNORECASE)
_TEXT_APPROVAL_MARKER_RE = re.compile(
    r"\b(tool_confirm_batch|pending_approval|requires_confirmation|require_confirmation|"
    r"confirmation_required|approval_required|awaiting_approval|pendingApproval|"
    r"requiresConfirmation|confirm_token|approval_token|approval_id)\b",
    re.IGNORECASE,
)


def _norm_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).casefold())


def _is_truthy_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    if isinstance(value, str):
        return value.strip().casefold() in {"true", "1", "yes", "y", "pending", "required"}
    if isinstance(value, (dict, list, tuple)):
        return len(value) > 0
    return False


def _is_falsy_flag(value: Any) -> bool:
    if value is False or value == 0:
        return True
    if isinstance(value, str):
        return value.strip().casefold() in {"false", "0", "no", "n"}
    return False


def detect_approval_details(result: McpToolResult | None) -> tuple[bool, str, str, str]:
    """Detect AnqiCMS v3.6.5+ turn-level approval requirement.

    Returns ``(required, approval_token, confirm_tool, token_key)``.
    """
    if result is None:
        return False, "", "", ""

    text = result.text if isinstance(getattr(result, "text", None), str) else ""
    structured = getattr(result, "structured", None)
    if structured is None and text.strip()[:1] in ("{", "["):
        try:
            structured = json.loads(text)
        except (ValueError, TypeError):
            structured = None
    if structured is None and not text.strip():
        raw = getattr(result, "raw", None)
        if isinstance(raw, dict):
            structured = raw

    explicitly_completed = False
    explicitly_false_pending = False
    has_pending_flag = False
    token = ""
    token_key = ""
    confirm_tool = ""

    if isinstance(structured, (dict, list)):
        queue: list[Any] = [structured]
        seen_ids: set[int] = set()
        while queue:
            current = queue.pop(0)
            obj_id = id(current)
            if obj_id in seen_ids:
                continue
            seen_ids.add(obj_id)

            if isinstance(current, list):
                for item in current:
                    if isinstance(item, (dict, list)):
                        queue.append(item)
                continue

            if not isinstance(current, dict):
                continue

            norm_entries: list[tuple[str, str, Any]] = []
            for orig_key, val in current.items():
                if isinstance(val, (dict, list)):
                    queue.append(val)
                norm_k = _norm_key(orig_key)
                norm_entries.append((str(orig_key), norm_k, val))

            for _, norm_k, val in norm_entries:
                if norm_k in {"status", "state"} and isinstance(val, str):
                    norm_v = _norm_key(val)
                    if norm_v in _APPROVAL_STATUS_COMPLETED:
                        explicitly_completed = True
                    elif norm_v in _APPROVAL_STATUS_PENDING:
                        has_pending_flag = True
                elif norm_k in {"confirmed", "approved"} and val is True:
                    explicitly_completed = True
                elif norm_k in _APPROVAL_FLAG_KEYS:
                    if _is_falsy_flag(val):
                        explicitly_false_pending = True
                    elif _is_truthy_flag(val):
                        has_pending_flag = True
                elif norm_k in {"error", "code", "reason", "type"} and isinstance(val, str):
                    if _norm_key(val) in _APPROVAL_STATUS_PENDING:
                        has_pending_flag = True
                elif norm_k in _CONFIRM_TOOL_KEYS and isinstance(val, str) and val.strip():
                    if not confirm_tool:
                        confirm_tool = val.strip()
                elif norm_k in _ACTION_OR_TOOL_KEYS and isinstance(val, str):
                    if _norm_key(val) in _CONFIRM_BATCH_TOOL_VALUES:
                        if not confirm_tool:
                            confirm_tool = val.strip()
                        has_pending_flag = True

            if not token:
                for wanted_norm in _APPROVAL_TOKEN_KEYS:
                    for orig_key, norm_k, val in norm_entries:
                        if norm_k == wanted_norm and val is not None and not isinstance(val, bool):
                            val_text = str(val).strip()
                            if val_text:
                                token = val_text
                                token_key = orig_key
                                break
                    if token:
                        break

    if text and not explicitly_completed:
        if not confirm_tool:
            batch_match = _TEXT_TOOL_CONFIRM_BATCH_RE.search(text)
            if batch_match:
                confirm_tool = batch_match.group(1)
            else:
                tool_match = _TEXT_CONFIRM_TOOL_RE.search(text)
                if tool_match:
                    confirm_tool = tool_match.group(1)
        if not token:
            tok_match = _TEXT_TOKEN_RE.search(text)
            if tok_match:
                token_key = tok_match.group(1)
                token = tok_match.group(2)
        if not has_pending_flag and _TEXT_APPROVAL_MARKER_RE.search(text):
            has_pending_flag = True

    required = (not explicitly_completed) and (
        has_pending_flag
        or (bool(token) and not explicitly_false_pending)
        or bool(confirm_tool)
    )
    if not required:
        return False, "", "", ""
    return True, token, confirm_tool, token_key


def detect_approval_required(result: McpToolResult | None) -> tuple[bool, str, str]:
    """Return ``(required, approval_token, confirm_tool)`` for a tool result."""
    required, token, confirm_tool, _ = detect_approval_details(result)
    return required, token, confirm_tool


def classify_scope_error(text: str) -> bool:
    """True when a tool error text looks like a closed-scope/permission denial."""
    if not isinstance(text, str) or not text:
        return False
    lowered = text.casefold()
    return any(keyword.casefold() in lowered for keyword in SCOPE_KEYWORDS)


@runtime_checkable
class McpClientLike(Protocol):
    def initialize(self) -> dict: ...

    def list_tools(self, *, refresh: bool = False) -> list[McpToolInfo]: ...

    def find_tools(
        self,
        keywords: Iterable[str],
        *,
        explicit_names: Iterable[str] = (),
    ) -> list[McpToolInfo]: ...

    def call_tool(
        self,
        name: str,
        arguments: dict | None = None,
        *,
        timeout: float | None = None,
    ) -> McpToolResult: ...

    def close(self) -> None: ...


McpClientFactory = Callable[[SiteConfig], McpClientLike]

_JUMP_EXPLICIT_MARKERS: tuple[str, ...] = (
    "ticket",
    "jump",
    "sso",
    "one_time",
    "switch_site",
    "票据",
    "跳转",
    "免密",
)
_JUMP_EXACT_ACTIONS = frozenset({"login", "admin_login", "sso_login"})
_JUMP_LOG_OR_READONLY_MARKERS: tuple[str, ...] = (
    "logs_",
    "_logs",
    "log_",
    "_log",
    "_list",
    "list_",
    "_info",
    "info_",
    "query",
    "get_",
)
_READONLY_NAME_MARKERS: tuple[str, ...] = (
    "check",
    "info",
    "version",
    "status",
    "list",
    "get",
    "query",
    "detect",
    "检查",
    "查询",
    "查看",
    "获取",
    "版本",
    "状态",
)
_UPGRADE_ACTION_MARKERS: tuple[str, ...] = (
    "upgrade",
    "update_system",
    "system_update",
    "升级",
)
_PROBE_UPGRADE_ACTION_MARKERS: tuple[str, ...] = (
    *_UPGRADE_ACTION_MARKERS,
    "check_update",
    "upgrade_check",
)


def tool_action_enum(tool: McpToolInfo | None) -> tuple[str, ...]:
    """Return allowed ``action`` enum strings declared in ``tool.input_schema``."""
    if tool is None:
        return ()
    schema = getattr(tool, "input_schema", None)
    if not isinstance(schema, dict):
        return ()
    props = schema.get("properties")
    if not isinstance(props, dict):
        return ()
    action_prop = props.get("action")
    if not isinstance(action_prop, dict):
        action_prop = next(
            (v for k, v in props.items() if isinstance(k, str) and k.casefold() == "action"),
            None,
        )
    if not isinstance(action_prop, dict):
        return ()
    raw_enum = action_prop.get("enum")
    if not isinstance(raw_enum, (list, tuple)):
        return ()
    return tuple(item.strip() for item in raw_enum if isinstance(item, str) and item.strip())


def is_jump_candidate_tool(
    tool: McpToolInfo,
    *,
    explicit_names: Iterable[str] = (),
) -> bool:
    """Return True if ``tool`` is plausibly an admin login/ticket jump tool."""
    explicit_folded = {
        item.strip().casefold()
        for item in (explicit_names or ())
        if isinstance(item, str) and item.strip()
    }
    name_folded = tool.name.casefold()
    if name_folded in explicit_folded:
        return True

    annotations = tool.annotations if isinstance(getattr(tool, "annotations", None), dict) else {}
    if annotations.get("readOnlyHint") is True:
        return False

    actions = tuple(a.casefold() for a in tool_action_enum(tool))
    if actions:
        has_jump_action = any(
            (
                any(marker in action for marker in _JUMP_EXPLICIT_MARKERS)
                or action in _JUMP_EXACT_ACTIONS
            )
            and not any(bad in action for bad in _JUMP_LOG_OR_READONLY_MARKERS)
            for action in actions
        )
        if not has_jump_action:
            return False

    desc_folded = (tool.description or "").casefold()
    name_and_desc = f"{name_folded} {desc_folded}"
    if not any(marker in name_and_desc for marker in _JUMP_EXPLICIT_MARKERS):
        looks_like_log_or_list = (
            any(bad in name_folded for bad in ("log", "logs", "audit", "list", "info", "account"))
            or any(bad in desc_folded for bad in ("日志", "审计"))
        )
        if looks_like_log_or_list:
            return False
    return True


def is_upgrade_candidate_tool(
    tool: McpToolInfo,
    *,
    explicit_names: Iterable[str] = (),
    include_check_tools: bool = False,
) -> bool:
    """Return True if ``tool`` is a valid upgrade (or check-update) tool."""
    explicit_folded = {
        item.strip().casefold()
        for item in (explicit_names or ())
        if isinstance(item, str) and item.strip()
    }
    name_folded = tool.name.casefold()
    if name_folded in explicit_folded:
        return True

    if name_folded == "system_config":
        return False

    if not include_check_tools and any(marker in name_folded for marker in _READONLY_NAME_MARKERS):
        return False

    actions = tuple(a.casefold() for a in tool_action_enum(tool))
    if actions:
        markers = _PROBE_UPGRADE_ACTION_MARKERS if include_check_tools else _UPGRADE_ACTION_MARKERS
        if any(marker in action for action in actions for marker in markers):
            return True
        if include_check_tools and ("check" in actions or "update" in actions):
            desc_folded = (tool.description or "").casefold()
            if any(m in f"{name_folded} {desc_folded}" for m in ("upgrade", "update", "升级", "更新")):
                return True
        return False

    return True


def score_tools(
    tools: Iterable[McpToolInfo],
    keywords: Iterable[str],
    explicit_names: Iterable[str] = (),
) -> list[McpToolInfo]:
    """Rank ``tools`` for a capability.

    1. Tools whose name exactly equals an explicit name come first, in the
       order the names were given (exact match preferred, then case-insensitive).
    2. The rest are scored ``name-hit * 3 + action-enum-hit * 2 + description-hit * 1``
       per keyword (case-insensitive substring); only score > 0 is kept; ties by name.
    """
    unique: dict[str, McpToolInfo] = {}
    for tool in tools or ():
        unique.setdefault(tool.name, tool)
    pool = list(unique.values())

    ordered: list[McpToolInfo] = []
    taken: set[str] = set()
    for wanted in explicit_names or ():
        if not isinstance(wanted, str) or not wanted.strip():
            continue
        wanted = wanted.strip()
        match = unique.get(wanted)
        if match is None:
            folded = wanted.casefold()
            match = next((t for t in pool if t.name.casefold() == folded), None)
        if match is not None and match.name not in taken:
            ordered.append(match)
            taken.add(match.name)

    needles: list[str] = []
    for keyword in keywords or ():
        if isinstance(keyword, str) and keyword.strip():
            folded = keyword.strip().casefold()
            if folded not in needles:
                needles.append(folded)

    scored: list[tuple[int, str, McpToolInfo]] = []
    for tool in pool:
        if tool.name in taken:
            continue
        name = tool.name.casefold()
        actions_text = " ".join(tool_action_enum(tool)).casefold()
        description = (tool.description or "").casefold()
        score = 0
        for needle in needles:
            if needle in name:
                score += 3
            if actions_text and needle in actions_text:
                score += 2
            if needle in description:
                score += 1
        if score > 0:
            scored.append((score, tool.name, tool))
    scored.sort(key=lambda entry: (-entry[0], entry[1]))
    return ordered + [entry[2] for entry in scored]
