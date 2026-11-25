from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest
import requests

from core.anqicms_mcp_client import AnqiMcpClient, probe_site, probe_sites_batch
from core.integration_settings import IntegrationSettings
from core.mcp_contracts import (
    APPROVAL_HINT,
    McpApprovalRequiredError,
    McpError,
    McpScopeError,
    McpToolError,
    detect_approval_required,
)
from core.upgrade_service import UpgradeService
from models.entities import SiteConfig
from models.sitegroup import (
    UPGRADE_FAILED,
    UPGRADE_UPGRADED,
    McpProbeResult,
    McpToolInfo,
    McpToolResult,
)


class _FakeMcpServer:
    """In-process 127.0.0.1 HTTP server for hermetic MCP v2 tests."""

    def __init__(
        self,
        handler: Callable[[str, str, dict[str, str], bytes], tuple[int, dict[str, str], bytes]],
    ) -> None:
        self.handler = handler
        self.requests_log: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        parent = self

        class _RequestHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                return

            def _dispatch(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length > 0 else b""
                headers = {k.lower(): v for k, v in self.headers.items()}
                with parent._lock:
                    parent.requests_log.append(
                        {
                            "method": method,
                            "path": self.path,
                            "headers": headers,
                            "body": body,
                        }
                    )
                status, resp_headers, resp_body = parent.handler(method, self.path, headers, body)
                self.send_response(status)
                has_length = any(k.lower() == "content-length" for k in resp_headers)
                for key, value in resp_headers.items():
                    self.send_header(key, value)
                if not has_length:
                    self.send_header("Content-Length", str(len(resp_body)))
                self.send_header("Connection", "close")
                self.end_headers()
                if resp_body:
                    self.wfile.write(resp_body)
                self.close_connection = True

            def do_GET(self) -> None:
                self._dispatch("GET")

            def do_POST(self) -> None:
                self._dispatch("POST")

            def do_DELETE(self) -> None:
                self._dispatch("DELETE")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _RequestHandler)
        self.base_url = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )

    def __enter__(self) -> "_FakeMcpServer":
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)


