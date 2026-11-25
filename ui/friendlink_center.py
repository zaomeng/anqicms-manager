"""Friend-link management center window (友链管理中心)."""

from __future__ import annotations

import threading
import tkinter as tk
from collections.abc import Callable, Sequence
from pathlib import Path
from tkinter import messagebox, ttk

from core.friendlink_ledger import FriendLinkLedger, ledger_path
from core.friendlink_service import (
    FriendLinkError,
    FriendLinkService,
    link_key,
    normalize_link_url,
    parse_batch_link_input,
)
from core.integration_settings import IntegrationSettings
from models.entities import SiteConfig
from models.sitegroup import (
    LINK_CREATED,
    LINK_DELETED,
    LINK_NOT_FOUND,
    LINK_SKIPPED_EXISTS,
    LINK_SKIPPED_SELF,
    FriendLink,
    FriendLinkSpec,
    LinkHealth,
    LinkOpResult,
)
from ui.admin_jump_actions import open_admin_backends
from ui.crosslink_panel import CrossLinkPanel
from ui.friendlink_batch_panel import FriendLinkBatchPanel
from ui.ui_async import BackgroundRunner

_TAB_KEY_TO_INDEX = {
    "single": 0,
    "单站友链": 0,
    "batch": 1,
    "统一上下链": 1,
    "crosslink": 2,
    "站群互链": 2,
}


