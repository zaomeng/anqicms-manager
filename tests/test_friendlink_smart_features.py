"""Unit tests for smart friend-link HTML parsing and root-domain backlink detection."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import requests

from core.friendlink_service import (
    _MAX_BACKLINK_BYTES,
    FriendLinkService,
    extract_root_domain,
    parse_batch_link_input,
    parse_html_links,
)
from models.entities import SiteConfig
from models.sitegroup import FriendLink, FriendLinkSpec


def _make_site(name: str, url: str, *, token: str = "tok-secret-12345") -> SiteConfig:
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
        site_id=name,
    )


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        *,
        text: str = "",
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        url: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")
        self.headers = headers or {}
        if url is not None:
            self.url = url
        self.closed = False

    def iter_content(self, chunk_size: int = 16384):
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
        self.calls.append({"method": "GET", "url": url, **kwargs})
        return self._handler("GET", url, kwargs)

    def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------- #
# HTML <a> parsing: parse_html_links & mixed parse_batch_link_input
# --------------------------------------------------------------------------- #
def test_parse_html_links_quotes_nesting_entities_rel_fallbacks_and_dedupe() -> None:
    html = """
    <ul class="friendlinks">
      <li><a href="https://a.example.com" rel="nofollow"><span>站A &amp; 官网</span></a></li>
      <li><a href='http://www.a.example.com/' rel='dofollow'>重复应忽略</a></li>
      <li><a href=https://b.example.com rel=NOFOLLOW>无引号站B</a></li>
      <li><a href="https://c.example.com" rel="noopener noreferrer nofollow"><b>站C</b> <i>博客</i></a></li>
      <li><a href="https://d.example.com" rel="nofollower">非nofollow词</a></li>
      <li><a href="https://e.example.com" title="Title属性 &amp; 标题"><span>  &nbsp;  </span></a></li>
      <li><a href="https://f.example.com/links" title="   "></a></li>
      <li>
        <a
          href="https://g.example.com"
          rel="external, nofollow">
          <strong>多行</strong>
          <span>站G</span>
        </a>
      </li>
      <li><a href="https://h1.example.com">同1</a> <a href="https://h2.example.com">同2</a></li>
      <li><a href="/relative.html">站内相对链接</a></li>
      <li><a href="#top">锚点</a></li>
      <li><a href="javascript:void(0)">JS链接</a></li>
      <li><a href="mailto:hi@example.com">邮箱</a></li>
      <li><a href="ftp://bad.example.com">FTP</a></li>
      <li><a>无Href</a></li>
    </ul>
    """
    specs = parse_html_links(html)
    assert specs == [
        FriendLinkSpec(title="站A & 官网", link="https://a.example.com/", nofollow=True),
        FriendLinkSpec(title="无引号站B", link="https://b.example.com/", nofollow=True),
        FriendLinkSpec(title="站C 博客", link="https://c.example.com/", nofollow=True),
        FriendLinkSpec(title="非nofollow词", link="https://d.example.com/", nofollow=False),
        FriendLinkSpec(title="Title属性 & 标题", link="https://e.example.com/", nofollow=False),
        FriendLinkSpec(title="f.example.com", link="https://f.example.com/links", nofollow=False),
        FriendLinkSpec(title="多行 站G", link="https://g.example.com/", nofollow=True),
        FriendLinkSpec(title="同1", link="https://h1.example.com/", nofollow=False),
        FriendLinkSpec(title="同2", link="https://h2.example.com/", nofollow=False),
    ]

    # default_nofollow=True applies unless rel explicitly specifies dofollow/follow
    nf_specs = parse_html_links(
        '<a href="https://x.com">X</a> <a href="https://y.com" rel="dofollow">Y</a>',
        default_nofollow=True,
    )
    assert nf_specs[0].nofollow is True
    assert nf_specs[1].nofollow is False


def test_parse_html_links_unclosed_and_nested_anchor() -> None:
    specs = parse_html_links('<a href="https://one.example.com">站一 <a href="https://two.example.com">站二')
    assert specs == [
        FriendLinkSpec(title="站一", link="https://one.example.com/"),
        FriendLinkSpec(title="站二", link="https://two.example.com/"),
    ]


def test_parse_batch_link_input_mixed_html_and_plain_text_ignores_wrappers() -> None:
    mixed = """
    <!-- 多行
         HTML 注释应忽略 -->
    <div
      class="friendlinks-box"
      id="links">
      <ul>
        <li><a href="https://html1.example.com" rel="nofollow"><span>HTML站1</span></a></li>
        <li>
          <a href="https://html2.example.com">
            HTML多行站2
          </a>
        </li>
      </ul>
      <p title="a,b">友情链接列表：</p>
    </div>
    # 注释行
    文本站1|https://text1.example.com|1|互链备注
    https://www.html1.example.com/|重复HTML1应忽略
    https://text2.example.com|文本站2|0
    制表站\thttps://tab.example.com
    逗号站,https://comma.example.com
    https://space.example.com 空格站点
    https://bare.example.com
    站<1>|https://angle.example.com
    这是一行无效文本
    标题|ftp://bad.example.com
    """
    specs, errors = parse_batch_link_input(mixed)
    assert [s.link for s in specs] == [
        "https://html1.example.com/",
        "https://html2.example.com/",
        "https://text1.example.com/",
        "https://text2.example.com/",
        "https://tab.example.com/",
        "https://comma.example.com/",
        "https://space.example.com/",
        "https://bare.example.com/",
        "https://angle.example.com/",
    ]
    assert specs[0] == FriendLinkSpec(title="HTML站1", link="https://html1.example.com/", nofollow=True)
    assert specs[1] == FriendLinkSpec(title="HTML多行站2", link="https://html2.example.com/", nofollow=False)
    assert specs[2] == FriendLinkSpec(title="文本站1", link="https://text1.example.com/", nofollow=True, remark="互链备注")
    assert specs[8] == FriendLinkSpec(title="站<1>", link="https://angle.example.com/", nofollow=False)

    # Pure HTML wrappers must NOT produce errors; only real bad plain-text lines do, with exact line numbers
    assert len(errors) == 2
    assert errors[0].startswith("第26行：网址无效")
    assert errors[1].startswith("第27行：网址无效")


def test_parse_batch_link_input_skips_non_http_html_anchors_without_error() -> None:
    raw = """
    <nav>
      <a href="/about.html">关于我们</a>
      <a href="#top">返回顶部</a>
      <a href="https://ok.example.com">有效友链</a>
    </nav>
    """
    specs, errors = parse_batch_link_input(raw)
    assert errors == []
    assert specs == [FriendLinkSpec(title="有效友链", link="https://ok.example.com/")]


def test_parse_batch_link_input_plain_first_wins_dedupe_over_html() -> None:
    raw = "纯文本标题|https://dup.example.com|1|保留备注\n<a href='https://www.dup.example.com/'>HTML标题</a>"
    specs, errors = parse_batch_link_input(raw)
    assert errors == []
    assert specs == [
        FriendLinkSpec(title="纯文本标题", link="https://dup.example.com/", nofollow=True, remark="保留备注")
    ]


# --------------------------------------------------------------------------- #
# Root domain extraction
# --------------------------------------------------------------------------- #
def test_extract_root_domain_variants_and_public_suffixes() -> None:
    cases = {
        "http://banjia1.banjia-site.test": "banjia-site.test",
        "https://WWW.BANJIA-SITE.TEST:8443/about/": "banjia-site.test",
        "banjia-site.test": "banjia-site.test",
        "a.b.banjia1.banjia-site.test:8080": "banjia-site.test",
        "//sub.banjia-site.test/path": "banjia-site.test",
        "https://www.foo.com.cn": "foo.com.cn",
        "http://sub.bar.net.cn/links": "bar.net.cn",
        "https://a.b.baz.org.cn": "baz.org.cn",
        "https://www.gov.cn": "www.gov.cn",
        "https://www.tsinghua.edu.cn": "tsinghua.edu.cn",
        "https://sub.cas.ac.cn": "cas.ac.cn",
        "https://www.bbc.co.uk/news": "bbc.co.uk",
        "https://sub.eff.org.uk": "eff.org.uk",
        "https://www.shop.com.hk": "shop.com.hk",
        "https://www.news.com.tw": "news.com.tw",
        "https://www.site.co.jp": "site.co.jp",
        "https://www.site.com.au": "site.com.au",
        "https://www.site.com.sg": "site.com.sg",
        "http://127.0.0.1:8080/test": "127.0.0.1",
        "192.168.1.10": "192.168.1.10",
        "http://[::1]:8000/": "::1",
        "::1": "::1",
        "http://localhost:3000/admin": "localhost",
        "localhost": "localhost",
        "": "",
        "   ": "",
        "mailto:admin@banjia-site.test": "",
        "javascript:void(0)": "",
        "http://nodot": "",
    }
    for raw, expected in cases.items():
        assert extract_root_domain(raw) == expected, f"failed for {raw!r}"


# --------------------------------------------------------------------------- #
# Automatic root-domain backlink detection, fallback, follow status & encoding
# --------------------------------------------------------------------------- #
def test_check_backlinks_root_domain_matching_and_false_positive_prevention() -> None:
    site = _make_site("搬家分站", "https://banjia1.banjia-site.test")
    pages = {
        "https://p1.example.com/": FakeResponse(
            200, text='<a href="https://banjia-site.test/">搬家主站</a>'
        ),
        "https://p2.example.com/": FakeResponse(
            200, text='<a href="//www.banjia-site.test/index.html"><span>协议相对链接</span></a>'
        ),
        "https://p3.example.com/": FakeResponse(
            200, text='<a href="http://banjia2.banjia-site.test/">同主域兄弟站</a>'
        ),
        "https://p4.example.com/": FakeResponse(
            200,
            text=(
                "<p>这里只提到了 banjia-site.test 文本，不是超链接</p>"
                '<a href="/go?target=https://banjia-site.test">站内跳转</a>'
                '<a href="https://notbanjia-site.test/">相似域名</a>'
                '<a href="https://banjia-site.test.evil.com/">后缀仿冒</a>'
                '<a href="mailto:contact@banjia-site.test">联系邮箱</a>'
            ),
        ),
    }

    svc = FriendLinkService(
        session_factory=lambda: RecordingSession(lambda _m, u, _kw: pages[u])
    )
    links = [
        FriendLink(id=1, title="P1", link="https://p1.example.com/"),
        FriendLink(id=2, title="P2", link="https://p2.example.com/"),
        FriendLink(id=3, title="P3", link="https://p3.example.com/"),
        FriendLink(id=4, title="P4", link="https://p4.example.com/"),
    ]
    res = svc.check_backlinks(site, links)

    assert res[0].reachable is True and res[0].has_backlink is True
    assert res[0].message == "有反链(dofollow · 搬家主站)"

    assert res[1].reachable is True and res[1].has_backlink is True
    assert res[1].message == "有反链(dofollow · 协议相对链接)"

    assert res[2].reachable is True and res[2].has_backlink is True
    assert res[2].message == "有反链(dofollow · 同主域兄弟站)"

    assert res[3].reachable is True and res[3].has_backlink is False
    assert res[3].message == "未发现我方主域名(banjia-site.test)反链"


def test_check_backlinks_prefers_dofollow_and_reports_nofollow_and_empty_anchor() -> None:
    site = _make_site("站A", "https://www.foo.com.cn")
    pages = {
        "https://only-nf.example.com/": FakeResponse(
            200,
            text='<a href="https://foo.com.cn/" rel="noopener nofollow">仅NF锚文本</a>',
        ),
        "https://mixed.example.com/": FakeResponse(
            200,
            text=(
                '<a href="https://sub.foo.com.cn/post/1" rel="nofollow">先出现的NF</a>'
                '<a href="https://www.foo.com.cn/">后出现的DF</a>'
            ),
        ),
        "https://empty-text.example.com/": FakeResponse(
            200,
            text='<a href="https://foo.com.cn/"></a>',
        ),
    }

    svc = FriendLinkService(
        session_factory=lambda: RecordingSession(lambda _m, u, _kw: pages[u])
    )
    res = svc.check_backlinks(
        site,
        [
            FriendLink(id=1, title="NF", link="https://only-nf.example.com/"),
            FriendLink(id=2, title="Mixed", link="https://mixed.example.com/"),
            FriendLink(id=3, title="Empty", link="https://empty-text.example.com/"),
        ],
    )
    assert res[0].has_backlink is True and res[0].message == "有反链(nofollow · 仅NF锚文本)"
    assert res[1].has_backlink is True and res[1].message == "有反链(dofollow · 后出现的DF)"
    assert res[2].has_backlink is True and res[2].message == "有反链(dofollow)"


def test_check_backlinks_homepage_fallback_for_non_root_link() -> None:
    site = _make_site("站A", "https://banjia1.banjia-site.test")
    seen_urls: list[str] = []

    def _handler(_method: str, url: str, _kw: dict[str, Any]) -> FakeResponse:
        seen_urls.append(url)
        # Case 1: /links.html has no backlink -> fallback to / finds it!
        if url == "https://p1.example.com/links.html":
            return FakeResponse(200, text="<p>内页暂无</p>")
        if url == "https://p1.example.com/":
            return FakeResponse(200, text='<a href="https://banjia-site.test/">首页反链</a>')

        # Case 2: /about already has backlink -> no extra homepage fetch
        if url == "https://p2.example.com/about":
            return FakeResponse(200, text='<a href="https://banjia-site.test/">关于页反链</a>')

        # Case 3: /news has no backlink AND homepage fails with 500 -> keep reachable=True, has_backlink=False
        if url == "https://p3.example.com/news":
            return FakeResponse(200, text="<p>暂无</p>")
        if url == "https://p3.example.com/":
            return FakeResponse(500)

        # Case 4: /go redirected to root / already -> do not double fetch root
        if url == "https://p4.example.com/go":
            return FakeResponse(200, text="<p>首页也没有</p>", url="https://p4.example.com/")

        return FakeResponse(404)

    svc = FriendLinkService(session_factory=lambda: RecordingSession(_handler))
    res = svc.check_backlinks(
        site,
        [
            FriendLink(id=1, title="P1", link="https://p1.example.com/links.html"),
            FriendLink(id=2, title="P2", link="https://p2.example.com/about"),
            FriendLink(id=3, title="P3", link="https://p3.example.com/news"),
            FriendLink(id=4, title="P4", link="https://p4.example.com/go"),
            FriendLink(id=5, title="P5", link="https://p5.example.com/"),
        ],
    )

    assert res[0].reachable is True and res[0].has_backlink is True
    assert res[0].message == "有反链(dofollow · 首页反链)"
    assert res[1].reachable is True and res[1].has_backlink is True
    assert res[1].message == "有反链(dofollow · 关于页反链)"
    assert res[2].reachable is True and res[2].has_backlink is False and res[2].http_status == 200
    assert res[2].message == "未发现我方主域名(banjia-site.test)反链"
    assert res[3].reachable is True and res[3].has_backlink is False
    assert res[4].reachable is False and res[4].http_status == 404
    assert res[4].message == "对方无法访问(HTTP 404)"

    # Verify p2 did not fetch /, p4 did not fetch / twice, p5 (already root) fetched once
    assert seen_urls == [
        "https://p1.example.com/links.html",
        "https://p1.example.com/",
        "https://p2.example.com/about",
        "https://p3.example.com/news",
        "https://p3.example.com/",
        "https://p4.example.com/go",
        "https://p5.example.com/",
    ]


def test_check_backlinks_gbk_gb2312_and_truncated_utf8_decoding() -> None:
    site = _make_site("搬家站", "https://banjia-site.test")

    # 1. GBK with Content-Type header
    gbk_html = '<a href="https://www.banjia-site.test/">中文搬家官网</a>'.encode("gbk")
    # 2. GB2312 with <meta charset="gb2312"> and no Content-Type header
    gb2312_html = (
        '<meta charset="gb2312"><a href="https://banjia-site.test/" rel="nofollow">同城搬家</a>'
    ).encode("gb2312")
    # 3. Raw GBK bytes without header or meta
    raw_gbk = '<a href="https://banjia1.banjia-site.test/">纯GBK友链</a>'.encode("gbk")
    # 4. UTF-8 page truncated right inside a 3-byte Chinese char at _MAX_BACKLINK_BYTES
    prefix = b'<a href="https://banjia-site.test/">UTF8\xe6\xb5\x8b\xe8\xaf\x95</a>'
    pad_len = _MAX_BACKLINK_BYTES - len(prefix) - 1  # leaves room for 1st byte of 3-byte '中'
    truncated_utf8 = prefix + (b"a" * pad_len) + "中".encode("utf-8")[:1]
    assert len(truncated_utf8) == _MAX_BACKLINK_BYTES

    pages = {
        "https://gbk-header.example.com/": FakeResponse(
            200, content=gbk_html, headers={"Content-Type": "text/html; charset=gbk"}
        ),
        "https://gb2312-meta.example.com/": FakeResponse(200, content=gb2312_html),
        "https://raw-gbk.example.com/": FakeResponse(200, content=raw_gbk),
        "https://trunc-utf8.example.com/": FakeResponse(200, content=truncated_utf8),
    }

    svc = FriendLinkService(session_factory=lambda: RecordingSession(lambda _m, u, _kw: pages[u]))
    res = svc.check_backlinks(
        site,
        [
            "https://gbk-header.example.com/",
            "https://gb2312-meta.example.com/",
            "https://raw-gbk.example.com/",
            "https://trunc-utf8.example.com/",
        ],
    )
    assert res[0].has_backlink is True and res[0].message == "有反链(dofollow · 中文搬家官网)"
    assert res[1].has_backlink is True and res[1].message == "有反链(nofollow · 同城搬家)"
    assert res[2].has_backlink is True and res[2].message == "有反链(dofollow · 纯GBK友链)"
    assert res[3].has_backlink is True and res[3].message == "有反链(dofollow · UTF8测试)"


def test_check_backlinks_unreachable_errors_redaction_and_cancel() -> None:
    secret = "super-secret-tok-777"
    site = _make_site("站A", "https://banjia-site.test", token=secret)

    def _handler(_method: str, url: str, _kw: dict[str, Any]) -> FakeResponse:
        if url == "https://ssl.example.com/":
            raise requests.exceptions.SSLError(f"cert error with token={secret}")
        if url == "https://timeout.example.com/":
            raise requests.exceptions.ReadTimeout("timed out")
        return FakeResponse(503)

    svc = FriendLinkService(session_factory=lambda: RecordingSession(_handler))
    res = svc.check_backlinks(
        site,
        [
            "https://ssl.example.com/",
            "https://timeout.example.com/",
            "https://down.example.com/",
        ],
    )
    assert res[0].reachable is False and "对方无法访问" in res[0].message
    assert secret not in res[0].message
    assert res[1].reachable is False and res[1].message == "对方无法访问(请求超时)"
    assert res[2].reachable is False and res[2].http_status == 503
    assert res[2].message == "对方无法访问(HTTP 503)"

    # Cancel prevents homepage fallback request
    cancel = threading.Event()
    calls: list[str] = []

    def _cancel_handler(_method: str, url: str, _kw: dict[str, Any]) -> FakeResponse:
        calls.append(url)
        cancel.set()
        return FakeResponse(200, text="<p>无反链</p>")

    svc_cancel = FriendLinkService(session_factory=lambda: RecordingSession(_cancel_handler))
    res_cancel = svc_cancel.check_backlinks(
        site,
        [
            FriendLink(id=1, title="P1", link="https://p1.example.com/links.html"),
            FriendLink(id=2, title="P2", link="https://p2.example.com/"),
        ],
        cancel_event=cancel,
    )
    assert len(res_cancel) == 1
    assert calls == ["https://p1.example.com/links.html"]
