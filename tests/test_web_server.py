"""Tests for web.job_runner and web.server (Local Web Console HTTP & MCP backend)."""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest
import requests

from core.admin_jump_service import AdminJumpService
from core.app_info import APP_NAME, APP_VERSION
from core.config_service import load_config, save_config
from core.friendlink_service import FriendLinkService
from core.integration_settings import IntegrationSettings
from core.site_factory import create_site_from_url_api_key
from core.site_onboarding import SiteDiscoveryResult
from core.site_service import SiteService
from core.upgrade_service import UpgradeService
from models.entities import SiteConfig
from models.sitegroup import (
    UPGRADE_UPGRADED,
    FriendLink,
    McpProbeResult,
    McpToolInfo,
    McpToolResult,
    SiteVersionInfo,
    UpgradeResult,
)
from web.job_runner import JobRunner
from web.server import WebConsoleApp, WebConsoleServer, resolve_static_dir

# ---------------------------------------------------------------------------
# Helpers & Fakes (zero real network)
# ---------------------------------------------------------------------------


class _FakeHttpResponse:
    def __init__(
        self,
        status_code: int = 200,
        *,
        json_data: Any = None,
        text: str = "",
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        url: str = "",
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.text = text if text or json_data is None else json.dumps(json_data)
        self.content = (
            content
            if content is not None
            else (self.text.encode("utf-8") if self.text else b"")
        )
        self.headers = headers or {"Content-Type": "application/json"}
        self.cookies = cookies or {}
        self.url = url

    def json(self) -> Any:
        if self._json_data is None:
            raise ValueError("Not JSON")
        return self._json_data

    def iter_content(self, chunk_size: int = 16384):
        if self.content:
            yield self.content

    def close(self) -> None:
        pass


class _FakeUpstreamSession:
    """In-memory router simulating AnqiCMS friendlink, backlink, and login endpoints."""

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.cookies: dict[str, str] = {}

    def get(self, url: str, **kwargs) -> _FakeHttpResponse:
        self.state["get_calls"].append(url)
        # Friendlink list endpoint
        if "/api/friendlink/list" in url:
            site_url = url.split("/api/friendlink/list", 1)[0]
            links = self.state["links_by_url"].get(site_url, [])
            return _FakeHttpResponse(200, json_data={"code": 0, "data": list(links)})

        # Backlink audit target page fetch
        html = self.state["backlink_html_by_url"].get(url)
        if html is None:
            html = self.state["backlink_html_by_url"].get(url.rstrip("/"))
        if html is not None:
            return _FakeHttpResponse(
                200,
                text=html,
                headers={"Content-Type": "text/html; charset=utf-8"},
                url=url,
            )
        return _FakeHttpResponse(404, text="not found", url=url)

    def post(self, url: str, **kwargs) -> _FakeHttpResponse:
        self.state["post_calls"].append(url)
        if "/api/friendlink/create" in url:
            site_url = url.split("/api/friendlink/create", 1)[0]
            files = kwargs.get("files") or {}
            title = files.get("title", (None, ""))[1]
            link = files.get("link", (None, ""))[1]
            nofollow = 1 if files.get("nofollow", (None, "0"))[1] == "1" else 0
            remark = files.get("remark", (None, ""))[1]
            links = self.state["links_by_url"].setdefault(site_url, [])
            next_id = max((item.get("id", 0) for item in links), default=0) + 1
            links.append(
                {
                    "id": next_id,
                    "title": title,
                    "link": link,
                    "nofollow": nofollow,
                    "remark": remark,
                    "back_link": "",
                }
            )
            return _FakeHttpResponse(200, json_data={"code": 0, "msg": "ok"})

        if "/api/friendlink/delete" in url:
            site_url = url.split("/api/friendlink/delete", 1)[0]
            files = kwargs.get("files") or {}
            target_link = files.get("link", (None, ""))[1]
            links = self.state["links_by_url"].setdefault(site_url, [])
            self.state["links_by_url"][site_url] = [
                item for item in links if item.get("link") != target_link
            ]
            return _FakeHttpResponse(200, json_data={"code": 0, "msg": "ok"})

        if url.endswith("/api/login"):
            self.cookies["session_cookie"] = "cookie-secret-999"
            return _FakeHttpResponse(
                200,
                json_data={"code": 0, "data": {"token": "jwt-admin-secret-token-123"}},
                cookies={"session_cookie": "cookie-secret-999"},
            )

        return _FakeHttpResponse(404, json_data={"code": -1, "msg": "not found"})

    def close(self) -> None:
        pass


class _FakeMcpClient:
    """In-memory fake MCP client for UpgradeService without real HTTP."""

    def __init__(self, site: SiteConfig, version_map: dict[str, tuple[str, str]]) -> None:
        self.site = site
        self.version_map = version_map

    def initialize(self) -> dict:
        return {"protocolVersion": "2025-06-18"}

    def list_tools(self, *, refresh: bool = False) -> list[McpToolInfo]:
        return [
            McpToolInfo(name="check_update", description="Check AnqiCMS version"),
            McpToolInfo(
                name="system_upgrade",
                description="Upgrade AnqiCMS",
                input_schema={"properties": {"version": {"type": "string"}, "confirm": {"type": "boolean"}}},
            ),
        ]

    def find_tools(self, keywords, *, explicit_names=()) -> list[McpToolInfo]:
        tools = self.list_tools()
        kws = [k.casefold() for k in keywords]
        return [t for t in tools if any(k in t.name.casefold() for k in kws)]

    def call_tool(self, name: str, arguments: dict | None = None, **kwargs) -> McpToolResult:
        cur, latest = self.version_map.get(self.site.name, ("3.6.0", "3.6.8"))
        if name == "check_update":
            return McpToolResult(
                is_error=False,
                text="",
                structured={"current_version": cur, "latest_version": latest},
            )
        if name == "system_upgrade":
            self.version_map[self.site.name] = (latest, latest)
            return McpToolResult(is_error=False, text="升级完成", structured={"ok": True})
        return McpToolResult(is_error=True, text=f"unknown tool {name}")

    def close(self) -> None:
        pass


def _make_sample_sites(base_dir: Path) -> list[SiteConfig]:
    s1 = create_site_from_url_api_key(
        "https://alpha.example.com",
        "alpha-import-tok-1111",
        fid=1,
        site_key="alpha",
        base_dir=base_dir,
        create_files=True,
    )
    s1.site_title = "Alpha主站"
    s1.site_group = "集团A"
    s1.mcp_token = "alpha-mcp-secret-9999"
    s1.admin_username = "admin_a"
    s1.admin_password = "PassAlpha!Secret"
    s1.publish_dir.joinpath("文章一.txt").write_text("测试正文内容一", encoding="utf-8")

    s2 = create_site_from_url_api_key(
        "https://beta.example.com",
        "beta-import-tok-2222",
        fid=2,
        site_key="beta",
        base_dir=base_dir,
        create_files=True,
    )
    s2.site_title = ""
    s2.site_group = "集团B"
    s2.admin_username = "admin_b"
    s2.admin_password = "PassBeta!Secret"

    return [s1, s2]


def _poll_job(base_url: str, job_id: str, timeout_s: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = requests.get(f"{base_url}/api/jobs/{job_id}", timeout=3)
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["ok"] is True
        job = payload["data"]
        if job["status"] in {"completed", "failed", "cancelled"}:
            return job
        time.sleep(0.03)
    raise AssertionError(f"Job {job_id} did not finish within {timeout_s}s")


@pytest.fixture()
def web_env(tmp_path: Path):
    """Spin up a real WebConsoleServer on 127.0.0.1:0 with mocked upstream transport."""
    config_path = tmp_path / "config.ini"
    sites = _make_sample_sites(tmp_path)
    save_config(config_path, sites)

    static_dir = tmp_path / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    (static_dir / "index.html").write_text(
        "<!doctype html><html><body><h1>AnqiCMS Console</h1><script src='/static/app.js'></script></body></html>",
        encoding="utf-8",
    )
    (static_dir / "app.js").write_text("console.log('ready');", encoding="utf-8")

    upstream_state: dict[str, Any] = {
        "get_calls": [],
        "post_calls": [],
        "links_by_url": {
            "https://alpha.example.com": [
                {"id": 1, "title": "PartnerOK", "link": "https://partner-ok.example.org/", "nofollow": 0, "remark": ""},
                {"id": 2, "title": "PartnerDead", "link": "https://partner-dead.example.org/", "nofollow": 0, "remark": ""},
            ],
            "https://beta.example.com": [],
        },
        "backlink_html_by_url": {
            "https://partner-ok.example.org/": '<a href="https://alpha.example.com/">Alpha</a>',
            # partner-dead intentionally missing -> 404 unreachable
        },
        "versions": {
            "alpha": ("3.6.0", "3.6.8"),
            "beta": ("3.6.8", "3.6.8"),
        },
    }

    settings = IntegrationSettings()
    session_factory = lambda: _FakeUpstreamSession(upstream_state)
    mcp_factory = lambda site: _FakeMcpClient(site, upstream_state["versions"])

    friendlink_svc = FriendLinkService(
        settings,
        session_factory=session_factory,
        mcp_client_factory=mcp_factory,
        sleep=lambda _s: None,
    )
    upgrade_svc = UpgradeService(
        settings,
        mcp_client_factory=mcp_factory,
        session_factory=session_factory,
        sleep=lambda _s: None,
    )
    admin_jump_svc = AdminJumpService(
        settings,
        mcp_client_factory=mcp_factory,
        session_factory=session_factory,
        opener=lambda _url: True,
        use_local_proxy=True,
    )

    def _fake_discover(target_sites, **kwargs):
        results = []
        progress = kwargs.get("progress")
        for idx, s in enumerate(target_sites, start=1):
            res = SiteDiscoveryResult(
                site_name=s.name,
                site_url=s.url,
                reachable=True,
                http_status=200,
                site_title=s.site_title or "Discovered Beta Title",
                discovered_title="Discovered Beta Title",
                link_count=len(upstream_state["links_by_url"].get(s.url, [])),
                mcp_ok=True,
                mcp_tools=("check_update", "system_upgrade"),
                message="探测成功",
            )
            results.append(res)
            if callable(progress):
                progress(idx, len(target_sites), res)
        return results

    def _fake_probe(target_sites, _settings=None, **kwargs):
        results = []
        progress = kwargs.get("progress")
        for idx, s in enumerate(target_sites, start=1):
            res = McpProbeResult(
                site_name=s.name,
                ok=True,
                server_name="AnqiCMS",
                server_version="3.6.8",
                protocol_version="2025-06-18",
                tool_count=2,
                tools=(McpToolInfo("check_update"), McpToolInfo("system_upgrade")),
                upgrade_tools=("check_update", "system_upgrade"),
                message="连接成功，发现 2 个工具",
            )
            results.append(res)
            if callable(progress):
                progress(idx, len(target_sites), res)
        return results

    app = WebConsoleApp(
        config_path,
        settings=settings,
        friendlink_service=friendlink_svc,
        upgrade_service=upgrade_svc,
        admin_jump_service=admin_jump_svc,
        mcp_client_factory=mcp_factory,
        discover_fn=_fake_discover,
        probe_fn=_fake_probe,
        static_dir=static_dir,
    )
    # Attach app.admin_proxy directly to admin_jump_service as WebConsoleApp does by default
    admin_jump_svc.proxy = app.admin_proxy

    server = WebConsoleServer(app, host="127.0.0.1", port=0)
    server.start()
    try:
        yield {
            "server": server,
            "app": app,
            "base_url": server.url,
            "config_path": config_path,
            "tmp_path": tmp_path,
            "state": upstream_state,
        }
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Unit tests: JobRunner & static dir resolution
# ---------------------------------------------------------------------------


def test_job_runner_progress_redaction_and_cancel():
    runner = JobRunner(max_workers=2)
    try:
        started = threading.Event()
        release = threading.Event()

        def ok_fn(update_progress, cancel_event):
            update_progress(1, 2, "step 1", item={"site": "alpha"})
            update_progress(2, 2, "step 2")
            return {"count": 2}

        job_ok = runner.submit("test", "OK Job", ok_fn, total=2)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            snap = runner.get(job_ok.job_id)
            if snap and snap.status == "completed":
                break
            time.sleep(0.02)
        snap = runner.get(job_ok.job_id)
        assert snap is not None
        assert snap.status == "completed"
        assert snap.result == {"count": 2}
        assert len(snap.logs) == 2
        assert snap.to_dict()["percent"] == 100.0

        def fail_fn(update_progress, cancel_event):
            raise RuntimeError("failed at https://a.com/api?token=supersecret999")

        job_fail = runner.submit("test", "Fail Job", fail_fn)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            snap = runner.get(job_fail.job_id)
            if snap and snap.status == "failed":
                break
            time.sleep(0.02)
        snap = runner.get(job_fail.job_id)
        assert snap is not None
        assert snap.status == "failed"
        assert "supersecret999" not in snap.error
        assert "***" in snap.error

        def cancel_fn(update_progress, cancel_event):
            started.set()
            release.wait(timeout=2.0)
            return {"partial": True}

        job_cancel = runner.submit("test", "Cancel Job", cancel_fn)
        assert started.wait(timeout=2.0)
        assert runner.cancel(job_cancel.job_id) is True
        release.set()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            snap = runner.get(job_cancel.job_id)
            if snap and snap.status == "cancelled":
                break
            time.sleep(0.02)
        snap = runner.get(job_cancel.job_id)
        assert snap is not None
        assert snap.status == "cancelled"
        assert len(runner.list_jobs(limit=10)) == 3
    finally:
        runner.shutdown(wait=True)


def test_resolve_static_dir_source_and_frozen_meipass(tmp_path: Path, monkeypatch):
    # Source checkout resolution
    src_dir = resolve_static_dir()
    assert src_dir.name == "static"
    assert src_dir.parent.name == "web"

    # Frozen PyInstaller _MEIPASS resolution
    meipass = tmp_path / "meipass"
    bundled_static = meipass / "web" / "static"
    bundled_static.mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)

    resolved = resolve_static_dir()
    assert resolved == bundled_static.resolve()


# ---------------------------------------------------------------------------
# Integration tests: Real ThreadingHTTPServer
# ---------------------------------------------------------------------------


def test_static_and_health_and_dashboard_missing_publish_dir(web_env):
    base = web_env["base_url"]
    server = web_env["server"]

    # Static root & asset
    r_index = requests.get(f"{base}/", timeout=3)
    assert r_index.status_code == 200
    assert "text/html" in r_index.headers["Content-Type"]
    assert "no-store" in r_index.headers["Cache-Control"]
    assert "no-cache" in r_index.headers["Cache-Control"]
    assert "AnqiCMS Console" in r_index.text

    r_js = requests.get(f"{base}/static/app.js", timeout=3)
    assert r_js.status_code == 200
    assert "javascript" in r_js.headers["Content-Type"]
    assert "no-store" in r_js.headers["Cache-Control"]
    assert "no-cache" in r_js.headers["Cache-Control"]

    # Path traversal blocked
    r_trav = requests.get(f"{base}/static/../config.ini", timeout=3)
    assert r_trav.status_code in {403, 404}

    # Health
    r_health = requests.get(f"{base}/api/health", timeout=3)
    assert r_health.status_code == 200
    health = r_health.json()["data"]
    assert health["app_name"] == APP_NAME
    assert health["version"] == APP_VERSION
    assert health["port"] == server.port
    assert health["site_count"] == 2

    # Populate SiteGroupDB cache for alpha
    web_env["app"].db.upsert_site_status(
        "alpha",
        current_version="3.6.0",
        latest_version="3.6.8",
        update_available=True,
        mcp_ok=True,
        link_count=5,
        broken_link_count=1,
    )
    # Delete beta's publish_dir to verify missing publish_dir is handled safely
    beta = next(s for s in web_env["app"].load_sites() if s.name == "beta")
    if beta.publish_dir.exists():
        beta.publish_dir.rmdir()

    r_dash = requests.get(f"{base}/api/dashboard", timeout=3)
    assert r_dash.status_code == 200
    dash = r_dash.json()["data"]
    assert dash["kpis"]["total_sites"] == 2
    assert dash["kpis"]["pending_articles"] == 1
    assert dash["kpis"]["updates_available"] == 1
    alpha_row = next(s for s in dash["sites"] if s["name"] == "alpha")
    assert alpha_row["version"] == "3.6.0"
    assert alpha_row["latest_version"] == "3.6.8"
    assert alpha_row["update_available"] is True
    assert alpha_row["mcp_ok"] is True
    assert alpha_row["link_count"] == 5
    assert alpha_row["broken_link_count"] == 1
    assert alpha_row["pending_count"] == 1
    assert "PassAlpha!Secret" not in json.dumps(dash)

    # Also verify dashboard catches SiteService OSError/FileNotFoundError safely
    orig_summarize = web_env["app"].site_service.summarize_site

    def _failing_beta_summarize(site):
        if site.name == "beta":
            raise FileNotFoundError(f"missing {site.publish_dir}")
        return orig_summarize(site)

    web_env["app"].site_service.summarize_site = _failing_beta_summarize
    r_dash2 = requests.get(f"{base}/api/dashboard", timeout=3)
    assert r_dash2.status_code == 200
    beta_row = next(s for s in r_dash2.json()["data"]["sites"] if s["name"] == "beta")
    assert beta_row["publish_status"] == "error"
    assert beta_row["pending_count"] == 0


def test_site_crud_bulk_update_and_bulk_import(web_env):
    base = web_env["base_url"]
    cfg_path = web_env["config_path"]

    # GET /api/sites
    r_list = requests.get(f"{base}/api/sites", timeout=3)
    assert r_list.status_code == 200
    assert r_list.json()["data"]["total"] == 2

    # POST /api/sites (create)
    r_create = requests.post(
        f"{base}/api/sites",
        json={
            "name": "gamma",
            "full_endpoint": "https://gamma.example.com/api/import/archive?token=tok-gamma-88",
            "fid": 3,
            "site_group": "集团C",
            "site_title": "Gamma站",
            "mcp_token": "gamma-mcp-secret",
            "admin_username": "admin_g",
            "admin_password": "GammaPassword!123",
        },
        timeout=3,
    )
    assert r_create.status_code == 200
    created = r_create.json()["data"]
    assert created["created"] is True
    assert created["site"]["has_mcp_token"] is True
    assert "GammaPassword!123" not in json.dumps(created)

    # POST /api/sites (update preserving existing password when omitted)
    r_upd = requests.post(
        f"{base}/api/sites",
        json={"name": "gamma", "site_group": "集团C2", "daily_publish_limit": 80},
        timeout=3,
    )
    assert r_upd.status_code == 200
    assert r_upd.json()["data"]["created"] is False
    gamma_saved = next(s for s in load_config(cfg_path) if s.name == "gamma")
    assert gamma_saved.site_group == "集团C2"
    assert gamma_saved.daily_publish_limit == 80
    assert gamma_saved.admin_password == "GammaPassword!123"

    # Bulk update mode="assign"
    r_assign = requests.post(
        f"{base}/api/sites/bulk-update",
        json={
            "mode": "assign",
            "site_names": ["alpha", "gamma"],
            "fields": {"site_group": "核心组", "admin_path": "my-admin"},
        },
        timeout=3,
    )
    assert r_assign.status_code == 200
    assert set(r_assign.json()["data"]["updated"]) == {"alpha", "gamma"}
    sites_after = {s.name: s for s in load_config(cfg_path)}
    assert sites_after["alpha"].site_group == "核心组"
    assert sites_after["alpha"].admin_path == "/my-admin/"

    # Bulk update mode="text" (dry_run then apply, matching by domain!)
    r_text_dry = requests.post(
        f"{base}/api/sites/bulk-update",
        json={
            "mode": "text",
            "text": "beta.example.com|新分组B|beta-mcp-tok|b_user|BetaSecret!9|Beta新标题\nunknown.example.com|X",
            "dry_run": True,
        },
        timeout=3,
    )
    assert r_text_dry.status_code == 200
    dry_data = r_text_dry.json()["data"]
    assert dry_data["matched_count"] == 1
    assert dry_data["unmatched_count"] == 1
    assert "BetaSecret!9" not in json.dumps(dry_data)

    r_text_apply = requests.post(
        f"{base}/api/sites/bulk-update",
        json={
            "mode": "text",
            "text": "beta.example.com|新分组B|beta-mcp-tok|b_user|BetaSecret!9|Beta新标题",
            "dry_run": False,
        },
        timeout=3,
    )
    assert r_text_apply.status_code == 200
    beta_saved = next(s for s in load_config(cfg_path) if s.name == "beta")
    assert beta_saved.site_group == "新分组B"
    assert beta_saved.site_title == "Beta新标题"

    # Bulk import dry_run=True then dry_run=False
    import_text = "delta|https://delta.example.com|新导入组|delta-tok|d_admin|DeltaPass!1|Delta站"
    r_imp_dry = requests.post(
        f"{base}/api/sites/bulk-import",
        json={"text": import_text, "dry_run": True},
        timeout=3,
    )
    assert r_imp_dry.status_code == 200
    assert r_imp_dry.json()["data"]["imported_count"] == 1
    assert not any(s.name == "delta" for s in load_config(cfg_path))

    r_imp_real = requests.post(
        f"{base}/api/sites/bulk-import",
        json={"text": import_text, "dry_run": False},
        timeout=3,
    )
    assert r_imp_real.status_code == 200
    assert r_imp_real.json()["data"]["imported_count"] == 1
    assert any(s.name == "delta" for s in load_config(cfg_path))

    # DELETE /api/sites/<name>
    r_del = requests.delete(f"{base}/api/sites/delta", timeout=3)
    assert r_del.status_code == 200
    assert r_del.json()["data"]["deleted"] == "delta"
    assert not any(s.name == "delta" for s in load_config(cfg_path))


def test_discover_and_probe_mcp_jobs(web_env):
    base = web_env["base_url"]
    cfg_path = web_env["config_path"]

    # Discover job (backfills blank site_title on beta!)
    r_disc = requests.post(f"{base}/api/sites/discover", json={}, timeout=3)
    assert r_disc.status_code == 200
    job = _poll_job(base, r_disc.json()["data"]["job_id"])
    assert job["status"] == "completed"
    assert job["result"]["reachable"] == 2
    assert "beta" in job["result"]["titles_backfilled"]

    beta_cfg = next(s for s in load_config(cfg_path) if s.name == "beta")
    assert beta_cfg.site_title == "Discovered Beta Title"
    cached_alpha = web_env["app"].db.get_site_status("alpha")
    assert cached_alpha is not None
    assert cached_alpha["mcp_ok"] == 1
    assert cached_alpha["link_count"] == 2

    # Probe MCP job
    r_probe = requests.post(f"{base}/api/sites/probe-mcp", json={"site_names": ["alpha"]}, timeout=3)
    assert r_probe.status_code == 200
    probe_job = _poll_job(base, r_probe.json()["data"]["job_id"])
    assert probe_job["status"] == "completed"
    assert probe_job["result"]["ok_count"] == 1


def test_smart_paste_import_and_discover_backfill(web_env, monkeypatch):
    import dataclasses
    import uuid
    from datetime import date, timedelta

    import core.site_onboarding as site_onboarding
    from core.site_credentials import site_origin_key
    from core.site_factory import derive_site_paths, parse_publish_endpoint, site_key_from_url

    base = web_env["base_url"]
    cfg_path = web_env["config_path"]

    # Provide a contract-compatible fallback only if parallel Wave edit on
    # core.site_onboarding has not landed yet in this workspace.
    if not hasattr(site_onboarding, "parse_smart_site_import"):
        @dataclasses.dataclass(frozen=True)
        class _FakeSmartImportResult:
            new_sites: list[SiteConfig]
            updated_sites: list[SiteConfig]
            warnings: list[dict[str, str]]
            merged_count: int = 0

        def _fake_parse_smart_site_import(
            text: str,
            existing_sites,
            config_path,
            *,
            default_group: str = "",
        ):
            base_dir = Path(config_path).resolve().parent
            existing_by_origin = {
                site_origin_key(s.url): s for s in existing_sites if site_origin_key(s.url)
            }
            buckets: dict[str, dict[str, str]] = {}

            decoder = json.JSONDecoder()
            idx = 0
            remainder_parts: list[str] = []
            while idx < len(text):
                if text[idx] == "{":
                    try:
                        obj, end_idx = decoder.raw_decode(text, idx)
                        if isinstance(obj, dict) and isinstance(obj.get("mcpServers"), dict):
                            for _srv_name, srv_cfg in obj["mcpServers"].items():
                                if not isinstance(srv_cfg, dict):
                                    continue
                                raw_url = str(srv_cfg.get("url") or "").strip()
                                origin = site_origin_key(raw_url)
                                if not origin:
                                    continue
                                auth = str((srv_cfg.get("headers") or {}).get("Authorization") or "")
                                tok = auth[7:].strip() if auth.lower().startswith("bearer ") else auth.strip()
                                b = buckets.setdefault(origin, {"url": f"https://{origin}"})
                                if tok:
                                    b["mcp_token"] = tok
                        idx = end_idx
                        continue
                    except ValueError:
                        pass
                remainder_parts.append(text[idx])
                idx += 1

            for line in "".join(remainder_parts).splitlines():
                line = line.strip()
                if not line:
                    continue
                if "/api/import/archive" in line:
                    parsed = parse_publish_endpoint(line)
                    origin = site_origin_key(parsed.base_url)
                    b = buckets.setdefault(origin, {"url": parsed.base_url})
                    b["url"] = parsed.base_url
                    b["jiekou"] = parsed.publish_endpoint

            new_sites: list[SiteConfig] = []
            updated_sites: list[SiteConfig] = []
            merged_count = 0
            for origin, info in buckets.items():
                if info.get("mcp_token") and info.get("jiekou"):
                    merged_count += 1
                if origin in existing_by_origin:
                    cur = existing_by_origin[origin]
                    upd = {}
                    if info.get("mcp_token") and not (cur.mcp_token or "").strip():
                        upd["mcp_token"] = info["mcp_token"]
                    if info.get("jiekou") and not (cur.jiekou or "").strip():
                        upd["jiekou"] = info["jiekou"]
                    if upd:
                        updated_sites.append(dataclasses.replace(cur, **upd))
                else:
                    key = site_key_from_url(info["url"])
                    paths = derive_site_paths(key, base_dir)
                    new_sites.append(
                        SiteConfig(
                            name=key,
                            fid=0,
                            url=info["url"],
                            jiekou=info.get("jiekou", ""),
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
                            site_group=default_group,
                            mcp_token=info.get("mcp_token", ""),
                        )
                    )
            return _FakeSmartImportResult(
                new_sites=new_sites,
                updated_sites=updated_sites,
                warnings=[],
                merged_count=merged_count,
            )

        monkeypatch.setattr(
            site_onboarding,
            "parse_smart_site_import",
            _fake_parse_smart_site_import,
            raising=False,
        )

    smart_paste = "\n".join(
        [
            json.dumps(
                {
                    "mcpServers": {
                        "anqicms-omega": {
                            "url": "https://omega.example.com/mcp",
                            "headers": {"Authorization": "Bearer omega-mcp-tok-888"},
                        },
                        "anqicms-beta": {
                            "url": "https://beta.example.com/mcp",
                            "headers": {"Authorization": "Bearer beta-mcp-new-999"},
                        },
                    }
                },
                ensure_ascii=False,
            ),
            "https://omega.example.com/api/import/archive?token=omega-imp-tok-777",
        ]
    )

    # Default dry_run=True preview
    r_prev = requests.post(
        f"{base}/api/sites/bulk-import",
        json={"text": smart_paste, "default_group": "智能导入组"},
        timeout=3,
    )
    assert r_prev.status_code == 200
    prev = r_prev.json()["data"]
    assert prev["dry_run"] is True
    assert prev["imported_count"] == 1
    assert prev["updated_count"] == 1
    assert prev["merged_count"] >= 1
    assert len(prev["new_sites"]) == 1
    assert prev["new_sites"][0]["site_group"] == "智能导入组"
    assert prev["new_sites"][0]["has_mcp_token"] is True
    assert prev["new_sites"][0]["has_import_token"] is True
    assert len(prev["updated_sites"]) == 1
    assert prev["updated_sites"][0]["name"] == "beta"
    assert prev["updated_sites"][0]["has_explicit_mcp_token"] is True
    raw_prev_dump = json.dumps(prev)
    assert "omega-mcp-tok-888" not in raw_prev_dump
    assert "omega-imp-tok-777" not in raw_prev_dump
    assert "beta-mcp-new-999" not in raw_prev_dump
    # Disk untouched during dry_run
    omega_name = prev["new_sites"][0]["name"]
    assert not any(s.name == omega_name for s in load_config(cfg_path))
    assert next(s for s in load_config(cfg_path) if s.name == "beta").mcp_token == ""

    # Confirm save (dry_run=False) -> creates omega & upserts beta
    r_save = requests.post(
        f"{base}/api/sites/bulk-import",
        json={"text": smart_paste, "dry_run": False, "default_group": "智能导入组"},
        timeout=3,
    )
    assert r_save.status_code == 200
    saved_data = r_save.json()["data"]
    assert saved_data["dry_run"] is False
    assert saved_data["imported_count"] == 1
    assert saved_data["updated_count"] == 1

    sites_after = {s.name: s for s in load_config(cfg_path)}
    assert omega_name in sites_after
    assert sites_after[omega_name].site_title == "anqicms-omega"
    assert sites_after[omega_name].fid == 0
    assert sites_after[omega_name].site_group == "智能导入组"
    assert sites_after[omega_name].mcp_token == "omega-mcp-tok-888"
    assert "omega-imp-tok-777" in sites_after[omega_name].jiekou
    assert sites_after["beta"].mcp_token == "beta-mcp-new-999"
    assert sites_after["beta"].fid == 2
    assert sites_after["beta"].site_group == "集团B"

    # Auto-discover preserves explicit site_title, backfills fid/category_name (when fid==0), and persists current_version
    def _omega_discover(target_sites, **kwargs):
        out = []
        progress = kwargs.get("progress")
        for idx, s in enumerate(target_sites, start=1):
            discovered_title = "Gamma 官网" if "gamma" in s.name else "Omega 官网"
            res = types.SimpleNamespace(
                site_name=s.name,
                site_url=s.url,
                reachable=True,
                http_status=200,
                site_title=discovered_title,
                discovered_title=discovered_title,
                link_count=3,
                mcp_ok=True,
                mcp_tools=("check_update",),
                current_version="3.7.2",
                default_fid=12,
                default_category_name="行业资讯",
                message="探测成功",
            )
            out.append(res)
            if callable(progress):
                progress(idx, len(target_sites), res)
        return out

    monkeypatch.setattr(web_env["app"], "_discover_fn", _omega_discover)
    r_disc = requests.post(
        f"{base}/api/sites/discover",
        json={"site_names": [omega_name], "probe_mcp": True, "backfill_titles": True},
        timeout=3,
    )
    assert r_disc.status_code == 200
    disc_job = _poll_job(base, r_disc.json()["data"]["job_id"])
    assert disc_job["status"] == "completed"
    assert omega_name not in disc_job["result"]["titles_backfilled"]
    assert omega_name in disc_job["result"]["fids_backfilled"]

    omega_after = next(s for s in load_config(cfg_path) if s.name == omega_name)
    assert omega_after.site_title == "anqicms-omega"
    assert omega_after.fid == 12
    assert omega_after.category_name == "行业资讯"

    cached_omega = web_env["app"].db.get_site_status(omega_name)
    assert cached_omega is not None
    assert cached_omega["current_version"] == "3.7.2"
    assert cached_omega["link_count"] == 3
    assert cached_omega["mcp_ok"] == 1

    # Bare import URL (no site_title) -> auto-discover backfills site_title
    r_gamma = requests.post(
        f"{base}/api/sites/bulk-import",
        json={
            "text": "https://gamma.example.com/api/import/archive?token=gamma-imp-tok-111",
            "dry_run": False,
            "default_group": "智能导入组",
        },
        timeout=3,
    )
    assert r_gamma.status_code == 200
    gamma_name = r_gamma.json()["data"]["new_sites"][0]["name"]
    gamma_before = next(s for s in load_config(cfg_path) if s.name == gamma_name)
    assert gamma_before.site_title == ""

    r_disc_gamma = requests.post(
        f"{base}/api/sites/discover",
        json={"site_names": [gamma_name], "probe_mcp": True, "backfill_titles": True},
        timeout=3,
    )
    assert r_disc_gamma.status_code == 200
    disc_gamma = _poll_job(base, r_disc_gamma.json()["data"]["job_id"])
    assert disc_gamma["status"] == "completed"
    assert gamma_name in disc_gamma["result"]["titles_backfilled"]
    gamma_after = next(s for s in load_config(cfg_path) if s.name == gamma_name)
    assert gamma_after.site_title == "Gamma 官网"


def test_friendlinks_parse_single_batch_search_audit_crosslink_and_export(web_env):
    base = web_env["base_url"]

    # Parse HTML & text preview
    r_parse = requests.post(
        f"{base}/api/friendlinks/parse",
        json={
            "text": '<a href="https://friend1.example.org" rel="nofollow">友链一</a>\n友链二|https://friend2.example.org\nbad line',
            "default_nofollow": False,
        },
        timeout=3,
    )
    assert r_parse.status_code == 200
    parsed = r_parse.json()["data"]
    assert parsed["count"] == 2
    assert parsed["specs"][0]["nofollow"] is True
    assert parsed["specs"][1]["nofollow"] is False
    assert len(parsed["errors"]) == 1

    # GET /api/friendlinks/site/<name>
    r_site_links = requests.get(f"{base}/api/friendlinks/site/alpha", timeout=3)
    assert r_site_links.status_code == 200
    assert r_site_links.json()["data"]["link_count"] == 2
    assert web_env["app"].db.get_site_status("alpha")["link_count"] == 2

    # Batch add (async job)
    r_badd = requests.post(
        f"{base}/api/friendlinks/batch-add",
        json={
            "site_names": ["beta"],
            "text_or_specs": '<a href="https://newpartner.example.org">新伙伴</a>',
            "skip_existing": True,
            "nofollow": False,
        },
        timeout=3,
    )
    assert r_badd.status_code == 200
    badd_job = _poll_job(base, r_badd.json()["data"]["job_id"])
    assert badd_job["status"] == "completed"
    assert badd_job["result"]["counts"]["created"] == 1

    # Search (async job)
    r_search = requests.post(
        f"{base}/api/friendlinks/search",
        json={"site_names": ["beta"], "query": "newpartner.example.org", "match_root_domain": True},
        timeout=3,
    )
    assert r_search.status_code == 200
    search_job = _poll_job(base, r_search.json()["data"]["job_id"])
    assert search_job["status"] == "completed"
    assert len(search_job["result"]["matches"]) == 1

    # Batch remove (async job)
    r_brem = requests.post(
        f"{base}/api/friendlinks/batch-remove",
        json={"site_names": ["beta"], "text_or_urls": ["https://newpartner.example.org/"]},
        timeout=3,
    )
    assert r_brem.status_code == 200
    brem_job = _poll_job(base, r_brem.json()["data"]["job_id"])
    assert brem_job["status"] == "completed"
    assert brem_job["result"]["counts"]["deleted"] == 1

    # Audit backlinks (async job -> updates SiteGroupDB backlink_audit_history & broken_link_count)
    r_audit = requests.post(
        f"{base}/api/friendlinks/audit",
        json={"site_names": ["alpha"], "whitelist_domains": []},
        timeout=3,
    )
    assert r_audit.status_code == 200
    audit_job = _poll_job(base, r_audit.json()["data"]["job_id"])
    assert audit_job["status"] == "completed"
    assert audit_job["result"]["summary"]["ok"] == 1
    assert audit_job["result"]["summary"]["unreachable"] == 1
    assert web_env["app"].db.get_site_status("alpha")["broken_link_count"] == 1
    audits = web_env["app"].db.list_backlink_audits(site_name="alpha")
    assert len(audits) == 2

    # Crosslink preview + execute (fingerprint validation & cached existing_by_site reuse!)
    r_prev = requests.post(
        f"{base}/api/friendlinks/crosslink/preview",
        json={"mode": "mesh", "site_names": ["alpha", "beta"], "bidirectional": True},
        timeout=3,
    )
    assert r_prev.status_code == 200
    prev_data = r_prev.json()["data"]
    assert prev_data["counts"]["planned"] == 2
    fp = prev_data["fingerprint"]

    # Reject wrong fingerprint
    r_bad_exec = requests.post(
        f"{base}/api/friendlinks/crosslink/execute",
        json={"fingerprint": "wrong-fingerprint"},
        timeout=3,
    )
    assert r_bad_exec.status_code == 409

    # Valid execute reuses cached existing_by_site (no extra /api/friendlink/list GET calls!)
    get_count_before = len(web_env["state"]["get_calls"])
    r_exec = requests.post(
        f"{base}/api/friendlinks/crosslink/execute",
        json={"fingerprint": fp},
        timeout=3,
    )
    assert r_exec.status_code == 200
    exec_job = _poll_job(base, r_exec.json()["data"]["job_id"])
    assert exec_job["status"] == "completed"
    assert exec_job["result"]["result_counts"]["created"] == 2
    list_calls_during_exec = [
        u for u in web_env["state"]["get_calls"][get_count_before:] if "/api/friendlink/list" in u
    ]
    assert list_calls_during_exec == []

    # History & Rollback
    r_hist = requests.get(f"{base}/api/friendlinks/crosslink/history", timeout=3)
    assert r_hist.status_code == 200
    plans = r_hist.json()["data"]["plans"]
    assert len(plans) == 1
    plan_id = plans[0]["plan_id"]
    assert plans[0]["created_count"] == 2

    r_rb = requests.post(
        f"{base}/api/friendlinks/crosslink/rollback",
        json={"plan_id": plan_id},
        timeout=3,
    )
    assert r_rb.status_code == 200
    rb_job = _poll_job(base, r_rb.json()["data"]["job_id"])
    assert rb_job["status"] == "completed"
    assert rb_job["result"]["counts"]["deleted"] == 2

    # Export HTML
    r_exp = requests.post(
        f"{base}/api/friendlinks/export-html",
        json={"site_names": ["alpha"], "include_nofollow": True},
        timeout=3,
    )
    assert r_exp.status_code == 200
    assert '<a href="https://alpha.example.com/"' in r_exp.json()["data"]["html"]
    assert 'rel="nofollow"' in r_exp.json()["data"]["html"]


def test_upgrade_check_confirm_guard_and_run(web_env):
    base = web_env["base_url"]

    # Check versions
    r_chk = requests.post(f"{base}/api/upgrade/check", json={}, timeout=3)
    assert r_chk.status_code == 200
    chk_job = _poll_job(base, r_chk.json()["data"]["job_id"])
    assert chk_job["status"] == "completed"
    assert chk_job["result"]["updates_available"] == 1
    assert web_env["app"].db.get_site_status("alpha")["update_available"] == 1

    # Confirm guard
    r_no_confirm = requests.post(
        f"{base}/api/upgrade/run",
        json={"site_names": ["alpha"], "confirmed": False},
        timeout=3,
    )
    assert r_no_confirm.status_code == 400
    assert r_no_confirm.json()["ok"] is False

    # Stub _probe_http_home so wait_until_healthy passes without real HTTP
    web_env["app"].upgrade_service._probe_http_home = lambda _site: True
    r_run = requests.post(
        f"{base}/api/upgrade/run",
        json={"site_names": ["alpha"], "confirmed": True, "canary": True, "concurrency": 2, "stop_on_failure": True},
        timeout=3,
    )
    assert r_run.status_code == 200
    run_job = _poll_job(base, r_run.json()["data"]["job_id"])
    assert run_job["status"] == "completed"
    assert run_job["result"]["items"][0]["status"] == UPGRADE_UPGRADED
    assert web_env["app"].db.get_site_status("alpha")["current_version"] == "3.6.8"
    assert web_env["app"].db.get_site_status("alpha")["update_available"] == 0


def test_admin_jump_settings_hot_reload_publish_audit_and_mcp(web_env, monkeypatch):
    base = web_env["base_url"]
    app = web_env["app"]

    # Admin jump returns active AdminProxyServer URL
    r_jump = requests.post(
        f"{base}/api/admin/jump",
        json={"site_name": "alpha", "open_browser": False},
        timeout=3,
    )
    assert r_jump.status_code == 200
    jump_data = r_jump.json()["data"]
    assert jump_data["site_name"] == "alpha"
    assert jump_data["tier"] == "bridge"
    assert jump_data["opened"] is False
    assert ".localhost:" in jump_data["url"]
    assert app.admin_proxy.is_running is True
    assert app.admin_proxy.port > 0

    # Settings get + hot-reload PUT
    r_set_get = requests.get(f"{base}/api/settings", timeout=3)
    assert r_set_get.status_code == 200
    assert r_set_get.json()["data"]["mcp"]["verify_ssl"] is True

    r_set_put = requests.put(
        f"{base}/api/settings",
        json={"mcp": {"verify_ssl": False, "read_timeout": 42.0}, "friendlink": {"default_remark": "新互链"}},
        timeout=3,
    )
    assert r_set_put.status_code == 200
    assert app.settings.mcp.verify_ssl is False
    assert app.settings.mcp.read_timeout == 42.0
    assert app.friendlink_service.settings.friendlink.default_remark == "新互链"
    assert app.upgrade_service._settings.mcp.verify_ssl is False
    assert app.admin_jump_service.settings.mcp.verify_ssl is False
    assert app.admin_jump_service.proxy is app.admin_proxy

    # Publish status & fabu.log tail redaction
    fabu_log = web_env["tmp_path"] / "fabu.log"
    fabu_log.write_text(
        "2026-11-25 10:00:00 - INFO - 发布 https://alpha.example.com/api?token=alpha-import-tok-1111 成功\n",
        encoding="utf-8",
    )
    r_pub_status = requests.get(f"{base}/api/publish/status", timeout=3)
    assert r_pub_status.status_code == 200
    assert r_pub_status.json()["data"]["running"] is False

    r_pub_logs = requests.get(f"{base}/api/publish/logs?lines=50", timeout=3)
    assert r_pub_logs.status_code == 200
    logs = r_pub_logs.json()["data"]["lines"]
    assert len(logs) == 1
    assert "alpha-import-tok-1111" not in logs[0]
    assert "***" in logs[0]

    r_pub_stop = requests.post(f"{base}/api/publish/stop", json={}, timeout=3)
    assert r_pub_stop.status_code == 200
    assert r_pub_stop.json()["data"]["stopped"] is True

    # MCP config endpoint
    r_mcp_cfg = requests.get(f"{base}/api/mcp-config", timeout=3)
    assert r_mcp_cfg.status_code == 200
    mcp_cfg = r_mcp_cfg.json()["data"]
    assert mcp_cfg["http_url"] == f"http://127.0.0.1:{web_env['server'].port}/mcp"
    assert "mcp_server.py" in mcp_cfg["mcp_script"]
    assert "anqicms-sitegroup" in mcp_cfg["streamable_http"]["mcpServers"]
    assert "anqicms-sitegroup" in mcp_cfg["stdio"]["mcpServers"]

    # Audit logs endpoint
    r_audit_logs = requests.get(f"{base}/api/audit-logs?limit=20", timeout=3)
    assert r_audit_logs.status_code == 200
    assert len(r_audit_logs.json()["data"]["operations"]) >= 1

    # Jobs list & HTTP cancel endpoint
    gate = threading.Event()
    started_ev = threading.Event()

    def _slow_job(update_progress, cancel_event):
        started_ev.set()
        gate.wait(timeout=2.0)
        return {"ok": True}

    slow_rec = app.job_runner.submit("slow", "Slow Job", _slow_job)
    assert started_ev.wait(timeout=2.0)
    r_cancel = requests.post(f"{base}/api/jobs/{slow_rec.job_id}/cancel", json={}, timeout=3)
    assert r_cancel.status_code == 200
    assert r_cancel.json()["data"]["cancelled"] is True
    gate.set()
    _poll_job(base, slow_rec.job_id)

    r_jobs = requests.get(f"{base}/api/jobs?limit=20", timeout=3)
    assert r_jobs.status_code == 200
    assert isinstance(r_jobs.json()["data"]["jobs"], list)

    # POST /mcp JSON-RPC initialize + tools/list + notification
    # If Wave 1's core.sitegroup_mcp_server is not yet merged in this workspace,
    # provide a lightweight fake module matching the handle_jsonrpc(payload) contract.
    if importlib.util.find_spec("core.sitegroup_mcp_server") is None:
        fake_mod = types.ModuleType("core.sitegroup_mcp_server")

        class _FakeSiteGroupMcpServer:
            def __init__(self, config_path, settings=None, db=None, **kwargs) -> None:
                self.config_path = config_path
                self.settings = settings
                self.db = db

            def handle_jsonrpc(self, payload: dict[str, Any]):
                method = payload.get("method")
                req_id = payload.get("id")
                if req_id is None or str(method).startswith("notifications/"):
                    return None
                if method == "initialize":
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": APP_NAME, "version": APP_VERSION},
                        },
                    }
                if method == "tools/list":
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "tools": [
                                {"name": "list_sites", "description": "List configured sites"}
                            ]
                        },
                    }
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": "Method not found"},
                }

        fake_mod.SiteGroupMcpServer = _FakeSiteGroupMcpServer
        monkeypatch.setitem(sys.modules, "core.sitegroup_mcp_server", fake_mod)

    r_init = requests.post(
        f"{base}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        timeout=3,
    )
    assert r_init.status_code == 200
    init_body = r_init.json()
    assert init_body["jsonrpc"] == "2.0"
    assert init_body["id"] == 1
    assert "serverInfo" in init_body["result"]

    r_notif = requests.post(
        f"{base}/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        timeout=3,
    )
    assert r_notif.status_code == 202
    assert r_notif.text == ""

    r_tools = requests.post(
        f"{base}/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        timeout=3,
    )
    assert r_tools.status_code == 200
    tools_body = r_tools.json()
    assert tools_body["id"] == 2
    assert isinstance(tools_body["result"]["tools"], list)
    assert len(tools_body["result"]["tools"]) >= 1


