from __future__ import annotations

import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pytest
import requests

from core.integration_settings import IntegrationSettings, UpgradeSettings
from core.mcp_contracts import (
    AUTH_HINT,
    REDIRECT_HINT,
    SCOPE_HINT,
    McpAuthError,
    McpProtocolError,
    McpScopeError,
    McpToolError,
    McpUnavailableError,
    score_tools,
)
from core.upgrade_service import (
    UpgradeService,
    compare_versions,
    parse_version_payload,
    scope_hint,
)
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
    UpgradeProgress,
    UpgradeResult,
)


def _make_site(
    name: str = "site-a",
    *,
    site_id: str = "",
    url: str = "https://site-a.example.com",
    jiekou: str = "https://site-a.example.com/api/import?token=imp-secret-1234",
    mcp_token: str = "mcp-secret-9999",
    admin_password: str = "adm-pass-8888",
) -> SiteConfig:
    return SiteConfig(
        name=name,
        fid=1,
        url=url,
        jiekou=jiekou,
        publish_dir=Path("."),
        daily_publish_filename=Path("daily.txt"),
        publish_time="08:00",
        interval_time=60,
        daily_publish_limit=10,
        site_id=site_id or f"id-{name}",
        mcp_token=mcp_token,
        admin_password=admin_password,
    )


class FakeClock:
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


@dataclass
class FakeResponse:
    status_code: int = 200
    text: str = ""
    json_data: Any = None

    def json(self) -> Any:
        if self.json_data is not None:
            return self.json_data
        raise ValueError("no json")


class FakeSession:
    def __init__(
        self,
        *,
        get_handler: Callable[[str, dict[str, Any]], FakeResponse] | None = None,
        post_handler: Callable[[str, dict[str, Any]], FakeResponse] | None = None,
    ) -> None:
        self.get_handler = get_handler
        self.post_handler = post_handler
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("GET", url, kwargs))
        if self.get_handler is None:
            return FakeResponse(status_code=200, text="ok")
        return self.get_handler(url, kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("POST", url, kwargs))
        if self.post_handler is None:
            return FakeResponse(status_code=200, text="{}")
        return self.post_handler(url, kwargs)

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeMcpClient:
    tools: list[McpToolInfo] = field(default_factory=list)
    init_error: Exception | None = None
    call_handler: Callable[[str, dict | None, float | None], McpToolResult] | None = None
    closed: bool = False
    initialized: bool = False
    calls: list[tuple[str, dict | None, float | None]] = field(default_factory=list)

    def initialize(self) -> dict:
        if self.init_error is not None:
            raise self.init_error
        self.initialized = True
        return {"protocolVersion": "2025-06-18"}

    def list_tools(self, *, refresh: bool = False) -> list[McpToolInfo]:
        return list(self.tools)

    def find_tools(self, keywords, *, explicit_names=()) -> list[McpToolInfo]:
        return score_tools(self.tools, keywords, explicit_names)

    def call_tool(
        self,
        name: str,
        arguments: dict | None = None,
        *,
        timeout: float | None = None,
    ) -> McpToolResult:
        self.calls.append((name, arguments, timeout))
        if self.call_handler is None:
            return McpToolResult(is_error=False, text="")
        return self.call_handler(name, arguments, timeout)

    def close(self) -> None:
        self.closed = True


def _fast_settings(**upgrade_overrides: Any) -> IntegrationSettings:
    settings = IntegrationSettings()
    defaults = {
        "restart_wait_s": 2.0,
        "health_timeout_s": 20.0,
        "health_interval_s": 5.0,
        "upgrade_call_timeout_s": 120.0,
        "max_concurrency": 3,
    }
    defaults.update(upgrade_overrides)
    for key, value in defaults.items():
        setattr(settings.upgrade, key, value)
    return settings


