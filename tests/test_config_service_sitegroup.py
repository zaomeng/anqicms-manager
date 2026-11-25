import configparser
from dataclasses import replace
from pathlib import Path

import pytest

from core.config_service import (
    SITE_GROUP_MAX_LENGTH,
    ConfigLoadError,
    load_config,
    migrate_site_metadata,
    save_config,
)
from core.site_factory import DEFAULT_ADMIN_PATH, clone_site_config, normalize_admin_path
from models.entities import SiteConfig


LEGACY_KEYS = {
    "fid", "category_keywords", "url", "jiekou", "publish_dir", "daily_publish_filename",
    "publish_time", "publish_times", "interval_time", "daily_publish_limit", "enabled", "image_file",
    "site_id", "category_name", "gallery_id", "article_warning_threshold", "activation_date",
}
SITEGROUP_KEYS = {
    "site_title", "site_group", "mcp_token", "admin_username", "admin_password", "admin_path",
}


def _site(tmp_path: Path, name: str = "demo", **overrides) -> SiteConfig:
    values = dict(
        name=name,
        fid=1,
        url=f"https://{name}.example.com",
        jiekou="/api/import/archive?token=import-token",
        publish_dir=tmp_path / "articles" / name,
        daily_publish_filename=tmp_path / "settings" / f"{name}_daily.txt",
        publish_time="06:00:00",
        interval_time=5,
        daily_publish_limit=9,
        image_file=tmp_path / f"{name}_images.txt",
    )
    values.update(overrides)
    return SiteConfig(**values)


def _read_section(config_path: Path, name: str = "demo") -> dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config_path, encoding="utf-8")
    return dict(parser[name])


def _write_legacy_ini(config_path: Path, extra: str = "") -> None:
    config_path.write_text(
        "[demo]\n"
        "fid = 1\n"
        "url = https://demo.example.com\n"
        "jiekou = /api/import/archive?token=import-token\n"
        "publish_dir = ./articles/demo\n"
        "daily_publish_filename = ./settings/demo_daily.txt\n"
        "publish_time = 06:00:00\n"
        f"{extra}",
        encoding="utf-8",
    )


# ---------------------------------------------------------------- legacy compatibility


def test_legacy_ini_loads_sitegroup_defaults(tmp_path):
    config_path = tmp_path / "config.ini"
    _write_legacy_ini(config_path)

    [site] = load_config(config_path)

    assert site.site_title == ""
    assert site.site_group == ""
    assert site.mcp_token == ""
    assert site.admin_username == ""
    assert site.admin_password == ""
    assert site.admin_path == DEFAULT_ADMIN_PATH


def test_default_sitegroup_values_keep_exact_legacy_key_set(tmp_path):
    config_path = tmp_path / "config.ini"
    save_config(config_path, [_site(tmp_path)])

    assert set(_read_section(config_path)) == LEGACY_KEYS


def test_whitespace_only_values_and_default_admin_path_variants_are_not_written(tmp_path):
    config_path = tmp_path / "config.ini"
    site = _site(
        tmp_path,
        site_title="   ",
        site_group=" ",
        mcp_token="",
        admin_username="  ",
        admin_password="",
        admin_path="system",
    )
    save_config(config_path, [site])

    assert set(_read_section(config_path)) == LEGACY_KEYS


@pytest.mark.parametrize("admin_path", ["", "   ", "/system/", "/system", "system/", "system"])
def test_default_admin_path_spellings_are_not_written(tmp_path, admin_path):
    config_path = tmp_path / "config.ini"
    save_config(config_path, [_site(tmp_path, admin_path=admin_path)])

    assert "admin_path" not in _read_section(config_path)
    [loaded] = load_config(config_path)
    assert loaded.admin_path == DEFAULT_ADMIN_PATH


