"""Integration smoke tests for MainWindow, ConfigCenter, and NewSiteDialog site-group wiring."""

from __future__ import annotations

import sys
import time
import tkinter as tk
from tkinter import ttk
from types import ModuleType, SimpleNamespace

import pytest

from core.anqicms_mcp_client import AnqiMcpClient
from core.category_service import Category
from core.config_service import load_config, save_config
from core.gallery_service import GalleryService
from core.integration_settings import IntegrationSettings
from core.site_factory import create_site_from_key, ensure_site_files, parse_publish_endpoint
from models.sitegroup import (
    AdminJumpResult,
    JUMP_TIER_DIRECT,
    JUMP_TIER_TICKET,
    McpProbeResult,
)
from ui.config_center import ConfigCenter
from ui.main_window import MainWindow
from ui.new_site_dialog import NewSiteDialog


FULL_ENDPOINT = "https://www.example.com/api/import/archive?token=import-tok-123"


def make_root():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk environment unavailable: {exc}")
    root.withdraw()
    return root


def pump_until(root: tk.Tk, predicate, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return
        time.sleep(0.01)
    root.update()
    assert predicate(), "Timed out waiting for Tk background callback"


def write_two_site_config(tmp_path):
    first = create_site_from_key(
        "alpha",
        url="https://alpha.example.com",
        jiekou="/api/import/archive?token=tok-alpha",
        fid=1,
        base_dir=tmp_path,
    )
    first.site_title = "阿尔法站"
    first.site_group = "资讯组"
    first.mcp_token = "mcp-alpha-secret"
    first.admin_path = "/admin-a/"

    second = create_site_from_key(
        "beta",
        url="https://beta.example.com",
        jiekou="/api/import/archive?token=tok-beta",
        fid=2,
        base_dir=tmp_path,
    )
    second.site_group = "电商组"
    ensure_site_files(first)
    ensure_site_files(second)
    config_path = tmp_path / "config.ini"
    save_config(config_path, [first, second])
    return config_path


def make_gallery(tmp_path):
    source = tmp_path / "gallery-source.txt"
    source.write_text("[默认]\nhttps://img.example.com/a.jpg\n", encoding="utf-8")
    service = GalleryService(tmp_path / "设置")
    gallery = service.create_gallery("默认图库", source)
    return service, gallery


def make_site(tmp_path, gallery=None, *, name="demo", endpoint=FULL_ENDPOINT, fid=7):
    parsed = parse_publish_endpoint(endpoint)
    site = create_site_from_key(
        name,
        url=parsed.base_url,
        jiekou=parsed.publish_endpoint,
        fid=fid,
        base_dir=tmp_path,
    )
    site.category_name = "新闻"
    if gallery is not None:
        site.gallery_id = gallery.gallery_id
        site.image_file = gallery.file_path
    return site


def collect_button_labels(widget) -> list[str]:
    labels: list[str] = []
    if isinstance(widget, ttk.Button):
        labels.append(str(widget.cget("text")))
    for child in widget.winfo_children():
        labels.extend(collect_button_labels(child))
    return labels


def collect_widget_texts(widget) -> list[str]:
    texts: list[str] = []
    for child in widget.winfo_children():
        try:
            text = child.cget("text")
        except tk.TclError:
            text = ""
        if text:
            texts.append(str(text))
        texts.extend(collect_widget_texts(child))
    return texts


class FakeCategoryService:
    def fetch(self, _endpoint):
        return []


# ---------------------------------------------------------------------------
# MainWindow integration tests
# ---------------------------------------------------------------------------


def test_main_window_title_tabs_buttons_and_mcp_factory(tmp_path):
    root = make_root()
    window = MainWindow(root, config_path=write_two_site_config(tmp_path))
    try:
        assert root.title() == "AnQiCMS 站群管理工具"

        toolbar_tabs = [
            window.toolbar_notebook.tab(tab_id, "text")
            for tab_id in window.toolbar_notebook.tabs()
        ]
        content_tabs = [
            window.content_notebook.tab(tab_id, "text")
            for tab_id in window.content_notebook.tabs()
        ]
        assert toolbar_tabs == ["发布", "管理", "工具"]
        assert content_tabs == ["站点状态", "今日计划", "详情", "日志", "汇总"]

        manage_tab = root.nametowidget(window.toolbar_notebook.tabs()[1])
        manage_buttons = collect_button_labels(manage_tab)
        assert {"建站", "配置", "图库", "刷新", "友链中心", "统一升级", "跳转后台"} <= set(
            manage_buttons
        )

        # Verify real AnqiMcpClient.for_site is wired as mcp_client_factory (no network).
        site = window.sites[0]
        for svc in (
            window.friendlink_service,
            window.admin_jump_service,
            window.upgrade_service,
        ):
            factory = getattr(svc, "_mcp_client_factory", None)
            assert callable(factory)
            client = factory(site)
            try:
                assert isinstance(client, AnqiMcpClient)
                assert client.endpoint_url == "https://alpha.example.com/api/mcp"
            finally:
                client.close()
    finally:
        window._shutdown_sitegroup()
        root.destroy()


def test_main_window_lazy_imports_and_reuses_single_instance_windows(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_two_site_config(tmp_path))

    created_fl = []
    created_up = []

    class FakeToplevel(tk.Toplevel):
        def __init__(self, master, sites_provider, **kwargs):
            super().__init__(master)
            self.withdraw()
            self.sites_provider = sites_provider
            self.kwargs = kwargs
            self.lifted = 0
            self.focused = 0
            self.selected_sites = []
            self.selected_tabs = []

        def lift(self, *args, **kwargs):
            self.lifted += 1

        def focus_force(self):
            self.focused += 1

        def select_site(self, name):
            self.selected_sites.append(name)

        def select_tab(self, tab):
            self.selected_tabs.append(tab)

        def select_sites(self, site_names):
            self.selected_sites.append(list(site_names))

        def is_running(self):
            return getattr(self, "_running", False)

        def reload_sites(self):
            self.reloads = getattr(self, "reloads", 0) + 1

        def on_close(self):
            self.on_close_called = getattr(self, "on_close_called", 0) + 1
            self.destroy()

    class FakeFriendLinkCenter(FakeToplevel):
        def __init__(self, master, sites_provider, **kwargs):
            super().__init__(master, sites_provider, **kwargs)
            created_fl.append(self)

    class FakeUpgradeCenter(FakeToplevel):
        def __init__(self, master, sites_provider, **kwargs):
            super().__init__(master, sites_provider, **kwargs)
            created_up.append(self)

    fl_mod = ModuleType("ui.friendlink_center")
    fl_mod.FriendLinkCenter = FakeFriendLinkCenter
    up_mod = ModuleType("ui.upgrade_center")
    up_mod.UpgradeCenter = FakeUpgradeCenter
    monkeypatch.setitem(sys.modules, "ui.friendlink_center", fl_mod)
    monkeypatch.setitem(sys.modules, "ui.upgrade_center", up_mod)

    try:
        fl1 = window.open_friendlink_center(site_name="alpha", initial_tab="single")
        fl2 = window.open_friendlink_center(site_name="beta", initial_tab="crosslink")
        assert fl1 is fl2
        assert len(created_fl) == 1
        assert fl1.lifted == 1
        assert fl1.focused == 1
        assert fl1.selected_sites == ["beta"]
        assert fl1.selected_tabs == ["crosslink"]
        assert [s.name for s in fl1.sites_provider()] == ["alpha", "beta"]
        assert fl1.kwargs["service"] is window.friendlink_service
        assert fl1.kwargs["ledger"] is window.friendlink_ledger
        assert fl1.kwargs["jump_service"] is window.admin_jump_service

        up1 = window.open_upgrade_center(site_names=["alpha"])
        up2 = window.open_upgrade_center(site_names=["beta"])
        assert up1 is up2
        assert len(created_up) == 1
        assert up1.lifted == 1
        assert up1.focused == 1
        assert up1.kwargs["service"] is window.upgrade_service
        assert up1.kwargs["initial_site_names"] == ["alpha"]
        assert up1.selected_sites == [["beta"]]

        # When upgrade window is busy, reusing it must NOT overwrite selection.
        up1._running = True
        window.open_upgrade_center(site_names=["alpha"])
        assert up1.selected_sites == [["beta"]]

        # _render_committed_sites reloads idle sitegroup windows, skipping busy ones.
        window._render_committed_sites()
        assert getattr(fl1, "reloads", 0) == 1
        assert getattr(up1, "reloads", 0) == 0
        up1._running = False
        window._render_committed_sites()
        assert getattr(fl1, "reloads", 0) == 2
        assert getattr(up1, "reloads", 0) == 1

        # Destroying lets a fresh instance be opened later.
        fl1.destroy()
        root.update()
        assert window.friendlink_center_window is None
        fl3 = window.open_friendlink_center()
        assert fl3 is not fl1
        assert len(created_fl) == 2

        # _shutdown_sitegroup uses on_close() for FriendLinkCenter and destroy() for UpgradeCenter.
        window._shutdown_sitegroup()
        assert getattr(fl3, "on_close_called", 0) == 1
        assert getattr(up1, "on_close_called", 0) == 0
        assert not up1.winfo_exists()
    finally:
        window._shutdown_sitegroup()
        root.destroy()


def test_main_window_context_menu_jump_and_copy_admin_url(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_two_site_config(tmp_path))
    try:
        assert window._site_context_menu is not None
        end_index = window._site_context_menu.index("end")
        menu_labels = [
            window._site_context_menu.entrycget(i, "label")
            for i in range(end_index + 1)
            if window._site_context_menu.type(i) == "command"
        ]
        assert "跳转后台" in menu_labels
        assert "复制后台地址" in menu_labels
        assert "单站友链管理" in menu_labels

        # Select both rows in the site tree.
        row_ids = window.site_tree.get_children()
        assert len(row_ids) == 2
        window.site_tree.selection_set(row_ids)

        copied = window.copy_admin_url_for_selected()
        assert copied == [
            "https://alpha.example.com/admin-a/",
            "https://beta.example.com/system/",
        ]
        assert root.clipboard_get() == "\n".join(copied)
        assert "已复制 2 个站点后台地址" in window.status_var.get()

        jumped = []

        def fake_open_many(sites, *, progress=None):
            results = [
                AdminJumpResult(
                    site_name=s.name,
                    tier=JUMP_TIER_TICKET if s.name == "alpha" else JUMP_TIER_DIRECT,
                    success=True,
                    display_url=f"{s.url}/system/",
                    message="ok",
                )
                for s in sites
            ]
            jumped.extend(s.name for s in sites)
            return results

        monkeypatch.setattr(window.admin_jump_service, "open_many", fake_open_many)
        started = window.open_admin_for_selected()
        assert started is True
        pump_until(root, lambda: "已打开 2 个后台" in window.status_var.get())
        assert jumped == ["alpha", "beta"]
        assert "免密 1 / 直达 1" in window.status_var.get()
    finally:
        window._shutdown_sitegroup()
        root.destroy()


def test_main_window_on_close_prompts_during_upgrade_and_shuts_down(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_two_site_config(tmp_path))

    class RunningUpgradeWindow(tk.Toplevel):
        def __init__(self, master):
            super().__init__(master)
            self.withdraw()
            self.running = True

        def is_upgrade_running(self):
            return self.running

    up_win = RunningUpgradeWindow(root)
    window.upgrade_center_window = up_win
    shutdown_calls = []
    monkeypatch.setattr(
        window.admin_jump_service,
        "shutdown",
        lambda: shutdown_calls.append("jump"),
    )
    real_destroy = root.destroy
    destroyed = []
    monkeypatch.setattr(root, "destroy", lambda: destroyed.append(True))

    # User declines closing while upgrade is running.
    monkeypatch.setattr(
        "ui.main_window_sitegroup.messagebox.askyesno",
        lambda *args, **kwargs: False,
    )
    window.on_close()
    assert window.closing is False
    assert destroyed == []
    assert shutdown_calls == []

    # User confirms closing.
    monkeypatch.setattr(
        "ui.main_window_sitegroup.messagebox.askyesno",
        lambda *args, **kwargs: True,
    )
    window.on_close()
    assert window.closing is True
    assert shutdown_calls == ["jump"]
    assert destroyed == [True]
    assert window.upgrade_center_window is None
    real_destroy()


# ---------------------------------------------------------------------------
# ConfigCenter integration tests
# ---------------------------------------------------------------------------


def test_config_center_saves_and_round_trips_six_sitegroup_fields(tmp_path):
    root = make_root()
    service, gallery = make_gallery(tmp_path)
    site = make_site(tmp_path, gallery)
    updated_holder = []

    # Backward-compatible constructor call (no new kwargs required).
    dialog = ConfigCenter(
        root,
        tmp_path / "config.ini",
        [site],
        lambda sites: updated_holder.extend(sites),
        category_service=FakeCategoryService(),
        gallery_service=service,
    )
    try:
        labels = collect_widget_texts(dialog)
        assert "站群与后台" in labels
        assert "跳转后台" in labels
        assert "测试 MCP" in labels
        assert dialog.credentials_frame is not None
        assert not dialog._has_unsaved_changes()

        # Edit all 6 new fields.
        dialog.rows[0]["site_title"].set("示例主站")
        assert dialog._has_unsaved_changes()
        dialog.rows[0]["site_group"].set("主站组")
        dialog.rows[0]["mcp_token"].set("plain-mcp-secret-999")
        dialog.rows[0]["admin_username"].set("admin")
        dialog.rows[0]["admin_password"].set("plain-pwd-123")
        dialog.rows[0]["admin_path"].set("manage")  # normalized -> /manage/

        dialog._save_all()
        assert not dialog._has_unsaved_changes()

        saved = load_config(tmp_path / "config.ini")[0]
        assert saved.site_title == "示例主站"
        assert saved.site_group == "主站组"
        assert saved.mcp_token == "plain-mcp-secret-999"
        assert saved.admin_username == "admin"
        assert saved.admin_password == "plain-pwd-123"
        assert saved.admin_path == "/manage/"
        assert "dpapi:" not in (tmp_path / "config.ini").read_text(encoding="utf-8")
    finally:
        dialog.destroy()
        root.destroy()


@pytest.mark.parametrize(
    "field_name,new_value",
    [
        ("site_title", "新名称"),
        ("site_group", "新分组"),
        ("mcp_token", "new-token"),
        ("admin_username", "root"),
        ("admin_password", "new-pass"),
        ("admin_path", "/custom-admin/"),
    ],
)
def test_config_center_unsaved_detection_for_each_sitegroup_field(
    tmp_path, field_name, new_value
):
    root = make_root()
    service, gallery = make_gallery(tmp_path)
    dialog = ConfigCenter(
        root,
        tmp_path / "config.ini",
        [make_site(tmp_path, gallery)],
        lambda _sites: None,
        category_service=FakeCategoryService(),
        gallery_service=service,
    )
    try:
        assert not dialog._has_unsaved_changes()
        dialog.rows[0][field_name].set(new_value)
        assert dialog._has_unsaved_changes()
    finally:
        dialog.destroy()
        root.destroy()


def test_config_center_rejects_invalid_admin_path_and_long_group(tmp_path):
    root = make_root()
    service, gallery = make_gallery(tmp_path)
    dialog = ConfigCenter(
        root,
        tmp_path / "config.ini",
        [make_site(tmp_path, gallery)],
        lambda _sites: None,
        category_service=FakeCategoryService(),
        gallery_service=service,
    )
    try:
        dialog.rows[0]["admin_path"].set("/../escape")
        assert dialog._materialize_sites() is None
        assert "后台路径无效" in dialog.message_var.get()

        dialog.rows[0]["admin_path"].set("/system/")
        dialog.rows[0]["site_group"].set("组" * 65)
        assert dialog._materialize_sites() is None
        assert "64" in dialog.message_var.get()
    finally:
        dialog.destroy()
        root.destroy()


def test_config_center_test_mcp_and_jump_use_unsaved_drafts(tmp_path):
    root = make_root()
    service, gallery = make_gallery(tmp_path)
    probed = []
    jumped = []

    def fake_probe(site, settings):
        probed.append((site.name, site.mcp_token, site.admin_path, settings))
        return McpProbeResult(
            site_name=site.name,
            ok=True,
            server_name="AnQiCMS",
            server_version="3.6.7",
            tool_count=5,
            upgrade_tools=("system_upgrade",),
        )

    class FakeJumpService:
        def open_many(self, sites, *, progress=None):
            jumped.extend((s.name, s.admin_path, s.admin_username) for s in sites)
            return [
                AdminJumpResult(
                    site_name=s.name,
                    tier=JUMP_TIER_DIRECT,
                    success=True,
                    display_url=f"{s.url}{s.admin_path}",
                    message="ok",
                )
                for s in sites
            ]

    custom_settings = IntegrationSettings()
    dialog = ConfigCenter(
        root,
        tmp_path / "config.ini",
        [make_site(tmp_path, gallery)],
        lambda _sites: None,
        category_service=FakeCategoryService(),
        gallery_service=service,
        jump_service=FakeJumpService(),
        integration_settings=custom_settings,
        probe_fn=fake_probe,
    )
    try:
        dialog.rows[0]["mcp_token"].set("draft-mcp-token")
        dialog.rows[0]["admin_username"].set("draft-admin")
        dialog.rows[0]["admin_path"].set("backoffice")

        dialog._test_current_mcp()
        pump_until(root, lambda: "MCP 正常" in dialog.credentials_frame.status_var.get())
        assert probed == [("demo", "draft-mcp-token", "/backoffice/", custom_settings)]

        dialog._open_current_backend()
        pump_until(root, lambda: "已打开 1 个后台" in dialog.message_var.get())
        assert jumped == [("demo", "/backoffice/", "draft-admin")]
    finally:
        dialog.destroy()
        root.destroy()


# ---------------------------------------------------------------------------
# NewSiteDialog integration tests
# ---------------------------------------------------------------------------


def _configure_new_site_dialog(dialog, tmp_path, *, endpoint=FULL_ENDPOINT):
    source = tmp_path / "new-site-gallery.txt"
    source.write_text("[默认]\nhttps://img.example.com/a.jpg\n", encoding="utf-8")
    gallery = dialog.gallery_service.create_gallery("默认图库", source)
    dialog._reload_galleries()
    dialog.gallery_box.set(gallery.name)
    dialog._gallery_selected()
    dialog.full_endpoint_var.set(endpoint)
    if dialog._category_after_job is not None:
        dialog.after_cancel(dialog._category_after_job)
        dialog._category_after_job = None
    dialog.category_picker.set_categories(
        [Category(id=2, parent_id=0, title="行业新闻")],
        selected_id=2,
    )
    return gallery


@pytest.mark.parametrize("mode", ["quick", "blank"])
def test_new_site_dialog_applies_optional_sitegroup_fields(tmp_path, mode):
    root = make_root()
    existing = make_site(
        tmp_path,
        name="existing",
        endpoint="https://existing.example.com/api/import/archive?token=old",
    )
    existing.site_group = "已有分组"
    added = []
    dialog = NewSiteDialog(
        root,
        [existing],
        added.extend,
        config_path=tmp_path / "config.ini",
    )
    try:
        dialog.mode_var.set(mode)
        dialog._refresh_mode()
        assert dialog.sitegroup_frame is not None
        assert "已有分组" in dialog.site_group_box.cget("values")

        _configure_new_site_dialog(dialog, tmp_path)
        initial_state = dialog._draft_state()
        assert len(initial_state) == 18  # original 12 + 6 appended sitegroup fields

        dialog.site_title_var.set("新站显示名")
        dialog.site_group_var.set("新分组")
        dialog.mcp_token_var.set("new-site-mcp-token")
        dialog.admin_username_var.set("admin")
        dialog.admin_password_var.set("admin-secret")
        dialog.admin_path_var.set("cms-admin")

        updated_state = dialog._draft_state()
        assert updated_state[:12] == initial_state[:12]
        assert updated_state[12:] == (
            "新站显示名",
            "新分组",
            "new-site-mcp-token",
            "admin",
            "admin-secret",
            "cms-admin",
        )

        dialog._create_sites()
        assert len(added) == 1
        created = added[0]
        assert created.site_title == "新站显示名"
        assert created.site_group == "新分组"
        assert created.mcp_token == "new-site-mcp-token"
        assert created.admin_username == "admin"
        assert created.admin_password == "admin-secret"
        assert created.admin_path == "/cms-admin/"
    finally:
        if dialog.winfo_exists():
            dialog.destroy()
        root.destroy()


def test_new_site_dialog_clone_inherits_group_and_admin_path_only(tmp_path):
    root = make_root()
    template = make_site(
        tmp_path,
        name="tpl",
        endpoint="https://tpl.example.com/api/import/archive?token=tpl-tok",
    )
    template.site_title = "原站名"
    template.site_group = "矩阵A"
    template.mcp_token = "tpl-mcp-secret"
    template.admin_username = "tpl-admin"
    template.admin_password = "tpl-password"
    template.admin_path = "/portal/"

    added = []
    dialog = NewSiteDialog(
        root,
        [template],
        added.extend,
        template_site=template,
        config_path=tmp_path / "config.ini",
    )
    try:
        assert dialog.site_group_var.get() == "矩阵A"
        assert dialog.admin_path_var.get() == "/portal/"
        assert dialog.site_title_var.get() == ""
        assert dialog.mcp_token_var.get() == ""
        assert dialog.admin_username_var.get() == ""
        assert dialog.admin_password_var.get() == ""
        assert dialog._draft_state() == dialog._initial_draft

        _configure_new_site_dialog(
            dialog,
            tmp_path,
            endpoint="https://cloned.example.com/api/import/archive?token=clone-tok",
        )
        dialog.name_var.set("tpl-copy")
        dialog._create_sites()

        assert len(added) == 1
        copied = added[0]
        assert copied.site_group == "矩阵A"
        assert copied.admin_path == "/portal/"
        assert copied.site_title == ""
        assert copied.mcp_token == ""
        assert copied.admin_username == ""
        assert copied.admin_password == ""
    finally:
        if dialog.winfo_exists():
            dialog.destroy()
        root.destroy()


def test_new_site_dialog_rejects_invalid_admin_path(tmp_path):
    root = make_root()
    added = []
    dialog = NewSiteDialog(root, [], added.extend, config_path=tmp_path / "config.ini")
    try:
        _configure_new_site_dialog(dialog, tmp_path)
        dialog.admin_path_var.set("https://evil.example/admin")
        dialog._create_sites()
        assert added == []
        assert "后台路径无效" in dialog.message_var.get()
    finally:
        if dialog.winfo_exists():
            dialog.destroy()
        root.destroy()