# --------------------------------------------------------------------------- #
# Version parsing, comparison & scope hints
# --------------------------------------------------------------------------- #
class TestVersionHelpers:
    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            ("3.6.7", "3.6.8", -1),
            ("3.6.10", "3.6.8", 1),
            ("v3.6.7", "3.6.7", 0),
            ("V3.6.7", "v3.6.7.0", 0),
            ("3.6.7-beta", "3.6.7", -1),
            ("3.6.7", "3.6.7-rc1", 1),
            ("3.6.7-alpha", "3.6.7-beta", -1),
            ("3.6.7-beta.2", "3.6.7-beta.10", -1),
            ("3.6.7+20260101", "3.6.7", 0),
            ("3.6", "3.6.1", -1),
            ("unknown", "3.6.7", 0),
            ("3.6.7", "", 0),
            ("", "", 0),
            ("not-a-ver", "also-not", 0),
        ],
    )
    def test_compare_versions(self, a: str, b: str, expected: int) -> None:
        assert compare_versions(a, b) == expected

    def test_parse_version_payload_structured_keys(self) -> None:
        res = McpToolResult(
            is_error=False,
            text="",
            structured={
                "current_version": "v3.6.7",
                "latest_version": "v3.6.9",
                "need_upgrade": True,
            },
        )
        assert parse_version_payload(res) == ("v3.6.7", "v3.6.9", True)

    def test_parse_version_payload_camel_and_nested_data(self) -> None:
        res = McpToolResult(
            is_error=False,
            text="",
            structured={
                "code": 0,
                "data": {
                    "currentVersion": "3.6.8",
                    "newVersion": "3.6.8",
                },
            },
        )
        assert parse_version_payload(res) == ("3.6.8", "3.6.8", False)

    def test_parse_version_payload_json_in_text(self) -> None:
        res = McpToolResult(
            is_error=False,
            text='{"version": "3.6.6", "latest": "3.6.8", "has_update": 1}',
        )
        assert parse_version_payload(res) == ("3.6.6", "3.6.8", True)

    def test_parse_version_payload_text_regex_chinese_labels(self) -> None:
        res = McpToolResult(
            is_error=False,
            text="系统信息：当前版本v3.6.6，最新版本3.6.8-beta，服务器IP 192.168.1.10。",
        )
        current, latest, available = parse_version_payload(res)
        assert current == "v3.6.6"
        assert latest == "3.6.8-beta"
        assert available is True

    def test_parse_version_payload_text_already_latest_phrase(self) -> None:
        res = McpToolResult(is_error=False, text="当前版本 3.6.8，已是最新版本，无需升级。")
        assert parse_version_payload(res) == ("3.6.8", "", False)

    def test_scope_hint_categories(self) -> None:
        upgrade_h = scope_hint("upgrade")
        assert "设置→AI 统一配置→接口开放范围" in upgrade_h
        assert "系统升级" in upgrade_h
        assert "后台" in upgrade_h
        assert "友情链接" in scope_hint("friendlink")
        assert "多站点" in scope_hint("jump")
        assert scope_hint("other") == SCOPE_HINT

    def test_does_not_import_concrete_mcp_client(self) -> None:
        # Ensure upgrade_service itself does not pull core.anqicms_mcp_client on import.
        assert "core.anqicms_mcp_client" not in getattr(
            sys.modules["core.upgrade_service"], "__dict__", {}
        )


