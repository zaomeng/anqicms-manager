"""Unit and integration tests for ``SiteGroupMcpServer`` and ``mcp_server.py``."""

from __future__ import annotations

import ast
import importlib
import io
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.config_service import load_config, save_config
from core.friendlink_ledger import FriendLinkLedger, ledger_path
from core.integration_settings import IntegrationSettings
from core.site_factory import create_site_from_key, ensure_site_files
from core.sitegroup_db import SiteGroupDB, db_path
from core.sitegroup_mcp_server import DEFAULT_PROTOCOL_VERSION, SiteGroupMcpServer
from models.entities import SiteConfig
from models.sitegroup import (
    JUMP_TIER_TICKET,
    LINK_CREATED,
    LINK_DELETED,
    LINK_PLANNED,
    LINK_SKIPPED_EXISTS,
    UPGRADE_ALREADY_LATEST,
    UPGRADE_UPGRADED,
    CrossLinkItem,
    CrossLinkPlan,
    CrossLinkReport,
    FriendLinkSpec,
    JumpPlan,
    LinkOpResult,
    McpToolInfo,
    SiteVersionInfo,
    UpgradeResult,
)

EXPECTED_TOOLS = {
    "list_sites",
    "search_links",
    "batch_add_links",
    "batch_remove_links",
    "audit_backlinks",
    "crosslink_plan",
    "rollback_crosslink",
    "export_html_links",
    "check_versions",
    "upgrade_sites",
    "get_admin_jump_url",
    "import_sites",
}


def _make_site(
    tmp_path: Path,
    name: str,
    url: str,
    *,
    group: str = "",
    title: str = "",
    token: str = "imp-tok",
    mcp_token: str = "",
    admin_user: str = "",
    admin_password: str = "",
    enabled: bool = True,
    fid: int = 1,
) -> SiteConfig:
    site = create_site_from_key(
        name,
        url=url,
        jiekou=f"/api/import/archive?token={token}" if token else "",
        fid=fid,
        base_dir=tmp_path,
    )
    site.site_group = group
    site.site_title = title
    site.mcp_token = mcp_token
    site.admin_username = admin_user
    site.admin_password = admin_password
    site.enabled = enabled
    ensure_site_files(site)
    return site


def _write_sample_config(tmp_path: Path) -> tuple[Path, list[SiteConfig]]:
    sites = [
        _make_site(
            tmp_path,
            "alpha",
            "https://alpha.example.com",
            group="资讯组",
            title="阿尔法资讯|alpha新闻",
            token="alpha-imp-secret",
            mcp_token="alpha-mcp-secret",
            admin_user="admin",
            admin_password="alpha-pass-secret",
            enabled=True,
        ),
        _make_site(
            tmp_path,
            "beta",
            "https://beta.example.com",
            group="电商组",
            title="贝塔商城",
            token="beta-imp-secret",
            enabled=True,
        ),
        _make_site(
            tmp_path,
            "gamma",
            "https://gamma.example.com",
            group="资讯组",
            title="伽马站",
            token="gamma-imp-secret",
            enabled=False,
        ),
    ]
    cfg_path = tmp_path / "config.ini"
    save_config(cfg_path, sites)
    return cfg_path, sites


def _call_rpc_tool(server: SiteGroupMcpServer, name: str, arguments: dict | None = None) -> dict:
    resp = server.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 100,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
    )
    assert resp is not None
    assert "result" in resp, f"unexpected error response: {resp}"
    return resp["result"]


# ---------------------------------------------------------------------------
# Protocol handshake & JSON-RPC 2.0 tests
# ---------------------------------------------------------------------------