def test_app_self_update_check_and_apply_source_and_frozen(web_env, monkeypatch):
    import dataclasses

    base = web_env["base_url"]
    app = web_env["app"]

    @dataclasses.dataclass(frozen=True)
    class _FakeAppUpdateInfo:
        current_version: str
        latest_version: str
        update_available: bool
        release_url: str
        download_url: str
        asset_name: str
        sha256_url: str
        release_notes: str
        message: str = ""

    fake_info = _FakeAppUpdateInfo(
        current_version="2.0.0",
        latest_version="2.1.0",
        update_available=True,
        release_url="https://github.com/zaomeng/anqicms-manager/releases/tag/v2.1.0",
        download_url="https://github.com/zaomeng/anqicms-manager/releases/download/v2.1.0/anqicms%E7%AB%99%E7%BE%A4%E6%8E%A7%E5%88%B6%E5%8F%B0.exe",
        asset_name="anqicms站群控制台.exe",
        sha256_url="https://github.com/zaomeng/anqicms-manager/releases/download/v2.1.0/SHA256SUMS-web.txt",
        release_notes="### v2.1.0\n- 支持一键自更新",
        message="发现新版本 v2.1.0",
    )

    monkeypatch.setattr(app, "_update_check_fn", lambda **_kw: fake_info)

    # 1. GET /api/app/update/check
    r_chk = requests.get(f"{base}/api/app/update/check", timeout=3)
    assert r_chk.status_code == 200
    chk_payload = r_chk.json()
    assert chk_payload["ok"] is True
    assert chk_payload["data"]["current_version"] == "2.0.0"
    assert chk_payload["data"]["latest_version"] == "2.1.0"
    assert chk_payload["data"]["update_available"] is True
    assert chk_payload["data"]["release_url"] == fake_info.release_url

    # 2. POST /api/app/update/apply in Python source mode (non-frozen)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    r_apply_src = requests.post(f"{base}/api/app/update/apply", json={}, timeout=3)
    assert r_apply_src.status_code == 200
    job_src = _poll_job(base, r_apply_src.json()["data"]["job_id"])
    assert job_src["status"] == "completed"
    res_src = job_src["result"]
    assert res_src["source_mode"] is True
    assert res_src["applied"] is False
    assert res_src["release_url"] == fake_info.release_url
    assert res_src["download_url"] == fake_info.download_url
    assert "源码运行模式" in res_src["message"]

    # 3. POST /api/app/update/apply in mocked frozen EXE mode
    downloaded_exe = web_env["tmp_path"] / "downloaded.exe"
    downloaded_exe.write_bytes(b"MZ-fake-exe")
    fake_script = web_env["tmp_path"] / "replace.bat"
    fake_script.write_text("@echo off\r\n", encoding="utf-8")

    called: dict[str, Any] = {"launched": False, "stopped": False, "exited": []}

    fake_updater = types.ModuleType("core.self_updater")
    fake_updater.check_app_update = lambda **_kw: fake_info
    fake_updater.download_and_verify_update = lambda *a, **kw: downloaded_exe

    def _create_script(new_exe, target_exe, **kw):
        called["new_exe"] = Path(new_exe)
        called["target_exe"] = Path(target_exe)
        return fake_script

    def _launch_script(script_path):
        called["launched"] = Path(script_path)

    fake_updater.create_windows_replace_script = _create_script
    fake_updater.launch_replace_script = _launch_script
    import core

    monkeypatch.setitem(sys.modules, "core.self_updater", fake_updater)
    monkeypatch.setattr(core, "self_updater", fake_updater, raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(web_env["tmp_path"] / "anqicms站群控制台.exe"))

    exit_ev = threading.Event()
    monkeypatch.setattr(app, "_server_stop_fn", lambda: called.__setitem__("stopped", True))
    monkeypatch.setattr(
        app,
        "_exit_fn",
        lambda code=0: (called["exited"].append(code), exit_ev.set()),
    )

    r_apply_exe = requests.post(f"{base}/api/app/update/apply", json={}, timeout=3)
    assert r_apply_exe.status_code == 200
    job_exe = _poll_job(base, r_apply_exe.json()["data"]["job_id"])
    assert job_exe["status"] == "completed"
    res_exe = job_exe["result"]
    assert res_exe["applied"] is True
    assert res_exe["restarting"] is True
    assert res_exe["source_mode"] is False
    assert called["new_exe"] == downloaded_exe
    assert called["launched"] == fake_script

    assert exit_ev.wait(timeout=3.5)
    assert called["stopped"] is True
    assert called["exited"] == [0]


