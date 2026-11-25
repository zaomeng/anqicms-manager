"""Tk smoke tests for ui/upgrade_center.py (AnQiCMS 统一升级中心)."""

from __future__ import annotations

import threading
import time
import tkinter as tk
from pathlib import Path
from typing import Any

import pytest

from core.integration_settings import IntegrationSettings, UpgradeSettings
from core.mcp_contracts import AUTH_HINT
from core.upgrade_service import scope_hint
from models.entities import SiteConfig
from models.sitegroup import (
    UPGRADE_ALREADY_LATEST,
    UPGRADE_AUTH_FAILED,
    UPGRADE_CANCELLED,
    UPGRADE_NO_TOOL,
    UPGRADE_SCOPE_DISABLED,
    UPGRADE_SKIPPED,
    UPGRADE_UPGRADED,
    McpProbeResult,
    McpToolInfo,
    SiteVersionInfo,
    UpgradeProgress,
    UpgradeResult,
)
from ui.upgrade_center import UpgradeCenter


def make_root() -> tk.Tk:
    last_exc: tk.TclError | None = None
    for _ in range(3):
        try:
            root = tk.Tk()
            root.withdraw()
            return root
        except tk.TclError as exc:
            last_exc = exc
            time.sleep(0.05)
    pytest.skip(f"Tk environment unavailable: {last_exc}")


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
    mcp_token: str = "tok-test-12345678",
) -> SiteConfig:
    return SiteConfig(
        name=name,
        fid=1,
        url=url or f"https://{name}.example.com",
        jiekou=f"/api/import/archive?token=import-{name}-secret",
        publish_dir=Path(f"articles/{name}"),
        daily_publish_filename=Path(f"logs/{name}.txt"),
        publish_time="06:00:00",
        interval_time=5,
        daily_publish_limit=10,
        site_id=site_id or f"id-{name}",
        site_title=site_title or f"{name}站",
        site_group=site_group,
        mcp_token=mcp_token,
    )


class FakeUpgradeService:
    """Hermetic fake UpgradeService recording calls without any network access."""

    def __init__(
        self,
        *,
        version_infos: list[SiteVersionInfo] | None = None,
        upgrade_results: list[UpgradeResult] | None = None,
        gate: threading.Event | None = None,
        started: threading.Event | None = None,
    ) -> None:
        self.version_infos = list(version_infos or ())
        self.upgrade_results = list(upgrade_results or ())
        self.gate = gate
        self.started = started
        self.check_calls: list[dict[str, Any]] = []
        self.upgrade_calls: list[dict[str, Any]] = []

    def check_versions(
        self,
        sites,
        *,
        progress=None,
        cancel_event=None,
        max_workers=None,
    ) -> list[SiteVersionInfo]:
        site_list = list(sites)
        self.check_calls.append(
            {
                "sites": [s.name for s in site_list],
                "max_workers": max_workers,
                "cancel_event": cancel_event,
            }
        )
        if progress is not None:
            for site in site_list:
                progress(
                    UpgradeProgress(
                        site_name=site.name,
                        stage="checking",
                        message="正在检测版本...",
                    )
                )
        if self.started is not None:
            self.started.set()
        if self.gate is not None:
            self.gate.wait(timeout=2.0)

        by_name = {info.site_name: info for info in self.version_infos}
        out: list[SiteVersionInfo] = []
        for site in site_list:
            if cancel_event is not None and cancel_event.is_set():
                info = SiteVersionInfo(
                    site_name=site.name,
                    site_id=site.site_id,
                    reachable=False,
                    message="已取消",
                )
            else:
                info = by_name.get(
                    site.name,
                    SiteVersionInfo(
                        site_name=site.name,
                        site_id=site.site_id,
                        reachable=True,
                        current_version="v3.6.6",
                        latest_version="v3.6.7",
                        upgrade_available=True,
                        source="mcp",
                        check_tool="system_info",
                        upgrade_tool="system_upgrade",
                        message="检测成功",
                    ),
                )
            out.append(info)
            if progress is not None:
                progress(
                    UpgradeProgress(
                        site_name=site.name,
                        stage="done",
                        message=info.message,
                    )
                )
        return out

    def upgrade_sites(
        self,
        sites,
        *,
        confirmed: bool,
        max_workers=None,
        canary_first: bool = True,
        stop_on_failure: bool = True,
        progress=None,
        result_callback=None,
        cancel_event=None,
    ) -> list[UpgradeResult]:
        if confirmed is not True:
            raise ValueError("升级需要用户确认")
        site_list = list(sites)
        self.upgrade_calls.append(
            {
                "sites": [s.name for s in site_list],
                "confirmed": confirmed,
                "max_workers": max_workers,
                "canary_first": canary_first,
                "stop_on_failure": stop_on_failure,
                "cancel_event": cancel_event,
            }
        )
        if progress is not None and site_list:
            progress(
                UpgradeProgress(
                    site_name=site_list[0].name,
                    stage="upgrading",
                    message="正在执行升级（system_upgrade）...",
                )
            )
        if self.started is not None:
            self.started.set()
        if self.gate is not None:
            self.gate.wait(timeout=2.0)

        by_name = {res.site_name: res for res in self.upgrade_results}
        out: list[UpgradeResult] = []
        for site in site_list:
            if cancel_event is not None and cancel_event.is_set():
                res = UpgradeResult(
                    site_name=site.name,
                    site_id=site.site_id,
                    status=UPGRADE_CANCELLED,
                    message="已取消",
                )
            else:
                res = by_name.get(
                    site.name,
                    UpgradeResult(
                        site_name=site.name,
                        site_id=site.site_id,
                        status=UPGRADE_UPGRADED,
                        from_version="v3.6.6",
                        to_version="v3.6.7",
                        message="升级成功：v3.6.6 → v3.6.7",
                    ),
                )
            out.append(res)
            if result_callback is not None:
                result_callback(res)
            if progress is not None:
                progress(
                    UpgradeProgress(
                        site_name=site.name,
                        stage="done",
                        message=res.message,
                    )
                )
        return out