def test_jsonrpc_handshake_ping_list_unknown_and_notifications(tmp_path):
    cfg_path, _ = _write_sample_config(tmp_path)
    with SiteGroupMcpServer(cfg_path) as server:
        # 2025-06-18
        init_new = server.handle_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
            }
        )
        assert init_new["id"] == 1
        assert init_new["result"]["protocolVersion"] == "2025-06-18"
        assert "tools" in init_new["result"]["capabilities"]
        assert init_new["result"]["serverInfo"]["name"]

        # 2024-11-05
        init_old = server.handle_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": "req-2",
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
            }
        )
        assert init_old["id"] == "req-2"
        assert init_old["result"]["protocolVersion"] == "2024-11-05"

        # Unsupported protocol falls back to DEFAULT_PROTOCOL_VERSION
        init_fallback = server.handle_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "initialize",
                "params": {"protocolVersion": "1999-01-01"},
            }
        )
        assert init_fallback["result"]["protocolVersion"] == DEFAULT_PROTOCOL_VERSION

        # Notification returns None
        assert (
            server.handle_jsonrpc(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}
            )
            is None
        )
        assert (
            server.handle_jsonrpc(
                {"jsonrpc": "2.0", "method": "notifications/custom_event", "params": {"x": 1}}
            )
            is None
        )

        # Ping
        ping_resp = server.handle_jsonrpc({"jsonrpc": "2.0", "id": 4, "method": "ping"})
        assert ping_resp == {"jsonrpc": "2.0", "id": 4, "result": {}}

        # tools/list
        tools_resp = server.handle_jsonrpc({"jsonrpc": "2.0", "id": 5, "method": "tools/list"})
        tools = tools_resp["result"]["tools"]
        names = {t["name"] for t in tools}
        assert names == EXPECTED_TOOLS
        for tool in tools:
            assert tool["description"]
            assert tool["inputSchema"]["type"] == "object"

        # Unknown tool -> JSON-RPC -32602
        unknown_tool_resp = server.handle_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "does_not_exist", "arguments": {}},
            }
        )
        assert unknown_tool_resp["error"]["code"] == -32602

        # Unknown method -> JSON-RPC -32601
        unknown_method_resp = server.handle_jsonrpc(
            {"jsonrpc": "2.0", "id": 7, "method": "resources/list"}
        )
        assert unknown_method_resp["error"]["code"] == -32601

        # Invalid arguments type -> JSON-RPC -32602
        bad_args_resp = server.handle_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {"name": "list_sites", "arguments": "not-a-dict"},
            }
        )
        assert bad_args_resp["error"]["code"] == -32602


# ---------------------------------------------------------------------------
# Tool tests
# ---------------------------------------------------------------------------


def test_list_sites_filters_merges_db_and_redacts_secrets(tmp_path):
    cfg_path, _ = _write_sample_config(tmp_path)
    db = SiteGroupDB(db_path(cfg_path))
    db.upsert_site_status(
        "alpha",
        site_url="https://alpha.example.com",
        current_version="3.6.5",
        latest_version="3.6.8",
        update_available=True,
        mcp_ok=True,
        mcp_tools=["check_update", "friendlink_list"],
        link_count=12,
        broken_link_count=1,
    )

    try:
        with SiteGroupMcpServer(cfg_path, db=db) as server:
            res_all = _call_rpc_tool(server, "list_sites", {})
            assert res_all["isError"] is False
            sc_all = res_all["structuredContent"]
            assert sc_all["total"] == 3
            assert sc_all["groups"] == ["电商组", "资讯组"]

            alpha = next(s for s in sc_all["sites"] if s["name"] == "alpha")
            assert alpha["current_version"] == "3.6.5"
            assert alpha["latest_version"] == "3.6.8"
            assert alpha["update_available"] is True
            assert alpha["mcp_ok"] is True
            assert alpha["mcp_tools"] == ["check_update", "friendlink_list"]
            assert alpha["link_count"] == 12
            assert alpha["broken_link_count"] == 1
            assert alpha["has_mcp_token"] is True
            assert alpha["has_admin_credentials"] is True

            serialized = json.dumps(res_all, ensure_ascii=False)
            assert "alpha-mcp-secret" not in serialized
            assert "alpha-pass-secret" not in serialized
            assert "alpha-imp-secret" not in serialized

            # Filter by group + enabled_only
            res_filtered = _call_rpc_tool(
                server,
                "list_sites",
                {"group": "资讯组", "enabled_only": True},
            )
            assert [s["name"] for s in res_filtered["structuredContent"]["sites"]] == ["alpha"]

            # Filter by search
            res_search = _call_rpc_tool(server, "list_sites", {"search": "贝塔"})
            assert [s["name"] for s in res_search["structuredContent"]["sites"]] == ["beta"]
    finally:
        db.close()


