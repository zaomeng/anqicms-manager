from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core import site_credentials as sc
from core.site_credentials import (
    DEFAULT_ADMIN_PATH,
    admin_url,
    display_name,
    effective_mcp_token,
    has_admin_credentials,
    import_token,
    mask_secret,
    normalize_admin_path,
    site_origin_key,
)
from models.entities import SiteConfig


def make_site(**overrides):
    values = dict(
        name="站点A",
        url="https://a.example.com",
        jiekou="https://a.example.com/api/import/archive?token=abc123",
        site_title="",
        mcp_token="",
        admin_username="",
        admin_password="",
        admin_path="/system/",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def make_real_site(**overrides) -> SiteConfig:
    values = dict(
        name="真实站",
        fid=1,
        url="https://real.example.com",
        jiekou="https://real.example.com/api/import/archive?token=realtoken",
        publish_dir=Path("a"),
        daily_publish_filename=Path("b"),
        publish_time="08:00",
        interval_time=10,
        daily_publish_limit=5,
    )
    values.update(overrides)
    return SiteConfig(**values)


# ---------------------------------------------------------------- import_token
def test_import_token_basic():
    assert import_token(make_site()) == "abc123"


def test_import_token_decodes_percent_and_plus():
    site = make_site(jiekou="https://a.example.com/api/import/archive?token=a%2Bb%3D%3D&x=1")
    assert import_token(site) == "a+b=="
    plus = make_site(jiekou="https://a.example.com/archive?token=a+b")
    assert import_token(plus) == "a b"


def test_import_token_with_other_params_and_encoded_name():
    site = make_site(jiekou="https://a.example.com/archive?foo=1&to%6Ben=zzz&bar=2")
    assert import_token(site) == "zzz"


def test_import_token_absent_or_invalid():
    assert import_token(make_site(jiekou="")) == ""
    assert import_token(make_site(jiekou="https://a.example.com/archive")) == ""
    assert import_token(make_site(jiekou="https://a.example.com/archive?other=1")) == ""
    assert import_token(make_site(jiekou="https://a.example.com/archive?token")) == ""
    assert import_token(make_site(jiekou="http://[bad/archive?token=x")) == ""
    assert import_token(SimpleNamespace()) == ""
    assert import_token(make_site(jiekou=None)) == ""


# ------------------------------------------------------- effective_mcp_token
def test_effective_mcp_token_prefers_dedicated_token():
    assert effective_mcp_token(make_site(mcp_token="  dedicated ")) == "dedicated"


def test_effective_mcp_token_falls_back_to_import_token():
    assert effective_mcp_token(make_site()) == "abc123"
    assert effective_mcp_token(make_site(mcp_token="   ")) == "abc123"


def test_effective_mcp_token_empty_when_nothing():
    assert effective_mcp_token(make_site(jiekou="")) == ""


def test_works_with_real_siteconfig():
    site = make_real_site()
    assert import_token(site) == "realtoken"
    assert effective_mcp_token(site) == "realtoken"
    assert admin_url(site) == "https://real.example.com/system/"
    assert display_name(site) == "真实站"
    assert has_admin_credentials(site) is False


# ----------------------------------------------------- normalize_admin_path
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", "/system/"),
        ("   ", "/system/"),
        (None, "/system/"),
        ("/system/", "/system/"),
        ("system", "/system/"),
        ("/system", "/system/"),
        ("/admin/login/", "/admin/login/"),
        (" /my-admin_1.x~ ", "/my-admin_1.x~/"),
        ("/", "/"),
    ],
)
def test_normalize_admin_path_valid(raw, expected):
    assert normalize_admin_path(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "/../etc/",
        "/a/../b/",
        "//evil.com/",
        "/a//b/",
        "http://evil.com/system/",
        "/system/?x=1",
        "/system/#f",
        "/sys tem/",
        "/系统/",
        "\\system\\",
        "/a\nb/",
    ],
)
def test_normalize_admin_path_invalid(raw):
    with pytest.raises(ValueError, match="后台路径无效"):
        normalize_admin_path(raw)


def test_default_admin_path_constant():
    assert DEFAULT_ADMIN_PATH == "/system/"
    assert sc.DEFAULT_ADMIN_PATH is DEFAULT_ADMIN_PATH


# ----------------------------------------------------------------- admin_url
def test_admin_url_default_and_custom():
    assert admin_url(make_site()) == "https://a.example.com/system/"
    assert admin_url(make_site(url="https://a.example.com/")) == "https://a.example.com/system/"
    assert admin_url(make_site(admin_path="manage")) == "https://a.example.com/manage/"
    assert admin_url(make_site(admin_path="")) == "https://a.example.com/system/"


def test_admin_url_invalid_path_raises():
    with pytest.raises(ValueError):
        admin_url(make_site(admin_path="/../x/"))


def test_admin_url_missing_attribute_uses_default():
    site = SimpleNamespace(url="https://x.test")
    assert admin_url(site) == "https://x.test/system/"


# ------------------------------------------------------------ site_origin_key
@pytest.mark.parametrize(
    "a, b",
    [
        ("https://Example.COM", "http://example.com/"),
        ("https://www.example.com/path", "https://example.com"),
        ("https://example.com:443/", "http://example.com:80/x"),
        ("example.com", "https://example.com/"),
        ("https://例子.中国/", "https://xn--fsqu00a.xn--fiqs8s/"),
        ("https://example.com./", "https://example.com"),
    ],
)
def test_site_origin_key_equal(a, b):
    assert site_origin_key(a) == site_origin_key(b) != ""


def test_site_origin_key_keeps_non_default_port_and_distinguishes_hosts():
    assert site_origin_key("https://example.com:8443/") == "example.com:8443"
    assert site_origin_key("https://example.com:8443/") != site_origin_key("https://example.com")
    assert site_origin_key("https://a.example.com") != site_origin_key("https://example.com")
    assert site_origin_key("https://a.com") != site_origin_key("https://b.com")


def test_site_origin_key_degenerate_inputs():
    for value in ("", "   ", "https://", "http://:80", "https://example.com:badport", None, 123):
        assert site_origin_key(value) == ""


# --------------------------------------------------------------- display_name
def test_display_name():
    assert display_name(make_site()) == "站点A"
    assert display_name(make_site(site_title="  友链名 ")) == "友链名"
    assert display_name(make_site(site_title="   ")) == "站点A"


# ----------------------------------------------------------------- mask_secret
def test_mask_secret():
    assert mask_secret("") == "未配置"
    assert mask_secret("   ") == "未配置"
    assert mask_secret(None) == "未配置"
    assert mask_secret("abcdefgh12ab") == "已配置(****12ab)"
    assert mask_secret("short") == "已配置(****)"
    assert "short" not in mask_secret("short")


# ------------------------------------------------------ has_admin_credentials
def test_has_admin_credentials():
    assert has_admin_credentials(make_site(admin_username="u", admin_password="p")) is True
    assert has_admin_credentials(make_site(admin_username="u")) is False
    assert has_admin_credentials(make_site(admin_password="p")) is False
    assert has_admin_credentials(make_site(admin_username=" ", admin_password="p")) is False
    assert has_admin_credentials(SimpleNamespace()) is False
