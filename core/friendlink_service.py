"""Friend-link (友情链接) & cross-link (站群互链) service.

Primary transport is the official AnqiCMS v3.6.x REST API:
- ``GET  {url}/api/friendlink/list?token=...``
- ``POST {url}/api/friendlink/create?token=...`` (multipart/form-data)
- ``POST {url}/api/friendlink/delete?token=...`` (multipart/form-data)
- ``GET|POST {url}/api/friendlink/check?token=...``

Optional MCP fallback is supported exclusively through the
``core.mcp_contracts.McpClientFactory`` protocol (no direct import of any
concrete MCP client module).
"""

from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import random
import re
import threading
import time
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Callable, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from core.friendlink_ledger import FriendLinkLedger
from core.integration_settings import IntegrationSettings
from core.mcp_contracts import McpClientFactory, McpError, SSL_HINT
from core.redaction import SecretRedactor, redact_text
from core.site_credentials import (
    display_name,
    effective_mcp_token,
    import_token,
    site_origin_key,
)
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
    CrossLinkItem,
    CrossLinkPlan,
    CrossLinkReport,
    FriendLink,
    FriendLinkSpec,
    LedgerEntry,
    LinkHealth,
    LinkOpResult,
)

AUTH_HINT = "导入 Token 无效或未开启友情链接 API"
REDIRECT_HINT = "接口发生重定向，请检查站点网址 http/https"
CHECK_UNSUPPORTED_HINT = "站点版本不支持友链检测接口"
MISSING_SITE_CREDS_MSG = "缺少站点网址或导入 Token"

CROSSLINK_RANDOM_K = "random_k"
CROSSLINK_GROUP_CROSS = "group_cross"

_MAX_BACKLINK_BYTES = 2 * 1024 * 1024
_BACKLINK_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
_SUCCESS_CODES = {0, "0", 200, "200"}
_VALID_CROSSLINK_MODES = {
    CROSSLINK_MESH,
    CROSSLINK_HUB,
    CROSSLINK_RING,
    CROSSLINK_RANDOM_K,
    CROSSLINK_GROUP_CROSS,
}
_HOST_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")

_NOFOLLOW_TRUE = frozenset({"1", "true", "yes", "y", "on", "nofollow", "nf", "是"})
_NOFOLLOW_FALSE = frozenset({"0", "false", "no", "n", "off", "dofollow", "follow", "否"})

_HTML_WRAPPER_LINE_RE = re.compile(
    r"^(?:<!--|<!DOCTYPE\b|</?[A-Za-z][A-Za-z0-9-]*(?:\s|/?\s*>|$))",
    re.IGNORECASE,
)
_CHARSET_RE = re.compile(r"charset\s*=\s*['\"]?\s*([A-Za-z0-9._-]+)", re.IGNORECASE)
_META_CHARSET_RE = re.compile(
    rb"<meta[^>]+charset\s*=\s*[\"']?\s*([A-Za-z0-9._-]+)",
    re.IGNORECASE,
)

_MULTI_PART_PUBLIC_SUFFIXES = frozenset(
    {
        # China (.cn)
        "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn", "mil.cn",
        "bj.cn", "sh.cn", "tj.cn", "cq.cn", "he.cn", "sx.cn", "nm.cn", "ln.cn",
        "jl.cn", "hl.cn", "js.cn", "zj.cn", "ah.cn", "fj.cn", "jx.cn", "sd.cn",
        "ha.cn", "hb.cn", "hn.cn", "gd.cn", "gx.cn", "hi.cn", "sc.cn", "gz.cn",
        "yn.cn", "xz.cn", "sn.cn", "gs.cn", "qh.cn", "nx.cn", "xj.cn", "hk.cn",
        "mo.cn", "tw.cn",
        # HK / TW / MO
        "com.hk", "org.hk", "net.hk", "edu.hk", "gov.hk", "idv.hk",
        "com.tw", "org.tw", "net.tw", "edu.tw", "gov.tw", "idv.tw", "club.tw", "ebiz.tw", "game.tw",
        "com.mo", "org.mo", "net.mo", "edu.mo", "gov.mo",
        # UK
        "co.uk", "org.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk", "ac.uk", "gov.uk", "sch.uk",
        # JP / KR
        "co.jp", "ne.jp", "or.jp", "ac.jp", "ad.jp", "ed.jp", "go.jp", "gr.jp", "lg.jp",
        "co.kr", "ne.kr", "or.kr", "re.kr", "pe.kr", "go.kr", "ac.kr", "hs.kr", "ms.kr", "es.kr", "kg.kr",
        # AU / NZ
        "com.au", "net.au", "org.au", "edu.au", "gov.au", "asn.au", "id.au",
        "co.nz", "net.nz", "org.nz", "govt.nz", "ac.nz", "school.nz", "geek.nz", "gen.nz", "maori.nz",
        # SEA / IN
        "com.sg", "org.sg", "net.sg", "edu.sg", "gov.sg", "per.sg",
        "com.my", "net.my", "org.my", "edu.my", "gov.my", "mil.my",
        "co.id", "or.id", "web.id", "ac.id", "go.id", "sch.id", "net.id", "my.id", "biz.id",
        "co.th", "in.th", "ac.th", "or.th", "go.th", "mi.th", "net.th",
        "com.vn", "net.vn", "org.vn", "edu.vn", "gov.vn", "biz.vn", "info.vn",
        "com.ph", "org.ph", "net.ph", "edu.ph", "gov.ph",
        "co.in", "net.in", "org.in", "firm.in", "gen.in", "ind.in", "ac.in", "edu.in", "gov.in", "res.in",
        # Americas / EMEA / Misc
        "co.za", "org.za", "net.za", "gov.za", "ac.za", "edu.za", "web.za",
        "com.br", "net.br", "org.br", "gov.br", "edu.br",
        "com.mx", "org.mx", "net.mx", "edu.mx", "gob.mx",
        "com.ar", "org.ar", "net.ar", "gov.ar", "edu.ar",
        "com.ru", "net.ru", "org.ru", "pp.ru",
        "com.tr", "net.tr", "org.tr", "gen.tr", "biz.tr", "info.tr", "gov.tr", "edu.tr",
        "co.il", "org.il", "net.il", "ac.il", "gov.il",
        "com.ua", "net.ua", "org.ua", "in.ua", "kiev.ua",
        "com.pl", "net.pl", "org.pl", "info.pl", "edu.pl",
        "com.co", "net.co", "nom.co", "eu.org", "us.org",
        "uk.com", "uk.net", "cn.com", "de.com", "jp.net",
        "com.es", "nom.es", "org.es", "edu.es", "gob.es",
    }
)

_MCP_LIST_KEYWORDS = ("friendlink_list", "list_friendlinks", "friend_link_list", "friendlink", "友情链接", "友链")
_MCP_CREATE_KEYWORDS = ("friendlink_create", "create_friendlink", "add_friendlink", "friendlink", "友情链接", "友链")
_MCP_DELETE_KEYWORDS = ("friendlink_delete", "delete_friendlink", "remove_friendlink", "friendlink", "友情链接", "友链")