def test_search_links_and_export_html_links(tmp_path):
    cfg_path, _ = _write_sample_config(tmp_path)
    recorded_calls = []

    class FakeFriendLinkService:
        def search_links_across_sites(self, sites, query="", *, match_root_domain=True):
            recorded_calls.append(([s.name for s in sites], query, match_root_domain))
            return {
                "matches": [
                    {
                        "site_name": "alpha",
                        "site_id": "1",
                        "site_url": "https://alpha.example.com",
                        "id": 9,
                        "title": "伙伴站",
                        "link": "https://www.partner.example.net/",
                        "root_domain": "partner.example.net",
                        "nofollow": False,
                        "back_link": "",
                        "remark": "交换",
                        "status": 1,
                        "status_text": "正常",
                    }
                ],
                "site_counts": {"alpha": 5},
                "site_errors": {},
                "total_links": 5,
            }

    db = SiteGroupDB(db_path(cfg_path))
    try:
        with SiteGroupMcpServer(cfg_path, db=db, friendlink_service=FakeFriendLinkService()) as server:
            res = _call_rpc_tool(
                server,
                "search_links",
                {"query": "partner.example.net", "group": "资讯组", "match_root_domain": True},
            )
            assert res["isError"] is False
            assert recorded_calls == [(["alpha", "gamma"], "partner.example.net", True)]
            assert res["structuredContent"]["matched_count"] == 1
            assert db.get_site_status("alpha")["link_count"] == 5

            html_res = _call_rpc_tool(
                server,
                "export_html_links",
                {"group": "资讯组", "include_nofollow": True},
            )
            assert html_res["isError"] is False
            html_out = html_res["structuredContent"]["html"]
            assert '<a href="https://alpha.example.com/" target="_blank" rel="nofollow">阿尔法资讯</a>' in html_out
            assert '<a href="https://gamma.example.com/" target="_blank" rel="nofollow">伽马站</a>' in html_out
            assert "beta.example.com" not in html_out
    finally:
        db.close()


def test_batch_add_links_and_remove_links_with_html_anchors(tmp_path):
    cfg_path, _ = _write_sample_config(tmp_path)
    add_calls = []
    remove_calls = []

    class FakeFriendLinkService:
        def batch_add(self, sites, specs, *, skip_existing=True):
            add_calls.append(([s.name for s in sites], list(specs), skip_existing))
            return [
                LinkOpResult(
                    site_name=s.name,
                    site_id=s.site_id,
                    action="add",
                    link=spec.link,
                    title=spec.title,
                    status=LINK_CREATED,
                    message="已添加",
                )
                for s in sites
                for spec in specs
            ]

        def batch_remove(self, sites, urls):
            remove_calls.append(([s.name for s in sites], list(urls)))
            return [
                LinkOpResult(
                    site_name=s.name,
                    site_id=s.site_id,
                    action="remove",
                    link=url,
                    title="",
                    status=LINK_DELETED,
                    message="已删除",
                )
                for s in sites
                for url in urls
            ]

    with SiteGroupMcpServer(cfg_path, friendlink_service=FakeFriendLinkService()) as server:
        links_input = (
            '<a href="https://friend1.example.com" rel="nofollow">友站一</a>\n'
            "友站二|https://friend2.example.com\n"
        )
        add_res = _call_rpc_tool(
            server,
            "batch_add_links",
            {
                "links_text": links_input,
                "site_names": ["alpha", "beta"],
                "skip_existing": True,
                "nofollow": False,
            },
        )
        assert add_res["isError"] is False
        assert add_res["structuredContent"]["parsed_count"] == 2
        assert add_res["structuredContent"]["summary"][LINK_CREATED] == 4
        assert len(add_calls) == 1
        site_list, specs, skip_existing = add_calls[0]
        assert site_list == ["alpha", "beta"]
        assert skip_existing is True
        assert [(s.title, s.link, s.nofollow) for s in specs] == [
            ("友站一", "https://friend1.example.com/", True),
            ("友站二", "https://friend2.example.com/", False),
        ]

        rem_res = _call_rpc_tool(
            server,
            "batch_remove_links",
            {
                "links_text": '<a href="https://friend1.example.com">友站一</a>\nhttps://friend2.example.com',
                "group": "电商组",
            },
        )
        assert rem_res["isError"] is False
        assert rem_res["structuredContent"]["summary"][LINK_DELETED] == 2
        assert remove_calls == [
            (["beta"], ["https://friend1.example.com/", "https://friend2.example.com/"])
        ]


