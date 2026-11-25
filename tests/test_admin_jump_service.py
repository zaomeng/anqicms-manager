from __future__ import annotations

import http.client
import threading
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import requests

from core.admin_jump_service import AdminJumpService
from core.integration_settings import AdminJumpSettings, IntegrationSettings, McpSettings
from core.jump_bridge import JumpBridge
from core.mcp_contracts import (
    McpAuthError,
    McpScopeError,
    McpUnavailableError,
    score_tools,
)
from models.sitegroup import (
    JUMP_TIER_BRIDGE,
    JUMP_TIER_DIRECT,
    JUMP_TIER_TICKET,
    McpToolInfo,
    McpToolResult,
)


def make_site(**overrides):
    data = dict(
        name="站点A",
        url="https://site-a.example.com",
        jiekou="https://site-a.example.com/api/import/archive?token=import-secret-9999",
        site_title="站A",
        site_group="一组",
        mcp_token="mcp-secret-token-1234",
        admin_username="",
        admin_password="",
        admin_path="/system/",
    )
    data.update(overrides)
    return SimpleNamespace(**data)


class FakeMcpClient:
    def __init__(
        self,
        *,
        tools: list[McpToolInfo] | None = None,
        call_result: McpToolResult | None = None,
        init_exc: Exception | None = None,
        call_exc: Exception | None = None,
    ) -> None:
        self.tools = list(tools or [])
        self.call_result = call_result
        self.init_exc = init_exc
        self.call_exc = call_exc
        self.initialized = False
        self.closed = False
        self.calls: list[tuple[str, dict | None]] = []

    def initialize(self) -> dict:
        if self.init_exc is not None:
            raise self.init_exc
        self.initialized = True
        return {}

    def list_tools(self, *, refresh: bool = False) -> list[McpToolInfo]:
        return list(self.tools)

    def find_tools(self, keywords, *, explicit_names=()) -> list[McpToolInfo]:
        return score_tools(self.tools, keywords, explicit_names=explicit_names)

    def call_tool(self, name: str, arguments: dict | None = None, *, timeout: float | None = None) -> McpToolResult:
        self.calls.append((name, arguments))
        if self.call_exc is not None:
            raise self.call_exc
        assert self.call_result is not None
        return self.call_result

    def close(self) -> None:
        self.closed = True


class FakeResponse:
    def __init__(self, status_code: int = 200, json_data=None, json_exc: Exception | None = None):
        self.status_code = status_code
        self._json_data = json_data
        self._json_exc = json_exc

    def json(self):
        if self._json_exc is not None:
            raise self._json_exc
        return self._json_data


class FakeSession:
    def __init__(self, responses: dict[str, FakeResponse | Exception]) -> None:
        self._responses = responses
        self.requests: list[dict] = []
        self.closed = False

    def post(self, url, *, json=None, timeout=None, allow_redirects=True, verify=True):
        self.requests.append(
            {
                "url": url,
                "json": json,
                "timeout": timeout,
                "allow_redirects": allow_redirects,
                "verify": verify,
            }
        )
        outcome = self._responses.get(url)
        if outcome is None:
            return FakeResponse(status_code=404)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self) -> None:
        self.closed = True


def _fetch_bridge(url: str) -> tuple[int, dict[str, str], str]:
    parsed = urlsplit(url)
    assert parsed.hostname == "127.0.0.1"
    conn = http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=3.0)
    try:
        conn.request("GET", parsed.path, headers={"Host": f"127.0.0.1:{parsed.port}"})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, headers, body
    finally:
        conn.close()


# ------------------------------------------------------------------ admin_url
def test_admin_url_and_invalid_site_never_raises_on_open():
    svc = AdminJumpService(opener=lambda u: True)
    assert svc.admin_url(make_site()) == "https://site-a.example.com/system/"
    assert svc.admin_url(make_site(admin_path="manage")) == "https://site-a.example.com/manage/"

    bad_url_site = make_site(url="")
    with pytest.raises(ValueError):
        svc.admin_url(bad_url_site)
    res = svc.open(bad_url_site)
    assert res.success is False
    assert res.tier == JUMP_TIER_DIRECT
    assert res.display_url == ""
    assert "网址" in res.message

    bad_path_site = make_site(admin_path="/../etc")
    res_path = svc.open(bad_path_site)
    assert res_path.success is False
    assert "后台路径无效" in res_path.message