def test_upgrade_center_window_title_controls_columns_and_initial_selection():
    root = make_root()
    try:
        sites = [
            make_site("alpha", site_group="资讯组"),
            make_site("beta", site_group="电商组"),
            make_site("gamma", site_group="资讯组"),
        ]
        warnings: list[tuple[str, str]] = []
        settings = IntegrationSettings(upgrade=UpgradeSettings(max_concurrency=3))
        service = FakeUpgradeService()

        win = UpgradeCenter(
            root,
            lambda: sites,
            settings=settings,
            service=service,
            initial_site_names=["alpha", "gamma"],
            warn_fn=lambda title, msg: warnings.append((title, msg)),
        )
        win.withdraw()

        assert win.title() == "AnQiCMS 统一升级中心"
        assert [s.name for s in win.site_picker.selected_sites()] == ["alpha", "gamma"]

        # Controls and options labels
        assert win.check_button.cget("text") == "检测版本"
        assert win.upgrade_button.cget("text") == "升级选中站点"
        assert win.test_mcp_button.cget("text") == "测试 MCP"
        assert win.stop_button.cget("text") == "停止"
        assert str(win.stop_button.cget("state")) == "disabled"

        assert win.canary_check.cget("text") == "金丝雀：先升级第 1 站验证"
        assert win.stop_on_failure_check.cget("text") == "失败即停"
        assert win.canary_var.get() is True
        assert win.stop_on_failure_var.get() is True
        assert win.concurrency_var.get() == 3
        assert int(float(win.concurrency_spinbox.cget("from"))) == 1
        assert int(float(win.concurrency_spinbox.cget("to"))) == 5

        # Result tree headings
        headings = [
            win.result_tree.heading(col, "text")
            for col in win.result_tree["columns"]
        ]
        assert "站点" in headings
        assert "分组" in headings
        assert "当前版本" in headings
        assert "最新版本" in headings
        assert "状态" in headings
        assert "提示/详情" in headings

        # Empty selection warning
        win.site_picker.select_none()
        assert win.check_selected_versions() is False
        assert win.upgrade_selected_sites() is False
        assert win.test_selected_mcp() is False
        assert len(warnings) == 3
        assert "请先勾选" in warnings[-1][1]

        win.on_close()
    finally:
        root.destroy()