def _json_resp(
    payload: Any,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    merged = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        merged.update(headers)
    return status, merged, body


def _make_site(
    name: str = "demo",
    *,
    site_id: str = "",
    url: str = "http://127.0.0.1:9999",
    mcp_token: str = "tok_v2_secret_1234",
) -> SiteConfig:
    return SiteConfig(
        name=name,
        fid=1,
        url=url,
        jiekou="",
        publish_dir=Path("."),
        daily_publish_filename=Path("daily.txt"),
        publish_time="08:00",
        interval_time=60,
        daily_publish_limit=10,
        site_id=site_id or f"id-{name}",
        mcp_token=mcp_token,
    )


class _FakeClock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start
        self.sleeps: list[float] = []
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        with self._lock:
            return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.sleeps.append(seconds)
            self.now += seconds


# --------------------------------------------------------------------------- #
# Contracts: McpApprovalRequiredError & detect_approval_required
# --------------------------------------------------------------------------- #
class TestApprovalContracts:
    def test_approval_error_hierarchy_and_fields(self) -> None:
        assert issubclass(McpApprovalRequiredError, McpToolError)
        assert issubclass(McpApprovalRequiredError, McpError)
        assert not issubclass(McpApprovalRequiredError, McpScopeError)

        res = McpToolResult(is_error=False, text="needs approval")
        err = McpApprovalRequiredError(
            "需要二次确认",
            approval_token="tok-123",
            confirm_tool="tool_confirm_batch",
            payload={"pending_approval": True},
            result=res,
        )
        assert str(err) == "需要二次确认"
        assert err.approval_token == "tok-123"
        assert err.confirm_tool == "tool_confirm_batch"
        assert err.payload == {"pending_approval": True}
        assert err.result is res
        assert err.hint == APPROVAL_HINT

        custom = McpApprovalRequiredError("x", hint="自定义确认提示")
        assert custom.hint == "自定义确认提示"

    def test_detect_approval_required_structured_nested_and_text(self) -> None:
        res_struct = McpToolResult(
            is_error=False,
            text="",
            structured={
                "pending_approval": True,
                "confirm_token": "ctok-1",
                "confirm_tool": "tool_confirm_batch",
            },
        )
        assert detect_approval_required(res_struct) == (True, "ctok-1", "tool_confirm_batch")

        res_nested = McpToolResult(
            is_error=False,
            text="",
            structured={
                "code": 0,
                "data": {
                    "requiresConfirmation": True,
                    "approvalId": "ap-99",
                },
            },
        )
        assert detect_approval_required(res_nested) == (True, "ap-99", "")

        res_json_text = McpToolResult(
            is_error=False,
            text='{"status": "pending_approval", "approval_token": "atok-7", "action": "tool_confirm_batch"}',
        )
        assert detect_approval_required(res_json_text) == (True, "atok-7", "tool_confirm_batch")

        res_plain_text = McpToolResult(
            is_error=True,
            text="requires_confirmation: please invoke tool_confirm_batch with approval_id=ap_plain_1",
        )
        assert detect_approval_required(res_plain_text) == (True, "ap_plain_1", "tool_confirm_batch")

    def test_detect_approval_required_negative_and_completed_responses(self) -> None:
        assert detect_approval_required(None) == (False, "", "")
        assert detect_approval_required(
            McpToolResult(
                is_error=False,
                text="",
                structured={"current_version": "3.6.7", "latest_version": "3.6.8"},
            )
        ) == (False, "", "")
        assert detect_approval_required(
            McpToolResult(is_error=True, text="系统升级未在接口开放范围内")
        ) == (False, "", "")
        # Response from confirm tool itself must NOT re-trigger approval detection
        assert detect_approval_required(
            McpToolResult(
                is_error=False,
                text="",
                structured={"status": "confirmed", "approved": True, "confirm_token": "ctok-done"},
            )
        ) == (False, "", "")


# --------------------------------------------------------------------------- #
# Intent Catalog discovery & transparent execution
# --------------------------------------------------------------------------- #
class TestIntentCatalog:
    def test_discover_intents_find_tools_and_transparent_routing(self) -> None:
        rpc_calls: list[tuple[str, dict[str, Any]]] = []
        catalog_queries = 0

        def handler(method: str, path: str, headers: dict[str, str], body: bytes):
            nonlocal catalog_queries
            if method == "DELETE":
                return 204, {}, b""
            data = json.loads(body.decode("utf-8"))
            rpc_method = data["method"]
            if rpc_method == "initialize":
                return _json_resp(
                    {
                        "jsonrpc": "2.0",
                        "id": data["id"],
                        "result": {
                            "protocolVersion": "2025-06-18",
                            "serverInfo": {"name": "anqicms-mcp", "version": "3.6.5"},
                        },
                    },
                    headers={"Mcp-Session-Id": "intent-sess"},
                )
            if rpc_method == "notifications/initialized":
                return 202, {}, b""
            if rpc_method == "tools/list":
                return _json_resp(
                    {
                        "jsonrpc": "2.0",
                        "id": data["id"],
                        "result": {
                            "tools": [
                                {
                                    "name": "intent_search",
                                    "description": "搜索 AnqiCMS 意图目录",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {"query": {"type": "string"}},
                                        "required": ["query"],
                                    },
                                },
                                {
                                    "name": "execute_intent",
                                    "description": "执行意图目录工具（支持友链、系统升级、登录跳转）",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {
                                            "intent_name": {"type": "string"},
                                            "params": {"type": "object"},
                                        },
                                    },
                                },
                                {
                                    "name": "site_ping",
                                    "description": "直接平铺工具",
                                },
                            ]
                        },
                    }
                )
            if rpc_method == "tools/call":
                name = data["params"]["name"]
                args = data["params"].get("arguments") or {}
                rpc_calls.append((name, args))
                if name == "intent_search":
                    catalog_queries += 1
                    assert "query" in args
                    return _json_resp(
                        {
                            "jsonrpc": "2.0",
                            "id": data["id"],
                            "result": {
                                "isError": False,
                                "structuredContent": {
                                    "intents": [
                                        {
                                            "name": "system_check_update",
                                            "description": "检查 AnqiCMS 版本更新",
                                            "inputSchema": {"type": "object", "properties": {}},
                                        },
                                        {
                                            "name": "system_upgrade",
                                            "description": "执行系统升级",
                                            "inputSchema": {
                                                "type": "object",
                                                "properties": {
                                                    "version": {"type": "string"},
                                                    "confirm": {"type": "boolean"},
                                                },
                                            },
                                        },
                                        {
                                            "name": "friendlink_create",
                                            "description": "新增友情链接",
                                        },
                                    ]
                                },
                            },
                        }
                    )
                if name == "execute_intent":
                    target_intent = args.get("intent_name")
                    target_args = args.get("params")
                    if target_intent == "system_check_update":
                        return _json_resp(
                            {
                                "jsonrpc": "2.0",
                                "id": data["id"],
                                "result": {
                                    "isError": False,
                                    "structuredContent": {
                                        "current_version": "3.6.5",
                                        "latest_version": "3.6.8",
                                    },
                                },
                            }
                        )
                    if target_intent == "system_upgrade":
                        return _json_resp(
                            {
                                "jsonrpc": "2.0",
                                "id": data["id"],
                                "result": {
                                    "isError": False,
                                    "content": [{"type": "text", "text": f"upgraded:{target_args.get('version')}"}],
                                },
                            }
                        )
                if name == "site_ping":
                    return _json_resp(
                        {
                            "jsonrpc": "2.0",
                            "id": data["id"],
                            "result": {"isError": False, "content": [{"type": "text", "text": "pong"}]},
                        }
                    )
            raise AssertionError(f"Unexpected RPC: {data}")

        with _FakeMcpServer(handler) as srv:
            with AnqiMcpClient(srv.base_url, "tok_v2_secret_1234") as client:
                intents1 = client.discover_intents()
                intents2 = client.discover_intents()
                assert [t.name for t in intents1] == [
                    "system_check_update",
                    "system_upgrade",
                    "friendlink_create",
                ]
                assert intents1 == intents2
                assert catalog_queries == 1

                # Refresh forces re-query
                client.discover_intents(refresh=True)
                assert catalog_queries == 2

                # find_tools includes virtual intents and excludes execute_intent meta-tool
                up_tools = client.find_tools(["upgrade", "升级"])
                assert [t.name for t in up_tools] == ["system_upgrade"]
                check_and_up = client.find_tools(["check_update", "upgrade", "升级"])
                assert [t.name for t in check_and_up] == ["system_upgrade", "system_check_update"]

                # Searching for friendlink does NOT return execute_intent even though description mentions 友链
                fl_tools = client.find_tools(["friendlink", "友链"])
                assert [t.name for t in fl_tools] == ["friendlink_create"]

                # Calling a virtual intent transparently routes via execute_intent with schema keys
                res = client.call_tool("system_upgrade", {"version": "3.6.8", "confirm": True})
                assert res.text == "upgraded:3.6.8"
                assert rpc_calls[-1] == (
                    "execute_intent",
                    {"intent_name": "system_upgrade", "params": {"version": "3.6.8", "confirm": True}},
                )

                # Calling a direct flat tool does NOT route through execute_intent
                ping_res = client.call_tool("site_ping")
                assert ping_res.text == "pong"
                assert rpc_calls[-1] == ("site_ping", {})

            # probe_site classifies virtual intents too
            site = _make_site("v365-site", url=srv.base_url)
            probe = probe_site(site, IntegrationSettings())
            assert probe.ok is True
            assert probe.server_version == "3.6.5"
            assert "system_upgrade" in probe.upgrade_tools
            assert "friendlink_create" in probe.friendlink_tools

    def test_custom_catalog_and_execute_names_via_descriptions(self) -> None:
        def handler(method: str, path: str, headers: dict[str, str], body: bytes):
            if method == "DELETE":
                return 204, {}, b""
            data = json.loads(body.decode("utf-8"))
            m = data["method"]
            if m == "initialize":
                return _json_resp({"jsonrpc": "2.0", "id": data["id"], "result": {"protocolVersion": "2025-06-18"}})
            if m == "notifications/initialized":
                return 202, {}, b""
            if m == "tools/list":
                return _json_resp(
                    {
                        "jsonrpc": "2.0",
                        "id": data["id"],
                        "result": {
                            "tools": [
                                {"name": "catalog_list", "description": "列出意图目录"},
                                {"name": "run_intent", "description": "执行意图"},
                            ]
                        },
                    }
                )
            if m == "tools/call":
                name = data["params"]["name"]
                args = data["params"].get("arguments") or {}
                if name == "catalog_list":
                    return _json_resp(
                        {
                            "jsonrpc": "2.0",
                            "id": data["id"],
                            "result": {
                                "isError": False,
                                "content": [
                                    {
                                        "type": "text",
                                        "text": json.dumps(
                                            {"data": {"items": [{"intent": "admin_login_ticket", "desc": "生成后台登录票据"}]}}
                                        ),
                                    }
                                ],
                            },
                        }
                    )
                if name == "run_intent":
                    assert args["intent"] == "admin_login_ticket"
                    return _json_resp(
                        {
                            "jsonrpc": "2.0",
                            "id": data["id"],
                            "result": {"isError": False, "structuredContent": {"ticket": "t-88"}},
                        }
                    )
            raise AssertionError(data)

        with _FakeMcpServer(handler) as srv:
            with AnqiMcpClient(srv.base_url, "tok_v2_secret_1234") as client:
                found = client.find_tools(["ticket", "登录"])
                assert [t.name for t in found] == ["admin_login_ticket"]
                res = client.call_tool("admin_login_ticket", {"ttl": 60})
                assert res.structured == {"ticket": "t-88"}


