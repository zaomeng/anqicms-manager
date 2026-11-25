from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from core.admin_jump_service import AdminJumpService
from core.anqicms_mcp_client import FRIENDLINK_TOOL_KEYWORDS, AnqiMcpClient, probe_site
from core.integration_settings import AdminJumpSettings, IntegrationSettings
from core.mcp_contracts import tool_action_enum
from core.upgrade_service import UpgradeService
from models.entities import SiteConfig
from models.sitegroup import (
    JUMP_TIER_BRIDGE,
    JUMP_TIER_DIRECT,
    McpToolInfo,
    UPGRADE_SCOPE_DISABLED,
    UPGRADE_UPGRADED,
)


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


class _AnqiV367LiveServer:
    """Models real AnqiCMS v3.6.7 live MCP responses (28 tools + 49-intent catalog)."""

    def __init__(
        self,
        *,
        expose_siteops_upgrade: bool = False,
        include_system_config: bool = True,
        system_config_error: bool = False,
        account_readonly_hint: bool = True,
        latest_version: str = "3.6.8",
    ) -> None:
        self.expose_siteops_upgrade = expose_siteops_upgrade
        self.include_system_config = include_system_config
        self.system_config_error = system_config_error
        self.account_readonly_hint = account_readonly_hint
        self.current_version = "3.6.7"
        self.latest_version = latest_version
        self.tool_calls: list[tuple[str, dict[str, Any]]] = []
        self._lock = threading.Lock()
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                return

            def _send(self, status: int, headers: dict[str, str], body: bytes) -> None:
                self.send_response(status)
                has_len = any(k.lower() == "content-length" for k in headers)
                for key, val in headers.items():
                    self.send_header(key, val)
                if not has_len:
                    self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                if body:
                    self.wfile.write(body)
                self.close_connection = True

            def do_GET(self) -> None:
                self._send(200, {"Content-Type": "text/html; charset=utf-8"}, b"<html>AnQiCMS</html>")

            def do_DELETE(self) -> None:
                self._send(204, {}, b"")

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length > 0 else b""
                data = json.loads(raw.decode("utf-8"))
                status, headers, body = parent._handle_rpc(data)
                self._send(status, headers, body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )

    def __enter__(self) -> "_AnqiV367LiveServer":
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)

    def _exposed_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        if self.include_system_config:
            tools.append(
                {
                    "name": "system_config",
                    "description": "系统信息、版本号及后台设置",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["site_info", "site_version", "site_anqi", "setting"],
                            }
                        },
                        "required": ["action"],
                    },
                }
            )
        tools.extend(
            [
                {
                    "name": "structure",
                    "description": "站点结构管理：分类、单页、导航、菜单等",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": [
                                    "category_list",
                                    "page_list",
                                    "nav_list",
                                    "friendlink_list",
                                    "friendlink_create",
                                    "friendlink_delete",
                                    "guestbook_list",
                                ],
                            }
                        },
                        "required": ["action"],
                    },
                },
                {
                    "name": "account",
                    "description": "管理员账号信息与登录日志查询",
                    "annotations": {"readOnlyHint": True} if self.account_readonly_hint else {},
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["admin_list", "admin_info", "logs_login", "logs_action"],
                            }
                        },
                        "required": ["action"],
                    },
                },
                {
                    "name": "content_article",
                    "description": "文章内容管理（支持内链/外链/link设置与发布）",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["article_list", "article_detail", "article_save", "article_delete"],
                            }
                        },
                        "required": ["action"],
                    },
                },
                {
                    "name": "mcp_list_intents",
                    "description": "列出所有受支持的 MCP 意图目录",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"domain": {"type": "string"}},
                    },
                },
            ]
        )
        # Pad up to 28 exposed tools (or 27 if system_config omitted) matching real v3.6.7 count.
        filler_index = 1
        target_base = 28 if self.include_system_config else 27
        while len(tools) < target_base:
            tools.append(
                {
                    "name": f"domain_tool_{filler_index:02d}",
                    "description": f"常规业务模块 {filler_index}",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"action": {"type": "string", "enum": ["list", "detail"]}},
                    },
                }
            )
            filler_index += 1

        if self.expose_siteops_upgrade:
            tools.append(
                {
                    "name": "siteops_upgrade",
                    "description": "系统版本检测与升级",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["check", "info", "upgrade"],
                            }
                        },
                        "required": ["action"],
                    },
                }
            )
        return tools

    def _catalog_intents(self) -> list[dict[str, Any]]:
        exposed = self._exposed_tools()
        items: list[dict[str, Any]] = [
            {
                "name": tool["name"],
                "title": tool["name"],
                "domain": "core",
                "risk": "low",
                "desc": tool["description"],
            }
            for tool in exposed
        ]
        disabled_defs = [
            ("siteops_upgrade", "系统升级", "siteops", "high", "检查更新并执行系统升级"),
            ("siteops_website", "多站点管理", "siteops", "high", "多站点配置管理"),
            ("siteops_backup", "站点备份恢复", "siteops", "high", "数据库与文件备份"),
        ]
        for idx in range(1, 19):
            disabled_defs.append(
                (f"siteops_extra_{idx:02d}", f"高级运维{idx}", "siteops", "medium", f"未开放运维意图 {idx}")
            )
        exposed_names = {t["name"] for t in exposed}
        for name, title, domain, risk, desc in disabled_defs:
            if name not in exposed_names:
                items.append(
                    {
                        "name": name,
                        "title": title,
                        "domain": domain,
                        "risk": risk,
                        "desc": desc,
                    }
                )
        return items

    def _handle_rpc(self, data: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
        method = data["method"]
        req_id = data.get("id")
        if method == "initialize":
            return _json_resp(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "AnQiCMS", "version": self.current_version},
                    },
                },
                headers={"Mcp-Session-Id": "v367-live-sess"},
            )
        if method == "notifications/initialized":
            return 202, {}, b""
        if method == "tools/list":
            return _json_resp(
                {"jsonrpc": "2.0", "id": req_id, "result": {"tools": self._exposed_tools()}}
            )
        if method == "tools/call":
            name = data["params"]["name"]
            args = data["params"].get("arguments") or {}
            with self._lock:
                self.tool_calls.append((name, dict(args)))
            if name == "mcp_list_intents":
                return _json_resp(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "isError": False,
                            "structuredContent": {"intents": self._catalog_intents()},
                        },
                    }
                )
            if name == "system_config" and self.include_system_config:
                if self.system_config_error:
                    return _json_resp(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {"isError": True, "content": [{"type": "text", "text": "config error"}]},
                        }
                    )
                if args.get("action") == "site_version":
                    return _json_resp(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {
                                "isError": False,
                                "structuredContent": {
                                    "code": 0,
                                    "data": {"trial": False, "version": self.current_version},
                                },
                            },
                        }
                    )
                return _json_resp(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "isError": True,
                            "content": [{"type": "text", "text": "missing or unsupported action"}],
                        },
                    }
                )
            if name == "account":
                return _json_resp(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "isError": True,
                            "content": [{"type": "text", "text": "action is required for account"}],
                        },
                    }
                )
            if name == "siteops_upgrade":
                if not self.expose_siteops_upgrade:
                    return _json_resp(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "error": {"code": -32601, "message": f'unknown tool "{name}"'},
                        }
                    )
                action = args.get("action")
                if action == "check":
                    return _json_resp(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {
                                "isError": False,
                                "structuredContent": {
                                    "code": 0,
                                    "data": {
                                        "current_version": self.current_version,
                                        "latest_version": self.latest_version,
                                        "need_upgrade": self.current_version != self.latest_version,
                                    },
                                },
                            },
                        }
                    )
                if action == "info":
                    return _json_resp(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {
                                "isError": False,
                                "structuredContent": {"code": 0, "data": {"version": self.current_version}},
                            },
                        }
                    )
                if action == "upgrade":
                    self.current_version = self.latest_version
                    return _json_resp(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {
                                "isError": False,
                                "structuredContent": {
                                    "code": 0,
                                    "data": {"upgraded": True, "version": self.current_version},
                                },
                            },
                        }
                    )
                return _json_resp(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"isError": True, "content": [{"type": "text", "text": "action required"}]},
                    }
                )
            return _json_resp(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f'unknown tool "{name}"'},
                }
            )
        raise AssertionError(f"Unexpected RPC method: {method}")