def test_legacy_config_round_trips_byte_identically(tmp_path):
    config_path = tmp_path / "config.ini"
    save_config(config_path, [_site(tmp_path), _site(tmp_path, name="other")])
    original = config_path.read_bytes()

    save_config(config_path, load_config(config_path))

    assert config_path.read_bytes() == original


# ---------------------------------------------------------------- non-default values


def test_non_default_sitegroup_values_round_trip_as_plain_text(tmp_path):
    config_path = tmp_path / "config.ini"
    site = _site(
        tmp_path,
        site_title="演示站点",
        site_group="分组A",
        mcp_token="mcp-token-abc123",
        admin_username="admin",
        admin_password="p@ss;word#%(x)s",
        admin_path="manage",
    )
    save_config(config_path, [site])

    section = _read_section(config_path)
    assert set(section) == LEGACY_KEYS | SITEGROUP_KEYS
    assert section["site_title"] == "演示站点"
    assert section["site_group"] == "分组A"
    assert section["mcp_token"] == "mcp-token-abc123"
    assert section["admin_username"] == "admin"
    assert section["admin_password"] == "p@ss;word#%(x)s"
    assert section["admin_path"] == "/manage/"

    [loaded] = load_config(config_path)
    assert loaded.site_title == "演示站点"
    assert loaded.site_group == "分组A"
    assert loaded.mcp_token == "mcp-token-abc123"
    assert loaded.admin_username == "admin"
    assert loaded.admin_password == "p@ss;word#%(x)s"
    assert loaded.admin_path == "/manage/"

    before = config_path.read_bytes()
    save_config(config_path, [loaded])
    assert config_path.read_bytes() == before


def test_only_non_empty_sitegroup_keys_are_written(tmp_path):
    config_path = tmp_path / "config.ini"
    save_config(config_path, [_site(tmp_path, site_group="g1", admin_username="root")])

    assert set(_read_section(config_path)) == LEGACY_KEYS | {"site_group", "admin_username"}


def test_sitegroup_keys_are_appended_after_legacy_keys(tmp_path):
    config_path = tmp_path / "config.ini"
    save_config(config_path, [_site(tmp_path, site_title="T", admin_path="/admin/")])

    keys = list(_read_section(config_path))
    assert keys[-2:] == ["site_title", "admin_path"]


def test_sitegroup_values_are_stripped_on_save(tmp_path):
    config_path = tmp_path / "config.ini"
    save_config(
        config_path,
        [_site(tmp_path, site_title="  Title  ", site_group=" g ", mcp_token=" tok ", admin_password=" pw ")],
    )

    [loaded] = load_config(config_path)
    assert loaded.site_title == "Title"
    assert loaded.site_group == "g"
    assert loaded.mcp_token == "tok"
    assert loaded.admin_password == "pw"


def test_hand_edited_sitegroup_keys_are_loaded(tmp_path):
    config_path = tmp_path / "config.ini"
    _write_legacy_ini(
        config_path,
        "site_title = Hand Title\n"
        "site_group = hand\n"
        "mcp_token = hand-token\n"
        "admin_username = boss\n"
        "admin_password = secret%value\n"
        "admin_path = /backend\n",
    )

    [site] = load_config(config_path)

    assert site.site_title == "Hand Title"
    assert site.site_group == "hand"
    assert site.mcp_token == "hand-token"
    assert site.admin_username == "boss"
    assert site.admin_password == "secret%value"
    assert site.admin_path == "/backend/"


def test_untouched_replace_preserves_sitegroup_fields_through_save(tmp_path):
    config_path = tmp_path / "config.ini"
    site = _site(tmp_path, site_group="g", mcp_token="tok", admin_path="/admin/")
    save_config(config_path, [replace(site, daily_publish_limit=3)])

    [loaded] = load_config(config_path)
    assert loaded.daily_publish_limit == 3
    assert (loaded.site_group, loaded.mcp_token, loaded.admin_path) == ("g", "tok", "/admin/")