def test_audit_backlinks_records_db_and_respects_confirmation_guard(tmp_path):
    cfg_path, _ = _write_sample_config(tmp_path)
    remove_calls = []

    class FakeFriendLinkService:
        def batch_check_backlinks(self, sites, *, whitelist_domains=()):
            assert whitelist_domains == ["trusted.example.com"]
            return {
                "items": [
                    {
                        "site_name": "alpha",
                        "site_id": "1",
                        "site_url": "https://alpha.example.com",
                        "title": "正常站",
                        "link": "https://ok.example.com/",
                        "reachable": True,
                        "has_backlink": True,
                        "http_status": 200,
                        "status": "ok",
                        "issue_kind": "ok",
                        "message": "有反链(dofollow)",
                    },
                    {
                        "site_name": "alpha",
                        "site_id": "1",
                        "site_url": "https://alpha.example.com",
                        "title": "掉链站",
                        "link": "https://dropped.example.com/",
                        "reachable": True,
                        "has_backlink": False,
                        "http_status": 200,
                        "status": "missing",
                        "issue_kind": "missing",
                        "message": "未发现我方主域名反链",
                    },
                    {
                        "site_name": "beta",
                        "site_id": "2",
                        "site_url": "https://beta.example.com",
                        "title": "白名单站",
                        "link": "https://trusted.example.com/",
                        "reachable": True,
                        "has_backlink": None,
                        "http_status": None,
                        "status": "whitelisted",
                        "issue_kind": "whitelisted",
                        "message": "白名单域名，已跳过",
                    },
                ],
                "site_errors": {},
                "summary": {
                    "total": 3,
                    "ok": 1,
                    "nofollow": 0,
                    "missing": 1,
                    "unreachable": 0,
                    "whitelisted": 1,
                },
            }

        def batch_remove(self, sites, urls):
            remove_calls.append(([s.name for s in sites], list(urls)))
            return [
                LinkOpResult(
                    site_name=sites[0].name,
                    site_id=sites[0].site_id,
                    action="remove",
                    link=urls[0],
                    title="",
                    status=LINK_DELETED,
                    message="已删除",
                )
            ]

    db = SiteGroupDB(db_path(cfg_path))
    try:
        with SiteGroupMcpServer(
            cfg_path,
            db=db,
            friendlink_service=FakeFriendLinkService(),
        ) as server:
            # remove_missing=True without confirmed=True must NOT delete!
            guard_res = _call_rpc_tool(
                server,
                "audit_backlinks",
                {
                    "site_names": ["alpha", "beta"],
                    "whitelist_domains": ["trusted.example.com"],
                    "remove_missing": True,
                    "confirmed": False,
                },
            )
            assert guard_res["isError"] is False
            assert remove_calls == []
            assert guard_res["structuredContent"]["confirmation_required"] is True
            assert guard_res["structuredContent"]["removal_summary"]["executed"] is False
            assert len(db.list_backlink_audits(site_name="alpha")) == 2
            assert db.get_site_status("alpha")["broken_link_count"] == 1

            # remove_missing=True AND confirmed=True removes ONLY alpha's missing link
            exec_res = _call_rpc_tool(
                server,
                "audit_backlinks",
                {
                    "site_names": ["alpha", "beta"],
                    "whitelist_domains": ["trusted.example.com"],
                    "remove_missing": True,
                    "confirmed": True,
                },
            )
            assert exec_res["isError"] is False
            assert remove_calls == [(["alpha"], ["https://dropped.example.com/"])]
            assert exec_res["structuredContent"]["removal_summary"]["executed"] is True
            assert exec_res["structuredContent"]["removal_summary"][LINK_DELETED] == 1
    finally:
        db.close()