# ------------------------------------------------------------ Tier 1: ticket
def test_tier1_mcp_ticket_url_via_bridge_and_no_secret_in_opener():
    ticket_secret = "one-time-ticket-SECRET999"
    ticket_url = f"https://site-a.example.com/system/?ticket={ticket_secret}"
    tool = McpToolInfo(
        name="create_login_ticket",
        description="生成后台一次性跳转票据",
        input_schema={"type": "object", "properties": {"redirect_url": {"type": "string"}}},
    )
    fake_client = FakeMcpClient(
        tools=[tool],
        call_result=McpToolResult(
            is_error=False,
            text="ok",
            structured={"login_url": ticket_url},
        ),
    )
    opened: list[str] = []
    with AdminJumpService(
        mcp_client_factory=lambda site: fake_client,
        opener=lambda u: (opened.append(u), True)[1],
    ) as svc:
        result = svc.open(make_site())
        assert result.success is True
        assert result.tier == JUMP_TIER_TICKET
        assert result.display_url == "https://site-a.example.com/system/"
        assert result.fallback_reasons == ()
        assert fake_client.initialized is True
        assert fake_client.closed is True
        assert fake_client.calls == [
            ("create_login_ticket", {"redirect_url": "https://site-a.example.com/system/"})
        ]

        # Opener received the local loopback bridge URL, never the ticket!
        assert len(opened) == 1
        assert opened[0].startswith("http://127.0.0.1:")
        assert ticket_secret not in opened[0]
        assert "mcp-secret-token-1234" not in opened[0]

        # Following the one-shot bridge URL redirects to the exact ticket URL.
        status, headers, _ = _fetch_bridge(opened[0])
        assert status == 302
        assert headers["location"] == ticket_url


def test_tier1_mcp_ticket_value_with_template():
    ticket_secret = "tkt+a/b=1234"
    tool = McpToolInfo(name="admin_login_ticket", description="ticket")
    fake_client = FakeMcpClient(
        tools=[tool],
        call_result=McpToolResult(
            is_error=False,
            text="",
            structured={"data": {"ticket": ticket_secret}},
        ),
    )
    settings = IntegrationSettings(
        admin_jump=AdminJumpSettings(ticket_url_template="{admin_url}?ticket={ticket}")
    )
    opened: list[str] = []
    with AdminJumpService(
        settings=settings,
        mcp_client_factory=lambda site: fake_client,
        opener=lambda u: (opened.append(u), True)[1],
    ) as svc:
        result = svc.open(make_site())
        assert result.success is True
        assert result.tier == JUMP_TIER_TICKET
        assert ticket_secret not in opened[0]

        status, headers, _ = _fetch_bridge(opened[0])
        assert status == 302
        assert headers["location"] == "https://site-a.example.com/system/?ticket=tkt%2Ba%2Fb%3D1234"


def test_tier1_rejects_cross_origin_url_and_falls_back_to_direct():
    tool = McpToolInfo(name="create_login_ticket", description="jump")
    fake_client = FakeMcpClient(
        tools=[tool],
        call_result=McpToolResult(
            is_error=False,
            text="https://evil.attacker.example/steal?ticket=secret-xyz",
            structured={"login_url": "https://evil.attacker.example/steal?ticket=secret-xyz"},
        ),
    )
    opened: list[str] = []
    with AdminJumpService(
        mcp_client_factory=lambda site: fake_client,
        opener=lambda u: (opened.append(u), True)[1],
    ) as svc:
        result = svc.open(make_site())
        assert result.success is True
        assert result.tier == JUMP_TIER_DIRECT
        assert opened == ["https://site-a.example.com/system/"]
        assert any("域名" in reason and "拒绝" in reason for reason in result.fallback_reasons)
        assert "secret-xyz" not in " ".join(result.fallback_reasons)


