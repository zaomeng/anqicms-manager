from __future__ import annotations

import pytest

from core import mcp_contracts as mc
from core.mcp_contracts import (
    SCOPE_KEYWORDS,
    McpAuthError,
    McpClientLike,
    McpError,
    McpProtocolError,
    McpScopeError,
    McpToolError,
    McpUnavailableError,
    classify_scope_error,
    score_tools,
)
from models.sitegroup import McpToolInfo, McpToolResult


def tool(name, description=""):
    return McpToolInfo(name=name, description=description)


# ------------------------------------------------------------- exceptions
def test_hierarchy():
    assert issubclass(McpAuthError, McpError)
    assert issubclass(McpUnavailableError, McpError)
    assert issubclass(McpProtocolError, McpError)
    assert issubclass(McpToolError, McpError)
    assert issubclass(McpScopeError, McpToolError)
    assert not issubclass(McpAuthError, McpToolError)
    assert issubclass(McpError, Exception)


def test_base_error_fields():
    err = McpError("boom", code=-32000, http_status=500, hint="h")
    assert str(err) == "boom"
    assert (err.code, err.http_status, err.hint) == (-32000, 500, "h")
    plain = McpError("x")
    assert (plain.code, plain.http_status, plain.hint) == (None, None, "")


def test_auth_error_default_and_custom_hint():
    err = McpAuthError("401", http_status=401)
    assert err.hint == mc.AUTH_HINT
    assert "设置→安全设置" in err.hint and "AI 统一配置" in err.hint
    assert McpAuthError("x", hint="custom").hint == "custom"


def test_scope_error_default_hint_and_result():
    result = McpToolResult(is_error=True, text="未开放")
    err = McpScopeError("scope", result=result)
    assert err.result is result
    assert err.hint == mc.SCOPE_HINT
    assert "接口开放范围" in err.hint and "系统升级" in err.hint
    assert McpScopeError("x", hint="mine").hint == "mine"


def test_tool_error_result_optional():
    err = McpToolError("failed", code=-1)
    assert err.result is None and err.code == -1 and err.hint == ""
    assert not isinstance(err, McpScopeError)


def test_unavailable_and_protocol_have_no_default_hint():
    assert McpUnavailableError("down").hint == ""
    assert McpUnavailableError("down", hint=mc.REDIRECT_HINT).hint == mc.REDIRECT_HINT
    assert "https://" in mc.REDIRECT_HINT
    assert "证书校验失败" in mc.SSL_HINT
    assert McpProtocolError("bad json").hint == ""


def test_errors_can_be_caught_via_base():
    for cls in (McpAuthError, McpUnavailableError, McpProtocolError, McpToolError, McpScopeError):
        with pytest.raises(McpError):
            raise cls("x")


# --------------------------------------------------------- scope classification
def test_scope_keywords_exact_set():
    assert SCOPE_KEYWORDS == (
        "开放范围", "未开放", "未开启", "无权", "权限", "高危",
        "scope", "permission", "forbidden", "not allowed", "disabled",
    )


@pytest.mark.parametrize(
    "text",
    [
        "该接口未在开放范围内",
        "系统升级域未开放",
        "功能未开启",
        "无权调用",
        "权限不足",
        "高危操作被禁用",
        "Scope 'system' is not enabled",
        "Permission denied",
        "403 FORBIDDEN",
        "Operation Not Allowed",
        "tool is DISABLED",
    ],
)
def test_classify_scope_error_positive(text):
    assert classify_scope_error(text) is True


@pytest.mark.parametrize(
    "text", ["", "连接超时", "database error: table missing", "invalid argument: url", "OK"]
)
def test_classify_scope_error_negative(text):
    assert classify_scope_error(text) is False


def test_classify_scope_error_non_string():
    assert classify_scope_error(None) is False
    assert classify_scope_error(123) is False