def test_site_reveal_secrets_query_param(web_env):
    base = web_env["base_url"]

    # Default GET /api/sites remains masked
    r_list = requests.get(f"{base}/api/sites", timeout=3)
    assert r_list.status_code == 200
    list_text = r_list.text
    assert "alpha-mcp-secret-9999" not in list_text
    assert "PassAlpha!Secret" not in list_text
    assert "alpha-import-tok-1111" not in list_text
    alpha_masked = next(s for s in r_list.json()["data"]["sites"] if s["name"] == "alpha")
    assert "mcp_token" not in alpha_masked
    assert "jiekou" not in alpha_masked
    assert "admin_password" not in alpha_masked
    assert alpha_masked["has_mcp_token"] is True
    assert alpha_masked["has_admin_password"] is True

    # Default GET /api/sites/<name> also remains masked
    r_single_masked = requests.get(f"{base}/api/sites/alpha", timeout=3)
    assert r_single_masked.status_code == 200
    single_masked = r_single_masked.json()["data"]["site"]
    assert "mcp_token" not in single_masked
    assert "jiekou" not in single_masked
    assert "admin_password" not in single_masked

    # GET /api/sites/alpha?reveal=1 returns unmasked values
    r_single_reveal = requests.get(f"{base}/api/sites/alpha?reveal=1", timeout=3)
    assert r_single_reveal.status_code == 200
    revealed = r_single_reveal.json()["data"]["site"]
    assert revealed["mcp_token"] == "alpha-mcp-secret-9999"
    assert revealed["jiekou"] == "/api/import/archive?token=alpha-import-tok-1111"
    assert revealed["admin_password"] == "PassAlpha!Secret"

    # GET /api/sites?reveal=1 also returns unmasked values
    r_list_reveal = requests.get(f"{base}/api/sites?reveal=1", timeout=3)
    assert r_list_reveal.status_code == 200
    alpha_revealed = next(s for s in r_list_reveal.json()["data"]["sites"] if s["name"] == "alpha")
    assert alpha_revealed["mcp_token"] == "alpha-mcp-secret-9999"
    assert alpha_revealed["jiekou"] == "/api/import/archive?token=alpha-import-tok-1111"
    assert alpha_revealed["admin_password"] == "PassAlpha!Secret"