def _make_site(
    base_url: str,
    *,
    name: str = "v367-live",
    admin_username: str = "",
    admin_password: str = "",
) -> SiteConfig:
    return SiteConfig(
        name=name,
        fid=1,
        url=base_url,
        jiekou="",
        publish_dir=Path("."),
        daily_publish_filename=Path("daily.txt"),
        publish_time="08:00",
        interval_time=60,
        daily_publish_limit=10,
        site_id=f"id-{name}",
        mcp_token="tok_v367_secret_1234",
        admin_username=admin_username,
        admin_password=admin_password,
    )


def test_tool_action_enum_helper() -> None:
    assert tool_action_enum(None) == ()
    assert tool_action_enum(McpToolInfo(name="plain")) == ()
    assert tool_action_enum(McpToolInfo(name="bad", input_schema={"properties": "nope"})) == ()
    assert tool_action_enum(
        McpToolInfo(
            name="siteops_upgrade",
            input_schema={
                "type": "object",
                "properties": {"Action": {"type": "string", "enum": ["check", "info", "upgrade", ""]}},
            },
        )
    ) == ("check", "info", "upgrade")


def test_v367_client_separates_executable_tools_and_disabled_intents() -> None:
    with _AnqiV367LiveServer(expose_siteops_upgrade=False) as srv:
        with AnqiMcpClient(srv.base_url, "tok_v367_secret_1234") as client:
            tools = client.list_tools()
            assert len(tools) == 28

            disabled = client.list_disabled_intents()
            assert len(disabled) == 21
            disabled_names = {t.name for t in disabled}
            assert {"siteops_upgrade", "siteops_website", "siteops_backup"}.issubset(disabled_names)
            assert "system_config" not in disabled_names

            assert client.has_disabled_intent("siteops_upgrade") is True
            assert client.has_disabled_intent(("siteops_upgrade", "upgrade", "升级")) is True
            assert client.has_disabled_intent("nonexistent_intent") is False

            # find_tools must NEVER return unexecutable catalog-only disabled intents
            assert client.find_tools(["upgrade", "升级"]) == []
            # friendlink search matches structure via action enum, never content_article
            fl = client.find_tools(FRIENDLINK_TOOL_KEYWORDS)
            assert [t.name for t in fl] == ["structure"]

        srv.expose_siteops_upgrade = True
        with AnqiMcpClient(srv.base_url, "tok_v367_secret_1234") as client2:
            assert len(client2.list_tools()) == 29
            assert len(client2.list_disabled_intents()) == 20
            assert client2.has_disabled_intent("siteops_upgrade") is False
            found_up = client2.find_tools(["upgrade", "升级"])
            assert [t.name for t in found_up] == ["siteops_upgrade"]