class FriendLinkCenter(tk.Toplevel):
    """Dedicated Toplevel for single-site, batch, and cross-link management."""

    TAB_TITLES = ("单站友链", "统一上下链", "站群互链")

    def __init__(
        self,
        master,
        sites_provider: Callable[[], Sequence[SiteConfig]] | Sequence[SiteConfig],
        *,
        config_path: str | Path | None = None,
        settings: IntegrationSettings | None = None,
        service: FriendLinkService | None = None,
        ledger: FriendLinkLedger | None = None,
        jump_service=None,
        initial_site_name: str | None = None,
        initial_tab: str | int = "single",
        confirm_fn: Callable[[str, str], bool] | None = None,
        warn_fn: Callable[[str, str], None] | None = None,
    ) -> None:
        super().__init__(master)
        self.title("友链管理中心")
        self.geometry("1040x740")
        self.minsize(880, 600)

        self._sites_provider = sites_provider
        self.config_path = Path(config_path) if config_path is not None else Path("config.ini")
        self.settings = settings or IntegrationSettings()
        self.service = service or FriendLinkService(self.settings)
        self.ledger = ledger or FriendLinkLedger(ledger_path(self.config_path))
        self.jump_service = jump_service
        self._confirm_fn = confirm_fn
        self._warn_fn = warn_fn

        self.runner = BackgroundRunner(self)
        self._single_busy = False
        self._single_cancel_event: threading.Event | None = None
        self._sites: list[SiteConfig] = []
        self._site_by_name: dict[str, SiteConfig] = {}
        self._current_links: dict[str, FriendLink] = {}
        self._health_by_key: dict[str, LinkHealth] = {}

        # Tab 1 vars
        self.site_var = tk.StringVar(value="")
        self.smart_nofollow_var = tk.BooleanVar(
            value=bool(self.settings.friendlink.default_nofollow)
        )
        self.add_title_var = tk.StringVar(value="")
        self.add_link_var = tk.StringVar(value="")
        self.add_nofollow_var = tk.BooleanVar(
            value=bool(self.settings.friendlink.default_nofollow)
        )
        self.add_remark_var = tk.StringVar(value="")
        self.add_back_link_var = tk.StringVar(value="")
        self.single_status_var = tk.StringVar(value="请选择站点并点击“刷新”加载友链列表")

        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self._load_site_choices()
        if initial_site_name:
            self.select_site(initial_site_name, auto_refresh=True)
        elif self._sites:
            self.site_var.set(self._sites[0].name)
        self.select_tab(initial_tab)

    def _warn(self, title: str, msg: str) -> None:
        if self._warn_fn is not None:
            self._warn_fn(title, msg)
        else:
            messagebox.showwarning(title, msg, parent=self)

    def _confirm(self, title: str, msg: str) -> bool:
        if self._confirm_fn is not None:
            return bool(self._confirm_fn(title, msg))
        return bool(messagebox.askyesno(title, msg, parent=self))

    def _fetch_sites(self) -> list[SiteConfig]:
        raw = self._sites_provider() if callable(self._sites_provider) else self._sites_provider
        return list(raw or ())

    def _build(self) -> None:
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        # Tab 1: 单站友链
        self.single_tab = ttk.Frame(self.notebook)
        self._build_single_tab(self.single_tab)
        self.notebook.add(self.single_tab, text=self.TAB_TITLES[0])

        # Tab 2: 统一上下链
        self.batch_panel = FriendLinkBatchPanel(
            self.notebook,
            self._sites_provider,
            self.service,
            self.runner,
            self.settings,
            confirm_fn=self._confirm_fn,
            warn_fn=self._warn_fn,
        )
        self.notebook.add(self.batch_panel, text=self.TAB_TITLES[1])

        # Tab 3: 站群互链
        self.crosslink_panel = CrossLinkPanel(
            self.notebook,
            self._sites_provider,
            self.service,
            self.ledger,
            self.runner,
            self.settings,
            confirm_fn=self._confirm_fn,
            warn_fn=self._warn_fn,
        )
        self.notebook.add(self.crosslink_panel, text=self.TAB_TITLES[2])

    def _build_single_tab(self, parent: ttk.Frame) -> None:
        top_bar = ttk.Frame(parent, padding=(8, 8, 8, 4))
        top_bar.pack(fill=tk.X)

        ttk.Label(top_bar, text="当前站点：").pack(side=tk.LEFT)
        self.site_box = ttk.Combobox(
            top_bar,
            textvariable=self.site_var,
            state="readonly",
            width=26,
        )
        self.site_box.pack(side=tk.LEFT, padx=(0, 8))
        self.site_box.bind("<<ComboboxSelected>>", lambda _e: self.refresh_single_links())

        self.refresh_button = ttk.Button(
            top_bar,
            text="刷新",
            command=self.refresh_single_links,
        )
        self.refresh_button.pack(side=tk.LEFT, padx=(0, 6))

        self.delete_button = ttk.Button(
            top_bar,
            text="删除选中",
            command=self.delete_selected_links,
        )
        self.delete_button.pack(side=tk.LEFT, padx=(0, 6))

        self.check_button = ttk.Button(
            top_bar,
            text="自动检测反链",
            command=self.check_single_backlinks,
        )
        self.check_button.pack(side=tk.LEFT, padx=(0, 6))

        self.jump_button = ttk.Button(
            top_bar,
            text="跳转后台",
            style="Compact.TButton",
            command=self.open_current_site_admin,
        )
        self.jump_button.pack(side=tk.RIGHT)

        hint_bar = ttk.Frame(parent, padding=(8, 0, 8, 2))
        hint_bar.pack(fill=tk.X)
        self.check_hint_label = ttk.Label(
            hint_bar,
            text="自动访问对方网站分析我方主域名，无需填写反链页",
            style="Muted.TLabel",
        )
        self.check_hint_label.pack(side=tk.LEFT)

        # Live friend-links Treeview
        tree_wrap = ttk.Frame(parent, padding=(8, 4, 8, 4))
        tree_wrap.pack(fill=tk.BOTH, expand=True)

        columns = (
            ("id", "ID", 52, False),
            ("title", "标题", 150, False),
            ("link", "链接", 220, True),
            ("nofollow", "nofollow", 72, False),
            ("remark", "备注", 140, True),
            ("status", "状态/反链", 230, True),
        )
        self.single_tree = ttk.Treeview(
            tree_wrap,
            columns=tuple(c[0] for c in columns),
            show="headings",
            selectmode="extended",
            height=11,
        )
        for col_id, heading, width, stretch in columns:
            self.single_tree.heading(col_id, text=heading)
            self.single_tree.column(col_id, width=width, minwidth=44, stretch=stretch)

        self.single_tree.tag_configure("backlink_ok", foreground="#15803d")
        self.single_tree.tag_configure(
            "backlink_nofollow", foreground="#b45309", background="#fffbeb"
        )
        self.single_tree.tag_configure(
            "backlink_missing", foreground="#b45309", background="#fffbeb"
        )
        self.single_tree.tag_configure(
            "backlink_error", foreground="#b91c1c", background="#fef2f2"
        )

        yscroll = ttk.Scrollbar(
            tree_wrap, orient=tk.VERTICAL, command=self.single_tree.yview
        )
        self.single_tree.configure(yscrollcommand=yscroll.set)
        self.single_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Smart paste quick-add area (HTML <a> or multi-line 标题|网址)
        smart_frame = ttk.LabelFrame(
            parent,
            text="快速粘贴上链（支持直接粘贴 HTML <a> 代码或多行 标题|网址）",
            padding=8,
        )
        smart_frame.pack(fill=tk.X, padx=8, pady=(4, 4))

        paste_wrap = ttk.Frame(smart_frame)
        paste_wrap.pack(fill=tk.X)
        self.smart_paste_text = tk.Text(paste_wrap, height=3, wrap=tk.NONE, undo=True)
        paste_yscroll = ttk.Scrollbar(
            paste_wrap, orient=tk.VERTICAL, command=self.smart_paste_text.yview
        )
        self.smart_paste_text.configure(yscrollcommand=paste_yscroll.set)
        self.smart_paste_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        paste_yscroll.pack(side=tk.RIGHT, fill=tk.Y)

        smart_bar = ttk.Frame(smart_frame)
        smart_bar.pack(fill=tk.X, pady=(6, 0))
        self.smart_nofollow_check = ttk.Checkbutton(
            smart_bar,
            text="默认 nofollow",
            variable=self.smart_nofollow_var,
        )
        self.smart_nofollow_check.pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(
            smart_bar,
            text='可直接粘贴 <a href="...">标题</a>、<li>...</li> 或 标题|网址，自动去重并跳过本站',
            style="Muted.TLabel",
        ).pack(side=tk.LEFT)

        self.smart_add_button = ttk.Button(
            smart_bar,
            text="智能识别并上链",
            style="Accent.TButton",
            command=self.smart_add_links,
        )
        self.smart_add_button.pack(side=tk.RIGHT)

        self.smart_clear_button = ttk.Button(
            smart_bar,
            text="清空",
            style="Compact.TButton",
            command=self.clear_smart_paste,
        )
        self.smart_clear_button.pack(side=tk.RIGHT, padx=(0, 6))

        # Add single link form (compact fallback)
        form_frame = ttk.LabelFrame(parent, text="单条手动补录（可选）", padding=6)
        form_frame.pack(fill=tk.X, padx=8, pady=(2, 4))

        row1 = ttk.Frame(form_frame)
        row1.pack(fill=tk.X)
        ttk.Label(row1, text="标题：").pack(side=tk.LEFT)
        self.add_title_entry = ttk.Entry(row1, textvariable=self.add_title_var, width=18)
        self.add_title_entry.pack(side=tk.LEFT, padx=(0, 8))

        ttk.Label(row1, text="链接：").pack(side=tk.LEFT)
        self.add_link_entry = ttk.Entry(row1, textvariable=self.add_link_var, width=28)
        self.add_link_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        ttk.Label(row1, text="备注：").pack(side=tk.LEFT)
        self.add_remark_entry = ttk.Entry(row1, textvariable=self.add_remark_var, width=16)
        self.add_remark_entry.pack(side=tk.LEFT, padx=(0, 8))

        self.add_nofollow_check = ttk.Checkbutton(
            row1,
            text="nofollow",
            variable=self.add_nofollow_var,
        )
        self.add_nofollow_check.pack(side=tk.LEFT, padx=(0, 8))

        self.add_button = ttk.Button(
            row1,
            text="添加友链",
            command=self.add_single_link,
        )
        self.add_button.pack(side=tk.RIGHT)

        # Status bar
        status_bar = ttk.Frame(parent, padding=(8, 4, 8, 8))
        status_bar.pack(fill=tk.X)
        self.single_status_label = ttk.Label(
            status_bar, textvariable=self.single_status_var, style="Muted.TLabel"
        )
        self.single_status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

    def _load_site_choices(self) -> None:
        self._sites = self._fetch_sites()
        self._site_by_name = {site.name: site for site in self._sites}
        names = [site.name for site in self._sites]
        self.site_box.configure(values=tuple(names))
        if self.site_var.get() not in self._site_by_name:
            self.site_var.set(names[0] if names else "")

    def reload_sites(self) -> None:
        """Reload sites across all three tabs."""
        self._load_site_choices()
        self.batch_panel.reload_sites()
        self.crosslink_panel.reload_sites()

    def current_site(self) -> SiteConfig | None:
        return self._site_by_name.get(self.site_var.get().strip())

    def select_site(self, name: str, *, auto_refresh: bool = True) -> bool:
        """Select *name* in Tab 1 and optionally trigger a live refresh."""
        if name not in self._site_by_name:
            self._load_site_choices()
        if name not in self._site_by_name:
            return False
        self.site_var.set(name)
        if auto_refresh:
            self.refresh_single_links()
        return True

    def select_tab(self, key: str | int) -> None:
        """Switch notebook tab by key (``single``/``batch``/``crosslink``), Chinese title or index."""
        if isinstance(key, int):
            idx = key
        else:
            idx = _TAB_KEY_TO_INDEX.get(str(key).strip().casefold(), _TAB_KEY_TO_INDEX.get(str(key).strip(), 0))
        if 0 <= idx < len(self.TAB_TITLES):
            self.notebook.select(idx)

    def _set_single_busy(self, busy: bool) -> None:
        self._single_busy = bool(busy)
        state = "disabled" if busy else "normal"
        self.site_box.configure(state="disabled" if busy else "readonly")
        self.refresh_button.configure(state=state)
        self.delete_button.configure(state=state)
        self.check_button.configure(state=state)
        self.smart_paste_text.configure(state=state)
        self.smart_nofollow_check.configure(state=state)
        self.smart_clear_button.configure(state=state)
        self.smart_add_button.configure(state=state)
        self.add_button.configure(state=state)

    def get_smart_paste_text(self) -> str:
        return self.smart_paste_text.get("1.0", tk.END)

    def set_smart_paste_text(self, text: str) -> None:
        self.smart_paste_text.delete("1.0", tk.END)
        self.smart_paste_text.insert("1.0", text)

    def clear_smart_paste(self) -> None:
        self.smart_paste_text.delete("1.0", tk.END)

    def _format_link_status(self, item: FriendLink) -> str:
        k = link_key(item.link)
        health = self._health_by_key.get(k) if k else None
        if health is not None:
            msg = (health.message or "").strip()
            if not health.reachable:
                if msg:
                    return msg if "不可达" in msg else f"不可达({msg})"
                return "不可达"
            if health.has_backlink is True:
                if msg and msg.upper() != "OK":
                    return msg
                return "正常/有反链"
            if health.has_backlink is False:
                if msg and msg.upper() != "OK":
                    return msg
                return "可达/无反链"
            return msg or "可达"
        if item.status_text:
            return item.status_text
        if item.status is not None:
            return "正常" if item.status == 1 else f"状态 {item.status}"
        return ""

    def _link_status_tags(self, item: FriendLink) -> tuple[str, ...]:
        k = link_key(item.link)
        health = self._health_by_key.get(k) if k else None
        if health is None:
            return ()
        if not health.reachable:
            return ("backlink_error",)
        if health.has_backlink is True:
            if "nofollow" in (health.message or "").casefold():
                return ("backlink_nofollow",)
            return ("backlink_ok",)
        if health.has_backlink is False:
            return ("backlink_missing",)
        return ()

    def _render_single_links(self, links: Sequence[FriendLink]) -> None:
        self._current_links = {}
        for item_id in self.single_tree.get_children():
            self.single_tree.delete(item_id)
        for idx, item in enumerate(links):
            iid = f"fl-{idx}-{item.id}"
            self._current_links[iid] = item
            self.single_tree.insert(
                "",
                tk.END,
                iid=iid,
                values=(
                    item.id if item.id else "",
                    item.title,
                    item.link,
                    "是" if item.nofollow else "否",
                    item.remark,
                    self._format_link_status(item),
                ),
                tags=self._link_status_tags(item),
            )

    def refresh_single_links(self) -> bool:
        if self._single_busy:
            return False
        site = self.current_site()
        if site is None:
            self.single_status_var.set("当前没有可用站点")
            return False

        self._health_by_key.clear()
        self._set_single_busy(True)
        self.single_status_var.set(f"正在加载 {site.name} 的友链列表…")

        def _work() -> list[FriendLink]:
            return self.service.list_links(site)

        def _on_done(links: list[FriendLink]) -> None:
            self._set_single_busy(False)
            self._render_single_links(links)
            self.single_status_var.set(f"{site.name}：共加载 {len(links)} 条友链")

        def _on_error(exc: Exception) -> None:
            self._set_single_busy(False)
            hint = getattr(exc, "hint", "")
            detail = f"{exc}（{hint}）" if hint and hint not in str(exc) else str(exc)
            self.single_status_var.set(f"加载 {site.name} 友链失败：{detail}")

        self.runner.submit(_work, _on_done, _on_error)
        return True

    def smart_add_links(self) -> bool:
        if self._single_busy:
            return False
        site = self.current_site()
        if site is None:
            self._warn("提示", "请先选择一个站点")
            return False

        raw_text = self.get_smart_paste_text().strip()
        if not raw_text:
            self._warn("提示", "请先粘贴 HTML <a> 代码或 标题|网址 列表")
            return False

        specs, errors = parse_batch_link_input(
            raw_text,
            default_nofollow=bool(self.smart_nofollow_var.get()),
        )
        if not specs:
            detail = f"\n{errors[0]}" if errors else ""
            self._warn(
                "未识别到有效友链",
                f"请检查粘贴内容是否为有效的 HTML <a> 代码或 标题|网址 格式{detail}",
            )
            return False

        if errors:
            if not self._confirm(
                "存在无法识别的行",
                f"已识别 {len(specs)} 条有效友链，另有 {len(errors)} 行无法识别（如：{errors[0]}）。\n是否忽略无效行并继续上链？",
            ):
                return False

        self._set_single_busy(True)
        self.single_status_var.set(
            f"正在向 {site.name} 智能上链（已识别 {len(specs)} 条）…"
        )

        def _work() -> tuple[list[LinkOpResult], list[FriendLink] | None]:
            results = self.service.batch_add([site], specs)
            refreshed: list[FriendLink] | None = None
            if any(r.status == LINK_CREATED for r in results):
                try:
                    refreshed = self.service.list_links(site)
                except FriendLinkError:
                    refreshed = None
            return results, refreshed

        def _on_done(payload: tuple[list[LinkOpResult], list[FriendLink] | None]) -> None:
            self._set_single_busy(False)
            results, refreshed = payload
            if refreshed is not None:
                self._render_single_links(refreshed)

            created = sum(1 for r in results if r.status == LINK_CREATED)
            skipped = sum(
                1 for r in results if r.status in {LINK_SKIPPED_EXISTS, LINK_SKIPPED_SELF}
            )
            failed = len(results) - created - skipped
            if created > 0 and failed == 0:
                self.clear_smart_paste()

            summary = f"智能上链完成（识别 {len(specs)} 条）：成功上链 {created} 条，跳过 {skipped} 条"
            if failed:
                summary += f"，失败 {failed} 条"
            self.single_status_var.set(summary)

        def _on_error(exc: Exception) -> None:
            self._set_single_busy(False)
            self.single_status_var.set(f"智能上链出错：{exc}")

        self.runner.submit(_work, _on_done, _on_error)
        return True

    def add_single_link(self) -> bool:
        if self._single_busy:
            return False
        site = self.current_site()
        if site is None:
            self._warn("提示", "请先选择一个站点")
            return False

        title = self.add_title_var.get().strip()
        raw_link = self.add_link_var.get().strip()
        if not raw_link:
            self._warn("提示", "请输入友链网址")
            return False
        try:
            normalized_link = normalize_link_url(raw_link)
        except ValueError as exc:
            self._warn("网址无效", str(exc))
            return False

        if not title:
            title = normalized_link

        spec = FriendLinkSpec(
            title=title,
            link=normalized_link,
            nofollow=bool(self.add_nofollow_var.get()),
            back_link=self.add_back_link_var.get().strip(),
            remark=self.add_remark_var.get().strip(),
        )
        existing_snapshot = list(self._current_links.values())

        self._set_single_busy(True)
        self.single_status_var.set(f"正在向 {site.name} 添加友链 {spec.title}…")

        def _work() -> tuple[LinkOpResult, list[FriendLink] | None]:
            op_res = self.service.add_link(site, spec, existing=existing_snapshot or None)
            refreshed: list[FriendLink] | None = None
            if op_res.status == LINK_CREATED:
                try:
                    refreshed = self.service.list_links(site)
                except FriendLinkError:
                    refreshed = None
            return op_res, refreshed

        def _on_done(payload: tuple[LinkOpResult, list[FriendLink] | None]) -> None:
            self._set_single_busy(False)
            op_res, refreshed = payload
            if refreshed is not None:
                self._render_single_links(refreshed)
            if op_res.status == LINK_CREATED:
                self.add_title_var.set("")
                self.add_link_var.set("")
                self.add_remark_var.set("")
                self.add_back_link_var.set("")
                self.single_status_var.set(f"已添加友链：{op_res.title} ({op_res.link})")
            elif op_res.status == LINK_SKIPPED_EXISTS:
                self.single_status_var.set(f"未重复添加：{op_res.message or '目标友链已存在'}")
            elif op_res.status == LINK_SKIPPED_SELF:
                self.single_status_var.set(f"已跳过：{op_res.message or '不能添加本站域名'}")
            else:
                self.single_status_var.set(f"添加失败：{op_res.message}")

        def _on_error(exc: Exception) -> None:
            self._set_single_busy(False)
            self.single_status_var.set(f"添加友链出错：{exc}")

        self.runner.submit(_work, _on_done, _on_error)
        return True

    def delete_selected_links(self) -> bool:
        if self._single_busy:
            return False
        site = self.current_site()
        if site is None:
            self._warn("提示", "请先选择一个站点")
            return False

        selected_iids = list(self.single_tree.selection())
        targets = [
            self._current_links[iid]
            for iid in selected_iids
            if iid in self._current_links
        ]
        if not targets:
            self._warn("提示", "请先在列表中选中要删除的友链")
            return False

        if not self._confirm(
            "确认删除友链",
            f"确定从站点 {site.name} 删除选中的 {len(targets)} 条友链吗？",
        ):
            return False

        existing_snapshot = list(self._current_links.values())
        self._set_single_busy(True)
        self.single_status_var.set(f"正在删除 {site.name} 的 {len(targets)} 条友链…")

        def _work() -> tuple[list[LinkOpResult], list[FriendLink] | None]:
            results: list[LinkOpResult] = []
            for item in targets:
                results.append(
                    self.service.remove_link(site, item.link, existing=existing_snapshot)
                )
            refreshed: list[FriendLink] | None = None
            try:
                refreshed = self.service.list_links(site)
            except FriendLinkError:
                refreshed = None
            return results, refreshed

        def _on_done(payload: tuple[list[LinkOpResult], list[FriendLink] | None]) -> None:
            self._set_single_busy(False)
            results, refreshed = payload
            if refreshed is not None:
                self._render_single_links(refreshed)
            deleted = sum(1 for r in results if r.status == LINK_DELETED)
            not_found = sum(1 for r in results if r.status == LINK_NOT_FOUND)
            failed = len(results) - deleted - not_found
            self.single_status_var.set(
                f"删除完成：已删 {deleted}，未找到 {not_found}，失败 {failed}"
            )

        def _on_error(exc: Exception) -> None:
            self._set_single_busy(False)
            self.single_status_var.set(f"删除友链出错：{exc}")

        self.runner.submit(_work, _on_done, _on_error)
        return True

    def check_single_backlinks(self) -> bool:
        if self._single_busy:
            return False
        site = self.current_site()
        if site is None:
            self._warn("提示", "请先选择一个站点")
            return False

        links_snapshot = list(self._current_links.values())
        self._single_cancel_event = threading.Event()
        cancel_event = self._single_cancel_event
        self._set_single_busy(True)
        self.single_status_var.set(f"正在检测 {site.name} 的友链与反链状态…")

        def _work() -> tuple[list[FriendLink], list[LinkHealth]]:
            target_links = links_snapshot if links_snapshot else self.service.list_links(site)
            health_list = self.service.check_backlinks(
                site,
                target_links,
                cancel_event=cancel_event,
            )
            return target_links, health_list

        def _on_done(payload: tuple[list[FriendLink], list[LinkHealth]]) -> None:
            self._set_single_busy(False)
            self._single_cancel_event = None
            target_links, health_list = payload
            self._health_by_key = {}
            for h in health_list:
                k = link_key(h.link)
                if k:
                    self._health_by_key[k] = h
            self._render_single_links(target_links)
            ok_back = sum(1 for h in health_list if h.reachable and h.has_backlink is True)
            nofollow_back = sum(
                1
                for h in health_list
                if h.reachable
                and h.has_backlink is True
                and "nofollow" in (h.message or "").casefold()
            )
            no_back = sum(1 for h in health_list if h.reachable and h.has_backlink is False)
            down = sum(1 for h in health_list if not h.reachable)
            nf_note = f"（含 nofollow {nofollow_back}）" if nofollow_back else ""
            self.single_status_var.set(
                f"反链检测完成（共 {len(health_list)} 条）：正常有反链 {ok_back}{nf_note}，无反链 {no_back}，不可达 {down}"
            )

        def _on_error(exc: Exception) -> None:
            self._set_single_busy(False)
            self._single_cancel_event = None
            self.single_status_var.set(f"反链检测失败：{exc}")

        self.runner.submit(_work, _on_done, _on_error)
        return True

    def open_current_site_admin(self) -> bool:
        site = self.current_site()
        if site is None:
            self._warn("提示", "请先选择一个站点")
            return False
        if self.jump_service is None:
            self._warn("提示", "当前未配置后台跳转服务")
            return False
        return open_admin_backends(
            self,
            [site],
            self.jump_service,
            self.runner,
            status_callback=self.single_status_var.set,
            settings=self.settings,
            confirm_fn=self._confirm_fn,
            warn_fn=self._warn_fn,
        )

    def on_close(self) -> None:
        if self._single_cancel_event is not None:
            self._single_cancel_event.set()
        self.batch_panel.cancel()
        self.crosslink_panel.cancel()
        self.runner.shutdown()
        try:
            if self.winfo_exists():
                self.destroy()
        except tk.TclError:
            pass