class FriendLinkError(RuntimeError):
    """Raised when a friend-link API operation fails."""

    def __init__(
        self,
        message: str,
        *,
        hint: str = "",
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.http_status = http_status


def _site_id(site) -> str:
    sid = getattr(site, "site_id", "")
    if isinstance(sid, str) and sid.strip():
        return sid.strip()
    name = getattr(site, "name", "")
    return name.strip() if isinstance(name, str) else ""


def _site_name(site) -> str:
    name = getattr(site, "name", "")
    return name if isinstance(name, str) else ""


def _site_url(site) -> str:
    raw = getattr(site, "url", "")
    return raw.strip().rstrip("/") if isinstance(raw, str) else ""


def _is_ip_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _is_valid_link_host(hostname: str) -> bool:
    if not hostname or any(ch.isspace() or ch == "\\" or ord(ch) < 32 or ord(ch) == 127 for ch in hostname):
        return False
    if _is_ip_address(hostname):
        return True
    if all(ch.isdigit() or ch == "." for ch in hostname):
        return False
    try:
        ascii_host = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    if not ascii_host or len(ascii_host) > 253 or ascii_host.startswith(".") or ascii_host.endswith("."):
        return False
    if ascii_host.casefold() == "localhost":
        return True
    if "." not in ascii_host:
        return False
    labels = ascii_host.split(".")
    return all(_HOST_LABEL_RE.fullmatch(label) for label in labels)


def _extract_normalized_host(url_or_host: str) -> str:
    """Extract normalized lowercase ASCII/IP hostname from URL or host[:port], or ``""``."""
    if not isinstance(url_or_host, str):
        return ""
    raw = url_or_host.strip()
    if not raw or any(ch.isspace() or ch == "\\" or ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        return ""

    bare_ip = raw[1:-1] if (raw.startswith("[") and raw.endswith("]")) else raw
    if _is_ip_address(bare_ip):
        return bare_ip.casefold()

    parse_target = raw if ("://" in raw or raw.startswith("//")) else f"//{raw}"
    try:
        parsed = urlsplit(parse_target)
        _ = parsed.port
    except ValueError:
        return ""

    if parsed.username is not None or parsed.password is not None:
        return ""

    host = (parsed.hostname or "").strip(".").casefold()
    if not host or not _is_valid_link_host(host):
        return ""
    if _is_ip_address(host):
        return host
    try:
        return host.encode("idna").decode("ascii").casefold()
    except UnicodeError:
        return host


def extract_root_domain(url_or_host: str) -> str:
    """Extract the registrable root domain from a URL or hostname.

    Normalizes case, strips scheme/port/path, preserves IPv4/IPv6/localhost as-is,
    and recognizes common multi-part public suffixes (e.g. ``.com.cn``, ``.co.uk``,
    ``.com.hk``, ``.com.tw``, ``.co.jp``, ``.com.au``, ``.com.sg``).
    """
    host = _extract_normalized_host(url_or_host)
    if not host:
        return ""
    if _is_ip_address(host) or host == "localhost":
        return host
    if host.endswith(".localhost"):
        return "localhost"

    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if len(labels) >= 3 and ".".join(labels[-2:]) in _MULTI_PART_PUBLIC_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def normalize_link_url(url: str) -> str:
    """Normalize a friend-link URL to canonical ``https?://host[:port]/path``.

    Raises ``ValueError`` if ``url`` is not a valid ``http``/``https`` URL.
    """
    if not isinstance(url, str):
        raise ValueError("网址无效")
    raw = url.strip()
    if not raw or any(ch.isspace() or ch == "\\" or ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise ValueError("网址无效")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("网址无效") from exc

    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"}:
        raise ValueError("网址必须以 http:// 或 https:// 开头")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("网址不能包含账号密码")

    hostname = (parsed.hostname or "").strip(".").casefold()
    if not _is_valid_link_host(hostname):
        raise ValueError("网址主机名无效")

    formatted_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    netloc = formatted_host if (port is None or port == default_port) else f"{formatted_host}:{port}"
    path = parsed.path or "/"
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def link_key(url: str) -> str:
    """Deduplication key: ``site_origin_key + path.rstrip('/')``.

    Ignores scheme, leading ``www.``, default port, and trailing slash.
    Returns ``""`` when ``url`` has no valid host.
    """
    if not isinstance(url, str):
        return ""
    text = url.strip()
    if not text or any(ch.isspace() or ch == "\\" or ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return ""
    origin = site_origin_key(text)
    if not origin:
        return ""
    parse_target = text if "://" in text else f"//{text}"
    try:
        parsed = urlsplit(parse_target)
        host = (parsed.hostname or "").strip(".").casefold()
    except ValueError:
        return ""
    if not _is_valid_link_host(host):
        return ""
    path = parsed.path.rstrip("/")
    if parsed.query:
        return f"{origin}{path}?{parsed.query}"
    return f"{origin}{path}"


def _looks_like_http_url(token: str) -> bool:
    lowered = token.strip().casefold()
    return lowered.startswith("http://") or lowered.startswith("https://")


def _parse_nofollow_and_remark(
    extra: list[str],
    *,
    default_nofollow: bool,
    joiner: str,
) -> tuple[bool, str]:
    if not extra:
        return default_nofollow, ""
    first = extra[0].strip()
    rest = joiner.join(part.strip() for part in extra[1:] if part.strip()).strip()
    if not first:
        return default_nofollow, rest
    lowered = first.casefold()
    if lowered in _NOFOLLOW_TRUE:
        return True, rest
    if lowered in _NOFOLLOW_FALSE:
        return False, rest
    # Unrecognized 3rd token: treat as remark rather than failing the whole line.
    combined = joiner.join(part.strip() for part in extra if part.strip()).strip()
    return default_nofollow, combined


def _split_delimited_line(
    parts: list[str],
    *,
    default_nofollow: bool,
    joiner: str,
) -> tuple[str, str, bool, str]:
    if len(parts) >= 2 and _looks_like_http_url(parts[0]) and not _looks_like_http_url(parts[1]):
        url_raw = parts[0]
        title = parts[1]
        nofollow, remark = _parse_nofollow_and_remark(
            parts[2:], default_nofollow=default_nofollow, joiner=joiner
        )
        return title, url_raw, nofollow, remark

    title = parts[0] if parts else ""
    url_raw = parts[1] if len(parts) > 1 else ""
    nofollow, remark = _parse_nofollow_and_remark(
        parts[2:], default_nofollow=default_nofollow, joiner=joiner
    )
    return title, url_raw, nofollow, remark


@dataclass(frozen=True, slots=True)
class _ParsedAnchor:
    start_line: int
    end_line: int
    href: str
    text: str
    title_attr: str
    img_alt: str
    rel: str


class _AnchorHTMLParser(HTMLParser):
    """Collect ``<a>`` elements with line spans, attributes, and nested inner text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[_ParsedAnchor] = []
        self.covered_lines: set[int] = set()
        self._in_anchor = False
        self._anchor_start_line = 0
        self._anchor_start_tag_end_line = 0
        self._anchor_href = ""
        self._anchor_title_attr = ""
        self._anchor_rel = ""
        self._anchor_text_parts: list[str] = []
        self._anchor_img_alts: list[str] = []

    def _finalize_anchor(self, end_line: int, *, mark_full_span: bool) -> None:
        if not self._in_anchor:
            return
        effective_end = max(self._anchor_start_tag_end_line, end_line)
        text = " ".join("".join(self._anchor_text_parts).split())
        title_attr = " ".join(self._anchor_title_attr.split())
        img_alt = " ".join(" ".join(self._anchor_img_alts).split())
        self.anchors.append(
            _ParsedAnchor(
                start_line=self._anchor_start_line,
                end_line=effective_end,
                href=self._anchor_href,
                text=text,
                title_attr=title_attr,
                img_alt=img_alt,
                rel=self._anchor_rel,
            )
        )
        if mark_full_span:
            self.covered_lines.update(range(self._anchor_start_line, effective_end + 1))
        self._in_anchor = False
        self._anchor_text_parts = []
        self._anchor_img_alts = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        start_line, _ = self.getpos()
        raw_tag = self.get_starttag_text() or ""
        tag_end_line = start_line + raw_tag.count("\n")
        if tag_end_line > start_line:
            self.covered_lines.update(range(start_line, tag_end_line + 1))

        lower_tag = tag.casefold()
        if lower_tag == "a":
            if self._in_anchor:
                self._finalize_anchor(start_line, mark_full_span=True)
            attr_map: dict[str, str] = {}
            for key, val in attrs:
                if key and key not in attr_map:
                    attr_map[key] = val or ""
            self._in_anchor = True
            self._anchor_start_line = start_line
            self._anchor_start_tag_end_line = tag_end_line
            self._anchor_href = attr_map.get("href", "").strip()
            self._anchor_title_attr = attr_map.get("title", "")
            self._anchor_rel = attr_map.get("rel", "")
            self._anchor_text_parts = []
            self._anchor_img_alts = []
            self.covered_lines.update(range(start_line, tag_end_line + 1))
        elif lower_tag == "img" and self._in_anchor:
            for key, val in attrs:
                if key == "alt" and val:
                    self._anchor_img_alts.append(val)
                    break

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._in_anchor:
            end_line, _ = self.getpos()
            self._finalize_anchor(end_line, mark_full_span=True)

    def handle_data(self, data: str) -> None:
        if self._in_anchor and data:
            self._anchor_text_parts.append(data)

    def handle_comment(self, data: str) -> None:
        start_line, _ = self.getpos()
        end_line = start_line + data.count("\n")
        self.covered_lines.update(range(start_line, end_line + 1))

    def close(self) -> None:
        super().close()
        if self._in_anchor:
            self._finalize_anchor(self._anchor_start_tag_end_line, mark_full_span=False)


def _parse_html_anchors(text: str) -> _AnchorHTMLParser:
    parser = _AnchorHTMLParser()
    parser.feed(text)
    parser.close()
    return parser


def _parse_rel_nofollow(rel_attr: str, *, default_nofollow: bool) -> bool:
    tokens = {tok.casefold() for tok in re.split(r"[\s,]+", rel_attr or "") if tok}
    if "nofollow" in tokens:
        return True
    if tokens & {"dofollow", "follow"}:
        return False
    return bool(default_nofollow)


def _anchor_to_spec(
    anchor: _ParsedAnchor,
    *,
    default_nofollow: bool,
) -> FriendLinkSpec | None:
    try:
        normalized = normalize_link_url(anchor.href)
    except ValueError:
        return None
    title = anchor.text or anchor.title_attr or (urlsplit(normalized).hostname or normalized)
    return FriendLinkSpec(
        title=title,
        link=normalized,
        nofollow=_parse_rel_nofollow(anchor.rel, default_nofollow=default_nofollow),
        remark="",
    )


def _is_ignorable_html_line(line: str) -> bool:
    if not _HTML_WRAPPER_LINE_RE.match(line):
        return False
    if "|" in line and any(_looks_like_http_url(part) for part in line.split("|")):
        return False
    if "\t" in line and any(_looks_like_http_url(part) for part in line.split("\t")):
        return False
    return True


def parse_html_links(
    text: str,
    *,
    default_nofollow: bool = False,
) -> list[FriendLinkSpec]:
    """Extract deduplicated ``FriendLinkSpec`` entries from HTML ``<a>`` tags."""
    if not text or not isinstance(text, str):
        return []
    parser = _parse_html_anchors(text.replace("\r\n", "\n").replace("\r", "\n"))
    specs: list[FriendLinkSpec] = []
    seen_keys: set[str] = set()
    for anchor in parser.anchors:
        spec = _anchor_to_spec(anchor, default_nofollow=default_nofollow)
        if spec is None:
            continue
        key = link_key(spec.link)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        specs.append(spec)
    return specs


def parse_batch_link_input(
    text: str,
    *,
    default_nofollow: bool = False,
) -> tuple[list[FriendLinkSpec], list[str]]:
    """Parse multi-line friend-link input into deduplicated specs and error lines.

    Supported formats (can be mixed in one paste):
    - HTML ``<a href="..." [rel="nofollow"] [title="..."]>锚文本</a>`` fragments
    - ``标题|URL[|nofollow][|备注]``
    - ``URL|标题[|nofollow][|备注]``
    - ``标题<TAB>URL`` / ``URL<TAB>标题``
    - ``标题,URL`` / ``URL,标题`` (ASCII or Chinese comma)
    - ``URL 标题`` / ``标题 URL``
    - bare ``URL`` (title defaults to host)
    """
    specs: list[FriendLinkSpec] = []
    errors: list[str] = []
    seen_keys: set[str] = set()

    if not text or not isinstance(text, str):
        return specs, errors

    normalized_text = text.replace("\r\n", "\n").replace("\r", "\n")
    parser = _parse_html_anchors(normalized_text)
    anchors_by_line: dict[int, list[_ParsedAnchor]] = {}
    for anchor in parser.anchors:
        anchors_by_line.setdefault(anchor.start_line, []).append(anchor)

    def _append_spec(spec: FriendLinkSpec) -> None:
        key = link_key(spec.link)
        if key in seen_keys:
            return
        seen_keys.add(key)
        specs.append(spec)

    for line_no, raw_line in enumerate(normalized_text.split("\n"), start=1):
        for anchor in anchors_by_line.get(line_no, ()):
            spec = _anchor_to_spec(anchor, default_nofollow=default_nofollow)
            if spec is not None:
                _append_spec(spec)

        if line_no in parser.covered_lines:
            continue

        line = raw_line.strip()
        if not line or line.startswith("#") or _is_ignorable_html_line(line):
            continue

        title = ""
        url_raw = ""
        nofollow = bool(default_nofollow)
        remark = ""

        if "|" in line:
            parts = [p.strip() for p in line.split("|")]
            title, url_raw, nofollow, remark = _split_delimited_line(
                parts, default_nofollow=nofollow, joiner="|"
            )
        elif "\t" in line:
            parts = [p.strip() for p in line.split("\t")]
            title, url_raw, nofollow, remark = _split_delimited_line(
                parts, default_nofollow=nofollow, joiner=" "
            )
        elif "," in line or "，" in line:
            sep = "," if "," in line else "，"
            parts = [p.strip() for p in line.split(sep)]
            title, url_raw, nofollow, remark = _split_delimited_line(
                parts, default_nofollow=nofollow, joiner=sep
            )
        elif any(ch.isspace() for ch in line):
            tokens = line.split()
            if _looks_like_http_url(tokens[0]):
                url_raw = tokens[0]
                title = line[len(tokens[0]):].strip()
            elif _looks_like_http_url(tokens[-1]):
                url_raw = tokens[-1]
                title = line[: len(line) - len(tokens[-1])].strip()
            else:
                errors.append(f"第{line_no}行：网址无效")
                continue
        else:
            url_raw = line
            title = ""

        try:
            normalized = normalize_link_url(url_raw)
        except ValueError:
            errors.append(f"第{line_no}行：网址无效")
            continue

        final_title = title.strip() or (urlsplit(normalized).hostname or normalized)
        _append_spec(
            FriendLinkSpec(
                title=final_title,
                link=normalized,
                nofollow=nofollow,
                remark=remark,
            )
        )

    return specs, errors


def _site_group(site) -> str:
    group = getattr(site, "site_group", "")
    return group.strip() if isinstance(group, str) else ""


def _canonical_site_link(site) -> str:
    base = _site_url(site)
    if not base:
        return ""
    try:
        return normalize_link_url(f"{base}/")
    except ValueError:
        return f"{base}/"


def _anchor_candidates(site) -> list[str]:
    raw_title = getattr(site, "site_title", "")
    if isinstance(raw_title, str) and "|" in raw_title:
        parts = [part.strip() for part in raw_title.split("|") if part.strip()]
        if parts:
            return parts
    fallback = display_name(site).strip()
    return [fallback] if fallback else []


def _first_anchor_title(site) -> str:
    candidates = _anchor_candidates(site)
    return candidates[0] if candidates else _site_name(site)


def _pick_anchor_title(src, dst) -> str:
    candidates = _anchor_candidates(dst)
    if not candidates:
        return _site_name(dst)
    if len(candidates) == 1:
        return candidates[0]
    key = f"{_site_name(src)}\x00{_site_name(dst)}".encode("utf-8")
    hash_idx = int.from_bytes(hashlib.sha256(key).digest()[:8], "big")
    return candidates[hash_idx % len(candidates)]


def export_sites_html_links(
    sites,
    *,
    separator: str = "\n",
    include_nofollow: bool = False,
) -> str:
    """Render HTML ``<a>`` friend-link lines for ``sites`` (first variant if ``|`` pool)."""
    rel_attr = ' rel="nofollow"' if include_nofollow else ""
    lines: list[str] = []
    for site in sites or ():
        url = _canonical_site_link(site)
        if not url:
            continue
        title = _first_anchor_title(site) or url
        lines.append(
            f'<a href="{html.escape(url, quote=True)}" target="_blank"{rel_attr}>'
            f"{html.escape(title, quote=False)}</a>"
        )
    return separator.join(lines)


def _format_plan_remark(remark: str, plan_id: str) -> str:
    base = (remark or "").strip()
    tag = f"#{plan_id[:8]}" if plan_id else ""
    if not tag:
        return base
    if base.endswith(tag):
        return base
    return f"{base}{tag}" if base else tag


def _validate_positive_k(k: int | None, *, default: int | None = None) -> int | None:
    if k is None:
        return default
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError("每站互链数量 k 必须为大于 0 的整数")
    return k


def _same_root_domain(src, dst) -> bool:
    src_root = extract_root_domain(_site_url(src))
    dst_root = extract_root_domain(_site_url(dst))
    return bool(src_root and dst_root and src_root == dst_root)


def _plan_random_k_pairs(
    unique_sites: list,
    *,
    k: int | None,
    seed: int | None,
    avoid_reciprocal: bool,
    avoid_same_root_domain: bool,
) -> list[tuple[Any, Any]]:
    effective_k = _validate_positive_k(k, default=3)
    assert effective_k is not None
    rng = random.Random(seed)
    out_edges: dict[str, list[Any]] = {_site_id(site): [] for site in unique_sites}
    in_degree: dict[str, int] = {_site_id(site): 0 for site in unique_sites}
    edge_set: set[tuple[str, str]] = set()

    for _ in range(effective_k):
        order = list(unique_sites)
        rng.shuffle(order)
        for src in order:
            src_id = _site_id(src)
            if len(out_edges[src_id]) >= effective_k:
                continue
            candidates = []
            for dst in unique_sites:
                dst_id = _site_id(dst)
                if dst_id == src_id or (src_id, dst_id) in edge_set:
                    continue
                if avoid_reciprocal and (dst_id, src_id) in edge_set:
                    continue
                if avoid_same_root_domain and _same_root_domain(src, dst):
                    continue
                candidates.append(dst)
            if not candidates:
                continue
            min_in = min(in_degree[_site_id(d)] for d in candidates)
            least_linked = [d for d in candidates if in_degree[_site_id(d)] == min_in]
            chosen = rng.choice(least_linked)
            chosen_id = _site_id(chosen)
            edge_set.add((src_id, chosen_id))
            out_edges[src_id].append(chosen)
            in_degree[chosen_id] += 1

    pairs: list[tuple[Any, Any]] = []
    for src in unique_sites:
        for dst in out_edges[_site_id(src)]:
            pairs.append((src, dst))
    return pairs


def _resolve_group_selector(unique_sites: list, selector: str | Iterable[Any]) -> list:
    if isinstance(selector, str):
        target = selector.strip()
        return [s for s in unique_sites if _site_group(s) == target] if target else []
    items = list(selector)
    group_names = {x.strip() for x in items if isinstance(x, str) and x.strip()}
    site_ids = {_site_id(x) for x in items if not isinstance(x, str) and _site_id(x)}
    return [
        s
        for s in unique_sites
        if (_site_group(s) in group_names) or (_site_id(s) in site_ids)
    ]


def _has_group_selector(selector: str | Iterable[Any] | None) -> bool:
    if selector is None:
        return False
    if isinstance(selector, str):
        return bool(selector.strip())
    return True


def _select_capped_targets(
    eligible: list,
    *,
    src_pos: int,
    max_k: int | None,
    rng: random.Random | None,
    in_degree: dict[str, int],
) -> list:
    if max_k is None or len(eligible) <= max_k:
        for dst in eligible:
            dst_id = _site_id(dst)
            in_degree[dst_id] = in_degree.get(dst_id, 0) + 1
        return eligible

    if rng is not None:
        pool = list(eligible)
        chosen: list = []
        for _ in range(max_k):
            min_in = min(in_degree.get(_site_id(d), 0) for d in pool)
            best = [d for d in pool if in_degree.get(_site_id(d), 0) == min_in]
            pick = rng.choice(best)
            chosen.append(pick)
            pool.remove(pick)
            pick_id = _site_id(pick)
            in_degree[pick_id] = in_degree.get(pick_id, 0) + 1
        return chosen

    start = src_pos % len(eligible)
    chosen = [eligible[(start + offset) % len(eligible)] for offset in range(max_k)]
    for dst in chosen:
        dst_id = _site_id(dst)
        in_degree[dst_id] = in_degree.get(dst_id, 0) + 1
    return chosen


def _plan_group_cross_pairs(
    unique_sites: list,
    *,
    bidirectional: bool,
    k: int | None,
    seed: int | None,
    avoid_same_root_domain: bool,
    group_a: str | Iterable[Any] | None,
    group_b: str | Iterable[Any] | None,
) -> list[tuple[Any, Any]]:
    max_k = _validate_positive_k(k, default=None)
    has_a = _has_group_selector(group_a)
    has_b = _has_group_selector(group_b)
    rng = random.Random(seed) if seed is not None else None
    in_degree: dict[str, int] = {_site_id(site): 0 for site in unique_sites}

    src_targets: list[tuple[Any, list[Any]]] = []
    if has_a or has_b:
        if has_a and has_b:
            assert group_a is not None and group_b is not None
            sites_a = _resolve_group_selector(unique_sites, group_a)
            sites_b = _resolve_group_selector(unique_sites, group_b)
        elif has_a:
            assert group_a is not None
            sites_a = _resolve_group_selector(unique_sites, group_a)
            a_ids = {_site_id(s) for s in sites_a}
            sites_b = [s for s in unique_sites if _site_id(s) not in a_ids]
        else:
            assert group_b is not None
            sites_b = _resolve_group_selector(unique_sites, group_b)
            b_ids = {_site_id(s) for s in sites_b}
            sites_a = [s for s in unique_sites if _site_id(s) not in b_ids]

        if not sites_a or not sites_b or {_site_id(s) for s in sites_a} == {_site_id(s) for s in sites_b}:
            raise ValueError("分组互链的两个分组均不能为空且不能相同")

        for src in sites_a:
            src_id = _site_id(src)
            eligible = [
                dst
                for dst in sites_b
                if _site_id(dst) != src_id
                and not (avoid_same_root_domain and _same_root_domain(src, dst))
            ]
            src_targets.append((src, eligible))
        if bidirectional:
            for src in sites_b:
                src_id = _site_id(src)
                eligible = [
                    dst
                    for dst in sites_a
                    if _site_id(dst) != src_id
                    and not (avoid_same_root_domain and _same_root_domain(src, dst))
                ]
                src_targets.append((src, eligible))
    else:
        group_order: list[str] = []
        buckets: dict[str, list[Any]] = {}
        for site in unique_sites:
            grp = _site_group(site)
            if not grp:
                continue
            if grp not in buckets:
                group_order.append(grp)
                buckets[grp] = []
            buckets[grp].append(site)

        if len(group_order) < 2:
            raise ValueError("分组互链至少需要 2 个不同的站点分组")

        group_rank = {name: idx for idx, name in enumerate(group_order)}
        for grp in group_order:
            src_rank = group_rank[grp]
            for src in buckets[grp]:
                src_id = _site_id(src)
                eligible = []
                for other_grp in group_order:
                    other_rank = group_rank[other_grp]
                    if other_grp == grp:
                        continue
                    if not bidirectional and other_rank <= src_rank:
                        continue
                    for dst in buckets[other_grp]:
                        if _site_id(dst) == src_id:
                            continue
                        if avoid_same_root_domain and _same_root_domain(src, dst):
                            continue
                        eligible.append(dst)
                src_targets.append((src, eligible))

    pairs: list[tuple[Any, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for src_pos, (src, eligible) in enumerate(src_targets):
        if not eligible:
            continue
        selected = _select_capped_targets(
            eligible,
            src_pos=src_pos,
            max_k=max_k,
            rng=rng,
            in_degree=in_degree,
        )
        src_id = _site_id(src)
        for dst in selected:
            pair_key = (src_id, _site_id(dst))
            if pair_key not in seen_pairs:
                seen_pairs.add(pair_key)
                pairs.append((src, dst))
    return pairs


def plan_crosslink_items(
    sites,
    mode: str,
    *,
    hub_site_id: str = "",
    bidirectional: bool = True,
    nofollow: bool = False,
    remark: str = "站群互链",
    plan_id: str = "",
    k: int | None = None,
    seed: int | None = None,
    avoid_reciprocal: bool = True,
    avoid_same_root_domain: bool = True,
    group_a: str | Iterable[Any] | None = None,
    group_b: str | Iterable[Any] | None = None,
) -> tuple[CrossLinkItem, ...]:
    """Pure topology planner for ``mesh``, ``hub``, ``ring``, ``random_k`` and ``group_cross``."""
    if mode not in _VALID_CROSSLINK_MODES:
        raise ValueError(f"不支持的互链模式：{mode}")

    unique_sites: list = []
    seen_ids: set[str] = set()
    for site in sites or ():
        sid = _site_id(site)
        if sid and sid not in seen_ids:
            seen_ids.add(sid)
            unique_sites.append(site)

    n = len(unique_sites)
    if n < 2:
        raise ValueError("至少选择 2 个站点")

    pairs: list[tuple[Any, Any]] = []
    if mode == CROSSLINK_MESH:
        for src in unique_sites:
            for dst in unique_sites:
                if _site_id(src) != _site_id(dst):
                    pairs.append((src, dst))
    elif mode == CROSSLINK_HUB:
        hub = None
        for site in unique_sites:
            if hub_site_id and (_site_id(site) == hub_site_id or _site_name(site) == hub_site_id):
                hub = site
                break
        if hub is None:
            raise ValueError("主站必须包含在所选站点中")
        hub_id = _site_id(hub)
        for site in unique_sites:
            sid = _site_id(site)
            if sid == hub_id:
                if bidirectional:
                    for spoke in unique_sites:
                        if _site_id(spoke) != hub_id:
                            pairs.append((hub, spoke))
            else:
                pairs.append((site, hub))
    elif mode == CROSSLINK_RING:
        seen_pairs: set[tuple[str, str]] = set()
        for idx, src in enumerate(unique_sites):
            src_id = _site_id(src)
            nxt = unique_sites[(idx + 1) % n]
            nxt_id = _site_id(nxt)
            if (src_id, nxt_id) not in seen_pairs:
                if bidirectional or (nxt_id, src_id) not in seen_pairs:
                    seen_pairs.add((src_id, nxt_id))
                    pairs.append((src, nxt))
            if bidirectional:
                prv = unique_sites[(idx - 1) % n]
                prv_id = _site_id(prv)
                if (src_id, prv_id) not in seen_pairs:
                    seen_pairs.add((src_id, prv_id))
                    pairs.append((src, prv))
    elif mode == CROSSLINK_RANDOM_K:
        pairs = _plan_random_k_pairs(
            unique_sites,
            k=k,
            seed=seed,
            avoid_reciprocal=avoid_reciprocal,
            avoid_same_root_domain=avoid_same_root_domain,
        )
    elif mode == CROSSLINK_GROUP_CROSS:
        pairs = _plan_group_cross_pairs(
            unique_sites,
            bidirectional=bidirectional,
            k=k,
            seed=seed,
            avoid_same_root_domain=avoid_same_root_domain,
            group_a=group_a,
            group_b=group_b,
        )

    item_remark = _format_plan_remark(remark, plan_id)
    items: list[CrossLinkItem] = []
    for src, dst in pairs:
        src_link = _canonical_site_link(src)
        dst_link = _canonical_site_link(dst)
        spec = FriendLinkSpec(
            title=_pick_anchor_title(src, dst),
            link=dst_link,
            nofollow=bool(nofollow),
            my_title=_first_anchor_title(src),
            my_link=src_link,
            remark=item_remark,
        )
        items.append(
            CrossLinkItem(
                source_site_id=_site_id(src),
                source_name=_site_name(src),
                target_site_id=_site_id(dst),
                target_name=_site_name(dst),
                spec=spec,
                state=LINK_PLANNED,
                message="",
            )
        )
    return tuple(items)


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().casefold() in _NOFOLLOW_TRUE
    return False


def _coerce_optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _parse_friendlink_item(raw: Any) -> FriendLink | None:
    if not isinstance(raw, dict):
        return None
    link = str(raw.get("link") or raw.get("url") or "").strip()
    title = str(raw.get("title") or "").strip()
    item_id = _coerce_optional_int(raw.get("id"))
    return FriendLink(
        id=item_id if item_id is not None else 0,
        title=title,
        link=link,
        nofollow=_coerce_bool(raw.get("nofollow", False)),
        back_link=str(raw.get("back_link") or ""),
        my_title=str(raw.get("my_title") or ""),
        my_link=str(raw.get("my_link") or ""),
        contact=str(raw.get("contact") or ""),
        remark=str(raw.get("remark") or ""),
        status=_coerce_optional_int(raw.get("status")),
        status_text=str(raw.get("status_text") or raw.get("status_name") or ""),
        raw=dict(raw),
    )


_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _extract_friendlink_list(data: Any, *, _depth: int = 0) -> list[FriendLink]:
    if data is None:
        return []
    items_raw: Any = None
    if isinstance(data, list):
        if data and not any(isinstance(elem, dict) for elem in data):
            raise FriendLinkError("友链列表返回格式无效")
        items_raw = data
    elif isinstance(data, dict):
        if "code" in data:
            code = data.get("code")
            if type(code) not in {int, str} or code not in _SUCCESS_CODES:
                raw_msg = data.get("msg") or data.get("message") or ""
                msg = str(raw_msg).strip() or f"code={code!r}"
                hint = AUTH_HINT if _is_auth_message(msg) else ""
                raise FriendLinkError(f"友链接口返回错误：{msg}", hint=hint)
        for key in ("list", "items", "links", "data"):
            if key in data:
                candidate = data[key]
                if candidate is None:
                    return []
                if isinstance(candidate, list):
                    items_raw = candidate
                    break
                if key == "data" and isinstance(candidate, dict) and _depth < 1:
                    return _extract_friendlink_list(candidate, _depth=_depth + 1)
    if not isinstance(items_raw, list):
        raise FriendLinkError("友链列表返回格式无效")

    parsed: list[FriendLink] = []
    for raw in items_raw:
        item = _parse_friendlink_item(raw)
        if item is not None:
            parsed.append(item)
    return parsed


def _iter_json_candidates(text: str):
    stripped = text.strip()
    try:
        obj = json.loads(stripped)
        if isinstance(obj, (dict, list)):
            yield obj
    except ValueError:
        pass

    for match in _FENCED_JSON_RE.finditer(text):
        block = match.group(1).strip()
        if not block:
            continue
        try:
            obj = json.loads(block)
            if isinstance(obj, (dict, list)):
                yield obj
        except ValueError:
            continue

    decoder = json.JSONDecoder()
    idx = 0
    length = len(text)
    while idx < length:
        ch = text[idx]
        if ch in "{[":
            try:
                obj, end_idx = decoder.raw_decode(text, idx)
                if isinstance(obj, (dict, list)):
                    yield obj
                idx = max(idx + 1, end_idx)
                continue
            except ValueError:
                pass
        idx += 1


def _extract_mcp_friendlinks(result: Any) -> list[FriendLink]:
    structured = getattr(result, "structured", None)
    if structured is not None:
        return _extract_friendlink_list(structured)

    text = getattr(result, "text", None)
    if not isinstance(text, str) or not text.strip():
        raise FriendLinkError("MCP 返回的友链数据无法解析")

    for candidate in _iter_json_candidates(text):
        try:
            return _extract_friendlink_list(candidate)
        except FriendLinkError:
            continue

    raise FriendLinkError("MCP 返回的友链数据无法解析")


def _is_not_found_message(text: str) -> bool:
    lowered = (text or "").casefold()
    return any(needle in lowered for needle in ("不存在", "未找到", "not found", "does not exist"))


def _is_exists_message(text: str) -> bool:
    lowered = (text or "").casefold()
    return any(needle in lowered for needle in ("已存在", "重复", "already exists", "duplicate"))


def _is_auth_message(text: str) -> bool:
    lowered = (text or "").casefold()
    return any(needle in lowered for needle in ("token", "未开启", "无权", "认证", "授权", "unauthorized", "forbidden"))


def _format_error_message(exc: FriendLinkError, redactor: SecretRedactor | None = None) -> str:
    raw_msg = (exc.message or str(exc) or "").strip()
    raw_hint = (exc.hint or "").strip()
    msg = redactor(raw_msg) if redactor is not None else raw_msg
    hint = redactor(raw_hint) if redactor is not None else raw_hint
    if hint and hint not in msg:
        return f"{msg} （{hint}）" if msg else hint
    return msg or hint or "操作失败"


def _close_session(session: Any) -> None:
    close = getattr(session, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _is_cancelled(cancel_event: threading.Event | None) -> bool:
    return bool(cancel_event is not None and cancel_event.is_set())


@dataclass(frozen=True, slots=True)
class _BacklinkHit:
    nofollow: bool
    anchor_text: str


def _normalize_charset(name: str) -> str:
    cleaned = (name or "").strip().strip("'\"").casefold()
    if not cleaned:
        return ""
    if cleaned in {"gb2312", "gbk", "cp936", "ms936", "windows-936", "x-gbk"}:
        return "gb18030"
    if cleaned in {"utf8", "utf-8"}:
        return "utf-8"
    return cleaned


def _try_strict_decode(raw_bytes: bytes, encoding: str) -> str | None:
    if not encoding:
        return None
    try:
        return raw_bytes.decode(encoding, errors="strict")
    except LookupError:
        return None
    except UnicodeDecodeError as exc:
        if exc.start >= max(0, len(raw_bytes) - 3):
            try:
                return raw_bytes[:exc.start].decode(encoding, errors="strict")
            except (LookupError, UnicodeDecodeError):
                return None
        return None


def _decode_html_bytes(response: Any, raw_bytes: bytes) -> str:
    if not raw_bytes:
        return ""
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        decoded = _try_strict_decode(raw_bytes, "utf-8-sig")
        if decoded is not None:
            return decoded
    elif raw_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
        decoded = _try_strict_decode(raw_bytes, "utf-16")
        if decoded is not None:
            return decoded

    candidates: list[str] = []
    headers = getattr(response, "headers", None)
    if hasattr(headers, "items"):
        for key, value in headers.items():
            if str(key).casefold() == "content-type" and isinstance(value, str):
                match = _CHARSET_RE.search(value)
                if match:
                    norm = _normalize_charset(match.group(1))
                    if norm and norm not in candidates:
                        candidates.append(norm)
                break

    meta_match = _META_CHARSET_RE.search(raw_bytes[:4096])
    if meta_match:
        meta_name = meta_match.group(1).decode("ascii", errors="ignore")
        norm = _normalize_charset(meta_name)
        if norm and norm not in candidates:
            candidates.append(norm)

    for fallback in ("utf-8", "gb18030"):
        if fallback not in candidates:
            candidates.append(fallback)

    for enc in candidates:
        decoded = _try_strict_decode(raw_bytes, enc)
        if decoded is not None:
            return decoded

    for enc in candidates:
        try:
            return raw_bytes.decode(enc, errors="replace")
        except LookupError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def _host_matches_root(anchor_host: str, target_root: str) -> bool:
    if not anchor_host or not target_root:
        return False
    if anchor_host == target_root:
        return True
    if _is_ip_address(target_root) or target_root == "localhost":
        return False
    return anchor_host.endswith(f".{target_root}")


def _truncate_anchor_label(text: str, max_len: int = 32) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= max_len:
        return cleaned
    return f"{cleaned[:max_len]}…"


def _find_best_backlink(html: str, *, base_url: str, target_root: str) -> _BacklinkHit | None:
    if not html or not target_root:
        return None
    parser = _parse_html_anchors(html)
    best_hit: _BacklinkHit | None = None
    best_score: tuple[bool, bool] = (False, False)

    for anchor in parser.anchors:
        if not anchor.href:
            continue
        try:
            resolved = urljoin(base_url, anchor.href)
            parsed = urlsplit(resolved)
        except ValueError:
            continue
        if parsed.scheme.casefold() not in {"http", "https"}:
            continue
        anchor_host = _extract_normalized_host(resolved)
        if not _host_matches_root(anchor_host, target_root):
            continue

        nofollow = _parse_rel_nofollow(anchor.rel, default_nofollow=False)
        label = _truncate_anchor_label(anchor.text or anchor.title_attr or anchor.img_alt)
        score = (not nofollow, bool(label))
        if best_hit is None or score > best_score:
            best_hit = _BacklinkHit(nofollow=nofollow, anchor_text=label)
            best_score = score
            if best_score == (True, True):
                break
    return best_hit


def _homepage_fallback_url(url: str) -> str:
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return ""
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    if parsed.path in {"", "/"} and not parsed.query:
        return ""
    return urlunsplit((scheme, parsed.netloc, "/", "", ""))


class FriendLinkService:
    """Single-site, batch and cross-link operations for independent AnqiCMS sites."""

    def __init__(
        self,
        settings: IntegrationSettings | None = None,
        *,
        session_factory: Callable[[], Any] = requests.Session,
        mcp_client_factory: McpClientFactory | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.settings = settings or IntegrationSettings()
        self._session_factory = session_factory
        self._mcp_client_factory = mcp_client_factory
        self._sleep = sleep
        self._now = now

    # ------------------------------------------------------------------ #
    # Single-site operations
    # ------------------------------------------------------------------ #
    def list_links(self, site) -> list[FriendLink]:
        """Fetch all friend links from a single site; raises ``FriendLinkError``."""
        redactor = SecretRedactor.from_sites([site])
        session = self._session_factory()
        try:
            return self._list_links_with_session(site, session, redactor)
        finally:
            _close_session(session)

    def add_link(
        self,
        site,
        spec: FriendLinkSpec,
        *,
        existing: list[FriendLink] | None = None,
    ) -> LinkOpResult:
        """Add one friend link on ``site`` with self-skip and optional existing-skip."""
        redactor = SecretRedactor.from_sites([site])
        existing_keys = (
            {link_key(item.link) for item in existing if link_key(item.link)}
            if existing is not None
            else None
        )
        session = self._session_factory()
        try:
            return self._add_link_with_session(
                site,
                spec,
                session,
                existing_keys=existing_keys,
                redactor=redactor,
            )
        finally:
            _close_session(session)

    def remove_link(
        self,
        site,
        link_url: str,
        *,
        existing: list[FriendLink] | None = None,
    ) -> LinkOpResult:
        """Delete a friend link from ``site``; returns ``not_found`` when absent in ``existing``."""
        redactor = SecretRedactor.from_sites([site])
        session = self._session_factory()
        try:
            return self._remove_link_with_session(
                site,
                link_url,
                session,
                existing=existing,
                redactor=redactor,
            )
        finally:
            _close_session(session)

    def server_check(self, site) -> LinkOpResult:
        """Invoke ``/api/friendlink/check`` (best-effort; 404 yields a clear hint)."""
        redactor = SecretRedactor.from_sites([site])
        base = _site_url(site)
        token = import_token(site)
        if not base or not token:
            return LinkOpResult(
                site_name=_site_name(site),
                site_id=_site_id(site),
                action="check",
                link="",
                title="",
                status=LINK_FAILED,
                message=MISSING_SITE_CREDS_MSG,
            )

        session = self._session_factory()
        try:
            endpoint = f"{base}/api/friendlink/check"
            payload = self._rest_check_request(session, endpoint, token, redactor)
            msg = redactor(str(payload.get("msg") or "").strip()) or "已完成服务端友链检测"
            return LinkOpResult(
                site_name=_site_name(site),
                site_id=_site_id(site),
                action="check",
                link="",
                title="",
                status="checked",
                message=msg,
            )
        except FriendLinkError as exc:
            return LinkOpResult(
                site_name=_site_name(site),
                site_id=_site_id(site),
                action="check",
                link="",
                title="",
                status=LINK_FAILED,
                message=_format_error_message(exc, redactor),
            )
        finally:
            _close_session(session)

    @staticmethod
    def _classify_backlink_health(health: LinkHealth) -> str:
        if not health.reachable:
            return "unreachable"
        if health.has_backlink is True:
            if (health.message or "").startswith("有反链(nofollow"):
                return "nofollow"
            return "ok"
        return "missing"

    def _check_one_backlink_item(
        self,
        site_name: str,
        item: FriendLink | str,
        *,
        my_root: str,
        timeout: tuple[float, float],
        redactor: SecretRedactor,
        cancel_event: threading.Event | None,
        session: Any | None = None,
    ) -> LinkHealth:
        if isinstance(item, FriendLink):
            display_link = item.link
            explicit_back_link = item.back_link.strip()
            fetch_url = explicit_back_link or item.link.strip()
        else:
            display_link = str(item)
            explicit_back_link = ""
            fetch_url = display_link.strip()

        if not fetch_url:
            return LinkHealth(
                site_name=site_name,
                link=display_link,
                reachable=False,
                has_backlink=False,
                http_status=None,
                message="网址为空",
            )

        own_session = session is None
        active_session = self._session_factory() if own_session else session
        try:
            return self._probe_backlink(
                active_session,
                site_name=site_name,
                display_link=display_link,
                fetch_url=fetch_url,
                my_root=my_root,
                allow_homepage_fallback=not explicit_back_link,
                timeout=timeout,
                redactor=redactor,
                cancel_event=cancel_event,
            )
        finally:
            if own_session:
                _close_session(active_session)

    def _run_check_backlinks(
        self,
        site,
        links: Iterable[FriendLink | str] | None = None,
        *,
        max_workers: int = 6,
        cancel_event: threading.Event | None = None,
        on_result: Callable[[int, LinkHealth], None] | None = None,
    ) -> list[LinkHealth]:
        redactor = SecretRedactor.from_sites([site])
        target_links = list(self.list_links(site) if links is None else links)
        if not target_links or _is_cancelled(cancel_event):
            return []

        my_root = extract_root_domain(_site_url(site))
        check_timeout = self.settings.friendlink.check_timeout
        timeout = (min(self.settings.mcp.connect_timeout, check_timeout), check_timeout)
        sname = _site_name(site)
        worker_count = max(1, min(int(max_workers or 1), len(target_links)))

        if worker_count == 1:
            results: list[LinkHealth] = []
            session = self._session_factory()
            try:
                for idx, item in enumerate(target_links):
                    if _is_cancelled(cancel_event):
                        break
                    health = self._check_one_backlink_item(
                        sname,
                        item,
                        my_root=my_root,
                        timeout=timeout,
                        redactor=redactor,
                        cancel_event=cancel_event,
                        session=session,
                    )
                    results.append(health)
                    if on_result is not None:
                        on_result(idx, health)
            finally:
                _close_session(session)
            return results

        def _worker(idx_item: tuple[int, FriendLink | str]) -> LinkHealth | None:
            idx, item = idx_item
            if _is_cancelled(cancel_event):
                return None
            health = self._check_one_backlink_item(
                sname,
                item,
                my_root=my_root,
                timeout=timeout,
                redactor=redactor,
                cancel_event=cancel_event,
                session=None,
            )
            if on_result is not None:
                on_result(idx, health)
            return health

        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = [pool.submit(_worker, pair) for pair in enumerate(target_links)]
            results = []
            for future in futures:
                health = future.result()
                if health is not None:
                    results.append(health)
            return results

    def check_backlinks(
        self,
        site,
        links: Iterable[FriendLink | str] | None = None,
        *,
        max_workers: int = 6,
        cancel_event: threading.Event | None = None,
    ) -> list[LinkHealth]:
        """Locally fetch each link's ``back_link`` (or ``link``) and inspect anchors for our root domain."""
        return self._run_check_backlinks(
            site,
            links,
            max_workers=max_workers,
            cancel_event=cancel_event,
        )

    def batch_check_backlinks(
        self,
        sites,
        *,
        max_site_workers: int = 6,
        max_link_workers: int = 4,
        whitelist_domains: Iterable[str] = (),
        progress: Callable[[dict[str, Any]], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Concurrently fetch live links across ``sites`` and audit backlink health."""
        site_list = list(sites or ())
        summary = {
            "total": 0,
            "ok": 0,
            "nofollow": 0,
            "missing": 0,
            "unreachable": 0,
            "whitelisted": 0,
        }
        if not site_list:
            return {"items": [], "site_errors": {}, "summary": summary}

        whitelist_roots = {
            root
            for raw in (whitelist_domains or ())
            if (root := extract_root_domain(str(raw)))
        }
        redactor = SecretRedactor.from_sites(site_list)
        emit_lock = threading.Lock()

        def _emit_item(entry: dict[str, Any]) -> None:
            if progress is not None:
                with emit_lock:
                    progress(entry)

        def _make_item(
            site,
            link_obj: FriendLink,
            *,
            reachable: bool,
            has_backlink: bool | None,
            http_status: int | None,
            status: str,
            message: str,
        ) -> dict[str, Any]:
            return {
                "site_name": _site_name(site),
                "site_id": _site_id(site),
                "site_url": _site_url(site),
                "title": link_obj.title,
                "link": link_obj.link,
                "reachable": reachable,
                "has_backlink": has_backlink,
                "http_status": http_status,
                "status": status,
                "issue_kind": status,
                "message": message,
            }

        def _site_worker(site) -> tuple[str, list[dict[str, Any]], str | None]:
            site_key = _site_name(site) or _site_id(site)
            if _is_cancelled(cancel_event):
                return site_key, [], None
            if not self._has_read_transport(site):
                return site_key, [], MISSING_SITE_CREDS_MSG

            session = self._session_factory()
            try:
                try:
                    live_links = self._list_links_with_session(site, session, redactor)
                except FriendLinkError as exc:
                    return site_key, [], _format_error_message(exc, redactor)
            finally:
                _close_session(session)

            if not live_links or _is_cancelled(cancel_event):
                return site_key, [], None

            ordered_items: list[dict[str, Any] | None] = [None] * len(live_links)
            check_indices: list[int] = []
            check_links: list[FriendLink] = []

            for idx, link_obj in enumerate(live_links):
                link_root = extract_root_domain(link_obj.link)
                if link_root and link_root in whitelist_roots:
                    entry = _make_item(
                        site,
                        link_obj,
                        reachable=True,
                        has_backlink=None,
                        http_status=None,
                        status="whitelisted",
                        message=f"白名单域名({link_root})，已跳过",
                    )
                    ordered_items[idx] = entry
                    _emit_item(entry)
                else:
                    check_indices.append(idx)
                    check_links.append(link_obj)

            if check_links and not _is_cancelled(cancel_event):
                def _on_health(sub_idx: int, health: LinkHealth) -> None:
                    orig_idx = check_indices[sub_idx]
                    link_obj = live_links[orig_idx]
                    status = self._classify_backlink_health(health)
                    entry = _make_item(
                        site,
                        link_obj,
                        reachable=health.reachable,
                        has_backlink=health.has_backlink,
                        http_status=health.http_status,
                        status=status,
                        message=health.message,
                    )
                    ordered_items[orig_idx] = entry
                    _emit_item(entry)

                self._run_check_backlinks(
                    site,
                    check_links,
                    max_workers=max_link_workers,
                    cancel_event=cancel_event,
                    on_result=_on_health,
                )

            return site_key, [entry for entry in ordered_items if entry is not None], None

        site_workers = max(1, min(int(max_site_workers or 1), len(site_list)))
        if site_workers == 1:
            raw_results = [_site_worker(site) for site in site_list]
        else:
            with ThreadPoolExecutor(max_workers=site_workers) as pool:
                futures = [pool.submit(_site_worker, site) for site in site_list]
                raw_results = [future.result() for future in futures]

        items: list[dict[str, Any]] = []
        site_errors: dict[str, str] = {}
        for site_key, site_items, err in raw_results:
            if err:
                site_errors[site_key] = err
            for entry in site_items:
                items.append(entry)
                st = entry["status"]
                if st in summary:
                    summary[st] += 1
        summary["total"] = len(items)
        return {"items": items, "site_errors": site_errors, "summary": summary}

    def search_links_across_sites(
        self,
        sites,
        query: str = "",
        *,
        match_root_domain: bool = True,
        max_workers: int = 10,
        progress: Callable[[dict[str, Any]], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Concurrently fetch live friend links from ``sites`` and filter by ``query``."""
        site_list = list(sites or ())
        if not site_list:
            return {"matches": [], "site_counts": {}, "site_errors": {}, "total_links": 0}

        raw_query = (query or "").strip()
        q_lower = raw_query.casefold()
        q_root = extract_root_domain(raw_query) if (raw_query and match_root_domain) else ""
        redactor = SecretRedactor.from_sites(site_list)
        emit_lock = threading.Lock()

        def _matches_link(link_obj: FriendLink) -> bool:
            if not q_lower:
                return True
            if q_root and extract_root_domain(link_obj.link) == q_root:
                return True
            return (
                q_lower in (link_obj.link or "").casefold()
                or q_lower in (link_obj.title or "").casefold()
                or q_lower in (link_obj.remark or "").casefold()
            )

        def _site_worker(site) -> tuple[str, list[dict[str, Any]], int | None, str | None]:
            site_key = _site_name(site) or _site_id(site)
            if _is_cancelled(cancel_event):
                return site_key, [], None, None
            if not self._has_read_transport(site):
                if progress is not None:
                    with emit_lock:
                        progress({"site_name": site_key, "total": 0, "matched": 0, "error": MISSING_SITE_CREDS_MSG})
                return site_key, [], None, MISSING_SITE_CREDS_MSG

            session = self._session_factory()
            try:
                try:
                    links = self._list_links_with_session(site, session, redactor)
                except FriendLinkError as exc:
                    err_msg = _format_error_message(exc, redactor)
                    if progress is not None:
                        with emit_lock:
                            progress({"site_name": site_key, "total": 0, "matched": 0, "error": err_msg})
                    return site_key, [], None, err_msg
            finally:
                _close_session(session)

            matched_items: list[dict[str, Any]] = []
            sname = _site_name(site)
            sid = _site_id(site)
            surl = _site_url(site)
            for link_obj in links:
                if _matches_link(link_obj):
                    matched_items.append(
                        {
                            "site_name": sname,
                            "site_id": sid,
                            "site_url": surl,
                            "id": link_obj.id,
                            "title": link_obj.title,
                            "link": link_obj.link,
                            "root_domain": extract_root_domain(link_obj.link),
                            "nofollow": link_obj.nofollow,
                            "back_link": link_obj.back_link,
                            "remark": link_obj.remark,
                            "status": link_obj.status,
                            "status_text": link_obj.status_text,
                        }
                    )

            if progress is not None:
                with emit_lock:
                    progress(
                        {
                            "site_name": site_key,
                            "total": len(links),
                            "matched": len(matched_items),
                            "error": None,
                        }
                    )
            return site_key, matched_items, len(links), None

        worker_count = max(1, min(int(max_workers or 1), len(site_list)))
        if worker_count == 1:
            raw_results = [_site_worker(site) for site in site_list]
        else:
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                futures = [pool.submit(_site_worker, site) for site in site_list]
                raw_results = [future.result() for future in futures]

        matches: list[dict[str, Any]] = []
        site_counts: dict[str, int] = {}
        site_errors: dict[str, str] = {}
        total_links = 0
        for site_key, site_matches, count, err in raw_results:
            if err:
                site_errors[site_key] = err
            elif count is not None:
                site_counts[site_key] = count
                total_links += count
            matches.extend(site_matches)

        return {
            "matches": matches,
            "site_counts": site_counts,
            "site_errors": site_errors,
            "total_links": total_links,
        }

    # ------------------------------------------------------------------ #
    # Batch operations (统一上下链)
    # ------------------------------------------------------------------ #
    def batch_add(
        self,
        sites,
        specs: Iterable[FriendLinkSpec],
        *,
        skip_existing: bool = True,
        progress: Callable[[LinkOpResult], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> list[LinkOpResult]:
        """Add ``specs`` to every site in ``sites`` with bounded concurrency."""
        site_list = list(sites or ())
        deduped_specs = self._dedupe_specs(specs)
        if not site_list or not deduped_specs:
            return []

        redactor = SecretRedactor.from_sites(site_list)

        def _worker(site) -> list[LinkOpResult]:
            out: list[LinkOpResult] = []
            if _is_cancelled(cancel_event):
                for spec in deduped_specs:
                    res = self._cancelled_op(site, "add", spec.link, spec.title)
                    out.append(res)
                    self._emit(progress, res)
                return out

            if not self._has_transport(site):
                for spec in deduped_specs:
                    res = LinkOpResult(
                        site_name=_site_name(site),
                        site_id=_site_id(site),
                        action="add",
                        link=spec.link,
                        title=spec.title,
                        status=LINK_FAILED,
                        message=MISSING_SITE_CREDS_MSG,
                    )
                    out.append(res)
                    self._emit(progress, res)
                return out

            session = self._session_factory()
            try:
                existing_keys: set[str] | None = None
                if skip_existing:
                    try:
                        existing = self._list_links_with_session(site, session, redactor)
                    except FriendLinkError as exc:
                        msg = _format_error_message(exc, redactor)
                        for spec in deduped_specs:
                            res = LinkOpResult(
                                site_name=_site_name(site),
                                site_id=_site_id(site),
                                action="add",
                                link=spec.link,
                                title=spec.title,
                                status=LINK_FAILED,
                                message=msg,
                            )
                            out.append(res)
                            self._emit(progress, res)
                        return out

                    existing_keys = {link_key(item.link) for item in existing if link_key(item.link)}

                total = len(deduped_specs)
                for idx, spec in enumerate(deduped_specs):
                    if _is_cancelled(cancel_event):
                        res = self._cancelled_op(site, "add", spec.link, spec.title)
                        out.append(res)
                        self._emit(progress, res)
                        continue

                    res = self._add_link_with_session(
                        site,
                        spec,
                        session,
                        existing_keys=existing_keys,
                        redactor=redactor,
                    )
                    if res.status == LINK_CREATED and existing_keys is not None:
                        key = link_key(res.link)
                        if key:
                            existing_keys.add(key)
                    out.append(res)
                    self._emit(progress, res)

                    if (
                        res.status == LINK_CREATED
                        and idx + 1 < total
                        and self.settings.friendlink.per_request_delay_s > 0
                        and not _is_cancelled(cancel_event)
                    ):
                        self._sleep(self.settings.friendlink.per_request_delay_s)
                return out
            finally:
                _close_session(session)

        return self._run_per_site(site_list, _worker)

    def batch_remove(
        self,
        sites,
        link_urls: Iterable[str],
        *,
        progress: Callable[[LinkOpResult], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> list[LinkOpResult]:
        """Remove ``link_urls`` from every site in ``sites`` with bounded concurrency."""
        site_list = list(sites or ())
        deduped_urls = self._dedupe_urls(link_urls)
        if not site_list or not deduped_urls:
            return []

        redactor = SecretRedactor.from_sites(site_list)

        def _worker(site) -> list[LinkOpResult]:
            out: list[LinkOpResult] = []
            if _is_cancelled(cancel_event):
                for url in deduped_urls:
                    res = self._cancelled_op(site, "remove", url, "")
                    out.append(res)
                    self._emit(progress, res)
                return out

            if not self._has_transport(site):
                for url in deduped_urls:
                    res = LinkOpResult(
                        site_name=_site_name(site),
                        site_id=_site_id(site),
                        action="remove",
                        link=url,
                        title="",
                        status=LINK_FAILED,
                        message=MISSING_SITE_CREDS_MSG,
                    )
                    out.append(res)
                    self._emit(progress, res)
                return out

            session = self._session_factory()
            try:
                try:
                    existing = list(self._list_links_with_session(site, session, redactor))
                except FriendLinkError as exc:
                    msg = _format_error_message(exc, redactor)
                    for url in deduped_urls:
                        res = LinkOpResult(
                            site_name=_site_name(site),
                            site_id=_site_id(site),
                            action="remove",
                            link=url,
                            title="",
                            status=LINK_FAILED,
                            message=msg,
                        )
                        out.append(res)
                        self._emit(progress, res)
                    return out

                total = len(deduped_urls)
                for idx, url in enumerate(deduped_urls):
                    if _is_cancelled(cancel_event):
                        res = self._cancelled_op(site, "remove", url, "")
                        out.append(res)
                        self._emit(progress, res)
                        continue

                    res = self._remove_link_with_session(
                        site,
                        url,
                        session,
                        existing=existing,
                        redactor=redactor,
                    )
                    if res.status == LINK_DELETED:
                        target_key = link_key(url)
                        existing = [
                            item
                            for item in existing
                            if not (
                                (target_key and link_key(item.link) == target_key)
                                or item.link.strip() == url.strip()
                            )
                        ]
                    out.append(res)
                    self._emit(progress, res)

                    if (
                        res.status == LINK_DELETED
                        and idx + 1 < total
                        and self.settings.friendlink.per_request_delay_s > 0
                        and not _is_cancelled(cancel_event)
                    ):
                        self._sleep(self.settings.friendlink.per_request_delay_s)
                return out
            finally:
                _close_session(session)

        return self._run_per_site(site_list, _worker)

    # ------------------------------------------------------------------ #
    # Cross-link planning, preview, execution, rollback, unlink
    # ------------------------------------------------------------------ #
    def plan_crosslinks(
        self,
        sites,
        mode: str,
        *,
        hub_site_id: str = "",
        bidirectional: bool = True,
        nofollow: bool = False,
        remark: str = "",
        k: int | None = None,
        seed: int | None = None,
        avoid_reciprocal: bool = True,
        avoid_same_root_domain: bool = True,
        group_a: str | Iterable[Any] | None = None,
        group_b: str | Iterable[Any] | None = None,
    ) -> CrossLinkPlan:
        """Build a pure cross-link plan without touching the network."""
        plan_id = uuid.uuid4().hex[:12]
        created_at = self._now().isoformat(timespec="seconds")
        effective_remark = remark if remark != "" else self.settings.friendlink.default_remark

        resolved_hub_id = ""
        if mode == CROSSLINK_HUB:
            for site in sites or ():
                if hub_site_id and (_site_id(site) == hub_site_id or _site_name(site) == hub_site_id):
                    resolved_hub_id = _site_id(site)
                    break

        items = plan_crosslink_items(
            sites,
            mode,
            hub_site_id=hub_site_id,
            bidirectional=bidirectional,
            nofollow=nofollow,
            remark=effective_remark,
            plan_id=plan_id,
            k=k,
            seed=seed,
            avoid_reciprocal=avoid_reciprocal,
            avoid_same_root_domain=avoid_same_root_domain,
            group_a=group_a,
            group_b=group_b,
        )
        return CrossLinkPlan(
            plan_id=plan_id,
            mode=mode,
            created_at=created_at,
            hub_site_id=resolved_hub_id if mode == CROSSLINK_HUB else "",
            bidirectional=bool(bidirectional),
            items=items,
        )

    def preview_plan(
        self,
        plan: CrossLinkPlan,
        sites,
        *,
        existing_out: dict[str, list[FriendLink]] | None = None,
        progress: Callable[[CrossLinkItem], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> CrossLinkPlan:
        """Dry-run ``plan``: list each source site once and mark existing/self/failed items."""
        if not plan.items:
            return plan

        site_list = list(sites or ())
        site_map = self._index_sites(site_list)
        redactor = SecretRedactor.from_sites(site_list)
        groups = self._group_indexed_items(plan.items, lambda item: item.source_site_id)
        cache_lock = threading.Lock()

        updated: list[CrossLinkItem | None] = [None] * len(plan.items)

        def _worker(group: tuple[str, list[tuple[int, CrossLinkItem]]]) -> list[tuple[int, CrossLinkItem]]:
            source_id, indexed = group
            results: list[tuple[int, CrossLinkItem]] = []
            if _is_cancelled(cancel_event):
                for idx, item in indexed:
                    new_item = replace(item, state=LINK_CANCELLED, message="已取消")
                    results.append((idx, new_item))
                    self._emit(progress, new_item)
                return results

            site = site_map.get(source_id) or site_map.get(indexed[0][1].source_name)
            if site is None:
                for idx, item in indexed:
                    new_item = replace(item, state=LINK_FAILED, message="未找到源站点配置")
                    results.append((idx, new_item))
                    self._emit(progress, new_item)
                return results

            if not self._has_transport(site):
                for idx, item in indexed:
                    new_item = replace(item, state=LINK_FAILED, message=MISSING_SITE_CREDS_MSG)
                    results.append((idx, new_item))
                    self._emit(progress, new_item)
                return results

            session = self._session_factory()
            try:
                try:
                    existing = self._list_links_with_session(site, session, redactor)
                except FriendLinkError as exc:
                    msg = _format_error_message(exc, redactor)
                    for idx, item in indexed:
                        new_item = replace(item, state=LINK_FAILED, message=msg)
                        results.append((idx, new_item))
                        self._emit(progress, new_item)
                    return results

                if existing_out is not None:
                    with cache_lock:
                        existing_out[source_id] = list(existing)

                source_origin = site_origin_key(_site_url(site))
                existing_keys = {link_key(link.link) for link in existing if link_key(link.link)}
                for idx, item in indexed:
                    target_origin = site_origin_key(item.spec.link)
                    if source_origin and target_origin and source_origin == target_origin:
                        new_item = replace(item, state=LINK_SKIPPED_SELF, message="跳过本站域名")
                    elif link_key(item.spec.link) in existing_keys:
                        new_item = replace(item, state=LINK_SKIPPED_EXISTS, message="目标友链已存在")
                    else:
                        new_item = replace(item, state=LINK_PLANNED, message="待创建")
                    results.append((idx, new_item))
                    self._emit(progress, new_item)
                return results
            finally:
                _close_session(session)

        for pair_list in self._run_groups(groups, _worker):
            for idx, new_item in pair_list:
                updated[idx] = new_item

        return replace(plan, items=tuple(item for item in updated if item is not None))

    def execute_plan(
        self,
        plan: CrossLinkPlan,
        sites,
        *,
        ledger: FriendLinkLedger | None = None,
        existing_by_site: Mapping[str, list[FriendLink]] | None = None,
        progress: Callable[[LinkOpResult], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> CrossLinkReport:
        """Execute ``plan`` by listing each source site (unless cached), creating links and writing ledger."""
        if ledger is not None:
            ledger.record_plan(plan)

        if not plan.items:
            return CrossLinkReport(plan=plan, results=())

        site_list = list(sites or ())
        site_map = self._index_sites(site_list)
        redactor = SecretRedactor.from_sites(site_list)
        groups = self._group_indexed_items(plan.items, lambda item: item.source_site_id)

        updated_items: list[CrossLinkItem | None] = [None] * len(plan.items)
        ordered_results: list[LinkOpResult | None] = [None] * len(plan.items)

        def _lookup_cached_existing(source_id: str, site, fallback_name: str) -> list[FriendLink] | None:
            if existing_by_site is None:
                return None
            for key in (source_id, _site_id(site), _site_name(site), fallback_name, _site_url(site)):
                if key and key in existing_by_site:
                    return list(existing_by_site[key])
            return None

        def _worker(
            group: tuple[str, list[tuple[int, CrossLinkItem]]],
        ) -> list[tuple[int, CrossLinkItem, LinkOpResult]]:
            source_id, indexed = group
            out: list[tuple[int, CrossLinkItem, LinkOpResult]] = []
            ledger_batch: list[LedgerEntry] = []

            if _is_cancelled(cancel_event):
                for idx, item in indexed:
                    res = LinkOpResult(
                        site_name=item.source_name,
                        site_id=item.source_site_id,
                        action="add",
                        link=item.spec.link,
                        title=item.spec.title,
                        status=LINK_CANCELLED,
                        message="已取消",
                    )
                    out.append((idx, replace(item, state=LINK_CANCELLED, message="已取消"), res))
                    self._emit(progress, res)
                return out

            site = site_map.get(source_id) or site_map.get(indexed[0][1].source_name)
            if site is None:
                for idx, item in indexed:
                    res = LinkOpResult(
                        site_name=item.source_name,
                        site_id=item.source_site_id,
                        action="add",
                        link=item.spec.link,
                        title=item.spec.title,
                        status=LINK_FAILED,
                        message="未找到源站点配置",
                    )
                    out.append((idx, replace(item, state=LINK_FAILED, message=res.message), res))
                    self._emit(progress, res)
                return out

            if not self._has_transport(site):
                for idx, item in indexed:
                    res = LinkOpResult(
                        site_name=_site_name(site),
                        site_id=_site_id(site),
                        action="add",
                        link=item.spec.link,
                        title=item.spec.title,
                        status=LINK_FAILED,
                        message=MISSING_SITE_CREDS_MSG,
                    )
                    out.append((idx, replace(item, state=LINK_FAILED, message=res.message), res))
                    self._emit(progress, res)
                return out

            session = self._session_factory()
            try:
                existing = _lookup_cached_existing(source_id, site, indexed[0][1].source_name)
                if existing is None:
                    try:
                        existing = self._list_links_with_session(site, session, redactor)
                    except FriendLinkError as exc:
                        msg = _format_error_message(exc, redactor)
                        for idx, item in indexed:
                            res = LinkOpResult(
                                site_name=_site_name(site),
                                site_id=_site_id(site),
                                action="add",
                                link=item.spec.link,
                                title=item.spec.title,
                                status=LINK_FAILED,
                                message=msg,
                            )
                            out.append((idx, replace(item, state=LINK_FAILED, message=msg), res))
                            self._emit(progress, res)
                        return out

                existing_keys = {link_key(link.link) for link in existing if link_key(link.link)}
                total = len(indexed)
                for pos, (idx, item) in enumerate(indexed):
                    if _is_cancelled(cancel_event):
                        res = self._cancelled_op(site, "add", item.spec.link, item.spec.title)
                        out.append((idx, replace(item, state=LINK_CANCELLED, message=res.message), res))
                        self._emit(progress, res)
                        continue

                    res = self._add_link_with_session(
                        site,
                        item.spec,
                        session,
                        existing_keys=existing_keys,
                        redactor=redactor,
                    )
                    if res.status == LINK_CREATED:
                        key = link_key(res.link)
                        if key:
                            existing_keys.add(key)

                    ledger_batch.append(
                        LedgerEntry(
                            plan_id=plan.plan_id,
                            source_site_id=item.source_site_id,
                            source_name=item.source_name,
                            target_site_id=item.target_site_id,
                            link=res.link,
                            title=res.title,
                            status=res.status,
                            created_at=self._now().isoformat(timespec="seconds"),
                        )
                    )
                    out.append((idx, replace(item, state=res.status, message=res.message), res))
                    self._emit(progress, res)

                    if (
                        res.status == LINK_CREATED
                        and pos + 1 < total
                        and self.settings.friendlink.per_request_delay_s > 0
                        and not _is_cancelled(cancel_event)
                    ):
                        self._sleep(self.settings.friendlink.per_request_delay_s)

                if ledger is not None and ledger_batch:
                    ledger.record_results(plan.plan_id, ledger_batch)
                return out
            finally:
                _close_session(session)

        for chunk in self._run_groups(groups, _worker):
            for idx, new_item, op_res in chunk:
                updated_items[idx] = new_item
                ordered_results[idx] = op_res

        final_plan = replace(plan, items=tuple(i for i in updated_items if i is not None))
        final_results = tuple(r for r in ordered_results if r is not None)
        return CrossLinkReport(plan=final_plan, results=final_results)

    def rollback_plan(
        self,
        plan_id: str,
        sites,
        *,
        ledger: FriendLinkLedger,
        progress: Callable[[LinkOpResult], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> list[LinkOpResult]:
        """Delete ONLY links recorded as ``created`` (and not rolled back) in ``ledger``."""
        entries = ledger.created_links(plan_id)
        if not entries:
            return []

        site_list = list(sites or ())
        site_map = self._index_sites(site_list)
        redactor = SecretRedactor.from_sites(site_list)
        groups = self._group_indexed_items(entries, lambda entry: entry.source_site_id)

        ordered_results: list[LinkOpResult | None] = [None] * len(entries)

        def _worker(
            group: tuple[str, list[tuple[int, LedgerEntry]]],
        ) -> list[tuple[int, LinkOpResult]]:
            source_id, indexed = group
            out: list[tuple[int, LinkOpResult]] = []

            if _is_cancelled(cancel_event):
                for idx, entry in indexed:
                    res = LinkOpResult(
                        site_name=entry.source_name,
                        site_id=entry.source_site_id,
                        action="remove",
                        link=entry.link,
                        title=entry.title,
                        status=LINK_CANCELLED,
                        message="已取消",
                    )
                    out.append((idx, res))
                    self._emit(progress, res)
                return out

            site = site_map.get(source_id) or site_map.get(indexed[0][1].source_name)
            if site is None:
                for idx, entry in indexed:
                    res = LinkOpResult(
                        site_name=entry.source_name,
                        site_id=entry.source_site_id,
                        action="remove",
                        link=entry.link,
                        title=entry.title,
                        status=LINK_FAILED,
                        message="未找到源站点配置",
                    )
                    out.append((idx, res))
                    self._emit(progress, res)
                return out

            if not self._has_transport(site):
                for idx, entry in indexed:
                    res = LinkOpResult(
                        site_name=_site_name(site),
                        site_id=_site_id(site),
                        action="remove",
                        link=entry.link,
                        title=entry.title,
                        status=LINK_FAILED,
                        message=MISSING_SITE_CREDS_MSG,
                    )
                    out.append((idx, res))
                    self._emit(progress, res)
                return out

            session = self._session_factory()
            try:
                try:
                    existing = list(self._list_links_with_session(site, session, redactor))
                except FriendLinkError as exc:
                    msg = _format_error_message(exc, redactor)
                    for idx, entry in indexed:
                        res = LinkOpResult(
                            site_name=_site_name(site),
                            site_id=_site_id(site),
                            action="remove",
                            link=entry.link,
                            title=entry.title,
                            status=LINK_FAILED,
                            message=msg,
                        )
                        out.append((idx, res))
                        self._emit(progress, res)
                    return out

                total = len(indexed)
                for pos, (idx, entry) in enumerate(indexed):
                    if _is_cancelled(cancel_event):
                        res = self._cancelled_op(site, "remove", entry.link, entry.title)
                        out.append((idx, res))
                        self._emit(progress, res)
                        continue

                    res = self._remove_link_with_session(
                        site,
                        entry.link,
                        session,
                        existing=existing,
                        redactor=redactor,
                        fallback_title=entry.title,
                    )
                    if res.status in {LINK_DELETED, LINK_NOT_FOUND}:
                        ledger.mark_rolled_back(plan_id, entry.source_site_id, entry.link)
                        if res.status == LINK_DELETED:
                            target_key = link_key(entry.link)
                            existing = [
                                item
                                for item in existing
                                if not (
                                    (target_key and link_key(item.link) == target_key)
                                    or item.link.strip() == entry.link.strip()
                                )
                            ]

                    out.append((idx, res))
                    self._emit(progress, res)

                    if (
                        res.status == LINK_DELETED
                        and pos + 1 < total
                        and self.settings.friendlink.per_request_delay_s > 0
                        and not _is_cancelled(cancel_event)
                    ):
                        self._sleep(self.settings.friendlink.per_request_delay_s)
                return out
            finally:
                _close_session(session)

        for chunk in self._run_groups(groups, _worker):
            for idx, op_res in chunk:
                ordered_results[idx] = op_res

        return [r for r in ordered_results if r is not None]

    def unlink_between(
        self,
        sites,
        *,
        progress: Callable[[LinkOpResult], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> list[LinkOpResult]:
        """On each site A, delete links whose origin matches another selected site B."""
        site_list = list(sites or ())
        if len(site_list) < 2:
            return []

        redactor = SecretRedactor.from_sites(site_list)
        selected_origins = {
            site_origin_key(_site_url(site))
            for site in site_list
            if site_origin_key(_site_url(site))
        }

        def _worker(site) -> list[LinkOpResult]:
            out: list[LinkOpResult] = []
            if _is_cancelled(cancel_event):
                res = self._cancelled_op(site, "remove", "", "")
                out.append(res)
                self._emit(progress, res)
                return out

            if not self._has_transport(site):
                res = LinkOpResult(
                    site_name=_site_name(site),
                    site_id=_site_id(site),
                    action="remove",
                    link="",
                    title="",
                    status=LINK_FAILED,
                    message=MISSING_SITE_CREDS_MSG,
                )
                out.append(res)
                self._emit(progress, res)
                return out

            session = self._session_factory()
            try:
                try:
                    existing = list(self._list_links_with_session(site, session, redactor))
                except FriendLinkError as exc:
                    res = LinkOpResult(
                        site_name=_site_name(site),
                        site_id=_site_id(site),
                        action="remove",
                        link="",
                        title="",
                        status=LINK_FAILED,
                        message=_format_error_message(exc, redactor),
                    )
                    out.append(res)
                    self._emit(progress, res)
                    return out

                own_origin = site_origin_key(_site_url(site))
                other_origins = selected_origins - ({own_origin} if own_origin else set())

                targets: list[FriendLink] = []
                seen_keys: set[str] = set()
                for item in existing:
                    origin = site_origin_key(item.link)
                    if not origin or origin not in other_origins:
                        continue
                    key = link_key(item.link) or item.link.strip()
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    targets.append(item)

                total = len(targets)
                for idx, target in enumerate(targets):
                    if _is_cancelled(cancel_event):
                        res = self._cancelled_op(site, "remove", target.link, target.title)
                        out.append(res)
                        self._emit(progress, res)
                        continue

                    res = self._remove_link_with_session(
                        site,
                        target.link,
                        session,
                        existing=existing,
                        redactor=redactor,
                        fallback_title=target.title,
                    )
                    if res.status == LINK_DELETED:
                        t_key = link_key(target.link)
                        existing = [
                            item
                            for item in existing
                            if not (
                                (t_key and link_key(item.link) == t_key)
                                or item.link.strip() == target.link.strip()
                            )
                        ]
                    out.append(res)
                    self._emit(progress, res)

                    if (
                        res.status == LINK_DELETED
                        and idx + 1 < total
                        and self.settings.friendlink.per_request_delay_s > 0
                        and not _is_cancelled(cancel_event)
                    ):
                        self._sleep(self.settings.friendlink.per_request_delay_s)
                return out
            finally:
                _close_session(session)

        return self._run_per_site(site_list, _worker)

    # ------------------------------------------------------------------ #
    # Internal per-session operations & transport
    # ------------------------------------------------------------------ #
    def _request_timeout(self) -> tuple[float, float]:
        return (self.settings.mcp.connect_timeout, self.settings.friendlink.request_timeout)

    def _has_transport(self, site) -> bool:
        base = _site_url(site)
        if not base:
            return False
        if import_token(site):
            return True
        if self._mcp_client_factory is not None and effective_mcp_token(site):
            return True
        return False

    @staticmethod
    def _has_read_transport(site) -> bool:
        return bool(_site_url(site))

    def _list_links_with_session(
        self,
        site,
        session: Any,
        redactor: SecretRedactor,
    ) -> list[FriendLink]:
        base = _site_url(site)
        token = import_token(site)
        if not base:
            raise FriendLinkError(MISSING_SITE_CREDS_MSG)

        endpoint = f"{base}/api/friendlink/list"
        if token:
            try:
                payload = self._rest_request(
                    session,
                    "GET",
                    endpoint,
                    token=token,
                    redactor=redactor,
                )
                return _extract_friendlink_list(payload.get("data"))
            except FriendLinkError as exc:
                if exc.http_status in {404, 405} and self._can_mcp_fallback(site):
                    mcp_links = self._mcp_list_links(site, redactor)
                    if mcp_links is not None:
                        return mcp_links
                raise

        if self._can_mcp_fallback(site):
            mcp_links = self._mcp_list_links(site, redactor)
            if mcp_links is not None:
                return mcp_links

        # Real AnqiCMS v3.6.7 returns HTTP 200 on GET /api/friendlink/list even without token.
        payload = self._rest_request(
            session,
            "GET",
            endpoint,
            token="",
            redactor=redactor,
        )
        return _extract_friendlink_list(payload.get("data"))

    def _add_link_with_session(
        self,
        site,
        spec: FriendLinkSpec,
        session: Any,
        *,
        existing_keys: set[str] | None,
        redactor: SecretRedactor,
    ) -> LinkOpResult:
        try:
            normalized_link = normalize_link_url(spec.link)
        except ValueError:
            return LinkOpResult(
                site_name=_site_name(site),
                site_id=_site_id(site),
                action="add",
                link=spec.link,
                title=spec.title,
                status=LINK_FAILED,
                message="目标网址无效",
            )

        site_origin = site_origin_key(_site_url(site))
        target_origin = site_origin_key(normalized_link)
        if site_origin and target_origin and site_origin == target_origin:
            return LinkOpResult(
                site_name=_site_name(site),
                site_id=_site_id(site),
                action="add",
                link=normalized_link,
                title=spec.title,
                status=LINK_SKIPPED_SELF,
                message="跳过本站域名",
            )

        target_key = link_key(normalized_link)
        if existing_keys is not None and target_key and target_key in existing_keys:
            return LinkOpResult(
                site_name=_site_name(site),
                site_id=_site_id(site),
                action="add",
                link=normalized_link,
                title=spec.title,
                status=LINK_SKIPPED_EXISTS,
                message="友链已存在，已跳过",
            )

        base = _site_url(site)
        token = import_token(site)
        if not base or (not token and not self._can_mcp_fallback(site)):
            return LinkOpResult(
                site_name=_site_name(site),
                site_id=_site_id(site),
                action="add",
                link=normalized_link,
                title=spec.title,
                status=LINK_FAILED,
                message=MISSING_SITE_CREDS_MSG,
            )

        effective_spec = replace(spec, link=normalized_link)
        try:
            if token:
                endpoint = f"{base}/api/friendlink/create"
                files = {
                    "title": (None, effective_spec.title),
                    "link": (None, effective_spec.link),
                    "nofollow": (None, "1" if effective_spec.nofollow else "0"),
                    "back_link": (None, effective_spec.back_link),
                    "my_title": (None, effective_spec.my_title),
                    "my_link": (None, effective_spec.my_link),
                    "contact": (None, effective_spec.contact),
                    "remark": (None, effective_spec.remark),
                }
                try:
                    self._rest_request(
                        session,
                        "POST",
                        endpoint,
                        token=token,
                        files=files,
                        redactor=redactor,
                    )
                except FriendLinkError as exc:
                    if exc.http_status in {404, 405} and self._can_mcp_fallback(site):
                        if not self._mcp_create_link(site, effective_spec, redactor):
                            raise
                    else:
                        raise
            else:
                if not self._mcp_create_link(site, effective_spec, redactor):
                    raise FriendLinkError("站点未暴露友链创建工具")
        except FriendLinkError as exc:
            if _is_exists_message(exc.message):
                return LinkOpResult(
                    site_name=_site_name(site),
                    site_id=_site_id(site),
                    action="add",
                    link=normalized_link,
                    title=spec.title,
                    status=LINK_SKIPPED_EXISTS,
                    message="友链已存在，已跳过",
                )
            return LinkOpResult(
                site_name=_site_name(site),
                site_id=_site_id(site),
                action="add",
                link=normalized_link,
                title=spec.title,
                status=LINK_FAILED,
                message=_format_error_message(exc, redactor),
            )

        return LinkOpResult(
            site_name=_site_name(site),
            site_id=_site_id(site),
            action="add",
            link=normalized_link,
            title=spec.title,
            status=LINK_CREATED,
            message="已添加",
        )

    def _remove_link_with_session(
        self,
        site,
        link_url: str,
        session: Any,
        *,
        existing: list[FriendLink] | None,
        redactor: SecretRedactor,
        fallback_title: str = "",
    ) -> LinkOpResult:
        raw_url = (link_url or "").strip()
        if not raw_url:
            return LinkOpResult(
                site_name=_site_name(site),
                site_id=_site_id(site),
                action="remove",
                link=raw_url,
                title=fallback_title,
                status=LINK_FAILED,
                message="目标网址为空",
            )

        delete_entries: list[tuple[str, int | None]] = []
        matched_count = 0
        title = fallback_title

        if existing is not None:
            target_key = link_key(raw_url)
            matches = [
                item
                for item in existing
                if (target_key and link_key(item.link) == target_key)
                or item.link.strip() == raw_url
            ]
            if not matches:
                return LinkOpResult(
                    site_name=_site_name(site),
                    site_id=_site_id(site),
                    action="remove",
                    link=raw_url,
                    title=fallback_title,
                    status=LINK_NOT_FOUND,
                    message="站点中未找到该友链",
                )
            matched_count = len(matches)
            title = matches[0].title or fallback_title
            seen_entries: set[tuple[str, int | None]] = set()
            for item in matches:
                candidate = item.link.strip() or raw_url
                item_id = _coerce_optional_int(getattr(item, "id", None))
                resolved_id = item_id if (item_id is not None and item_id > 0) else None
                entry_key = (candidate, resolved_id)
                if entry_key not in seen_entries:
                    seen_entries.add(entry_key)
                    delete_entries.append(entry_key)
        else:
            delete_entries.append((raw_url, None))
            matched_count = 1

        base = _site_url(site)
        token = import_token(site)
        if not base or (not token and not self._can_mcp_fallback(site)):
            return LinkOpResult(
                site_name=_site_name(site),
                site_id=_site_id(site),
                action="remove",
                link=raw_url,
                title=title,
                status=LINK_FAILED,
                message=MISSING_SITE_CREDS_MSG,
            )

        try:
            seen_rest_links: set[str] = set()
            for target_link, target_id in delete_entries:
                if token:
                    if target_link in seen_rest_links:
                        continue
                    endpoint = f"{base}/api/friendlink/delete"
                    try:
                        self._rest_request(
                            session,
                            "POST",
                            endpoint,
                            token=token,
                            files={"link": (None, target_link)},
                            redactor=redactor,
                        )
                        seen_rest_links.add(target_link)
                    except FriendLinkError as exc:
                        if exc.http_status in {404, 405} and self._can_mcp_fallback(site):
                            if not self._mcp_delete_link(
                                site,
                                target_link,
                                redactor,
                                link_id=target_id,
                                existing=existing,
                            ):
                                raise
                        else:
                            raise
                else:
                    if not self._mcp_delete_link(
                        site,
                        target_link,
                        redactor,
                        link_id=target_id,
                        existing=existing,
                    ):
                        raise FriendLinkError("站点未暴露友链删除工具")
        except FriendLinkError as exc:
            if existing is None and _is_not_found_message(exc.message):
                return LinkOpResult(
                    site_name=_site_name(site),
                    site_id=_site_id(site),
                    action="remove",
                    link=raw_url,
                    title=title,
                    status=LINK_NOT_FOUND,
                    message="站点中未找到该友链",
                )
            return LinkOpResult(
                site_name=_site_name(site),
                site_id=_site_id(site),
                action="remove",
                link=raw_url,
                title=title,
                status=LINK_FAILED,
                message=_format_error_message(exc, redactor),
            )

        msg = f"已删除 {matched_count} 条友链" if matched_count > 1 else "已删除"
        return LinkOpResult(
            site_name=_site_name(site),
            site_id=_site_id(site),
            action="remove",
            link=raw_url,
            title=title,
            status=LINK_DELETED,
            message=msg,
        )

    def _rest_request(
        self,
        session: Any,
        method: str,
        url: str,
        *,
        token: str,
        files: dict | None = None,
        redactor: SecretRedactor,
    ) -> dict:
        kwargs: dict[str, Any] = {
            "timeout": self._request_timeout(),
            "allow_redirects": False,
            "verify": self.settings.mcp.verify_ssl,
        }
        if token:
            kwargs["params"] = {"token": token}
        if files is not None:
            kwargs["files"] = files

        try:
            if method == "GET":
                response = session.get(url, **kwargs)
            else:
                response = session.post(url, **kwargs)
        except requests.exceptions.SSLError as exc:
            raise FriendLinkError(
                f"证书校验失败：{redactor(str(exc))}",
                hint=SSL_HINT,
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise FriendLinkError("友链接口请求超时") from exc
        except requests.RequestException as exc:
            raise FriendLinkError(f"友链接口网络错误：{redactor(str(exc))}") from exc

        status = int(response.status_code)
        if status in {401, 403}:
            raise FriendLinkError(
                f"友链接口鉴权失败 (HTTP {status})",
                hint=AUTH_HINT,
                http_status=status,
            )
        if 300 <= status < 400:
            raise FriendLinkError(
                f"友链接口禁止重定向 (HTTP {status})",
                hint=REDIRECT_HINT,
                http_status=status,
            )
        if status == 404:
            raise FriendLinkError(
                "友链接口不存在 (HTTP 404)",
                hint="请检查站点版本或站点网址",
                http_status=status,
            )
        if not 200 <= status < 300:
            raise FriendLinkError(
                f"友链接口请求失败 (HTTP {status})",
                http_status=status,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise FriendLinkError("友链接口返回的不是有效 JSON", http_status=status) from exc

        if not isinstance(payload, dict):
            raise FriendLinkError("友链接口返回格式无效", http_status=status)

        code = payload.get("code")
        if type(code) not in {int, str} or code not in _SUCCESS_CODES:
            raw_msg = payload.get("msg") or payload.get("message") or ""
            msg = redactor(str(raw_msg).strip()) or f"code={code!r}"
            hint = AUTH_HINT if _is_auth_message(msg) else ""
            raise FriendLinkError(f"友链接口返回错误：{msg}", hint=hint, http_status=status)

        return payload

    def _rest_check_request(
        self,
        session: Any,
        url: str,
        token: str,
        redactor: SecretRedactor,
    ) -> dict:
        try:
            return self._rest_request(session, "GET", url, token=token, redactor=redactor)
        except FriendLinkError as exc:
            if exc.http_status == 405:
                try:
                    return self._rest_request(session, "POST", url, token=token, redactor=redactor)
                except FriendLinkError as post_exc:
                    if post_exc.http_status == 404:
                        raise FriendLinkError(
                            CHECK_UNSUPPORTED_HINT,
                            hint=CHECK_UNSUPPORTED_HINT,
                            http_status=404,
                        ) from post_exc
                    raise
            if exc.http_status == 404:
                raise FriendLinkError(
                    CHECK_UNSUPPORTED_HINT,
                    hint=CHECK_UNSUPPORTED_HINT,
                    http_status=404,
                ) from exc
            raise

    def _probe_backlink(
        self,
        session: Any,
        *,
        site_name: str,
        display_link: str,
        fetch_url: str,
        my_root: str,
        allow_homepage_fallback: bool,
        timeout: tuple[float, float],
        redactor: SecretRedactor,
        cancel_event: threading.Event | None = None,
    ) -> LinkHealth:
        primary, final_url = self._fetch_and_inspect_backlink(
            session,
            site_name=site_name,
            display_link=display_link,
            fetch_url=fetch_url,
            my_root=my_root,
            timeout=timeout,
            redactor=redactor,
        )
        if (
            allow_homepage_fallback
            and primary.reachable
            and primary.has_backlink is False
            and my_root
            and not _is_cancelled(cancel_event)
        ):
            fallback_url = _homepage_fallback_url(final_url or fetch_url)
            if fallback_url:
                fallback, _ = self._fetch_and_inspect_backlink(
                    session,
                    site_name=site_name,
                    display_link=display_link,
                    fetch_url=fallback_url,
                    my_root=my_root,
                    timeout=timeout,
                    redactor=redactor,
                )
                if fallback.reachable:
                    return fallback
        return primary

    def _fetch_and_inspect_backlink(
        self,
        session: Any,
        *,
        site_name: str,
        display_link: str,
        fetch_url: str,
        my_root: str,
        timeout: tuple[float, float],
        redactor: SecretRedactor,
    ) -> tuple[LinkHealth, str]:
        try:
            response = session.get(
                fetch_url,
                headers=dict(_BACKLINK_REQUEST_HEADERS),
                timeout=timeout,
                allow_redirects=True,
                verify=self.settings.mcp.verify_ssl,
                stream=True,
            )
        except requests.exceptions.SSLError as exc:
            return (
                LinkHealth(
                    site_name=site_name,
                    link=display_link,
                    reachable=False,
                    has_backlink=False,
                    http_status=None,
                    message=redactor(f"对方无法访问(证书校验失败：{exc})"),
                ),
                fetch_url,
            )
        except requests.exceptions.Timeout:
            return (
                LinkHealth(
                    site_name=site_name,
                    link=display_link,
                    reachable=False,
                    has_backlink=False,
                    http_status=None,
                    message="对方无法访问(请求超时)",
                ),
                fetch_url,
            )
        except requests.RequestException as exc:
            return (
                LinkHealth(
                    site_name=site_name,
                    link=display_link,
                    reachable=False,
                    has_backlink=False,
                    http_status=None,
                    message=redactor(f"对方无法访问({exc})"),
                ),
                fetch_url,
            )

        try:
            status_code = int(response.status_code)
            raw_bytes = self._read_bounded_body(response, _MAX_BACKLINK_BYTES)
            resp_url = getattr(response, "url", None)
            final_url = resp_url.strip() if isinstance(resp_url, str) and resp_url.strip() else fetch_url
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

        reachable = 200 <= status_code < 400
        if not reachable:
            return (
                LinkHealth(
                    site_name=site_name,
                    link=display_link,
                    reachable=False,
                    has_backlink=False,
                    http_status=status_code,
                    message=f"对方无法访问(HTTP {status_code})",
                ),
                final_url,
            )

        if not my_root:
            return (
                LinkHealth(
                    site_name=site_name,
                    link=display_link,
                    reachable=True,
                    has_backlink=None,
                    http_status=status_code,
                    message="可访问（未配置本站域名，无法校验反链）",
                ),
                final_url,
            )

        decoded_html = _decode_html_bytes(response, raw_bytes)
        hit = _find_best_backlink(decoded_html, base_url=final_url, target_root=my_root)
        if hit is not None:
            follow_tag = "nofollow" if hit.nofollow else "dofollow"
            detail = f"{follow_tag} · {hit.anchor_text}" if hit.anchor_text else follow_tag
            return (
                LinkHealth(
                    site_name=site_name,
                    link=display_link,
                    reachable=True,
                    has_backlink=True,
                    http_status=status_code,
                    message=f"有反链({detail})",
                ),
                final_url,
            )

        return (
            LinkHealth(
                site_name=site_name,
                link=display_link,
                reachable=True,
                has_backlink=False,
                http_status=status_code,
                message=f"未发现我方主域名({my_root})反链",
            ),
            final_url,
        )

    @staticmethod
    def _read_bounded_body(response: Any, max_bytes: int) -> bytes:
        iter_content = getattr(response, "iter_content", None)
        if callable(iter_content):
            chunks: list[bytes] = []
            remaining = max_bytes
            for chunk in iter_content(chunk_size=16384):
                if not chunk:
                    continue
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="replace")
                if len(chunk) >= remaining:
                    chunks.append(chunk[:remaining])
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)

        content = getattr(response, "content", None)
        if isinstance(content, (bytes, bytearray)):
            return bytes(content[:max_bytes])
        text = getattr(response, "text", "")
        if isinstance(text, str):
            return text.encode("utf-8", errors="replace")[:max_bytes]
        return b""

    # ------------------------------------------------------------------ #
    # Optional MCP fallback (strictly via McpClientFactory protocol)
    # ------------------------------------------------------------------ #
    def _can_mcp_fallback(self, site) -> bool:
        return self._mcp_client_factory is not None and bool(effective_mcp_token(site))

    @staticmethod
    def _tool_action_enum(tool: Any) -> list[str]:
        schema = getattr(tool, "input_schema", None)
        if not isinstance(schema, dict):
            return []
        props = schema.get("properties")
        if not isinstance(props, dict):
            return []
        action_prop = props.get("action")
        if not isinstance(action_prop, dict):
            return []
        enum_vals = action_prop.get("enum")
        if not isinstance(enum_vals, (list, tuple)):
            return []
        return [val for val in enum_vals if isinstance(val, str)]

    @staticmethod
    def _pick_mcp_tool(tools, preferred_tokens: tuple[str, ...]):
        for tool in tools:
            folded = f"{tool.name} {tool.description}".casefold()
            if any(tok in folded for tok in preferred_tokens):
                return tool
        return None

    @classmethod
    def _find_mcp_tool(
        cls,
        client: Any,
        keywords: tuple[str, ...],
        preferred_tokens: tuple[str, ...],
        *,
        action_name: str,
    ):
        found = list(client.find_tools(keywords))
        list_tools_fn = getattr(client, "list_tools", None)
        listed = list(list_tools_fn()) if callable(list_tools_fn) else []

        seen: set[str] = set()
        candidates: list[Any] = []
        for tool in (*found, *listed):
            name = getattr(tool, "name", "")
            if name and name not in seen:
                seen.add(name)
                candidates.append(tool)

        multi_action_matches = [
            tool for tool in candidates if action_name in cls._tool_action_enum(tool)
        ]
        if multi_action_matches:
            for tool in multi_action_matches:
                if getattr(tool, "name", "").casefold() == "structure":
                    return tool
            return multi_action_matches[0]

        return cls._pick_mcp_tool(found, preferred_tokens)

    @staticmethod
    def _check_mcp_tool_result(
        result: Any,
        redactor: SecretRedactor,
        default_msg: str,
    ) -> None:
        if getattr(result, "is_error", False):
            err_text = redactor(str(getattr(result, "text", "") or "").strip())
            raise FriendLinkError(err_text or default_msg)

        payload = getattr(result, "structured", None)
        if not isinstance(payload, dict):
            raw_text = getattr(result, "text", None)
            if isinstance(raw_text, str) and raw_text.strip():
                payload = next(
                    (cand for cand in _iter_json_candidates(raw_text) if isinstance(cand, dict)),
                    None,
                )

        if isinstance(payload, dict) and "code" in payload:
            code = payload.get("code")
            if type(code) not in {int, str} or code not in _SUCCESS_CODES:
                raw_msg = payload.get("msg") or payload.get("message") or ""
                msg = redactor(str(raw_msg).strip()) or f"code={code!r}"
                hint = AUTH_HINT if _is_auth_message(msg) else ""
                raise FriendLinkError(f"友链接口返回错误：{msg}", hint=hint)

    def _mcp_list_links_with_client(
        self,
        client: Any,
        redactor: SecretRedactor,
    ) -> list[FriendLink] | None:
        tool = self._find_mcp_tool(
            client,
            _MCP_LIST_KEYWORDS,
            ("list", "get", "query", "列表", "查询"),
            action_name="friendlink_list",
        )
        if tool is None:
            return None
        args: dict[str, Any] = (
            {"action": "friendlink_list"}
            if "friendlink_list" in self._tool_action_enum(tool)
            else {}
        )
        result = client.call_tool(tool.name, args)
        if getattr(result, "is_error", False):
            err_text = redactor(str(getattr(result, "text", "") or "").strip())
            raise FriendLinkError(err_text or "MCP 友链工具返回错误")
        try:
            return _extract_mcp_friendlinks(result)
        except FriendLinkError as exc:
            raise FriendLinkError(
                redactor(exc.message),
                hint=redactor(exc.hint),
                http_status=exc.http_status,
            ) from exc

    def _mcp_list_links(self, site, redactor: SecretRedactor) -> list[FriendLink] | None:
        assert self._mcp_client_factory is not None
        client = self._mcp_client_factory(site)
        try:
            client.initialize()
            return self._mcp_list_links_with_client(client, redactor)
        except McpError as exc:
            raise FriendLinkError(redactor(exc.message), hint=exc.hint, http_status=exc.http_status) from exc
        finally:
            _close_session(client)

    def _mcp_create_link(
        self,
        site,
        spec: FriendLinkSpec,
        redactor: SecretRedactor,
    ) -> bool:
        assert self._mcp_client_factory is not None
        client = self._mcp_client_factory(site)
        try:
            client.initialize()
            tool = self._find_mcp_tool(
                client,
                _MCP_CREATE_KEYWORDS,
                ("create", "add", "save", "添加", "创建", "新增"),
                action_name="friendlink_create",
            )
            if tool is None:
                return False
            is_multi = "friendlink_create" in self._tool_action_enum(tool)
            args = self._build_mcp_create_args(
                tool.input_schema,
                spec,
                multi_action="friendlink_create" if is_multi else "",
            )
            result = client.call_tool(tool.name, args)
            self._check_mcp_tool_result(result, redactor, "MCP 创建友链失败")
            return True
        except McpError as exc:
            raise FriendLinkError(redactor(exc.message), hint=exc.hint, http_status=exc.http_status) from exc
        finally:
            _close_session(client)

    def _resolve_mcp_delete_ids(
        self,
        client: Any,
        link_url: str,
        redactor: SecretRedactor,
        *,
        link_id: int | None = None,
        existing: list[FriendLink] | None = None,
    ) -> list[int]:
        if link_id is not None and link_id > 0:
            return [int(link_id)]

        target_key = link_key(link_url)
        raw = (link_url or "").strip()

        def _matching_ids(items: Iterable[FriendLink]) -> list[int]:
            ids: list[int] = []
            seen_ids: set[int] = set()
            for item in items:
                item_key = link_key(item.link)
                if (target_key and item_key == target_key) or (raw and item.link.strip() == raw):
                    item_id = _coerce_optional_int(getattr(item, "id", None))
                    if item_id is not None and item_id > 0 and item_id not in seen_ids:
                        seen_ids.add(item_id)
                        ids.append(item_id)
            return ids

        if existing is not None:
            matched = _matching_ids(existing)
            if matched:
                return matched

        live_links = self._mcp_list_links_with_client(client, redactor)
        if not live_links:
            return []
        return _matching_ids(live_links)

    def _mcp_delete_link(
        self,
        site,
        link_url: str,
        redactor: SecretRedactor,
        *,
        link_id: int | None = None,
        existing: list[FriendLink] | None = None,
    ) -> bool:
        assert self._mcp_client_factory is not None
        client = self._mcp_client_factory(site)
        try:
            client.initialize()
            tool = self._find_mcp_tool(
                client,
                _MCP_DELETE_KEYWORDS,
                ("delete", "remove", "del", "删除", "移除"),
                action_name="friendlink_delete",
            )
            if tool is None:
                return False
            if "friendlink_delete" in self._tool_action_enum(tool):
                target_ids = self._resolve_mcp_delete_ids(
                    client,
                    link_url,
                    redactor,
                    link_id=link_id,
                    existing=existing,
                )
                if not target_ids:
                    raise FriendLinkError("站点中未找到该友链")
                for tid in target_ids:
                    result = client.call_tool(
                        tool.name,
                        {"action": "friendlink_delete", "id": int(tid)},
                    )
                    self._check_mcp_tool_result(result, redactor, "MCP 删除友链失败")
                return True

            props = tool.input_schema.get("properties", {}) if isinstance(tool.input_schema, dict) else {}
            if "url" in props and "link" not in props:
                args = {"url": link_url}
            else:
                args = {"link": link_url}
            result = client.call_tool(tool.name, args)
            self._check_mcp_tool_result(result, redactor, "MCP 删除友链失败")
            return True
        except McpError as exc:
            raise FriendLinkError(redactor(exc.message), hint=exc.hint, http_status=exc.http_status) from exc
        finally:
            _close_session(client)

    @staticmethod
    def _build_mcp_create_args(
        schema: dict,
        spec: FriendLinkSpec,
        *,
        multi_action: str = "",
    ) -> dict[str, Any]:
        props = schema.get("properties", {}) if isinstance(schema, dict) else {}
        if not isinstance(props, dict):
            props = {}
        link_field = "url" if ("url" in props and "link" not in props) else "link"
        nofollow_prop = props.get("nofollow")
        nofollow_val: Any = (
            bool(spec.nofollow)
            if isinstance(nofollow_prop, dict) and nofollow_prop.get("type") == "boolean"
            else (1 if spec.nofollow else 0)
        )

        if multi_action:
            args: dict[str, Any] = {
                "action": multi_action,
                "title": spec.title,
                link_field: spec.link,
            }
            logo = str(getattr(spec, "logo", "") or "").strip()
            if logo and (not props or "logo" in props):
                args["logo"] = logo
            if "nofollow" in props:
                args["nofollow"] = nofollow_val
            for key, value in (
                ("back_link", spec.back_link),
                ("my_title", spec.my_title),
                ("my_link", spec.my_link),
                ("contact", spec.contact),
                ("remark", spec.remark),
            ):
                if key in props and value:
                    args[key] = value
            return args

        args = {
            "title": spec.title,
            link_field: spec.link,
            "nofollow": nofollow_val,
        }
        for key, value in (
            ("back_link", spec.back_link),
            ("my_title", spec.my_title),
            ("my_link", spec.my_link),
            ("contact", spec.contact),
            ("remark", spec.remark),
        ):
            if value or key in props:
                args[key] = value
        return args

    # ------------------------------------------------------------------ #
    # Concurrency & helper utilities
    # ------------------------------------------------------------------ #
    def _run_per_site(
        self,
        sites: list,
        worker: Callable[[Any], list[LinkOpResult]],
    ) -> list[LinkOpResult]:
        if not sites:
            return []
        max_workers = max(1, min(self.settings.friendlink.max_concurrency, len(sites)))
        if max_workers == 1:
            combined: list[LinkOpResult] = []
            for site in sites:
                combined.extend(worker(site))
            return combined

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(worker, site) for site in sites]
            combined = []
            for future in futures:
                combined.extend(future.result())
            return combined

    def _run_groups(self, groups: list, worker: Callable[[Any], Any]) -> list:
        if not groups:
            return []
        max_workers = max(1, min(self.settings.friendlink.max_concurrency, len(groups)))
        if max_workers == 1:
            return [worker(group) for group in groups]

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(worker, group) for group in groups]
            return [future.result() for future in futures]

    @staticmethod
    def _index_sites(sites: list) -> dict[str, Any]:
        mapping: dict[str, Any] = {}
        for site in sites:
            sid = _site_id(site)
            if sid:
                mapping.setdefault(sid, site)
            name = _site_name(site)
            if name:
                mapping.setdefault(name, site)
        return mapping

    @staticmethod
    def _group_indexed_items(items: Iterable, key_fn: Callable[[Any], str]) -> list[tuple[str, list[tuple[int, Any]]]]:
        order: list[str] = []
        buckets: dict[str, list[tuple[int, Any]]] = {}
        for idx, item in enumerate(items):
            key = key_fn(item)
            if key not in buckets:
                order.append(key)
                buckets[key] = []
            buckets[key].append((idx, item))
        return [(key, buckets[key]) for key in order]

    @staticmethod
    def _dedupe_specs(specs: Iterable[FriendLinkSpec]) -> list[FriendLinkSpec]:
        result: list[FriendLinkSpec] = []
        seen: set[str] = set()
        for spec in specs or ():
            key = link_key(spec.link)
            if key:
                if key in seen:
                    continue
                seen.add(key)
            result.append(spec)
        return result

    @staticmethod
    def _dedupe_urls(urls: Iterable[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in urls or ():
            text = (raw or "").strip()
            if not text:
                continue
            key = link_key(text) or text.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(text)
        return result

    @staticmethod
    def _cancelled_op(site, action: str, link: str, title: str) -> LinkOpResult:
        return LinkOpResult(
            site_name=_site_name(site),
            site_id=_site_id(site),
            action=action,
            link=link,
            title=title,
            status=LINK_CANCELLED,
            message="已取消",
        )

    @staticmethod
    def _emit(callback: Callable[[Any], None] | None, item: Any) -> None:
        if callback is not None:
            callback(item)