# --------------------------------------------------------------------------- #
# Turn-level Approval & tool_confirm_batch
# --------------------------------------------------------------------------- #
class TestTurnLevelApproval:
    def test_approval_required_raises_when_auto_confirm_false_and_confirms_once_when_true(self) -> None:
        calls: list[tuple[str, dict[str, Any]]] = []
        secret = "tok_bearer_secret_9876"

        def handler(method: str, path: str, headers: dict[str, str], body: bytes):
            if method == "DELETE":
                return 204, {}, b""
            data = json.loads(body.decode("utf-8"))
            m = data["method"]
            if m == "initialize":
                return _json_resp({"jsonrpc": "2.0", "id": data["id"], "result": {"protocolVersion": "2025-06-18"}})
            if m == "notifications/initialized":
                return 202, {}, b""
            if m == "tools/list":
                return _json_resp(
                    {
                        "jsonrpc": "2.0",
                        "id": data["id"],
                        "result": {
                            "tools": [
                                {"name": "system_upgrade", "description": "升级系统"},
                                {
                                    "name": "tool_confirm_batch",
                                    "description": "确认高危批量操作",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {
                                            "approval_ids": {"type": "array"},
                                            "approve": {"type": "boolean"},
                                        },
                                    },
                                },
                            ]
                        },
                    }
                )
            if m == "tools/call":
                name = data["params"]["name"]
                args = data["params"].get("arguments") or {}
                calls.append((name, args))
                if name == "system_upgrade":
                    return _json_resp(
                        {
                            "jsonrpc": "2.0",
                            "id": data["id"],
                            "result": {
                                "isError": False,
                                "content": [{"type": "text", "text": f"需要确认操作 Bearer {secret}"}],
                                "structuredContent": {
                                    "pending_approval": True,
                                    "approval_id": "ap-token-42",
                                    "confirm_tool": "tool_confirm_batch",
                                },
                            },
                        }
                    )
                if name == "tool_confirm_batch":
                    assert args == {"approval_ids": ["ap-token-42"], "approve": True}
                    return _json_resp(
                        {
                            "jsonrpc": "2.0",
                            "id": data["id"],
                            "result": {
                                "isError": False,
                                "structuredContent": {"status": "confirmed", "upgraded": True},
                            },
                        }
                    )
            raise AssertionError(data)

        with _FakeMcpServer(handler) as srv:
            with AnqiMcpClient(srv.base_url, secret) as client:
                with pytest.raises(McpApprovalRequiredError) as exc_info:
                    client.call_tool("system_upgrade", {"version": "3.6.8"}, auto_confirm=False)
                err = exc_info.value
                assert err.approval_token == "ap-token-42"
                assert err.confirm_tool == "tool_confirm_batch"
                assert secret not in str(err)
                assert "确认" in err.hint
                assert calls == [("system_upgrade", {"version": "3.6.8"})]

                calls.clear()
                res = client.call_tool("system_upgrade", {"version": "3.6.8"}, auto_confirm=True)
                assert res.structured == {"status": "confirmed", "upgraded": True}
                assert calls == [
                    ("system_upgrade", {"version": "3.6.8"}),
                    ("tool_confirm_batch", {"approval_ids": ["ap-token-42"], "approve": True}),
                ]

    def test_auto_confirm_true_raises_when_confirm_tool_not_exposed(self) -> None:
        def handler(method: str, path: str, headers: dict[str, str], body: bytes):
            if method == "DELETE":
                return 204, {}, b""
            data = json.loads(body.decode("utf-8"))
            m = data["method"]
            if m == "initialize":
                return _json_resp({"jsonrpc": "2.0", "id": data["id"], "result": {"protocolVersion": "2025-06-18"}})
            if m == "notifications/initialized":
                return 202, {}, b""
            if m == "tools/list":
                return _json_resp(
                    {
                        "jsonrpc": "2.0",
                        "id": data["id"],
                        "result": {"tools": [{"name": "system_upgrade", "description": "升级系统"}]},
                    }
                )
            if m == "tools/call":
                return _json_resp(
                    {
                        "jsonrpc": "2.0",
                        "id": data["id"],
                        "result": {
                            "isError": False,
                            "structuredContent": {
                                "requires_confirmation": True,
                                "confirm_token": "tok-missing-confirm",
                            },
                        },
                    }
                )
            raise AssertionError(data)

        with _FakeMcpServer(handler) as srv:
            with AnqiMcpClient(srv.base_url, "tok_v2_secret_1234") as client:
                with pytest.raises(McpApprovalRequiredError) as exc_info:
                    client.call_tool("system_upgrade", {}, auto_confirm=True)
                assert exc_info.value.approval_token == "tok-missing-confirm"
                assert "确认" in exc_info.value.hint