# ------------------------------------------------------------- protocol
def test_client_like_protocol_runtime_check():
    class Good:
        def initialize(self): return {}
        def list_tools(self, *, refresh=False): return []
        def find_tools(self, keywords, *, explicit_names=()): return []
        def call_tool(self, name, arguments=None, *, timeout=None): return McpToolResult(False, "")
        def close(self): return None

    class Bad:
        def initialize(self): return {}

    assert isinstance(Good(), McpClientLike)
    assert not isinstance(Bad(), McpClientLike)
    assert mc.McpClientFactory is not None


# ----------------------------------------------------------- score_tools
def names(tools):
    return [t.name for t in tools]


def test_explicit_names_first_in_given_order():
    tools = [tool("upgrade_check"), tool("system_upgrade"), tool("other")]
    ranked = score_tools(tools, ["upgrade"], explicit_names=["other", "system_upgrade"])
    assert names(ranked) == ["other", "system_upgrade", "upgrade_check"]


def test_explicit_name_case_insensitive_fallback_and_missing_ignored():
    tools = [tool("System_Upgrade"), tool("x")]
    assert names(score_tools(tools, [], explicit_names=["system_upgrade", "ghost", "", None])) == ["System_Upgrade"]


def test_explicit_exact_preferred_over_case_variant():
    tools = [tool("Foo"), tool("foo")]
    assert names(score_tools(tools, [], explicit_names=["foo"])) == ["foo"]


def test_name_hit_outweighs_description_hit():
    tools = [
        tool("alpha", "performs upgrade of the system"),
        tool("system_upgrade", "does something"),
    ]
    assert names(score_tools(tools, ["upgrade"])) == ["system_upgrade", "alpha"]


def test_score_accumulates_across_keywords_and_fields():
    tools = [
        tool("version_info", "check update"),            # 3 + 0 (update hit in desc: 1) -> version name 3, update desc 1 = 4
        tool("check_update", "show the version"),         # check_update name hit 3 (+ 'update'?)...
        tool("misc", "version"),                          # 1
    ]
    ranked = score_tools(tools, ["version", "update"])
    # version_info: name(version)=3, desc(update)=1 => 4
    # check_update: name(update)=3, desc(version)=1 => 4  (tie -> by name)
    # misc: desc(version)=1
    assert names(ranked) == ["check_update", "version_info", "misc"]


def test_case_insensitive_and_chinese_keywords():
    tools = [tool("SYSTEM_UPGRADE"), tool("工具", "执行 升级 操作"), tool("nope", "nothing")]
    ranked = score_tools(tools, ["Upgrade", "升级"])
    assert names(ranked) == ["SYSTEM_UPGRADE", "工具"]


def test_zero_score_excluded_and_empty_inputs():
    tools = [tool("a", "b")]
    assert score_tools(tools, ["zzz"]) == []
    assert score_tools(tools, []) == []
    assert score_tools([], ["a"]) == []
    assert score_tools(tools, ["", "  ", None]) == []
    assert score_tools(None, None) == []


def test_ties_broken_by_name():
    tools = [tool("b_upgrade"), tool("a_upgrade"), tool("c_upgrade")]
    assert names(score_tools(tools, ["upgrade"])) == ["a_upgrade", "b_upgrade", "c_upgrade"]


def test_duplicate_keywords_not_double_counted():
    tools = [tool("x_upgrade"), tool("y", "upgrade upgrade")]
    assert names(score_tools(tools, ["upgrade", "UPGRADE"])) == ["x_upgrade", "y"]


def test_duplicate_tool_names_deduped_and_explicit_not_repeated():
    tools = [tool("a_upgrade"), tool("a_upgrade", "again")]
    assert names(score_tools(tools, ["upgrade"], explicit_names=["a_upgrade"])) == ["a_upgrade"]


def test_accepts_generators_and_does_not_mutate_input():
    tools = [tool("b_upgrade"), tool("a_upgrade")]
    ranked = score_tools((t for t in tools), (k for k in ["upgrade"]), (n for n in ()))
    assert names(ranked) == ["a_upgrade", "b_upgrade"]
    assert names(tools) == ["b_upgrade", "a_upgrade"]