def test_crosslink_plan_dry_run_vs_execute_and_rollback(tmp_path):
    cfg_path, _ = _write_sample_config(tmp_path)
    ledger = FriendLinkLedger(ledger_path(cfg_path))
    called = {"preview": 0, "execute": 0, "rollback": 0}

    class FakeFriendLinkService:
        def plan_crosslinks(self, sites, mode, **kwargs):
            s0, s1 = sites[0], sites[1]
            item = CrossLinkItem(
                source_site_id=s0.site_id,
                source_name=s0.name,
                target_site_id=s1.site_id,
                target_name=s1.name,
                spec=FriendLinkSpec(title="贝塔商城", link="https://beta.example.com/"),
                state=LINK_PLANNED,
                message="",
            )
            return CrossLinkPlan(
                plan_id="plan-abc123",
                mode=mode,
                created_at="2026-11-25T12:00:00",
                hub_site_id=kwargs.get("hub_site_id", ""),
                bidirectional=bool(kwargs.get("bidirectional", True)),
                items=(item,),
            )

        def preview_plan(self, plan, sites):
            called["preview"] += 1
            return plan

        def execute_plan(self, plan, sites, *, ledger):
            called["execute"] += 1
            ledger.record_plan(plan)
            item = plan.items[0]
            op_res = LinkOpResult(
                site_name=item.source_name,
                site_id=item.source_site_id,
                action="add",
                link=item.spec.link,
                title=item.spec.title,
                status=LINK_CREATED,
                message="已添加",
            )
            from models.sitegroup import LedgerEntry

            ledger.record_results(
                plan.plan_id,
                [
                    LedgerEntry(
                        plan_id=plan.plan_id,
                        source_site_id=item.source_site_id,
                        source_name=item.source_name,
                        target_site_id=item.target_site_id,
                        link=item.spec.link,
                        title=item.spec.title,
                        status=LINK_CREATED,
                        created_at="2026-11-25T12:00:01",
                    )
                ],
            )
            created_item = CrossLinkItem(
                source_site_id=item.source_site_id,
                source_name=item.source_name,
                target_site_id=item.target_site_id,
                target_name=item.target_name,
                spec=item.spec,
                state=LINK_CREATED,
                message="已添加",
            )
            return CrossLinkReport(
                plan=CrossLinkPlan(
                    plan_id=plan.plan_id,
                    mode=plan.mode,
                    created_at=plan.created_at,
                    hub_site_id=plan.hub_site_id,
                    bidirectional=plan.bidirectional,
                    items=(created_item,),
                ),
                results=(op_res,),
            )

        def rollback_plan(self, plan_id, sites, *, ledger):
            called["rollback"] += 1
            entries = ledger.created_links(plan_id)
            out = []
            for entry in entries:
                ledger.mark_rolled_back(plan_id, entry.source_site_id, entry.link)
                out.append(
                    LinkOpResult(
                        site_name=entry.source_name,
                        site_id=entry.source_site_id,
                        action="remove",
                        link=entry.link,
                        title=entry.title,
                        status=LINK_DELETED,
                        message="已删除",
                    )
                )
            return out

    with SiteGroupMcpServer(
        cfg_path,
        friendlink_service=FakeFriendLinkService(),
        ledger=ledger,
    ) as server:
        # Default dry_run=True -> previews only, no ledger
        preview_res = _call_rpc_tool(
            server,
            "crosslink_plan",
            {"mode": "mesh", "site_names": ["alpha", "beta"]},
        )
        assert preview_res["isError"] is False
        assert preview_res["structuredContent"]["dry_run"] is True
        assert preview_res["structuredContent"]["executed"] is False
        assert called == {"preview": 1, "execute": 0, "rollback": 0}
        assert ledger.list_plans() == []

        # dry_run=False without confirmed=True -> refused
        refused_res = _call_rpc_tool(
            server,
            "crosslink_plan",
            {"mode": "mesh", "site_names": ["alpha", "beta"], "dry_run": False, "confirmed": False},
        )
        assert refused_res["isError"] is True
        assert refused_res["structuredContent"]["confirmation_required"] is True
        assert called["execute"] == 0

        # dry_run=False AND confirmed=True -> executes & records ledger
        exec_res = _call_rpc_tool(
            server,
            "crosslink_plan",
            {"mode": "mesh", "site_names": ["alpha", "beta"], "dry_run": False, "confirmed": True},
        )
        assert exec_res["isError"] is False
        assert exec_res["structuredContent"]["executed"] is True
        assert exec_res["structuredContent"]["plan_id"] == "plan-abc123"
        assert called["execute"] == 1
        assert len(ledger.created_links("plan-abc123")) == 1

        # rollback without confirmed=True -> refused
        rb_refused = _call_rpc_tool(
            server,
            "rollback_crosslink",
            {"plan_id": "plan-abc123", "confirmed": False},
        )
        assert rb_refused["isError"] is True
        assert rb_refused["structuredContent"]["confirmation_required"] is True
        assert called["rollback"] == 0

        # rollback with confirmed=True -> rolls back
        rb_exec = _call_rpc_tool(
            server,
            "rollback_crosslink",
            {"plan_id": "plan-abc123", "confirmed": True},
        )
        assert rb_exec["isError"] is False
        assert rb_exec["structuredContent"]["summary"][LINK_DELETED] == 1
        assert called["rollback"] == 1
        assert ledger.created_links("plan-abc123") == []