# --------------------------------------------------------------------------- #
# probe_sites_batch concurrency, ordering & cancel
# --------------------------------------------------------------------------- #
class TestProbeSitesBatch:
    def test_probe_sites_batch_concurrent_and_preserves_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lock = threading.Lock()
        active = 0
        peak_active = 0
        barrier_hit = threading.Event()

        def fake_probe_site(
            site: SiteConfig,
            settings: IntegrationSettings | None = None,
            *,
            session_factory: Callable[[], requests.Session] = requests.Session,
        ) -> McpProbeResult:
            nonlocal active, peak_active
            with lock:
                active += 1
                peak_active = max(peak_active, active)
                if active >= 3:
                    barrier_hit.set()
            # Earlier sites sleep slightly longer to verify input order is preserved
            if site.name == "s0":
                barrier_hit.wait(timeout=1.0)
                time.sleep(0.03)
            else:
                time.sleep(0.01)
            with lock:
                active -= 1
            return McpProbeResult(site_name=site.name, ok=True, message=f"ok:{site.name}")

        monkeypatch.setattr("core.anqicms_mcp_client.probe_site", fake_probe_site)

        sites = [_make_site(f"s{i}", url=f"http://127.0.0.1:{9000 + i}") for i in range(6)]
        progress_1arg: list[str] = []
        results = probe_sites_batch(
            sites,
            max_workers=4,
            progress=lambda res: progress_1arg.append(res.site_name),
        )
        assert [r.site_name for r in results] == [f"s{i}" for i in range(6)]
        assert peak_active >= 2
        assert peak_active <= 4
        assert len(progress_1arg) == 6

        # 2-arg and 3-arg progress callbacks & cancel_event
        progress_3arg: list[tuple[int, int, str]] = []
        cancel = threading.Event()
        cancel.set()
        cancelled_results = probe_sites_batch(
            sites[:3],
            cancel_event=cancel,
            progress=lambda done, total, res: progress_3arg.append((done, total, res.message)),
        )
        assert [r.message for r in cancelled_results] == ["已取消"] * 3
        assert progress_3arg == [(1, 3, "已取消"), (2, 3, "已取消"), (3, 3, "已取消")]

        progress_2arg: list[tuple[str, str]] = []
        res_2arg = probe_sites_batch(
            sites[:2],
            progress=lambda s, r: progress_2arg.append((s.name, r.site_name)),
        )
        assert len(res_2arg) == 2
        assert sorted(progress_2arg) == [("s0", "s0"), ("s1", "s1")]
        assert probe_sites_batch([]) == []


