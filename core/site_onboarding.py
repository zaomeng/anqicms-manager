"""Bulk site onboarding & concurrent auto-discovery for AnQiCMS site-group."""

from __future__ import annotations

import json
import re
import threading
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

from core.category_service import CategoryService, CategoryServiceError
from core.redaction import SecretRedactor, redact_text
from core.site_credentials import import_token, site_origin_key
from core.site_factory import (
    DEFAULT_ADMIN_PATH,
    build_import_endpoint,
    derive_site_paths,
    is_valid_site_url,
    normalize_site_url,
    parse_publish_endpoint,
    site_key_from_url,
    site_url_identity,
    unique_site_key,
    validate_site_key,
)
from models.entities import SiteConfig
from models.sitegroup import McpProbeResult

__all__ = [
    "SiteDiscoveryResult",
    "SmartImportResult",
    "parse_smart_site_import",
    "parse_bulk_new_sites",
    "auto_discover_site",
    "auto_discover_sites",
]

SITE_GROUP_MAX_LENGTH = 64
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_MAX_HTML_BYTES = 256 * 1024
_SUCCESS_CODES = {0, "0", 200, "200"}
_CHARSET_RE = re.compile(r"charset\s*=\s*['\"]?\s*([A-Za-z0-9._-]+)", re.IGNORECASE)
_META_CHARSET_RE = re.compile(
    rb"<meta[^>]+charset\s*=\s*[\"']?\s*([A-Za-z0-9._-]+)",
    re.IGNORECASE,
)
_IMPORT_URL_RE = re.compile(
    r"https?://[^\s<>\"'\)\],;]+/archive\?[^\s<>\"'\)\],;]*",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class SiteDiscoveryResult:
    """Result of probing homepage title, friend-link count and MCP availability."""

    site_name: str
    site_url: str
    reachable: bool
    http_status: int | None = None
    site_title: str = ""
    discovered_title: str = ""
    link_count: int | None = None
    mcp_ok: bool | None = None
    mcp_tools: tuple[str, ...] = ()
    mcp_probe: McpProbeResult | None = None
    message: str = ""
    current_version: str = ""
    default_fid: int = 0
    default_category_name: str = ""


@dataclass(slots=True)
class SmartImportResult:
    """Structured outcome of :func:`parse_smart_site_import`."""

    new_sites: list[SiteConfig]
    updated_sites: list[SiteConfig]
    warnings: list[dict[str, str]]
    merged_count: int = 0


class _TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_title = False
        self._in_svg = 0
        self._parts: list[str] = []
        self.title: str = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        lower = tag.casefold()
        if lower == "svg":
            self._in_svg += 1
        elif lower == "title" and self._in_svg == 0 and not self.title:
            self._in_title = True
            self._parts.clear()

    def handle_endtag(self, tag: str) -> None:
        lower = tag.casefold()
        if lower == "svg" and self._in_svg > 0:
            self._in_svg -= 1
        elif lower == "title" and self._in_title:
            self._in_title = False
            candidate = " ".join("".join(self._parts).split()).strip()
            if candidate and not self.title:
                self.title = candidate

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._parts.append(data)


def _has_control_characters(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _split_line_columns(line: str) -> list[str]:
    if "|" in line:
        return [part.strip() for part in line.split("|")]
    if "\t" in line:
        return [part.strip() for part in line.split("\t")]
    if "," in line:
        return [part.strip() for part in line.split(",")]
    return [line.strip()]


def _looks_like_header_row(parts: list[str]) -> bool:
    if len(parts) < 2:
        return False
    col0 = parts[0].strip().casefold()
    col1 = parts[1].strip().casefold()
    return (
        col0 in {"站点名", "站点标识", "name", "site_name", "site"}
        and ("网址" in col1 or "导入接口" in col1 or col1 in {"url", "site_url", "endpoint"})
    )


def _derive_url_and_endpoint(
    raw_target: str,
    mcp_token: str,
) -> tuple[str, str]:
    """Return ``(site_url, jiekou)`` from a bare domain/URL or full import URL."""
    target = raw_target.strip()
    if not target:
        raise ValueError("网址或导入接口不能为空")
    if _has_control_characters(target) or any(ch.isspace() or ch == "\\" for ch in target):
        raise ValueError("网址格式无效")

    normalized_candidate = target if "://" in target else f"https://{target}"
    try:
        parsed = urlsplit(normalized_candidate)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("网址格式无效") from exc

    path_lower = (parsed.path or "").casefold()
    looks_like_import = (
        path_lower.endswith("/archive")
        or bool(parsed.query)
        or (bool(parsed.path) and parsed.path != "/")
    )

    if looks_like_import:
        parsed_endpoint = parse_publish_endpoint(normalized_candidate)
        return parsed_endpoint.base_url, parsed_endpoint.publish_endpoint

    clean_url = normalize_site_url(target)
    if not is_valid_site_url(clean_url):
        raise ValueError("站点网址无效")
    jiekou = build_import_endpoint(mcp_token) if mcp_token else ""
    return clean_url, jiekou


def _safe_warning(
    line_no: int,
    *,
    name: str,
    url: str,
    raw_line: str,
    reason: str,
    secrets: Sequence[str] = (),
) -> dict[str, str]:
    redactor = SecretRedactor(secrets)
    return {
        "line": str(line_no),
        "name": name,
        "url": redactor(url),
        "raw": redactor(raw_line),
        "reason": redactor(reason),
    }


@dataclass(slots=True)
class _ParsedSiteCandidate:
    line_no: int
    raw_name: str
    site_url: str
    jiekou: str
    site_group: str
    mcp_token: str
    admin_username: str
    admin_password: str
    site_title: str
    raw_line: str
    source: str
    secrets: tuple[str, ...] = ()


def _strip_mcp_url(raw_url: str) -> str:
    """Strip ``/api/mcp`` suffix from an MCP endpoint URL and validate the root URL."""
    target = raw_url.strip()
    if not target:
        raise ValueError("MCP 网址不能为空")
    if _has_control_characters(target) or any(ch.isspace() or ch == "\\" for ch in target):
        raise ValueError("MCP 网址格式无效")

    candidate = target if "://" in target else f"https://{target}"
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("MCP 网址格式无效") from exc

    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"} or parsed.username is not None or parsed.password is not None:
        raise ValueError("MCP 网址必须使用 http:// 或 https:// 且不能包含账号密码")

    path = (parsed.path or "").rstrip("/")
    for suffix in ("/api/mcp", "/mcp"):
        if path.casefold().endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            break

    hostname = (parsed.hostname or "").strip(".")
    formatted_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    netloc = formatted_host if (port is None or port == default_port) else f"{formatted_host}:{port}"
    site_url = urlunsplit((scheme, netloc, path, "", "")).rstrip("/")
    if not is_valid_site_url(site_url):
        raise ValueError("MCP 站点网址无效")
    return site_url


def _extract_mcp_bearer_token(server_cfg: dict[str, Any]) -> str:
    """Extract bearer token from ``headers.Authorization`` (or fallback ``token``)."""
    headers = server_cfg.get("headers")
    if isinstance(headers, dict):
        for key, value in headers.items():
            if str(key).strip().casefold() == "authorization" and isinstance(value, str):
                cleaned = re.sub(r"(?i)^bearer\s+", "", value.strip()).strip()
                if cleaned:
                    return cleaned
    raw_token = server_cfg.get("token")
    return raw_token.strip() if isinstance(raw_token, str) else ""


def _extract_mcp_json_blocks(
    text: str,
    *,
    default_group: str,
) -> tuple[list[_ParsedSiteCandidate], list[dict[str, str]], str]:
    """Scan ``text`` for one or more ``{"mcpServers": ...}`` JSON blocks using ``raw_decode``."""
    candidates: list[_ParsedSiteCandidate] = []
    warnings: list[dict[str, str]] = []
    consumed_spans: list[tuple[int, int]] = []
    decoder = json.JSONDecoder()
    idx = 0
    length = len(text)

    while idx < length:
        brace_idx = text.find("{", idx)
        if brace_idx == -1:
            break
        try:
            obj, end_idx = decoder.raw_decode(text, brace_idx)
        except ValueError:
            idx = brace_idx + 1
            continue

        if not isinstance(obj, dict) or "mcpServers" not in obj:
            idx = brace_idx + 1
            continue

        consumed_spans.append((brace_idx, end_idx))
        idx = end_idx
        start_line = text[:brace_idx].count("\n") + 1
        raw_snippet = text[brace_idx:end_idx]
        servers = obj.get("mcpServers")
        if not isinstance(servers, dict):
            warnings.append(
                _safe_warning(
                    start_line,
                    name="",
                    url="",
                    raw_line=raw_snippet,
                    reason="mcpServers 字段格式无效，必须是对象",
                )
            )
            continue

        for server_key, server_cfg in servers.items():
            site_title = str(server_key or "").strip()
            if not isinstance(server_cfg, dict):
                warnings.append(
                    _safe_warning(
                        start_line,
                        name=site_title,
                        url="",
                        raw_line=raw_snippet,
                        reason="mcpServers 站点配置必须是对象",
                    )
                )
                continue

            raw_url = str(server_cfg.get("url") or "").strip()
            mcp_token = _extract_mcp_bearer_token(server_cfg)
            secrets = tuple(s for s in (mcp_token,) if s)

            if any(_has_control_characters(val) for val in (site_title, mcp_token, default_group)):
                warnings.append(
                    _safe_warning(
                        start_line,
                        name=site_title,
                        url=raw_url,
                        raw_line=raw_snippet,
                        reason="MCP 配置包含非法控制字符",
                        secrets=secrets,
                    )
                )
                continue

            try:
                site_url = _strip_mcp_url(raw_url)
            except ValueError as exc:
                warnings.append(
                    _safe_warning(
                        start_line,
                        name=site_title,
                        url=raw_url,
                        raw_line=raw_snippet,
                        reason=str(exc),
                        secrets=secrets,
                    )
                )
                continue

            candidates.append(
                _ParsedSiteCandidate(
                    line_no=start_line,
                    raw_name="",
                    site_url=site_url,
                    jiekou="",
                    site_group="",
                    mcp_token=mcp_token,
                    admin_username="",
                    admin_password="",
                    site_title=site_title,
                    raw_line=raw_snippet,
                    source="mcp_json",
                    secrets=secrets,
                )
            )

    if not consumed_spans:
        return candidates, warnings, text

    parts: list[str] = []
    cursor = 0
    for start, end in consumed_spans:
        parts.append(text[cursor:start])
        parts.append("\n" * text[start:end].count("\n"))
        cursor = end
    parts.append(text[cursor:])
    return candidates, warnings, "".join(parts)


def _merge_candidate(base: _ParsedSiteCandidate, extra: _ParsedSiteCandidate) -> _ParsedSiteCandidate:
    """Merge two candidates sharing the same origin, keeping richer/more specific fields."""
    site_url = base.site_url
    if site_url.startswith("http://") and extra.site_url.startswith("https://"):
        site_url = extra.site_url

    raw_name = base.raw_name or extra.raw_name

    jiekou = base.jiekou
    if extra.jiekou:
        if not jiekou or extra.source == "import_url":
            jiekou = extra.jiekou

    mcp_token = base.mcp_token
    if extra.mcp_token and (not mcp_token or extra.source == "mcp_json"):
        mcp_token = extra.mcp_token

    site_title = base.site_title
    if extra.site_title and (not site_title or extra.source == "mcp_json"):
        site_title = extra.site_title

    site_group = base.site_group or extra.site_group
    admin_username = base.admin_username or extra.admin_username
    admin_password = base.admin_password or extra.admin_password
    secrets = tuple(dict.fromkeys((*base.secrets, *extra.secrets)))

    return _ParsedSiteCandidate(
        line_no=base.line_no,
        raw_name=raw_name,
        site_url=site_url,
        jiekou=jiekou,
        site_group=site_group,
        mcp_token=mcp_token,
        admin_username=admin_username,
        admin_password=admin_password,
        site_title=site_title,
        raw_line=base.raw_line,
        source=base.source if base.source == extra.source else "merged",
        secrets=secrets,
    )


def _extract_remaining_candidates(
    remaining_text: str,
) -> tuple[list[_ParsedSiteCandidate], list[dict[str, str]]]:
    """Parse ``/api/import/archive?token=...`` URLs and traditional delimited lines."""
    candidates: list[_ParsedSiteCandidate] = []
    warnings: list[dict[str, str]] = []

    for line_no, raw_line in enumerate((remaining_text or "").splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        raw_parts = _split_line_columns(stripped)
        if _looks_like_header_row(raw_parts):
            warnings.append(
                _safe_warning(
                    line_no,
                    name="",
                    url="",
                    raw_line=stripped,
                    reason="跳过表头行",
                )
            )
            continue

        is_comma_url_list = (
            "," in stripped
            and "|" not in stripped
            and "\t" not in stripped
            and all(
                part.strip().casefold().startswith(("http://", "https://"))
                for part in stripped.split(",")
                if part.strip()
            )
        )
        is_delimited = "|" in stripped or "\t" in stripped or ("," in stripped and not is_comma_url_list)

        if not is_delimited:
            import_matches = list(_IMPORT_URL_RE.finditer(stripped))
            if import_matches:
                for match in import_matches:
                    matched_url = match.group(0)
                    try:
                        parsed_ep = parse_publish_endpoint(matched_url)
                    except ValueError as exc:
                        warnings.append(
                            _safe_warning(
                                line_no,
                                name="",
                                url=matched_url,
                                raw_line=stripped,
                                reason=str(exc),
                            )
                        )
                        continue
                    candidates.append(
                        _ParsedSiteCandidate(
                            line_no=line_no,
                            raw_name="",
                            site_url=parsed_ep.base_url,
                            jiekou=parsed_ep.publish_endpoint,
                            site_group="",
                            mcp_token="",
                            admin_username="",
                            admin_password="",
                            site_title="",
                            raw_line=stripped,
                            source="import_url",
                        )
                    )
                continue

        if len(raw_parts) == 1:
            raw_name = ""
            raw_target = raw_parts[0]
            rest = ["", "", "", "", ""]
        else:
            padded = (raw_parts + ["", "", "", "", "", "", ""])[:7]
            raw_name, raw_target = padded[0], padded[1]
            rest = padded[2:7]

        site_group, mcp_token, admin_username, admin_password, site_title = rest
        line_secrets = tuple(s for s in (mcp_token, admin_password) if s)

        if any(
            _has_control_characters(val)
            for val in (raw_name, site_group, mcp_token, admin_username, admin_password, site_title)
        ):
            warnings.append(
                _safe_warning(
                    line_no,
                    name=raw_name,
                    url=raw_target,
                    raw_line=stripped,
                    reason="字段包含非法控制字符",
                    secrets=line_secrets,
                )
            )
            continue

        if len(site_group) > SITE_GROUP_MAX_LENGTH:
            warnings.append(
                _safe_warning(
                    line_no,
                    name=raw_name,
                    url=raw_target,
                    raw_line=stripped,
                    reason=f"站点分组超过 {SITE_GROUP_MAX_LENGTH} 个字符",
                    secrets=line_secrets,
                )
            )
            continue

        try:
            site_url, jiekou = _derive_url_and_endpoint(raw_target, mcp_token)
        except ValueError as exc:
            warnings.append(
                _safe_warning(
                    line_no,
                    name=raw_name,
                    url=raw_target,
                    raw_line=stripped,
                    reason=str(exc),
                    secrets=line_secrets,
                )
            )
            continue

        candidates.append(
            _ParsedSiteCandidate(
                line_no=line_no,
                raw_name=raw_name,
                site_url=site_url,
                jiekou=jiekou,
                site_group=site_group,
                mcp_token=mcp_token,
                admin_username=admin_username,
                admin_password=admin_password,
                site_title=site_title,
                raw_line=stripped,
                source="delimited",
                secrets=line_secrets,
            )
        )

    return candidates, warnings


def parse_smart_site_import(
    text: str,
    existing_sites: Sequence[SiteConfig],
    config_path: str | Path,
    *,
    default_group: str = "",
) -> SmartImportResult:
    """Parse mixed AnqiCMS ``mcpServers`` JSON, ``/api/import/archive`` URLs, and delimited lines.

    Entries sharing the same normalized origin (``site_origin_key``) are merged
    within the paste, and entries matching an already-configured site produce an
    updated ``SiteConfig`` in ``updated_sites`` instead of being dropped.
    """
    base_dir = Path(config_path).resolve().parent
    default_group_clean = (default_group or "").strip()
    if _has_control_characters(default_group_clean):
        raise ValueError("默认分组包含非法控制字符")
    if len(default_group_clean) > SITE_GROUP_MAX_LENGTH:
        raise ValueError(f"默认分组超过 {SITE_GROUP_MAX_LENGTH} 个字符")

    seen_names: set[str] = set()
    existing_by_origin: dict[str, SiteConfig] = {}
    existing_by_identity: dict[tuple[str, str, int | None, str], SiteConfig] = {}
    for site in existing_sites or ():
        existing_name = str(getattr(site, "name", "") or "").strip()
        if existing_name:
            seen_names.add(existing_name.casefold())
        existing_url = str(getattr(site, "url", "") or "").strip()
        if existing_url:
            origin = site_origin_key(existing_url)
            if origin:
                existing_by_origin.setdefault(origin, site)
            existing_by_identity.setdefault(site_url_identity(existing_url), site)

    json_candidates, json_warnings, remaining_text = _extract_mcp_json_blocks(
        text or "",
        default_group=default_group_clean,
    )
    line_candidates, line_warnings = _extract_remaining_candidates(remaining_text)

    warnings: list[dict[str, str]] = [*json_warnings, *line_warnings]
    merged_order: list[Any] = []
    merged_by_key: dict[Any, _ParsedSiteCandidate] = {}
    merged_count = 0

    for cand in (*json_candidates, *line_candidates):
        origin = site_origin_key(cand.site_url)
        merge_key: Any = ("origin", origin) if origin else ("id", site_url_identity(cand.site_url))
        if merge_key in merged_by_key:
            merged_by_key[merge_key] = _merge_candidate(merged_by_key[merge_key], cand)
            merged_count += 1
        else:
            merged_by_key[merge_key] = cand
            merged_order.append(merge_key)

    new_sites: list[SiteConfig] = []
    updated_sites: list[SiteConfig] = []
    default_activation = (date.today() + timedelta(days=1)).isoformat()

    for merge_key in merged_order:
        cand = merged_by_key[merge_key]
        origin_key = site_origin_key(cand.site_url)
        url_identity = site_url_identity(cand.site_url)
        existing_match = (
            existing_by_origin.get(origin_key) if origin_key else None
        ) or existing_by_identity.get(url_identity)

        if existing_match is not None:
            updates: dict[str, Any] = {}
            for field_name in (
                "mcp_token",
                "jiekou",
                "site_title",
                "site_group",
                "admin_username",
                "admin_password",
            ):
                new_val = str(getattr(cand, field_name, "") or "").strip()
                if new_val:
                    updates[field_name] = new_val
            if (
                default_group_clean
                and "site_group" not in updates
                and not str(getattr(existing_match, "site_group", "") or "").strip()
            ):
                updates["site_group"] = default_group_clean
            updated_site = replace(existing_match, **updates) if updates else replace(existing_match)
            updated_sites.append(updated_site)
            continue

        if cand.raw_name:
            try:
                site_name = validate_site_key(cand.raw_name)
            except ValueError as exc:
                warnings.append(
                    _safe_warning(
                        cand.line_no,
                        name=cand.raw_name,
                        url=cand.site_url,
                        raw_line=cand.raw_line,
                        reason=str(exc),
                        secrets=cand.secrets,
                    )
                )
                continue
            if site_name.casefold() in seen_names:
                warnings.append(
                    _safe_warning(
                        cand.line_no,
                        name=site_name,
                        url=cand.site_url,
                        raw_line=cand.raw_line,
                        reason=f"站点名重复，已跳过：{site_name}",
                        secrets=cand.secrets,
                    )
                )
                continue
        else:
            derived = site_key_from_url(cand.site_url)
            try:
                derived = validate_site_key(derived)
            except ValueError:
                derived = "site"
            site_name = unique_site_key(derived, seen_names)
            try:
                site_name = validate_site_key(site_name)
            except ValueError:
                site_name = unique_site_key("site", seen_names)

        paths = derive_site_paths(site_name, base_dir)
        site_cfg = SiteConfig(
            name=site_name,
            fid=0,
            url=cand.site_url,
            jiekou=cand.jiekou,
            publish_dir=paths["publish_dir"],
            daily_publish_filename=paths["daily_publish_filename"],
            publish_time="06:00:00",
            publish_times=["06:00:00"],
            interval_time=60,
            daily_publish_limit=60,
            enabled=False,
            image_file=None,
            site_id=str(uuid.uuid4()),
            activation_date=default_activation,
            site_title=cand.site_title,
            site_group=cand.site_group or default_group_clean,
            mcp_token=cand.mcp_token,
            admin_username=cand.admin_username,
            admin_password=cand.admin_password,
            admin_path=DEFAULT_ADMIN_PATH,
        )
        new_sites.append(site_cfg)
        seen_names.add(site_name.casefold())

    return SmartImportResult(
        new_sites=new_sites,
        updated_sites=updated_sites,
        warnings=warnings,
        merged_count=merged_count,
    )


def parse_bulk_new_sites(
    text: str,
    existing_sites: Sequence[SiteConfig],
    config_path: str | Path,
) -> tuple[list[SiteConfig], list[dict[str, str]]]:
    """Parse multi-line bulk import text into ``(new_sites, warnings_or_skipped)``.

    Delegates to :func:`parse_smart_site_import` and surfaces matched existing
    sites / intra-paste duplicate merges as duplicate warnings for backward
    compatibility.
    """
    result = parse_smart_site_import(text, existing_sites, config_path)
    warnings = list(result.warnings)
    redactor = SecretRedactor.from_sites([*result.new_sites, *result.updated_sites])
    for updated in result.updated_sites:
        warnings.append(
            {
                "line": "",
                "name": updated.name,
                "url": redactor(updated.url),
                "raw": "",
                "reason": redactor(f"站点网址重复，已跳过：{updated.url}"),
            }
        )
    for _ in range(result.merged_count):
        warnings.append(
            {
                "line": "",
                "name": "",
                "url": "",
                "raw": "",
                "reason": "同域名重复条目已合并",
            }
        )
    return result.new_sites, warnings


# ------------------------------------------------------------------ HTML decode & discovery helpers


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
                return raw_bytes[: exc.start].decode(encoding, errors="strict")
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


def _extract_html_title(html_text: str) -> str:
    if not html_text:
        return ""
    parser = _TitleParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:  # noqa: BLE001
        return ""
    return parser.title


def _read_response_bytes(response: Any, limit: int = _MAX_HTML_BYTES) -> bytes:
    iter_content = getattr(response, "iter_content", None)
    if callable(iter_content):
        try:
            chunks: list[bytes] = []
            total = 0
            for chunk in iter_content(chunk_size=16384):
                if not chunk:
                    continue
                remaining = limit - total
                if remaining <= 0:
                    break
                chunks.append(chunk[:remaining])
                total += len(chunks[-1])
                if total >= limit:
                    break
            if chunks:
                return b"".join(chunks)
        except Exception:  # noqa: BLE001
            pass
    content = getattr(response, "content", b"")
    if isinstance(content, (bytes, bytearray)):
        return bytes(content[:limit])
    text = getattr(response, "text", "")
    if isinstance(text, str):
        return text.encode("utf-8", errors="replace")[:limit]
    return b""


def _extract_friendlink_count(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    if "code" in payload and payload["code"] not in _SUCCESS_CODES:
        return None
    data = payload.get("data")
    if data is None:
        return 0
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for key in ("list", "items", "links", "data"):
            if key in data:
                candidate = data[key]
                if candidate is None:
                    return 0
                if isinstance(candidate, list):
                    return len(candidate)
        for count_key in ("total", "count"):
            val = data.get(count_key)
            if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
                return val
    return None


def _probe_friendlink_count(
    session: requests.Session,
    site,
    base_url: str,
    *,
    timeout: float | tuple[float, float],
    verify_ssl: bool,
    redactor: SecretRedactor,
) -> tuple[int | None, str]:
    token = import_token(site)
    if not token:
        return None, ""
    endpoint = f"{base_url}/api/friendlink/list"
    try:
        resp = session.get(
            endpoint,
            params={"token": token},
            timeout=timeout,
            allow_redirects=False,
            verify=verify_ssl,
        )
    except requests.RequestException as exc:
        return None, f"友链接口探测失败：{redactor(str(exc))}"

    status = int(getattr(resp, "status_code", 0) or 0)
    if not (200 <= status < 300):
        return None, f"友链接口返回 HTTP {status}"
    try:
        payload = resp.json()
    except ValueError:
        return None, "友链接口响应不是有效 JSON"
    count = _extract_friendlink_count(payload)
    if count is None:
        return None, "友链接口返回格式不兼容"
    return count, ""


def _resolve_full_publish_endpoint(site_url: str, raw_jiekou: str) -> str:
    jiekou = raw_jiekou.strip()
    if not jiekou:
        return ""
    if jiekou.casefold().startswith(("http://", "https://")):
        return jiekou
    if jiekou.startswith("/"):
        return f"{site_url.rstrip('/')}{jiekou}"
    return f"{site_url.rstrip('/')}/{jiekou}"


def _probe_default_category(
    session: requests.Session,
    site,
    site_url: str,
    *,
    timeout: float | tuple[float, float],
    verify_ssl: bool,
    update_site: bool,
) -> tuple[int, str]:
    raw_jiekou = str(getattr(site, "jiekou", "") or "").strip()
    if not raw_jiekou:
        return 0, ""
    try:
        current_fid = int(getattr(site, "fid", 0) or 0)
    except (TypeError, ValueError):
        current_fid = 0
    if current_fid != 0:
        return 0, ""

    full_endpoint = _resolve_full_publish_endpoint(site_url, raw_jiekou)
    if not full_endpoint:
        return 0, ""

    cat_timeout = timeout[1] if isinstance(timeout, tuple) and len(timeout) >= 2 else timeout
    is_real_session = isinstance(session, requests.Session)
    prev_verify = session.verify if is_real_session else None
    if is_real_session:
        session.verify = verify_ssl
    try:
        categories = CategoryService(session=session, timeout=cat_timeout).fetch(full_endpoint)
    except (CategoryServiceError, ValueError, Exception):  # noqa: BLE001
        return 0, ""
    finally:
        if is_real_session and prev_verify is not None:
            session.verify = prev_verify

    if not categories:
        return 0, ""
    first = categories[0]
    if update_site:
        try:
            setattr(site, "fid", first.id)
            if not str(getattr(site, "category_name", "") or "").strip():
                setattr(site, "category_name", first.title)
        except Exception:  # noqa: BLE001
            pass
    return first.id, first.title


def auto_discover_site(
    site,
    *,
    session_factory: Callable[[], requests.Session] = requests.Session,
    probe_fn: Callable[[Any], Any] | None = None,
    timeout: float | tuple[float, float] = (5.0, 10.0),
    verify_ssl: bool = True,
    update_site: bool = True,
) -> SiteDiscoveryResult:
    """Probe a single site's homepage title, friendlink count and optional MCP."""
    site_name = str(getattr(site, "name", "") or "").strip()
    site_url = normalize_site_url(str(getattr(site, "url", "") or ""))
    existing_title = str(getattr(site, "site_title", "") or "").strip()
    redactor = SecretRedactor.from_sites([site])

    if not site_url or not is_valid_site_url(site_url):
        return SiteDiscoveryResult(
            site_name=site_name,
            site_url=site_url,
            reachable=False,
            site_title=existing_title,
            message="站点网址无效",
        )

    notes: list[str] = []
    reachable = False
    http_status: int | None = None
    discovered_title = ""
    link_count: int | None = None
    mcp_ok: bool | None = None
    mcp_tools: tuple[str, ...] = ()
    mcp_probe: McpProbeResult | None = None
    current_version = ""
    default_fid = 0
    default_category_name = ""

    session = session_factory()
    try:
        try:
            resp = session.get(
                site_url,
                headers={"User-Agent": _BROWSER_USER_AGENT},
                timeout=timeout,
                allow_redirects=True,
                verify=verify_ssl,
            )
            http_status = int(getattr(resp, "status_code", 0) or 0)
            reachable = 200 <= http_status < 400
            raw_bytes = _read_response_bytes(resp)
            html_text = _decode_html_bytes(resp, raw_bytes)
            discovered_title = _extract_html_title(html_text)
            if not reachable:
                notes.append(f"首页返回 HTTP {http_status}")
        except requests.exceptions.SSLError:
            notes.append("首页 HTTPS 证书校验失败")
        except requests.exceptions.Timeout:
            notes.append("首页访问超时")
        except requests.RequestException as exc:
            notes.append(f"首页访问失败：{redactor(str(exc))}")

        if update_site and not existing_title and discovered_title:
            try:
                setattr(site, "site_title", discovered_title)
            except Exception:  # noqa: BLE001
                pass

        link_count, link_note = _probe_friendlink_count(
            session,
            site,
            site_url,
            timeout=timeout,
            verify_ssl=verify_ssl,
            redactor=redactor,
        )
        if link_note:
            notes.append(link_note)

        default_fid, default_category_name = _probe_default_category(
            session,
            site,
            site_url,
            timeout=timeout,
            verify_ssl=verify_ssl,
            update_site=update_site,
        )
    finally:
        close = getattr(session, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass

    if probe_fn is not None:
        try:
            probe_res = probe_fn(site)
        except Exception as exc:  # noqa: BLE001
            mcp_ok = False
            notes.append(f"MCP 探测异常：{redactor(str(exc))}")
        else:
            if isinstance(probe_res, McpProbeResult):
                mcp_probe = probe_res
                mcp_ok = bool(probe_res.ok)
                mcp_tools = tuple(tool.name for tool in probe_res.tools)
                current_version = str(getattr(probe_res, "server_version", "") or "").strip()
                if not probe_res.ok and probe_res.message:
                    notes.append(redactor(probe_res.message))
            elif isinstance(probe_res, dict):
                mcp_ok = bool(probe_res.get("ok"))
                raw_tools = probe_res.get("tools") or ()
                mcp_tools = tuple(
                    str(getattr(item, "name", item)) for item in raw_tools if item
                )
                current_version = str(probe_res.get("server_version") or "").strip()
            elif isinstance(probe_res, bool):
                mcp_ok = probe_res

    effective_title = existing_title or discovered_title
    summary = "；".join(notes) if notes else ("探测成功" if reachable else "探测完成")
    return SiteDiscoveryResult(
        site_name=site_name,
        site_url=site_url,
        reachable=reachable,
        http_status=http_status,
        site_title=effective_title,
        discovered_title=discovered_title,
        link_count=link_count,
        mcp_ok=mcp_ok,
        mcp_tools=mcp_tools,
        mcp_probe=mcp_probe,
        message=redact_text(summary),
        current_version=current_version,
        default_fid=default_fid,
        default_category_name=default_category_name,
    )


def auto_discover_sites(
    sites: Sequence[Any],
    *,
    max_concurrency: int = 6,
    session_factory: Callable[[], requests.Session] = requests.Session,
    probe_fn: Callable[[Any], Any] | None = None,
    timeout: float | tuple[float, float] = (5.0, 10.0),
    verify_ssl: bool = True,
    update_site: bool = True,
    progress: Callable[[int, int, SiteDiscoveryResult], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> list[SiteDiscoveryResult]:
    """Concurrently discover metadata for ``sites``, preserving input order."""
    site_list = list(sites)
    total = len(site_list)
    if total == 0:
        return []

    workers = max(1, min(int(max_concurrency), total))
    results: list[SiteDiscoveryResult | None] = [None] * total
    progress_lock = threading.Lock()
    done_count = 0

    def _run_one(index: int, site_obj: Any) -> tuple[int, SiteDiscoveryResult]:
        if cancel_event is not None and cancel_event.is_set():
            name = str(getattr(site_obj, "name", "") or "")
            url = str(getattr(site_obj, "url", "") or "")
            title = str(getattr(site_obj, "site_title", "") or "")
            return index, SiteDiscoveryResult(
                site_name=name,
                site_url=url,
                reachable=False,
                site_title=title,
                message="已取消",
            )
        return index, auto_discover_site(
            site_obj,
            session_factory=session_factory,
            probe_fn=probe_fn,
            timeout=timeout,
            verify_ssl=verify_ssl,
            update_site=update_site,
        )

    if workers == 1:
        for idx, site_obj in enumerate(site_list):
            if cancel_event is not None and cancel_event.is_set():
                break
            _, res = _run_one(idx, site_obj)
            results[idx] = res
            done_count += 1
            if progress is not None:
                progress(done_count, total, res)
        return [r for r in results if r is not None]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {}
        for idx, site_obj in enumerate(site_list):
            if cancel_event is not None and cancel_event.is_set():
                break
            future_map[pool.submit(_run_one, idx, site_obj)] = idx

        for future in as_completed(future_map):
            idx, res = future.result()
            results[idx] = res
            if progress is not None:
                with progress_lock:
                    done_count += 1
                    progress(done_count, total, res)

    return [r for r in results if r is not None]