def test_tier1_mcp_errors_redact_secrets_and_fall_through():
    secret_tok = "mcp-secret-token-1234"
    for exc in (
        McpScopeError(f"scope denied for Bearer {secret_tok}"),
        McpAuthError(f"auth failed token={secret_tok}"),
        McpUnavailableError(f"timeout for {secret_tok}"),
    ):
        client = FakeMcpClient(init_exc=exc)
        opened: list[str] = []
        with AdminJumpService(
            mcp_client_factory=lambda site, c=client: c,
            opener=lambda u: (opened.append(u), True)[1],
        ) as svc:
            res = svc.open(make_site())
            assert res.success is True
            assert res.tier == JUMP_TIER_DIRECT
            assert client.closed is True
            assert len(res.fallback_reasons) == 1
            assert secret_tok not in res.fallback_reasons[0]
            assert secret_tok not in res.message


def test_tier1_ticket_without_template_falls_through():
    tool = McpToolInfo(name="login_ticket", description="ticket")
    client = FakeMcpClient(
        tools=[tool],
        call_result=McpToolResult(is_error=False, text="", structured={"ticket": "only-ticket-12345"}),
    )
    opened: list[str] = []
    with AdminJumpService(
        mcp_client_factory=lambda site: client,
        opener=lambda u: (opened.append(u), True)[1],
    ) as svc:
        res = svc.open(make_site())
        assert res.tier == JUMP_TIER_DIRECT
        assert any("模板" in r for r in res.fallback_reasons)
        assert "only-ticket-12345" not in " ".join(res.fallback_reasons)


# ----------------------------------------------------------- Tier 2: bridge
def test_tier2_silent_login_with_handoff_template_and_second_path_fallback():
    login_token = "admin-jwt-SECRET8888"
    admin_pwd = "super-secret-pwd-777"
    fake_session = FakeSession(
        {
            "https://site-a.example.com/system/api/login": FakeResponse(status_code=404),
            "https://site-a.example.com/system/api/user/login": FakeResponse(
                status_code=200,
                json_data={"code": 0, "data": {"token": login_token}},
            ),
        }
    )
    settings = IntegrationSettings(
        mcp=McpSettings(verify_ssl=False),
        admin_jump=AdminJumpSettings(
            enable_ticket=False,
            token_handoff_url_template="{admin_url}#token={token}",
        ),
    )
    opened: list[str] = []
    site = make_site(admin_username="admin", admin_password=admin_pwd)
    with AdminJumpService(
        settings=settings,
        session_factory=lambda: fake_session,
        opener=lambda u: (opened.append(u), True)[1],
    ) as svc:
        res = svc.open(site)
        assert res.success is True
        assert res.tier == JUMP_TIER_BRIDGE
        assert res.display_url == "https://site-a.example.com/system/"
        assert fake_session.closed is True

        # Verify request rules: allow_redirects=False, verify=False, JSON credentials.
        assert len(fake_session.requests) == 2
        for req in fake_session.requests:
            assert req["allow_redirects"] is False
            assert req["verify"] is False
            assert req["json"] == {"user_name": "admin", "password": admin_pwd}

        # Opener never gets token or password.
        assert opened[0].startswith("http://127.0.0.1:")
        assert login_token not in opened[0]
        assert admin_pwd not in opened[0]

        status, headers, _ = _fetch_bridge(opened[0])
        assert status == 302
        assert headers["location"] == f"https://site-a.example.com/system/#token={login_token}"