def test_v367_probe_site_disabled_and_enabled_siteops_upgrade() -> None:
    with _AnqiV367LiveServer(expose_siteops_upgrade=False) as srv:
        site = _make_site(srv.base_url)
        probe_disabled = probe_site(site, IntegrationSettings())
        assert probe_disabled.ok is True
        assert probe_disabled.server_name == "AnQiCMS"
        assert probe_disabled.server_version == "3.6.7"
        assert probe_disabled.tool_count == 28
        assert len(probe_disabled.tools) == 28
        assert probe_disabled.friendlink_tools == ("structure",)
        assert probe_disabled.jump_tools == ()
        assert probe_disabled.upgrade_tools == ()
        assert "21 个未开放意图" in probe_disabled.message

        srv.expose_siteops_upgrade = True
        probe_enabled = probe_site(site, IntegrationSettings())
        assert probe_enabled.ok is True
        assert probe_enabled.tool_count == 29
        assert probe_enabled.friendlink_tools == ("structure",)
        assert probe_enabled.jump_tools == ()
        assert probe_enabled.upgrade_tools == ("siteops_upgrade",)


def test_v367_check_version_uses_system_config_and_server_info_fallback() -> None:
    settings = IntegrationSettings()
    with _AnqiV367LiveServer(expose_siteops_upgrade=False) as srv:
        site = _make_site(srv.base_url)
        svc = UpgradeService(
            settings,
            mcp_client_factory=lambda s: AnqiMcpClient.for_site(s, settings),
        )
        info = svc.check_version(site)
        assert info.reachable is True
        assert info.current_version == "3.6.7"
        assert info.source == "mcp"
        assert info.upgrade_tool == ""
        assert "3.6.7" in info.message
        assert "siteops_upgrade" in info.message
        assert "设置 → AI 统一配置" in info.message
        assert ("system_config", {"action": "site_version"}) in srv.tool_calls
        assert all(name != "siteops_upgrade" for name, _ in srv.tool_calls)

    # Fallback to serverInfo.version when system_config is absent or errors
    with _AnqiV367LiveServer(expose_siteops_upgrade=False, system_config_error=True) as srv2:
        site2 = _make_site(srv2.base_url)
        svc2 = UpgradeService(
            settings,
            mcp_client_factory=lambda s: AnqiMcpClient.for_site(s, settings),
        )
        info2 = svc2.check_version(site2)
        assert info2.reachable is True
        assert info2.current_version == "3.6.7"
        assert "siteops_upgrade" in info2.message


