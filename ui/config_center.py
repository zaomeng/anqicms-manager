from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from core.admin_jump_service import AdminJumpService
from core.anqicms_mcp_client import AnqiMcpClient, probe_site
from core.category_service import (
    Category,
    CategoryCache,
    CategoryService,
    CategoryServiceError,
)
from core.config_service import ConfigLoadError, SITE_GROUP_MAX_LENGTH, save_config
from core.gallery_service import GalleryError, GalleryService
from core.integration_settings import (
    IntegrationSettings,
    load_integration_settings,
    settings_path,
)
from core.schedule_service import is_valid_publish_time, normalize_publish_time
from core.site_credentials import DEFAULT_ADMIN_PATH, normalize_admin_path
from core.site_factory import parse_publish_endpoint, site_url_identity
from ui.admin_jump_actions import open_admin_backends
from ui.bulk_sitegroup_dialog import BulkSiteGroupDialog
from ui.category_picker import CategoryPicker
from ui.new_site_dialog import NewSiteDialog
from ui.site_credentials_panel import SiteCredentialsFrame, format_probe_summary
from ui.time_picker import TimePicker
from ui.ui_async import BackgroundRunner


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


_SITEGROUP_TEXT_LABELS = (
    ("site_title", "站点显示名"),
    ("site_group", "站点分组"),
    ("mcp_token", "MCP Token"),
    ("admin_username", "后台账号"),
    ("admin_password", "后台密码"),
)