def test_upgrade_center_check_versions_and_chinese_hints():
    root = make_root()
    try:
        sites = [
            make_site("s_up", site_group="A组"),
            make_site("s_latest", site_group="A组"),
            make_site("s_scope", site_group="B组"),
            make_site("s_notool", site_group="B组"),
            make_site("s_auth", site_group="C组"),
        ]
        infos = [
            SiteVersionInfo(
                site_name="s_up",
                site_id="id-s_up",
                reachable=True,
                current_version="v3.6.5",
                latest_version="v3.6.7",
                upgrade_available=True,
                source="mcp",
                check_tool="check_update",
                upgrade_tool="system_upgrade",
                message="检测成功",
            ),
            SiteVersionInfo(
                site_name="s_latest",
                site_id="id-s_latest",
                reachable=True,
                current_version="v3.6.7",
                latest_version="v3.6.7",
                upgrade_available=False,
                source="mcp",
                check_tool="check_update",
                upgrade_tool="system_upgrade",
                message="检测成功",
            ),
            SiteVersionInfo(
                site_name="s_scope",
                site_id="id-s_scope",
                reachable=True,
                source="mcp",
                message="接口开放范围未勾选",
                hint=scope_hint("upgrade"),
            ),
            SiteVersionInfo(
                site_name="s_notool",
                site_id="id-s_notool",
                reachable=True,
                source="mcp",
                check_tool="",
                upgrade_tool="",
                message="未找到版本检测工具",
            ),
            SiteVersionInfo(
                site_name="s_auth",
                site_id="id-s_auth",
                reachable=False,
                source="none",
                message="MCP 认证失败（HTTP 401）",
                hint=AUTH_HINT,
            ),
        ]
        service = FakeUpgradeService(version_infos=infos)
        info_dialogs: list[tuple[str, str]] = []

        win = UpgradeCenter(
            root,
            sites,
            service=service,
            initial_site_names=[s.name for s in sites],
            info_fn=lambda title, msg: info_dialogs.append((title, msg)),
        )
        win.withdraw()

        # Clamp concurrency > 5 down to 5
        win.concurrency_var.set(9)
        assert win.check_selected_versions() is True
        assert pump_until(root, lambda: not win.is_running())

        assert len(service.check_calls) == 1
        assert service.check_calls[0]["max_workers"] == 5

        up_vals = win.result_tree.item("id-s_up", "values")
        assert up_vals[2] == "v3.6.5"
        assert up_vals[3] == "v3.6.7"
        assert up_vals[4] == "是"
        assert up_vals[5] == "可升级"

        latest_vals = win.result_tree.item("id-s_latest", "values")
        assert latest_vals[4] == "否"
        assert latest_vals[5] == "已是最新"

        scope_vals = win.result_tree.item("id-s_scope", "values")
        assert "scope_disabled" in scope_vals[5]
        assert "接口开放范围" in scope_vals[6]
        assert "warn" in win.result_tree.item("id-s_scope", "tags")

        notool_vals = win.result_tree.item("id-s_notool", "values")
        assert "no_tool" in notool_vals[5]
        assert "接口开放范围" in notool_vals[6]
        assert "warn" in win.result_tree.item("id-s_notool", "tags")

        auth_vals = win.result_tree.item("id-s_auth", "values")
        assert "auth_failed" in auth_vals[5]
        assert "MCP Token" in auth_vals[6]
        assert "warn" in win.result_tree.item("id-s_auth", "tags")

        # Double-click / show_row_detail displays full hint
        detail = win.show_row_detail("id-s_scope")
        assert "接口开放范围" in detail
        assert info_dialogs and "接口开放范围" in info_dialogs[-1][1]

        win.on_close()
    finally:
        root.destroy()