# --------------------------------------------------------------------------- #
# Version detection (single & batch + REST fallback)
# --------------------------------------------------------------------------- #
class TestCheckVersions:
    def test_check_version_mcp_success(self) -> None:
        site = _make_site("alpha")
        tools = [
            McpToolInfo(name="check_update", description="检查更新"),
            McpToolInfo(name="system_upgrade", description="执行系统升级"),
        ]
        client = FakeMcpClient(
            tools=tools,
            call_handler=lambda name, args, timeout: McpToolResult(
                is_error=False,
                text="",
                structured={"current_version": "3.6.7", "latest_version": "3.6.8"},
            ),
        )
        service = UpgradeService(_fast_settings(), mcp_client_factory=lambda s: client)

        info = service.check_version(site)

        assert info.reachable is True
        assert info.current_version == "3.6.7"
        assert info.latest_version == "3.6.8"
        assert info.upgrade_available is True
        assert info.source == "mcp"
        assert info.check_tool == "check_update"
        assert info.upgrade_tool == "system_upgrade"
        assert client.closed is True

    def test_check_version_excludes_readonly_from_upgrade_tool(self) -> None:
        site = _make_site("alpha")
        tools = [
            McpToolInfo(name="upgrade_check", description="检查更新状态"),
            McpToolInfo(name="system_info", description="系统信息"),
        ]
        client = FakeMcpClient(
            tools=tools,
            call_handler=lambda name, args, timeout: McpToolResult(
                is_error=False,
                text="",
                structured={"current_version": "3.6.7", "latest_version": "3.6.8"},
            ),
        )
        service = UpgradeService(_fast_settings(), mcp_client_factory=lambda s: client)

        info = service.check_version(site)

        assert info.check_tool == "upgrade_check"
        assert info.upgrade_tool == ""
        assert "系统升级" in info.hint

    def test_explicit_tool_names_override_keywords_and_filter(self) -> None:
        site = _make_site("alpha")
        tools = [
            McpToolInfo(name="custom_status_checker", description="misc"),
            McpToolInfo(name="custom_check_and_upgrade", description="misc"),
        ]
        client = FakeMcpClient(
            tools=tools,
            call_handler=lambda n, a, t: McpToolResult(
                is_error=False,
                text="",
                structured={"current_version": "3.6.7", "latest_version": "3.6.8"},
            ),
        )
        service = UpgradeService(
            _fast_settings(
                check_tool_names=["custom_status_checker"],
                upgrade_tool_names=["custom_check_and_upgrade"],
            ),
            mcp_client_factory=lambda s: client,
        )
        info = service.check_version(site)
        assert info.check_tool == "custom_status_checker"
        assert info.upgrade_tool == "custom_check_and_upgrade"

    def test_check_version_auth_and_scope_errors_redact_secrets(self) -> None:
        site = _make_site("alpha", mcp_token="top-secret-token-xyz")
        auth_client = FakeMcpClient(
            init_error=McpAuthError("401 failed for top-secret-token-xyz")
        )
        service = UpgradeService(_fast_settings(), mcp_client_factory=lambda s: auth_client)
        info = service.check_version(site)
        assert info.reachable is False
        assert "top-secret-token-xyz" not in info.message
        assert info.hint == AUTH_HINT

        scope_client = FakeMcpClient(
            tools=[McpToolInfo(name="check_update")],
            call_handler=lambda n, a, t: (_ for _ in ()).throw(
                McpScopeError("接口未开放 top-secret-token-xyz")
            ),
        )
        service2 = UpgradeService(_fast_settings(), mcp_client_factory=lambda s: scope_client)
        info2 = service2.check_version(site)
        assert info2.reachable is True
        assert "top-secret-token-xyz" not in info2.message
        assert info2.hint == scope_hint("upgrade")

    def test_check_version_rest_fallback_only_when_configured(self) -> None:
        site = _make_site("alpha")
        sessions: list[FakeSession] = []

        def make_session() -> FakeSession:
            sess = FakeSession(
                get_handler=lambda url, kw: FakeResponse(
                    status_code=200,
                    json_data={"code": 0, "data": {"version": "3.6.6", "latest": "3.6.9"}},
                )
            )
            sessions.append(sess)
            return sess

        # Default settings: rest_version_paths is empty → no HTTP calls.
        svc_default = UpgradeService(
            _fast_settings(),
            mcp_client_factory=lambda s: FakeMcpClient(
                init_error=McpUnavailableError("MCP 404")
            ),
            session_factory=make_session,
        )
        info_default = svc_default.check_version(site)
        assert info_default.reachable is False
        assert info_default.source == "none"
        assert sessions == []

        # Configured REST fallback: queries path with allow_redirects=False.
        svc_rest = UpgradeService(
            _fast_settings(rest_version_paths=["/api/system/version"]),
            mcp_client_factory=lambda s: FakeMcpClient(
                init_error=McpUnavailableError("MCP 404")
            ),
            session_factory=make_session,
        )
        info_rest = svc_rest.check_version(site)
        assert info_rest.reachable is True
        assert info_rest.source == "rest"
        assert info_rest.current_version == "3.6.6"
        assert info_rest.latest_version == "3.6.9"
        assert info_rest.upgrade_available is True
        assert len(sessions) == 1
        assert sessions[0].closed is True
        _, url, kwargs = sessions[0].calls[0]
        assert url == "https://site-a.example.com/api/system/version"
        assert kwargs["allow_redirects"] is False

    def test_check_versions_preserves_input_order_and_supports_cancel(self) -> None:
        sites = [_make_site(f"s{i}", url=f"https://s{i}.example.com") for i in range(4)]
        progress_events: list[UpgradeProgress] = []
        worker_threads: set[int] = set()

        def factory(site: SiteConfig) -> FakeMcpClient:
            return FakeMcpClient(
                tools=[McpToolInfo(name="check_update")],
                call_handler=lambda n, a, t: McpToolResult(
                    is_error=False,
                    text="",
                    structured={"current_version": "3.6.7", "latest_version": "3.6.8"},
                ),
            )

        def on_progress(ev: UpgradeProgress) -> None:
            worker_threads.add(threading.get_ident())
            progress_events.append(ev)

        service = UpgradeService(_fast_settings(), mcp_client_factory=factory)
        results = service.check_versions(sites, progress=on_progress, max_workers=10)

        assert [r.site_name for r in results] == ["s0", "s1", "s2", "s3"]
        assert all(r.reachable and r.current_version == "3.6.7" for r in results)
        assert any(ev.stage == "checking" for ev in progress_events)
        assert any(ev.stage == "done" for ev in progress_events)
        assert worker_threads  # progress callbacks fired on worker threads

        # Pre-cancelled batch does no network work
        called = False

        def fail_if_called(site: SiteConfig) -> FakeMcpClient:
            nonlocal called
            called = True
            raise AssertionError("should not open client when cancelled")

        cancel = threading.Event()
        cancel.set()
        service_c = UpgradeService(_fast_settings(), mcp_client_factory=fail_if_called)
        res_c = service_c.check_versions(sites, cancel_event=cancel)
        assert called is False
        assert [r.message for r in res_c] == ["已取消"] * 4