def test_check_versions_and_upgrade_sites_confirmation_guard(tmp_path):
    cfg_path, _ = _write_sample_config(tmp_path)
    upgrade_calls = []

    class FakeUpgradeService:
        def check_versions(self, sites):
            return [
                SiteVersionInfo(
                    site_name=s.name,
                    site_id=s.site_id,
                    reachable=True,
                    current_version="3.6.5",
                    latest_version="3.6.8",
                    upgrade_available=True,
                    source="mcp",
                    check_tool="check_update",
                    upgrade_tool="system_upgrade",
                    message="有新版本 3.6.8",
                )
                for s in sites
            ]

        def upgrade_sites(
            self,
            sites,
            *,
            confirmed,
            max_workers=None,
            canary_first=True,
            stop_on_failure=True,
        ):
            assert confirmed is True
            upgrade_calls.append(
                ([s.name for s in sites], max_workers, canary_first, stop_on_failure)
            )
            return [
                UpgradeResult(
                    site_name=sites[0].name,
                    site_id=sites[0].site_id,
                    status=UPGRADE_UPGRADED,
                    from_version="3.6.5",
                    to_version="3.6.8",
                    message="升级成功",
                ),
                UpgradeResult(
                    site_name=sites[1].name,
                    site_id=sites[1].site_id,
                    status=UPGRADE_ALREADY_LATEST,
                    from_version="3.6.8",
                    to_version="3.6.8",
                    message="已是最新",
                ),
            ]

    db = SiteGroupDB(db_path(cfg_path))
    try:
        with SiteGroupMcpServer(cfg_path, db=db, upgrade_service=FakeUpgradeService()) as server:
            chk_res = _call_rpc_tool(server, "check_versions", {"group": "资讯组"})
            assert chk_res["isError"] is False
            assert chk_res["structuredContent"]["summary"]["upgrade_available"] == 2
            cached = db.get_site_status("alpha")
            assert cached["current_version"] == "3.6.5"
            assert cached["latest_version"] == "3.6.8"
            assert cached["update_available"] == 1
            assert cached["mcp_ok"] == 1

            # Strict confirmation guard
            blocked = _call_rpc_tool(
                server,
                "upgrade_sites",
                {"site_names": ["alpha", "gamma"], "confirmed": False},
            )
            assert blocked["isError"] is True
            assert blocked["structuredContent"]["confirmation_required"] is True
            assert upgrade_calls == []

            # Confirmed execution
            ok_res = _call_rpc_tool(
                server,
                "upgrade_sites",
                {
                    "site_names": ["alpha", "gamma"],
                    "confirmed": True,
                    "canary": True,
                    "concurrency": 3,
                    "stop_on_failure": False,
                },
            )
            assert ok_res["isError"] is False
            assert upgrade_calls == [(["alpha", "gamma"], 3, True, False)]
            assert ok_res["structuredContent"]["summary"][UPGRADE_UPGRADED] == 1
            assert db.get_site_status("alpha")["current_version"] == "3.6.8"
            assert db.get_site_status("alpha")["update_available"] == 0
    finally:
        db.close()


def test_get_admin_jump_url_does_not_open_browser_and_redacts_secrets(tmp_path):
    cfg_path, _ = _write_sample_config(tmp_path)
    calls = {"resolve": 0, "open": 0}

    class FakeJumpService:
        def resolve(self, site):
            calls["resolve"] += 1
            return JumpPlan(
                site_name=site.name,
                tier=JUMP_TIER_TICKET,
                target_url="https://alpha.example.com/system/?ticket=alpha-mcp-secret&token=raw-secret-1234",
                display_url="https://alpha.example.com/system/",
                fallback_reasons=("used alpha-pass-secret fallback",),
            )

        def open(self, site):
            calls["open"] += 1
            raise AssertionError("MCP mode must never call AdminJumpService.open()")

    with SiteGroupMcpServer(cfg_path, jump_service=FakeJumpService()) as server:
        res = _call_rpc_tool(server, "get_admin_jump_url", {"site_name": "alpha"})
        assert res["isError"] is False
        assert calls == {"resolve": 1, "open": 0}
        sc = res["structuredContent"]
        assert sc["tier"] == JUMP_TIER_TICKET
        assert "alpha-mcp-secret" not in sc["url"]
        assert "raw-secret-1234" not in sc["url"]
        assert "alpha-pass-secret" not in sc["reason"]
        assert "***" in sc["url"]
        assert "***" in sc["reason"]

        missing_res = _call_rpc_tool(server, "get_admin_jump_url", {"site_name": "nope"})
        assert missing_res["isError"] is True


