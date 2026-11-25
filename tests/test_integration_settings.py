from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from core.integration_settings import (
    SETTINGS_FILENAME,
    AdminJumpSettings,
    FriendLinkSettings,
    IntegrationSettings,
    McpSettings,
    UpgradeSettings,
    load_integration_settings,
    save_integration_settings,
    settings_path,
)


def test_defaults_match_blueprint():
    s = IntegrationSettings()
    assert s.mcp == McpSettings(
        endpoint_path="/api/mcp",
        protocol_version="2025-06-18",
        fallback_protocol_versions=["2025-03-26", "2024-11-05"],
        connect_timeout=5.0,
        read_timeout=20.0,
        verify_ssl=True,
    )
    f = s.friendlink
    assert (f.max_concurrency, f.request_timeout, f.check_timeout) == (6, 15.0, 8.0)
    assert (f.per_request_delay_s, f.default_nofollow, f.default_remark) == (0.2, False, "站群互链")
    j = s.admin_jump
    assert j.enable_ticket is True and j.ticket_tool_names == []
    assert j.ticket_keywords == [
        "login_ticket", "ticket", "jump", "switch_site", "admin_login", "登录", "跳转",
    ]
    assert j.ticket_url_template == "" and j.token_handoff_url_template == ""
    assert j.enable_silent_login is True and j.autopost_login is False
    assert j.login_api_paths == ["api/login", "api/user/login"]
    assert (j.batch_confirm_threshold, j.batch_max, j.batch_stagger_ms, j.bridge_ttl_s) == (5, 30, 400, 60.0)
    u = s.upgrade
    assert u.check_tool_names == [] and u.upgrade_tool_names == []
    assert u.check_keywords == ["check_update", "upgrade_check", "version", "system_info", "检查更新", "版本"]
    assert u.upgrade_keywords == ["system_upgrade", "upgrade", "update_system", "升级"]
    assert u.rest_version_paths == [] and u.rest_upgrade_paths == []
    assert (u.max_concurrency, u.upgrade_call_timeout_s, u.restart_wait_s) == (3, 180.0, 8.0)
    assert (u.health_timeout_s, u.health_interval_s) == (240.0, 5.0)


def test_default_lists_are_not_shared_between_instances():
    a, b = IntegrationSettings(), IntegrationSettings()
    a.mcp.fallback_protocol_versions.append("x")
    a.admin_jump.ticket_keywords.append("x")
    assert "x" not in b.mcp.fallback_protocol_versions
    assert "x" not in b.admin_jump.ticket_keywords


def test_settings_path(tmp_path):
    config = tmp_path / "cfg" / "config.ini"
    path = settings_path(config)
    assert path == tmp_path / "cfg" / "设置" / SETTINGS_FILENAME
    assert SETTINGS_FILENAME == "integration_settings.json"
    assert settings_path(str(config)) == path


def test_missing_file_gives_defaults(tmp_path):
    assert load_integration_settings(tmp_path / "nope.json") == IntegrationSettings()


@pytest.mark.parametrize("content", ["", "{not json", "[1, 2]", "null", '"text"', "123", "\x00\x01"])
def test_corrupt_or_wrong_root_gives_defaults(tmp_path, content):
    path = tmp_path / "s.json"
    path.write_text(content, encoding="utf-8")
    assert load_integration_settings(path) == IntegrationSettings()


def test_binary_garbage_gives_defaults(tmp_path):
    path = tmp_path / "s.json"
    path.write_bytes(b"\xff\xfe\x00\x80\x81")
    assert load_integration_settings(path) == IntegrationSettings()


def test_directory_path_gives_defaults(tmp_path):
    assert load_integration_settings(tmp_path) == IntegrationSettings()


def test_partial_file_overrides_only_given_fields(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps({"mcp": {"verify_ssl": False, "read_timeout": 30}, "upgrade": {"max_concurrency": 5}}),
        encoding="utf-8",
    )
    s = load_integration_settings(path)
    assert s.mcp.verify_ssl is False
    assert s.mcp.read_timeout == 30.0 and isinstance(s.mcp.read_timeout, float)
    assert s.mcp.endpoint_path == "/api/mcp"
    assert s.upgrade.max_concurrency == 5
    assert s.friendlink == FriendLinkSettings()
    assert s.admin_jump == AdminJumpSettings()


def test_unknown_keys_ignored(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps({"extra": 1, "mcp": {"bogus": True, "endpoint_path": "/mcp"}, "other": {"a": 1}}),
        encoding="utf-8",
    )
    s = load_integration_settings(path)
    assert s.mcp.endpoint_path == "/mcp"
    assert not hasattr(s.mcp, "bogus")


