import pytest

from core.site_factory import (
    ParsedPublishEndpoint,
    build_import_endpoint,
    clone_site_config,
    create_site_from_key,
    create_site_from_url_api_key,
    derive_site_paths,
    derive_categories_url,
    is_valid_publish_endpoint,
    is_valid_site_url,
    parse_publish_endpoint,
    redact_sensitive_url,
    site_key_from_url,
    unique_site_key,
    validate_site_key,
)
from models.entities import SiteConfig


WINDOWS_RESERVED_SITE_KEYS = [
    "CON",
    "prn",
    "AuX",
    "nul",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
    "con.txt",
    "CoM1.log",
    "lPt9.backup",
    "CON .txt",
    "com1 .log",
]


def test_derive_site_paths():
    paths = derive_site_paths("foo", "F:/python/anqicms")
    assert str(paths["publish_dir"]).endswith("文章目录\\foo")
    assert str(paths["daily_publish_filename"]).endswith("设置\\foo_daily.txt")


def test_create_site_from_key_sets_defaults():
    site = create_site_from_key("foo", url="http://example.com", jiekou="/api", publish_times=["09:00:00", "14:00:00"])
    assert site.name == "foo"
    assert site.publish_times == ["09:00:00", "14:00:00"]
    assert site.image_file is not None
    assert site.daily_publish_filename.name == "foo_daily.txt"
    assert site.daily_publish_filename.parent.name == "设置"


def test_recreated_same_name_site_keeps_readable_counter_filename(tmp_path):
    deleted_site = create_site_from_key("same-name", base_dir=tmp_path)
    replacement_site = create_site_from_key("same-name", base_dir=tmp_path)

    assert replacement_site.site_id != deleted_site.site_id
    assert replacement_site.daily_publish_filename == deleted_site.daily_publish_filename
    assert replacement_site.daily_publish_filename.name == "same-name_daily.txt"


def test_clone_site_config_derives_new_paths(tmp_path):
    site = SiteConfig(
        name="old",
        fid=9,
        url="http://example.com",
        jiekou="/api",
        publish_dir=tmp_path / "文章目录" / "old",
        daily_publish_filename=tmp_path / "设置" / "old_daily.txt",
        publish_time="09:00:00",
        publish_times=["09:00:00"],
        interval_time=10,
        daily_publish_limit=7,
    )
    cloned = clone_site_config(site, "new")
    assert cloned.name == "new"
    assert "new" in str(cloned.publish_dir)


def test_clone_site_config_uses_explicit_target_config_base_for_all_paths(tmp_path):
    source_root = tmp_path / "custom-source-root"
    config_root = tmp_path / "target-config-root"
    site = SiteConfig(
        name="old",
        fid=9,
        url="https://example.com",
        jiekou="/api",
        publish_dir=source_root / "arbitrary" / "deep" / "old-articles",
        daily_publish_filename=source_root / "custom" / "old-daily.txt",
        publish_time="09:00:00",
        publish_times=["09:00:00"],
        interval_time=10,
        daily_publish_limit=7,
        image_file=source_root / "custom" / "old-images.txt",
        category_keywords={3: ("实验室",)},
    )

    cloned = clone_site_config(site, "new", base_dir=config_root)

    expected = derive_site_paths("new", config_root)
    assert cloned.publish_dir == expected["publish_dir"]
    assert cloned.daily_publish_filename == expected["daily_publish_filename"]
    assert cloned.image_file == expected["image_file"]
    assert cloned.category_keywords == {3: ("实验室",)}
    for managed_path in (
        cloned.publish_dir,
        cloned.daily_publish_filename,
        cloned.image_file,
    ):
        managed_path.resolve().relative_to(config_root.resolve())


def test_site_key_from_url():
    assert site_key_from_url("https://www.example.com") == "example-com"
    assert site_key_from_url("demo.anqicms.cn") == "demo-anqicms-cn"
    assert site_key_from_url("https://user:password@example.com") == "example-com"


def test_site_url_rejects_embedded_credentials():
    assert not is_valid_site_url("https://user:password@example.com")


def test_site_url_accepts_http_and_https():
    assert is_valid_site_url("http://example.com")
    assert is_valid_site_url("http://127.0.0.1:8000")
    assert is_valid_site_url("http://localhost:8000")
    assert not is_valid_site_url("https://example.com:abc")
    assert not is_valid_site_url("https://example.com:99999")
    assert not is_valid_site_url("https://[::1")
    assert not is_valid_site_url("https://example.com/path?redirect=1")
    assert not is_valid_site_url("https://example.com/path#fragment")
    assert site_key_from_url("https://[::1") == "site"


def test_build_import_endpoint_from_api_key():
    assert build_import_endpoint("abc") == "/api/import/archive?token=abc"
    assert build_import_endpoint("abc", "/custom?x=1") == "/custom?x=1&token=abc"
    assert build_import_endpoint("a+b&c=d") == "/api/import/archive?token=a%2Bb%26c%3Dd"


def test_publish_endpoint_must_be_relative_absolute_path():
    assert is_valid_publish_endpoint("/api/import?token=synthetic")
    assert not is_valid_publish_endpoint(".attacker.test/api")
    assert not is_valid_publish_endpoint("//attacker.test/api")
    assert not is_valid_publish_endpoint("https://attacker.test/api")
    assert not is_valid_publish_endpoint("/api#fragment")