def test_import_sites_preview_save_and_auto_discover(tmp_path):
    cfg_path, _ = _write_sample_config(tmp_path)

    class _HomeHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

        def do_GET(self):  # noqa: N802
            body = "<html><head><title>新站首页标题</title></head><body>ok</body></html>".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _HomeHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]

    class FakeMcpClient:
        def initialize(self):
            return {"protocolVersion": "2025-06-18"}

        def list_tools(self):
            return [McpToolInfo(name="check_update"), McpToolInfo(name="friendlink_list")]

        def close(self):
            pass

    try:
        bulk_text = (
            f"delta|http://127.0.0.1:{port}|新分组|delta-mcp-secret|root|delta-pwd-secret|\n"
            "alpha|https://alpha.example.com|资讯组\n"  # duplicate skipped
        )
        with SiteGroupMcpServer(
            cfg_path,
            mcp_client_factory=lambda _site: FakeMcpClient(),
        ) as server:
            # Preview only (save=False)
            preview = _call_rpc_tool(
                server,
                "import_sites",
                {"text": bulk_text, "save": False, "auto_discover": True},
            )
            assert preview["isError"] is False
            sc_prev = preview["structuredContent"]
            assert sc_prev["saved"] is False
            assert sc_prev["parsed_count"] == 1
            assert sc_prev["warning_count"] == 1
            assert sc_prev["sites"][0]["name"] == "delta"
            assert sc_prev["sites"][0]["site_title"] == "新站首页标题"
            assert sc_prev["sites"][0]["discovery"]["reachable"] is True
            assert sc_prev["sites"][0]["discovery"]["mcp_ok"] is True
            assert len(load_config(cfg_path)) == 3  # not saved yet!

            serialized_prev = json.dumps(preview, ensure_ascii=False)
            assert "delta-mcp-secret" not in serialized_prev
            assert "delta-pwd-secret" not in serialized_prev

            # Save=True persists to config.ini
            saved = _call_rpc_tool(
                server,
                "import_sites",
                {"text": bulk_text, "save": True, "auto_discover": False},
            )
            assert saved["isError"] is False
            assert saved["structuredContent"]["saved"] is True
            loaded = load_config(cfg_path)
            assert [s.name for s in loaded] == ["alpha", "beta", "gamma", "delta"]
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------------------
# Stdio & mcp_server.py CLI tests
# ---------------------------------------------------------------------------


def test_serve_stdio_roundtrip_and_parse_error(tmp_path):
    cfg_path, _ = _write_sample_config(tmp_path)
    stdin_lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),  # notification -> no output line
        "",  # blank line ignored
        "{invalid json",  # parse error -> -32700
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_sites", "arguments": {"enabled_only": True}}}),
    ]
    in_buf = io.StringIO("\n".join(stdin_lines) + "\n")
    out_buf = io.StringIO()

    with SiteGroupMcpServer(cfg_path) as server:
        server.serve_stdio(stdin=in_buf, stdout=out_buf)

    raw_outputs = [line for line in out_buf.getvalue().splitlines() if line.strip()]
    assert len(raw_outputs) == 4
    responses = [json.loads(line) for line in raw_outputs]

    assert responses[0]["id"] == 1
    assert responses[0]["result"]["protocolVersion"] == "2025-06-18"
    assert responses[1]["error"]["code"] == -32700
    assert responses[2] == {"jsonrpc": "2.0", "id": 2, "result": {}}
    assert responses[3]["id"] == 3
    assert responses[3]["result"]["structuredContent"]["total"] == 2


def test_importing_mcp_server_entrypoint_does_not_block():
    mod = importlib.import_module("mcp_server")
    assert hasattr(mod, "main")
    assert hasattr(mod, "resolve_config_path")