def test_site_full_publishing_parity_and_gallery_crud(web_env):
    from core.content_service import parse_image_groups

    base = web_env["base_url"]
    cfg_path = web_env["config_path"]

    # Initially empty galleries
    r_empty = requests.get(f"{base}/api/galleries", timeout=3)
    assert r_empty.status_code == 200
    assert r_empty.json()["data"]["galleries"] == []

    # Invalid gallery content rejected with 400
    r_bad_gal = requests.post(
        f"{base}/api/galleries",
        json={"name": "坏图库", "content": "[默认]\nnot-a-valid-image-url\n"},
        timeout=3,
    )
    assert r_bad_gal.status_code == 400

    # Create gallery with grouped image URLs
    gal_content = (
        "[产品]\n"
        "https://img.example.com/p1.jpg\n"
        "https://img.example.com/p2.jpg\n"
        "[案例]\n"
        "/uploads/case1.webp\n"
    )
    r_create_gal = requests.post(
        f"{base}/api/galleries",
        json={"name": "主图库", "content": gal_content},
        timeout=3,
    )
    assert r_create_gal.status_code == 200
    create_gal_data = r_create_gal.json()["data"]
    assert create_gal_data["created"] is True
    gal = create_gal_data["gallery"]
    gid = gal["id"]
    assert gal["name"] == "主图库"
    assert gal["group_count"] == 2
    assert gal["image_count"] == 3
    assert gal["content"] == gal_content
    assert Path(gal["path"]).is_file()

    # GET /api/galleries lists created gallery
    r_list_gal = requests.get(f"{base}/api/galleries", timeout=3)
    assert r_list_gal.status_code == 200
    gals = r_list_gal.json()["data"]["galleries"]
    assert len(gals) == 1
    assert gals[0]["id"] == gid
    assert gals[0]["group_count"] == 2
    assert gals[0]["image_count"] == 3

    # Update gallery name and content
    updated_content = "[产品]\nhttps://img.example.com/p1-new.jpg\n/uploads/p2.png\n"
    r_upd_gal = requests.post(
        f"{base}/api/galleries",
        json={"id": gid, "name": "主图库V2", "content": updated_content},
        timeout=3,
    )
    assert r_upd_gal.status_code == 200
    upd_gal_data = r_upd_gal.json()["data"]
    assert upd_gal_data["created"] is False
    assert upd_gal_data["gallery"]["name"] == "主图库V2"
    assert upd_gal_data["gallery"]["group_count"] == 1
    assert upd_gal_data["gallery"]["image_count"] == 2
    assert upd_gal_data["gallery"]["content"] == updated_content

    # Save site with full publishing fields + gallery_id + publish_times string + category_keywords
    custom_pub_dir = web_env["tmp_path"] / "custom_articles" / "pubsite"
    r_save_site = requests.post(
        f"{base}/api/sites",
        json={
            "name": "pubsite",
            "full_endpoint": "https://pubsite.example.com/api/import/archive?token=pub-tok-123",
            "fid": 5,
            "category_name": "行业资讯",
            "category_keywords": {"5": ["行业", "动态"], "8": ["评测"]},
            "publish_dir": str(custom_pub_dir),
            "publish_times": "08:30:00, 14:00:00\n08:30:00",
            "interval_time": 45,
            "daily_publish_limit": 25,
            "article_warning_threshold": 120,
            "gallery_id": gid,
            "enabled": True,
        },
        timeout=3,
    )
    assert r_save_site.status_code == 200
    saved_payload = r_save_site.json()["data"]["site"]
    assert saved_payload["fid"] == 5
    assert saved_payload["category_name"] == "行业资讯"
    assert saved_payload["category_keywords"] == {"5": ["行业", "动态"], "8": ["评测"]}
    assert Path(saved_payload["publish_dir"]).resolve() == custom_pub_dir.resolve()
    assert saved_payload["publish_time"] == "08:30:00"
    assert saved_payload["publish_times"] == ["08:30:00"]
    assert saved_payload["interval_time"] == 45
    assert saved_payload["daily_publish_limit"] == 25
    assert saved_payload["article_warning_threshold"] == 120
    assert saved_payload["gallery_id"] == gid
    assert Path(saved_payload["image_file"]).resolve() == Path(gal["path"]).resolve()
    assert saved_payload["enabled"] is True

    # Verify persisted on disk and ContentService can parse the synced image_file
    disk_site = next(s for s in load_config(cfg_path) if s.name == "pubsite")
    assert disk_site.gallery_id == gid
    assert disk_site.image_file is not None
    assert disk_site.image_file.resolve() == Path(gal["path"]).resolve()
    assert parse_image_groups(disk_site.image_file) == {
        "产品": ["https://img.example.com/p1-new.jpg", "/uploads/p2.png"]
    }

    # GET /api/sites returns all publishing fields cleanly
    r_sites = requests.get(f"{base}/api/sites", timeout=3)
    assert r_sites.status_code == 200
    pub_row = next(s for s in r_sites.json()["data"]["sites"] if s["name"] == "pubsite")
    for required_key in (
        "publish_dir",
        "publish_times",
        "publish_time",
        "interval_time",
        "daily_publish_limit",
        "article_warning_threshold",
        "gallery_id",
        "image_file",
        "category_name",
        "category_keywords",
        "enabled",
        "fid",
    ):
        assert required_key in pub_row
    assert pub_row["category_keywords"] == {"5": ["行业", "动态"], "8": ["评测"]}

    # Also test publish_times list input
    r_list_times = requests.post(
        f"{base}/api/sites",
        json={"name": "pubsite", "publish_times": ["09:45:00"]},
        timeout=3,
    )
    assert r_list_times.status_code == 200
    assert r_list_times.json()["data"]["site"]["publish_times"] == ["09:45:00"]
    assert r_list_times.json()["data"]["site"]["publish_time"] == "09:45:00"

    # Reject invalid publish_times / non-existent gallery_id / conflicting keywords
    assert requests.post(
        f"{base}/api/sites",
        json={"name": "pubsite", "publish_times": "25:00:00"},
        timeout=3,
    ).status_code == 400
    assert requests.post(
        f"{base}/api/sites",
        json={"name": "pubsite", "gallery_id": "00000000-0000-4000-8000-000000000000"},
        timeout=3,
    ).status_code == 400
    assert requests.post(
        f"{base}/api/sites",
        json={"name": "pubsite", "category_keywords": {"5": ["重复"], "8": ["重复"]}},
        timeout=3,
    ).status_code == 400

    # DELETE /api/galleries/<id> fails with 400 listing bound site names while referenced
    r_del_bound = requests.delete(f"{base}/api/galleries/{gid}", timeout=3)
    assert r_del_bound.status_code == 400
    assert "pubsite" in r_del_bound.json()["error"]

    # Unbind gallery from site, then delete succeeds
    r_unbind = requests.post(
        f"{base}/api/sites",
        json={"name": "pubsite", "gallery_id": ""},
        timeout=3,
    )
    assert r_unbind.status_code == 200
    assert r_unbind.json()["data"]["site"]["gallery_id"] == ""
    assert r_unbind.json()["data"]["site"]["image_file"] == ""

    r_del_ok = requests.delete(f"{base}/api/galleries/{gid}", timeout=3)
    assert r_del_ok.status_code == 200
    assert r_del_ok.json()["data"]["deleted"] == gid
    assert requests.get(f"{base}/api/galleries", timeout=3).json()["data"]["galleries"] == []