@pytest.mark.parametrize("endpoint", ["/api\\attacker", "/api\x00attacker"])
def test_publish_endpoint_rejects_backslash_and_c0_control_characters(endpoint):
    assert is_valid_publish_endpoint(endpoint) is False


def test_create_site_from_url_api_key_creates_defaults(tmp_path):
    site = create_site_from_url_api_key(
        "https://www.example.com",
        "abc123",
        existing_names={"example-com"},
        base_dir=tmp_path,
        publish_times=["09:00:00"],
    )
    assert site.name == "example-com-2"
    assert site.url == "https://www.example.com"
    assert site.jiekou == "/api/import/archive?token=abc123"
    assert site.publish_dir.exists()
    assert site.daily_publish_filename.exists()
    assert site.image_file is not None and site.image_file.exists()
    assert site.daily_publish_filename.read_text(encoding="utf-8").splitlines()[1] == "0"


@pytest.mark.parametrize("site_key", ["../outside", "..\\outside", "C:\\outside", "name/child", "name*bad"])
def test_site_key_cannot_escape_managed_directories(site_key, tmp_path):
    with pytest.raises(ValueError, match="站点标识"):
        create_site_from_key(site_key, base_dir=tmp_path)


@pytest.mark.parametrize("site_key", WINDOWS_RESERVED_SITE_KEYS)
def test_windows_reserved_device_names_are_invalid_site_keys(site_key, tmp_path):
    with pytest.raises(ValueError, match="站点标识"):
        validate_site_key(site_key)
    with pytest.raises(ValueError, match="站点标识"):
        create_site_from_key(site_key, base_dir=tmp_path)


@pytest.mark.parametrize(
    "site_key",
    ["console", "com10", "lpt10", "auxiliary", "nulled", "com1-site", "x.con"],
)
def test_site_keys_similar_to_windows_device_names_remain_valid(site_key, tmp_path):
    assert validate_site_key(site_key) == site_key
    assert create_site_from_key(site_key, base_dir=tmp_path).name == site_key


def test_site_key_and_unique_site_key_treat_names_case_insensitively():
    assert site_key_from_url("HTTPS://WWW.Example.COM") == "example-com"
    assert unique_site_key("demo", {"Demo"}) == "demo-2"
    assert unique_site_key("demo", {"DEMO", "Demo-2"}) == "demo-3"


@pytest.mark.parametrize(
    "url",
    [
        "https://exa mple.com",
        "https://example.com\\attacker",
        "https://example.com\x01",
        "https://.",
    ],
)
def test_site_url_rejects_obviously_invalid_hostnames(url):
    assert is_valid_site_url(url) is False


@pytest.mark.parametrize(
    "url",
    ["https://例子.测试", "https://192.0.2.1", "http://localhost:8000", "http://127.0.0.1:8000"],
)
def test_site_url_keeps_valid_idn_ip_and_loopback_boundaries(url):
    assert is_valid_site_url(url) is True


def test_parse_full_publish_endpoint_preserves_query_and_derives_site_details():
    endpoint = "https://www.sample.example.com/api/import/archive?token=a%2Bb%25c&debug=1"

    parsed = parse_publish_endpoint(endpoint)

    assert parsed == ParsedPublishEndpoint(
        base_url="https://www.sample.example.com",
        publish_endpoint="/api/import/archive?token=a%2Bb%25c&debug=1",
        categories_url="https://www.sample.example.com/api/import/categories?token=a%2Bb%25c&debug=1",
        site_key="sample",
        full_url=endpoint,
        raw_query="token=a%2Bb%25c&debug=1",
    )
    assert parsed.site_url == parsed.origin == parsed.base_url
    assert derive_categories_url(endpoint) == parsed.categories_url


def test_parse_full_publish_endpoint_makes_site_key_unique_case_insensitively():
    parsed = parse_publish_endpoint(
        "https://www.sample.example.com/api/import/archive?token=secret",
        existing_names={"SaMpLe", "sample-2"},
    )
    assert parsed.site_key == "sample-3"


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:pass@www.sample.example.com/api/import/archive?token=secret",
        "https://www.sample.example.com/api/import/archive?token=secret#fragment",
        "https://www.sample.example.com/api/import/archives?token=secret",
        "https://www.sample.example.com/api/import/archive/?token=secret",
        "https://www.sample.example.com/api/import/archive",
        "https://www.sample.example.com/api/import/archive?token=",
        "https://www.sample.example.com/api/import/archive?token=%20",
        "https://www.sample.example.com/api/import/archive?token=one&token=two",
        "https://www.sample.example.com/api/import/archive?token=secret&",
        "https://www.sample.example.com/api/import/archive?token=%ZZ",
        "https://www.sample.example.com/api\\import/archive?token=secret",
    ],
)
def test_parse_full_publish_endpoint_rejects_invalid_or_ambiguous_urls(endpoint):
    with pytest.raises(ValueError):
        parse_publish_endpoint(endpoint)


def test_redact_sensitive_url_hides_token_and_userinfo():
    value = "request failed: https://user:pass@example.com/api?x=1&token=secret&debug=1"
    redacted = redact_sensitive_url(value)
    assert "secret" not in redacted
    assert "user:pass" not in redacted
    assert "token=***" in redacted