# --------------------------------------------------------------------------- #
# UpgradeService P0/P1 enhancements
# --------------------------------------------------------------------------- #
@dataclass
class _FakeUpgradeClient:
    tools: list[McpToolInfo] = field(default_factory=list)
    call_handler: Callable[[str, dict | None, float | None, bool], McpToolResult] | None = None
    calls: list[tuple[str, dict | None, float | None, bool]] = field(default_factory=list)
    closed: bool = False

    def initialize(self) -> dict:
        return {"protocolVersion": "2025-06-18"}

    def list_tools(self, *, refresh: bool = False) -> list[McpToolInfo]:
        return list(self.tools)

    def find_tools(self, keywords, *, explicit_names=()) -> list[McpToolInfo]:
        from core.mcp_contracts import score_tools
        return score_tools(self.tools, keywords, explicit_names)

    def call_tool(
        self,
        name: str,
        arguments: dict | None = None,
        *,
        timeout: float | None = None,
        auto_confirm: bool = False,
    ) -> McpToolResult:
        self.calls.append((name, arguments, timeout, auto_confirm))
        if self.call_handler is None:
            return McpToolResult(is_error=False, text="")
        return self.call_handler(name, arguments, timeout, auto_confirm)

    def close(self) -> None:
        self.closed = True


