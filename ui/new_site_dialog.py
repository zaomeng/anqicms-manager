from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from tkinter import messagebox, ttk

from core.config_service import ConfigLoadError, SITE_GROUP_MAX_LENGTH
from core.category_service import CategoryCache, CategoryService, CategoryServiceError
from core.gallery_service import GalleryError, GalleryService
from core.schedule_service import is_valid_publish_time, normalize_publish_time
from core.site_factory import (
    DEFAULT_ADMIN_PATH,
    clone_site_config,
    create_site_from_key,
    ensure_site_files,
    is_valid_publish_endpoint,
    is_valid_site_url,
    normalize_admin_path,
    site_url_identity,
    parse_publish_endpoint,
    validate_site_key,
)
from ui.category_picker import CategoryPicker
from ui.gallery_manager import GalleryManager
from ui.time_picker import TimePicker


class NewSiteDialog(tk.Toplevel):
    def __init__(
        self,
        master,
        sites,
        on_add_sites,
        template_site=None,
        config_path: str | Path = "config.ini",
        is_task_running=None,
        category_cache: CategoryCache | None = None,
    ):
        super().__init__(master)
        self.title("新增站点")
        self.geometry("760x640")
        self.minsize(680, 560)
        self.transient(master)
        self.grab_set()
        self.sites = sites
        self.on_add_sites = on_add_sites
        self.template_site = template_site
        self.is_task_running = is_task_running or (lambda: False)
        self.config_path = Path(config_path).resolve()
        self.base_dir = self.config_path.parent
        self.mode_var = tk.StringVar(value="clone" if self.template_site is not None else "quick")
        self.mode_options = ["clone"] if self.template_site is not None else ["quick", "blank"]
        self.name_var = tk.StringVar(value="")
        self.full_endpoint_var = tk.StringVar(value="")
        self.interval_time_var = tk.StringVar(value="60")
        self.daily_limit_var = tk.StringVar(value="60")
        self.warning_threshold_var = tk.StringVar(value="500")
        self.publish_times_var = tk.StringVar(value="06:00:00")
        self.gallery_id_var = tk.StringVar(value="")
        self.start_today_var = tk.BooleanVar(value=False)
        self.site_title_var = tk.StringVar(value="")
        self.site_group_var = tk.StringVar(value="")
        self.mcp_token_var = tk.StringVar(value="")
        self.admin_username_var = tk.StringVar(value="")
        self.admin_password_var = tk.StringVar(value="")
        self.admin_path_var = tk.StringVar(value=DEFAULT_ADMIN_PATH)
        self._show_mcp_token_var = tk.BooleanVar(value=False)
        self._show_admin_password_var = tk.BooleanVar(value=False)
        self.sitegroup_frame = None
        self.site_group_box = None
        self._mcp_token_entry = None
        self._admin_password_entry = None
        self.advanced_visible = tk.BooleanVar(value=False)
        self.preview_var = tk.StringVar(value="")
        self.mode_bar = None
        self.quick_frame = None
        self.advanced_frame = None
        self.manual_frame = None
        self.preview_frame = None
        self.advanced_toggle = None
        self.action_var = tk.StringVar(value="新增站点")
        self.action_button = None
        self.footer = None
        self.body_canvas = None
        self.body_container = None
        self.body_window = None
        self.message_var = tk.StringVar(value="只填网址和 API Key 即可创建站点")
        self.gallery_service = GalleryService(self.base_dir / "设置")
        self.category_service = CategoryService()
        self.category_cache = category_cache or CategoryCache(
            self.base_dir / "设置" / "category_cache.json"
        )
        self.category_picker = None
        self.gallery_box = None
        self.time_picker = None
        self.automation_frame = None
        self._gallery_by_name = {}
        self._category_generation = 0
        self._category_after_job = None
        self._category_poll_job = None
        self._category_requests_pending = set()
        self._category_results = queue.SimpleQueue()
        self._pending_category_id: int | None = None
        self._pending_category_keywords: dict[int, tuple[str, ...]] = (
            dict(self.template_site.category_keywords) if self.template_site is not None else {}
        )
        self._build()
        self._update_preview()
        self._initial_draft = self._draft_state()

    def _build(self) -> None:
        shell = ttk.Frame(self)
        shell.pack(fill=tk.BOTH, expand=True)
        self.body_canvas = tk.Canvas(shell, highlightthickness=0)
        scrollbar = ttk.Scrollbar(shell, orient=tk.VERTICAL, command=self.body_canvas.yview)
        self.body_canvas.configure(yscrollcommand=scrollbar.set)
        self.body_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        container = ttk.Frame(self.body_canvas, padding=18)
        self.body_container = container
        self.body_window = self.body_canvas.create_window((0, 0), window=container, anchor="nw")
        container.bind("<Configure>", lambda _event: self._update_scroll_layout())
        self.body_canvas.bind("<Configure>", lambda _event: self._update_scroll_layout())
        self.bind("<MouseWheel>", self._scroll_body)

        heading = ttk.Frame(container)
        heading.pack(fill=tk.X, pady=(0, 14))
        ttk.Label(heading, text="新增站点", style="Metric.TLabel").pack(anchor="w")
        ttk.Label(heading, text="粘贴完整发布接口，选择栏目和图库即可。", style="Muted.TLabel").pack(anchor="w", pady=(3, 0))

        self.mode_bar = ttk.Frame(container)
        self.mode_bar.pack(fill=tk.X, pady=(0, 10))
        for label, value in [("快速新增", "quick"), ("完整配置", "blank"), ("复制当前站点", "clone")]:
            if value not in self.mode_options:
                continue
            ttk.Radiobutton(self.mode_bar, text=label, value=value, variable=self.mode_var, command=self._refresh_mode).pack(
                side=tk.LEFT, padx=(0, 12)
            )

        self.quick_frame = ttk.LabelFrame(container, text="连接信息", padding=12)
        self.quick_entries = {}
        ttk.Label(self.quick_frame, text="完整发布接口").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=7)
        endpoint_entry = ttk.Entry(self.quick_frame, textvariable=self.full_endpoint_var)
        endpoint_entry.grid(row=0, column=1, sticky="ew", pady=7, ipady=3)
        self.quick_entries["full_endpoint"] = endpoint_entry
        self.quick_frame.columnconfigure(1, weight=1)
        self.full_endpoint_var.trace_add("write", lambda *_: self._endpoint_changed())

        self.advanced_toggle = ttk.Checkbutton(
            container,
            text="显示高级项",
            variable=self.advanced_visible,
            command=self._toggle_advanced,
        )

        self.advanced_frame = ttk.LabelFrame(container, text="高级设置", padding=12)
        advanced_fields = [
            ("站点标识", self.name_var),
            ("发布间隔秒", self.interval_time_var),
            ("每日上限", self.daily_limit_var),
            ("库存提醒阈值", self.warning_threshold_var),
        ]
        for row, (label, var) in enumerate(advanced_fields):
            ttk.Label(self.advanced_frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)
            ttk.Entry(self.advanced_frame, textvariable=var).grid(row=row, column=1, sticky="ew", pady=5, ipady=2)
        self._build_sitegroup_fields(self.advanced_frame, row=len(advanced_fields))
        self.advanced_frame.columnconfigure(1, weight=1)
        self._build_automation_fields(container)

        self.manual_frame = ttk.LabelFrame(container, text="站点配置", padding=12)
        ttk.Label(self.manual_frame, text="完整发布接口").grid(
            row=0, column=0, sticky="w", padx=(0, 10), pady=5
        )
        ttk.Entry(self.manual_frame, textvariable=self.full_endpoint_var).grid(
            row=0, column=1, sticky="ew", pady=5, ipady=2
        )
        self.manual_frame.columnconfigure(1, weight=1)
        self.name_var.trace_add("write", lambda *_: self._update_preview())

        self.preview_frame = ttk.LabelFrame(container, text="将创建的本地文件", padding=12)
        ttk.Label(self.preview_frame, textvariable=self.preview_var, justify=tk.LEFT, wraplength=660).pack(anchor="w")

        self.footer = ttk.Frame(container)
        self.footer.pack(side=tk.BOTTOM, fill=tk.X, pady=(12, 0))
        ttk.Label(self.footer, textvariable=self.message_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.action_button = ttk.Button(self.footer, textvariable=self.action_var, command=self._create_sites, style="Accent.TButton")
        self.action_button.pack(side=tk.RIGHT, padx=(6, 0), ipady=3)
        ttk.Button(self.footer, text="取消", command=self._request_close).pack(side=tk.RIGHT, ipady=3)
        self._refresh_mode()
        self.bind("<Escape>", lambda _event: self._request_close())
        self.bind("<Control-Return>", lambda _event: self._create_sites())
        self.protocol("WM_DELETE_WINDOW", self._request_close)
        self.after_idle(lambda: self.quick_entries["full_endpoint"].focus_set() if self.mode_var.get() == "quick" else None)

    def _update_scroll_layout(self) -> None:
        if not self.body_canvas or not self.body_container or self.body_window is None:
            return
        viewport_width = max(1, self.body_canvas.winfo_width())
        viewport_height = max(1, self.body_canvas.winfo_height())
        content_height = max(self.body_container.winfo_reqheight(), viewport_height)
        self.body_canvas.itemconfigure(self.body_window, width=viewport_width, height=content_height)
        self.body_canvas.configure(scrollregion=(0, 0, viewport_width, content_height))

    def _scroll_body(self, event) -> None:
        self.body_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def _existing_site_groups(self) -> list[str]:
        groups: list[str] = []
        seen: set[str] = set()
        for site in self.sites:
            value = str(getattr(site, "site_group", "") or "").strip()
            if value and value not in seen:
                seen.add(value)
                groups.append(value)
        return groups

    def _build_sitegroup_fields(self, parent, *, row: int) -> None:
        frame = ttk.LabelFrame(parent, text="站群信息（可选）", padding=10)
        frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)
        self.sitegroup_frame = frame

        ttk.Label(frame, text="站点名称").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(frame, textvariable=self.site_title_var).grid(
            row=0, column=1, sticky="ew", padx=(0, 12), pady=4, ipady=2
        )

        ttk.Label(frame, text="站点分组").grid(row=0, column=2, sticky="w", padx=(0, 8), pady=4)
        self.site_group_box = ttk.Combobox(
            frame,
            textvariable=self.site_group_var,
            values=self._existing_site_groups(),
        )
        self.site_group_box.grid(row=0, column=3, sticky="ew", pady=4)

        ttk.Label(frame, text="MCP Token").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        token_wrap = ttk.Frame(frame)
        token_wrap.grid(row=1, column=1, columnspan=3, sticky="ew", pady=4)
        token_wrap.columnconfigure(0, weight=1)
        self._mcp_token_entry = ttk.Entry(token_wrap, textvariable=self.mcp_token_var, show="•")
        self._mcp_token_entry.grid(row=0, column=0, sticky="ew", ipady=2)
        ttk.Checkbutton(
            token_wrap,
            text="显示",
            variable=self._show_mcp_token_var,
            command=self._sync_sitegroup_secret_masks,
        ).grid(row=0, column=1, sticky="w", padx=(6, 0))

        ttk.Label(frame, text="后台账号").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(frame, textvariable=self.admin_username_var).grid(
            row=2, column=1, sticky="ew", padx=(0, 12), pady=4, ipady=2
        )

        ttk.Label(frame, text="后台路径").grid(row=2, column=2, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(frame, textvariable=self.admin_path_var).grid(
            row=2, column=3, sticky="ew", pady=4, ipady=2
        )

        ttk.Label(frame, text="后台密码").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=4)
        pwd_wrap = ttk.Frame(frame)
        pwd_wrap.grid(row=3, column=1, columnspan=3, sticky="ew", pady=4)
        pwd_wrap.columnconfigure(0, weight=1)
        self._admin_password_entry = ttk.Entry(
            pwd_wrap,
            textvariable=self.admin_password_var,
            show="•",
        )
        self._admin_password_entry.grid(row=0, column=0, sticky="ew", ipady=2)
        ttk.Checkbutton(
            pwd_wrap,
            text="显示",
            variable=self._show_admin_password_var,
            command=self._sync_sitegroup_secret_masks,
        ).grid(row=0, column=1, sticky="w", padx=(6, 0))

    def _sync_sitegroup_secret_masks(self) -> None:
        if self._mcp_token_entry is not None:
            self._mcp_token_entry.configure(
                show="" if self._show_mcp_token_var.get() else "•"
            )
        if self._admin_password_entry is not None:
            self._admin_password_entry.configure(
                show="" if self._show_admin_password_var.get() else "•"
            )

    def _sitegroup_extras(self) -> dict[str, str] | None:
        text_fields = (
            ("site_title", "站点名称", self.site_title_var),
            ("site_group", "站点分组", self.site_group_var),
            ("mcp_token", "MCP Token", self.mcp_token_var),
            ("admin_username", "后台账号", self.admin_username_var),
            ("admin_password", "后台密码", self.admin_password_var),
        )
        extras: dict[str, str] = {}
        for key, label, var in text_fields:
            value = var.get().strip()
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
                self.message_var.set(f"{label}不能包含换行或控制字符")
                return None
            if key == "site_group" and len(value) > SITE_GROUP_MAX_LENGTH:
                self.message_var.set(f"{label}不能超过 {SITE_GROUP_MAX_LENGTH} 个字符")
                return None
            extras[key] = value
        try:
            extras["admin_path"] = normalize_admin_path(self.admin_path_var.get())
        except ValueError as exc:
            self.message_var.set(str(exc))
            return None
        return extras

    def _build_automation_fields(self, parent) -> None:
        self.automation_frame = ttk.LabelFrame(parent, text="自动发布设置", padding=12)
        ttk.Label(self.automation_frame, text="发布栏目").grid(row=0, column=0, sticky="nw", padx=(0, 12), pady=5)
        category_area = ttk.Frame(self.automation_frame)
        category_area.grid(row=0, column=1, sticky="nsew", pady=5)
        self.category_picker = CategoryPicker(
            category_area,
            height=5,
            on_keywords_changed=self._category_keywords_changed,
            on_error=self.message_var.set,
        )
        self.category_picker.pack(fill=tk.BOTH, expand=True)
        ttk.Button(category_area, text="重新获取栏目", command=self._fetch_categories).pack(anchor="e", pady=(5, 0))

        ttk.Label(self.automation_frame, text="插图图库").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=5)
        gallery_area = ttk.Frame(self.automation_frame)
        gallery_area.grid(row=1, column=1, sticky="ew", pady=5)
        self.gallery_box = ttk.Combobox(gallery_area, state="readonly")
        self.gallery_box.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.gallery_box.bind("<<ComboboxSelected>>", self._gallery_selected)
        ttk.Button(gallery_area, text="图库设置", command=self._open_gallery_manager).pack(side=tk.LEFT, padx=(6, 0))

        ttk.Label(self.automation_frame, text="每日启动时间").grid(row=2, column=0, sticky="w", padx=(0, 12), pady=5)
        self.time_picker = TimePicker(self.automation_frame, "06:00:00")
        self.time_picker.grid(row=2, column=1, sticky="w", pady=5)
        self.publish_times_var.trace_add("write", lambda *_: self.time_picker.set(self.publish_times_var.get()))
        ttk.Checkbutton(
            self.automation_frame,
            text="发布时间已过时则今天立即执行",
            variable=self.start_today_var,
        ).grid(row=3, column=1, sticky="w", pady=5)
        self.automation_frame.columnconfigure(1, weight=1)
        self._reload_galleries()

    def _reload_galleries(self) -> None:
        try:
            galleries = self.gallery_service.list_galleries()
        except GalleryError as exc:
            self.message_var.set(f"图库加载失败：{exc}")
            galleries = []
        self._gallery_by_name = {gallery.name: gallery for gallery in galleries}
        names = list(self._gallery_by_name)
        if self.gallery_box is not None:
            self.gallery_box.configure(values=names)
            current_id = self.gallery_id_var.get()
            current = next((item.name for item in galleries if item.gallery_id == current_id), "")
            self.gallery_box.set(current)

    def _gallery_selected(self, _event=None) -> None:
        gallery = self._gallery_by_name.get(self.gallery_box.get()) if self.gallery_box is not None else None
        self.gallery_id_var.set(gallery.gallery_id if gallery else "")

    def _open_gallery_manager(self) -> None:
        if self.is_task_running():
            self.message_var.set("发布任务运行中，暂不能修改图库")
            return
        GalleryManager(
            self,
            self.gallery_service,
            self.sites,
            on_changed=self._reload_galleries,
            is_task_running=self.is_task_running,
        )

    def _endpoint_changed(self) -> None:
        self._category_generation += 1
        self._pending_category_id = None
        if self.category_picker is not None:
            self.category_picker.clear()
        if self._category_after_job is not None:
            try:
                self.after_cancel(self._category_after_job)
            except tk.TclError:
                pass
            self._category_after_job = None
        endpoint = self.full_endpoint_var.get().strip()
        cached_categories = self.category_cache.get(endpoint) if endpoint else None
        if cached_categories is not None:
            if self.category_picker is not None:
                self.category_picker.set_categories(
                    cached_categories,
                    category_keywords=self._pending_category_keywords,
                )
            self.message_var.set(f"已自动加载 {len(cached_categories)} 个栏目")
        else:
            self._category_after_job = self.after(600, self._fetch_categories)
        self._update_preview()

    def _fetch_categories(self) -> None:
        if self.category_picker is not None and not self.category_picker.commit_pending_edit():
            return
        if self._category_after_job is not None:
            try:
                self.after_cancel(self._category_after_job)
            except tk.TclError:
                pass
            self._category_after_job = None
        endpoint = self.full_endpoint_var.get().strip()
        if not endpoint:
            return
        self._category_generation += 1
        generation = self._category_generation
        self._category_requests_pending.add(generation)
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
            self._category_results.put((generation, endpoint, categories, error))

        threading.Thread(target=worker, daemon=True, name="category-fetch").start()
        if self._category_poll_job is None:
            self._category_poll_job = self.after(50, self._poll_category_results)

    def _poll_category_results(self) -> None:
        self._category_poll_job = None
        while True:
            try:
                generation, endpoint, categories, error = self._category_results.get_nowait()
            except queue.Empty:
                break
            self._category_requests_pending.discard(generation)
            if generation != self._category_generation or endpoint != self.full_endpoint_var.get().strip():
                continue
            if error:
                self.message_var.set(f"{error}；已保留原有标题关键词，可稍后重试")
                continue
            if self.category_picker is not None and not self.category_picker.commit_pending_edit():
                continue
            category_ids = {category.id for category in categories}
            self._pending_category_keywords = CategoryPicker.normalize_keyword_mapping(
                self._pending_category_keywords
            )
            removed_keyword_ids = [
                category_id
                for category_id in self._pending_category_keywords
                if category_id not in category_ids
            ]
            if removed_keyword_ids:
                self._pending_category_keywords = {
                    category_id: keywords
                    for category_id, keywords in self._pending_category_keywords.items()
                    if category_id in category_ids
                }
            if self.category_picker is not None:
                self.category_picker.set_categories(
                    categories,
                    selected_id=self._pending_category_id,
                    category_keywords=self._pending_category_keywords,
                )
            cache_error = ""
            try:
                self.category_cache.put(endpoint, categories)
            except (OSError, ValueError):
                cache_error = "栏目已获取，但自动缓存保存失败"
            self._pending_category_id = None
            if removed_keyword_ids:
                self.message_var.set(
                    "已获取栏目，并清理不存在栏目的标题关键词：ID "
                    + "、".join(str(category_id) for category_id in removed_keyword_ids)
                )
            elif cache_error:
                self.message_var.set(cache_error)
            else:
                self.message_var.set(f"已获取 {len(categories)} 个栏目，请选择发布栏目")
        if self._category_requests_pending:
            self._category_poll_job = self.after(50, self._poll_category_results)

    def _refresh_mode(self) -> None:
        mode = self.mode_var.get()
        if mode not in self.mode_options:
            self.mode_var.set("quick")
            mode = "quick"
        self.quick_frame.pack_forget()
        self.advanced_frame.pack_forget()
        self.manual_frame.pack_forget()
        self.preview_frame.pack_forget()
        self.advanced_toggle.pack_forget()
        self.automation_frame.pack_forget()
        if mode == "quick":
            self.message_var.set("粘贴完整发布接口后自动获取栏目")
            self.action_var.set("新增站点")
            self.quick_frame.pack(fill=tk.X)
            self.automation_frame.pack(fill=tk.X, pady=(8, 0))
            self.advanced_toggle.pack(anchor="w", pady=(10, 0))
            if self.advanced_visible.get():
                self.advanced_frame.pack(fill=tk.X, pady=(8, 0))
        elif mode == "clone" and self.template_site:
            self.name_var.set(f"{self.template_site.name}-copy")
            self.interval_time_var.set(str(self.template_site.interval_time))
            self.daily_limit_var.set(str(self.template_site.daily_publish_limit))
            self.publish_times_var.set(",".join(self.template_site.publish_times))
            self.full_endpoint_var.set(f"{self.template_site.url}{self.template_site.jiekou}")
            self._pending_category_id = self.template_site.fid
            cached_categories = self.category_cache.get(
                self.full_endpoint_var.get().strip()
            )
            if cached_categories is not None and self.category_picker is not None:
                self.category_picker.set_categories(
                    cached_categories,
                    selected_id=self._pending_category_id,
                    category_keywords=self._pending_category_keywords,
                    selected_title=self.template_site.category_name,
                )
            self.gallery_id_var.set(self.template_site.gallery_id)
            self.site_group_var.set(getattr(self.template_site, "site_group", "") or "")
            self.admin_path_var.set(
                getattr(self.template_site, "admin_path", DEFAULT_ADMIN_PATH)
                or DEFAULT_ADMIN_PATH
            )
            self.message_var.set("复制已有网站会继承原站点参数和接口")
            self.action_var.set("复制站点")
            self.manual_frame.pack(fill=tk.X, pady=(8, 0))
            self.automation_frame.pack(fill=tk.X, pady=(8, 0))
            self.advanced_frame.pack(fill=tk.X, pady=(8, 0))
            self._reload_galleries()
        elif mode == "blank":
            self.message_var.set("完整配置仍从完整发布接口获取站点和栏目")
            self.action_var.set("新增站点")
            self.manual_frame.pack(fill=tk.X, pady=(8, 0))
            self.automation_frame.pack(fill=tk.X, pady=(8, 0))
            self.advanced_frame.pack(fill=tk.X, pady=(8, 0))
        else:
            self.message_var.set("可使用高级项维护特殊站点")
        self._update_preview()
        self.after_idle(self._update_scroll_layout)

    def _toggle_advanced(self) -> None:
        if self.advanced_frame is None:
            return
        if self.advanced_visible.get():
            self.advanced_frame.pack(fill=tk.X, pady=(8, 0))
        else:
            self.advanced_frame.pack_forget()
        self.after_idle(self._update_scroll_layout)

    def _update_preview(self) -> None:
        from core.site_factory import derive_site_paths

        site_key = self.name_var.get().strip()
        if not site_key and self.full_endpoint_var.get().strip():
            try:
                site_key = parse_publish_endpoint(self.full_endpoint_var.get()).site_key
            except ValueError:
                site_key = ""
        if not site_key:
            self.preview_var.set("")
            return
        try:
            paths = derive_site_paths(site_key, self.base_dir)
        except ValueError:
            self.preview_var.set(f"站点标识无效: {site_key}")
            return
        self.preview_var.set(
            "\n".join(
                [
                    f"站点标识: {site_key}",
                    f"文章目录: {paths['publish_dir']}",
                    f"计数文件: {paths['daily_publish_filename']}",
                ]
            )
        )

    def _parse_publish_times(self):
        value = self.time_picker.get() if self.time_picker is not None else self.publish_times_var.get().strip()
        if not is_valid_publish_time(value):
            self.message_var.set("发布时间无效")
            return None
        normalized = normalize_publish_time(value)
        self.publish_times_var.set(normalized)
        return [normalized]

    def _draft_state(self) -> tuple:
        selected_category = self.category_picker.selected() if self.category_picker is not None else None
        selected_category_id = selected_category[0] if selected_category is not None else self._pending_category_id
        publish_time = self.time_picker.get() if self.time_picker is not None else self.publish_times_var.get()
        return (
            self.mode_var.get(),
            self.name_var.get(),
            self.full_endpoint_var.get(),
            self.interval_time_var.get(),
            self.daily_limit_var.get(),
            self.warning_threshold_var.get(),
            self.publish_times_var.get(),
            publish_time,
            self.gallery_id_var.get(),
            selected_category_id,
            tuple(self._pending_category_keywords.items()),
            bool(self.start_today_var.get()),
            self.site_title_var.get(),
            self.site_group_var.get(),
            self.mcp_token_var.get(),
            self.admin_username_var.get(),
            self.admin_password_var.get(),
            self.admin_path_var.get(),
        )

    def _request_close(self) -> None:
        if self.category_picker is not None and not self.category_picker.commit_pending_edit():
            return
        if self._draft_state() != self._initial_draft and not messagebox.askyesno(
            "放弃新增",
            "当前内容尚未创建，确定放弃吗？",
            parent=self,
        ):
            return
        self.destroy()

    def destroy(self) -> None:
        self._category_generation += 1
        self._category_requests_pending.clear()
        for attribute in ("_category_after_job", "_category_poll_job"):
            job = getattr(self, attribute, None)
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, attribute, None)
        super().destroy()

    def _create_sites(self) -> None:
        if self.is_task_running():
            self.message_var.set("发布任务运行中，暂不能新增或复制站点")
            return
        if self.category_picker is not None and not self.category_picker.commit_pending_edit():
            return
        mode = self.mode_var.get()
        if mode == "quick":
            sites = self._create_quick_site()
        elif mode == "clone" and self.template_site is not None:
            sites = self._create_cloned_site()
        else:
            sites = self._create_blank_site()
        if sites is None:
            return

        existing_names = {site.name.casefold() for site in self.sites}
        new_names = [site.name.casefold() for site in sites]
        if any(name in existing_names for name in new_names):
            self.message_var.set("站点标识重复，请更换名称")
            return
        if len(new_names) != len(set(new_names)):
            self.message_var.set("新增站点中存在重复站点名")
            return
        invalid_site = next((site for site in sites if not is_valid_site_url(site.url)), None)
        if invalid_site is not None:
            shown_url = invalid_site.url or "（空）"
            self.message_var.set(f"站点网址无效，公网仅支持 HTTPS：{shown_url}")
            return
        if any(not is_valid_publish_endpoint(site.jiekou) for site in sites):
            self.message_var.set("发布接口必须以单个 / 开头，且不能包含片段")
            return
        existing_urls = {site_url_identity(site.url) for site in self.sites if site.url.strip()}
        new_urls = [site_url_identity(site.url) for site in sites]
        if any(url in existing_urls for url in new_urls) or len(new_urls) != len(set(new_urls)):
            self.message_var.set("站点网址重复，请检查后再添加")
            return
        config_root = self.base_dir.resolve()
        for site in sites:
            if site.image_file is None:
                continue
            try:
                site.image_file.resolve().relative_to(config_root)
            except ValueError:
                self.message_var.set("图片映射文件必须位于配置目录内")
                return
        created_paths = self._new_managed_paths(sites)
        try:
            for site in sites:
                ensure_site_files(site)
        except OSError as exc:
            self._rollback_created_paths(created_paths)
            self.message_var.set(f"创建本地文件失败：{exc}")
            return
        try:
            accepted = self.on_add_sites(sites)
        except (ConfigLoadError, OSError) as exc:
            self._rollback_created_paths(created_paths)
            self.message_var.set(f"保存失败：{exc}")
            return
        if accepted is False:
            self._rollback_created_paths(created_paths)
            self.message_var.set("保存失败，请检查配置文件是否可写")
            return
        if isinstance(accepted, str):
            self._rollback_created_paths(created_paths)
            self.message_var.set(accepted)
            return
        self.destroy()

    def _new_managed_paths(self, sites) -> set[Path]:
        base = self.base_dir.resolve()
        managed: set[Path] = set()
        for site in sites:
            targets = [site.publish_dir, site.daily_publish_filename]
            if site.image_file is not None:
                targets.append(site.image_file)
            for target in targets:
                path = target.resolve()
                while path != base and base in path.parents:
                    if not path.exists():
                        managed.add(path)
                    path = path.parent
        return managed

    @staticmethod
    def _rollback_created_paths(paths: set[Path]) -> None:
        for path in sorted(paths, key=lambda item: len(item.parts), reverse=True):
            try:
                if path.is_dir():
                    path.rmdir()
                elif path.exists():
                    path.unlink()
            except OSError:
                pass

    def _create_quick_site(self):
        full_endpoint = self.full_endpoint_var.get().strip()
        if not full_endpoint:
            self.message_var.set("完整发布接口不能为空")
            return None
        try:
            parsed = parse_publish_endpoint(
                full_endpoint,
                existing_names={site.name for site in self.sites},
            )
        except ValueError as exc:
            self.message_var.set(str(exc))
            return None
        selected_category = self.category_picker.selected() if self.category_picker is not None else None
        if selected_category is None or selected_category[0] <= 0:
            self.message_var.set("必须从接口返回结果中选择发布栏目")
            return None
        gallery = self._selected_gallery()
        if gallery is None:
            return None
        publish_times = self._parse_publish_times()
        if publish_times is None:
            return None
        requested_name = self.name_var.get().strip()
        if requested_name and not self._validate_site_name(requested_name):
            return None
        values = self._parse_numeric_settings()
        if values is None:
            return None
        extras = self._sitegroup_extras()
        if extras is None:
            return None
        interval_time, daily_limit, warning_threshold = values
        category_id, category_name = selected_category
        site = create_site_from_key(
            requested_name or parsed.site_key,
            url=parsed.base_url,
            jiekou=parsed.publish_endpoint,
            fid=category_id,
            interval_time=interval_time,
            daily_publish_limit=daily_limit,
            publish_times=publish_times,
            base_dir=self.base_dir,
        )
        site = replace(
            site,
            category_name=category_name,
            category_keywords=self.category_picker.keyword_mapping(),
            gallery_id=gallery.gallery_id,
            image_file=gallery.file_path,
            article_warning_threshold=warning_threshold,
            activation_date=(
                date.today() if self.start_today_var.get() else date.today() + timedelta(days=1)
            ).isoformat(),
            **extras,
        )
        return [site]

    def _create_cloned_site(self):
        name = self.name_var.get().strip()
        if not name:
            self.message_var.set("站点标识不能为空")
            return None
        if not self._validate_site_name(name):
            return None
        try:
            parsed = parse_publish_endpoint(self.full_endpoint_var.get(), existing_names={site.name for site in self.sites})
        except ValueError as exc:
            self.message_var.set(str(exc))
            return None
        selected_category = self.category_picker.selected() if self.category_picker is not None else None
        if selected_category is None or selected_category[0] <= 0:
            self.message_var.set("必须从接口返回结果中选择发布栏目")
            return None
        gallery = self._selected_gallery()
        if gallery is None:
            return None
        publish_times = self._parse_publish_times()
        if publish_times is None:
            return None
        values = self._parse_numeric_settings()
        if values is None:
            return None
        extras = self._sitegroup_extras()
        if extras is None:
            return None
        interval_time, daily_limit, warning_threshold = values
        cloned = clone_site_config(
            self.template_site,
            name,
            parsed.base_url,
            parsed.publish_endpoint,
            base_dir=self.base_dir,
        )
        cloned = replace(
            cloned,
            fid=selected_category[0],
            interval_time=interval_time,
            daily_publish_limit=daily_limit,
            article_warning_threshold=warning_threshold,
            publish_time=publish_times[0],
            publish_times=publish_times,
            image_file=gallery.file_path,
            category_name=selected_category[1],
            category_keywords=self.category_picker.keyword_mapping(),
            gallery_id=gallery.gallery_id,
            activation_date=(date.today() if self.start_today_var.get() else date.today() + timedelta(days=1)).isoformat(),
            **extras,
        )
        return [cloned]

    def _create_blank_site(self):
        return self._create_quick_site()

    def _category_keywords_changed(self, mapping: dict[int, tuple[str, ...]]) -> None:
        self._pending_category_keywords = dict(mapping)
        self.message_var.set("标题关键词已更新")

    def _parse_numeric_settings(self):
        try:
            values = (
                int(self.interval_time_var.get().strip() or "60"),
                int(self.daily_limit_var.get().strip() or "60"),
                int(self.warning_threshold_var.get().strip() or "500"),
            )
        except ValueError:
            self.message_var.set("发布间隔、每日上限和库存阈值必须是数字")
            return None
        if any(value < 0 for value in values):
            self.message_var.set("发布间隔、每日上限和库存阈值不能为负数")
            return None
        return values

    def _selected_gallery(self):
        self._gallery_selected()
        gallery_id = self.gallery_id_var.get().strip()
        if not gallery_id:
            self.message_var.set("必须选择图库")
            return None
        try:
            validation = self.gallery_service.validate_gallery(gallery_id)
            gallery = self.gallery_service.require_gallery(gallery_id)
        except GalleryError as exc:
            self.message_var.set(f"图库不可用：{exc}")
            return None
        if not validation.valid:
            self.message_var.set(f"图库不可用：{validation.message}")
            return None
        return gallery

    def _validate_site_name(self, name: str, prefix: str = "") -> bool:
        try:
            validate_site_key(name)
        except ValueError:
            label = f"{prefix}：" if prefix else ""
            self.message_var.set(f"{label}站点标识无效：包含非法路径字符或 Windows 保留名称")
            return False
        return True
