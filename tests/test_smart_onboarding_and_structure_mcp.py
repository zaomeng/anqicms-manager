"""Tests for smart paste onboarding and real AnqiCMS v3.6.7 MCP `structure` friendlinks."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.config_service import load_config, save_config
from core.friendlink_service import FriendLinkError, FriendLinkService
from core.mcp_contracts import score_tools
from core.site_onboarding import (
    SmartImportResult,
    auto_discover_site,
    parse_bulk_new_sites,
    parse_smart_site_import,
)
from models.entities import SiteConfig
from models.sitegroup import (
    LINK_CREATED,
    LINK_DELETED,
    LINK_NOT_FOUND,
    LINK_SKIPPED_EXISTS,
    FriendLink,
    FriendLinkSpec,
    McpProbeResult,
    McpToolInfo,
    McpToolResult,
)


def _make_site(
    name: str,
    url: str,
    *,
    jiekou: str = "",
    mcp_token: str = "",
    fid: int = 1,
    site_title: str = "",
    site_group: str = "",
) -> SiteConfig:
    return SiteConfig(
        name=name,
        fid=fid,
        url=url,
        jiekou=jiekou,
        publish_dir=Path("文章目录") / name,
        daily_publish_filename=Path("设置") / f"{name}_daily.txt",
        publish_time="08:00:00",
        publish_times=["08:00:00", "20:00:00"],
        interval_time=45,
        daily_publish_limit=25,
        enabled=True,
        gallery_id="gal-existing",
        category_name="原有栏目",
        site_id=f"id-{name}",
        site_title=site_title,
        site_group=site_group,
        mcp_token=mcp_token,
    )


class FakeResponse:
    def __init__(self, status_code: int = 200, *, json_data: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.text = text or (json.dumps(json_data, ensure_ascii=False) if json_data is not None else "")
        self.content = self.text.encode("utf-8")
        self.headers = {"Content-Type": "application/json; charset=utf-8"} if json_data is not None else {}

    def json(self) -> Any:
        if self._json_data is None:
            raise ValueError("no json")
        return self._json_data

    def close(self) -> None:
        return None


class RecordingSession:
    def __init__(self, handler) -> None:
        self._handler = handler
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": "GET", "url": url, **kwargs})
        return self._handler("GET", url, kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": "POST", "url": url, **kwargs})
        return self._handler("POST", url, kwargs)

    def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------- #
# Smart Paste Onboarding
# --------------------------------------------------------------------------- #
def test_parse_smart_site_import_fuses_mcp_json_and_archive_url(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ini"
    paste = """
    {
      "mcpServers": {
        "示例视觉传媒": {
          "url": "https://demo.example.com/api/mcp",
          "headers": {
            "Authorization": "Bearer tok1"
          }
        }
      }
    }

    https://demo.example.com/api/import/archive?token=tok2
    """

    result = parse_smart_site_import(paste, [], cfg_path, default_group="品牌站")
    assert isinstance(result, SmartImportResult)
    assert result.warnings == []
    assert result.updated_sites == []
    assert result.merged_count == 1
    assert len(result.new_sites) == 1

    site = result.new_sites[0]
    assert site.site_title == "示例视觉传媒"
    assert site.url == "https://demo.example.com"
    assert site.mcp_token == "tok1"
    assert site.jiekou == "/api/import/archive?token=tok2"
    assert site.site_group == "品牌站"
    assert site.fid == 0
    assert site.enabled is False

    # Config roundtrip succeeds cleanly
    save_config(cfg_path, result.new_sites)
    loaded = load_config(cfg_path)
    assert len(loaded) == 1
    assert loaded[0].site_title == "示例视觉传媒"
    assert loaded[0].mcp_token == "tok1"
    assert loaded[0].jiekou == "/api/import/archive?token=tok2"


def test_parse_smart_site_import_mixed_multiple_blocks_urls_and_pipes(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ini"
    paste = """
    # Archive URL BEFORE its mcpServers block + www normalization
    发布接口：http://alpha-site.example.com/api/import/archive?token=imp-main-1

    {
      "mcpServers": {
        "示例视觉传媒": {
          "url": "https://www.alpha-site.example.com/api/mcp/",
          "headers": {
            "Authorization": "Bearer mcp-main-1"
          }
        }
      }
    }
    {
      "mcpServers": {
        "站B": {
          "url": "https://b.example.com/mcp",
          "headers": {
            "authorization": "bearer mcp-b-2"
          }
        },
        "坏站点": {
          "url": "not a valid url",
          "headers": {
            "Authorization": "Bearer secret-bad-tok"
          }
        }
      }
    }
    https://c.example.com/api/import/archive?token=imp-c-3
    custom-d|https://d.example.com|自定义组|mcp-d-4|adminD|passD|站D
    """

    result = parse_smart_site_import(paste, [], cfg_path, default_group="默认组")
    assert len(result.new_sites) == 4
    assert result.merged_count == 1
    assert len(result.warnings) == 1
    assert "secret-bad-tok" not in json.dumps(result.warnings, ensure_ascii=False)

    by_title = {s.site_title or s.name: s for s in result.new_sites}
    main_site = by_title["示例视觉传媒"]
    assert main_site.url == "https://www.alpha-site.example.com"
    assert main_site.mcp_token == "mcp-main-1"
    assert main_site.jiekou == "/api/import/archive?token=imp-main-1"
    assert main_site.site_group == "默认组"

    sb = by_title["站B"]
    assert sb.url == "https://b.example.com"
    assert sb.mcp_token == "mcp-b-2"
    assert sb.jiekou == ""
    assert sb.site_group == "默认组"

    sc = next(s for s in result.new_sites if s.url == "https://c.example.com")
    assert sc.jiekou == "/api/import/archive?token=imp-c-3"
    assert sc.mcp_token == ""
    assert sc.site_group == "默认组"

    sd = by_title["站D"]
    assert sd.name == "custom-d"
    assert sd.site_group == "自定义组"
    assert sd.admin_username == "adminD"
    assert sd.admin_password == "passD"


def test_parse_smart_site_import_smart_upsert_preserves_existing_settings(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ini"
    existing_full = _make_site(
        "main-site",
        "https://www.alpha-site.example.com",
        jiekou="/api/import/archive?token=old-imp",
        mcp_token="",
        fid=12,
        site_title="旧标题",
        site_group="旧分组",
    )
    existing_partial = _make_site(
        "partial-site",
        "https://partial.example.com",
        jiekou="/api/import/archive?token=keep-me",
        mcp_token="",
        fid=7,
        site_title="",
        site_group="保留组",
    )

    paste = """
    {
      "mcpServers": {
        "示例视觉传媒": {
          "url": "https://alpha-site.example.com/api/mcp",
          "headers": {
            "Authorization": "Bearer new-mcp-tok"
          }
        },
        "局部站点": {
          "url": "https://partial.example.com/api/mcp",
          "headers": {
            "Authorization": "Bearer partial-mcp-tok"
          }
        }
      }
    }
    https://alpha-site.example.com/api/import/archive?token=new-imp-tok
    https://brandnew.example.com/api/import/archive?token=new-site-tok
    """

    res = parse_smart_site_import(
        paste,
        [existing_full, existing_partial],
        cfg_path,
        default_group="默认新组",
    )
    assert res.warnings == []
    assert res.merged_count == 1
    assert [s.url for s in res.new_sites] == ["https://brandnew.example.com"]
    assert res.new_sites[0].site_group == "默认新组"
    assert len(res.updated_sites) == 2

    upd_main = next(s for s in res.updated_sites if s.name == "main-site")
    assert upd_main.mcp_token == "new-mcp-tok"
    assert upd_main.jiekou == "/api/import/archive?token=new-imp-tok"
    assert upd_main.site_title == "示例视觉传媒"
    # Existing schedule/gallery/publish settings preserved intact
    assert upd_main.fid == 12
    assert upd_main.enabled is True
    assert upd_main.publish_times == ["08:00:00", "20:00:00"]
    assert upd_main.interval_time == 45
    assert upd_main.daily_publish_limit == 25
    assert upd_main.gallery_id == "gal-existing"
    assert upd_main.category_name == "原有栏目"
    assert upd_main.site_group == "旧分组"

    upd_partial = next(s for s in res.updated_sites if s.name == "partial-site")
    assert upd_partial.mcp_token == "partial-mcp-tok"
    assert upd_partial.jiekou == "/api/import/archive?token=keep-me"
    assert upd_partial.site_title == "局部站点"
    assert upd_partial.site_group == "保留组"

    # parse_bulk_new_sites delegates cleanly and preserves backward-compat return tuple
    bulk_new, bulk_warnings = parse_bulk_new_sites(paste, [existing_full, existing_partial], cfg_path)
    assert [s.url for s in bulk_new] == ["https://brandnew.example.com"]
    assert len(bulk_warnings) >= 2
    joined = json.dumps(bulk_warnings, ensure_ascii=False)
    assert "new-mcp-tok" not in joined
    assert "new-imp-tok" not in joined


def test_auto_discover_site_captures_version_and_default_category(tmp_path: Path) -> None:
    site = _make_site(
        "newsite",
        "https://demo.example.com",
        jiekou="/api/import/archive?token=imp-tok-1",
        mcp_token="mcp-tok-1",
        fid=0,
    )
    site.category_name = ""

    def _handler(_method: str, url: str, _kw: dict[str, Any]) -> FakeResponse:
        if url == "https://demo.example.com":
            return FakeResponse(200, text="<html><head><title>示例官网</title></head></html>")
        if url.startswith("https://demo.example.com/api/friendlink/list"):
            return FakeResponse(200, json_data={"code": 0, "data": [{"id": 1, "link": "https://a.com"}]})
        if url.startswith("https://demo.example.com/api/import/categories"):
            return FakeResponse(
                200,
                json_data={
                    "code": 0,
                    "data": [
                        {"id": 5, "parent_id": 0, "title": "公司新闻"},
                        {"id": 6, "parent_id": 0, "title": "行业资讯"},
                    ],
                },
            )
        return FakeResponse(404)

    probe = McpProbeResult(
        site_name="newsite",
        ok=True,
        server_name="AnqiCMS",
        server_version="3.6.7",
        tools=(McpToolInfo(name="structure"),),
    )

    res = auto_discover_site(
        site,
        session_factory=lambda: RecordingSession(_handler),
        probe_fn=lambda _s: probe,
        update_site=True,
    )
    assert res.reachable is True
    assert res.site_title == "示例官网"
    assert res.link_count == 1
    assert res.mcp_ok is True
    assert res.current_version == "3.6.7"
    assert res.default_fid == 5
    assert res.default_category_name == "公司新闻"
    assert site.fid == 5
    assert site.category_name == "公司新闻"


# --------------------------------------------------------------------------- #
# Real AnqiCMS v3.6.7 MCP `structure` FriendLink Support
# --------------------------------------------------------------------------- #
class _StructureMcpClient:
    """Fake AnqiCMS v3.6.7 MCP server exposing `content_article` + `structure`."""

    def __init__(self, initial_links: list[dict[str, Any]] | None = None) -> None:
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.links: list[dict[str, Any]] = [dict(item) for item in (initial_links or [])]
        self.next_id = max((item.get("id", 0) for item in self.links), default=0) + 1
        self.tools = [
            # Put content_article FIRST and mention friendlink in description to verify structure always wins!
            McpToolInfo(
                name="content_article",
                description="Article & friendlink link manager",
                input_schema={
                    "type": "object",
                    "properties": {"action": {"type": "string", "enum": ["list", "create", "delete"]}},
                },
            ),
            McpToolInfo(
                name="structure",
                description="Manage site categories, modules, and tags",
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [
                                "category_list",
                                "friendlink_list",
                                "friendlink_create",
                                "friendlink_delete",
                            ],
                        },
                        "id": {"type": "integer"},
                        "title": {"type": "string"},
                        "link": {"type": "string"},
                        "logo": {"type": "string"},
                    },
                },
            ),
        ]

    def initialize(self) -> dict:
        return {"protocolVersion": "2025-06-18", "serverInfo": {"name": "AnqiCMS", "version": "3.6.7"}}

    def list_tools(self, *, refresh: bool = False) -> list[McpToolInfo]:
        return list(self.tools)

    def find_tools(self, keywords, *, explicit_names=()) -> list[McpToolInfo]:
        return score_tools(self.tools, keywords, explicit_names)

    def call_tool(self, name: str, arguments=None, *, timeout=None) -> McpToolResult:
        args = dict(arguments or {})
        self.calls.append((name, args))
        if name != "structure":
            return McpToolResult(is_error=True, text=f"unexpected tool {name}")

        action = args.get("action")
        if action == "friendlink_list":
            payload = {"code": 0, "data": list(self.links)}
            return McpToolResult(is_error=False, text=json.dumps(payload), structured=payload)
        if action == "friendlink_create":
            assert set(args.keys()) <= {"action", "title", "link", "logo"}
            if any(item["link"] == args["link"] for item in self.links):
                payload = {"code": -1, "msg": "友链已存在"}
                return McpToolResult(is_error=False, text=json.dumps(payload), structured=payload)
            entry = {
                "id": self.next_id,
                "title": args["title"],
                "link": args["link"],
                "logo": args.get("logo", ""),
            }
            self.next_id += 1
            self.links.append(entry)
            payload = {"code": 0, "msg": "添加成功", "data": entry}
            return McpToolResult(is_error=False, text=json.dumps(payload), structured=payload)
        if action == "friendlink_delete":
            target_id = args.get("id")
            assert type(target_id) is int
            before = len(self.links)
            self.links = [item for item in self.links if item["id"] != target_id]
            if len(self.links) == before:
                payload = {"code": -1, "msg": "友链不存在"}
                return McpToolResult(is_error=False, text=json.dumps(payload), structured=payload)
            payload = {"code": 0, "msg": "删除成功"}
            return McpToolResult(is_error=False, text=json.dumps(payload), structured=payload)

        return McpToolResult(is_error=True, text=f"unknown action {action}")

    def close(self) -> None:
        self.closed = True


def test_mcp_structure_friendlink_list_create_delete_and_id_resolution() -> None:
    initial = [{"id": 15, "title": "老朋友", "link": "https://old.example.com/"}]
    shared_state = list(initial)
    clients: list[_StructureMcpClient] = []

    def _mcp_factory(_site) -> _StructureMcpClient:
        client = _StructureMcpClient(shared_state)

        orig_call = client.call_tool

        def _synced_call(name: str, arguments=None, *, timeout=None):
            res = orig_call(name, arguments, timeout=timeout)
            shared_state[:] = list(client.links)
            return res

        client.call_tool = _synced_call  # type: ignore[method-assign]
        clients.append(client)
        return client

    site = _make_site("示例站", "https://demo.example.com", jiekou="", mcp_token="mcp-tok-367")
    svc = FriendLinkService(
        session_factory=lambda: RecordingSession(lambda *_: FakeResponse(404)),
        mcp_client_factory=_mcp_factory,
    )

    # 1. list_links uses structure({"action": "friendlink_list"}) and beats content_article
    links = svc.list_links(site)
    assert len(links) == 1
    assert links[0].id == 15
    assert links[0].title == "老朋友"
    assert clients[-1].calls == [("structure", {"action": "friendlink_list"})]

    # 2. add_link uses structure({"action": "friendlink_create", ...}) and strips unsupported fields
    add_res = svc.add_link(
        site,
        FriendLinkSpec(
            title="新朋友",
            link="https://new.example.com/",
            nofollow=True,
            back_link="https://new.example.com/links",
            my_title="示例站",
            my_link="https://demo.example.com/",
            contact="qq:100",
            remark="不应传给structure",
        ),
    )
    assert add_res.status == LINK_CREATED
    assert clients[-1].calls == [
        ("structure", {"action": "friendlink_create", "title": "新朋友", "link": "https://new.example.com/"})
    ]

    # 2b. Optional logo IS included when present on spec
    @dataclass(frozen=True)
    class _SpecWithLogo(FriendLinkSpec):
        logo: str = ""

    add_logo_res = svc.add_link(
        site,
        _SpecWithLogo(title="带Logo", link="https://logo.example.com/", logo="https://logo.example.com/l.png"),
    )
    assert add_logo_res.status == LINK_CREATED
    assert clients[-1].calls == [
        (
            "structure",
            {
                "action": "friendlink_create",
                "title": "带Logo",
                "link": "https://logo.example.com/",
                "logo": "https://logo.example.com/l.png",
            },
        )
    ]

    # 2c. Duplicate create error maps to LINK_SKIPPED_EXISTS
    dup_res = svc.add_link(site, FriendLinkSpec(title="重复", link="https://new.example.com/"))
    assert dup_res.status == LINK_SKIPPED_EXISTS

    # 3. remove_link WITHOUT existing resolves id via friendlink_list and deletes by int id!
    del_res = svc.remove_link(site, "http://www.new.example.com")
    assert del_res.status == LINK_DELETED
    assert clients[-1].calls == [
        ("structure", {"action": "friendlink_list"}),
        ("structure", {"action": "friendlink_delete", "id": 16}),
    ]

    # 3b. remove_link WITH existing uses existing.id directly (no extra friendlink_list call)
    del_existing_res = svc.remove_link(
        site,
        "https://old.example.com/",
        existing=[FriendLink(id=15, title="老朋友", link="https://old.example.com/")],
    )
    assert del_existing_res.status == LINK_DELETED
    assert clients[-1].calls == [("structure", {"action": "friendlink_delete", "id": 15})]

    # 3c. remove_link for absent link returns LINK_NOT_FOUND
    nf_res = svc.remove_link(site, "https://missing.example.com/")
    assert nf_res.status == LINK_NOT_FOUND

    assert all(c.closed for c in clients)


def test_list_links_without_tokens_tries_public_rest_endpoint() -> None:
    site = _make_site("无Token站", "https://demo.example.com", jiekou="", mcp_token="")
    sessions: list[RecordingSession] = []

    def _handler(_method: str, url: str, kwargs: dict[str, Any]) -> FakeResponse:
        assert url == "https://demo.example.com/api/friendlink/list"
        assert "params" not in kwargs
        return FakeResponse(
            200,
            json_data={
                "code": 0,
                "data": [{"id": 1, "title": "公开友链", "link": "https://friend.example.com/"}],
            },
        )

    def _session_factory() -> RecordingSession:
        sess = RecordingSession(_handler)
        sessions.append(sess)
        return sess

    svc = FriendLinkService(session_factory=_session_factory)
    links = svc.list_links(site)
    assert len(links) == 1
    assert links[0].title == "公开友链"
    assert links[0].link == "https://friend.example.com/"
    assert all(s.closed for s in sessions)

    # Error response {"code": -1} raises FriendLinkError instead of returning empty list
    def _err_mcp_factory(_site):
        client = _StructureMcpClient()
        client.call_tool = lambda *_a, **_kw: McpToolResult(  # type: ignore[method-assign]
            is_error=False,
            text='{"code": -1, "msg": "未开启友情链接权限", "data": null}',
            structured={"code": -1, "msg": "未开启友情链接权限", "data": None},
        )
        return client

    site_mcp = _make_site("权限不足", "https://demo.example.com", jiekou="", mcp_token="tok")
    svc_err = FriendLinkService(
        session_factory=lambda: RecordingSession(lambda *_: FakeResponse(404)),
        mcp_client_factory=_err_mcp_factory,
    )
    try:
        svc_err.list_links(site_mcp)
        raise AssertionError("Expected FriendLinkError")
    except FriendLinkError as exc:
        assert "未开启友情链接权限" in exc.message
