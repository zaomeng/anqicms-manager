from __future__ import annotations

import hashlib
import queue
import subprocess
import threading
import time
import tkinter as tk
import webbrowser
import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from core.archive_service import ArchiveService
from core.automation_state import (
    AutomationStateStore,
    BatchState,
    StateRecoveryRequired,
)
from core.config_service import ConfigLoadError, load_config, migrate_site_metadata, save_config
from core.gallery_service import GalleryError, GalleryService
from core.instance_lock import ConfigInstanceLock, InstanceAlreadyRunningError
from core.logger import configure_logging
from core.publisher import Publisher
from core.schedule_service import build_daily_plan
from core.site_factory import is_valid_publish_endpoint, is_valid_site_url
from core.site_service import SiteService
from core.startup_service import StartupPreferenceError, StartupService
from core.task_manager import TaskManager
from core.state_store import StateFileError
from models.entities import (
    ArticleRecord,
    PlannedRun,
    PublishResult,
    SiteConfig,
    SiteStatusSummary,
    TaskBuildDiagnostic,
)
from ui.config_center import ConfigCenter
from ui.gallery_manager import GalleryManager
from ui.main_window_sitegroup import SiteGroupActionsMixin
from ui.new_site_dialog import NewSiteDialog
from ui.style import apply_console_style