def test_migrate_site_metadata_preserves_sitegroup_fields(tmp_path):
    config_path = tmp_path / "config.ini"
    _write_legacy_ini(
        config_path,
        "site_group = keep\nmcp_token = keep-token\nadmin_password = keep-pass\nadmin_path = /panel/\n",
    )
    sites = load_config(config_path)

    assert migrate_site_metadata(config_path, sites) is True

    [loaded] = load_config(config_path)
    assert loaded.site_id
    assert loaded.site_group == "keep"
    assert loaded.mcp_token == "keep-token"
    assert loaded.admin_password == "keep-pass"
    assert loaded.admin_path == "/panel/"


# ---------------------------------------------------------------- validation


INVALID_ADMIN_PATHS = [
    "../system/",
    "/a/../b/",
    "//evil.example.com/",
    "/a//b/",
    "https://evil.example.com/system/",
    "javascript:alert(1)",
    "/system/?x=1",
    "/system/#frag",
    "/sys tem/",
    "/system\\admin/",
    "/系统/",
    "/user@host/",
    "/a%2e%2e/",
    "/a\tb/",
]


@pytest.mark.parametrize("admin_path", INVALID_ADMIN_PATHS)
def test_invalid_admin_path_is_rejected_on_save(tmp_path, admin_path):
    config_path = tmp_path / "config.ini"

    with pytest.raises(ConfigLoadError, match="后台路径无效"):
        save_config(config_path, [_site(tmp_path, admin_path=admin_path)])
    assert not config_path.exists()


@pytest.mark.parametrize(
    "admin_path",
    [path for path in INVALID_ADMIN_PATHS if "\t" not in path and "#" not in path],
)
def test_invalid_admin_path_is_rejected_on_load(tmp_path, admin_path):
    config_path = tmp_path / "config.ini"
    _write_legacy_ini(config_path, f"admin_path = {admin_path}\n")

    with pytest.raises(ConfigLoadError, match="站点 demo 的后台路径无效"):
        load_config(config_path)


@pytest.mark.parametrize("field_name", ["site_title", "site_group", "mcp_token", "admin_username", "admin_password"])
@pytest.mark.parametrize("bad_value", ["line1\nline2", "a\rb", "a\x00b", "a\x1bb", "a\x7fb"])
def test_control_characters_are_rejected_on_save(tmp_path, field_name, bad_value):
    config_path = tmp_path / "config.ini"

    with pytest.raises(ConfigLoadError, match="站点 demo 的.*控制字符"):
        save_config(config_path, [_site(tmp_path, **{field_name: bad_value})])
    assert not config_path.exists()


def test_tab_inside_value_is_rejected_on_save(tmp_path):
    with pytest.raises(ConfigLoadError, match="控制字符"):
        save_config(tmp_path / "config.ini", [_site(tmp_path, site_title="a\tb")])


def test_site_group_length_limit(tmp_path):
    config_path = tmp_path / "config.ini"
    save_config(config_path, [_site(tmp_path, site_group="g" * SITE_GROUP_MAX_LENGTH)])
    [loaded] = load_config(config_path)
    assert loaded.site_group == "g" * SITE_GROUP_MAX_LENGTH

    with pytest.raises(ConfigLoadError, match="站点分组不能超过 64"):
        save_config(config_path, [_site(tmp_path, site_group="g" * (SITE_GROUP_MAX_LENGTH + 1))])


def test_overlong_site_group_is_rejected_on_load(tmp_path):
    config_path = tmp_path / "config.ini"
    _write_legacy_ini(config_path, f"site_group = {'x' * (SITE_GROUP_MAX_LENGTH + 1)}\n")

    with pytest.raises(ConfigLoadError, match="站点分组"):
        load_config(config_path)


def test_multiline_ini_value_is_rejected_on_load(tmp_path):
    config_path = tmp_path / "config.ini"
    _write_legacy_ini(config_path, "site_title = first\n  continuation\n")

    with pytest.raises(ConfigLoadError, match="站点显示名不能包含换行"):
        load_config(config_path)