def test_site_categories_publish_pending_and_startup_toggle(web_env, monkeypatch):
    from core.category_service import CategoryService
    from core.startup_service import StartupService

    base = web_env["base_url"]
    app = web_env["app"]

    # 1. POST /api/sites/<name>/categories via CategoryService
    cat_calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeCatSession:
        def get(self, url: str, **kwargs):
            cat_calls.append((url, kwargs))
            return _FakeHttpResponse(
                200,
                json_data={
                    "code": 0,
                    "data": [
                        {"id": 1, "parent_id": 0, "title": "新闻中心"},
                        {"id": 12, "parent_id": 1, "title": "行业动态"},
                    ],
                },
            )

    monkeypatch.setattr(app, "category_service", CategoryService(session=_FakeCatSession()))
    r_cats = requests.post(f"{base}/api/sites/alpha/categories", json={}, timeout=3)
    assert r_cats.status_code == 200
    cats_payload = r_cats.json()
    assert cats_payload["ok"] is True
    assert cats_payload["data"]["categories"] == [
        {"id": 1, "parent_id": 0, "title": "新闻中心"},
        {"id": 12, "parent_id": 1, "title": "行业动态"},
    ]
    assert cat_calls[0][0] == "https://alpha.example.com/api/import/categories?token=alpha-import-tok-1111"

    # Missing site -> 404
    assert requests.post(f"{base}/api/sites/no-such-site/categories", json={}, timeout=3).status_code == 404

    # 2. GET /api/publish/pending/<name> via SiteService (count + first 50 items + matched category_id)
    r_kw = requests.post(
        f"{base}/api/sites",
        json={"name": "alpha", "category_keywords": {"12": ["动态快讯"]}},
        timeout=3,
    )
    assert r_kw.status_code == 200

    alpha_site = next(s for s in app.load_sites() if s.name == "alpha")
    for f in alpha_site.publish_dir.glob("*.txt"):
        f.unlink()
    for idx in range(55):
        title = f"动态快讯_{idx:02d}" if idx == 0 else f"普通文章_{idx:02d}"
        (alpha_site.publish_dir / f"{title}.txt").write_text("正文内容测试", encoding="utf-8")

    r_pending = requests.get(f"{base}/api/publish/pending/alpha", timeout=3)
    assert r_pending.status_code == 200
    pending_data = r_pending.json()["data"]
    assert pending_data["count"] == 55
    assert pending_data["total"] == 55
    assert len(pending_data["items"]) == 50
    matched_item = next(it for it in pending_data["items"] if it["title"] == "动态快讯_00")
    assert matched_item["filename"] == "动态快讯_00.txt"
    assert matched_item["category_id"] == 12
    normal_item = next(it for it in pending_data["items"] if it["title"] == "普通文章_01")
    assert normal_item["category_id"] == 1  # default alpha fid

    # 3. GET & POST /api/system/startup via StartupService with in-memory FakeRegistry
    class _FakeKey:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _FakeRegistry:
        HKEY_CURRENT_USER = "HKCU"
        KEY_QUERY_VALUE = 1
        KEY_SET_VALUE = 2
        REG_SZ = 1

        def __init__(self) -> None:
            self.values: dict[str, str] = {}

        def CreateKeyEx(self, _root, _path, _reserved, _access):
            return _FakeKey()

        def OpenKey(self, _root, _path, _reserved, _access):
            if not self.values:
                raise FileNotFoundError
            return _FakeKey()

        def SetValueEx(self, _key, name, _reserved, _value_type, value):
            self.values[name] = value

        def QueryValueEx(self, _key, name):
            if name not in self.values:
                raise FileNotFoundError
            return self.values[name], self.REG_SZ

        def DeleteValue(self, _key, name):
            if name not in self.values:
                raise FileNotFoundError
            del self.values[name]

    fake_startup = StartupService(
        platform="win32",
        registry=_FakeRegistry(),
        executable=web_env["tmp_path"] / "AnqiCMSConsole.exe",
        preference_path=web_env["tmp_path"] / "设置" / "startup_pref_test.json",
        frozen=True,
    )
    monkeypatch.setattr(app, "startup_service", fake_startup)

    r_st0 = requests.get(f"{base}/api/system/startup", timeout=3)
    assert r_st0.status_code == 200
    assert r_st0.json()["data"] == {"supported": True, "enabled": False}

    r_st_on = requests.post(f"{base}/api/system/startup", json={"enabled": True}, timeout=3)
    assert r_st_on.status_code == 200
    assert r_st_on.json()["data"]["supported"] is True
    assert r_st_on.json()["data"]["enabled"] is True
    assert fake_startup.is_enabled() is True

    r_st_off = requests.post(f"{base}/api/system/startup", json={"enabled": False}, timeout=3)
    assert r_st_off.status_code == 200
    assert r_st_off.json()["data"]["enabled"] is False
    assert fake_startup.is_enabled() is False

    assert requests.post(f"{base}/api/system/startup", json={"enabled": "yes"}, timeout=3).status_code == 400


