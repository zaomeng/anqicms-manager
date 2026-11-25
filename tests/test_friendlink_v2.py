"""Comprehensive hermetic v2 tests for core/friendlink_service.py (zero real network)."""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import requests

from core.friendlink_ledger import FriendLinkLedger
from core.friendlink_service import (
    _MAX_BACKLINK_BYTES,
    CROSSLINK_GROUP_CROSS,
    CROSSLINK_MESH,
    CROSSLINK_RANDOM_K,
    FriendLinkError,
    FriendLinkService,
    export_sites_html_links,
    plan_crosslink_items,
)
from core.integration_settings import FriendLinkSettings, IntegrationSettings, McpSettings
from core.mcp_contracts import score_tools
from models.entities import SiteConfig
from models.sitegroup import (
    LINK_CREATED,
    LINK_DELETED,
    LINK_FAILED,
    LINK_PLANNED,
    LINK_SKIPPED_EXISTS,
    LINK_SKIPPED_SELF,
    FriendLink,
    FriendLinkSpec,
    McpToolInfo,
    McpToolResult,
)


def _make_site(
    name: str,
    url: str,
    *,
    token: str = "tok-v2-secret-12345",
    site_id: str = "",
    site_title: str = "",
    site_group: str = "",
    mcp_token: str = "",
) -> SiteConfig:
    jiekou = f"{url.rstrip('/')}/api/import/archive?token={token}" if (url and token) else ""
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
        site_id=site_id or name,
        site_title=site_title,
        site_group=site_group,
        mcp_token=mcp_token,
    )


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        *,
        json_data: Any = None,
        text: str = "",
        content: bytes | None = None,
        chunks: list[bytes] | None = None,
        headers: dict[str, str] | None = None,
        url: str | None = None,
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")
        self._chunks = chunks
        self.headers = headers or {}
        if url is not None:
            self.url = url
        self.closed = False
        self.chunks_yielded = 0

    def json(self) -> Any:
        return self._json_data

    def iter_content(self, chunk_size: int = 16384):
        if self._chunks is not None:
            for chunk in self._chunks:
                self.chunks_yielded += 1
                yield chunk
        else:
            data = self.content
            for start in range(0, len(data), chunk_size):
                self.chunks_yielded += 1
                yield data[start : start + chunk_size]

    def close(self) -> None:
        self.closed = True


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