def test_mcp_server_cli_subprocess_roundtrip(tmp_path):
    cfg_path, _ = _write_sample_config(tmp_path)
    repo_root = Path(__file__).resolve().parents[1]
    requests_payload = "\n".join(
        [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_sites", "arguments": {}}}),
        ]
    ) + "\n"

    proc = subprocess.run(
        [sys.executable, str(repo_root / "mcp_server.py"), "--config", str(cfg_path)],
        input=requests_payload,
        text=True,
        encoding="utf-8",
        capture_output=True,
        cwd=str(repo_root),
        timeout=15,
        check=False,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 3
    msgs = [json.loads(line) for line in lines]
    assert msgs[0]["result"]["protocolVersion"] == "2024-11-05"
    assert {t["name"] for t in msgs[1]["result"]["tools"]} == EXPECTED_TOOLS
    assert msgs[2]["result"]["structuredContent"]["total"] == 3


def test_zero_tk_imports_injected_config_service_and_crosslink_modes(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    for rel_path in ("core/sitegroup_mcp_server.py", "mcp_server.py"):
        tree = ast.parse((repo_root / rel_path).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "tkinter" not in imported
        assert "ui" not in imported

    cfg_path, sites = _write_sample_config(tmp_path)

    class CustomConfigService:
        def __init__(self, initial):
            self.sites = list(initial)
            self.saved_calls = 0

        def load(self):
            return list(self.sites)

        def save(self, updated):
            self.saved_calls += 1
            self.sites = list(updated)

    fake_cfg_svc = CustomConfigService(sites)
    proxy_sentinel = object()
    captured_crosslink_kwargs = []

    class RecordingFriendLinkService:
        def plan_crosslinks(self, target_sites, mode, **kwargs):
            captured_crosslink_kwargs.append(([s.name for s in target_sites], mode, kwargs))
            return CrossLinkPlan(
                plan_id="plan-rec",
                mode=mode,
                created_at="2026-11-25T12:00:00",
                hub_site_id=kwargs.get("hub_site_id", ""),
                bidirectional=bool(kwargs.get("bidirectional", True)),
                items=(),
            )

        def preview_plan(self, plan, target_sites):
            return plan

    with SiteGroupMcpServer(
        cfg_path,
        config_service=fake_cfg_svc,
        friendlink_service=RecordingFriendLinkService(),
        proxy=proxy_sentinel,
    ) as server:
        # Lazy AdminJumpService receives injected proxy and resolves direct tier safely
        assert server._get_jump_service().proxy is proxy_sentinel
        jump_res = _call_rpc_tool(server, "get_admin_jump_url", {"site_name": "beta"})
        assert jump_res["isError"] is False
        assert jump_res["structuredContent"]["tier"] == "direct"
        assert jump_res["structuredContent"]["url"] == "https://beta.example.com/system/"

        # Custom config_service persists import_sites(save=True)
        imp_res = _call_rpc_tool(
            server,
            "import_sites",
            {"text": "epsilon|https://epsilon.example.com|电商组", "save": True},
        )
        assert imp_res["isError"] is False
        assert imp_res["structuredContent"]["saved"] is True
        assert fake_cfg_svc.saved_calls == 1
        assert [s.name for s in fake_cfg_svc.sites] == ["alpha", "beta", "gamma", "epsilon"]

        # crosslink_plan forwards hub / random_k / group_cross params accurately
        _call_rpc_tool(
            server,
            "crosslink_plan",
            {
                "mode": "hub",
                "site_names": ["alpha", "beta"],
                "hub_site": "alpha",
                "bidirectional": False,
                "nofollow": True,
            },
        )
        _call_rpc_tool(
            server,
            "crosslink_plan",
            {
                "mode": "group_cross",
                "group_a": "资讯组",
                "group_b": "电商组",
                "k": 2,
                "avoid_reciprocal": False,
                "avoid_same_root_domain": False,
            },
        )

    assert len(captured_crosslink_kwargs) == 2
    assert captured_crosslink_kwargs[0][1] == "hub"
    assert captured_crosslink_kwargs[0][2]["hub_site_id"] == "alpha"
    assert captured_crosslink_kwargs[0][2]["bidirectional"] is False
    assert captured_crosslink_kwargs[0][2]["nofollow"] is True
    assert captured_crosslink_kwargs[1][1] == "group_cross"
    assert captured_crosslink_kwargs[1][2]["group_a"] == "资讯组"
    assert captured_crosslink_kwargs[1][2]["group_b"] == "电商组"
    assert captured_crosslink_kwargs[1][2]["k"] == 2
    assert captured_crosslink_kwargs[1][2]["avoid_reciprocal"] is False
    assert captured_crosslink_kwargs[1][2]["avoid_same_root_domain"] is False