def test_tier2_silent_login_autopost_mode():
    admin_pwd = "My<Secret>&Pwd"
    fake_session = FakeSession(
        {
            "https://site-a.example.com/system/api/login": FakeResponse(
                status_code=200,
                json_data={"code": 0, "data": {"token": "tok-12345"}},
            ),
        }
    )
    settings = IntegrationSettings(
        admin_jump=AdminJumpSettings(enable_ticket=False, autopost_login=True)
    )
    opened: list[str] = []
    site = make_site(admin_username="admin", admin_password=admin_pwd)
    with AdminJumpService(
        settings=settings,
        session_factory=lambda: fake_session,
        opener=lambda u: (opened.append(u), True)[1],
    ) as svc:
        res = svc.open(site)
        assert res.success is True
        assert res.tier == JUMP_TIER_BRIDGE
        assert admin_pwd not in opened[0]

        status, headers, body = _fetch_bridge(opened[0])
        assert status == 200
        assert "form-action https://site-a.example.com" in headers["content-security-policy"]
        assert "action='https://site-a.example.com/system/api/login'" in body
        assert "My&lt;Secret&gt;&amp;Pwd" in body


def test_tier2_default_unconfigured_handoff_falls_through_safely():
    fake_session = FakeSession(
        {
            "https://site-a.example.com/system/api/login": FakeResponse(
                status_code=200,
                json_data={"code": 0, "data": {"token": "tok-abc123"}},
            ),
        }
    )
    opened: list[str] = []
    site = make_site(mcp_token="", jiekou="", admin_username="admin", admin_password="secret-password")
    with AdminJumpService(
        session_factory=lambda: fake_session,
        opener=lambda u: (opened.append(u), True)[1],
    ) as svc:
        res = svc.open(site)
        assert res.success is True
        assert res.tier == JUMP_TIER_DIRECT
        assert opened == ["https://site-a.example.com/system/"]
        assert res.fallback_reasons == ("账号已校验，站点未配置免密交接方式",)


def test_tier2_wrong_password_and_network_errors_fall_through():
    cases = [
        (FakeResponse(status_code=401), "后台账号或密码错误"),
        (FakeResponse(status_code=200, json_data={"code": -1, "msg": "用户名或密码错误"}), "后台账号或密码错误"),
        (FakeResponse(status_code=302), "重定向"),
        (requests.exceptions.SSLError("bad cert"), "证书"),
        (requests.exceptions.Timeout("slow"), "超时"),
    ]
    for outcome, expected_snippet in cases:
        fake_session = FakeSession({"https://site-a.example.com/system/api/login": outcome})
        opened: list[str] = []
        site = make_site(mcp_token="", jiekou="", admin_username="admin", admin_password="wrong-password")
        with AdminJumpService(
            session_factory=lambda s=fake_session: s,
            opener=lambda u: (opened.append(u), True)[1],
        ) as svc:
            res = svc.open(site)
            assert res.success is True
            assert res.tier == JUMP_TIER_DIRECT
            assert opened == ["https://site-a.example.com/system/"]
            assert any(expected_snippet in r for r in res.fallback_reasons)
            assert "wrong-password" not in " ".join(res.fallback_reasons)


def test_tier2_handoff_template_origin_mismatch_rejected():
    fake_session = FakeSession(
        {
            "https://site-a.example.com/system/api/login": FakeResponse(
                status_code=200,
                json_data={"code": 0, "data": {"token": "tok-secret-99"}},
            ),
        }
    )
    settings = IntegrationSettings(
        admin_jump=AdminJumpSettings(
            enable_ticket=False,
            token_handoff_url_template="https://evil.example.com/steal?token={token}",
        )
    )
    opened: list[str] = []
    site = make_site(admin_username="admin", admin_password="secret-password")
    with AdminJumpService(
        settings=settings,
        session_factory=lambda: fake_session,
        opener=lambda u: (opened.append(u), True)[1],
    ) as svc:
        res = svc.open(site)
        assert res.tier == JUMP_TIER_DIRECT
        assert opened == ["https://site-a.example.com/system/"]
        assert any("域名" in r and "拒绝" in r for r in res.fallback_reasons)