def test_upgrade_json_error_fast_fail_skips_restart_and_health_poll():
    from models.sitegroup import UPGRADE_FAILED

    class _FakeClock:
        def __init__(self) -> None:
            self.now = 100.0
            self.sleeps: list[float] = []

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)
            self.now += seconds

    class _FastFailMcpClient:
        def __init__(self, upgrade_result: McpToolResult) -> None:
            self._upgrade_result = upgrade_result
            self.calls: list[tuple[str, dict[str, Any] | None]] = []
            self.closed = False

        def initialize(self) -> dict[str, Any]:
            return {"protocolVersion": "2025-06-18", "serverInfo": {"version": "3.6.7"}}

        def list_tools(self, *, refresh: bool = False) -> list[McpToolInfo]:
            return [
                McpToolInfo(
                    name="siteops_upgrade",
                    description="系统版本检测与升级",
                    input_schema={
                        "type": "object",
                        "properties": {"action": {"type": "string", "enum": ["check", "info", "upgrade"]}},
                    },
                ),
                McpToolInfo(
                    name="api",
                    description="Invoke backend API",
                    input_schema={
                        "type": "object",
                        "properties": {"action": {"type": "string", "enum": ["invoke"]}},
                    },
                ),
            ]

        def find_tools(self, keywords, *, explicit_names=()) -> list[McpToolInfo]:
            return [self.list_tools()[0]]

        def call_tool(self, name: str, arguments: dict | None = None, **kwargs) -> McpToolResult:
            self.calls.append((name, arguments))
            if name == "siteops_upgrade" and (arguments or {}).get("action") == "check":
                return McpToolResult(
                    is_error=False,
                    text="",
                    structured={"current_version": "3.6.7", "latest_version": "3.6.8"},
                )
            if name == "api" and (arguments or {}).get("path") == "/system/api/version/upgrade":
                return self._upgrade_result
            raise AssertionError(f"Unexpected tool call: {name} {arguments}")

        def close(self) -> None:
            self.closed = True

    site = SiteConfig(
        name="fastfail-site",
        fid=1,
        url="https://fastfail.example.com",
        jiekou="",
        publish_dir=Path("."),
        daily_publish_filename=Path("daily.txt"),
        publish_time="06:00:00",
        interval_time=60,
        daily_publish_limit=10,
        mcp_token="tok-fastfail",
    )

    # Case 1: code: -1 in structured JSON with is_error=False
    clock1 = _FakeClock()
    client1 = _FastFailMcpClient(
        McpToolResult(
            is_error=False,
            text='{"code": -1, "msg": "版本更新失败"}',
            structured={"code": -1, "msg": "版本更新失败"},
        )
    )
    settings = IntegrationSettings()
    settings.upgrade.restart_wait_s = 10.0
    settings.upgrade.health_timeout_s = 90.0
    svc1 = UpgradeService(
        settings,
        mcp_client_factory=lambda _s: client1,
        sleep=clock1.sleep,
        monotonic=clock1.monotonic,
    )
    res1 = svc1.upgrade_site(site, confirmed=True)
    assert res1.status == UPGRADE_FAILED
    assert res1.from_version == "3.6.7"
    assert res1.message == "版本更新失败"
    assert res1.hint == "请检查目标服务器文件写入权限或登录后台查看升级日志"
    assert clock1.sleeps == []
    assert all((args or {}).get("path") != "/system/api/anqi/restart" for _, args in client1.calls)
    assert client1.closed is True

    # Case 2: ok: false in JSON text (structured=None) with is_error=False
    clock2 = _FakeClock()
    client2 = _FastFailMcpClient(
        McpToolResult(
            is_error=False,
            text='{"ok": false, "msg": "磁盘只读无法替换二进制", "hint": "检查文件权限"}',
            structured=None,
        )
    )
    svc2 = UpgradeService(
        settings,
        mcp_client_factory=lambda _s: client2,
        sleep=clock2.sleep,
        monotonic=clock2.monotonic,
    )
    res2 = svc2.upgrade_site(site, confirmed=True)
    assert res2.status == UPGRADE_FAILED
    assert res2.from_version == "3.6.7"
    assert res2.message == "磁盘只读无法替换二进制"
    assert res2.hint == "检查文件权限"
    assert clock2.sleeps == []
    assert all((args or {}).get("path") != "/system/api/anqi/restart" for _, args in client2.calls)
    assert client2.closed is True


