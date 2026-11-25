"""Helpers for the per-site credential fields added by the site-group feature.

All functions are pure and duck-typed on ``site`` (``getattr`` with defaults),
so they work with ``SiteConfig`` and with light test doubles alike.
"""

from __future__ import annotations

from urllib.parse import unquote_plus, urlsplit

# Single canonical validator lives in site_factory (also used by config_service).
from core.site_factory import DEFAULT_ADMIN_PATH, normalize_admin_path

__all__ = [
    "DEFAULT_ADMIN_PATH",
    "normalize_admin_path",
    "import_token",
    "effective_mcp_token",
    "admin_url",
    "site_origin_key",
    "display_name",
    "mask_secret",
    "has_admin_credentials",
]

_DEFAULT_PORTS = {80, 443}


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def import_token(site) -> str:
    """Token embedded in ``site.jiekou`` query string (``""`` if absent)."""
    jiekou = _text(getattr(site, "jiekou", "")).strip()
    if not jiekou:
        return ""
    try:
        query = urlsplit(jiekou).query
    except ValueError:
        return ""
    for item in query.split("&"):
        raw_name, separator, raw_value = item.partition("=")
        if separator and unquote_plus(raw_name) == "token":
            return unquote_plus(raw_value).strip()
    return ""


def effective_mcp_token(site) -> str:
    """Dedicated MCP token when set, otherwise the import token."""
    configured = _text(getattr(site, "mcp_token", "")).strip()
    if configured:
        return configured
    return import_token(site)


def admin_url(site) -> str:
    base = _text(getattr(site, "url", "")).strip().rstrip("/")
    path = normalize_admin_path(getattr(site, "admin_path", DEFAULT_ADMIN_PATH))
    return f"{base}{path}"


def site_origin_key(url: str) -> str:
    """Comparable ``host[:port]`` key: scheme/www./default ports are ignored.

    Returns ``""`` when ``url`` has no usable host.
    """
    text = _text(url).strip()
    if not text:
        return ""
    if "://" not in text:
        text = "//" + text
    try:
        parsed = urlsplit(text)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return ""
    host = host.strip(".").casefold()
    if not host:
        return ""
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return ""
    if port is not None and port not in _DEFAULT_PORTS:
        return f"{host}:{port}"
    return host


def display_name(site) -> str:
    title = _text(getattr(site, "site_title", "")).strip()
    return title or _text(getattr(site, "name", ""))


def mask_secret(value: str) -> str:
    text = _text(value).strip()
    if not text:
        return "未配置"
    if len(text) < 8:
        return "已配置(****)"
    return f"已配置(****{text[-4:]})"


def has_admin_credentials(site) -> bool:
    username = _text(getattr(site, "admin_username", "")).strip()
    password = _text(getattr(site, "admin_password", "")).strip()
    return bool(username and password)
