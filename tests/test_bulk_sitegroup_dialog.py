"""Unit and Tk smoke tests for BulkSiteGroupDialog and ConfigCenter integration."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path

import pytest

from core.category_service import CategoryService
from core.config_service import load_config
from core.gallery_service import GalleryService
from core.site_factory import create_site_from_key, parse_publish_endpoint
from ui.bulk_sitegroup_dialog import (
    BulkSiteGroupDialog,
    match_site_by_identifier,
    parse_bulk_text_lines,
    validate_bulk_fields,
)
from ui.config_center import ConfigCenter


class FakeCategoryService(CategoryService):
    def fetch(self, _endpoint):
        return []


def make_root():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk environment unavailable: {exc}")
    root.withdraw()
    return root


def make_site(
    tmp_path: Path,
    *,
    name: str,
    url: str,
    token: str = "tok",
    fid: int = 1,
):
    parsed = parse_publish_endpoint(f"{url.rstrip('/')}/api/import/archive?token={token}")
    site = create_site_from_key(
        name,
        url=parsed.base_url,
        jiekou=parsed.publish_endpoint,
        fid=fid,
        base_dir=tmp_path,
    )
    site.category_name = "新闻"
    return site


def collect_button_labels(widget) -> list[str]:
    labels: list[str] = []
    if widget.winfo_class() == "TButton":
        labels.append(str(widget.cget("text")))
    for child in widget.winfo_children():
        labels.extend(collect_button_labels(child))
    return labels


# ---------------------------------------------------------------------------
# Pure unit tests: validation, matching, and text parsing
# ---------------------------------------------------------------------------


def test_validate_bulk_fields_omits_blank_normalizes_admin_path_and_rejects_invalid():
    cleaned = validate_bulk_fields(
        {
            "site_group": "  矩阵A ",
            "mcp_token": "   ",
            "admin_username": "",
            "admin_password": "secret",
            "admin_path": "manage",
        }
    )
    assert cleaned == {
        "site_group": "矩阵A",
        "admin_password": "secret",
        "admin_path": "/manage/",
    }

    with pytest.raises(ValueError, match="后台路径无效"):
        validate_bulk_fields({"admin_path": "/../escape"})

    with pytest.raises(ValueError, match="64"):
        validate_bulk_fields({"site_group": "组" * 65})

    with pytest.raises(ValueError, match="控制字符"):
        validate_bulk_fields({"mcp_token": "bad\ntoken"})


def test_match_site_by_identifier_supports_name_and_domain_variants(tmp_path):
    sites = [
        make_site(tmp_path, name="sz-banjia", url="https://banjia1.example.com"),
        make_site(tmp_path, name="www-demo", url="https://www.demo.example.com"),
        make_site(tmp_path, name="port-site", url="http://panel.example.net:8080"),
    ]

    assert match_site_by_identifier("sz-banjia", sites)[0].name == "sz-banjia"
    assert match_site_by_identifier("SZ-BANJIA", sites)[0].name == "sz-banjia"
    assert match_site_by_identifier("banjia1.example.com", sites)[0].name == "sz-banjia"
    assert match_site_by_identifier("http://banjia1.example.com", sites)[0].name == "sz-banjia"
    assert match_site_by_identifier("https://www.banjia1.example.com/system/", sites)[0].name == "sz-banjia"
    assert match_site_by_identifier("demo.example.com", sites)[0].name == "www-demo"
    assert match_site_by_identifier("panel.example.net:8080", sites)[0].name == "port-site"

    unmatched, reason = match_site_by_identifier("unknown.example.com", sites)
    assert unmatched is None
    assert "未找到" in reason


def test_match_site_by_identifier_handles_scheme_disambiguation(tmp_path):
    sites = [
        make_site(tmp_path, name="http-site", url="http://shared.example.com"),
        make_site(tmp_path, name="https-site", url="https://shared.example.com"),
    ]
    assert match_site_by_identifier("http://shared.example.com", sites)[0].name == "http-site"
    assert match_site_by_identifier("https://shared.example.com", sites)[0].name == "https-site"

    ambiguous, reason = match_site_by_identifier("shared.example.com", sites)
    assert ambiguous is None
    assert "多个站点" in reason


def test_parse_bulk_text_lines_delimiters_comments_blank_columns_and_errors(tmp_path):
    sites = [
        make_site(tmp_path, name="sz-banjia", url="https://banjia1.example.com"),
        make_site(tmp_path, name="alpha", url="https://alpha.example.com"),
    ]
    raw = "\ufeff" + "\n".join(
        [
            "# 注释行应忽略",
            "",
            "banjia1.example.com|搬家组|tok-1|admin1|pass1|搬家1站",
            "alpha\t资讯组\t\troot",
            "http://banjia1.example.com,,,,pass1-new,搬家新标题",
            "alpha|||||",
            "missing.example.com|组A|tok-x",
            "alpha|g|t|u|p|title|extra-col",
            f"alpha|{'组' * 65}",
        ]
    )
    records, aggregated = parse_bulk_text_lines(raw, sites)

    assert [rec.status for rec in records] == [
        "matched",
        "matched",
        "matched",
        "unchanged",
        "unmatched",
        "error",
        "error",
    ]
    assert aggregated == {
        "sz-banjia": {
            "site_group": "搬家组",
            "mcp_token": "tok-1",
            "admin_username": "admin1",
            "admin_password": "pass1-new",
            "site_title": "搬家新标题",
        },
        "alpha": {
            "site_group": "资讯组",
            "admin_username": "root",
        },
    }


# ---------------------------------------------------------------------------
# Tk smoke tests: BulkSiteGroupDialog & ConfigCenter integration
# ---------------------------------------------------------------------------


def test_bulk_dialog_tab1_preselects_and_applies_non_blank_fields(tmp_path):
    root = make_root()
    sites = [
        make_site(tmp_path, name="site1", url="https://s1.example.com"),
        make_site(tmp_path, name="site2", url="https://s2.example.com"),
    ]
    sites[0].site_group = "原有分组"
    applied = []

    dialog = BulkSiteGroupDialog(root, sites, lambda data: applied.append(data) or True)
    try:
        assert dialog.title() == "批量配置站群信息"
        tabs = [dialog.notebook.tab(tab_id, "text") for tab_id in dialog.notebook.tabs()]
        assert tabs == ["批量赋值", "批量文本导入"]
        assert [s.name for s in dialog.site_picker.selected_sites()] == ["site1", "site2"]

        dialog.site_picker.set_selected(["site2"])
        dialog.site_group_var.set("新分组")
        dialog.mcp_token_var.set("")  # blank -> do not overwrite
        dialog.admin_username_var.set("admin")
        dialog.admin_path_var.set("cms-admin")

        assert dialog._apply_assign_tab() is True
        assert applied == [
            {
                "site2": {
                    "site_group": "新分组",
                    "admin_username": "admin",
                    "admin_path": "/cms-admin/",
                }
            }
        ]
        assert not dialog.winfo_exists()
    finally:
        if dialog.winfo_exists():
            dialog.destroy()
        root.destroy()


def test_bulk_dialog_tab1_rejects_invalid_inputs_without_closing(tmp_path):
    root = make_root()
    sites = [make_site(tmp_path, name="site1", url="https://s1.example.com")]
    applied = []
    dialog = BulkSiteGroupDialog(
        root,
        sites,
        lambda data: applied.append(data) or True,
        initial_selected_names=["site1"],
    )
    try:
        # Empty fields rejected
        assert dialog._apply_assign_tab() is False
        assert "至少填写一项" in dialog.message_var.get()

        # Invalid admin_path rejected
        dialog.admin_path_var.set("/../bad")
        assert dialog._apply_assign_tab() is False
        assert "后台路径无效" in dialog.message_var.get()

        # No sites selected rejected
        dialog.admin_path_var.set("system")
        dialog.site_picker.select_none()
        assert dialog._apply_assign_tab() is False
        assert "至少一个目标站点" in dialog.message_var.get()
        assert applied == []
        assert dialog.winfo_exists()
    finally:
        if dialog.winfo_exists():
            dialog.destroy()
        root.destroy()


def test_bulk_dialog_tab2_preview_and_apply_with_unmatched_prompt(tmp_path, monkeypatch):
    root = make_root()
    sites = [
        make_site(tmp_path, name="sz-banjia", url="https://banjia1.example.com"),
        make_site(tmp_path, name="alpha", url="https://alpha.example.com"),
    ]
    applied = []
    dialog = BulkSiteGroupDialog(root, sites, lambda data: applied.append(data) or True)
    try:
        dialog.import_text.insert(
            "1.0",
            "banjia1.example.com|搬家组|mcp-1|admin|pwd|搬家1站\n"
            "unknown.example.com|缺失组\n",
        )
        records, _ = dialog._preview_text_import()
        assert len(records) == 2
        assert len(dialog.preview_tree.get_children()) == 2
        assert "匹配 1 条" in dialog.import_summary_var.get()
        assert "未匹配 1 条" in dialog.import_summary_var.get()

        # User declines applying when unmatched exists
        monkeypatch.setattr("ui.bulk_sitegroup_dialog.messagebox.askyesno", lambda *_, **__: False)
        assert dialog._apply_text_import() is False
        assert applied == []
        assert dialog.winfo_exists()

        # User confirms applying matched sites
        monkeypatch.setattr("ui.bulk_sitegroup_dialog.messagebox.askyesno", lambda *_, **__: True)
        assert dialog._apply_text_import() is True
        assert applied == [
            {
                "sz-banjia": {
                    "site_group": "搬家组",
                    "mcp_token": "mcp-1",
                    "admin_username": "admin",
                    "admin_password": "pwd",
                    "site_title": "搬家1站",
                }
            }
        ]
        assert not dialog.winfo_exists()
    finally:
        if dialog.winfo_exists():
            dialog.destroy()
        root.destroy()


def test_config_center_bulk_sitegroup_button_updates_drafts_editor_and_saves(tmp_path):
    root = make_root()
    sites = [
        make_site(tmp_path, name="sz-banjia", url="https://banjia1.example.com"),
        make_site(tmp_path, name="alpha", url="https://alpha.example.com"),
    ]
    sites[0].mcp_token = "keep-existing-token"
    sites[0].site_title = "原搬家标题"

    config_center = ConfigCenter(
        root,
        tmp_path / "config.ini",
        sites,
        lambda _sites: None,
        category_service=FakeCategoryService(),
        gallery_service=GalleryService(tmp_path / "设置"),
    )
    try:
        assert "批量配置站群" in collect_button_labels(config_center)
        assert not config_center._has_unsaved_changes()

        # Open bulk dialog while row 0 (sz-banjia) is selected in editor
        bulk = config_center._open_bulk_sitegroup_dialog()
        assert bulk is not None
        bulk.site_picker.set_selected(["sz-banjia", "alpha"])
        bulk.site_group_var.set("华南站群")
        bulk.admin_username_var.set("batch-admin")
        bulk.admin_path_var.set("panel")
        assert bulk._apply_assign_tab() is True

        # Drafts updated, current editor refreshed, dirty flag set
        assert config_center._has_unsaved_changes()
        assert config_center.rows[0]["site_group"].get() == "华南站群"
        assert config_center.rows[0]["admin_username"].get() == "batch-admin"
        assert config_center.rows[0]["admin_path"].get() == "/panel/"
        assert config_center.rows[0]["mcp_token"].get() == "keep-existing-token"
        assert config_center.rows[1]["site_group"].get() == "华南站群"
        assert config_center.credentials_frame.entries["site_group"].get() == "华南站群"
        assert config_center.credentials_frame.entries["admin_path"].get() == "/panel/"
        assert "华南站群" in config_center.credentials_frame.group_box.cget("values")

        # Also test Tab 2 text import via ConfigCenter
        bulk2 = config_center._open_bulk_sitegroup_dialog()
        assert bulk2 is not None
        bulk2.import_text.insert(
            "1.0",
            "http://banjia1.example.com||new-mcp-tok|||搬家主站\n",
        )
        assert bulk2._apply_text_import() is True
        assert config_center.rows[0]["mcp_token"].get() == "new-mcp-tok"
        assert config_center.rows[0]["site_title"].get() == "搬家主站"
        # Group from first bulk apply is preserved when blank column is imported
        assert config_center.rows[0]["site_group"].get() == "华南站群"

        config_center._save_all()
        assert not config_center._has_unsaved_changes()

        saved = load_config(tmp_path / "config.ini")
        assert saved[0].site_group == "华南站群"
        assert saved[0].mcp_token == "new-mcp-tok"
        assert saved[0].admin_username == "batch-admin"
        assert saved[0].admin_path == "/panel/"
        assert saved[0].site_title == "搬家主站"
        assert saved[1].site_group == "华南站群"
        assert saved[1].admin_username == "batch-admin"
        assert saved[1].admin_path == "/panel/"
    finally:
        if config_center.winfo_exists():
            config_center.destroy()
        root.destroy()