class InMemoryRestStore:
    """Thread-safe fake AnqiCMS backend for multi-site v2 tests."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.links: dict[str, list[dict[str, Any]]] = {}
        self.fail_sites: dict[str, Any] = {}
        self.list_counts: dict[str, int] = {}
        self.create_counts: dict[str, int] = {}
        self.site_delays: dict[str, float] = {}
        self.calls: list[dict[str, Any]] = []
        self.sessions: list[RecordingSession] = []
        self.next_id = 1
        self.active_workers = 0
        self.max_active_workers = 0
        self.reject_duplicate_on_create = False

    def seed(self, base_url: str, items: list[dict[str, Any]]) -> None:
        self.links[base_url.rstrip("/")] = [dict(i) for i in items]

    def session_factory(self) -> RecordingSession:
        sess = RecordingSession(self._handle)
        with self.lock:
            self.sessions.append(sess)
        return sess

    @staticmethod
    def _base_of(url: str) -> str:
        idx = url.find("/api/friendlink/")
        return url[:idx].rstrip("/") if idx != -1 else url.rstrip("/")

    def _handle(self, method: str, url: str, kwargs: dict[str, Any]) -> FakeResponse:
        base = self._base_of(url)
        with self.lock:
            self.calls.append({"method": method, "url": url, **kwargs})
            self.active_workers += 1
            if self.active_workers > self.max_active_workers:
                self.max_active_workers = self.active_workers
            delay = self.site_delays.get(base, 0.0)
            failure = self.fail_sites.get(base)
        try:
            if delay > 0:
                time.sleep(delay)
            if isinstance(failure, Exception):
                raise failure
            if isinstance(failure, FakeResponse):
                return failure

            with self.lock:
                if url.endswith("/api/friendlink/list"):
                    self.list_counts[base] = self.list_counts.get(base, 0) + 1
                    items = [dict(i) for i in self.links.get(base, [])]
                    return FakeResponse(
                        200, json_data={"code": 0, "data": {"list": items, "total": len(items)}}
                    )
                if url.endswith("/api/friendlink/create"):
                    self.create_counts[base] = self.create_counts.get(base, 0) + 1
                    files = kwargs.get("files") or {}
                    target_link = files["link"][1]
                    if self.reject_duplicate_on_create and any(
                        i["link"] == target_link for i in self.links.get(base, [])
                    ):
                        return FakeResponse(200, json_data={"code": -1, "msg": "友链已存在"})
                    entry = {
                        "id": self.next_id,
                        "title": files["title"][1],
                        "link": target_link,
                        "nofollow": int(files["nofollow"][1]),
                        "back_link": files["back_link"][1],
                        "my_title": files["my_title"][1],
                        "my_link": files["my_link"][1],
                        "contact": files["contact"][1],
                        "remark": files["remark"][1],
                    }
                    self.next_id += 1
                    self.links.setdefault(base, []).append(entry)
                    return FakeResponse(200, json_data={"code": 0, "msg": "ok"})
                if url.endswith("/api/friendlink/delete"):
                    files = kwargs.get("files") or {}
                    target = files["link"][1]
                    self.links[base] = [
                        i for i in self.links.get(base, []) if i.get("link") != target
                    ]
                    return FakeResponse(200, json_data={"code": 0, "msg": "deleted"})
            return FakeResponse(404)
        finally:
            with self.lock:
                self.active_workers -= 1


# --------------------------------------------------------------------------- #
# P0 Fixes
# --------------------------------------------------------------------------- #
def test_p0_backlink_sends_browser_headers_and_respects_2mb_cap() -> None:
    assert _MAX_BACKLINK_BYTES == 2 * 1024 * 1024

    site = _make_site("主站", "https://mysite.example.com")
    seen_headers: list[dict[str, str]] = []
    large_ok_chunks = [
        b" " * (700 * 1024),
        b" " * (600 * 1024),
        b'<footer><a href="https://mysite.example.com/">\xe9\xa1\xb5\xe8\x84\x9a\xe5\x8f\x8b\xe9\x93\xbe</a></footer>',
    ]
    too_large_resp = FakeResponse(
        200,
        chunks=[
            b"a" * (1024 * 1024),
            b"b" * (1024 * 1024),
            b'<a href="https://mysite.example.com/">\xe8\xb6\x85\xe5\x87\xba2MB</a>',
        ],
    )

    def _handler(_method: str, url: str, kwargs: dict[str, Any]) -> FakeResponse:
        headers = kwargs.get("headers") or {}
        seen_headers.append(dict(headers))
        ua = headers.get("User-Agent", "")
        if "Chrome/131.0.0.0" not in ua:
            return FakeResponse(403)
        if url == "https://waf-footer.example.com/links":
            return FakeResponse(200, text="<p>no link here</p>")
        if url == "https://waf-footer.example.com/":
            return FakeResponse(200, chunks=large_ok_chunks)
        if url == "https://over2mb.example.com/":
            return too_large_resp
        return FakeResponse(404)

    svc = FriendLinkService(session_factory=lambda: RecordingSession(_handler))
    res = svc.check_backlinks(
        site,
        [
            FriendLink(id=1, title="Footer", link="https://waf-footer.example.com/links"),
            FriendLink(id=2, title="Huge", link="https://over2mb.example.com/"),
        ],
    )

    assert len(res) == 2
    assert res[0].reachable is True and res[0].has_backlink is True
    assert res[0].message == "有反链(dofollow · 页脚友链)"
    assert res[1].reachable is True and res[1].has_backlink is False
    assert too_large_resp.chunks_yielded == 2  # stopped right at 2MB

    assert len(seen_headers) == 3  # primary + homepage fallback + huge
    for h in seen_headers:
        assert "Chrome/131.0.0.0" in h["User-Agent"]
        assert h["Accept"] == "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        assert h["Accept-Language"] == "zh-CN,zh;q=0.9,en;q=0.8"


def test_p0_batch_add_skip_existing_true_vs_false() -> None:
    store = InMemoryRestStore()
    site_a = _make_site("站A", "https://a.example.com", site_id="sa")
    store.seed(site_a.url, [{"id": 1, "title": "旧链", "link": "https://partner.example.com/"}])

    svc = FriendLinkService(
        IntegrationSettings(friendlink=FriendLinkSettings(per_request_delay_s=0.0)),
        session_factory=store.session_factory,
        sleep=lambda _: None,
    )
    specs = [
        FriendLinkSpec(title="重复链", link="http://www.partner.example.com"),
        FriendLinkSpec(title="自己", link="https://a.example.com/"),
        FriendLinkSpec(title="全新链", link="https://new.example.com/"),
    ]

    # Default skip_existing=True skips existing locally without calling create
    res_skip = svc.batch_add([site_a], specs)
    assert [r.status for r in res_skip] == [LINK_SKIPPED_EXISTS, LINK_SKIPPED_SELF, LINK_CREATED]
    assert store.list_counts[site_a.url] == 1
    assert store.create_counts[site_a.url] == 1

    # skip_existing=False attempts create even when already present; still skips self
    res_force = svc.batch_add([site_a], specs[:2], skip_existing=False)
    assert [r.status for r in res_force] == [LINK_CREATED, LINK_SKIPPED_SELF]
    assert store.list_counts[site_a.url] == 1  # did not needlessly call /list
    assert store.create_counts[site_a.url] == 2

    # When server itself rejects duplicate with "友链已存在", still maps cleanly to skipped_exists
    store.reject_duplicate_on_create = True
    res_server_dup = svc.batch_add([site_a], [specs[0]], skip_existing=False)
    assert res_server_dup[0].status == LINK_SKIPPED_EXISTS
    assert store.create_counts[site_a.url] == 3


def test_p0_mcp_list_links_parses_text_json_and_raises_on_unparseable() -> None:
    class TextMcpClient:
        def __init__(self, result: McpToolResult) -> None:
            self._result = result
            self.closed = False
            self.tools = [McpToolInfo(name="friendlink_list", description="列出友链")]

        def initialize(self) -> dict:
            return {}

        def list_tools(self, *, refresh: bool = False) -> list[McpToolInfo]:
            return list(self.tools)

        def find_tools(self, keywords, *, explicit_names=()) -> list[McpToolInfo]:
            return score_tools(self.tools, keywords, explicit_names)

        def call_tool(self, name: str, arguments=None, *, timeout=None) -> McpToolResult:
            return self._result

        def close(self) -> None:
            self.closed = True

    def _run_with_result(result: McpToolResult) -> list[FriendLink]:
        clients: list[TextMcpClient] = []

        def _factory(_site):
            c = TextMcpClient(result)
            clients.append(c)
            return c

        site = _make_site("站A", "https://a.example.com", token="", mcp_token="mcp-secret-tok")
        svc = FriendLinkService(
            session_factory=lambda: RecordingSession(lambda *_: FakeResponse(404)),
            mcp_client_factory=_factory,
        )
        try:
            return svc.list_links(site)
        finally:
            assert all(c.closed for c in clients)

    # 1. Direct JSON object in text
    links1 = _run_with_result(
        McpToolResult(
            is_error=False,
            text='{"list": [{"id": 10, "title": "直出JSON", "link": "https://d.example.com/"}]}',
            structured=None,
        )
    )
    assert len(links1) == 1 and links1[0].title == "直出JSON"

    # 2. Fenced ```json ... ``` block inside prose
    links2 = _run_with_result(
        McpToolResult(
            is_error=False,
            text='查询结果如下：\n```json\n{"items": [{"id": 11, "title": "围栏JSON", "link": "https://f.example.com/"}]}\n```\n完成',
            structured=None,
        )
    )
    assert len(links2) == 1 and links2[0].title == "围栏JSON"

    # 3. Fenced ``` ... ``` bare array
    links3 = _run_with_result(
        McpToolResult(
            is_error=False,
            text='```\n[{"id": 12, "title": "裸数组", "link": "https://arr.example.com/"}]\n```',
            structured=None,
        )
    )
    assert len(links3) == 1 and links3[0].title == "裸数组"

    # 4. Embedded nested data object amidst noise brackets
    links4 = _run_with_result(
        McpToolResult(
            is_error=False,
            text='[INFO] payload={"code": 0, "data": {"list": [{"id": 13, "title": "嵌套", "link": "https://n.example.com/"}]}} ok',
            structured=None,
        )
    )
    assert len(links4) == 1 and links4[0].title == "嵌套"

    # 5. Embedded JSON array substring
    links5 = _run_with_result(
        McpToolResult(
            is_error=False,
            text='links: [{"id": 14, "title": "子串数组", "link": "https://s.example.com/"}] done',
            structured=None,
        )
    )
    assert len(links5) == 1 and links5[0].title == "子串数组"

    # 6. Empty JSON list still returns []
    assert _run_with_result(McpToolResult(is_error=False, text='{"list": []}', structured=None)) == []

    # 7. Unparseable text MUST raise FriendLinkError instead of returning []
    for bad_text in ("", "   ", "数据库连接异常，返回非JSON文本", "```json\n{bad json}\n```", "123"):
        with pytest.raises(FriendLinkError, match="MCP 返回的友链数据无法解析"):
            _run_with_result(McpToolResult(is_error=False, text=bad_text, structured=None))

    # 8. is_error=True redacts secret token and raises FriendLinkError
    with pytest.raises(FriendLinkError) as exc_err:
        _run_with_result(
            McpToolResult(is_error=True, text="failed with token=mcp-secret-tok", structured=None)
        )
    assert "mcp-secret-tok" not in str(exc_err.value)


def test_p0_check_backlinks_concurrent_preserves_input_order() -> None:
    site = _make_site("主站", "https://mysite.example.com")
    lock = threading.Lock()
    active = 0
    max_active = 0
    sessions: list[RecordingSession] = []

    def _handler(_method: str, url: str, _kwargs: dict[str, Any]) -> FakeResponse:
        nonlocal active, max_active
        with lock:
            active += 1
            if active > max_active:
                max_active = active
        try:
            # Earlier links sleep longer so threads finish out of order
            idx = int(url.split("partner")[1].split(".")[0])
            time.sleep(0.025 if idx < 4 else 0.005)
            return FakeResponse(200, text=f'<a href="https://mysite.example.com/">站{idx}</a>')
        finally:
            with lock:
                active -= 1

    def _session_factory() -> RecordingSession:
        sess = RecordingSession(_handler)
        with lock:
            sessions.append(sess)
        return sess

    svc = FriendLinkService(session_factory=_session_factory)
    links = [FriendLink(id=i, title=f"P{i}", link=f"https://partner{i}.example.com/") for i in range(12)]
    results = svc.check_backlinks(site, links, max_workers=4)

    assert len(results) == 12
    assert [r.link for r in results] == [f"https://partner{i}.example.com/" for i in range(12)]
    assert [r.message for r in results] == [f"有反链(dofollow · 站{i})" for i in range(12)]
    assert 1 < max_active <= 4
    assert all(s.closed for s in sessions)


# --------------------------------------------------------------------------- #
# P1: batch_check_backlinks & search_links_across_sites
# --------------------------------------------------------------------------- #
def test_p1_batch_check_backlinks_classifications_whitelist_errors_and_progress() -> None:
    site_a = _make_site("站A", "https://sitea.example.com", site_id="sa", token="tok-A-9999")
    site_b = _make_site("站B", "https://siteb.example.com", site_id="sb", token="tok-B-9999")
    site_bad = _make_site("坏站", "https://bad.example.com", site_id="sbad", token="tok-BAD-9999")
    site_empty = _make_site("空配置", "", token="", site_id="sempty")

    live_links = {
        "https://sitea.example.com": [
            {"id": 1, "title": "正常友链", "link": "https://ok.partner.com/"},
            {"id": 2, "title": "Anchor含nofollow字样但是df", "link": "https://tricky-ok.partner.com/"},
            {"id": 3, "title": "NF友链", "link": "https://nf.partner.com/"},
            {"id": 4, "title": "百度大站", "link": "https://tieba.baidu.com/"},
        ],
        "https://siteb.example.com": [
            {"id": 5, "title": "撤链站", "link": "https://missing.partner.com/"},
            {"id": 6, "title": "宕机站", "link": "https://down.partner.com/"},
            {"id": 7, "title": "知乎", "link": "https://www.zhihu.com/links"},
        ],
    }

    fetched_external: list[str] = []
    lock = threading.Lock()

    def _handler(_method: str, url: str, _kwargs: dict[str, Any]) -> FakeResponse:
        if url.endswith("/api/friendlink/list"):
            base = url.split("/api/friendlink/")[0]
            if base == "https://bad.example.com":
                return FakeResponse(401)
            return FakeResponse(200, json_data={"code": 0, "data": {"list": live_links[base]}})

        with lock:
            fetched_external.append(url)
        if url == "https://ok.partner.com/":
            return FakeResponse(200, text='<a href="https://sitea.example.com/">站A官网</a>')
        if url == "https://tricky-ok.partner.com/":
            return FakeResponse(200, text='<a href="https://sitea.example.com/">nofollow优化指南</a>')
        if url == "https://nf.partner.com/":
            return FakeResponse(200, text='<a href="https://sitea.example.com/" rel="nofollow">站A</a>')
        if url == "https://missing.partner.com/":
            return FakeResponse(200, text='<a href="https://unrelated-domain.org/">他人</a>')
        if url == "https://down.partner.com/":
            return FakeResponse(503)
        raise AssertionError(f"Unexpected fetch to {url}")

    progress_items: list[dict[str, Any]] = []
    svc = FriendLinkService(session_factory=lambda: RecordingSession(_handler))
    audit = svc.batch_check_backlinks(
        [site_a, site_b, site_bad, site_empty],
        max_site_workers=3,
        max_link_workers=2,
        whitelist_domains=["baidu.com", "https://zhihu.com/"],
        progress=progress_items.append,
    )

    # Whitelisted domains MUST NOT be fetched over HTTP
    assert not any("baidu.com" in u or "zhihu.com" in u for u in fetched_external)

    assert audit["summary"] == {
        "total": 7,
        "ok": 2,
        "nofollow": 1,
        "missing": 1,
        "unreachable": 1,
        "whitelisted": 2,
    }
    assert [(i["site_name"], i["link"], i["status"], i["issue_kind"]) for i in audit["items"]] == [
        ("站A", "https://ok.partner.com/", "ok", "ok"),
        ("站A", "https://tricky-ok.partner.com/", "ok", "ok"),
        ("站A", "https://nf.partner.com/", "nofollow", "nofollow"),
        ("站A", "https://tieba.baidu.com/", "whitelisted", "whitelisted"),
        ("站B", "https://missing.partner.com/", "missing", "missing"),
        ("站B", "https://down.partner.com/", "unreachable", "unreachable"),
        ("站B", "https://www.zhihu.com/links", "whitelisted", "whitelisted"),
    ]
    assert set(audit["site_errors"].keys()) == {"坏站", "空配置"}
    assert "tok-BAD-9999" not in audit["site_errors"]["坏站"]
    assert len(progress_items) == 7

    # Empty sites -> empty structured response
    empty_audit = svc.batch_check_backlinks([])
    assert empty_audit == {
        "items": [],
        "site_errors": {},
        "summary": {"total": 0, "ok": 0, "nofollow": 0, "missing": 0, "unreachable": 0, "whitelisted": 0},
    }


def test_p1_search_links_across_sites_filters_root_domain_substring_and_errors() -> None:
    store = InMemoryRestStore()
    site_a = _make_site("站A", "https://a.example.com", site_id="sa")
    site_b = _make_site("站B", "https://b.example.com", site_id="sb")
    site_c = _make_site("站C", "https://c.example.com", site_id="sc")
    site_d = _make_site("站D", "", token="", site_id="sd")

    store.seed(
        site_a.url,
        [
            {"id": 1, "title": "深圳搬家主站", "link": "https://www.banjia.com/", "remark": "核心交换"},
            {"id": 2, "title": "无关相似域", "link": "https://notbanjia.com/", "remark": "普通"},
        ],
    )
    store.seed(
        site_b.url,
        [
            {"id": 3, "title": "搬家子站", "link": "http://sub.banjia.com/links", "remark": "SEO_VIP"},
            {"id": 4, "title": "装修网", "link": "https://zhuangxiu.cn/", "remark": "Banjia合作"},
        ],
    )
    store.fail_sites[site_c.url] = FakeResponse(403)
    store.site_delays[site_a.url] = 0.02
    store.site_delays[site_b.url] = 0.02

    svc = FriendLinkService(session_factory=store.session_factory)

    # 1. Empty query -> returns all links
    progress_events: list[dict[str, Any]] = []
    all_res = svc.search_links_across_sites(
        [site_a, site_b, site_c, site_d],
        "",
        max_workers=4,
        progress=progress_events.append,
    )
    assert store.max_active_workers >= 2
    assert len(all_res["matches"]) == 4
    assert all_res["site_counts"] == {"站A": 2, "站B": 2}
    assert all_res["total_links"] == 4
    assert set(all_res["site_errors"].keys()) == {"站C", "站D"}
    assert len(progress_events) == 4

    # 2. Root domain match: query "https://other.banjia.com/about" matches www.banjia.com & sub.banjia.com
    root_res = svc.search_links_across_sites(
        [site_a, site_b],
        "https://other.banjia.com/about",
        match_root_domain=True,
    )
    assert [(m["site_name"], m["link"]) for m in root_res["matches"]] == [
        ("站A", "https://www.banjia.com/"),
        ("站B", "http://sub.banjia.com/links"),
    ]

    # 3. With match_root_domain=False, full URL doesn't match different subdomain
    strict_res = svc.search_links_across_sites(
        [site_a, site_b],
        "https://other.banjia.com/about",
        match_root_domain=False,
    )
    assert strict_res["matches"] == []

    # 4. Case-insensitive substring across link / title / remark
    kw_res = svc.search_links_across_sites([site_a, site_b], "banjia", match_root_domain=False)
    assert [m["id"] for m in kw_res["matches"]] == [1, 2, 3, 4]

    remark_res = svc.search_links_across_sites([site_a, site_b], "seo_vip")
    assert [m["id"] for m in remark_res["matches"]] == [3]


# --------------------------------------------------------------------------- #
# P1: SEO-Safe Cross-Linking, Anchor Pool, HTML Export & Preview Cache Reuse
# --------------------------------------------------------------------------- #
def test_p1_crosslink_random_k_modes_seed_reciprocal_and_root_domain() -> None:
    sites = [
        _make_site("S1", "https://s1.alpha.com", site_id="s1", site_title="Alpha1"),
        _make_site("S2", "https://s2.alpha.com", site_id="s2", site_title="Alpha2"),  # same root as S1
        _make_site("S3", "https://beta.com", site_id="s3", site_title="Beta"),
        _make_site("S4", "https://gamma.com", site_id="s4", site_title="Gamma"),
        _make_site("S5", "https://delta.com", site_id="s5", site_title="Delta"),
        _make_site("S6", "https://epsilon.com", site_id="s6", site_title="Epsilon"),
        _make_site("S7", "https://zeta.com", site_id="s7", site_title="Zeta"),
    ]

    # Reproducibility with seed + avoid_reciprocal=True + avoid_same_root_domain=True
    p1 = plan_crosslink_items(sites, CROSSLINK_RANDOM_K, k=2, seed=42, avoid_reciprocal=True, avoid_same_root_domain=True)
    p2 = plan_crosslink_items(sites, CROSSLINK_RANDOM_K, k=2, seed=42, avoid_reciprocal=True, avoid_same_root_domain=True)
    p3 = plan_crosslink_items(sites, CROSSLINK_RANDOM_K, k=2, seed=99, avoid_reciprocal=True, avoid_same_root_domain=True)

    pairs1 = [(i.source_site_id, i.target_site_id) for i in p1]
    pairs2 = [(i.source_site_id, i.target_site_id) for i in p2]
    pairs3 = [(i.source_site_id, i.target_site_id) for i in p3]
    assert pairs1 == pairs2
    assert pairs1 != pairs3

    pair_set = set(pairs1)
    assert ("s1", "s2") not in pair_set and ("s2", "s1") not in pair_set  # same root skipped
    for src_id, dst_id in pair_set:
        assert (dst_id, src_id) not in pair_set  # no reciprocals

    out_counts: dict[str, int] = {}
    for src_id, _ in pairs1:
        out_counts[src_id] = out_counts.get(src_id, 0) + 1
    assert all(c <= 2 for c in out_counts.values())

    # Default k=3, allow reciprocal & allow same root
    svc = FriendLinkService()
    plan_allow = svc.plan_crosslinks(
        sites[:4],
        CROSSLINK_RANDOM_K,
        seed=7,
        avoid_reciprocal=False,
        avoid_same_root_domain=False,
    )
    assert plan_allow.mode == CROSSLINK_RANDOM_K
    assert len(plan_allow.items) == 12  # 4 sites * default k=3

    with pytest.raises(ValueError, match="k"):
        plan_crosslink_items(sites, CROSSLINK_RANDOM_K, k=0)


def test_p1_crosslink_group_cross_explicit_and_auto_groups() -> None:
    sites = [
        _make_site("A1", "https://a1.com", site_id="a1", site_group="华南"),
        _make_site("A2", "https://a2.com", site_id="a2", site_group="华南"),
        _make_site("B1", "https://b1.com", site_id="b1", site_group="华东"),
        _make_site("B2", "https://b2.com", site_id="b2", site_group="华东"),
        _make_site("B3", "https://a1-sub.a1.com", site_id="b3", site_group="华东"),  # same root as A1!
        _make_site("C1", "https://c1.com", site_id="c1", site_group="华北"),
    ]

    # Explicit group_a & group_b, unidirectional, avoid_same_root_domain=True
    uni = plan_crosslink_items(
        sites,
        CROSSLINK_GROUP_CROSS,
        group_a="华南",
        group_b="华东",
        bidirectional=False,
        avoid_same_root_domain=True,
    )
    uni_pairs = {(i.source_site_id, i.target_site_id) for i in uni}
    assert ("a1", "b3") not in uni_pairs  # same root a1.com skipped
    assert uni_pairs == {
        ("a1", "b1"),
        ("a1", "b2"),
        ("a2", "b1"),
        ("a2", "b2"),
        ("a2", "b3"),
    }

    # Explicit group_a & group_b, bidirectional=True, k=1 cap per site
    bi_capped = plan_crosslink_items(
        sites,
        CROSSLINK_GROUP_CROSS,
        group_a="华南",
        group_b="华东",
        bidirectional=True,
        k=1,
        seed=5,
    )
    out_per_src: dict[str, int] = {}
    for item in bi_capped:
        out_per_src[item.source_site_id] = out_per_src.get(item.source_site_id, 0) + 1
    assert out_per_src == {"a1": 1, "a2": 1, "b1": 1, "b2": 1, "b3": 1}

    # Auto cross-group pairing (group_a & group_b omitted) across all 3 site_groups
    auto_uni = plan_crosslink_items(sites, CROSSLINK_GROUP_CROSS, bidirectional=False)
    for item in auto_uni:
        assert item.source_site_id != item.target_site_id
        # Unidirectional across groups means reverse edge never appears
        assert not any(
            rev.source_site_id == item.target_site_id and rev.target_site_id == item.source_site_id
            for rev in auto_uni
        )

    # Fewer than 2 distinct site_groups raises ValueError
    no_groups = [
        _make_site("X1", "https://x1.com", site_group="同组"),
        _make_site("X2", "https://x2.com", site_group="同组"),
    ]
    with pytest.raises(ValueError, match="分组"):
        plan_crosslink_items(no_groups, CROSSLINK_GROUP_CROSS)


def test_p1_anchor_text_rotation_pool_and_export_html_links() -> None:
    target = _make_site(
        "深圳总站",
        "https://sz.example.com",
        site_id="target",
        site_title="深圳搬家|搬家公司|南山搬家",
    )
    sources = [
        _make_site(f"分站{i}", f"https://s{i}.example.com", site_id=f"s{i}", site_title=f"分站{i}主词|副词{i}")
        for i in range(30)
    ]

    # Hub unidirectional: all 30 sources link to target
    items_1 = plan_crosslink_items([target, *sources], "hub", hub_site_id="target", bidirectional=False)
    items_2 = plan_crosslink_items([target, *sources], "hub", hub_site_id="target", bidirectional=False)

    anchors_1 = [i.spec.title for i in items_1]
    anchors_2 = [i.spec.title for i in items_2]
    assert anchors_1 == anchors_2  # deterministic rotation
    assert set(anchors_1) == {"深圳搬家", "搬家公司", "南山搬家"}  # all variants rotated across 30 sites
    assert all(i.spec.my_title == f"分站{idx}主词" for idx, i in enumerate(items_1))

    # export_sites_html_links picks first anchor variant, supports separator & include_nofollow, escapes HTML
    s_amp = _make_site("Amp站", "https://amp.example.com", site_title="A & B 搬家|备选词")
    s_no_url = _make_site("无URL", "")
    html_out = export_sites_html_links([target, s_amp, s_no_url])
    assert html_out == (
        '<a href="https://sz.example.com/" target="_blank">深圳搬家</a>\n'
        '<a href="https://amp.example.com/" target="_blank">A &amp; B 搬家</a>'
    )

    html_nf = export_sites_html_links([target, s_amp], separator=" | ", include_nofollow=True)
    assert html_nf == (
        '<a href="https://sz.example.com/" target="_blank" rel="nofollow">深圳搬家</a> | '
        '<a href="https://amp.example.com/" target="_blank" rel="nofollow">A &amp; B 搬家</a>'
    )
    assert export_sites_html_links([]) == ""


def test_p1_execute_plan_reuses_preview_cache_without_refetching_list(tmp_path: Path) -> None:
    store = InMemoryRestStore()
    site_a = _make_site("站A", "https://a.example.com", site_id="s1", site_title="站点A")
    site_b = _make_site("站B", "https://b.example.com", site_id="s2", site_title="站点B")
    site_c = _make_site("站C", "https://c.example.com", site_id="s3", site_title="站点C")
    sites = [site_a, site_b, site_c]

    # Site A already has link to B; Site B & C have 0 links
    store.seed(site_a.url, [{"id": 1, "title": "B", "link": "https://b.example.com/"}])

    ledger = FriendLinkLedger(tmp_path / "设置" / "friendlink_ledger.json")
    svc = FriendLinkService(
        IntegrationSettings(friendlink=FriendLinkSettings(max_concurrency=3, per_request_delay_s=0.0)),
        session_factory=store.session_factory,
        sleep=lambda _: None,
        now=lambda: datetime(2026, 11, 25, 12, 0, 0),
    )

    plan = svc.plan_crosslinks(sites, CROSSLINK_MESH)
    cached_existing: dict[str, list[FriendLink]] = {}
    preview = svc.preview_plan(plan, sites, existing_out=cached_existing)
    assert set(cached_existing.keys()) == {"s1", "s2", "s3"}
    assert cached_existing["s2"] == []  # empty list still cached
    assert store.list_counts == {
        "https://a.example.com": 1,
        "https://b.example.com": 1,
        "https://c.example.com": 1,
    }

    # Executing with existing_by_site=cached_existing MUST NOT call /api/friendlink/list again!
    report = svc.execute_plan(preview, sites, ledger=ledger, existing_by_site=cached_existing)
    assert store.list_counts == {
        "https://a.example.com": 1,
        "https://b.example.com": 1,
        "https://c.example.com": 1,
    }
    assert report.plan.counts[LINK_CREATED] == 5
    assert report.plan.counts[LINK_SKIPPED_EXISTS] == 1

    # Partial cache (only s1 provided) only skips /list for s1 and fetches s2, s3
    plan2 = svc.plan_crosslinks(sites, CROSSLINK_MESH)
    svc.execute_plan(plan2, sites, existing_by_site={"s1": cached_existing["s1"]})
    assert store.list_counts == {
        "https://a.example.com": 1,
        "https://b.example.com": 2,
        "https://c.example.com": 2,
    }


def test_p1_batch_check_backlinks_concurrency_and_cancellation() -> None:
    sites = [
        _make_site(f"站{i}", f"https://site{i}.example.com", site_id=f"s{i}")
        for i in range(4)
    ]
    lock = threading.Lock()
    active = 0
    max_active = 0
    cancel = threading.Event()

    def _handler(_method: str, url: str, _kwargs: dict[str, Any]) -> FakeResponse:
        nonlocal active, max_active
        if url.endswith("/api/friendlink/list"):
            base = url.split("/api/friendlink/")[0]
            return FakeResponse(
                200,
                json_data={
                    "code": 0,
                    "data": {
                        "list": [
                            {"id": 1, "title": "P1", "link": f"https://p1.{base.split('//')[1]}/"},
                            {"id": 2, "title": "P2", "link": f"https://p2.{base.split('//')[1]}/"},
                        ]
                    },
                },
            )
        with lock:
            active += 1
            if active > max_active:
                max_active = active
        try:
            time.sleep(0.02)
            return FakeResponse(200, text='<a href="https://site0.example.com/">OK</a>')
        finally:
            with lock:
                active -= 1

    svc = FriendLinkService(session_factory=lambda: RecordingSession(_handler))
    audit = svc.batch_check_backlinks(sites, max_site_workers=4, max_link_workers=2)
    assert audit["summary"]["total"] == 8
    assert max_active >= 2

    # Pre-set cancel_event yields empty items with zero external fetches
    cancel.set()
    cancelled_audit = svc.batch_check_backlinks(sites, cancel_event=cancel)
    assert cancelled_audit["items"] == []
    assert cancelled_audit["site_errors"] == {}


def test_p1_crosslink_new_modes_execute_and_rollback(tmp_path: Path) -> None:
    store = InMemoryRestStore()
    sites = [
        _make_site("A1", "https://a1.com", site_id="a1", site_group="A"),
        _make_site("A2", "https://a2.com", site_id="a2", site_group="A"),
        _make_site("B1", "https://b1.com", site_id="b1", site_group="B"),
        _make_site("B2", "https://b2.com", site_id="b2", site_group="B"),
    ]
    ledger = FriendLinkLedger(tmp_path / "设置" / "friendlink_ledger.json")
    svc = FriendLinkService(
        IntegrationSettings(friendlink=FriendLinkSettings(max_concurrency=2, per_request_delay_s=0.0)),
        session_factory=store.session_factory,
        sleep=lambda _: None,
        now=lambda: datetime(2026, 11, 25, 13, 0, 0),
    )

    plan = svc.plan_crosslinks(sites, CROSSLINK_GROUP_CROSS, bidirectional=False)
    cache: dict[str, list[FriendLink]] = {}
    preview = svc.preview_plan(plan, sites, existing_out=cache)
    assert len(preview.items) == 4
    report = svc.execute_plan(preview, sites, ledger=ledger, existing_by_site=cache)
    assert report.plan.counts[LINK_CREATED] == 4
    assert all(count == 1 for count in store.list_counts.values())

    rb = svc.rollback_plan(plan.plan_id, sites, ledger=ledger)
    assert len(rb) == 4
    assert all(r.status == LINK_DELETED for r in rb)
    assert all(store.links.get(s.url, []) == [] for s in sites)