def test_non_string_sitegroup_value_is_rejected_on_save(tmp_path):
    with pytest.raises(ConfigLoadError, match="站点分组无效"):
        save_config(tmp_path / "config.ini", [_site(tmp_path, site_group=123)])


def test_none_sitegroup_values_are_treated_as_empty(tmp_path):
    config_path = tmp_path / "config.ini"
    save_config(config_path, [_site(tmp_path, site_title=None, admin_path=None)])

    assert set(_read_section(config_path)) == LEGACY_KEYS


@pytest.mark.parametrize("reserved_name", ["friendlink_ledger.json", "integration_settings.json"])
def test_sitegroup_runtime_files_are_reserved(tmp_path, reserved_name):
    config_path = tmp_path / "config.ini"
    site = _site(tmp_path, daily_publish_filename=tmp_path / "设置" / reserved_name)

    with pytest.raises(ConfigLoadError, match="保留"):
        save_config(config_path, [site])


@pytest.mark.parametrize("reserved_name", ["friendlink_ledger.json", "integration_settings.json"])
def test_sitegroup_runtime_files_are_reserved_on_load(tmp_path, reserved_name):
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[demo]\nfid = 1\nurl = https://demo.example.com\njiekou = /api\n"
        f"publish_dir = ./articles/demo\ndaily_publish_filename = ./设置/{reserved_name}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigLoadError, match="保留"):
        load_config(config_path)


# ---------------------------------------------------------------- normalize_admin_path


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", "/system/"),
        ("   ", "/system/"),
        (None, "/system/"),
        ("/system/", "/system/"),
        ("system", "/system/"),
        ("/admin", "/admin/"),
        ("admin/", "/admin/"),
        (" /a/b ", "/a/b/"),
        ("/A-Z_0.9~x/", "/A-Z_0.9~x/"),
        ("/", "/"),
    ],
)
def test_normalize_admin_path_valid(raw, expected):
    assert normalize_admin_path(raw) == expected


@pytest.mark.parametrize("raw", INVALID_ADMIN_PATHS + ["..", "/./../", "http:/x", "ftp://x/"])
def test_normalize_admin_path_invalid(raw):
    with pytest.raises(ValueError, match="后台路径无效"):
        normalize_admin_path(raw)


# ---------------------------------------------------------------- clone_site_config


def test_clone_site_config_copies_group_and_admin_path_only(tmp_path):
    source = _site(
        tmp_path,
        site_title="Source Title",
        site_group="group-x",
        mcp_token="source-mcp-token",
        admin_username="source-admin",
        admin_password="source-password",
        admin_path="/panel/",
    )

    clone = clone_site_config(source, "copy", new_url="https://copy.example.com", base_dir=tmp_path)

    assert clone.site_group == "group-x"
    assert clone.admin_path == "/panel/"
    assert clone.site_title == ""
    assert clone.mcp_token == ""
    assert clone.admin_username == ""
    assert clone.admin_password == ""
    assert clone.name == "copy"
    assert clone.site_id and clone.site_id != source.site_id


def test_clone_site_config_defaults_empty_admin_path(tmp_path):
    clone = clone_site_config(_site(tmp_path, admin_path=""), "copy", base_dir=tmp_path)

    assert clone.admin_path == DEFAULT_ADMIN_PATH
    assert clone.site_group == ""


def test_cloned_site_saves_without_credentials(tmp_path):
    config_path = tmp_path / "config.ini"
    source = _site(tmp_path, site_group="g", mcp_token="tok", admin_password="pw")
    clone = clone_site_config(source, "copy", new_url="https://copy.example.com", base_dir=tmp_path)

    save_config(config_path, [source, clone])

    copy_section = _read_section(config_path, "copy")
    assert copy_section["site_group"] == "g"
    assert "mcp_token" not in copy_section
    assert "admin_password" not in copy_section
