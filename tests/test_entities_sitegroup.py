from dataclasses import fields, replace
from pathlib import Path

from models.entities import SiteConfig


SITEGROUP_FIELDS = (
    "site_title",
    "site_group",
    "mcp_token",
    "admin_username",
    "admin_password",
    "admin_path",
)


def _site(**overrides) -> SiteConfig:
    values = dict(
        name="demo",
        fid=1,
        url="https://example.com",
        jiekou="/api/import/archive?token=import-token",
        publish_dir=Path("articles/demo"),
        daily_publish_filename=Path("settings/demo_daily.txt"),
        publish_time="06:00:00",
        interval_time=60,
        daily_publish_limit=10,
    )
    values.update(overrides)
    return SiteConfig(**values)


def test_sitegroup_field_defaults():
    site = _site()

    assert site.site_title == ""
    assert site.site_group == ""
    assert site.mcp_token == ""
    assert site.admin_username == ""
    assert site.admin_password == ""
    assert site.admin_path == "/system/"


def test_sitegroup_fields_are_appended_after_existing_fields():
    names = [item.name for item in fields(SiteConfig)]

    assert names[-len(SITEGROUP_FIELDS):] == list(SITEGROUP_FIELDS)
    assert names.index("image_groups_snapshot") == len(names) - len(SITEGROUP_FIELDS) - 1


def test_legacy_positional_construction_still_works():
    site = SiteConfig(
        "demo",
        3,
        "https://example.com",
        "/api",
        Path("a"),
        Path("b.txt"),
        "06:00:00",
        5,
        7,
    )

    assert site.fid == 3
    assert site.enabled is True
    assert site.admin_path == "/system/"


def test_repr_hides_secrets_but_keeps_non_secret_fields():
    site = _site(
        site_title="Demo Title",
        site_group="group-a",
        mcp_token="mcp-secret-token-123",
        admin_username="admin-user",
        admin_password="admin-secret-pass-456",
        admin_path="/manage/",
    )

    text = repr(site)

    assert "mcp-secret-token-123" not in text
    assert "admin-secret-pass-456" not in text
    assert "mcp_token" not in text
    assert "admin_password" not in text
    assert "group-a" in text
    assert "Demo Title" in text
    assert "admin-user" in text
    assert "/manage/" in text
    assert "mcp-secret-token-123" not in str(site)


def test_replace_preserves_sitegroup_fields():
    site = _site(
        site_title="T",
        site_group="G",
        mcp_token="tok",
        admin_username="u",
        admin_password="p",
        admin_path="/admin/",
    )

    updated = replace(site, fid=9, url="https://other.example.com")

    assert updated.fid == 9
    for name in SITEGROUP_FIELDS:
        assert getattr(updated, name) == getattr(site, name)


def test_equality_considers_sitegroup_fields():
    assert _site() == _site()
    assert _site(site_group="a") != _site(site_group="b")
    assert _site(admin_password="x") != _site(admin_password="y")