# ---------------------------------------------- Full chain + Tier 3 + opener
def test_full_tier1_to_tier2_to_tier3_fallthrough_collects_reasons():
    client = FakeMcpClient(tools=[])  # No ticket tools
    fake_session = FakeSession(
        {"https://site-a.example.com/system/api/login": FakeResponse(status_code=403)}
    )
    opened: list[str] = []
    site = make_site(admin_username="admin", admin_password="secret-password")
    with AdminJumpService(
        mcp_client_factory=lambda s: client,
        session_factory=lambda: fake_session,
        opener=lambda u: (opened.append(u), True)[1],
    ) as svc:
        res = svc.open(site)
        assert res.success is True
        assert res.tier == JUMP_TIER_DIRECT
        assert opened == ["https://site-a.example.com/system/"]
        assert len(res.fallback_reasons) == 2
        assert "票据工具" in res.fallback_reasons[0]
        assert "密码错误" in res.fallback_reasons[1]


def test_tier3_direct_and_opener_failure_never_raises():
    site = make_site(mcp_token="", jiekou="", admin_username="", admin_password="")

    svc_false = AdminJumpService(opener=lambda u: False)
    res_false = svc_false.open(site)
    assert res_false.success is False
    assert res_false.tier == JUMP_TIER_DIRECT
    assert res_false.display_url == "https://site-a.example.com/system/"

    def broken_opener(url: str) -> bool:
        raise RuntimeError("no browser found")

    svc_err = AdminJumpService(opener=broken_opener)
    res_err = svc_err.open(site)
    assert res_err.success is False
    assert "打开浏览器失败" in res_err.message


def test_jump_plan_repr_hides_target_url():
    bridge = JumpBridge()
    tool = McpToolInfo(name="login_ticket", description="ticket")
    client = FakeMcpClient(
        tools=[tool],
        call_result=McpToolResult(
            is_error=False,
            text="",
            structured={"login_url": "https://site-a.example.com/system/?ticket=topsecret"},
        ),
    )
    with AdminJumpService(mcp_client_factory=lambda s: client, bridge=bridge) as svc:
        plan = svc.resolve(make_site())
        assert plan.tier == JUMP_TIER_TICKET
        assert "127.0.0.1" not in repr(plan)
        assert "topsecret" not in repr(plan)


# -------------------------------------------------------------- open_many
def test_open_many_caps_batch_max_staggers_and_reports_progress():
    sites = [
        make_site(
            name=f"站点{i}",
            url=f"https://site-{i}.example.com",
            mcp_token="",
            jiekou="",
        )
        for i in range(5)
    ]
    settings = IntegrationSettings(
        admin_jump=AdminJumpSettings(batch_max=3, batch_stagger_ms=250)
    )
    opened: list[str] = []
    sleeps: list[float] = []
    progress_events: list[tuple[int, int, str]] = []

    svc = AdminJumpService(
        settings=settings,
        opener=lambda u: (opened.append(u), True)[1],
        sleep=sleeps.append,
    )
    results = svc.open_many(
        sites,
        progress=lambda done, total, res: progress_events.append((done, total, res.site_name)),
    )

    assert len(results) == 3
    assert [r.site_name for r in results] == ["站点0", "站点1", "站点2"]
    assert opened == [
        "https://site-0.example.com/system/",
        "https://site-1.example.com/system/",
        "https://site-2.example.com/system/",
    ]
    assert sleeps == [0.25, 0.25]
    assert progress_events == [(1, 3, "站点0"), (2, 3, "站点1"), (3, 3, "站点2")]


def test_open_many_honors_cancel_event():
    sites = [
        make_site(name=f"站点{i}", url=f"https://site-{i}.example.com", mcp_token="", jiekou="")
        for i in range(4)
    ]
    cancel = threading.Event()
    opened: list[str] = []

    def cancel_after_second(url: str) -> bool:
        opened.append(url)
        if len(opened) == 2:
            cancel.set()
        return True

    svc = AdminJumpService(
        settings=IntegrationSettings(admin_jump=AdminJumpSettings(batch_stagger_ms=100)),
        opener=cancel_after_second,
        sleep=lambda _: None,
    )
    results = svc.open_many(sites, cancel_event=cancel)
    assert len(results) == 2
    assert len(opened) == 2