def test_wrong_types_fall_back_per_field(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps(
            {
                "mcp": {
                    "endpoint_path": 5,
                    "protocol_version": None,
                    "fallback_protocol_versions": "2024-11-05",
                    "connect_timeout": "fast",
                    "read_timeout": 12.5,
                    "verify_ssl": "no",
                },
                "friendlink": {"max_concurrency": "6", "default_nofollow": 1, "default_remark": ["x"]},
                "admin_jump": {"ticket_keywords": ["ok", 3], "batch_max": True, "autopost_login": "yes"},
                "upgrade": {"restart_wait_s": [1], "check_tool_names": ["a", "b"]},
            }
        ),
        encoding="utf-8",
    )
    s = load_integration_settings(path)
    d = IntegrationSettings()
    assert s.mcp.endpoint_path == d.mcp.endpoint_path
    assert s.mcp.protocol_version == d.mcp.protocol_version
    assert s.mcp.fallback_protocol_versions == d.mcp.fallback_protocol_versions
    assert s.mcp.connect_timeout == d.mcp.connect_timeout
    assert s.mcp.read_timeout == 12.5  # valid sibling kept
    assert s.mcp.verify_ssl is True
    assert s.friendlink.max_concurrency == 6
    assert s.friendlink.default_nofollow is False
    assert s.friendlink.default_remark == "站群互链"
    assert s.admin_jump.ticket_keywords == d.admin_jump.ticket_keywords
    assert s.admin_jump.batch_max == 30
    assert s.admin_jump.autopost_login is False
    assert s.upgrade.restart_wait_s == 8.0
    assert s.upgrade.check_tool_names == ["a", "b"]


def test_wrong_section_types_ignored(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"mcp": [1], "friendlink": "x", "admin_jump": None, "upgrade": 3}), encoding="utf-8")
    assert load_integration_settings(path) == IntegrationSettings()


def test_out_of_range_numbers_fall_back(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps(
            {
                "mcp": {"connect_timeout": 0, "read_timeout": -3},
                "friendlink": {"max_concurrency": 0, "per_request_delay_s": -1, "check_timeout": 1e999},
                "admin_jump": {"batch_stagger_ms": -5, "batch_max": 0},
                "upgrade": {"max_concurrency": 2.5, "health_interval_s": 0},
            }
        ).replace("1e999", "1e999"),
        encoding="utf-8",
    )
    s = load_integration_settings(path)
    d = IntegrationSettings()
    assert s.mcp.connect_timeout == d.mcp.connect_timeout
    assert s.mcp.read_timeout == d.mcp.read_timeout
    assert s.friendlink.max_concurrency == 6
    assert s.friendlink.per_request_delay_s == 0.2
    assert s.friendlink.check_timeout == 8.0  # infinity rejected
    assert s.admin_jump.batch_stagger_ms == 400
    assert s.admin_jump.batch_max == 30
    assert s.upgrade.max_concurrency == 3
    assert s.upgrade.health_interval_s == 5.0


def test_zero_allowed_for_delay_and_stagger(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps({"friendlink": {"per_request_delay_s": 0}, "admin_jump": {"batch_stagger_ms": 0}}),
        encoding="utf-8",
    )
    s = load_integration_settings(path)
    assert s.friendlink.per_request_delay_s == 0.0
    assert s.admin_jump.batch_stagger_ms == 0


def test_endpoint_path_must_start_with_slash(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"mcp": {"endpoint_path": "api/mcp"}}), encoding="utf-8")
    assert load_integration_settings(path).mcp.endpoint_path == "/api/mcp"


def test_utf8_bom_accepted(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"friendlink": {"default_remark": "互链"}}), encoding="utf-8-sig")
    assert load_integration_settings(path).friendlink.default_remark == "互链"


def test_save_and_round_trip(tmp_path):
    path = tmp_path / "设置" / SETTINGS_FILENAME
    s = IntegrationSettings()
    s.mcp.verify_ssl = False
    s.friendlink.default_remark = "我的互链"
    s.admin_jump.ticket_tool_names = ["custom_ticket"]
    s.upgrade.rest_version_paths = ["api/version"]
    save_integration_settings(path, s)
    text = path.read_text(encoding="utf-8")
    assert "我的互链" in text  # ensure_ascii=False
    assert text.startswith("{\n  ")  # indent=2
    assert load_integration_settings(path) == s
    assert asdict(load_integration_settings(path)) == json.loads(text)
    assert not list(path.parent.glob("*.tmp"))


def test_save_overwrites_existing(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("garbage", encoding="utf-8")
    save_integration_settings(path, IntegrationSettings())
    assert load_integration_settings(path) == IntegrationSettings()