def test_upgrade_center_upgrade_requires_askyesno_confirmation_and_passes_confirmed_true(monkeypatch):
    root = make_root()
    try:
        sites = [
            make_site("alpha", site_group="主站"),
            make_site("beta", site_group="分站"),
            make_site("gamma", site_group="分站"),
            make_site("delta", site_group="分站"),
        ]
        results = [
            UpgradeResult(
                site_name="alpha",
                site_id="id-alpha",
                status=UPGRADE_UPGRADED,
                from_version="v3.6.6",
                to_version="v3.6.7",
                message="升级成功：v3.6.6 → v3.6.7",
            ),
            UpgradeResult(
                site_name="beta",
                site_id="id-beta",
                status=UPGRADE_SCOPE_DISABLED,
                from_version="v3.6.6",
                message="系统升级接口未开放",
                hint="",  # UI must populate default Chinese hint for scope_disabled
            ),
            UpgradeResult(
                site_name="gamma",
                site_id="id-gamma",
                status=UPGRADE_NO_TOOL,
                from_version="v3.6.6",
                message="未暴露升级工具",
                hint="",  # UI must populate default Chinese hint for no_tool
            ),
            UpgradeResult(
                site_name="delta",
                site_id="id-delta",
                status=UPGRADE_AUTH_FAILED,
                message="鉴权失败",
                hint="",  # UI must populate default Chinese hint for auth_failed
            ),
        ]
        service = FakeUpgradeService(upgrade_results=results)

        win = UpgradeCenter(
            root,
            sites,
            service=service,
            initial_site_names=[s.name for s in sites],
        )
        win.withdraw()

        # 1. User declines messagebox.askyesno -> service.upgrade_sites MUST NOT be called
        ask_calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "ui.upgrade_center.messagebox.askyesno",
            lambda title, msg, **kw: (ask_calls.append((title, msg)) or False),
        )
        assert win.upgrade_selected_sites() is False
        assert len(ask_calls) == 1
        assert service.upgrade_calls == []
        assert "已取消" in win.status_var.get()

        # 2. User accepts messagebox.askyesno -> service.upgrade_sites called with confirmed=True
        win.concurrency_var.set(2)
        win.canary_var.set(False)
        win.stop_on_failure_var.set(True)
        monkeypatch.setattr(
            "ui.upgrade_center.messagebox.askyesno",
            lambda title, msg, **kw: (ask_calls.append((title, msg)) or True),
        )
        assert win.upgrade_selected_sites() is True
        assert pump_until(root, lambda: not win.is_running())

        assert len(service.upgrade_calls) == 1
        call = service.upgrade_calls[0]
        assert call["confirmed"] is True
        assert call["max_workers"] == 2
        assert call["canary_first"] is False
        assert call["stop_on_failure"] is True
        assert call["sites"] == ["alpha", "beta", "gamma", "delta"]

        # Verify result rows and Chinese hints for scope_disabled / no_tool / auth_failed
        alpha_vals = win.result_tree.item("id-alpha", "values")
        assert alpha_vals[2] == "v3.6.7"
        assert alpha_vals[5] == "升级成功"

        beta_vals = win.result_tree.item("id-beta", "values")
        assert "scope_disabled" in beta_vals[5]
        assert "接口开放范围" in beta_vals[6]

        gamma_vals = win.result_tree.item("id-gamma", "values")
        assert "no_tool" in gamma_vals[5]
        assert "未通过 MCP 暴露升级工具" in gamma_vals[6]

        delta_vals = win.result_tree.item("id-delta", "values")
        assert "auth_failed" in delta_vals[5]
        assert "MCP Token" in delta_vals[6]

        win.on_close()
    finally:
        root.destroy()