# --------------------------------------------------------------------------- #
# Single-site upgrade flows
# --------------------------------------------------------------------------- #
class TestUpgradeSite:
    def test_requires_confirmed_true(self) -> None:
        site = _make_site("alpha")
        service = UpgradeService(_fast_settings())
        with pytest.raises(ValueError, match="确认"):
            service.upgrade_site(site, confirmed=False)
        with pytest.raises(ValueError, match="确认"):
            service.upgrade_sites([site], confirmed=False)

    def test_already_latest_skips_upgrade_call(self) -> None:
        site = _make_site("alpha")
        client = FakeMcpClient(
            tools=[
                McpToolInfo(name="check_update"),
                McpToolInfo(name="system_upgrade"),
            ],
            call_handler=lambda name, args, timeout: McpToolResult(
                is_error=False,
                text="",
                structured={"current_version": "3.6.8", "latest_version": "3.6.8"},
            ),
        )
        service = UpgradeService(_fast_settings(), mcp_client_factory=lambda s: client)

        result = service.upgrade_site(site, confirmed=True)

        assert result.status == UPGRADE_ALREADY_LATEST
        assert result.from_version == "3.6.8"
        assert result.to_version == "3.6.8"
        assert [c[0] for c in client.calls] == ["check_update"]
        assert client.closed is True

    def test_scope_disabled_returns_chinese_hint(self) -> None:
        site = _make_site("alpha")
        tools = [
            McpToolInfo(name="check_update"),
            McpToolInfo(
                name="system_upgrade",
                input_schema={
                    "type": "object",
                    "properties": {
                        "version": {"type": "string"},
                        "confirm": {"type": "boolean"},
                    },
                },
            ),
        ]

        def call_handler(name: str, args: dict | None, timeout: float | None) -> McpToolResult:
            if name == "check_update":
                return McpToolResult(
                    is_error=False,
                    text="",
                    structured={"current_version": "3.6.7", "latest_version": "3.6.8"},
                )
            raise McpScopeError("系统升级未在接口开放范围中勾选")

        client = FakeMcpClient(tools=tools, call_handler=call_handler)
        service = UpgradeService(_fast_settings(), mcp_client_factory=lambda s: client)

        result = service.upgrade_site(site, confirmed=True)

        assert result.status == UPGRADE_SCOPE_DISABLED
        assert "设置→AI 统一配置→接口开放范围" in result.hint
        assert "系统升级" in result.hint
        # Confirm args were built from inputSchema
        upgrade_call = [c for c in client.calls if c[0] == "system_upgrade"][0]
        assert upgrade_call[1] == {"version": "3.6.8", "confirm": True}
        assert upgrade_call[2] == 120.0

    def test_no_tool_when_only_check_tool_exposed(self) -> None:
        site = _make_site("alpha")
        tools = [
            McpToolInfo(name="upgrade_check", description="检查更新"),
            McpToolInfo(name="version_info", description="获取版本"),
        ]
        client = FakeMcpClient(
            tools=tools,
            call_handler=lambda n, a, t: McpToolResult(
                is_error=False,
                text="",
                structured={"current_version": "3.6.7", "latest_version": "3.6.8"},
            ),
        )
        service = UpgradeService(_fast_settings(), mcp_client_factory=lambda s: client)

        result = service.upgrade_site(site, confirmed=True)

        assert result.status == UPGRADE_NO_TOOL
        assert "接口开放范围" in result.hint
        assert "系统升级" in result.hint

    def test_auth_failed_and_unreachable(self) -> None:
        site = _make_site("alpha")
        svc_auth = UpgradeService(
            _fast_settings(),
            mcp_client_factory=lambda s: FakeMcpClient(init_error=McpAuthError("bad token")),
        )
        res_auth = svc_auth.upgrade_site(site, confirmed=True)
        assert res_auth.status == UPGRADE_AUTH_FAILED
        assert res_auth.hint == AUTH_HINT

        svc_unreach = UpgradeService(
            _fast_settings(),
            mcp_client_factory=lambda s: FakeMcpClient(
                init_error=McpUnavailableError("301", http_status=301, hint=REDIRECT_HINT)
            ),
        )
        res_unreach = svc_unreach.upgrade_site(site, confirmed=True)
        assert res_unreach.status == UPGRADE_UNREACHABLE
        assert res_unreach.hint == REDIRECT_HINT

    def test_timeout_during_upgrade_call_continues_to_health_poll_and_succeeds(self) -> None:
        site = _make_site("alpha")
        clock = FakeClock()
        stages: list[str] = []
        state = {"upgraded": False, "http_attempts": 0}

        tools = [
            McpToolInfo(name="check_update"),
            McpToolInfo(
                name="system_upgrade",
                input_schema={
                    "type": "object",
                    "properties": {
                        "target_version": {"type": "string"},
                        "approved": {"type": "boolean"},
                    },
                },
            ),
        ]

        def factory(s: SiteConfig) -> FakeMcpClient:
            def call_handler(name: str, args: dict | None, timeout: float | None) -> McpToolResult:
                if name == "check_update":
                    ver = "3.6.8" if state["upgraded"] else "3.6.7"
                    return McpToolResult(
                        is_error=False,
                        text="",
                        structured={"current_version": ver, "latest_version": "3.6.8"},
                    )
                assert args == {"target_version": "3.6.8", "approved": True}
                state["upgraded"] = True
                raise requests.ReadTimeout("server restarting mid-response")

            return FakeMcpClient(tools=tools, call_handler=call_handler)

        def make_session() -> FakeSession:
            def on_get(url: str, kw: dict[str, Any]) -> FakeResponse:
                state["http_attempts"] += 1
                if state["http_attempts"] < 3:
                    raise requests.ConnectionError("still restarting")
                return FakeResponse(status_code=200, text="<html>ok</html>")

            return FakeSession(get_handler=on_get)

        service = UpgradeService(
            _fast_settings(restart_wait_s=4.0, health_interval_s=3.0, health_timeout_s=30.0),
            mcp_client_factory=factory,
            session_factory=make_session,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        result = service.upgrade_site(
            site,
            confirmed=True,
            progress=lambda ev: stages.append(ev.stage),
        )

        assert result.status == UPGRADE_UPGRADED
        assert result.from_version == "3.6.7"
        assert result.to_version == "3.6.8"
        assert state["http_attempts"] == 3
        assert clock.sleeps == [4.0, 3.0, 3.0]
        assert result.duration_s == pytest.approx(10.0)
        assert stages[0] == "connecting"
        assert "checking" in stages
        assert "upgrading" in stages
        assert "restarting" in stages
        assert "verifying" in stages
        assert stages[-1] == "done"

    def test_mcp_unavailable_timeout_during_call_also_continues_to_health_poll(self) -> None:
        site = _make_site("alpha")
        clock = FakeClock()
        upgraded = {"done": False}
        tools = [McpToolInfo(name="check_update"), McpToolInfo(name="system_upgrade")]

        def factory(s: SiteConfig) -> FakeMcpClient:
            def call_handler(name: str, args: dict | None, timeout: float | None) -> McpToolResult:
                if name == "check_update":
                    ver = "3.6.8" if upgraded["done"] else "3.6.7"
                    return McpToolResult(
                        is_error=False,
                        text="",
                        structured={"current_version": ver, "latest_version": "3.6.8"},
                    )
                upgraded["done"] = True
                raise McpUnavailableError("连接在重启时中断")

            return FakeMcpClient(tools=tools, call_handler=call_handler)

        service = UpgradeService(
            _fast_settings(restart_wait_s=1.0, health_interval_s=2.0, health_timeout_s=10.0),
            mcp_client_factory=factory,
            session_factory=lambda: FakeSession(
                get_handler=lambda url, kw: FakeResponse(status_code=302)
            ),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        result = service.upgrade_site(site, confirmed=True)
        assert result.status == UPGRADE_UPGRADED
        assert result.to_version == "3.6.8"

    def test_unchanged_version_after_restart_returns_failed(self) -> None:
        site = _make_site("alpha")
        clock = FakeClock()
        tools = [McpToolInfo(name="check_update"), McpToolInfo(name="system_upgrade")]

        def factory(s: SiteConfig) -> FakeMcpClient:
            def call_handler(name: str, args: dict | None, timeout: float | None) -> McpToolResult:
                if name == "check_update":
                    return McpToolResult(
                        is_error=False,
                        text="",
                        structured={"current_version": "3.6.7", "latest_version": "3.6.8"},
                    )
                return McpToolResult(is_error=False, text="upgrade triggered")

            return FakeMcpClient(tools=tools, call_handler=call_handler)

        service = UpgradeService(
            _fast_settings(restart_wait_s=1.0, health_interval_s=2.0, health_timeout_s=10.0),
            mcp_client_factory=factory,
            session_factory=lambda: FakeSession(
                get_handler=lambda url, kw: FakeResponse(status_code=200)
            ),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        result = service.upgrade_site(site, confirmed=True)

        assert result.status == UPGRADE_FAILED
        assert result.from_version == "3.6.7"
        assert result.to_version == "3.6.7"
        assert "升级后版本未变化" in result.message

    def test_health_timeout_returns_timeout_with_hint(self) -> None:
        site = _make_site("alpha")
        clock = FakeClock()
        tools = [McpToolInfo(name="check_update"), McpToolInfo(name="system_upgrade")]

        def factory(s: SiteConfig) -> FakeMcpClient:
            def call_handler(name: str, args: dict | None, timeout: float | None) -> McpToolResult:
                if name == "check_update":
                    return McpToolResult(
                        is_error=False,
                        text="",
                        structured={"current_version": "3.6.7", "latest_version": "3.6.8"},
                    )
                return McpToolResult(is_error=False, text="restarting")

            return FakeMcpClient(tools=tools, call_handler=call_handler)

        def down_session() -> FakeSession:
            def on_get(url: str, kw: dict[str, Any]) -> FakeResponse:
                raise requests.ConnectionError("connection refused")

            return FakeSession(get_handler=on_get)

        service = UpgradeService(
            _fast_settings(restart_wait_s=5.0, health_interval_s=5.0, health_timeout_s=15.0),
            mcp_client_factory=factory,
            session_factory=down_session,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        result = service.upgrade_site(site, confirmed=True)

        assert result.status == UPGRADE_TIMEOUT
        assert "15" in result.hint
        assert "守护" in result.hint
        assert clock.sleeps == [5.0, 5.0, 5.0, 5.0]

    def test_rest_upgrade_fallback_when_configured(self) -> None:
        site = _make_site("alpha")
        clock = FakeClock()
        state = {"upgraded": False}

        def make_session() -> FakeSession:
            def on_get(url: str, kw: dict[str, Any]) -> FakeResponse:
                if url.endswith("/api/system/version"):
                    ver = "3.6.8" if state["upgraded"] else "3.6.7"
                    return FakeResponse(
                        status_code=200,
                        json_data={"code": 0, "data": {"current_version": ver, "latest_version": "3.6.8"}},
                    )
                return FakeResponse(status_code=200, text="ok")

            def on_post(url: str, kw: dict[str, Any]) -> FakeResponse:
                assert kw["allow_redirects"] is False
                state["upgraded"] = True
                return FakeResponse(status_code=200, json_data={"code": 0, "msg": "ok"})

            return FakeSession(get_handler=on_get, post_handler=on_post)

        service = UpgradeService(
            _fast_settings(
                rest_version_paths=["/api/system/version"],
                rest_upgrade_paths=["/api/system/upgrade"],
                restart_wait_s=1.0,
            ),
            mcp_client_factory=lambda s: FakeMcpClient(
                init_error=McpUnavailableError("MCP disabled")
            ),
            session_factory=make_session,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        result = service.upgrade_site(site, confirmed=True)
        assert result.status == UPGRADE_UPGRADED
        assert result.from_version == "3.6.7"
        assert result.to_version == "3.6.8"

    def test_is_error_scope_result_and_secret_redaction(self) -> None:
        site = _make_site("alpha", mcp_token="secret-mcp-7777")
        tools = [McpToolInfo(name="check_update"), McpToolInfo(name="system_upgrade")]

        def call_handler(name: str, args: dict | None, timeout: float | None) -> McpToolResult:
            if name == "check_update":
                return McpToolResult(
                    is_error=False,
                    text="",
                    structured={"current_version": "3.6.7", "latest_version": "3.6.8"},
                )
            return McpToolResult(
                is_error=True,
                text="permission denied for token secret-mcp-7777",
            )

        events: list[UpgradeProgress] = []
        service = UpgradeService(
            _fast_settings(),
            mcp_client_factory=lambda s: FakeMcpClient(tools=tools, call_handler=call_handler),
        )
        result = service.upgrade_site(site, confirmed=True, progress=events.append)
        assert result.status == UPGRADE_SCOPE_DISABLED
        assert "设置→AI 统一配置→接口开放范围" in result.hint
        assert "secret-mcp-7777" not in result.message
        assert all("secret-mcp-7777" not in ev.message for ev in events)

    def test_missing_url_and_explicit_target_version(self) -> None:
        empty_site = _make_site("empty", url="")
        service = UpgradeService(_fast_settings())
        res_empty = service.upgrade_site(empty_site, confirmed=True)
        assert res_empty.status == UPGRADE_UNREACHABLE

        site = _make_site("alpha")
        client = FakeMcpClient(
            tools=[McpToolInfo(name="check_update"), McpToolInfo(name="system_upgrade")],
            call_handler=lambda n, a, t: McpToolResult(
                is_error=False,
                text="",
                structured={"current_version": "3.6.8", "latest_version": "3.6.9"},
            ),
        )
        svc2 = UpgradeService(_fast_settings(), mcp_client_factory=lambda s: client)
        res_target = svc2.upgrade_site(site, confirmed=True, target_version="3.6.8")
        assert res_target.status == UPGRADE_ALREADY_LATEST

    def test_wait_until_healthy_frozen_clock_and_cancel(self) -> None:
        site = _make_site("alpha")
        sleeps: list[float] = []

        def down_session() -> FakeSession:
            return FakeSession(
                get_handler=lambda u, k: (_ for _ in ()).throw(requests.ConnectionError("down"))
            )

        service = UpgradeService(
            _fast_settings(health_timeout_s=10.0, health_interval_s=4.0),
            session_factory=down_session,
            sleep=sleeps.append,
            monotonic=lambda: 0.0,  # frozen clock must not infinite-loop
        )
        healthy, ver = service.wait_until_healthy(site, previous_version="3.6.7")
        assert healthy is False
        assert ver == ""
        assert sleeps == [4.0, 4.0, 2.0]

        cancel = threading.Event()
        cancel.set()
        healthy_c, ver_c = service.wait_until_healthy(
            site, previous_version="3.6.7", cancel_event=cancel
        )
        assert healthy_c is False
        assert ver_c == ""


# --------------------------------------------------------------------------- #
# Staged batch upgrades (canary, stop-on-failure, concurrency cap, cancel)
# --------------------------------------------------------------------------- #
class TestUpgradeSites:
    def test_canary_failure_with_stop_on_failure_marks_rest_skipped(self) -> None:
        sites = [_make_site(f"s{i}", url=f"https://s{i}.example.com") for i in range(4)]
        opened: list[str] = []

        def factory(site: SiteConfig) -> FakeMcpClient:
            opened.append(site.name)
            return FakeMcpClient(init_error=McpAuthError("invalid token"))

        emitted: list[UpgradeResult] = []
        service = UpgradeService(_fast_settings(), mcp_client_factory=factory)

        results = service.upgrade_sites(
            sites,
            confirmed=True,
            canary_first=True,
            stop_on_failure=True,
            result_callback=emitted.append,
        )

        assert opened == ["s0"]
        assert [r.site_name for r in results] == ["s0", "s1", "s2", "s3"]
        assert results[0].status == UPGRADE_AUTH_FAILED
        assert [r.status for r in results[1:]] == [UPGRADE_SKIPPED] * 3
        assert len(emitted) == 4

    def test_canary_failure_without_stop_on_failure_continues_remaining(self) -> None:
        sites = [_make_site(f"s{i}", url=f"https://s{i}.example.com") for i in range(3)]
        clock = FakeClock()
        versions = {s.name: "3.6.7" for s in sites}
        tools = [McpToolInfo(name="check_update"), McpToolInfo(name="system_upgrade")]

        def factory(site: SiteConfig) -> FakeMcpClient:
            if site.name == "s0":
                return FakeMcpClient(init_error=McpAuthError("bad token"))

            def call_handler(name: str, args: dict | None, timeout: float | None) -> McpToolResult:
                if name == "check_update":
                    return McpToolResult(
                        is_error=False,
                        text="",
                        structured={"current_version": versions[site.name], "latest_version": "3.6.8"},
                    )
                versions[site.name] = "3.6.8"
                return McpToolResult(is_error=False, text="ok")

            return FakeMcpClient(tools=tools, call_handler=call_handler)

        service = UpgradeService(
            _fast_settings(restart_wait_s=0.0),
            mcp_client_factory=factory,
            session_factory=lambda: FakeSession(get_handler=lambda u, k: FakeResponse(200)),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        results = service.upgrade_sites(
            sites,
            confirmed=True,
            canary_first=True,
            stop_on_failure=False,
            max_workers=2,
        )

        assert [r.status for r in results] == [
            UPGRADE_AUTH_FAILED,
            UPGRADE_UPGRADED,
            UPGRADE_UPGRADED,
        ]

    def test_canary_runs_alone_before_remaining_and_concurrency_capped_at_5(self) -> None:
        sites = [_make_site(f"s{i}", url=f"https://s{i}.example.com") for i in range(8)]
        clock = FakeClock()
        versions = {s.name: "3.6.7" for s in sites}
        tools = [McpToolInfo(name="check_update"), McpToolInfo(name="system_upgrade")]

        lock = threading.Lock()
        active = 0
        peak_active = 0
        canary_finished = False
        remaining_saw_canary_done: list[bool] = []

        def factory(site: SiteConfig) -> FakeMcpClient:
            def call_handler(name: str, args: dict | None, timeout: float | None) -> McpToolResult:
                nonlocal active, peak_active, canary_finished
                if name == "check_update":
                    return McpToolResult(
                        is_error=False,
                        text="",
                        structured={"current_version": versions[site.name], "latest_version": "3.6.8"},
                    )
                with lock:
                    if site.name != "s0":
                        remaining_saw_canary_done.append(canary_finished)
                    active += 1
                    peak_active = max(peak_active, active)
                # Brief yield to allow threads to overlap
                threading.Event().wait(0.02)
                with lock:
                    active -= 1
                    versions[site.name] = "3.6.8"
                    if site.name == "s0":
                        canary_finished = True
                return McpToolResult(is_error=False, text="ok")

            return FakeMcpClient(tools=tools, call_handler=call_handler)

        service = UpgradeService(
            _fast_settings(restart_wait_s=0.0),
            mcp_client_factory=factory,
            session_factory=lambda: FakeSession(get_handler=lambda u, k: FakeResponse(200)),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        results = service.upgrade_sites(
            sites,
            confirmed=True,
            max_workers=20,  # should be hard-capped to 5
            canary_first=True,
            stop_on_failure=True,
        )

        assert [r.site_name for r in results] == [f"s{i}" for i in range(8)]
        assert all(r.status == UPGRADE_UPGRADED for r in results)
        assert remaining_saw_canary_done and all(remaining_saw_canary_done)
        assert 1 <= peak_active <= 5

    def test_cancel_marks_unstarted_sites_cancelled(self) -> None:
        sites = [_make_site(f"s{i}", url=f"https://s{i}.example.com") for i in range(4)]
        clock = FakeClock()
        cancel = threading.Event()
        tools = [McpToolInfo(name="check_update"), McpToolInfo(name="system_upgrade")]

        def factory(site: SiteConfig) -> FakeMcpClient:
            def call_handler(name: str, args: dict | None, timeout: float | None) -> McpToolResult:
                if site.name == "s0":
                    cancel.set()  # cancel during canary
                return McpToolResult(
                    is_error=False,
                    text="",
                    structured={"current_version": "3.6.8", "latest_version": "3.6.8"},
                )

            return FakeMcpClient(tools=tools, call_handler=call_handler)

        service = UpgradeService(
            _fast_settings(restart_wait_s=0.0),
            mcp_client_factory=factory,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        results = service.upgrade_sites(
            sites,
            confirmed=True,
            canary_first=True,
            cancel_event=cancel,
        )

        assert results[0].status == UPGRADE_ALREADY_LATEST
        assert [r.status for r in results[1:]] == [UPGRADE_CANCELLED] * 3

    def test_batch_stop_on_failure_without_canary_skips_queued_sites(self) -> None:
        sites = [_make_site(f"s{i}", url=f"https://s{i}.example.com") for i in range(4)]
        opened: list[str] = []

        def factory(site: SiteConfig) -> FakeMcpClient:
            opened.append(site.name)
            if site.name == "s1":
                return FakeMcpClient(init_error=McpAuthError("bad token"))
            return FakeMcpClient(
                tools=[McpToolInfo(name="check_update")],
                call_handler=lambda n, a, t: McpToolResult(
                    is_error=False,
                    text="",
                    structured={"current_version": "3.6.8", "latest_version": "3.6.8"},
                ),
            )

        service = UpgradeService(_fast_settings(), mcp_client_factory=factory)

        results = service.upgrade_sites(
            sites,
            confirmed=True,
            canary_first=False,
            stop_on_failure=True,
            max_workers=1,
        )

        assert opened == ["s0", "s1"]
        assert [r.status for r in results] == [
            UPGRADE_ALREADY_LATEST,
            UPGRADE_AUTH_FAILED,
            UPGRADE_SKIPPED,
            UPGRADE_SKIPPED,
        ]