def test_v367_upgrade_site_scope_disabled_vs_enabled_multi_action() -> None:
    clock = _FakeClock()
    settings = IntegrationSettings()
    settings.upgrade.restart_wait_s = 1.0
    settings.upgrade.health_interval_s = 1.0
    settings.upgrade.health_timeout_s = 5.0

    with _AnqiV367LiveServer(expose_siteops_upgrade=False) as srv:
        site = _make_site(srv.base_url)
        svc = UpgradeService(
            settings,
            mcp_client_factory=lambda s: AnqiMcpClient.for_site(s, settings),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        disabled_res = svc.upgrade_site(site, confirmed=True)
        assert disabled_res.status == UPGRADE_SCOPE_DISABLED
        assert disabled_res.from_version == "3.6.7"
        assert "设置 → AI 统一配置" in disabled_res.hint
        assert "siteops_upgrade" in disabled_res.hint
        assert all(name != "siteops_upgrade" for name, _ in srv.tool_calls)
        assert ("system_config", {}) not in srv.tool_calls

        # Enable siteops_upgrade in backend -> check + upgrade both inject action
        srv.tool_calls.clear()
        srv.expose_siteops_upgrade = True
        info_enabled = svc.check_version(site)
        assert info_enabled.current_version == "3.6.7"
        assert info_enabled.latest_version == "3.6.8"
        assert info_enabled.upgrade_available is True
        assert info_enabled.check_tool == "siteops_upgrade"
        assert info_enabled.upgrade_tool == "siteops_upgrade"
        assert ("siteops_upgrade", {"action": "check"}) in srv.tool_calls

        srv.tool_calls.clear()
        upgraded_res = svc.upgrade_site(site, confirmed=True)
        assert upgraded_res.status == UPGRADE_UPGRADED
        assert upgraded_res.from_version == "3.6.7"
        assert upgraded_res.to_version == "3.6.8"
        assert ("siteops_upgrade", {"action": "check"}) in srv.tool_calls
        assert ("siteops_upgrade", {"action": "upgrade"}) in srv.tool_calls


def test_v367_admin_jump_skips_readonly_account_without_calling_it() -> None:
    settings = IntegrationSettings(
        admin_jump=AdminJumpSettings(enable_silent_login=False)
    )
    for readonly_hint in (True, False):
        with _AnqiV367LiveServer(account_readonly_hint=readonly_hint) as srv:
            site = _make_site(srv.base_url)
            with AdminJumpService(
                settings=settings,
                mcp_client_factory=lambda s: AnqiMcpClient.for_site(s, settings),
            ) as jump_svc:
                plan = jump_svc.resolve(site)
                assert plan.tier == JUMP_TIER_DIRECT
                assert plan.target_url == f"{srv.base_url}/system/"
                assert plan.fallback_reasons == ("站点 MCP 未提供免密登录票据工具",)
                assert all(name != "account" for name, _ in srv.tool_calls)

    # With silent login + autopost enabled, cleanly progresses to Tier 2 bridge without calling account
    settings_bridge = IntegrationSettings(
        admin_jump=AdminJumpSettings(enable_silent_login=True, autopost_login=True)
    )

    class _FakeResp:
        def __init__(self, status_code: int, json_data: Any) -> None:
            self.status_code = status_code
            self._json_data = json_data
            self.cookies: dict[str, str] = {}

        def json(self) -> Any:
            return self._json_data

    class _FakeSess:
        def __init__(self) -> None:
            self.cookies: dict[str, str] = {}

        def post(self, url: str, **kwargs: Any) -> _FakeResp:
            return _FakeResp(200, {"code": 0, "data": {"token": "adm-tok"}})

        def close(self) -> None:
            return

    with _AnqiV367LiveServer() as srv:
        site = _make_site(srv.base_url, admin_username="admin", admin_password="pw")
        with AdminJumpService(
            settings=settings_bridge,
            mcp_client_factory=lambda s: AnqiMcpClient.for_site(s, settings_bridge),
            session_factory=_FakeSess,
        ) as jump_svc:
            plan = jump_svc.resolve(site)
            assert plan.tier == JUMP_TIER_BRIDGE
            assert all(name != "account" for name, _ in srv.tool_calls)