class TestUpgradeServiceV2:
    def test_check_versions_supports_high_concurrency_up_to_20(self) -> None:
        sites = [_make_site(f"s{i}", url=f"https://s{i}.example.com") for i in range(24)]
        lock = threading.Lock()
        active = 0
        peak_active = 0
        reached_high = threading.Event()

        def factory(site: SiteConfig) -> _FakeUpgradeClient:
            def handler(name: str, args: dict | None, timeout: float | None, auto_confirm: bool) -> McpToolResult:
                nonlocal active, peak_active
                with lock:
                    active += 1
                    peak_active = max(peak_active, active)
                    if active >= 12:
                        reached_high.set()
                reached_high.wait(timeout=1.0)
                time.sleep(0.01)
                with lock:
                    active -= 1
                return McpToolResult(
                    is_error=False,
                    text="",
                    structured={"current_version": "3.6.7", "latest_version": "3.6.8"},
                )

            return _FakeUpgradeClient(
                tools=[McpToolInfo(name="check_update")],
                call_handler=handler,
            )

        service = UpgradeService(IntegrationSettings(), mcp_client_factory=factory)
        results = service.check_versions(sites, max_workers=30)
        assert [r.site_name for r in results] == [f"s{i}" for i in range(24)]
        assert 12 <= peak_active <= 20

    def test_check_versions_default_concurrency_is_10(self) -> None:
        sites = [_make_site(f"s{i}", url=f"https://s{i}.example.com") for i in range(14)]
        lock = threading.Lock()
        active = 0
        peak_active = 0
        reached_default = threading.Event()

        def factory(site: SiteConfig) -> _FakeUpgradeClient:
            def handler(name: str, args: dict | None, timeout: float | None, auto_confirm: bool) -> McpToolResult:
                nonlocal active, peak_active
                with lock:
                    active += 1
                    peak_active = max(peak_active, active)
                    if active >= 8:
                        reached_default.set()
                reached_default.wait(timeout=1.0)
                time.sleep(0.01)
                with lock:
                    active -= 1
                return McpToolResult(
                    is_error=False,
                    text="",
                    structured={"current_version": "3.6.7", "latest_version": "3.6.8"},
                )

            return _FakeUpgradeClient(
                tools=[McpToolInfo(name="check_update")],
                call_handler=handler,
            )

        service = UpgradeService(IntegrationSettings(), mcp_client_factory=factory)
        results = service.check_versions(sites)
        assert len(results) == 14
        assert 8 <= peak_active <= 10

    def test_check_version_no_check_tool_clearly_states_mcp_connected(self) -> None:
        site = _make_site("alpha", url="https://alpha.example.com")
        client = _FakeUpgradeClient(
            tools=[McpToolInfo(name="article_publish", description="发布文章")]
        )
        service = UpgradeService(IntegrationSettings(), mcp_client_factory=lambda s: client)

        info = service.check_version(site)
        assert info.reachable is True
        assert info.source == "mcp"
        assert info.check_tool == ""
        assert info.current_version == ""
        assert info.latest_version == ""
        assert info.upgrade_available is None
        assert "MCP 已连接" in info.message
        assert "未找到版本检测工具" in info.message

    def test_upgrade_site_passes_auto_confirm_true_and_fast_fails_on_approval_error(self) -> None:
        site = _make_site("alpha", url="https://alpha.example.com")
        clock = _FakeClock()
        tools = [McpToolInfo(name="check_update"), McpToolInfo(name="system_upgrade")]

        # 1. McpApprovalRequiredError raised (e.g. missing tool_confirm_batch) -> fast fail, zero sleeps
        seen_auto_confirm: list[bool] = []

        def approval_fail_handler(
            name: str,
            args: dict | None,
            timeout: float | None,
            auto_confirm: bool,
        ) -> McpToolResult:
            if name == "check_update":
                return McpToolResult(
                    is_error=False,
                    text="",
                    structured={"current_version": "3.6.7", "latest_version": "3.6.8"},
                )
            seen_auto_confirm.append(auto_confirm)
            raise McpApprovalRequiredError(
                "需要二次确认",
                approval_token="tok-1",
                confirm_tool="tool_confirm_batch",
            )

        service = UpgradeService(
            IntegrationSettings(),
            mcp_client_factory=lambda s: _FakeUpgradeClient(tools=tools, call_handler=approval_fail_handler),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        result = service.upgrade_site(site, confirmed=True)
        assert seen_auto_confirm == [True]
        assert result.status == UPGRADE_FAILED
        assert "确认" in result.message
        assert "tool_confirm_batch" in result.hint
        assert clock.sleeps == []

        # 2. Raw McpToolResult with pending_approval=True (e.g. from custom fake) also fails fast
        clock2 = _FakeClock()

        def raw_pending_handler(
            name: str,
            args: dict | None,
            timeout: float | None,
            auto_confirm: bool,
        ) -> McpToolResult:
            if name == "check_update":
                return McpToolResult(
                    is_error=False,
                    text="",
                    structured={"current_version": "3.6.7", "latest_version": "3.6.8"},
                )
            return McpToolResult(
                is_error=False,
                text="",
                structured={"pending_approval": True, "confirm_token": "tok-raw"},
            )

        service2 = UpgradeService(
            IntegrationSettings(),
            mcp_client_factory=lambda s: _FakeUpgradeClient(tools=tools, call_handler=raw_pending_handler),
            sleep=clock2.sleep,
            monotonic=clock2.monotonic,
        )
        result2 = service2.upgrade_site(site, confirmed=True)
        assert result2.status == UPGRADE_FAILED
        assert "确认" in result2.message
        assert clock2.sleeps == []

    def test_upgrade_site_end_to_end_with_intent_catalog_and_auto_confirm(self) -> None:
        upgraded = {"done": False}
        calls: list[str] = []

        def handler(method: str, path: str, headers: dict[str, str], body: bytes):
            if method == "GET":
                return 200, {"Content-Type": "text/html"}, b"ok"
            if method == "DELETE":
                return 204, {}, b""
            data = json.loads(body.decode("utf-8"))
            m = data["method"]
            if m == "initialize":
                return _json_resp({"jsonrpc": "2.0", "id": data["id"], "result": {"protocolVersion": "2025-06-18"}})
            if m == "notifications/initialized":
                return 202, {}, b""
            if m == "tools/list":
                return _json_resp(
                    {
                        "jsonrpc": "2.0",
                        "id": data["id"],
                        "result": {
                            "tools": [
                                {"name": "intent_search", "description": "意图目录"},
                                {"name": "execute_intent", "description": "执行意图"},
                                {
                                    "name": "tool_confirm_batch",
                                    "description": "确认操作",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {"confirm_token": {"type": "string"}},
                                    },
                                },
                            ]
                        },
                    }
                )
            if m == "tools/call":
                name = data["params"]["name"]
                args = data["params"].get("arguments") or {}
                calls.append(name)
                if name == "intent_search":
                    return _json_resp(
                        {
                            "jsonrpc": "2.0",
                            "id": data["id"],
                            "result": {
                                "isError": False,
                                "structuredContent": {
                                    "intents": [
                                        {"name": "check_update", "description": "检查版本更新"},
                                        {
                                            "name": "system_upgrade",
                                            "description": "执行系统升级",
                                            "inputSchema": {
                                                "type": "object",
                                                "properties": {
                                                    "version": {"type": "string"},
                                                    "confirm": {"type": "boolean"},
                                                },
                                            },
                                        },
                                    ]
                                },
                            },
                        }
                    )
                if name == "execute_intent":
                    intent = args.get("intent")
                    if intent == "check_update":
                        ver = "3.6.8" if upgraded["done"] else "3.6.7"
                        return _json_resp(
                            {
                                "jsonrpc": "2.0",
                                "id": data["id"],
                                "result": {
                                    "isError": False,
                                    "structuredContent": {"current_version": ver, "latest_version": "3.6.8"},
                                },
                            }
                        )
                    if intent == "system_upgrade":
                        return _json_resp(
                            {
                                "jsonrpc": "2.0",
                                "id": data["id"],
                                "result": {
                                    "isError": False,
                                    "structuredContent": {
                                        "pending_approval": True,
                                        "confirm_token": "e2e-confirm-tok",
                                    },
                                },
                            }
                        )
                if name == "tool_confirm_batch":
                    assert args == {"confirm_token": "e2e-confirm-tok"}
                    upgraded["done"] = True
                    return _json_resp(
                        {
                            "jsonrpc": "2.0",
                            "id": data["id"],
                            "result": {"isError": False, "structuredContent": {"confirmed": True}},
                        }
                    )
            raise AssertionError(f"Unexpected: {method} {data}")

        clock = _FakeClock()
        settings = IntegrationSettings()
        settings.upgrade.restart_wait_s = 1.0
        settings.upgrade.health_interval_s = 1.0
        settings.upgrade.health_timeout_s = 5.0

        with _FakeMcpServer(handler) as srv:
            site = _make_site("e2e-v365", url=srv.base_url)
            service = UpgradeService(
                settings,
                mcp_client_factory=lambda s: AnqiMcpClient.for_site(s, settings),
                sleep=clock.sleep,
                monotonic=clock.monotonic,
            )
            result = service.upgrade_site(site, confirmed=True)
            assert result.status == UPGRADE_UPGRADED
            assert result.from_version == "3.6.7"
            assert result.to_version == "3.6.8"
            assert "tool_confirm_batch" in calls
