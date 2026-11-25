from __future__ import annotations

import logging
import threading
import time
import tkinter as tk
from pathlib import Path

import pytest

from core.integration_settings import AdminJumpSettings, IntegrationSettings
from models.entities import SiteConfig
from models.sitegroup import (
    AdminJumpResult,
    JUMP_TIER_BRIDGE,
    JUMP_TIER_DIRECT,
    JUMP_TIER_TICKET,
    McpProbeResult,
    McpToolInfo,
)
from ui.admin_jump_actions import format_jump_summary, open_admin_backends
from ui.site_credentials_panel import SiteCredentialsFrame, format_probe_summary
from ui.site_picker import SitePicker
from ui.ui_async import BackgroundRunner


def make_root():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk environment unavailable: {exc}")
    root.withdraw()
    return root


def pump_until(root: tk.Tk, predicate, *, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            root.update()
        except tk.TclError:
            return bool(predicate())
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def make_site(
    name: str,
    *,
    site_id: str = "",
    site_title: str = "",
    site_group: str = "",
    url: str = "",
    jiekou: str = "",
    mcp_token: str = "",
    admin_username: str = "",
    admin_password: str = "",
    admin_path: str = "/system/",
) -> SiteConfig:
    resolved_url = url or f"https://{name}.example.com"
    resolved_jiekou = jiekou or f"/api/import/archive?token=import-{name}-secret"
    return SiteConfig(
        name=name,
        fid=1,
        url=resolved_url,
        jiekou=resolved_jiekou,
        publish_dir=Path(f"articles/{name}"),
        daily_publish_filename=Path(f"logs/{name}.txt"),
        publish_time="06:00:00",
        interval_time=5,
        daily_publish_limit=10,
        site_id=site_id or f"id-{name}",
        site_title=site_title,
        site_group=site_group,
        mcp_token=mcp_token,
        admin_username=admin_username,
        admin_password=admin_password,
        admin_path=admin_path,
    )


# --------------------------------------------------------------------------- #
# BackgroundRunner tests
# --------------------------------------------------------------------------- #
def test_background_runner_delivers_success_and_progress_on_tk_thread():
    root = make_root()
    try:
        runner = BackgroundRunner(root, poll_ms=10)
        ui_thread = threading.current_thread()
        work_threads: list[threading.Thread] = []
        progress_events: list[tuple[str, threading.Thread]] = []
        results: list[tuple[int, threading.Thread, bool]] = []
        gate = threading.Event()

        progress = runner.make_progress(
            lambda stage: progress_events.append((stage, threading.current_thread()))
        )

        def work():
            work_threads.append(threading.current_thread())
            progress("step-1")
            gate.wait(timeout=2.0)
            progress("step-2")
            return 42

        thread = runner.submit(
            work,
            on_success=lambda val: results.append(
                (val, threading.current_thread(), runner.busy)
            ),
        )
        assert thread is not None
        assert runner.busy is True
        gate.set()

        assert pump_until(root, lambda: len(results) == 1)
        thread.join(timeout=2.0)

        assert work_threads == [thread]
        assert work_threads[0] is not ui_thread
        assert [stage for stage, _ in progress_events] == ["step-1", "step-2"]
        assert all(t is ui_thread for _, t in progress_events)
        assert results == [(42, ui_thread, False)]
        assert runner.busy is False
        assert runner._poll_job is None  # polling stops when idle
        runner.shutdown()
    finally:
        root.destroy()


def test_background_runner_delivers_error_on_tk_thread():
    root = make_root()
    try:
        runner = BackgroundRunner(root, poll_ms=10)
        ui_thread = threading.current_thread()
        errors: list[tuple[str, threading.Thread]] = []

        def failing():
            raise RuntimeError("boom")

        thread = runner.submit(
            failing,
            on_success=lambda _v: pytest.fail("should not succeed"),
            on_error=lambda exc: errors.append((str(exc), threading.current_thread())),
        )
        assert thread is not None
        assert pump_until(root, lambda: len(errors) == 1)
        thread.join(timeout=2.0)

        assert errors == [("boom", ui_thread)]
        assert runner.busy is False
        runner.shutdown()
    finally:
        root.destroy()


def test_background_runner_silently_drops_callbacks_after_widget_destroyed():
    root = make_root()
    try:
        child = tk.Toplevel(root)
        child.withdraw()
        runner = BackgroundRunner(child, poll_ms=10)
        started = threading.Event()
        release = threading.Event()
        called: list[str] = []

        progress = runner.make_progress(lambda msg: called.append(f"progress:{msg}"))

        def work():
            started.set()
            release.wait(timeout=2.0)
            progress("late")
            return "late-result"

        thread = runner.submit(
            work,
            on_success=lambda val: called.append(f"ok:{val}"),
            on_error=lambda exc: called.append(f"err:{exc}"),
        )
        assert thread is not None
        assert started.wait(timeout=2.0)

        child.destroy()
        root.update()
        release.set()
        thread.join(timeout=2.0)

        runner.post(lambda: called.append("after-destroy"))
        for _ in range(5):
            root.update()
            time.sleep(0.01)

        assert called == []
        assert runner.busy is False
    finally:
        root.destroy()


def test_background_runner_logs_callback_exception_and_keeps_draining(caplog):
    root = make_root()
    try:
        runner = BackgroundRunner(root, poll_ms=10)
        ran: list[str] = []

        def bad_cb():
            raise ValueError("callback failed")

        with caplog.at_level(logging.ERROR, logger="anqicms.ui_async"):
            runner.post(bad_cb)
            runner.post(lambda: ran.append("recovered"))
            assert pump_until(root, lambda: ran == ["recovered"])

        assert any("callback failed" in rec.getMessage() or rec.exc_info for rec in caplog.records)
        runner.shutdown()
    finally:
        root.destroy()


# --------------------------------------------------------------------------- #
# SitePicker tests
# --------------------------------------------------------------------------- #
def test_site_picker_columns_masking_filtering_and_selection():
    root = make_root()
    try:
        sites = [
            make_site(
                "alpha",
                site_title="阿尔法站",
                site_group="医疗",
                mcp_token="mcp-super-secret-1234",
            ),
            make_site(
                "beta",
                site_title="贝塔站",
                site_group="科技",
                jiekou="/api/import/archive?token=beta-token-9876",
            ),
            make_site(
                "gamma",
                site_title="伽马站",
                site_group="",
                jiekou="/api/import/archive",
            ),
        ]
        events: list[list[str]] = []
        picker = SitePicker(
            root,
            lambda: sites,
            height=8,
            show_status=True,
            on_selection_changed=lambda selected: events.append([s.name for s in selected]),
        )

        assert list(picker.tree["columns"]) == [
            "checked",
            "name",
            "title",
            "group",
            "url",
            "token",
            "status",
        ]
        alpha_vals = picker.tree.item("id-alpha", "values")
        assert alpha_vals[0] == SitePicker.UNCHECKED_MARK
        assert alpha_vals[1] == "alpha"
        assert alpha_vals[2] == "阿尔法站"
        assert alpha_vals[3] == "医疗"
        assert "1234" in alpha_vals[5]
        assert "mcp-super-secret" not in alpha_vals[5]
        assert picker.tree.item("id-gamma", "values")[5] == "未配置"

        # Group filter
        assert list(picker.group_box.cget("values")) == ["全部分组", "医疗", "科技", "未分组"]
        picker.group_var.set("医疗")
        assert [s.name for s in picker.visible_sites()] == ["alpha"]

        # Select all applies to currently visible group
        picker.select_all()
        assert [s.name for s in picker.selected_sites()] == ["alpha"]
        assert picker.tree.item("id-alpha", "values")[0] == SitePicker.CHECKED_MARK

        # Switch to ungrouped and invert
        picker.group_var.set("未分组")
        assert [s.name for s in picker.visible_sites()] == ["gamma"]
        picker.invert_selection()
        assert [s.name for s in picker.selected_sites()] == ["alpha", "gamma"]

        # Search filter on all groups
        picker.group_var.set("全部分组")
        picker.search_var.set("贝塔")
        assert [s.name for s in picker.visible_sites()] == ["beta"]
        picker.search_var.set("")

        # Select none clears visible (all)
        picker.select_none()
        assert picker.selected_sites() == []

        # set_selected accepts both site_id and name, keeps provider order
        picker.set_selected(["gamma", "id-alpha"])
        assert [s.name for s in picker.selected_sites()] == ["alpha", "gamma"]

        # Row status updates & clear
        picker.set_row_status("id-alpha", "检查中…")
        assert picker.tree.item("id-alpha", "values")[6] == "检查中…"
        picker.set_row_status("gamma", "已完成")
        assert picker.tree.item("id-gamma", "values")[6] == "已完成"
        picker.clear_statuses()
        assert picker.tree.item("id-alpha", "values")[6] == ""
        assert picker.tree.item("id-gamma", "values")[6] == ""

        # Reload drops removed site from selection
        sites.pop(0)  # remove alpha
        picker.reload()
        assert [s.name for s in picker.selected_sites()] == ["gamma"]
        assert events[-1] == ["gamma"]

        picker.destroy()
    finally:
        root.destroy()


def test_site_picker_without_status_column():
    root = make_root()
    try:
        picker = SitePicker(root, [make_site("only")], show_status=False)
        assert "status" not in picker.tree["columns"]
        assert len(picker.tree.item("id-only", "values")) == 6
        picker.set_row_status("id-only", "ignored")
        picker.clear_statuses()
        picker.destroy()
    finally:
        root.destroy()


# --------------------------------------------------------------------------- #
# SiteCredentialsFrame & format_probe_summary tests
# --------------------------------------------------------------------------- #
def test_site_credentials_frame_bind_mask_enable_and_callbacks():
    root = make_root()
    try:
        clicked: list[str] = []
        frame = SiteCredentialsFrame(
            root,
            on_test_mcp=lambda: clicked.append("mcp"),
            on_open_admin=lambda: clicked.append("admin"),
        )
        assert frame.cget("text") == "站群与后台"
        assert frame.FIELDS == (
            "site_title",
            "site_group",
            "mcp_token",
            "admin_username",
            "admin_password",
            "admin_path",
        )

        row_a = {
            "name": tk.StringVar(value="ignored-extra"),
            "site_title": tk.StringVar(value="站点A"),
            "site_group": tk.StringVar(value="金融"),
            "mcp_token": tk.StringVar(value="tok-secret-aaaa"),
            "admin_username": tk.StringVar(value="admin"),
            "admin_password": tk.StringVar(value="pwd-secret-1111"),
            "admin_path": tk.StringVar(value="/system/"),
        }
        frame.set_group_choices(["金融", "医疗", "金融", ""])
        assert list(frame.group_box.cget("values")) == ["金融", "医疗"]

        frame.bind_vars(row_a)
        assert frame.entries["site_title"].get() == "站点A"
        assert frame.entries["site_group"].get() == "金融"
        assert frame.entries["mcp_token"].get() == "tok-secret-aaaa"
        assert frame.entries["admin_username"].get() == "admin"
        assert frame.entries["admin_password"].get() == "pwd-secret-1111"
        assert frame.entries["admin_path"].get() == "/system/"

        # Masked by default
        assert frame.entries["mcp_token"].cget("show") == "•"
        assert frame.entries["admin_password"].cget("show") == "•"

        # Toggle visibility
        frame.show_token_var.set(True)
        frame.show_password_var.set(True)
        assert frame.entries["mcp_token"].cget("show") == ""
        assert frame.entries["admin_password"].cget("show") == ""

        # Rebinding resets masks to hidden and ignores missing fields safely
        row_b = {"site_title": tk.StringVar(value="站点B")}
        frame.bind_vars(row_b)
        assert frame.entries["site_title"].get() == "站点B"
        assert frame.entries["mcp_token"].get() == ""
        assert frame.entries["mcp_token"].cget("show") == "•"
        assert frame.entries["admin_password"].cget("show") == "•"

        # Callbacks & status
        frame.test_mcp_button.invoke()
        frame.open_admin_button.invoke()
        assert clicked == ["mcp", "admin"]

        frame.set_status("探测完成")
        assert frame.status_var.get() == "探测完成"

        # Disable blocks actions & disables inputs
        frame.set_enabled(False)
        assert str(frame.entries["site_title"].cget("state")) == "disabled"
        assert str(frame.test_mcp_button.cget("state")) == "disabled"
        frame.set_enabled(True)
        assert str(frame.entries["site_title"].cget("state")) == "normal"

        frame.destroy()
    finally:
        root.destroy()


def test_format_probe_summary_ok_and_error_cases():
    ok_res = McpProbeResult(
        site_name="alpha",
        ok=True,
        server_name="AnqiCMS",
        server_version="3.6.7",
        tool_count=8,
        tools=(McpToolInfo(name="t1"),),
        friendlink_tools=("friendlink_list",),
        upgrade_tools=("system_upgrade",),
    )
    summary = format_probe_summary(ok_res)
    assert "MCP 正常" in summary
    assert "AnqiCMS 3.6.7" in summary
    assert "共 8 个工具" in summary
    assert "友链" in summary and "升级" in summary

    empty_tools = McpProbeResult(site_name="beta", ok=True, tool_count=0)
    assert "未发现站群工具" in format_probe_summary(empty_tools)

    err_res = McpProbeResult(
        site_name="gamma",
        ok=False,
        message="认证失败(401)",
        hint="请检查 MCP Token",
    )
    err_summary = format_probe_summary(err_res)
    assert "MCP 连接失败" in err_summary
    assert "认证失败(401)" in err_summary
    assert "请检查 MCP Token" in err_summary


# --------------------------------------------------------------------------- #
# open_admin_backends tests
# --------------------------------------------------------------------------- #
def test_open_admin_backends_empty_confirm_cap_and_summary():
    root = make_root()
    try:
        runner = BackgroundRunner(root, poll_ms=10)
        statuses: list[str] = []
        completed: list[list[AdminJumpResult]] = []
        called_batches: list[list[str]] = []

        def fake_jump(batch):
            called_batches.append([s.name for s in batch])
            out = []
            for idx, site in enumerate(batch):
                if idx == 0:
                    out.append(
                        AdminJumpResult(site.name, JUMP_TIER_TICKET, True, f"{site.url}/system/", "ok")
                    )
                elif idx == 1:
                    out.append(
                        AdminJumpResult(site.name, JUMP_TIER_BRIDGE, True, f"{site.url}/system/", "ok")
                    )
                elif idx == 2:
                    out.append(
                        AdminJumpResult(site.name, JUMP_TIER_DIRECT, True, f"{site.url}/system/", "ok")
                    )
                else:
                    out.append(
                        AdminJumpResult(site.name, JUMP_TIER_DIRECT, False, f"{site.url}/system/", "fail")
                    )
            return out

        # 1. Empty selection
        assert (
            open_admin_backends(
                root,
                [],
                runner=runner,
                jump_fn=fake_jump,
                status_callback=statuses.append,
            )
            is False
        )
        assert "请先选择" in statuses[-1]
        assert called_batches == []

        # 2. <= 5 sites: no confirmation prompt
        confirm_calls: list[str] = []
        four_sites = [make_site(f"s{i}") for i in range(4)]
        assert (
            open_admin_backends(
                root,
                four_sites,
                runner=runner,
                jump_fn=fake_jump,
                status_callback=statuses.append,
                on_complete=completed.append,
                confirm_fn=lambda title, msg: (confirm_calls.append(msg) or True),
            )
            is True
        )
        assert confirm_calls == []
        assert pump_until(root, lambda: len(completed) == 1)
        assert called_batches[-1] == ["s0", "s1", "s2", "s3"]
        assert statuses[-1] == "已打开 3 个后台（免密 2 / 直达 1），失败 1"

        # 3. > 5 sites: confirmation rejected -> cancelled, jump not called
        six_sites = [make_site(f"m{i}") for i in range(6)]
        assert (
            open_admin_backends(
                root,
                six_sites,
                runner=runner,
                jump_fn=fake_jump,
                status_callback=statuses.append,
                confirm_fn=lambda title, msg: (confirm_calls.append(msg) or False),
            )
            is False
        )
        assert len(confirm_calls) == 1
        assert "6" in confirm_calls[0]
        assert len(called_batches) == 1
        assert "已取消" in statuses[-1]

        # 4. Exceeds batch_max -> prompts with cap and truncates batch
        settings = IntegrationSettings(
            admin_jump=AdminJumpSettings(batch_confirm_threshold=2, batch_max=3)
        )
        five_sites = [make_site(f"c{i}") for i in range(5)]
        cap_prompts: list[str] = []
        assert (
            open_admin_backends(
                root,
                five_sites,
                runner=runner,
                jump_fn=fake_jump,
                settings=settings,
                status_callback=statuses.append,
                on_complete=completed.append,
                confirm_fn=lambda title, msg: (cap_prompts.append(msg) or True),
            )
            is True
        )
        assert len(cap_prompts) == 1
        assert "最多打开 3 个" in cap_prompts[0]
        assert pump_until(root, lambda: len(completed) == 2)
        assert called_batches[-1] == ["c0", "c1", "c2"]
        assert statuses[-1].startswith("已打开 3 个后台（免密 2 / 直达 1），失败 0")
        assert "跳过 2 个" in statuses[-1]

        runner.shutdown()
    finally:
        root.destroy()


def test_open_admin_backends_accepts_duck_typed_service_and_handles_error():
    root = make_root()
    try:
        class FakeService:
            def __init__(self):
                self.seen = []

            def open_many(self, sites, *, progress=None):
                self.seen = [s.name for s in sites]
                results = []
                total = len(sites)
                for idx, s in enumerate(sites, start=1):
                    res = AdminJumpResult(s.name, JUMP_TIER_DIRECT, True, f"{s.url}/system/", "ok")
                    results.append(res)
                    if progress is not None:
                        progress(idx, total, res)
                return results

        svc = FakeService()
        statuses: list[str] = []
        ok = open_admin_backends(
            root,
            [make_site("one"), make_site("two")],
            svc,
            runner=None,
            status_callback=statuses.append,
        )
        assert ok is True
        assert svc.seen == ["one", "two"]
        assert any("(1/2)：one" in s for s in statuses)
        assert any("(2/2)：two" in s for s in statuses)
        assert statuses[-1] == "已打开 2 个后台（免密 0 / 直达 2），失败 0"

        # Error handling
        err_statuses: list[str] = []
        open_admin_backends(
            root,
            [make_site("bad")],
            runner=None,
            jump_fn=lambda _batch: (_ for _ in ()).throw(RuntimeError("network down")),
            status_callback=err_statuses.append,
        )
        assert "network down" in err_statuses[-1]

        assert (
            format_jump_summary([])
            == "已打开 0 个后台（免密 0 / 直达 0），失败 0"
        )
    finally:
        root.destroy()