class MainWindow(SiteGroupActionsMixin):
    def __init__(self, root: tk.Tk, config_path: str | Path = "config.ini") -> None:
        self.root = root
        apply_console_style(self.root)
        self.root.title("AnQiCMS 站群管理工具")
        self.root.geometry("1680x920")
        self.root.minsize(1180, 700)
        self.config_path = Path(config_path).resolve()
        self.gallery_service = GalleryService(self.config_path.parent / "设置")
        self.config_error_message = ""
        self.automation_config_blocked = False
        try:
            self.sites: list[SiteConfig] = load_config(self.config_path)
        except ConfigLoadError as exc:
            self.sites = []
            self.config_error_message = str(exc)
            self.automation_config_blocked = True
        else:
            legacy_gallery_sites = {
                site.name
                for site in self.sites
                if site.image_file is not None and not site.gallery_id
            }
            try:
                changed = migrate_site_metadata(
                    self.config_path,
                    self.sites,
                    self.gallery_service,
                )
                if changed:
                    self.sites = load_config(self.config_path)
            except (ConfigLoadError, GalleryError, OSError, ValueError) as exc:
                self.config_error_message = f"站点元数据迁移失败，自动发布已阻断: {exc}"
                self.automation_config_blocked = True
            else:
                failed_galleries = sorted(
                    site.name
                    for site in self.sites
                    if site.name in legacy_gallery_sites and not site.gallery_id
                )
                if failed_galleries:
                    self.config_error_message = (
                        "旧图库迁移失败，相关站点已停用: "
                        + ", ".join(failed_galleries)
                    )
                try:
                    self.gallery_service.list_galleries()
                except GalleryError as exc:
                    self.config_error_message = f"图库注册表不可用，自动发布已阻断: {exc}"
                    self.automation_config_blocked = True
        self.archive_service = ArchiveService(
            archive_root=self.config_path.parent / "归档",
            cleanup_state_file=(
                self.config_path.parent / "设置" / "archive_cleanup_state.txt"
            ),
            identity_map_file=(
                self.config_path.parent / "设置" / "archive_site_ids.json"
            ),
        )
        archive_identity_errors: list[str] = []
        for site in self.sites:
            if not site.site_id:
                continue
            try:
                self.archive_service.migrate_site_identity(site.name, site.site_id)
            except OSError as exc:
                archive_identity_errors.append(f"{site.name}: {exc}")
        if archive_identity_errors:
            self.config_error_message = (
                "归档身份迁移失败，自动发布已阻断: "
                + "; ".join(archive_identity_errors)
            )
            self.automation_config_blocked = True
        self.site_service = SiteService(archive_service=self.archive_service)
        self.publisher = Publisher(archive_service=self.archive_service)
        self.task_manager = TaskManager(
            self.publisher,
            max_concurrent_sites=3,
            gallery_service=self.gallery_service,
        )
        self.startup_service = StartupService(
            preference_path=(
                self.config_path.parent / "设置" / "startup_preference.json"
            )
        )
        self.automation_store = AutomationStateStore(
            self.config_path.parent / "设置" / "automation_state.json"
        )
        self.logger = configure_logging(log_file=self.config_path.parent / "fabu.log", callback=self.append_log)
        self.site_summaries: dict[str, SiteStatusSummary] = {}
        self.articles_by_site = {}
        self.daily_plan: list[PlannedRun] = []
        self.failed_results: list[PublishResult] = []
        self._live_applied_results: dict[int, PublishResult] = {}
        self.current_site_name: str | None = None
        self.window_state_file = self.config_path.parent / "设置" / "window_state.json"
        self.status_var = tk.StringVar(value=self.config_error_message or "就绪")
        self.startup_var = tk.BooleanVar(value=False)
        self.startup_status = ""
        self.progress_value = tk.DoubleVar(value=0.0)
        self.metric_vars = {
            "总文章": tk.StringVar(value="0"),
            "成功": tk.StringVar(value="0"),
            "失败": tk.StringVar(value="0"),
            "耗时": tk.StringVar(value="-"),
        }
        self.detail_vars = {
            "name": tk.StringVar(value="-"),
            "url": tk.StringVar(value="-"),
            "enabled": tk.StringVar(value="-"),
            "activation_date": tk.StringVar(value="-"),
            "category": tk.StringVar(value="-"),
            "gallery": tk.StringVar(value="-"),
            "times": tk.StringVar(value="-"),
            "interval": tk.StringVar(value="-"),
            "published": tk.StringVar(value="-"),
            "pending": tk.StringVar(value="-"),
            "inventory": tk.StringVar(value="-"),
            "last_result": tk.StringVar(value="-"),
            "error": tk.StringVar(value="-"),
        }
        self.success_count = 0
        self.failure_count = 0
        self.run_started_at = None
        self.last_schedule_date = datetime.now().strftime("%Y-%m-%d")
        self.automation_state_error = ""
        try:
            self.automation_batches = self.automation_store.recover_interrupted()
        except (OSError, StateFileError) as exc:
            self.automation_batches: dict[str, BatchState] = {}
            self.automation_state_error = str(exc)
            self.status_var.set(self.automation_state_error)
        self.closing = False
        self._close_completed = False
        self.content_notebook = None
        self.plan_tab = None
        self.toolbar_notebook = None
        self.config_center_window = None
        self._config_center_open = False
        self._gallery_names: dict[str, str] = {}
        self._tooltip_window = None
        self._tooltip_label = None
        self._window_state_job = None
        self._scan_results: queue.SimpleQueue = queue.SimpleQueue()
        self._scan_generation = 0
        self._scan_pending = 0
        self._refresh_requested = False
        self._refresh_poll_job = None
        self._pending_rollover_date = ""
        self._manual_workflow_active = False
        self._scan_poll_job = None
        self._due_scan_keys: set[str] = set()
        self._fresh_due_scan_ready: set[str] = set()
        self._scan_threads: set[threading.Thread] = set()
        self._scan_threads_lock = threading.Lock()
        self._last_window_geometry = ""
        self.detail_value_labels: dict[str, ttk.Label] = {}
        self._load_window_geometry()
        self._load_startup_setting()
        self._init_sitegroup()
        self._build()
        self._render_site_table(scanning=True)
        if self.sites:
            self._render_site_details(self.sites[0].name)
        self.root.after(100, self.refresh_data)
        self.root.bind("<Configure>", self._schedule_window_state_save, add="+")
        self.root.after(15000, self._schedule_tick)
        if self.startup_status == "pending_default":
            self.root.after(500, self._ensure_default_startup)

    def _build(self) -> None:
        container = ttk.Frame(self.root, padding=8)
        container.pack(fill=tk.BOTH, expand=True)
        self._build_toolbar(container)
        self.content_notebook = ttk.Notebook(container)
        self.content_notebook.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        site_tab = ttk.Frame(self.content_notebook, padding=8)
        plan_tab = ttk.Frame(self.content_notebook, padding=8)
        detail_tab = ttk.Frame(self.content_notebook, padding=8)
        log_tab = ttk.Frame(self.content_notebook, padding=8)
        summary_tab = ttk.Frame(self.content_notebook, padding=8)
        self.plan_tab = plan_tab
        self.content_notebook.add(site_tab, text="站点状态")
        self.content_notebook.add(plan_tab, text="今日计划")
        self.content_notebook.add(detail_tab, text="详情")
        self.content_notebook.add(log_tab, text="日志")
        self.content_notebook.add(summary_tab, text="汇总")

        self._build_site_panel(site_tab)
        self._build_plan_panel(plan_tab)
        self._build_detail_panel(detail_tab)
        self._build_log_panel(log_tab)
        self._build_summary_panel(summary_tab)
        self._build_status_bar(container)

    def _build_toolbar(self, parent) -> None:
        wrapper = ttk.Frame(parent)
        wrapper.pack(fill=tk.X, pady=(0, 4))
        self.toolbar_notebook = ttk.Notebook(wrapper, style="Toolbar.TNotebook")
        self.toolbar_notebook.pack(fill=tk.X)

        publish_tab = ttk.Frame(self.toolbar_notebook, padding=(6, 4))
        manage_tab = ttk.Frame(self.toolbar_notebook, padding=(6, 4))
        tools_tab = ttk.Frame(self.toolbar_notebook, padding=(6, 4))
        self.toolbar_notebook.add(publish_tab, text="发布")
        self.toolbar_notebook.add(manage_tab, text="管理")
        self.toolbar_notebook.add(tools_tab, text="工具")

        self._build_toolbar_row(
            publish_tab,
            [
                ("全部发布", self.start_publish_all, "Accent.TButton"),
                ("立即发布选中", self.start_publish_selected_sites, "Compact.TButton"),
                ("暂停", self.pause_tasks, "Compact.TButton"),
                ("继续", self.resume_tasks, "Compact.TButton"),
                ("停止", self.stop_tasks, "Compact.TButton"),
            ],
        )
        self._build_toolbar_row(
            manage_tab,
            [
                ("建站", self.open_new_site_dialog, "Accent.TButton"),
                ("配置", self.open_config_center, "Compact.TButton"),
                ("图库", self.open_gallery_manager, "Compact.TButton"),
                ("刷新", self.refresh_data, "Compact.TButton"),
            ]
            + self._sitegroup_toolbar_actions(),
        )
        tools_row = self._build_toolbar_row(
            tools_tab,
            [
                ("失败归档", self.open_failure_archive, "Compact.TButton"),
                ("日志目录", self.open_log_directory, "Compact.TButton"),
            ],
        )
        ttk.Checkbutton(
            tools_row,
            text="登录后自动启动",
            variable=self.startup_var,
            command=self._toggle_startup,
        ).pack(side=tk.LEFT, padx=(12, 0))

    def _build_toolbar_row(self, parent, actions) -> ttk.Frame:
        row = ttk.Frame(parent)
        row.pack(fill=tk.X)
        for index, (label, command, style) in enumerate(actions):
            ttk.Button(row, text=label, command=command, style=style).pack(side=tk.LEFT, padx=(0, 6 if index < len(actions) - 1 else 0))
        return row

    def _load_startup_setting(self) -> None:
        if not self.startup_service.supported:
            self.startup_status = "unsupported"
            return
        try:
            preference = self.startup_service.preference()
            registered = self.startup_service.registered_command()
        except (OSError, StartupPreferenceError) as exc:
            self.startup_status = f"error:{exc}"
            return
        if preference is None:
            self.startup_status = "pending_default"
            return
        self.startup_var.set(registered == self.startup_service.build_command())
        if self.startup_var.get():
            self.startup_status = "enabled"
        elif registered is not None:
            self.startup_status = "foreign"
        else:
            self.startup_status = "disabled" if preference is False else "missing"

    def _ensure_default_startup(self) -> None:
        if self.closing or self.startup_status != "pending_default":
            return
        try:
            enabled = self.startup_service.ensure_default_enabled()
        except (OSError, StartupPreferenceError) as exc:
            self.startup_status = f"error:{exc}"
            self.status_var.set(f"登录自启动设置失败: {exc}")
            return
        self.startup_var.set(bool(enabled))
        if enabled:
            self.startup_status = "enabled"
        else:
            try:
                registered = self.startup_service.registered_command()
            except OSError:
                registered = None
            self.startup_status = (
                "foreign"
                if registered is not None
                and registered != self.startup_service.build_command()
                else "unsupported"
            )
        if enabled:
            self.status_var.set("已默认开启登录后自动启动")

    def _toggle_startup(self) -> None:
        requested = self.startup_var.get()
        try:
            changed = self.startup_service.set_enabled(requested)
        except (OSError, StartupPreferenceError) as exc:
            self.startup_var.set(not requested)
            self.status_var.set(f"登录自启动设置失败: {exc}")
            return
        if requested and not changed:
            self.startup_var.set(False)
            self.status_var.set("当前系统不支持登录自启动")
            return
        self.startup_status = "enabled" if requested else "disabled"
        self.status_var.set("已开启登录后自动启动" if requested else "已关闭登录后自动启动")

    def _build_site_panel(self, parent) -> None:
        columns = (
            "index",
            "name",
            "published_limit",
            "enabled",
            "category",
            "times",
            "interval",
            "pending",
            "gallery",
            "inventory",
        )
        self.site_tree = ttk.Treeview(parent, columns=columns, show="headings", selectmode="extended")
        specs = [
            ("index", "序号", 48),
            ("name", "站点", 130),
            ("published_limit", "今日已发/上限", 96),
            ("enabled", "启用", 64),
            ("category", "默认栏目", 130),
            ("times", "每日时间", 86),
            ("interval", "间隔", 72),
            ("pending", "待发", 62),
            ("gallery", "图库", 112),
            ("inventory", "库存状态", 180),
        ]
        for key, label, width in specs:
            self.site_tree.heading(key, text=label)
            self.site_tree.column(
                key,
                width=width,
                minwidth=max(52, width // 2),
                anchor=tk.W,
                stretch=key == "inventory",
            )
        y_scroll = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=self.site_tree.yview)
        x_scroll = ttk.Scrollbar(parent, orient=tk.HORIZONTAL, command=self.site_tree.xview)
        self.site_tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        self.site_tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        self.site_tree.bind("<<TreeviewSelect>>", self.on_site_selected)
        self.site_tree.bind("<Double-1>", self._handle_site_double_click)
        self.site_tree.bind("<Motion>", self._handle_site_tree_motion)
        self.site_tree.bind("<Leave>", self._handle_site_tree_leave)
        self._bind_sitegroup_context_menu()

    def _build_plan_panel(self, parent) -> None:
        columns = ("time", "site", "status", "total", "interval", "earliest_finish", "summary")
        self.plan_tree = ttk.Treeview(parent, columns=columns, show="headings")
        specs = [
            ("time", "时间", 72),
            ("site", "站点", 96),
            ("status", "状态", 90),
            ("total", "计划篇数", 70),
            ("interval", "间隔", 70),
            ("earliest_finish", "最早完成", 90),
            ("summary", "执行结果", 300),
        ]
        for key, label, width in specs:
            self.plan_tree.heading(key, text=label)
            self.plan_tree.column(key, width=width, anchor=tk.W)
        self.plan_tree.pack(fill=tk.BOTH, expand=True)
        self.plan_tree.bind("<Motion>", lambda event: self._handle_tree_hover(event, self.plan_tree))
        self.plan_tree.bind("<Leave>", lambda _event: self._hide_tooltip())

    def _build_detail_panel(self, parent) -> None:
        parent.columnconfigure(1, weight=1)
        parent.columnconfigure(3, weight=1)
        detail_rows = [
            (("站点", "name"), ("网址", "url")),
            (("启用状态", "enabled"), ("启用日期", "activation_date")),
            (("默认栏目", "category"), ("图库", "gallery")),
            (("每日时间", "times"), ("发布间隔", "interval")),
            (("今日已发/上限", "published"), ("待发文章", "pending")),
        ]
        for row, pairs in enumerate(detail_rows):
            for pair_index, (label, key) in enumerate(pairs):
                column = pair_index * 2
                ttk.Label(parent, text=label, style="Muted.TLabel").grid(
                    row=row,
                    column=column,
                    sticky="nw",
                    padx=(0, 8),
                    pady=6,
                )
                value_label = ttk.Label(
                    parent,
                    textvariable=self.detail_vars[key],
                    wraplength=420,
                    justify=tk.LEFT,
                )
                value_label.grid(row=row, column=column + 1, sticky="nw", padx=(0, 24), pady=6)
                self._bind_detail_tooltip(value_label, key)

        for offset, (label, key) in enumerate(
            (("库存状态", "inventory"), ("最近结果", "last_result"), ("异常", "error")),
            start=len(detail_rows),
        ):
            ttk.Label(parent, text=label, style="Muted.TLabel").grid(
                row=offset,
                column=0,
                sticky="nw",
                padx=(0, 8),
                pady=6,
            )
            value_label = ttk.Label(
                parent,
                textvariable=self.detail_vars[key],
                wraplength=880,
                justify=tk.LEFT,
            )
            value_label.grid(row=offset, column=1, columnspan=3, sticky="new", pady=6)
            self._bind_detail_tooltip(value_label, key)

    def _bind_detail_tooltip(self, label: ttk.Label, key: str) -> None:
        label.bind("<Enter>", lambda event, detail_key=key: self._handle_detail_hover(event, detail_key))
        label.bind("<Motion>", lambda event, detail_key=key: self._handle_detail_hover(event, detail_key))
        label.bind("<Leave>", lambda _event: self._hide_tooltip())
        self.detail_value_labels[key] = label

    def _build_log_panel(self, parent) -> None:
        self.log_text = tk.Text(parent, wrap="word", state=tk.DISABLED)
        scroll = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

    def _build_summary_panel(self, parent) -> None:
        metrics = ttk.Frame(parent)
        metrics.pack(anchor="nw")
        for index, (label, var) in enumerate(self.metric_vars.items()):
            ttk.Label(metrics, text=label, style="Muted.TLabel").grid(
                row=index // 2,
                column=(index % 2) * 2,
                sticky="w",
                padx=(0, 8),
                pady=6,
            )
            ttk.Label(metrics, textvariable=var, style="Metric.TLabel").grid(
                row=index // 2,
                column=(index % 2) * 2 + 1,
                sticky="w",
                padx=(0, 32),
                pady=6,
            )

    def _build_status_bar(self, parent) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(bar, textvariable=self.status_var).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Progressbar(bar, variable=self.progress_value, maximum=100).pack(side=tk.LEFT, fill=tk.X, expand=True)

    def _refresh_busy_reason(self) -> str:
        if self._tasks_running():
            return "发布任务正在执行"
        if self._manual_workflow_active:
            return "手动发布准备中"
        if self._scan_pending:
            return "站点扫描正在进行"
        return ""

    def _schedule_queued_refresh_check(
        self,
        delay: int = 250,
        *,
        replace_existing: bool = False,
    ) -> None:
        if self.closing or not self._refresh_requested:
            return
        if self._refresh_poll_job is not None:
            if not replace_existing:
                return
            try:
                self.root.after_cancel(self._refresh_poll_job)
            except tk.TclError:
                pass
            self._refresh_poll_job = None
        self._refresh_poll_job = self.root.after(
            max(int(delay), 1),
            self._try_run_queued_refresh,
        )

    def _try_run_queued_refresh(self) -> None:
        self._refresh_poll_job = None
        if self.closing or not self._refresh_requested:
            return
        busy_reason = self._refresh_busy_reason()
        if busy_reason:
            self._schedule_queued_refresh_check()
            return
        self.refresh_data()

    def _queue_rollover_refresh(self, run_date: str, busy_reason: str) -> None:
        self._pending_rollover_date = run_date
        self._refresh_requested = True
        self.status_var.set(f"{busy_reason}，跨日刷新将在空闲后执行")
        self._schedule_queued_refresh_check()

    def refresh_data(self) -> None:
        busy_reason = self._refresh_busy_reason()
        if busy_reason:
            self._refresh_requested = True
            self.status_var.set(f"{busy_reason}，刷新将在空闲后执行")
            self._schedule_queued_refresh_check()
            return
        today = datetime.now().strftime("%Y-%m-%d")
        rollover_date = self._pending_rollover_date
        if today != self.last_schedule_date:
            rollover_date = today
        self._pending_rollover_date = ""
        self._refresh_requested = False
        if rollover_date:
            self._request_scan(
                self.sites,
                reason="full",
                run_date=rollover_date,
            )
        else:
            self._request_scan(self.sites, reason="full")

    def _request_scan(
        self,
        sites: list[SiteConfig],
        *,
        reason: str,
        due_site_ids: tuple[str, ...] = (),
        run_date: str = "",
        manual_site_names: tuple[str, ...] = (),
    ) -> None:
        if reason != "due":
            self._scan_generation += 1
        generation = self._scan_generation
        snapshots = [
            replace(site, publish_times=list(site.publish_times))
            for site in sites
        ]
        self._scan_pending += 1

        def worker() -> None:
            try:
                summaries, articles = self.site_service.scan_all_sites(snapshots)
                error = ""
            except Exception as exc:
                summaries, articles = {}, {}
                error = str(exc)
            try:
                self._scan_results.put(
                    (
                        generation,
                        reason,
                        due_site_ids,
                        run_date,
                        summaries,
                        articles,
                        error,
                        manual_site_names,
                    )
                )
            finally:
                with self._scan_threads_lock:
                    self._scan_threads.discard(threading.current_thread())

        scan_thread = threading.Thread(target=worker, daemon=True, name="site-scan")
        with self._scan_threads_lock:
            self._scan_threads.add(scan_thread)
        scan_thread.start()
        if self._scan_poll_job is None and not self.closing:
            self._scan_poll_job = self.root.after(50, self._poll_scan_results)

    def _poll_scan_results(self) -> None:
        self._scan_poll_job = None
        while not self._scan_results.empty():
            (
                generation,
                reason,
                due_site_ids,
                run_date,
                summaries,
                articles,
                error,
                manual_site_names,
            ) = self._scan_results.get()
            self._scan_pending = max(self._scan_pending - 1, 0)
            if generation != self._scan_generation:
                for site_id in due_site_ids:
                    self._due_scan_keys.discard(f"{run_date}|{site_id}")
                if reason == "manual":
                    self._manual_workflow_active = False
                if reason == "full" and run_date:
                    self._pending_rollover_date = run_date
                    self._refresh_requested = True
                continue
            if error:
                for site_id in due_site_ids:
                    self._due_scan_keys.discard(f"{run_date}|{site_id}")
                if reason == "manual":
                    self._manual_workflow_active = False
                if reason == "full" and run_date:
                    self._pending_rollover_date = run_date
                    self._refresh_requested = True
                    self._schedule_queued_refresh_check(
                        15000,
                        replace_existing=True,
                    )
                self.status_var.set(f"文章扫描失败: {error}")
                continue
            self._apply_scan_result(
                summaries,
                articles,
                replace_all=reason == "full",
            )
            if reason == "full" and run_date:
                self.last_schedule_date = run_date
                if self._pending_rollover_date == run_date:
                    self._pending_rollover_date = ""
                self._refresh_requested = False
            if reason == "due" and due_site_ids:
                for site_id in due_site_ids:
                    key = AutomationStateStore.batch_key(run_date, site_id)
                    self._due_scan_keys.discard(key)
                    self._fresh_due_scan_ready.add(key)
                self._schedule_tick(schedule_next=False)
            elif reason == "manual":
                self._run_manual_after_scan(set(manual_site_names))
        if self._scan_pending and not self.closing:
            self._scan_poll_job = self.root.after(50, self._poll_scan_results)
        elif self._refresh_requested and not self.closing:
            self._schedule_queued_refresh_check()

    def _apply_scan_result(
        self,
        summaries: dict[str, SiteStatusSummary],
        articles: dict[str, list[ArticleRecord]],
        *,
        replace_all: bool,
    ) -> None:
        if replace_all:
            self.site_summaries = summaries
            self.articles_by_site = articles
        else:
            self.site_summaries.update(summaries)
            self.articles_by_site.update(articles)
        self.daily_plan = build_daily_plan(self.sites, self.site_summaries)
        recovery_needed = self.automation_store.recovery_required
        try:
            self.automation_batches = self.automation_store.load()
            self.automation_state_error = ""
        except StateRecoveryRequired as exc:
            self.automation_batches = {}
            self.automation_state_error = str(exc)
            recovery_needed = True
        except (OSError, StateFileError) as exc:
            self.automation_state_error = str(exc)
        if recovery_needed:
            if not replace_all:
                self.status_var.set("自动化状态待恢复，正在执行全站扫描...")
                self.refresh_data()
                return
            today = datetime.now().strftime("%Y-%m-%d")
            quarantine = self.automation_store.last_quarantined_path
            note = "自动化状态损坏后按本地证据重建"
            if quarantine is not None:
                note += f": {quarantine.name}"
            rebuilt_batches: dict[str, BatchState] = {}
            for site in self.sites:
                if not site.site_id:
                    continue
                summary = self.site_summaries.get(
                    site.name,
                    SiteStatusSummary(site_name=site.name),
                )
                rebuilt = self.automation_store.rebuild_from_evidence(
                    today,
                    site.site_id,
                    success_count=summary.published_today,
                    pending_count=summary.pending_count,
                    daily_limit=site.daily_publish_limit,
                    pending_attempt=self.archive_service.has_pending_attempt(
                        site.site_id or site.name
                    ),
                    note=note,
                    evidence_error=(
                        summary.last_error if summary.status == "error" else ""
                    ),
                )
                rebuilt_batches[rebuilt.batch_key] = rebuilt
            self.automation_store.complete_recovery(rebuilt_batches)
            self.automation_batches = rebuilt_batches
            self.automation_state_error = note
        self._render_site_table()
        self._render_plan_table()
        self.metric_vars["总文章"].set(str(sum(len(items) for items in self.articles_by_site.values())))
        if self.current_site_name and self.current_site_name in {site.name for site in self.sites}:
            self._render_site_details(self.current_site_name)
        elif self.sites:
            self._render_site_details(self.sites[0].name)

    @staticmethod
    def _site_row_id(site: SiteConfig) -> str:
        return site.site_id or f"site:{site.name}"

    @staticmethod
    def _category_display(site: SiteConfig) -> str:
        if site.category_name.strip():
            return site.category_name.strip()
        if site.fid > 0:
            return f"栏目 ID {site.fid}"
        return "未配置"

    @staticmethod
    def _publish_time_display(site: SiteConfig) -> str:
        if site.publish_time:
            return site.publish_time
        return site.publish_times[0] if site.publish_times else "-"

    def _gallery_display(self, site: SiteConfig) -> str:
        if not site.gallery_id:
            return "未配置"
        return self._gallery_names.get(site.gallery_id, "图库不可用")

    def _inventory_for_site(self, site: SiteConfig) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        batch = (
            self.automation_batches.get(
                AutomationStateStore.batch_key(today, site.site_id)
            )
            if site.site_id
            else None
        )
        if site.site_id and (batch is None or batch.inventory_count is None):
            batch = self._latest_inventory_batch(site.site_id)
        return self._inventory_text(site, batch)

    def _render_site_table(self, *, scanning: bool = False) -> None:
        selected_rows = set(self.site_tree.selection())
        focused_row = self.site_tree.focus()
        for item in self.site_tree.get_children():
            self.site_tree.delete(item)
        try:
            self._gallery_names = {
                gallery.gallery_id: gallery.name
                for gallery in self.gallery_service.list_galleries()
            }
        except GalleryError:
            self._gallery_names = {}

        inserted_rows: list[str] = []
        for index, site in enumerate(self.sites, start=1):
            summary = self.site_summaries.get(site.name)
            row_id = self._site_row_id(site)
            inserted_rows.append(row_id)
            if summary is None and scanning:
                published_text = f"扫描中 / {site.daily_publish_limit}"
                pending_text: int | str = "扫描中"
                inventory_text = "扫描中"
            else:
                summary = summary or SiteStatusSummary(site_name=site.name)
                published_text = f"{summary.published_today} / {site.daily_publish_limit}"
                pending_text = "异常" if summary.status == "error" else summary.pending_count
                inventory_text = self._inventory_for_site(site)
            self.site_tree.insert(
                "",
                tk.END,
                iid=row_id,
                values=(
                    index,
                    site.name,
                    published_text,
                    "启用" if site.enabled else "停用",
                    self._category_display(site),
                    self._publish_time_display(site),
                    f"{site.interval_time} 秒",
                    pending_text,
                    self._gallery_display(site),
                    inventory_text,
                ),
            )

        restored = [row_id for row_id in inserted_rows if row_id in selected_rows]
        if not restored and self.current_site_name:
            restored = [
                self._site_row_id(site)
                for site in self.sites
                if site.name == self.current_site_name
            ]
        if not restored and inserted_rows:
            restored = [inserted_rows[0]]
        if restored:
            self.site_tree.selection_set(*restored)
            focus = focused_row if focused_row in restored else restored[-1]
            self.site_tree.focus(focus)
            self.site_tree.see(focus)

    def _render_plan_table(self) -> None:
        for item in self.plan_tree.get_children():
            self.plan_tree.delete(item)
        for plan in self.daily_plan:
            status = self._runtime_status_for_plan(plan)
            site = next((item for item in self.sites if item.name == plan.site_name), None)
            interval = site.interval_time if site else 0
            earliest_finish = self._earliest_finish(plan.scheduled_time, plan.total_count, interval)
            self.plan_tree.insert(
                "",
                tk.END,
                values=(
                    plan.scheduled_time,
                    plan.site_name,
                    status,
                    plan.total_count,
                    f"{interval} 秒",
                    earliest_finish,
                    self._compact_text(self._plan_summary(plan), 36),
                ),
            )

    @staticmethod
    def _inventory_text(site: SiteConfig, batch: BatchState | None) -> str:
        if batch is None or batch.inventory_count is None:
            return "待批次后检查"
        count = batch.inventory_count
        threshold = site.article_warning_threshold
        if count < threshold:
            suffix = f"，异常 {batch.inventory_unreadable}" if batch.inventory_unreadable else ""
            return f"库存不足：{count}/{threshold}{suffix}"
        return f"正常：{count}/{threshold}"

    def _latest_inventory_batch(self, site_id: str) -> BatchState | None:
        candidates = [
            batch
            for batch in self.automation_batches.values()
            if batch.site_id == site_id and batch.inventory_checked_at
        ]
        return max(candidates, key=lambda batch: batch.inventory_checked_at, default=None)

    def _gallery_block_reason(self, site: SiteConfig) -> str:
        if not site.gallery_id:
            return "未配置图库"
        try:
            gallery = self.gallery_service.require_gallery(site.gallery_id)
            validation = self.gallery_service.validate_gallery(site.gallery_id)
        except GalleryError as exc:
            return f"图库不可用: {exc}"
        if not validation.valid:
            return f"图库不可用: {validation.message}"
        if site.image_file is None:
            return "图库文件未配置"
        try:
            matches_registry = (
                Path(site.image_file).resolve() == gallery.file_path.resolve()
            )
        except OSError:
            matches_registry = False
        return "" if matches_registry else "图库文件与注册记录不一致"

    @staticmethod
    def _site_config_fingerprint(site: SiteConfig) -> str:
        payload = {
            "url": site.url,
            "endpoint": site.jiekou,
            "fid": site.fid,
            "gallery_id": site.gallery_id,
            "image_file": str(site.image_file or ""),
            "enabled": site.enabled,
        }
        serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _earliest_finish(scheduled_time: str, total: int, interval: int) -> str:
        try:
            start = datetime.combine(date.today(), datetime.strptime(scheduled_time, "%H:%M:%S").time())
        except ValueError:
            return "-"
        finish = start + timedelta(seconds=max(total - 1, 0) * max(interval, 0))
        return f"不早于 {finish:%H:%M}"

    def _plan_summary(self, plan: PlannedRun) -> str:
        site = next((item for item in self.sites if item.name == plan.site_name), None)
        if site is None or not site.site_id:
            return "缺少稳定站点 ID，自动发布已阻断"
        batch = self.automation_batches.get(
            AutomationStateStore.batch_key(self.last_schedule_date, site.site_id)
        )
        if batch is None:
            return plan.result_summary
        result = batch.stop_reason or plan.result_summary
        inventory_batch = (
            batch
            if batch.inventory_count is not None
            else self._latest_inventory_batch(site.site_id)
        )
        if (
            inventory_batch is not None
            and inventory_batch.inventory_count is not None
            and inventory_batch.inventory_count < inventory_batch.inventory_threshold
        ):
            warning = (
                f"库存不足：{inventory_batch.inventory_count}/"
                f"{inventory_batch.inventory_threshold}"
            )
            result = f"{result}；{warning}" if result else warning
        return result

    def on_site_selected(self, _event=None) -> None:
        selected = self.site_tree.selection()
        if not selected:
            return
        focused = self.site_tree.focus()
        row_id = focused if focused in selected else selected[-1]
        if focused not in selected:
            self.site_tree.focus(row_id)
        site_name = self.site_tree.item(row_id, "values")[1]
        self._render_site_details(site_name)

    def _render_site_details(self, site_name: str) -> None:
        self.current_site_name = site_name
        site = next((item for item in self.sites if item.name == site_name), None)
        if site is None:
            return
        summary = self.site_summaries.get(site_name)
        published_today = summary.published_today if summary is not None else 0
        pending_count: int | str = summary.pending_count if summary is not None else "-"
        self.detail_vars["name"].set(site.name)
        self.detail_vars["url"].set(site.url or "-")
        self.detail_vars["enabled"].set("启用" if site.enabled else "停用")
        self.detail_vars["activation_date"].set(site.activation_date or "-")
        self.detail_vars["category"].set(self._category_display(site))
        self.detail_vars["gallery"].set(self._gallery_display(site))
        self.detail_vars["times"].set(self._publish_time_display(site))
        self.detail_vars["interval"].set(f"{site.interval_time} 秒")
        self.detail_vars["published"].set(f"{published_today} / {site.daily_publish_limit}")
        self.detail_vars["pending"].set(str(pending_count))
        self.detail_vars["inventory"].set(self._inventory_for_site(site))
        self.detail_vars["last_result"].set(
            summary.last_result if summary is not None and summary.last_result else "-"
        )
        messages: list[str] = []
        if self.config_error_message:
            messages.append(self.config_error_message)
        if self.automation_state_error:
            messages.append(self.automation_state_error)
        if summary is not None and summary.status == "error":
            messages.append(f"站点异常：{summary.last_error or '无法读取站点状态'}")
        article_errors = [
            article.last_error
            for article in self.articles_by_site.get(site_name, [])
            if article.status == "error" and article.last_error
        ]
        if article_errors:
            messages.append(f"文章异常：{article_errors[0]}")
        detail_error = (
            summary.last_error
            if summary is not None and summary.last_error
            else article_errors[0] if article_errors else "-"
        )
        self.detail_vars["error"].set(detail_error)
        if messages:
            self.status_var.set("；".join(messages))
        else:
            selected_count = len(self.site_tree.selection())
            self.status_var.set(
                f"已选择 {selected_count} 个站点" if selected_count > 1 else "就绪"
            )

    def append_log(self, message: str) -> None:
        if self.closing:
            return
        self.root.after(0, self._append_log_on_ui, message)

    def _append_log_on_ui(self, message: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _append_publish_result_log(self, result: PublishResult) -> None:
        self.log_text.configure(state=tk.NORMAL)
        timestamp = result.published_at.strftime("%Y-%m-%d %H:%M:%S")
        prefix = f"{result.site_name} -> "
        title = result.title
        suffix = f" [{timestamp}]：{result.message}"
        if result.article_url:
            tag_name = f"article-link-{self.log_text.index(tk.END).replace('.', '-') }"
            self.log_text.tag_configure(tag_name, foreground="#0b63ce", underline=True)
            self.log_text.tag_bind(
                tag_name,
                "<Button-1>",
                lambda _event, url=result.article_url: self._open_article_url(url),
            )
            self.log_text.tag_bind(tag_name, "<Enter>", lambda _event: self.log_text.configure(cursor="hand2"))
            self.log_text.tag_bind(tag_name, "<Leave>", lambda _event: self.log_text.configure(cursor=""))
            self.log_text.insert(tk.END, prefix)
            self.log_text.insert(tk.END, title, tag_name)
            self.log_text.insert(tk.END, f" [{timestamp}]：{result.message} ")
            self.log_text.insert(tk.END, result.article_url, tag_name)
            self.log_text.insert(tk.END, "\n")
        else:
            self.log_text.insert(tk.END, prefix + title + suffix + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _open_article_url(self, url: str) -> None:
        try:
            webbrowser.open_new_tab(url)
        except Exception as exc:
            self.status_var.set(f"无法打开文章链接: {exc}")

    def _task_pairs(
        self,
        selected_sites: set[str] | None = None,
        *,
        allow_disabled: bool = False,
    ):
        return self.task_manager.build_tasks(
            self.sites,
            self.articles_by_site,
            selected_sites=selected_sites,
            allow_disabled=allow_disabled,
        )

    def _publish_busy_reason(self) -> str:
        if self._tasks_running():
            return "已有发布任务正在执行"
        if self._scan_pending:
            return "站点扫描正在进行，请等待扫描完成"
        if self._manual_workflow_active:
            return "手动发布准备中，请等待当前操作完成"
        return ""

    def start_publish_all(self) -> None:
        busy_reason = self._publish_busy_reason()
        if busy_reason:
            self.status_var.set(busy_reason)
            return
        site_names = {site.name for site in self.sites}
        self._request_manual_scan(site_names)

    def start_publish_selected_sites(self) -> None:
        busy_reason = self._publish_busy_reason()
        if busy_reason:
            self.status_var.set(busy_reason)
            return
        selected_site_names = {
            self.site_tree.item(item, "values")[1]
            for item in self.site_tree.selection()
        }
        if not selected_site_names:
            self.status_var.set("请先选中至少一个站点")
            return
        self._request_manual_scan(selected_site_names)

    def _request_manual_scan(self, site_names: set[str]) -> None:
        busy_reason = self._publish_busy_reason()
        if busy_reason:
            self.status_var.set(busy_reason)
            return
        if self.automation_config_blocked:
            self.status_var.set(self.config_error_message or "自动发布配置不可用")
            return
        sites = [
            site
            for site in self.sites
            if site.name in site_names
        ]
        if not sites:
            self.status_var.set("没有可扫描的站点")
            return
        self.status_var.set("正在扫描最新文章...")
        self._manual_workflow_active = True
        try:
            self._request_scan(
                sites,
                reason="manual",
                manual_site_names=tuple(site.name for site in sites),
            )
        except Exception:
            self._manual_workflow_active = False
            raise

    def _run_manual_after_scan(self, site_names: set[str]) -> None:
        try:
            self._run_manual_after_scan_impl(site_names)
        finally:
            self._manual_workflow_active = False

    def _run_manual_after_scan_impl(self, site_names: set[str]) -> None:
        healthy_names = {
            name
            for name in site_names
            if self.site_summaries.get(name) is not None
            and self.site_summaries[name].status != "error"
        }
        failed_names = sorted(site_names - healthy_names)
        if failed_names:
            self.status_var.set("以下站点扫描失败: " + ", ".join(failed_names))
        tasks = self._task_pairs(
            selected_sites=healthy_names,
            allow_disabled=True,
        )
        if not tasks:
            diagnostics = [
                diagnostic.message
                for name, diagnostic in self.task_manager.last_build_diagnostics.items()
                if name in healthy_names and diagnostic.severity == "error"
            ]
            if diagnostics:
                self.status_var.set(diagnostics[0])
            elif not failed_names:
                self.status_var.set("没有可执行任务")
            return
        task_site_names = {site.name for site, _article in tasks}
        skipped = [
            f"{name}：{diagnostic.message}"
            for name, diagnostic in self.task_manager.last_build_diagnostics.items()
            if name in healthy_names and name not in task_site_names
        ]
        for message in skipped:
            self.append_log(f"手动发布跳过 {message}")
        if len(site_names) > 1:
            message = f"将发布 {len(task_site_names)} 个站点，共 {len(tasks)} 篇文章。"
            if skipped or failed_names:
                message += f"\n另有 {len(skipped) + len(failed_names)} 个站点被跳过。"
            message += "\n\n确定立即发布吗？"
            confirmed = messagebox.askyesno(
                "确认批量发布",
                message,
                parent=self.root,
            )
            if not confirmed:
                self.status_var.set("已取消批量发布")
                return
            busy_reason = (
                "已有发布任务正在执行"
                if self._tasks_running()
                else "站点扫描正在进行，请等待扫描完成"
                if self._scan_pending
                else ""
            )
            if busy_reason:
                self.status_var.set(busy_reason)
                return
        self._run_tasks(tasks, "开始手动发布")

    def _run_tasks(
        self,
        tasks,
        label: str,
        batch_context: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        if self.task_manager.worker and self.task_manager.worker.is_alive():
            self.status_var.set("已有发布任务正在执行")
            return
        total = len(tasks)
        if total == 0:
            self.status_var.set("没有可执行任务")
            return
        self.success_count = 0
        self.failure_count = 0
        self.failed_results.clear()
        self._live_applied_results.clear()
        self.progress_value.set(0)
        self.metric_vars["成功"].set("0")
        self.metric_vars["失败"].set("0")
        self.run_started_at = time.time()
        self.status_var.set(label)
        batch_context = batch_context or {}
        if batch_context:
            try:
                transitioned = self.automation_store.transition_many(
                    batch_context.values(),
                    "running",
                )
            except (OSError, StateFileError) as exc:
                self.status_var.set(f"自动化状态保存失败，任务未启动: {exc}")
                return
            self.automation_batches.update(transitioned)

        def on_log(message: str) -> None:
            self.append_log(message)

        def on_result(result: PublishResult) -> bool:
            context = batch_context.get(result.site_id) or batch_context.get(result.site_name)
            persisted = True
            if context is not None:
                run_date, site_id = context
                try:
                    self.automation_store.record_attempt(
                        run_date, site_id, success=result.success
                    )
                except (OSError, StateFileError) as exc:
                    self.append_log(f"自动化结果保存失败: {result.site_name}: {exc}")
                    persisted = False
            if not self.closing:
                self.root.after(0, self._handle_publish_result, result, total)
            return persisted

        def on_site_complete(site: SiteConfig, results: list[PublishResult], reason: str) -> None:
            context = batch_context.get(site.site_id) or batch_context.get(site.name)
            batch = None
            if context is not None:
                run_date, site_id = context
                try:
                    self._record_inventory(site, run_date, site_id, reason)
                    if reason == "retry_wait":
                        batch = self.automation_store.schedule_retry(
                            run_date,
                            site_id,
                            reason=results[-1].message if results else "等待重试",
                        )
                    else:
                        final_status = reason if reason in {"blocked", "stopped"} else "completed"
                        stop_code = {
                            "blocked": results[-1].failure_kind if results else "blocked",
                            "stopped": "user_stopped",
                            "completed": "completed",
                        }[final_status]
                        batch = self.automation_store.transition(
                            run_date,
                            site_id,
                            final_status,
                            stop_code=stop_code,
                            config_fingerprint=self._site_config_fingerprint(site),
                            stop_reason=(results[-1].message if results and final_status != "completed" else ""),
                            next_retry_at="",
                        )
                except Exception as exc:
                    self.append_log(f"自动化批次保存失败: {site.name}: {exc}")
                    try:
                        batch = self.automation_store.transition(
                            run_date,
                            site_id,
                            "blocked",
                            stop_code="scan_error",
                            stop_reason=f"本地库存或批次状态保存失败: {exc}",
                            config_fingerprint=self._site_config_fingerprint(site),
                            next_retry_at="",
                        )
                    except (OSError, StateFileError) as block_exc:
                        self.append_log(
                            f"自动化批次无法写入阻断状态: {site.name}: {block_exc}"
                        )
            if not self.closing:
                self.root.after(0, self._handle_site_complete_ui, site, results, batch)

        self.task_manager.run_tasks(
            tasks,
            on_log=on_log,
            on_result=on_result,
            on_site_complete=on_site_complete,
        )

    @staticmethod
    def _publish_result_text(result: PublishResult) -> str:
        published_at = result.published_at.strftime("%Y-%m-%d %H:%M:%S")
        link_suffix = f" {result.article_url}" if result.article_url else ""
        return f"{result.title} [{published_at}]：{result.message}{link_suffix}"

    def _apply_live_publish_success(self, result: PublishResult) -> bool:
        if not result.success:
            return False
        applied_results = getattr(self, "_live_applied_results", None)
        if applied_results is None:
            applied_results = {}
            self._live_applied_results = applied_results
        result_id = id(result)
        if result_id in applied_results:
            return False
        site = next(
            (
                item
                for item in self.sites
                if item.name == result.site_name
                or (result.site_id and item.site_id == result.site_id)
            ),
            None,
        )
        if site is None:
            return False

        applied_results[result_id] = result
        cached_articles = [
            article
            for article in self.articles_by_site.get(site.name, [])
            if str(article.path) != str(result.article_path)
        ]
        self.articles_by_site[site.name] = cached_articles
        previous = self.site_summaries.get(
            site.name,
            SiteStatusSummary(
                site_name=site.name,
                daily_limit=site.daily_publish_limit,
            ),
        )
        published_today = previous.published_today + 1
        pending_count = sum(article.status == "pending" for article in cached_articles)
        unreadable = sum(article.status == "error" for article in cached_articles)
        self.site_summaries[site.name] = replace(
            previous,
            pending_count=pending_count,
            article_count=len(cached_articles),
            published_today=published_today,
            daily_limit=site.daily_publish_limit,
            remaining_quota=max(site.daily_publish_limit - published_today, 0),
            status="error" if unreadable else "idle",
            last_result=self._publish_result_text(result),
            last_error=(f"有 {unreadable} 篇文章无法读取" if unreadable else ""),
        )
        self.daily_plan = build_daily_plan(self.sites, self.site_summaries)
        self._render_site_table()
        self._render_plan_table()
        if self.current_site_name == site.name:
            self._render_site_details(site.name)
        self.metric_vars["总文章"].set(
            str(sum(len(items) for items in self.articles_by_site.values()))
        )
        return True

    def _handle_publish_result(self, result: PublishResult, total: int) -> None:
        self._apply_live_publish_success(result)
        self._append_publish_result_log(result)
        if result.success:
            self.success_count += 1
        else:
            self.failure_count += 1
            self.failed_results.append(result)
        completed = self.success_count + self.failure_count
        self.metric_vars["成功"].set(str(self.success_count))
        self.metric_vars["失败"].set(str(self.failure_count))
        self.progress_value.set((completed / total) * 100)
        self.status_var.set(f"已完成 {completed}/{total}")
        if self.run_started_at is not None:
            self.metric_vars["耗时"].set(f"{time.time() - self.run_started_at:.1f}s")

    def _record_inventory(
        self,
        site: SiteConfig,
        run_date: str,
        site_id: str,
        reason: str,
    ) -> BatchState:
        articles = self.site_service.list_articles(site)
        pending = sum(article.status == "pending" for article in articles)
        unreadable = sum(article.status == "error" for article in articles)
        return self.automation_store.record_inventory(
            run_date,
            site_id,
            count=pending,
            threshold=site.article_warning_threshold,
            unreadable=unreadable,
            reason=reason,
        )

    def _handle_site_complete_ui(
        self,
        site: SiteConfig,
        results: list[PublishResult],
        batch: BatchState | None,
    ) -> None:
        if batch is not None:
            self.automation_batches[batch.batch_key] = batch
        result_by_path = {str(result.article_path): result for result in results}
        cached_articles: list[ArticleRecord] = []
        for article in self.articles_by_site.get(site.name, []):
            result = result_by_path.get(str(article.path))
            if result is None:
                cached_articles.append(article)
                continue
            if result.success or result.failure_kind in {"article_rejected", "uncertain"}:
                continue
            article.last_error = result.message
            article.status = "pending" if result.retryable else "error"
            cached_articles.append(article)
        self.articles_by_site[site.name] = cached_articles

        previous = self.site_summaries.get(
            site.name,
            SiteStatusSummary(
                site_name=site.name,
                daily_limit=site.daily_publish_limit,
            ),
        )
        applied_results = getattr(self, "_live_applied_results", None)
        if applied_results is None:
            applied_results = {}
            self._live_applied_results = applied_results
        published_today = previous.published_today + sum(
            result.success and id(result) not in applied_results for result in results
        )
        applied_results.update(
            (id(result), result) for result in results if result.success
        )
        pending_count = (
            batch.inventory_count
            if batch is not None and batch.inventory_count is not None
            else sum(article.status == "pending" for article in cached_articles)
        )
        unreadable = batch.inventory_unreadable if batch is not None else sum(
            article.status == "error" for article in cached_articles
        )
        self.site_summaries[site.name] = SiteStatusSummary(
            site_name=site.name,
            pending_count=pending_count,
            article_count=pending_count + unreadable,
            published_today=published_today,
            daily_limit=site.daily_publish_limit,
            remaining_quota=max(site.daily_publish_limit - published_today, 0),
            status="error" if unreadable else "idle",
            last_result=(
                batch.stop_reason
                if batch is not None and batch.stop_reason
                else self._publish_result_text(results[-1])
                if results
                else previous.last_result
            ),
            last_error=(f"有 {unreadable} 篇文章无法读取" if unreadable else ""),
        )
        self.daily_plan = build_daily_plan(self.sites, self.site_summaries)
        self._render_site_table()
        self._render_plan_table()
        if self.current_site_name == site.name:
            self._render_site_details(site.name)
        self.metric_vars["总文章"].set(
            str(sum(len(items) for items in self.articles_by_site.values()))
        )
        if batch is not None and batch.inventory_count is not None:
            if batch.inventory_count < batch.inventory_threshold:
                self.append_log(
                    f"{site.name} 库存不足: {batch.inventory_count}/{batch.inventory_threshold}"
                )
        if not results and batch is not None:
            self.status_var.set(batch.stop_reason or "批次检查完成")

    def pause_tasks(self) -> None:
        self.task_manager.pause()
        self.status_var.set("已暂停")

    def resume_tasks(self) -> None:
        self.task_manager.resume()
        self.status_var.set("继续执行")

    def stop_tasks(self) -> None:
        self.task_manager.stop()
        self.status_var.set("已停止")

    def open_failure_archive(self) -> None:
        site_name = self.current_site_name or (self.sites[0].name if self.sites else "")
        site = next((item for item in self.sites if item.name == site_name), None)
        archive_key = (site.site_id or site.name) if site is not None else ""
        archive_dir = self.publisher.archive_service.failure_dir(archive_key) if archive_key else self.publisher.archive_service.archive_root / "发布失败"
        archive_dir.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["explorer", str(archive_dir)])

    def _tasks_running(self) -> bool:
        return bool(self.task_manager.worker and self.task_manager.worker.is_alive())

    def _configuration_busy_reason(self) -> str:
        if self._tasks_running():
            return "发布任务运行中，暂不能修改配置"
        if self._scan_pending:
            return "站点扫描中，扫描完成后再修改配置"
        return ""

    def open_config_center(self) -> None:
        busy_reason = self._configuration_busy_reason()
        if busy_reason:
            self.status_var.set(busy_reason)
            return
        if (
            self._config_center_open
            and self.config_center_window is not None
            and self.config_center_window.winfo_exists()
        ):
            self.config_center_window.lift()
            self.config_center_window.focus_force()
            return
        dialog = ConfigCenter(
            self.root,
            self.config_path,
            self.sites,
            self._apply_saved_sites,
            None,
            self.open_new_site_dialog,
            gallery_service=self.gallery_service,
            is_task_running=self._tasks_running,
            jump_service=self.admin_jump_service,
            integration_settings=self.integration_settings,
        )
        self.config_center_window = dialog
        self._config_center_open = dialog is not None
        if dialog is not None and hasattr(dialog, "bind"):
            dialog.bind(
                "<Destroy>",
                lambda event, current=dialog: self._handle_config_center_destroyed(
                    event, current
                ),
                add="+",
            )

    def _handle_config_center_destroyed(self, event, dialog) -> None:
        if event.widget is not dialog:
            return
        if self.config_center_window is dialog:
            self.config_center_window = None
            self._config_center_open = False

    def open_gallery_manager(self) -> None:
        busy_reason = self._configuration_busy_reason()
        if busy_reason:
            self.status_var.set(busy_reason.replace("修改配置", "修改图库"))
            return
        GalleryManager(
            self.root,
            self.gallery_service,
            self.sites,
            on_changed=self._handle_gallery_changed,
            is_task_running=self._tasks_running,
        )

    def _handle_gallery_changed(self) -> None:
        self._refresh_after_config_commit("图库已更新")

    def open_new_site_dialog(self, template_site=None) -> None:
        busy_reason = self._configuration_busy_reason()
        if busy_reason:
            self.status_var.set(busy_reason.replace("修改配置", "新增或复制站点"))
            return
        NewSiteDialog(
            self.root,
            self.sites,
            self._append_sites,
            template_site=template_site,
            config_path=self.config_path,
            is_task_running=self._tasks_running,
        )

    def _site_from_tree_row(self, row_id: str) -> SiteConfig | None:
        values = self.site_tree.item(row_id, "values")
        if not values:
            return None
        return next(
            (site for site in self.sites if site.name == str(values[1])),
            None,
        )

    def _site_column_key(self, column_id: str) -> str:
        try:
            index = int(column_id[1:]) - 1
            if index < 0:
                return ""
            return self.site_tree["columns"][index]
        except (IndexError, TypeError, ValueError):
            return ""

    def _interaction_busy_reason(self) -> str:
        if self._tasks_running():
            return "发布任务运行中，暂不能快速修改站点"
        if self._scan_pending:
            return "站点扫描中，暂不能快速修改站点"
        if self._config_center_open:
            return "配置中心已打开，暂不能快速修改站点"
        return ""

    def _site_enable_block_reason(self, site: SiteConfig) -> str:
        reasons: list[str] = []
        if not is_valid_site_url(site.url):
            reasons.append("站点网址无效")
        if not is_valid_publish_endpoint(site.jiekou):
            reasons.append("发布接口无效")
        if site.fid <= 0:
            reasons.append("未配置默认栏目")
        gallery_reason = self._gallery_block_reason(site)
        if gallery_reason:
            reasons.append(gallery_reason)
        return "、".join(reasons)

    def _handle_site_double_click(self, event) -> None:
        if self.site_tree.identify_region(event.x, event.y) != "cell":
            return
        row_id = self.site_tree.identify_row(event.y)
        column_id = self.site_tree.identify_column(event.x)
        if not row_id or not column_id:
            return
        column_key = self._site_column_key(column_id)
        site = self._site_from_tree_row(row_id)
        if site is None:
            return
        self.site_tree.focus(row_id)
        if row_id not in self.site_tree.selection():
            self.site_tree.selection_set(row_id)
        self._render_site_details(site.name)
        if column_key in {"name", "gallery"}:
            self._open_site_directory(site, column_key)
            return
        if column_key not in {"published_limit", "enabled", "interval"}:
            return
        busy_reason = self._interaction_busy_reason()
        if busy_reason:
            self.status_var.set(busy_reason)
            return
        if column_key == "enabled":
            requested = not site.enabled
            if requested:
                block_reason = self._site_enable_block_reason(site)
                if block_reason:
                    message = f"无法启用 {site.name}：{block_reason}"
                    self.status_var.set(message)
                    messagebox.showerror("无法启用站点", message, parent=self.root)
                    return
            self._commit_quick_site_update(site.name, enabled=requested)
            return

        field = (
            "daily_publish_limit"
            if column_key == "published_limit"
            else "interval_time"
        )
        current = getattr(site, field)
        label = "每日发布上限" if field == "daily_publish_limit" else "发布间隔（秒）"
        value = simpledialog.askinteger(
            f"修改{label}",
            f"{site.name} 的{label}：",
            parent=self.root,
            initialvalue=current,
            minvalue=0,
        )
        if value is None:
            return
        busy_reason = self._interaction_busy_reason()
        if busy_reason:
            self.status_var.set(busy_reason)
            return
        self._commit_quick_site_update(site.name, **{field: value})

    def _open_site_directory_from_tree(self, event) -> None:
        self._handle_site_double_click(event)

    def _open_site_directory(self, site: SiteConfig, column_key: str) -> None:
        label = "目录"
        try:
            if column_key == "name":
                target = site.publish_dir
                target.mkdir(parents=True, exist_ok=True)
                label = "文章目录"
            else:
                if not site.gallery_id:
                    self.status_var.set("该站点未配置图库")
                    return
                gallery = self.gallery_service.require_gallery(site.gallery_id)
                target = gallery.file_path.parent
                if not target.is_dir():
                    self.status_var.set("图库目录不存在")
                    return
                label = "图库目录"
            subprocess.Popen(["explorer", str(target)])
        except (GalleryError, OSError) as exc:
            self.status_var.set(f"无法打开{label}: {exc}")

    def _commit_quick_site_update(self, site_name: str, **changes) -> bool:
        busy_reason = self._interaction_busy_reason()
        if busy_reason:
            self.status_var.set(busy_reason)
            return False
        site = next((item for item in self.sites if item.name == site_name), None)
        if site is None:
            self.status_var.set("站点不存在，配置未修改")
            return False
        candidate = replace(site, **changes)
        updated_sites = [candidate if item.name == site_name else item for item in self.sites]
        try:
            save_config(self.config_path, updated_sites)
        except (ConfigLoadError, OSError, ValueError) as exc:
            message = f"保存失败：{exc}"
            self.status_var.set(message)
            messagebox.showerror("保存失败", message, parent=self.root)
            return False

        self.sites = updated_sites
        if not self._revalidate_automation_configuration():
            self._render_site_table()
            self._render_plan_table()
            self._render_site_details(site_name)
            self.status_var.set(self.config_error_message)
            return True

        current_site = next(
            (item for item in self.sites if item.name == site_name),
            candidate,
        )
        summary = self.site_summaries.get(site_name)
        if summary is not None:
            self.site_summaries[site_name] = replace(
                summary,
                daily_limit=current_site.daily_publish_limit,
                remaining_quota=max(
                    current_site.daily_publish_limit - summary.published_today,
                    0,
                ),
            )
        self.daily_plan = build_daily_plan(self.sites, self.site_summaries)
        self._render_site_table()
        self._render_plan_table()
        self._render_site_details(site_name)
        if "enabled" in changes:
            message = "已启用自动发布" if current_site.enabled else "已停用自动发布"
        elif "daily_publish_limit" in changes:
            message = f"每日发布上限已修改为 {current_site.daily_publish_limit}"
        else:
            message = f"发布间隔已修改为 {current_site.interval_time} 秒"
        self.status_var.set(f"{site_name}：{message}")
        return True

    def _append_sites(self, sites: list[SiteConfig]) -> bool:
        updated = self.sites + list(sites)
        save_config(self.config_path, updated)
        self.sites = updated
        self._refresh_after_config_commit("配置已保存")
        return True

    def _apply_saved_sites(self, sites: list[SiteConfig]) -> None:
        self.sites = list(sites)
        self._refresh_after_config_commit("配置已保存")

    def _render_committed_sites(self) -> None:
        site_names = {site.name for site in self.sites}
        if self.current_site_name not in site_names:
            self.current_site_name = self.sites[0].name if self.sites else None
        self.daily_plan = build_daily_plan(self.sites, self.site_summaries)
        self._render_site_table()
        self._render_plan_table()
        if self.current_site_name:
            self._render_site_details(self.current_site_name)
        else:
            for detail_var in self.detail_vars.values():
                detail_var.set("-")
        self._reload_sitegroup_windows()

    def _refresh_after_config_commit(self, success_message: str) -> None:
        configuration_valid = self._revalidate_automation_configuration()
        self._render_committed_sites()
        if not configuration_valid:
            self.status_var.set(self.config_error_message)
            return
        if self._scan_pending:
            self._scan_generation += 1
            self._refresh_requested = True
            self.status_var.set(f"{success_message}，当前扫描完成后刷新")
            return
        try:
            self.refresh_data()
        except Exception as exc:
            self.logger.warning("配置已保存，但刷新失败: %s", exc)
            self.status_var.set(f"配置已保存，但刷新失败: {exc}")
            return
        self.status_var.set(success_message)

    def _revalidate_automation_configuration(self) -> bool:
        try:
            changed = migrate_site_metadata(
                self.config_path,
                self.sites,
                self.gallery_service,
            )
            if changed:
                self.sites = load_config(self.config_path)
            self.gallery_service.list_galleries()
            for site in self.sites:
                if site.site_id:
                    self.archive_service.migrate_site_identity(
                        site.name,
                        site.site_id,
                    )
        except (ConfigLoadError, GalleryError, OSError, ValueError) as exc:
            self.config_error_message = f"自动发布配置仍不可用: {exc}"
            self.automation_config_blocked = True
            return False
        self.config_error_message = ""
        self.automation_config_blocked = False
        return True

    def open_log_directory(self) -> None:
        subprocess.Popen(["explorer", str(self.config_path.parent)])

    @staticmethod
    def _compact_text(value: str, max_len: int) -> str:
        text = (value or "").strip()
        if len(text) <= max_len:
            return text
        return text[: max_len - 1] + "…"

    def _load_window_geometry(self) -> None:
        if not self.window_state_file.exists():
            self.root.geometry("1480x860")
            return
        try:
            payload = json.loads(self.window_state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            self.root.geometry("1480x860")
            return
        geometry = payload.get("geometry", "").strip()
        if geometry:
            self.root.geometry(geometry)
            self._last_window_geometry = geometry
        else:
            self.root.geometry("1480x860")

    def _schedule_window_state_save(self, _event=None) -> None:
        if self._window_state_job is not None:
            self.root.after_cancel(self._window_state_job)
        self._window_state_job = self.root.after(250, self._save_window_geometry)

    def _save_window_geometry(self) -> None:
        self._window_state_job = None
        geometry = self.root.winfo_geometry()
        if not geometry:
            return
        if geometry == self._last_window_geometry:
            return
        payload = {"geometry": geometry}
        self.window_state_file.parent.mkdir(parents=True, exist_ok=True)
        self.window_state_file.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
        self._last_window_geometry = geometry

    def _handle_site_tree_motion(self, event) -> None:
        column_key = self._site_column_key(self.site_tree.identify_column(event.x))
        self.site_tree.configure(
            cursor="hand2"
            if column_key
            in {"name", "gallery", "published_limit", "enabled", "interval"}
            else ""
        )
        self._handle_tree_hover(event, self.site_tree)

    def _handle_site_tree_leave(self, _event=None) -> None:
        self.site_tree.configure(cursor="")
        self._hide_tooltip()

    def _handle_tree_hover(self, event, tree) -> None:
        row_id = tree.identify_row(event.y)
        column_id = tree.identify_column(event.x)
        if not row_id or not column_id:
            self._hide_tooltip()
            return
        text = self._tooltip_text_for_tree(tree, row_id, column_id)
        if not text:
            self._hide_tooltip()
            return
        self._show_tooltip(event.x_root + 12, event.y_root + 12, text)

    def _tooltip_text_for_tree(self, tree, row_id: str, column_id: str) -> str:
        values = tree.item(row_id, "values")
        if not values:
            return ""
        if tree is self.site_tree:
            key = self._site_column_key(column_id)
            try:
                value = str(values[int(column_id[1:]) - 1])
            except (IndexError, TypeError, ValueError):
                return ""
            instructions = {
                "name": "双击打开文章目录",
                "published_limit": "双击修改每日发布上限",
                "enabled": "双击启用或停用自动发布",
                "interval": "双击修改发布间隔",
                "gallery": "双击打开图库目录",
            }
            instruction = instructions.get(key, "")
            if instruction:
                return f"{value}\n{instruction}"
            if key in {"category", "times", "inventory"}:
                return value
            return ""
        if tree is self.plan_tree:
            columns = tree["columns"]
            key = columns[int(column_id[1:]) - 1]
            if key == "summary":
                return str(values[int(column_id[1:]) - 1])
        return ""

    def _handle_detail_hover(self, event, key: str) -> None:
        text = self._tooltip_text_for_detail(key)
        if not text:
            self._hide_tooltip()
            return
        self._show_tooltip(event.x_root + 12, event.y_root + 12, text)

    def _tooltip_text_for_detail(self, key: str) -> str:
        text = self.detail_vars[key].get().strip()
        if not text or text == "-":
            return ""
        return text

    def _show_tooltip(self, x: int, y: int, text: str) -> None:
        if not text:
            self._hide_tooltip()
            return
        if self._tooltip_window is None or not self._tooltip_window.winfo_exists():
            self._tooltip_window = tk.Toplevel(self.root)
            self._tooltip_window.wm_overrideredirect(True)
            self._tooltip_window.attributes("-topmost", True)
            self._tooltip_label = tk.Label(
                self._tooltip_window,
                background="#0f172a",
                foreground="#f8fafc",
                relief="solid",
                borderwidth=1,
                padx=8,
                pady=4,
                justify=tk.LEFT,
                wraplength=320,
            )
            self._tooltip_label.pack()
        self._tooltip_label.configure(text=text)
        self._tooltip_window.geometry(f"+{x}+{y}")
        self._tooltip_window.deiconify()

    def _hide_tooltip(self) -> None:
        if self._tooltip_window is not None and self._tooltip_window.winfo_exists():
            self._tooltip_window.withdraw()

    def on_close(self) -> None:
        if self.closing:
            return
        if not self._confirm_sitegroup_close():
            return
        if self.task_manager.worker and self.task_manager.worker.is_alive():
            if not messagebox.askyesno("确认退出", "当前仍有任务在运行，确定退出吗？", parent=self.root):
                return
            self.closing = True
            self._shutdown_sitegroup()
            self.task_manager.stop()
            self.root.after(50, self._finish_close_when_idle)
            return
        self.closing = True
        self._shutdown_sitegroup()
        self._finish_close_when_idle()

    def _finish_close_when_idle(self) -> None:
        if self._close_completed:
            return
        if self.task_manager.worker and self.task_manager.worker.is_alive():
            self.root.after(50, self._finish_close_when_idle)
            return
        with self._scan_threads_lock:
            scan_running = any(thread.is_alive() for thread in self._scan_threads)
        if scan_running:
            self.root.after(50, self._finish_close_when_idle)
            return
        self._close_completed = True
        try:
            self._save_window_geometry()
        except Exception as exc:
            self.logger.warning("窗口状态保存失败: %s", exc)
        finally:
            self.root.destroy()

    def _runtime_status_for_plan(self, plan: PlannedRun) -> str:
        site = next((item for item in self.sites if item.name == plan.site_name), None)
        if site is None or not site.site_id:
            return "已阻断"
        today = datetime.now().strftime("%Y-%m-%d")
        if site.activation_date and site.activation_date > today:
            return f"{site.activation_date} 启用"
        batch = self.automation_batches.get(
            AutomationStateStore.batch_key(today, site.site_id)
        )
        if batch is not None:
            labels = {
                "pending": "待执行",
                "queued": "排队中",
                "running": "运行中",
                "retry_wait": "等待重试",
                "blocked": "已阻断",
                "stopped": "已停止",
                "completed": "已完成",
            }
            label = labels.get(batch.status, batch.status)
            if batch.status == "running" and batch.started_at:
                return f"{label} {batch.started_at[11:16]}"
            return label
        current_time = datetime.now().strftime("%H:%M:%S")
        if plan.scheduled_time <= current_time:
            return "待触发"
        return "待执行"

    def _collect_due_sites(self, today: str, now: datetime) -> list[SiteConfig]:
        current_time = now.strftime("%H:%M:%S")
        due_sites: list[SiteConfig] = []
        seen: set[str] = set()
        for plan in self.daily_plan:
            site = next(
                (
                    item
                    for item in self.sites
                    if item.name == plan.site_name and item.enabled
                ),
                None,
            )
            if site is None or not site.site_id or site.site_id in seen:
                continue
            seen.add(site.site_id)
            if site.activation_date and site.activation_date > today:
                continue
            key = AutomationStateStore.batch_key(today, site.site_id)
            batch = self.automation_batches.get(key)
            if batch is not None and batch.status in {"completed", "stopped"}:
                continue
            if batch is not None and batch.status == "blocked":
                fingerprint = self._site_config_fingerprint(site)
                if batch.stop_code == "gallery_invalid":
                    if self._gallery_block_reason(site):
                        continue
                    batch = self.automation_store.transition(
                        today,
                        site.site_id,
                        "queued",
                        stop_code="",
                        stop_reason="图库已修复，等待重新扫描",
                        config_fingerprint=fingerprint,
                        finished_at="",
                    )
                    self.automation_batches[key] = batch
                elif batch.stop_code == "scan_error":
                    pass
                elif batch.stop_code in {"configuration", "configuration_blocked"}:
                    if (
                        batch.config_fingerprint == fingerprint
                        or site.fid <= 0
                        or self._gallery_block_reason(site)
                    ):
                        continue
                    batch = self.automation_store.transition(
                        today,
                        site.site_id,
                        "queued",
                        stop_code="",
                        stop_reason="配置已修复，等待重新扫描",
                        config_fingerprint=fingerprint,
                        finished_at="",
                    )
                    self.automation_batches[key] = batch
                else:
                    continue
            if batch is not None and batch.status == "retry_wait":
                try:
                    retry_due = datetime.fromisoformat(batch.next_retry_at) <= now
                except ValueError:
                    retry_due = True
                if not retry_due:
                    continue
            elif batch is None and plan.scheduled_time > current_time:
                continue
            due_sites.append(site)
        return due_sites

    def _schedule_tick(self, schedule_next: bool = True) -> None:
        if self.closing:
            return
        if self.automation_config_blocked:
            self.status_var.set(self.config_error_message)
            if schedule_next:
                self.root.after(15000, self._schedule_tick)
            return
        today = datetime.now().strftime("%Y-%m-%d")
        rollover_pending = today != self.last_schedule_date
        if getattr(self, "_manual_workflow_active", False):
            if rollover_pending:
                self._queue_rollover_refresh(today, "手动发布准备中")
            if schedule_next:
                self.root.after(15000, self._schedule_tick)
            return
        if getattr(self, "_scan_pending", 0):
            if rollover_pending:
                self._queue_rollover_refresh(today, "站点扫描中")
            else:
                self.status_var.set("站点扫描中，自动发布将在下次检查")
            if schedule_next:
                self.root.after(15000, self._schedule_tick)
            return
        if self._tasks_running():
            if rollover_pending:
                self._queue_rollover_refresh(today, "发布任务正在执行")
            if schedule_next:
                self.root.after(15000, self._schedule_tick)
            return
        if rollover_pending:
            self._pending_rollover_date = today
            self._refresh_requested = True
            self.refresh_data()
            if schedule_next:
                self.root.after(15000, self._schedule_tick)
            return
        try:
            self.automation_batches = self.automation_store.load()
            self.automation_state_error = ""
        except StateRecoveryRequired as exc:
            self.automation_batches = {}
            self.automation_state_error = str(exc)
            self.status_var.set("自动化状态待恢复，正在执行全站扫描...")
            self.refresh_data()
            if schedule_next:
                self.root.after(15000, self._schedule_tick)
            return
        except (OSError, StateFileError) as exc:
            self.automation_state_error = str(exc)
            self.status_var.set(f"自动化状态不可用，将在下次重试: {exc}")
            if schedule_next:
                self.root.after(15000, self._schedule_tick)
            return
        if not (self.task_manager.worker and self.task_manager.worker.is_alive()):
            now = datetime.now()
            try:
                due_sites = self._collect_due_sites(today, now)
            except StateRecoveryRequired as exc:
                self.automation_state_error = str(exc)
                self.refresh_data()
                due_sites = []
            except (OSError, StateFileError) as exc:
                self.automation_state_error = str(exc)
                self.status_var.set(f"自动化状态保存失败: {exc}")
                due_sites = []
            scan_targets = [
                site
                for site in due_sites
                if AutomationStateStore.batch_key(today, site.site_id)
                not in self._fresh_due_scan_ready
                and AutomationStateStore.batch_key(today, site.site_id)
                not in self._due_scan_keys
            ]
            if scan_targets:
                for site in scan_targets:
                    self._due_scan_keys.add(
                        AutomationStateStore.batch_key(today, site.site_id)
                    )
                self.status_var.set(
                    "正在扫描到期站点的最新文章: "
                    + ", ".join(site.name for site in scan_targets)
                )
                self._request_scan(
                    scan_targets,
                    reason="due",
                    due_site_ids=tuple(site.site_id for site in scan_targets),
                    run_date=today,
                )
            elif due_sites and all(
                AutomationStateStore.batch_key(today, site.site_id)
                in self._fresh_due_scan_ready
                for site in due_sites
            ):
                try:
                    self._build_and_run_due_sites(today, due_sites)
                except StateRecoveryRequired as exc:
                    self.automation_state_error = str(exc)
                    self.refresh_data()
                except (OSError, StateFileError) as exc:
                    self.automation_state_error = str(exc)
                    self.status_var.set(f"自动化状态保存失败: {exc}")
        if schedule_next:
            self.root.after(15000, self._schedule_tick)

    def _build_and_run_due_sites(
        self,
        today: str,
        due_sites: list[SiteConfig],
    ) -> None:
        tasks: list[tuple[SiteConfig, ArticleRecord]] = []
        batch_context: dict[str, tuple[str, str]] = {}
        for site in due_sites:
            key = AutomationStateStore.batch_key(today, site.site_id)
            self._fresh_due_scan_ready.discard(key)
            summary = self.site_summaries.get(site.name)
            if summary is None or summary.status == "error":
                message = (
                    summary.last_error
                    if summary is not None and summary.last_error
                    else "站点扫描失败"
                )
                blocked = self.automation_store.transition(
                    today,
                    site.site_id,
                    "blocked",
                    planned_count=0,
                    stop_code="scan_error",
                    stop_reason=message,
                    config_fingerprint=self._site_config_fingerprint(site),
                )
                self.automation_batches[key] = blocked
                self._handle_site_complete_ui(site, [], blocked)
                continue

            batch = self.automation_batches.get(key)
            if batch is not None and batch.status == "blocked" and batch.stop_code == "scan_error":
                batch = self.automation_store.transition(
                    today,
                    site.site_id,
                    "queued",
                    stop_code="",
                    stop_reason="扫描已恢复，等待构建任务",
                    finished_at="",
                )
                self.automation_batches[key] = batch

            gallery_block = self._gallery_block_reason(site)
            if gallery_block:
                blocked = self.automation_store.transition(
                    today,
                    site.site_id,
                    "blocked",
                    planned_count=0,
                    stop_code="gallery_invalid",
                    stop_reason=gallery_block,
                    config_fingerprint=self._site_config_fingerprint(site),
                )
                self.automation_batches[key] = blocked
                self._handle_site_complete_ui(site, [], blocked)
                continue

            site_tasks = self.task_manager.build_tasks(
                [site],
                {site.name: self.articles_by_site.get(site.name, [])},
            )
            diagnostic = self.task_manager.last_build_diagnostics.get(
                site.name,
                TaskBuildDiagnostic("empty", "没有可执行任务"),
            )
            queued = self.automation_store.transition(
                today,
                site.site_id,
                "queued",
                planned_count=len(site_tasks),
                stop_reason="",
                stop_code="",
                config_fingerprint=self._site_config_fingerprint(site),
                finished_at="",
            )
            self.automation_batches[key] = queued
            if not site_tasks:
                safe_empty = diagnostic.code in {"empty", "quota_exhausted"}
                if safe_empty:
                    try:
                        self._record_inventory(
                            site,
                            today,
                            site.site_id,
                            "zero_tasks",
                        )
                    except Exception as exc:
                        completed = self.automation_store.transition(
                            today,
                            site.site_id,
                            "blocked",
                            stop_code="scan_error",
                            stop_reason=f"库存检查失败: {exc}",
                            next_retry_at="",
                        )
                    else:
                        completed = self.automation_store.transition(
                            today,
                            site.site_id,
                            "completed",
                            stop_code=diagnostic.code,
                            stop_reason=diagnostic.message,
                            next_retry_at="",
                        )
                else:
                    completed = self.automation_store.transition(
                        today,
                        site.site_id,
                        "blocked",
                        stop_code=diagnostic.code,
                        stop_reason=diagnostic.message,
                        next_retry_at="",
                    )
                self.automation_batches[key] = completed
                self._handle_site_complete_ui(site, [], completed)
                continue
            tasks.extend(site_tasks)
            batch_context[site.site_id] = (today, site.site_id)

        if tasks and not self.automation_state_error:
            self._run_tasks(
                tasks,
                f"自动批次执行: {', '.join(sorted({site.name for site, _article in tasks}))}",
                batch_context=batch_context,
            )
            if self.content_notebook is not None and self.plan_tab is not None:
                self.content_notebook.select(self.plan_tab)


def run_app(config_path: str | Path = "config.ini") -> None:
    root = tk.Tk()
    try:
        with ConfigInstanceLock(config_path):
            window = MainWindow(root, config_path=config_path)
            root.protocol("WM_DELETE_WINDOW", window.on_close)
            root.mainloop()
    except InstanceAlreadyRunningError as exc:
        root.withdraw()
        messagebox.showerror("程序已在运行", str(exc), parent=root)
        root.destroy()