def test_upgrade_center_test_mcp_and_close_confirmation_during_active_upgrade():
    root = make_root()
    try:
        sites = [
            make_site("alpha"),
            make_site("beta"),
        ]
        probe_results = {
            "alpha": McpProbeResult(
                site_name="alpha",
                ok=True,
                server_name="AnqiCMS",
                server_version="3.6.7",
                tool_count=4,
                tools=(McpToolInfo(name="system_upgrade"),),
                upgrade_tools=("system_upgrade",),
            ),
            "beta": McpProbeResult(
                site_name="beta",
                ok=True,
                server_name="AnqiCMS",
                server_version="3.6.7",
                tool_count=2,
                tools=(McpToolInfo(name="article_list"),),
                upgrade_tools=(),
            ),
        }

        started = threading.Event()
        gate = threading.Event()
        service = FakeUpgradeService(started=started, gate=gate)
        confirm_Queue: list[bool] = []
        confirm_prompts: list[tuple[str, str]] = []

        def fake_confirm(title: str, msg: str) -> bool:
            confirm_prompts.append((title, msg))
            return confirm_Queue.pop(0) if confirm_Queue else True

        win = UpgradeCenter(
            root,
            sites,
            service=service,
            initial_site_names=["alpha", "beta"],
            probe_fn=lambda site, _settings: probe_results[site.name],
            confirm_fn=fake_confirm,
        )
        win.withdraw()

        # 1. Test MCP button
        assert win.test_selected_mcp() is True
        assert pump_until(root, lambda: not win.is_running())
        assert "含升级工具" in win.result_tree.item("id-alpha", "values")[5]
        beta_row = win.result_tree.item("id-beta", "values")
        assert "no_tool" in beta_row[5]
        assert "接口开放范围" in beta_row[6]

        # 2. Start upgrade that pauses on gate
        confirm_Queue.append(True)  # confirm starting upgrade
        assert win.upgrade_selected_sites() is True
        assert started.wait(timeout=2.0)
        assert win.is_upgrade_running() is True

        # 3. Try closing while upgrade is running -> user declines (False)
        confirm_Queue.append(False)
        assert win.on_close() is False
        assert win.winfo_exists()
        assert win.is_upgrade_running() is True
        assert service.upgrade_calls[0]["cancel_event"].is_set() is False

        # 4. Try closing again -> user confirms (True) -> cancels & shuts down runner
        confirm_Queue.append(True)
        assert win.on_close() is True
        assert service.upgrade_calls[0]["cancel_event"].is_set() is True
        gate.set()
        assert win.runner.busy is False

        # 5. Programmatic destroy() during active upgrade cancels & shuts down WITHOUT prompting
        started2 = threading.Event()
        gate2 = threading.Event()
        service2 = FakeUpgradeService(started=started2, gate=gate2)
        destroy_prompts: list[tuple[str, str]] = []

        win2 = UpgradeCenter(
            root,
            sites,
            service=service2,
            initial_site_names=["alpha", "beta"],
            confirm_fn=lambda t, m: (destroy_prompts.append((t, m)) or True),
        )
        win2.withdraw()
        assert win2.upgrade_selected_sites() is True
        assert started2.wait(timeout=2.0)
        assert len(destroy_prompts) == 1  # only the upgrade start prompt

        win2.destroy()
        assert len(destroy_prompts) == 1  # destroy() did NOT re-prompt
        assert service2.upgrade_calls[0]["cancel_event"].is_set() is True
        assert win2.is_upgrade_running() is False
        assert win2.runner.busy is False
        gate2.set()
    finally:
        gate.set()
        root.destroy()


def test_upgrade_center_stop_button_sets_cancel_event_and_realtime_progress():
    root = make_root()
    started = threading.Event()
    gate = threading.Event()
    try:
        sites = [
            make_site("alpha", site_group="一组"),
            make_site("beta", site_group="二组"),
        ]
        service = FakeUpgradeService(started=started, gate=gate)
        win = UpgradeCenter(
            root,
            sites,
            service=service,
            initial_site_names=["alpha", "beta"],
            confirm_fn=lambda _t, _m: True,
        )
        win.withdraw()

        assert win.upgrade_selected_sites() is True
        assert started.wait(timeout=2.0)

        # Pump so the 'upgrading' progress callback is processed on Tk thread
        assert pump_until(
            root,
            lambda: "正在执行升级" in win.result_tree.item("id-alpha", "values")[5],
        )
        assert str(win.stop_button.cget("state")) == "normal"

        # Click "停止" button -> sets cancel_event and disables stop_button
        assert win.stop_active_task() is True
        assert service.upgrade_calls[0]["cancel_event"].is_set() is True
        assert str(win.stop_button.cget("state")) == "disabled"
        assert "正在停止" in win.status_var.get()

        # Release worker and wait for completion
        gate.set()
        assert pump_until(root, lambda: not win.is_running())
        assert win.result_tree.item("id-alpha", "values")[5] == "已取消"
        assert win.result_tree.item("id-beta", "values")[5] == "已取消"

        win.on_close()
    finally:
        gate.set()
        root.destroy()

