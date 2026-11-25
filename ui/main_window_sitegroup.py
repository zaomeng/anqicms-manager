"""Site-group actions mixin for :class:`ui.main_window.MainWindow`.

Keeps ``ui/main_window.py`` lean while wiring the Wave 2 site-group services
(MCP client, friend-link service/ledger, admin jump service, upgrade service)
and lazily opening the dedicated Toplevel windows (``FriendLinkCenter`` and
``UpgradeCenter``).
"""

from __future__ import annotations

import inspect
import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox
from typing import Any

from core.admin_jump_service import AdminJumpService
from core.anqicms_mcp_client import AnqiMcpClient
from core.friendlink_ledger import FriendLinkLedger, ledger_path
from core.friendlink_service import FriendLinkService
from core.integration_settings import load_integration_settings, settings_path
from core.site_credentials import admin_url
from core.upgrade_service import UpgradeService
from models.entities import SiteConfig
from ui.admin_jump_actions import open_admin_backends
from ui.ui_async import BackgroundRunner


def _filter_supported_kwargs(target: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Keep only keyword arguments accepted by *target* (or all if ``**kwargs`` exists)."""
    try:
        sig = inspect.signature(target)
    except (TypeError, ValueError):
        return dict(kwargs)
    params = sig.parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(kwargs)
    return {key: value for key, value in kwargs.items() if key in params}


def _window_alive(window: Any) -> bool:
    if window is None:
        return False
    exists = getattr(window, "winfo_exists", None)
    if callable(exists):
        try:
            return bool(exists())
        except tk.TclError:
            return False
    return False


class SiteGroupActionsMixin:
    """Mixin adding site-group toolbar buttons, context menu, and Toplevel windows."""

    def _init_sitegroup(self) -> None:
        self.integration_settings = load_integration_settings(
            settings_path(self.config_path)
        )
        self._mcp_client_factory = lambda site: AnqiMcpClient.for_site(
            site, self.integration_settings
        )
        self.friendlink_ledger = FriendLinkLedger(ledger_path(self.config_path))
        self.friendlink_service = FriendLinkService(
            self.integration_settings,
            mcp_client_factory=self._mcp_client_factory,
        )
        self.admin_jump_service = AdminJumpService(
            self.integration_settings,
            mcp_client_factory=self._mcp_client_factory,
        )
        self.upgrade_service = UpgradeService(
            self.integration_settings,
            mcp_client_factory=self._mcp_client_factory,
        )
        self._sitegroup_runner = BackgroundRunner(self.root)
        self.friendlink_center_window: Any = None
        self.upgrade_center_window: Any = None
        self._site_context_menu: tk.Menu | None = None

    def _sitegroup_toolbar_actions(self) -> list[tuple[str, Callable[[], None], str]]:
        return [
            ("友链中心", self.open_friendlink_center, "Compact.TButton"),
            ("统一升级", self.open_upgrade_center, "Compact.TButton"),
            ("跳转后台", self.open_admin_for_selected, "Compact.TButton"),
        ]

    def _bind_sitegroup_context_menu(self) -> None:
        menu = tk.Menu(self.site_tree, tearoff=0)
        menu.add_command(label="跳转后台", command=self.open_admin_for_selected)
        menu.add_command(label="复制后台地址", command=self.copy_admin_url_for_selected)
        menu.add_separator()
        menu.add_command(label="单站友链管理", command=self._open_friendlink_for_selected_site)
        menu.add_command(label="统一升级 / 检测版本", command=self._open_upgrade_for_selected_sites)
        self._site_context_menu = menu
        self.site_tree.bind("<Button-3>", self._show_site_context_menu, add="+")

    def _show_site_context_menu(self, event) -> None:
        if self._site_context_menu is None:
            return
        row_id = self.site_tree.identify_row(event.y)
        if row_id:
            if row_id not in self.site_tree.selection():
                self.site_tree.selection_set(row_id)
            self.site_tree.focus(row_id)
            site = self._site_from_tree_row(row_id)
            if site is not None:
                self._render_site_details(site.name)
        try:
            self._site_context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                self._site_context_menu.grab_release()
            except tk.TclError:
                pass

    def _selected_site_configs(self) -> list[SiteConfig]:
        selected: list[SiteConfig] = []
        seen: set[str] = set()
        if getattr(self, "site_tree", None) is not None:
            for row_id in self.site_tree.selection():
                site = self._site_from_tree_row(row_id)
                if site is None:
                    continue
                key = site.site_id or site.name
                if key not in seen:
                    seen.add(key)
                    selected.append(site)
        if selected:
            return selected
        current_name = getattr(self, "current_site_name", None)
        if current_name:
            match = next((site for site in self.sites if site.name == current_name), None)
            if match is not None:
                return [match]
        return []

    def _sites_snapshot(self) -> list[SiteConfig]:
        return list(self.sites)

    def open_friendlink_center(
        self,
        site_name: str | None = None,
        *,
        initial_tab: str = "single",
    ) -> Any:
        existing = self.friendlink_center_window
        if _window_alive(existing):
            if site_name and hasattr(existing, "select_site"):
                existing.select_site(site_name)
            if initial_tab and hasattr(existing, "select_tab"):
                existing.select_tab(initial_tab)
            existing.lift()
            existing.focus_force()
            return existing

        from ui.friendlink_center import FriendLinkCenter

        raw_kwargs = {
            "config_path": self.config_path,
            "settings": self.integration_settings,
            "service": self.friendlink_service,
            "ledger": self.friendlink_ledger,
            "jump_service": self.admin_jump_service,
            "initial_site_name": site_name,
            "initial_tab": initial_tab,
        }
        window = FriendLinkCenter(
            self.root,
            self._sites_snapshot,
            **_filter_supported_kwargs(FriendLinkCenter, raw_kwargs),
        )
        self.friendlink_center_window = window
        if window is not None and hasattr(window, "bind"):
            window.bind(
                "<Destroy>",
                lambda event, current=window: self._clear_sitegroup_window(
                    event, "friendlink_center_window", current
                ),
                add="+",
            )
        return window

    def open_upgrade_center(
        self,
        site_names: list[str] | None = None,
    ) -> Any:
        existing = self.upgrade_center_window
        if _window_alive(existing):
            is_running = getattr(existing, "is_running", None)
            busy = bool(is_running()) if callable(is_running) else False
            if site_names and not busy and hasattr(existing, "select_sites"):
                existing.select_sites(list(site_names))
            existing.lift()
            existing.focus_force()
            return existing

        from ui.upgrade_center import UpgradeCenter

        raw_kwargs = {
            "config_path": self.config_path,
            "settings": self.integration_settings,
            "service": self.upgrade_service,
            "initial_site_names": list(site_names) if site_names else None,
        }
        window = UpgradeCenter(
            self.root,
            self._sites_snapshot,
            **_filter_supported_kwargs(UpgradeCenter, raw_kwargs),
        )
        self.upgrade_center_window = window
        if window is not None and hasattr(window, "bind"):
            window.bind(
                "<Destroy>",
                lambda event, current=window: self._clear_sitegroup_window(
                    event, "upgrade_center_window", current
                ),
                add="+",
            )
        return window

    def _clear_sitegroup_window(self, event, attr_name: str, expected: Any) -> None:
        if getattr(event, "widget", None) is not expected:
            return
        if getattr(self, attr_name, None) is expected:
            setattr(self, attr_name, None)

    def _open_friendlink_for_selected_site(self) -> None:
        selected = self._selected_site_configs()
        site_name = selected[0].name if selected else None
        self.open_friendlink_center(site_name=site_name, initial_tab="single")

    def _open_upgrade_for_selected_sites(self) -> None:
        selected = self._selected_site_configs()
        names = [site.name for site in selected] if selected else None
        self.open_upgrade_center(site_names=names)

    def open_admin_for_selected(self) -> bool:
        return open_admin_backends(
            self.root,
            self._selected_site_configs(),
            service=self.admin_jump_service,
            runner=self._sitegroup_runner,
            status_callback=self.status_var.set,
            settings=self.integration_settings,
        )

    def copy_admin_url_for_selected(self) -> list[str]:
        selected = self._selected_site_configs()
        if not selected:
            self.status_var.set("请先选择至少一个站点")
            return []
        urls: list[str] = []
        for site in selected:
            if not (site.url or "").strip():
                continue
            try:
                urls.append(admin_url(site))
            except ValueError:
                continue
        if not urls:
            self.status_var.set("所选站点缺少有效的网址或后台路径")
            return []
        text = "\n".join(urls)
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except tk.TclError as exc:
            self.status_var.set(f"复制后台地址失败：{exc}")
            return []
        if len(urls) == 1:
            self.status_var.set(f"已复制后台地址：{urls[0]}")
        else:
            self.status_var.set(f"已复制 {len(urls)} 个站点后台地址")
        return urls

    def _is_sitegroup_upgrade_running(self) -> bool:
        window = getattr(self, "upgrade_center_window", None)
        if not _window_alive(window):
            return False
        for attr in ("is_upgrade_running", "is_running"):
            checker = getattr(window, attr, None)
            if callable(checker):
                try:
                    return bool(checker())
                except Exception:  # noqa: BLE001
                    return False
        runner = getattr(window, "runner", None) or getattr(window, "_runner", None)
        if runner is not None and bool(getattr(runner, "busy", False)):
            return True
        return False

    def _confirm_sitegroup_close(self) -> bool:
        if self._is_sitegroup_upgrade_running():
            return bool(
                messagebox.askyesno(
                    "确认退出",
                    "统一升级中心仍有升级任务在运行，确定退出吗？",
                    parent=self.root,
                )
            )
        return True

    @staticmethod
    def _is_window_idle(window: Any) -> bool:
        for attr in ("is_running", "is_upgrade_running"):
            checker = getattr(window, attr, None)
            if callable(checker):
                try:
                    if checker():
                        return False
                except Exception:  # noqa: BLE001
                    return False
        for target in (
            window,
            getattr(window, "batch_panel", None),
            getattr(window, "crosslink_panel", None),
        ):
            if target is None:
                continue
            runner = getattr(target, "runner", None) or getattr(target, "_runner", None)
            if runner is not None and bool(getattr(runner, "busy", False)):
                return False
        return True

    def _reload_sitegroup_windows(self) -> None:
        for attr in ("friendlink_center_window", "upgrade_center_window"):
            window = getattr(self, attr, None)
            if not _window_alive(window) or not self._is_window_idle(window):
                continue
            reloader = getattr(window, "reload_sites", None)
            if callable(reloader):
                try:
                    reloader()
                except tk.TclError:
                    pass

    def _shutdown_sitegroup(self) -> None:
        runner = getattr(self, "_sitegroup_runner", None)
        if runner is not None:
            try:
                runner.shutdown()
            except Exception:  # noqa: BLE001
                pass
        jump_service = getattr(self, "admin_jump_service", None)
        if jump_service is not None:
            try:
                jump_service.shutdown()
            except Exception:  # noqa: BLE001
                pass

        fl_window = getattr(self, "friendlink_center_window", None)
        self.friendlink_center_window = None
        if _window_alive(fl_window):
            on_close = getattr(fl_window, "on_close", None)
            try:
                if callable(on_close):
                    on_close()
                else:
                    fl_window.destroy()
            except tk.TclError:
                pass

        up_window = getattr(self, "upgrade_center_window", None)
        self.upgrade_center_window = None
        if _window_alive(up_window):
            try:
                up_window.destroy()
            except tk.TclError:
                pass