class ConfigCenter(tk.Toplevel):
    def __init__(
        self,
        master,
        config_path,
        sites,
        on_sites_updated,
        open_schedule_editor=None,
        open_new_site_dialog=None,
        *,
        category_service: CategoryService | None = None,
        category_cache: CategoryCache | None = None,
        gallery_service: GalleryService | None = None,
        is_task_running=None,
        jump_service=None,
        integration_settings: IntegrationSettings | None = None,
        probe_fn=None,
    ):
        super().__init__(master)
        self.title("站点管理")
        self.geometry("1180x860")
        self.minsize(980, 740)
        self.transient(master)
        self.config_path = Path(config_path).resolve()
        self.sites = sites
        self.on_sites_updated = on_sites_updated
        # Kept for callers on older main-window code. There is no visible schedule action.
        self.open_schedule_editor = open_schedule_editor
        self.open_new_site_dialog = open_new_site_dialog
        self.category_service = category_service or CategoryService()
        self.category_cache = category_cache or CategoryCache(
            self.config_path.parent / "设置" / "category_cache.json"
        )
        self.gallery_service = gallery_service or GalleryService(self.config_path.parent / "设置")
        self.is_task_running = is_task_running or (lambda: False)
        self.integration_settings = (
            integration_settings
            if integration_settings is not None
            else load_integration_settings(settings_path(self.config_path))
        )
        self._owns_jump_service = jump_service is None
        self.jump_service = jump_service
        self._probe_fn = probe_fn or probe_site
        self._sitegroup_runner = BackgroundRunner(self)
        self.credentials_frame: SiteCredentialsFrame | None = None
        self.rows: list[dict[str, object]] = []
        self._automation_disabled_sites: list[str] = []
        self._gallery_by_name = {}
        self._gallery_name_by_id: dict[str, str] = {}
        self._category_generation = 0
        self._category_after_job = None
        self._category_poll_job = None
        self._category_pending = 0
        self._category_results: queue.SimpleQueue = queue.SimpleQueue()
        self.current_index = -1
        self.site_tree = None
        self.editor_entries: dict[str, ttk.Entry] = {}
        self.enabled_check = None
        self.cancel_button = None
        self.category_picker = None
        self.gallery_box = None
        self.time_picker = None
        self.empty_label = None
        self.editor_frame = None
        self.message_var = tk.StringVar(value="选择站点后可编辑完整接口和自动发布参数")
        self.detail_vars = {
            "publish_dir": tk.StringVar(value="-"),
            "daily_publish_filename": tk.StringVar(value="-"),
        }
        self._create_row_drafts()
        self._build()
        self.bind("<Control-s>", lambda _event: self._save_all())
        self.bind("<Escape>", lambda _event: self._request_close())
        self.protocol("WM_DELETE_WINDOW", self._request_close)

    @staticmethod
    def _site_full_endpoint(site) -> str:
        if site.url and site.jiekou:
            return f"{site.url.rstrip('/')}{site.jiekou}"
        return site.url or site.jiekou

    def _create_row_drafts(self) -> None:
        self.rows = []
        for site in self.sites:
            full_endpoint = self._site_full_endpoint(site)
            endpoint_var = tk.StringVar(value=full_endpoint)
            cached_categories = (
                self.category_cache.get(full_endpoint) if full_endpoint else None
            )
            self.rows.append(
                {
                    "selected": tk.BooleanVar(value=False),
                    "name": tk.StringVar(value=site.name),
                    "url": tk.StringVar(value=site.url),
                    "jiekou": tk.StringVar(value=site.jiekou),
                    "full_endpoint": endpoint_var,
                    "full_endpoint_display": endpoint_var,
                    # Compatibility alias for tests/extensions that inspected the old field.
                    "jiekou_display": endpoint_var,
                    "fid": tk.StringVar(value=str(site.fid) if site.fid > 0 else ""),
                    "category_name": tk.StringVar(value=site.category_name),
                    "category_fetch_state": (
                        "success" if cached_categories is not None else "idle"
                    ),
                    "category_fetch_endpoint": (
                        full_endpoint if cached_categories is not None else ""
                    ),
                    "category_ids": {
                        category.id for category in (cached_categories or [])
                    },
                    "categories": list(cached_categories or []),
                    "category_request_id": 0,
                    "category_keywords": dict(site.category_keywords),
                    "interval_time": tk.StringVar(value=str(site.interval_time)),
                    "daily_publish_limit": tk.StringVar(value=str(site.daily_publish_limit)),
                    "article_warning_threshold": tk.StringVar(value=str(site.article_warning_threshold)),
                    "publish_time": tk.StringVar(value=site.publish_time or "06:00:00"),
                    "enabled": tk.BooleanVar(value=site.enabled),
                    "publish_dir": tk.StringVar(value=str(site.publish_dir)),
                    "gallery_id": tk.StringVar(value=site.gallery_id),
                    "image_file": tk.StringVar(value="" if site.image_file is None else str(site.image_file)),
                    "site_title": tk.StringVar(value=site.site_title),
                    "site_group": tk.StringVar(value=site.site_group),
                    "mcp_token": tk.StringVar(value=site.mcp_token),
                    "admin_username": tk.StringVar(value=site.admin_username),
                    "admin_password": tk.StringVar(value=site.admin_password),
                    "admin_path": tk.StringVar(value=site.admin_path or DEFAULT_ADMIN_PATH),
                }
            )

    def _build(self) -> None:
        container = ttk.Frame(self, padding=14)
        container.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(container)
        header.pack(fill=tk.X, pady=(0, 12))
        heading = ttk.Frame(header)
        heading.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(heading, text="站点管理", style="Metric.TLabel").pack(anchor="w")
        ttk.Label(
            heading,
            text="一个完整接口完成连接配置；栏目、图库和发布时间在同一处保存。",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(2, 0))
        ttk.Button(header, text="保存更改", command=self._save_all, style="Accent.TButton").pack(side=tk.RIGHT)
        self.cancel_button = ttk.Button(header, text="取消", command=self._request_close)
        self.cancel_button.pack(side=tk.RIGHT, padx=(0, 8))

        toolbar = ttk.Frame(container)
        toolbar.pack(fill=tk.X, pady=(0, 10))
        for label, command in [
            ("新增站点", self._open_new_site),
            ("复制当前站点", self._clone_selected_site),
            ("删除选中站点", self._delete_selected_sites),
            ("批量每日上限", self._batch_edit_daily_limit),
            ("批量停发", lambda: self._batch_set_enabled(False)),
            ("批量启用", lambda: self._batch_set_enabled(True)),
            ("批量配置站群", self._open_bulk_sitegroup_dialog),
            ("跳转后台", self._open_selected_backends),
            ("测试 MCP", self._test_current_mcp),
        ]:
            ttk.Button(toolbar, text=label, command=command).pack(side=tk.LEFT, padx=(0, 7))

        panes = ttk.Panedwindow(container, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True)
        list_panel = ttk.LabelFrame(panes, text="站点列表", padding=8)
        editor_panel = ttk.Frame(panes)
        panes.add(list_panel, weight=1)
        panes.add(editor_panel, weight=2)
        self.after_idle(lambda: panes.sashpos(0, 370))

        self.site_tree = ttk.Treeview(
            list_panel,
            columns=("name", "url", "enabled"),
            show="headings",
            selectmode="extended",
        )
        for key, label, width in [
            ("name", "站点", 120),
            ("url", "主网址", 200),
            ("enabled", "状态", 58),
        ]:
            self.site_tree.heading(key, text=label)
            self.site_tree.column(key, width=width, minwidth=45, anchor=tk.W, stretch=key == "url")
        tree_scroll = ttk.Scrollbar(list_panel, orient=tk.VERTICAL, command=self.site_tree.yview)
        self.site_tree.configure(yscrollcommand=tree_scroll.set)
        self.site_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.site_tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        self.editor_frame = ttk.LabelFrame(editor_panel, text="当前站点", padding=12)
        self.editor_frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(self.editor_frame, text="站点标识").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=5)
        name_entry = ttk.Entry(self.editor_frame, state="readonly")
        name_entry.grid(row=0, column=1, sticky="ew", pady=5, ipady=2)
        self.editor_entries["name"] = name_entry

        ttk.Label(self.editor_frame, text="完整发布接口").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=5)
        endpoint_entry = ttk.Entry(self.editor_frame)
        endpoint_entry.grid(row=1, column=1, sticky="ew", pady=5, ipady=2)
        endpoint_entry.bind("<KeyRelease>", self._endpoint_changed)
        self.editor_entries["full_endpoint_display"] = endpoint_entry
        self.editor_entries["jiekou_display"] = endpoint_entry
        endpoint_actions = ttk.Frame(self.editor_frame)
        endpoint_actions.grid(row=1, column=2, sticky="w", padx=(8, 0), pady=5)
        ttk.Button(endpoint_actions, text="刷新栏目", command=self._fetch_categories).pack(side=tk.LEFT)

        ttk.Label(self.editor_frame, text="发布栏目").grid(row=2, column=0, sticky="nw", padx=(0, 12), pady=5)
        self.category_picker = CategoryPicker(
            self.editor_frame,
            height=4,
            on_keywords_changed=self._category_keywords_changed,
            on_error=self.message_var.set,
        )
        self.category_picker.grid(row=2, column=1, columnspan=2, sticky="nsew", pady=5)
        self.category_picker.tree.bind(
            "<<TreeviewSelect>>",
            self._category_selected,
            add="+",
        )

        ttk.Label(self.editor_frame, text="插图图库").grid(row=3, column=0, sticky="w", padx=(0, 12), pady=5)
        self.gallery_box = ttk.Combobox(self.editor_frame, state="readonly")
        self.gallery_box.grid(row=3, column=1, sticky="ew", pady=5)
        self.gallery_box.bind("<<ComboboxSelected>>", self._gallery_selected)

        ttk.Label(self.editor_frame, text="每日启动时间").grid(row=4, column=0, sticky="w", padx=(0, 12), pady=5)
        self.time_picker = TimePicker(self.editor_frame, "06:00:00")
        self.time_picker.grid(row=4, column=1, sticky="w", pady=5)

        numeric_fields = [
            ("发布间隔（秒）", "interval_time"),
            ("每日上限（篇）", "daily_publish_limit"),
            ("库存提醒（篇）", "article_warning_threshold"),
        ]
        for offset, (label, key) in enumerate(numeric_fields, start=5):
            ttk.Label(self.editor_frame, text=label).grid(row=offset, column=0, sticky="w", padx=(0, 12), pady=5)
            entry = ttk.Entry(self.editor_frame, width=12)
            entry.grid(row=offset, column=1, sticky="w", pady=5, ipady=2)
            self.editor_entries[key] = entry

        ttk.Label(self.editor_frame, text="自动发布").grid(row=8, column=0, sticky="w", padx=(0, 12), pady=5)
        self.enabled_check = ttk.Checkbutton(self.editor_frame, text="参与每日扫描和自动发布")
        self.enabled_check.grid(row=8, column=1, sticky="w", pady=5)

        ttk.Label(self.editor_frame, text="文章目录").grid(row=9, column=0, sticky="w", padx=(0, 12), pady=5)
        publish_dir_entry = ttk.Entry(self.editor_frame)
        publish_dir_entry.grid(row=9, column=1, sticky="ew", pady=5, ipady=2)
        self.editor_entries["publish_dir"] = publish_dir_entry
        ttk.Button(self.editor_frame, text="选择...", command=self._choose_publish_dir).grid(
            row=9, column=2, sticky="w", padx=(8, 0), pady=5
        )

        details = ttk.LabelFrame(self.editor_frame, text="本地状态文件", padding=8)
        details.grid(row=10, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Label(details, text="计数文件", style="Muted.TLabel").grid(
            row=0, column=0, sticky="nw", padx=(0, 12), pady=3
        )
        ttk.Label(
            details,
            textvariable=self.detail_vars["daily_publish_filename"],
            justify=tk.LEFT,
            wraplength=560,
        ).grid(row=0, column=1, sticky="w", pady=3)
        details.columnconfigure(1, weight=1)
        self.credentials_frame = SiteCredentialsFrame(
            self.editor_frame,
            on_test_mcp=self._test_current_mcp,
            on_open_admin=self._open_current_backend,
        )
        self.credentials_frame.grid(row=11, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.editor_frame.columnconfigure(1, weight=1)
        self.editor_frame.rowconfigure(2, weight=1)

        ttk.Label(container, textvariable=self.message_var, style="Muted.TLabel").pack(fill=tk.X, pady=(10, 0))
        self._reload_galleries()
        self._render_site_list()
        if self.sites:
            self._select_site(0)
        else:
            self._set_editor_enabled(False)

    def _reload_galleries(self) -> None:
        try:
            galleries = self.gallery_service.list_galleries()
        except GalleryError as exc:
            galleries = []
            self.message_var.set(f"图库加载失败：{exc}")
        self._gallery_by_name = {gallery.name: gallery for gallery in galleries}
        self._gallery_name_by_id = {gallery.gallery_id: gallery.name for gallery in galleries}
        if self.gallery_box is not None:
            self.gallery_box.configure(values=list(self._gallery_by_name))
            if self.current_index >= 0:
                row = self.rows[self.current_index]
                self.gallery_box.set(self._gallery_name_by_id.get(row["gallery_id"].get(), ""))

    def _render_site_list(self) -> None:
        for item in self.site_tree.get_children():
            self.site_tree.delete(item)
        for index, row in enumerate(self.rows):
            self.site_tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(row["name"].get(), row["url"].get(), "启用" if row["enabled"].get() else "停用"),
            )

    def _refresh_tree_item(self, index: int) -> None:
        if index < 0 or index >= len(self.rows) or not self.site_tree.exists(str(index)):
            return
        row = self.rows[index]
        self.site_tree.item(
            str(index),
            values=(row["name"].get(), row["url"].get(), "启用" if row["enabled"].get() else "停用"),
        )

    def _on_tree_select(self, _event=None) -> None:
        selected = self.site_tree.selection()
        if selected:
            focused = self.site_tree.focus()
            active_item = focused if focused in selected else selected[-1]
            self._bind_editor(int(active_item))

    def _select_site(self, index: int) -> None:
        if index < 0 or index >= len(self.rows):
            return
        self.site_tree.selection_set(str(index))
        self.site_tree.focus(str(index))
        self.site_tree.see(str(index))
        self._bind_editor(index)

    def _restore_current_site_selection(self) -> None:
        if self.current_index < 0 or not self.site_tree.exists(str(self.current_index)):
            return
        item = str(self.current_index)
        self.site_tree.selection_set(item)
        self.site_tree.focus(item)
        self.site_tree.see(item)
        self.category_picker.refocus_pending_edit()

    def _capture_current_widgets(self) -> bool:
        if self.current_index < 0:
            return True
        if not self.category_picker.commit_pending_edit():
            return False
        row = self.rows[self.current_index]
        if self.time_picker is not None:
            row["publish_time"].set(self.time_picker.get())
        return True

    def _bind_editor(self, index: int) -> None:
        if self.current_index == index:
            return
        if self.current_index >= 0:
            if not self._capture_current_widgets():
                self._restore_current_site_selection()
                return
            self._refresh_tree_item(self.current_index)
        if self._category_after_job is not None:
            try:
                self.after_cancel(self._category_after_job)
            except tk.TclError:
                pass
            self._category_after_job = None
        self.current_index = index
        row = self.rows[index]
        for key, entry in self.editor_entries.items():
            if key == "jiekou_display":
                continue
            entry.configure(textvariable=row[key])
        self.enabled_check.configure(variable=row["enabled"])
        self.gallery_box.set(self._gallery_name_by_id.get(row["gallery_id"].get(), ""))
        self.time_picker.set(row["publish_time"].get())
        self.category_picker.clear()
        saved_category_id = (
            int(row["fid"].get()) if row["fid"].get().isdigit() else None
        )
        self.category_picker.set_default_category(
            saved_category_id,
            row["category_name"].get(),
        )
        if self.credentials_frame is not None:
            self.credentials_frame.set_group_choices(self._collect_group_choices())
            self.credentials_frame.bind_vars(row)
            self.credentials_frame.set_status("")
        self._set_editor_enabled(True)
        self._show_details(index)
        category = row["category_name"].get().strip()
        category_text = f"，当前栏目：{category} (ID {row['fid'].get()})" if row["fid"].get() else ""
        endpoint = row["full_endpoint"].get().strip()
        cached_categories = row["categories"]
        if (
            row["category_fetch_state"] == "success"
            and row["category_fetch_endpoint"] == endpoint
        ):
            selected_id = int(row["fid"].get()) if row["fid"].get().isdigit() else None
            self.category_picker.set_categories(
                cached_categories,
                selected_id=selected_id,
                category_keywords=row["category_keywords"],
                selected_title=row["category_name"].get(),
            )
            self.message_var.set(
                f"正在编辑：{row['name'].get()}{category_text}；已加载 {len(cached_categories)} 个栏目"
            )
        else:
            self.message_var.set(f"正在编辑：{row['name'].get()}{category_text}")
            if endpoint:
                self._category_after_job = self.after(0, self._fetch_categories)

    def _set_editor_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for key, entry in self.editor_entries.items():
            if key == "jiekou_display":
                continue
            entry.configure(state="readonly" if enabled and key == "name" else state)
        self.enabled_check.configure(state=state)
        self.gallery_box.configure(state="readonly" if enabled else "disabled")
        self.category_picker.set_enabled(enabled)
        self.time_picker.configure_state(enabled)
        if self.credentials_frame is not None:
            self.credentials_frame.set_enabled(enabled)

    def _show_details(self, index: int) -> None:
        if index < 0 or index >= len(self.sites):
            return
        site = self.sites[index]
        self.detail_vars["publish_dir"].set(str(site.publish_dir))
        self.detail_vars["daily_publish_filename"].set(str(site.daily_publish_filename))

    def _endpoint_changed(self, _event=None) -> None:
        if self.current_index < 0:
            return
        self._invalidate_current_category(schedule_fetch=True)

    def _invalidate_current_category(self, *, schedule_fetch: bool) -> None:
        if self.current_index < 0:
            return
        self._category_generation += 1
        row = self.rows[self.current_index]
        row["category_request_id"] = self._category_generation
        row["fid"].set("")
        row["category_name"].set("")
        row["category_fetch_state"] = "idle"
        row["category_fetch_endpoint"] = ""
        row["category_ids"] = set()
        row["categories"] = []
        self.category_picker.clear()
        if self._category_after_job is not None:
            try:
                self.after_cancel(self._category_after_job)
            except tk.TclError:
                pass
            self._category_after_job = None
        if schedule_fetch:
            self._category_after_job = self.after(600, self._fetch_categories)

    def _fetch_categories(self) -> None:
        if self.current_index < 0:
            return
        if not self.category_picker.commit_pending_edit():
            return
        self._category_after_job = None
        row_index = self.current_index
        row = self.rows[row_index]
        endpoint = row["full_endpoint"].get().strip()
        if not endpoint:
            self.message_var.set("请先输入完整发布接口")
            return
        self._category_generation += 1
        generation = self._category_generation
        row["category_request_id"] = generation
        row["category_fetch_state"] = "loading"
        self.message_var.set("正在获取栏目...")

        def worker() -> None:
            try:
                categories = self.category_service.fetch(endpoint)
                error = ""
            except (CategoryServiceError, ValueError) as exc:
                categories = []
                error = str(exc)
            except Exception:
                categories = []
                error = "栏目获取失败，请稍后重试"
            self._category_results.put((row_index, generation, endpoint, categories, error))

        self._category_pending += 1
        threading.Thread(target=worker, daemon=True, name="config-category-fetch").start()
        if self._category_poll_job is None:
            self._category_poll_job = self.after(50, self._poll_category_results)

    def _poll_category_results(self) -> None:
        self._category_poll_job = None
        while not self._category_results.empty():
            result = self._category_results.get()
            self._category_pending = max(self._category_pending - 1, 0)
            self._apply_category_result(*result)
        if self._category_pending:
            self._category_poll_job = self.after(50, self._poll_category_results)

    def _apply_category_result(
        self,
        row_index: int,
        generation: int,
        endpoint: str,
        categories: list[Category],
        error: str,
    ) -> None:
        if not self.winfo_exists() or row_index < 0 or row_index >= len(self.rows):
            return
        row = self.rows[row_index]
        if (
            generation != row["category_request_id"]
            or endpoint != row["full_endpoint"].get().strip()
        ):
            return
        is_current = row_index == self.current_index
        row["category_fetch_endpoint"] = endpoint
        if error:
            row["category_fetch_state"] = "error"
            if is_current:
                self.message_var.set(
                    f"{error}；已保留原有栏目配置，可稍后重试"
                )
            return
        if is_current and not self.category_picker.commit_pending_edit():
            row["category_fetch_state"] = "idle"
            return
        row["category_fetch_state"] = "success"
        row["category_ids"] = {category.id for category in categories}
        row["categories"] = list(categories)
        cache_error = ""
        try:
            self.category_cache.put(endpoint, categories)
        except (OSError, ValueError):
            cache_error = "栏目已获取，但自动缓存保存失败"
        row["category_keywords"] = CategoryPicker.normalize_keyword_mapping(row["category_keywords"])
        removed_keyword_ids = [
            category_id
            for category_id in row["category_keywords"]
            if category_id not in row["category_ids"]
        ]
        if removed_keyword_ids:
            row["category_keywords"] = {
                category_id: keywords
                for category_id, keywords in row["category_keywords"].items()
                if category_id in row["category_ids"]
            }
        selected_id = int(row["fid"].get()) if row["fid"].get().isdigit() else None
        if is_current:
            self.category_picker.set_categories(
                categories,
                selected_id=selected_id,
                category_keywords=row["category_keywords"],
                selected_title=row["category_name"].get(),
            )
            row["category_keywords"] = self.category_picker.keyword_mapping()
        notices = []
        if cache_error:
            notices.append(cache_error)
        if removed_keyword_ids:
            notices.append(
                "已清理不存在栏目的标题关键词：ID "
                + "、".join(str(category_id) for category_id in removed_keyword_ids)
            )
        if selected_id is not None and selected_id not in row["category_ids"]:
            notices.append(f"原栏目 ID {selected_id} 已不存在；该站点保存后将停用，请重新选择")
        else:
            selected_category = next((item for item in categories if item.id == selected_id), None)
            if selected_category is not None:
                row["category_name"].set(selected_category.title)
        if is_current:
            self.message_var.set(
                "；".join(notices)
                if notices
                else f"已获取 {len(categories)} 个栏目，请选择发布栏目"
            )

    def _category_selected(self, _event=None) -> None:
        if self.current_index < 0:
            return
        selected = self.category_picker.selected()
        if selected is None:
            return
        row = self.rows[self.current_index]
        row["fid"].set(str(selected[0]))
        row["category_name"].set(selected[1])
        self.message_var.set(f"已选择栏目：{selected[1]} (ID {selected[0]})")

    def _category_keywords_changed(self, mapping: dict[int, tuple[str, ...]]) -> None:
        if self.current_index < 0:
            return
        self.rows[self.current_index]["category_keywords"] = dict(mapping)
        self.message_var.set("标题关键词已更新，保存更改后生效")

    def _gallery_selected(self, _event=None) -> None:
        if self.current_index < 0:
            return
        gallery = self._gallery_by_name.get(self.gallery_box.get())
        row = self.rows[self.current_index]
        row["gallery_id"].set(gallery.gallery_id if gallery else "")
        row["image_file"].set(str(gallery.file_path) if gallery else "")

    def _choose_publish_dir(self) -> None:
        if self.current_index < 0:
            return
        current = self.rows[self.current_index]["publish_dir"].get().strip()
        initial_dir = current if Path(current).is_dir() else str(self.config_path.parent)
        selected = filedialog.askdirectory(parent=self, initialdir=initial_dir)
        if selected:
            self.rows[self.current_index]["publish_dir"].set(str(Path(selected).resolve()))

    def _materialize_sites(self):
        if not self._capture_current_widgets():
            return None
        self._automation_disabled_sites = []
        updated = []
        for original, row in zip(self.sites, self.rows):
            name = row["name"].get().strip()
            if not name:
                self.message_var.set("站点标识不能为空")
                return None
            publish_dir_text = row["publish_dir"].get().strip()
            if not publish_dir_text:
                self.message_var.set(f"{name}：文章目录不能为空")
                return None
            publish_dir = Path(publish_dir_text)
            if not publish_dir.is_absolute():
                publish_dir = self.config_path.parent / publish_dir
            publish_dir = publish_dir.resolve()
            try:
                interval_time = int(row["interval_time"].get().strip())
                daily_limit = int(row["daily_publish_limit"].get().strip())
                warning_threshold = int(row["article_warning_threshold"].get().strip())
            except ValueError:
                self.message_var.set(f"{name}：发布间隔、每日上限和库存提醒必须是数字")
                return None
            if any(value < 0 for value in (interval_time, daily_limit, warning_threshold)):
                self.message_var.set(f"{name}：发布间隔、每日上限和库存提醒不能为负数")
                return None

            full_endpoint = row["full_endpoint"].get().strip()
            if full_endpoint:
                try:
                    parsed = parse_publish_endpoint(full_endpoint)
                except ValueError as exc:
                    self.message_var.set(f"{name}：{exc}")
                    return None
                normalized_url = parsed.base_url
                endpoint = parsed.publish_endpoint
            else:
                normalized_url = ""
                endpoint = ""

            publish_time = row["publish_time"].get().strip()
            if not is_valid_publish_time(publish_time):
                self.message_var.set(f"{name}：发布时间无效")
                return None
            publish_time = normalize_publish_time(publish_time)

            fid_text = row["fid"].get().strip()
            fid = int(fid_text) if fid_text.isdigit() else 0
            category_name = row["category_name"].get().strip()
            category_keywords = CategoryPicker.normalize_keyword_mapping(row["category_keywords"])
            duplicate = CategoryPicker.find_duplicate_keyword(category_keywords)
            if duplicate is not None:
                keyword, first_id, second_id = duplicate
                self.message_var.set(
                    f"{name}：标题关键词“{keyword}”同时分配给栏目 ID {first_id} 和 {second_id}"
                )
                return None
            gallery_id = row["gallery_id"].get().strip()
            image_file = original.image_file
            gallery_problem = ""
            if gallery_id:
                try:
                    gallery = self.gallery_service.require_gallery(gallery_id)
                    validation = self.gallery_service.validate_gallery(gallery_id)
                except GalleryError as exc:
                    gallery_problem = str(exc)
                else:
                    if validation.valid:
                        image_file = gallery.file_path
                    else:
                        gallery_problem = validation.message

            requested_enabled = bool(row["enabled"].get())
            disabled_reasons = []
            if not full_endpoint:
                disabled_reasons.append("缺少完整发布接口")
            if fid <= 0:
                disabled_reasons.append("未选择栏目")
            if (
                row["category_fetch_state"] == "success"
                and row["category_fetch_endpoint"] == full_endpoint
                and fid not in row["category_ids"]
            ):
                disabled_reasons.append("栏目已失效")
            if not gallery_id:
                disabled_reasons.append("未选择图库")
            elif gallery_problem:
                disabled_reasons.append(f"图库不可用：{gallery_problem}")
            enabled = requested_enabled and not disabled_reasons
            if requested_enabled and disabled_reasons:
                self._automation_disabled_sites.append(f"{name}（{'、'.join(disabled_reasons)}）")

            sitegroup_fields = self._extract_sitegroup_fields(name, row)
            if sitegroup_fields is None:
                return None

            updated.append(
                replace(
                    original,
                    name=name,
                    url=normalized_url,
                    jiekou=endpoint,
                    fid=fid,
                    category_name=category_name,
                    category_keywords=category_keywords,
                    gallery_id=gallery_id,
                    image_file=image_file,
                    publish_dir=publish_dir,
                    interval_time=interval_time,
                    daily_publish_limit=daily_limit,
                    article_warning_threshold=warning_threshold,
                    publish_time=publish_time,
                    publish_times=[publish_time],
                    enabled=enabled,
                    **sitegroup_fields,
                )
            )
        names = [site.name.casefold() for site in updated]
        if len(names) != len(set(names)):
            self.message_var.set("站点标识不能重复")
            return None
        urls = [site_url_identity(site.url) for site in updated if site.url]
        if len(urls) != len(set(urls)):
            self.message_var.set("站点网址不能重复")
            return None
        return updated

    def _save_all(self) -> None:
        if self.is_task_running():
            self.message_var.set("发布任务运行中，暂不能保存配置")
            return
        materialized = self._materialize_sites()
        if materialized is None:
            return
        selected_index = max(self.current_index, 0)
        try:
            save_config(self.config_path, materialized)
        except (ConfigLoadError, OSError) as exc:
            self.message_var.set(f"保存失败：{exc}")
            return
        self.sites[:] = materialized
        self.on_sites_updated(self.sites)
        self._rebuild_rows(selected_index)
        if self._automation_disabled_sites:
            self.message_var.set(f"已保存；以下站点因自动发布配置不完整已停用：{'、'.join(self._automation_disabled_sites)}")
        else:
            self.message_var.set("全部更改已保存")

    def _has_unsaved_changes(self) -> bool:
        if not self._capture_current_widgets():
            return True
        if len(self.rows) != len(self.sites):
            return True
        for site, row in zip(self.sites, self.rows):
            current = (
                row["name"].get().strip(),
                row["full_endpoint"].get().strip(),
                row["fid"].get().strip(),
                row["category_name"].get().strip(),
                tuple(row["category_keywords"].items()),
                row["gallery_id"].get().strip(),
                row["publish_dir"].get().strip(),
                row["interval_time"].get().strip(),
                row["daily_publish_limit"].get().strip(),
                row["article_warning_threshold"].get().strip(),
                row["publish_time"].get().strip(),
                bool(row["enabled"].get()),
                row["site_title"].get().strip(),
                row["site_group"].get().strip(),
                row["mcp_token"].get().strip(),
                row["admin_username"].get().strip(),
                row["admin_password"].get().strip(),
                row["admin_path"].get().strip() or DEFAULT_ADMIN_PATH,
            )
            original = (
                site.name,
                self._site_full_endpoint(site),
                str(site.fid) if site.fid > 0 else "",
                site.category_name,
                tuple(site.category_keywords.items()),
                site.gallery_id,
                str(site.publish_dir),
                str(site.interval_time),
                str(site.daily_publish_limit),
                str(site.article_warning_threshold),
                site.publish_time or "06:00:00",
                site.enabled,
                site.site_title,
                site.site_group,
                site.mcp_token,
                site.admin_username,
                site.admin_password,
                site.admin_path or DEFAULT_ADMIN_PATH,
            )
            if current != original:
                return True
        return False

    def _request_close(self) -> None:
        if self._has_unsaved_changes() and not messagebox.askyesno(
            "放弃更改", "当前修改尚未保存，确定放弃吗？", parent=self
        ):
            return
        self.destroy()

    def _selected_indexes(self) -> list[int]:
        selected = sorted(int(item) for item in self.site_tree.selection())
        if selected:
            return selected
        return [index for index, row in enumerate(self.rows) if row["selected"].get()]

    def _require_selected_indexes(self, action: str) -> list[int]:
        indexes = self._selected_indexes()
        if not indexes:
            self.message_var.set(f"请先选择要{action}的站点")
        return indexes

    def _batch_edit_daily_limit(self) -> None:
        indexes = self._require_selected_indexes("批量修改")
        if not indexes:
            return
        value = simpledialog.askinteger(
            "批量修改每日上限",
            f"为已选 {len(indexes)} 个站点设置每日发布上限（篇）：",
            parent=self,
            minvalue=0,
        )
        if value is None:
            return
        for index in indexes:
            self.rows[index]["daily_publish_limit"].set(str(value))
            self._refresh_tree_item(index)
        self.message_var.set(
            f"已将 {len(indexes)} 个站点的每日上限改为 {value} 篇；点击“保存更改”后生效"
        )

    def _batch_set_enabled(self, enabled: bool) -> None:
        indexes = self._require_selected_indexes("批量启用" if enabled else "批量停发")
        if not indexes:
            return
        for index in indexes:
            self.rows[index]["enabled"].set(enabled)
            self._refresh_tree_item(index)
        action = "启用自动发布" if enabled else "停用自动发布"
        self.message_var.set(
            f"已将 {len(indexes)} 个站点设为{action}；点击“保存更改”后生效"
        )

    def _draft_sites_snapshot(self):
        snapshots = []
        for original, row in zip(self.sites, self.rows):
            name = row["name"].get().strip() or original.name
            full_endpoint = row["full_endpoint"].get().strip()
            url = row["url"].get().strip() or original.url
            if full_endpoint:
                try:
                    url = parse_publish_endpoint(full_endpoint).base_url
                except ValueError:
                    pass
            snapshots.append(
                replace(
                    original,
                    name=name,
                    url=url,
                    site_title=row["site_title"].get().strip(),
                    site_group=row["site_group"].get().strip(),
                    mcp_token=row["mcp_token"].get().strip(),
                    admin_username=row["admin_username"].get().strip(),
                    admin_password=row["admin_password"].get().strip(),
                    admin_path=row["admin_path"].get().strip() or DEFAULT_ADMIN_PATH,
                )
            )
        return snapshots

    def _open_bulk_sitegroup_dialog(self):
        if not self.rows:
            self.message_var.set("暂无可配置的站点")
            return None
        if not self._capture_current_widgets():
            return None
        indexes = self._selected_indexes()
        initial_names = (
            [self.rows[index]["name"].get().strip() for index in indexes]
            if len(indexes) > 1
            else None
        )
        return BulkSiteGroupDialog(
            self,
            self._draft_sites_snapshot(),
            self._apply_bulk_sitegroup_updates,
            initial_selected_names=initial_names,
        )

    def _apply_bulk_sitegroup_updates(self, updates: dict[str, dict[str, str]]) -> bool:
        if not updates:
            return False
        by_exact: dict[str, int] = {}
        by_folded: dict[str, int] = {}
        for index, row in enumerate(self.rows):
            name = row["name"].get().strip()
            if name:
                by_exact[name] = index
                by_folded.setdefault(name.casefold(), index)

        updated_indexes: list[int] = []
        allowed_fields = {
            "site_title",
            "site_group",
            "mcp_token",
            "admin_username",
            "admin_password",
            "admin_path",
        }
        for site_name, field_updates in updates.items():
            index = by_exact.get(site_name)
            if index is None:
                index = by_folded.get(site_name.casefold())
            if index is None:
                continue
            row = self.rows[index]
            changed = False
            for field, value in field_updates.items():
                if field in allowed_fields and field in row:
                    row[field].set(value)
                    changed = True
            if changed:
                updated_indexes.append(index)
                self._refresh_tree_item(index)

        if self.credentials_frame is not None:
            self.credentials_frame.set_group_choices(self._collect_group_choices())
            if self.current_index in updated_indexes:
                self.credentials_frame.bind_vars(self.rows[self.current_index])

        self.message_var.set(
            f"已批量更新 {len(updated_indexes)} 个站点的站群配置；点击“保存更改”后生效"
        )
        return True

    def _clone_selected_site(self) -> None:
        indexes = self._require_selected_indexes("复制")
        if not indexes:
            self.message_var.set("请先选择一个站点")
            return
        materialized = self._materialize_sites()
        if materialized is not None:
            self._show_new_site_dialog(template_site=materialized[indexes[0]])
            self.message_var.set("请确认副本的完整接口和自动发布参数")

    def _delete_selected_sites(self) -> None:
        if self.is_task_running():
            self.message_var.set("发布任务运行中，暂不能删除站点")
            return
        indexes = self._require_selected_indexes("删除")
        if not indexes:
            return
        names = [self.sites[index].name for index in indexes]
        display_names = "、".join(names[:5])
        if len(names) > 5:
            display_names += f" 等 {len(names)} 个"
        if not messagebox.askyesno(
            "删除站点",
            f"确定删除站点“{display_names}”吗？\n"
            "本地文章和图库文件不会删除，未保存的编辑内容也不会随此次删除保存。",
            parent=self,
        ):
            return
        selected = set(indexes)
        updated = [site for index, site in enumerate(self.sites) if index not in selected]
        try:
            save_config(self.config_path, updated)
        except (ConfigLoadError, OSError) as exc:
            self.message_var.set(f"删除失败：{exc}")
            return
        self.sites[:] = updated
        self.on_sites_updated(self.sites)
        self._rebuild_rows(min(indexes[0], len(self.sites) - 1))
        self.message_var.set(f"已删除 {len(names)} 个站点：{display_names}")

    # Compatibility alias for callers that used the prior singular method name.
    def _delete_selected_site(self) -> None:
        self._delete_selected_sites()

    def _open_schedule_editor(self) -> None:
        self.message_var.set("发布时间已合并到当前站点配置，无需单独调度")

    def _open_new_site(self) -> None:
        self._show_new_site_dialog(template_site=None)

    def _show_new_site_dialog(self, template_site=None) -> None:
        if self.is_task_running():
            self.message_var.set("发布任务运行中，暂不能新增或复制站点")
            return
        NewSiteDialog(
            self,
            self.sites,
            self._append_sites_from_dialog,
            template_site=template_site,
            config_path=self.config_path,
            is_task_running=self.is_task_running,
            category_cache=self.category_cache,
        )

    def _append_sites_from_dialog(self, sites) -> bool | str:
        if self.is_task_running():
            return "发布任务运行中，暂不能新增站点"
        materialized = self._materialize_sites()
        if materialized is None:
            return self.message_var.get()
        updated = materialized + list(sites)
        try:
            save_config(self.config_path, updated)
        except (ConfigLoadError, OSError) as exc:
            self.message_var.set(f"新增失败：{exc}")
            return self.message_var.get()
        self.sites[:] = updated
        self.on_sites_updated(self.sites)
        self._rebuild_rows(len(self.sites) - 1)
        self.message_var.set(f"已新增 {len(sites)} 个站点")
        return True

    def _rebuild_rows(self, selected_index: int = 0) -> None:
        self.current_index = -1
        self._create_row_drafts()
        self._reload_galleries()
        self._render_site_list()
        if self.sites:
            self._select_site(max(0, min(selected_index, len(self.sites) - 1)))
        else:
            if self.credentials_frame is not None:
                self.credentials_frame.bind_vars(None)
                self.credentials_frame.set_group_choices([])
                self.credentials_frame.set_status("")
            self._set_editor_enabled(False)
            for value in self.detail_vars.values():
                value.set("-")

    def _collect_group_choices(self) -> list[str]:
        groups: list[str] = []
        seen: set[str] = set()
        for row in self.rows:
            value = row["site_group"].get().strip()
            if value and value not in seen:
                seen.add(value)
                groups.append(value)
        return groups

    def _extract_sitegroup_fields(
        self,
        site_name: str,
        row: dict[str, object],
    ) -> dict[str, str] | None:
        values: dict[str, str] = {}
        for key, label in _SITEGROUP_TEXT_LABELS:
            text = row[key].get().strip()
            if _has_control_chars(text):
                self.message_var.set(f"{site_name}：{label}不能包含换行或控制字符")
                return None
            if key == "site_group" and len(text) > SITE_GROUP_MAX_LENGTH:
                self.message_var.set(
                    f"{site_name}：{label}不能超过 {SITE_GROUP_MAX_LENGTH} 个字符"
                )
                return None
            values[key] = text
        try:
            values["admin_path"] = normalize_admin_path(row["admin_path"].get())
        except ValueError as exc:
            self.message_var.set(f"{site_name}：{exc}")
            return None
        return values

    def _build_draft_site(self, index: int):
        if index < 0 or index >= len(self.rows) or index >= len(self.sites):
            return None
        original = self.sites[index]
        row = self.rows[index]
        name = row["name"].get().strip() or original.name
        full_endpoint = row["full_endpoint"].get().strip()
        if full_endpoint:
            try:
                parsed = parse_publish_endpoint(full_endpoint)
            except ValueError as exc:
                self._set_sitegroup_status(f"{name}：{exc}")
                return None
            url = parsed.base_url
            jiekou = parsed.publish_endpoint
        else:
            url = ""
            jiekou = ""
        sitegroup_fields = self._extract_sitegroup_fields(name, row)
        if sitegroup_fields is None:
            if self.credentials_frame is not None:
                self.credentials_frame.set_status(self.message_var.get())
            return None
        return replace(
            original,
            name=name,
            url=url,
            jiekou=jiekou,
            **sitegroup_fields,
        )

    def _get_jump_service(self):
        if self.jump_service is None:
            mcp_factory = lambda site: AnqiMcpClient.for_site(
                site, self.integration_settings
            )
            self.jump_service = AdminJumpService(
                self.integration_settings,
                mcp_client_factory=mcp_factory,
            )
            self._owns_jump_service = True
        return self.jump_service

    def _set_sitegroup_status(self, text: str) -> None:
        self.message_var.set(text)
        if self.credentials_frame is not None:
            self.credentials_frame.set_status(text)

    def _open_current_backend(self) -> None:
        if self.current_index < 0:
            self.message_var.set("请先选择一个站点")
            return
        draft = self._build_draft_site(self.current_index)
        if draft is None:
            return
        open_admin_backends(
            self,
            [draft],
            service=self._get_jump_service(),
            runner=self._sitegroup_runner,
            status_callback=self._set_sitegroup_status,
            settings=self.integration_settings,
        )

    def _open_selected_backends(self) -> None:
        indexes = self._selected_indexes()
        if not indexes and self.current_index >= 0:
            indexes = [self.current_index]
        if not indexes:
            self.message_var.set("请先选择要跳转后台的站点")
            return
        drafts = []
        for index in indexes:
            draft = self._build_draft_site(index)
            if draft is None:
                return
            drafts.append(draft)
        open_admin_backends(
            self,
            drafts,
            service=self._get_jump_service(),
            runner=self._sitegroup_runner,
            status_callback=self._set_sitegroup_status,
            settings=self.integration_settings,
        )

    def _test_current_mcp(self) -> None:
        if self.current_index < 0:
            self.message_var.set("请先选择一个站点")
            return
        target_index = self.current_index
        draft = self._build_draft_site(target_index)
        if draft is None:
            return
        self._set_sitegroup_status(f"正在测试 {draft.name} 的 MCP 连接…")

        def _on_success(result) -> None:
            summary = format_probe_summary(result)
            self.message_var.set(f"{draft.name}：{summary}")
            if self.credentials_frame is not None and self.current_index == target_index:
                self.credentials_frame.set_status(summary)

        def _on_error(exc: Exception) -> None:
            text = f"MCP 测试失败：{exc}"
            self.message_var.set(f"{draft.name}：{text}")
            if self.credentials_frame is not None and self.current_index == target_index:
                self.credentials_frame.set_status(text)

        self._sitegroup_runner.submit(
            lambda: self._probe_fn(draft, self.integration_settings),
            on_success=_on_success,
            on_error=_on_error,
        )

    def destroy(self) -> None:
        self._category_generation += 1
        runner = getattr(self, "_sitegroup_runner", None)
        if runner is not None:
            runner.shutdown()
        if getattr(self, "_owns_jump_service", False) and self.jump_service is not None:
            try:
                self.jump_service.shutdown()
            except Exception:  # noqa: BLE001
                pass
            self.jump_service = None
        if self._category_after_job is not None:
            try:
                self.after_cancel(self._category_after_job)
            except tk.TclError:
                pass
            self._category_after_job = None
        if self._category_poll_job is not None:
            try:
                self.after_cancel(self._category_poll_job)
            except tk.TclError:
                pass
            self._category_poll_job = None
        super().destroy()
