"""Unit tests for core/friendlink_service.py (zero real network)."""

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
    AUTH_HINT,
    CHECK_UNSUPPORTED_HINT,
    REDIRECT_HINT,
    FriendLinkError,
    FriendLinkService,
    link_key,
    normalize_link_url,
    parse_batch_link_input,
    plan_crosslink_items,
)
from core.integration_settings import FriendLinkSettings, IntegrationSettings, McpSettings
from core.mcp_contracts import SSL_HINT, score_tools
from models.entities import SiteConfig
from models.sitegroup import (
    CROSSLINK_HUB,
    CROSSLINK_MESH,
    CROSSLINK_RING,
    LINK_CANCELLED,
    LINK_CREATED,
    LINK_DELETED,
    LINK_FAILED,
    LINK_NOT_FOUND,
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
    token: str = "tok-secret-12345",
    site_id: str = "",
    site_title: str = "",
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
        json_error: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self._json_error = json_error
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")
        self._chunks = chunks
        self.closed = False

    def json(self) -> Any:
        if self._json_error is not None:
            raise self._json_error
        return self._json_data

    def iter_content(self, chunk_size: int = 16384):
        if self._chunks is not None:
            yield from self._chunks
        else:
            data = self.content
            for start in range(0, len(data), chunk_size):
                yield data[start : start + chunk_size]

    def close(self) -> None:
        self.closed = True


class RecordingSession:
    def __init__(self, handler) -> None:
        self._handler = handler
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        call = {"method": "GET", "url": url, **kwargs}
        self.calls.append(call)
        return self._handler("GET", url, kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        call = {"method": "POST", "url": url, **kwargs}
        self.calls.append(call)
        return self._handler("POST", url, kwargs)

    def close(self) -> None:
        self.closed = True


class InMemorySiteStore:
    """Thread-safe fake AnqiCMS friendlink REST backend keyed by site base URL."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.links: dict[str, list[dict[str, Any]]] = {}
        self.next_id = 1
        self.calls: list[dict[str, Any]] = []
        self.sessions: list[RecordingSession] = []
        self.fail_sites: dict[str, Any] = {}
        self.list_counts: dict[str, int] = {}
        self.site_delays: dict[str, float] = {}
        self.active_workers = 0
        self.max_active_workers = 0
        self.on_create_hook = None

    def seed(self, base_url: str, items: list[dict[str, Any]]) -> None:
        self.links[base_url.rstrip("/")] = [dict(i) for i in items]

    def session_factory(self) -> RecordingSession:
        session = RecordingSession(self._handle)
        with self.lock:
            self.sessions.append(session)
        return session

    def _base_of(self, url: str) -> str:
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
                    return FakeResponse(200, json_data={"code": 0, "data": {"list": items, "total": len(items)}})

                if url.endswith("/api/friendlink/create"):
                    files = kwargs.get("files") or {}
                    entry = {
                        "id": self.next_id,
                        "title": files["title"][1],
                        "link": files["link"][1],
                        "nofollow": int(files["nofollow"][1]),
                        "back_link": files["back_link"][1],
                        "my_title": files["my_title"][1],
                        "my_link": files["my_link"][1],
                        "contact": files["contact"][1],
                        "remark": files["remark"][1],
                    }
                    self.next_id += 1
                    self.links.setdefault(base, []).append(entry)
                    if self.on_create_hook is not None:
                        self.on_create_hook(base, entry)
                    return FakeResponse(200, json_data={"code": 0, "msg": "ok"})

                if url.endswith("/api/friendlink/delete"):
                    files = kwargs.get("files") or {}
                    target = files["link"][1]
                    current = self.links.get(base, [])
                    self.links[base] = [item for item in current if item.get("link") != target]
                    return FakeResponse(200, json_data={"code": 0, "msg": "deleted"})

                if url.endswith("/api/friendlink/check"):
                    return FakeResponse(200, json_data={"code": 0, "msg": "ok"})

                return FakeResponse(404, json_data={"code": 404, "msg": "not found"})
        finally:
            with self.lock:
                self.active_workers -= 1


# --------------------------------------------------------------------------- #
# URL normalization, link_key & batch input parsing
# --------------------------------------------------------------------------- #
def test_normalize_link_url_and_link_key() -> None:
    assert normalize_link_url("https://Example.COM:443") == "https://example.com/"
    assert normalize_link_url("http://WWW.Example.com:80/blog/") == "http://www.example.com/blog/"
    assert normalize_link_url("https://example.com:8443/a") == "https://example.com:8443/a"
    assert normalize_link_url("http://127.0.0.1:8001/test") == "http://127.0.0.1:8001/test"

    assert link_key("https://www.Example.COM/") == "example.com"
    assert link_key("http://example.com:80") == "example.com"
    assert link_key("https://example.com:443/about/") == "example.com/about"
    assert link_key("http://www.example.com/about") == "example.com/about"
    assert link_key("https://example.com:8080/x/") == "example.com:8080/x"
    assert link_key("not a url") == ""

    for bad in ("", "ftp://a.com", "example.com", "http://bad host.com", "https://user:pass@a.com", "http://nodot"):
        with pytest.raises(ValueError):
            normalize_link_url(bad)


def test_parse_batch_link_input_all_formats_dedupe_and_errors() -> None:
    raw = """
    # 注释行应当忽略

    管道站点|https://a.example.com|1|互链备注
    https://www.a.example.com/|重复应当丢弃
    https://b.example.com|反序管道|0|普通
    制表站点\thttps://c.example.com/path/
    逗号站点,https://d.example.com,nofollow,合作
    https://e.example.com 空格标题 含空格
    https://f.example.com
    无效行没有网址
    标题|ftp://bad.example.com
    """
    specs, errors = parse_batch_link_input(raw, default_nofollow=False)

    assert len(specs) == 6
    assert specs[0] == FriendLinkSpec(title="管道站点", link="https://a.example.com/", nofollow=True, remark="互链备注")
    assert specs[1] == FriendLinkSpec(title="反序管道", link="https://b.example.com/", nofollow=False, remark="普通")
    assert specs[2] == FriendLinkSpec(title="制表站点", link="https://c.example.com/path/", nofollow=False, remark="")
    assert specs[3] == FriendLinkSpec(title="逗号站点", link="https://d.example.com/", nofollow=True, remark="合作")
    assert specs[4] == FriendLinkSpec(title="空格标题 含空格", link="https://e.example.com/", nofollow=False, remark="")
    assert specs[5] == FriendLinkSpec(title="f.example.com", link="https://f.example.com/", nofollow=False, remark="")

    assert len(errors) == 2
    assert errors[0].startswith("第11行：网址无效")
    assert errors[1].startswith("第12行：网址无效")


def test_parse_batch_link_input_respects_default_nofollow() -> None:
    specs, errors = parse_batch_link_input("https://a.example.com\n站B|https://b.example.com|0", default_nofollow=True)
    assert errors == []
    assert specs[0].nofollow is True
    assert specs[1].nofollow is False


# --------------------------------------------------------------------------- #
# Single-site REST operations, multipart/params/no-redirect, errors & redaction
# --------------------------------------------------------------------------- #
def test_single_site_list_add_remove_and_multipart_contract() -> None:
    store = InMemorySiteStore()
    site_a = _make_site("站A", "https://a.example.com", token="tok-AAA-99999", site_title="站点A")
    store.seed(
        site_a.url,
        [{"id": 7, "title": "旧链", "link": "https://old.example.com/", "nofollow": 1, "remark": "r1"}],
    )

    service = FriendLinkService(
        IntegrationSettings(mcp=McpSettings(verify_ssl=False)),
        session_factory=store.session_factory,
        sleep=lambda _: None,
    )

    links = service.list_links(site_a)
    assert len(links) == 1
    assert links[0].id == 7
    assert links[0].nofollow is True
    list_call = store.calls[-1]
    assert list_call["method"] == "GET"
    assert list_call["url"] == "https://a.example.com/api/friendlink/list"
    assert list_call["params"] == {"token": "tok-AAA-99999"}
    assert list_call["allow_redirects"] is False
    assert list_call["verify"] is False

    # Self-skip (including same host with different path)
    self_res = service.add_link(
        site_a,
        FriendLinkSpec(title="自己", link="http://www.a.example.com/subpath"),
        existing=links,
    )
    assert self_res.status == LINK_SKIPPED_SELF

    # Existing-skip (different scheme + trailing slash)
    exists_res = service.add_link(
        site_a,
        FriendLinkSpec(title="旧链", link="http://www.old.example.com"),
        existing=links,
    )
    assert exists_res.status == LINK_SKIPPED_EXISTS

    # Successful add verifies multipart fields + token param + allow_redirects=False
    spec = FriendLinkSpec(
        title="新站",
        link="https://new.example.com",
        nofollow=True,
        back_link="https://new.example.com/links",
        my_title="站点A",
        my_link="https://a.example.com/",
        contact="qq:123",
        remark="测试",
    )
    add_res = service.add_link(site_a, spec, existing=links)
    assert add_res.status == LINK_CREATED
    create_call = store.calls[-1]
    assert create_call["method"] == "POST"
    assert create_call["url"] == "https://a.example.com/api/friendlink/create"
    assert create_call["params"] == {"token": "tok-AAA-99999"}
    assert create_call["allow_redirects"] is False
    assert create_call["files"] == {
        "title": (None, "新站"),
        "link": (None, "https://new.example.com/"),
        "nofollow": (None, "1"),
        "back_link": (None, "https://new.example.com/links"),
        "my_title": (None, "站点A"),
        "my_link": (None, "https://a.example.com/"),
        "contact": (None, "qq:123"),
        "remark": (None, "测试"),
    }

    # Remove absent link -> not_found (no HTTP delete sent)
    calls_before = len(store.calls)
    nf_res = service.remove_link(site_a, "https://missing.example.com/", existing=links)
    assert nf_res.status == LINK_NOT_FOUND
    assert len(store.calls) == calls_before

    # Remove duplicate server links -> deletes all variants & reports count
    dup_existing = [
        FriendLink(id=1, title="重复站", link="http://dup.example.com"),
        FriendLink(id=2, title="重复站", link="https://www.dup.example.com/"),
    ]
    del_res = service.remove_link(site_a, "https://dup.example.com/", existing=dup_existing)
    assert del_res.status == LINK_DELETED
    assert "2" in del_res.message
    assert all(s.closed for s in store.sessions)


def test_list_links_handles_bare_list_and_null_data() -> None:
    site = _make_site("站A", "https://a.example.com")
    responses = iter(
        [
            FakeResponse(200, json_data={"code": 200, "data": [{"id": 1, "title": "X", "link": "https://x.com/"}]}),
            FakeResponse(200, json_data={"code": 0, "data": None}),
        ]
    )
    service = FriendLinkService(session_factory=lambda: RecordingSession(lambda m, u, kw: next(responses)))

    first = service.list_links(site)
    assert len(first) == 1 and first[0].title == "X"
    second = service.list_links(site)
    assert second == []


def test_error_mapping_hints_and_token_redaction() -> None:
    secret = "super-secret-token-xyz"
    site = _make_site("站A", "https://a.example.com", token=secret)

    # 401 -> auth hint
    svc_401 = FriendLinkService(session_factory=lambda: RecordingSession(lambda m, u, kw: FakeResponse(401)))
    with pytest.raises(FriendLinkError) as exc_401:
        svc_401.list_links(site)
    assert exc_401.value.hint == AUTH_HINT
    assert secret not in str(exc_401.value)

    # 302 -> redirect hint
    svc_302 = FriendLinkService(session_factory=lambda: RecordingSession(lambda m, u, kw: FakeResponse(302)))
    with pytest.raises(FriendLinkError) as exc_302:
        svc_302.list_links(site)
    assert exc_302.value.hint == REDIRECT_HINT

    # SSLError -> SSL hint + token scrubbed from error text
    def _raise_ssl(m, u, kw):
        raise requests.exceptions.SSLError(f"cert failed for https://a.example.com?token={secret}")

    svc_ssl = FriendLinkService(session_factory=lambda: RecordingSession(_raise_ssl))
    res = svc_ssl.add_link(site, FriendLinkSpec(title="B", link="https://b.example.com/"))
    assert res.status == LINK_FAILED
    assert SSL_HINT in res.message
    assert secret not in res.message


def test_server_check_404_and_405_fallback() -> None:
    site = _make_site("站A", "https://a.example.com")

    # 404 -> clear hint
    svc_404 = FriendLinkService(session_factory=lambda: RecordingSession(lambda m, u, kw: FakeResponse(404)))
    res_404 = svc_404.server_check(site)
    assert res_404.status == LINK_FAILED
    assert CHECK_UNSUPPORTED_HINT in res_404.message

    # GET 405 -> fallback POST 200
    methods_seen: list[str] = []

    def _handle_405(method: str, url: str, kwargs: dict[str, Any]) -> FakeResponse:
        methods_seen.append(method)
        if method == "GET":
            return FakeResponse(405)
        return FakeResponse(200, json_data={"code": 0, "msg": "检测完成"})

    svc_405 = FriendLinkService(session_factory=lambda: RecordingSession(_handle_405))
    res_405 = svc_405.server_check(site)
    assert methods_seen == ["GET", "POST"]
    assert res_405.status == "checked"
    assert "检测完成" in res_405.message


def test_check_backlinks_no_token_redirects_and_512kb_cap() -> None:
    site = _make_site("站A", "https://www.mysite.example.com", token="secret-tok-999")
    captured_calls: list[dict[str, Any]] = []

    def _handler(method: str, url: str, kwargs: dict[str, Any]) -> FakeResponse:
        captured_calls.append({"url": url, **kwargs})
        if url == "https://partner1.example.com/links.html":
            return FakeResponse(200, text='<a href="https://mysite.example.com/">SiteA</a>')
        if url == "https://partner2.example.com/":
            # Backlink appears ONLY after 600KB -> must NOT be read past 512KB cap
            chunks = [b"x" * (300 * 1024), b"y" * (300 * 1024), b"mysite.example.com"]
            return FakeResponse(200, chunks=chunks)
        return FakeResponse(500)

    svc = FriendLinkService(session_factory=lambda: RecordingSession(_handler))
    links = [
        FriendLink(id=1, title="P1", link="https://partner1.example.com/", back_link="https://partner1.example.com/links.html"),
        FriendLink(id=2, title="P2", link="https://partner2.example.com/"),
        FriendLink(id=3, title="P3", link="https://partner3.example.com/"),
    ]
    health = svc.check_backlinks(site, links)
    assert len(health) == 3
    assert health[0].reachable is True and health[0].has_backlink is True and health[0].http_status == 200
    assert health[1].reachable is True and health[1].has_backlink is False
    assert health[2].reachable is False and health[2].http_status == 500
    # Verify no token passed and redirects allowed
    assert all("params" not in call for call in captured_calls)
    assert all(call["allow_redirects"] is True for call in captured_calls)


def test_mcp_fallback_only_when_rest_unavailable() -> None:
    class FakeMcpClient:
        def __init__(self) -> None:
            self.closed = False
            self.calls: list[tuple[str, dict]] = []
            self.tools = [
                McpToolInfo(name="friendlink_list", description="列出友情链接"),
                McpToolInfo(
                    name="friendlink_create",
                    description="创建友情链接",
                    input_schema={"properties": {"title": {"type": "string"}, "link": {"type": "string"}, "nofollow": {"type": "boolean"}}},
                ),
                McpToolInfo(name="friendlink_delete", description="删除友情链接"),
            ]

        def initialize(self) -> dict:
            return {}

        def list_tools(self, *, refresh: bool = False) -> list[McpToolInfo]:
            return list(self.tools)

        def find_tools(self, keywords, *, explicit_names=()) -> list[McpToolInfo]:
            return score_tools(self.tools, keywords, explicit_names)

        def call_tool(self, name: str, arguments=None, *, timeout=None) -> McpToolResult:
            self.calls.append((name, dict(arguments or {})))
            if name == "friendlink_list":
                return McpToolResult(
                    is_error=False,
                    text="ok",
                    structured={"list": [{"id": 9, "title": "MCP链", "link": "https://mcp.example.com/"}]},
                )
            return McpToolResult(is_error=False, text="ok", structured={"ok": True})

        def close(self) -> None:
            self.closed = True

    created_clients: list[FakeMcpClient] = []

    def _mcp_factory(site) -> FakeMcpClient:
        c = FakeMcpClient()
        created_clients.append(c)
        return c

    site = _make_site("站A", "https://a.example.com", token="tok-11111", mcp_token="mcp-22222")
    svc = FriendLinkService(
        session_factory=lambda: RecordingSession(lambda m, u, kw: FakeResponse(404)),
        mcp_client_factory=_mcp_factory,
    )

    links = svc.list_links(site)
    assert len(links) == 1 and links[0].title == "MCP链"
    add_res = svc.add_link(site, FriendLinkSpec(title="新链", link="https://b.example.com/", nofollow=True))
    assert add_res.status == LINK_CREATED
    del_res = svc.remove_link(site, "https://b.example.com/")
    assert del_res.status == LINK_DELETED
    assert all(c.closed for c in created_clients)


# --------------------------------------------------------------------------- #
# Batch add / remove: concurrency, ordering, delay, cancellation, missing creds
# --------------------------------------------------------------------------- #
def test_batch_add_and_remove_concurrency_ordering_delay_and_missing_creds() -> None:
    store = InMemorySiteStore()
    site_a = _make_site("站A", "https://a.example.com", site_id="sa")
    site_b = _make_site("站B", "https://b.example.com", site_id="sb")
    site_c = _make_site("站C", "", token="", site_id="sc")  # missing url & token

    # Make site A slower so B finishes first; output must still keep [A..., B..., C...] order.
    store.site_delays[site_a.url] = 0.03
    store.seed(site_b.url, [{"id": 1, "title": "已有", "link": "https://ext1.example.com/"}])

    sleeps: list[float] = []
    progress_events = []
    settings = IntegrationSettings(
        friendlink=FriendLinkSettings(max_concurrency=2, per_request_delay_s=0.15)
    )
    svc = FriendLinkService(settings, session_factory=store.session_factory, sleep=sleeps.append)

    specs = [
        FriendLinkSpec(title="外链1", link="https://ext1.example.com/"),
        FriendLinkSpec(title="外链1重复", link="http://www.ext1.example.com"),
        FriendLinkSpec(title="外链2", link="https://ext2.example.com/"),
        FriendLinkSpec(title="自链A", link="https://a.example.com/"),
    ]
    results = svc.batch_add([site_a, site_b, site_c], specs, progress=progress_events.append)

    # 3 deduped specs * 3 sites = 9 results in deterministic site order, then spec order
    assert len(results) == 9
    assert [r.site_id for r in results] == ["sa", "sa", "sa", "sb", "sb", "sb", "sc", "sc", "sc"]
    assert [r.status for r in results[:3]] == [LINK_CREATED, LINK_CREATED, LINK_SKIPPED_SELF]
    assert [r.status for r in results[3:6]] == [LINK_SKIPPED_EXISTS, LINK_CREATED, LINK_CREATED]
    assert [r.status for r in results[6:]] == [LINK_FAILED, LINK_FAILED, LINK_FAILED]
    assert store.list_counts[site_a.url] == 1
    assert store.list_counts[site_b.url] == 1
    assert store.max_active_workers <= 2
    assert len(progress_events) == 9
    assert all(d == 0.15 for d in sleeps) and len(sleeps) >= 2

    # batch_remove
    rm_results = svc.batch_remove(
        [site_a, site_b],
        ["https://ext1.example.com/", "https://never.example.com/"],
    )
    assert [(r.site_id, r.status) for r in rm_results] == [
        ("sa", LINK_DELETED),
        ("sa", LINK_NOT_FOUND),
        ("sb", LINK_DELETED),
        ("sb", LINK_NOT_FOUND),
    ]


def test_batch_add_cancellation() -> None:
    store = InMemorySiteStore()
    site_a = _make_site("站A", "https://a.example.com", site_id="sa")
    cancel = threading.Event()

    def _cancel_after_first(base: str, entry: dict) -> None:
        cancel.set()

    store.on_create_hook = _cancel_after_first
    svc = FriendLinkService(
        IntegrationSettings(friendlink=FriendLinkSettings(max_concurrency=1, per_request_delay_s=0.0)),
        session_factory=store.session_factory,
        sleep=lambda _: None,
    )
    specs = [
        FriendLinkSpec(title="L1", link="https://l1.example.com/"),
        FriendLinkSpec(title="L2", link="https://l2.example.com/"),
        FriendLinkSpec(title="L3", link="https://l3.example.com/"),
    ]
    results = svc.batch_add([site_a], specs, cancel_event=cancel)
    assert [r.status for r in results] == [LINK_CREATED, LINK_CANCELLED, LINK_CANCELLED]


# --------------------------------------------------------------------------- #
# Cross-link topology planning (pure)
# --------------------------------------------------------------------------- #
def test_plan_crosslink_items_mesh_hub_ring_and_validation() -> None:
    sites = [
        _make_site("站A", "https://a.example.com", site_id="s1", site_title="品牌A"),
        _make_site("站B", "https://b.example.com", site_id="s2", site_title="品牌B"),
        _make_site("站C", "https://c.example.com", site_id="s3", site_title="品牌C"),
        _make_site("站D", "https://d.example.com", site_id="s4"),
    ]

    # Mesh: n*(n-1) = 12
    mesh = plan_crosslink_items(sites, CROSSLINK_MESH, plan_id="abcdef123456")
    assert len(mesh) == 12
    assert mesh[0].spec.title == "品牌B"
    assert mesh[0].spec.link == "https://b.example.com/"
    assert mesh[0].spec.my_title == "品牌A"
    assert mesh[0].spec.my_link == "https://a.example.com/"
    assert mesh[0].spec.remark == "站群互链#abcdef12"

    # Hub unidirectional: n-1 = 3 (spokes -> hub)
    hub_uni = plan_crosslink_items(sites, CROSSLINK_HUB, hub_site_id="s1", bidirectional=False)
    assert len(hub_uni) == 3
    assert all(item.target_site_id == "s1" for item in hub_uni)

    # Hub bidirectional: 2*(n-1) = 6
    hub_bi = plan_crosslink_items(sites, CROSSLINK_HUB, hub_site_id="s1", bidirectional=True)
    assert len(hub_bi) == 6

    # Ring n=4: uni=4, bi=8
    ring_uni = plan_crosslink_items(sites, CROSSLINK_RING, bidirectional=False)
    assert len(ring_uni) == 4
    assert [(i.source_site_id, i.target_site_id) for i in ring_uni] == [
        ("s1", "s2"),
        ("s2", "s3"),
        ("s3", "s4"),
        ("s4", "s1"),
    ]
    ring_bi = plan_crosslink_items(sites, CROSSLINK_RING, bidirectional=True)
    assert len(ring_bi) == 8

    # Ring n=2 dedupes to 1 (uni) / 2 (bi)
    two_sites = sites[:2]
    ring2_uni = plan_crosslink_items(two_sites, CROSSLINK_RING, bidirectional=False)
    ring2_bi = plan_crosslink_items(two_sites, CROSSLINK_RING, bidirectional=True)
    assert len(ring2_uni) == 1
    assert [(i.source_site_id, i.target_site_id) for i in ring2_uni] == [("s1", "s2")]
    assert len(ring2_bi) == 2
    assert {(i.source_site_id, i.target_site_id) for i in ring2_bi} == {("s1", "s2"), ("s2", "s1")}

    # Validation errors
    with pytest.raises(ValueError, match="至少选择 2 个站点"):
        plan_crosslink_items(sites[:1], CROSSLINK_MESH)
    with pytest.raises(ValueError, match="主站"):
        plan_crosslink_items(sites, CROSSLINK_HUB, hub_site_id="not-in-list")
    with pytest.raises(ValueError, match="不支持"):
        plan_crosslink_items(sites, "star")


# --------------------------------------------------------------------------- #
# Cross-link preview -> execute -> ledger -> rollback & unlink_between
# --------------------------------------------------------------------------- #
def test_crosslink_preview_execute_rollback_and_unlink(tmp_path: Path) -> None:
    store = InMemorySiteStore()
    site_a = _make_site("站A", "https://a.example.com", site_id="s1", site_title="站点A")
    site_b = _make_site("站B", "https://b.example.com", site_id="s2", site_title="站点B")
    site_c = _make_site("站C", "https://c.example.com", site_id="s3", site_title="站点C")
    sites = [site_a, site_b, site_c]

    # Seed pre-existing link A->B and an unrelated external link on A
    store.seed(
        site_a.url,
        [
            {"id": 1, "title": "原有B", "link": "https://b.example.com/"},
            {"id": 2, "title": "外部友链", "link": "https://external.example.org/"},
        ],
    )

    ledger = FriendLinkLedger(tmp_path / "设置" / "friendlink_ledger.json")
    svc = FriendLinkService(
        IntegrationSettings(friendlink=FriendLinkSettings(max_concurrency=3, per_request_delay_s=0.0)),
        session_factory=store.session_factory,
        sleep=lambda _: None,
        now=lambda: datetime(2026, 11, 21, 14, 0, 0),
    )

    plan = svc.plan_crosslinks(sites, CROSSLINK_MESH)
    assert len(plan.items) == 6
    fp_before = plan.fingerprint()

    # Preview (dry-run: zero POST requests!)
    preview = svc.preview_plan(plan, sites)
    assert preview.fingerprint() == fp_before
    assert not any(c["method"] == "POST" for c in store.calls)
    assert preview.counts[LINK_SKIPPED_EXISTS] == 1
    assert preview.counts[LINK_PLANNED] == 5

    # Execute plan -> creates 5 new links, skips pre-existing A->B, writes ledger
    report = svc.execute_plan(preview, sites, ledger=ledger)
    assert len(report.results) == 6
    assert report.plan.counts[LINK_CREATED] == 5
    assert report.plan.counts[LINK_SKIPPED_EXISTS] == 1

    # Ledger has only 5 created links (A->B was skipped_exists, must NOT be rolled back)
    created_entries = ledger.created_links(plan.plan_id)
    assert len(created_entries) == 5
    assert not any(e.source_site_id == "s1" and e.link == "https://b.example.com/" for e in created_entries)

    # Simulate user manually deleting one created link (B->C) before rollback
    store.links[site_b.url] = [item for item in store.links[site_b.url] if item["link"] != "https://c.example.com/"]

    # Rollback plan: deletes the 4 remaining created links, marks manually-deleted B->C as not_found & rolled back,
    # and MUST keep pre-existing A->B and external link intact!
    rb_results = svc.rollback_plan(plan.plan_id, sites, ledger=ledger)
    assert len(rb_results) == 5
    statuses = [r.status for r in rb_results]
    assert statuses.count(LINK_DELETED) == 4
    assert statuses.count(LINK_NOT_FOUND) == 1
    assert ledger.created_links(plan.plan_id) == []

    # Pre-existing A->B and external link on A remain untouched after rollback!
    remaining_a = {item["link"] for item in store.links[site_a.url]}
    assert remaining_a == {"https://b.example.com/", "https://external.example.org/"}

    # Second rollback is a no-op
    assert svc.rollback_plan(plan.plan_id, sites, ledger=ledger) == []

    # unlink_between removes remaining cross-link A->B while keeping external link untouched
    unlink_results = svc.unlink_between(sites)
    assert len(unlink_results) == 1
    assert unlink_results[0].site_id == "s1"
    assert unlink_results[0].link == "https://b.example.com/"
    assert unlink_results[0].status == LINK_DELETED
    assert {item["link"] for item in store.links[site_a.url]} == {"https://external.example.org/"}


def test_preview_plan_marks_unreachable_source_as_failed() -> None:
    store = InMemorySiteStore()
    site_a = _make_site("站A", "https://a.example.com", site_id="s1")
    site_b = _make_site("站B", "https://b.example.com", site_id="s2")
    store.fail_sites[site_a.url] = FakeResponse(403)

    svc = FriendLinkService(session_factory=store.session_factory, sleep=lambda _: None)
    plan = svc.plan_crosslinks([site_a, site_b], CROSSLINK_MESH)
    preview = svc.preview_plan(plan, [site_a, site_b])

    by_source = {item.source_site_id: item for item in preview.items}
    assert by_source["s1"].state == LINK_FAILED
    assert AUTH_HINT in by_source["s1"].message
    assert by_source["s2"].state == LINK_PLANNED
