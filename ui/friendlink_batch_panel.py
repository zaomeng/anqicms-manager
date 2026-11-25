"""Batch friend-link add/remove panel (统一上下链) for FriendLinkCenter."""

from __future__ import annotations

import csv
import threading
import tkinter as tk
from collections.abc import Callable, Sequence
from tkinter import filedialog, messagebox, ttk

from core.friendlink_service import (
    FriendLinkService,
    link_key,
    normalize_link_url,
    parse_batch_link_input,
)
from core.integration_settings import IntegrationSettings
from models.entities import SiteConfig
from models.sitegroup import (
    LINK_CANCELLED,
    LINK_CREATED,
    LINK_DELETED,
    LINK_FAILED,
    LINK_NOT_FOUND,
    LINK_SKIPPED_EXISTS,
    LINK_SKIPPED_SELF,
    FriendLinkSpec,
    LinkOpResult,
)
from ui.site_picker import SitePicker
from ui.ui_async import BackgroundRunner

_ACTION_LABELS = {
    "add": "上链",
    "remove": "下链",
    "check": "检测",
}

_STATUS_LABELS = {
    LINK_CREATED: "已添加",
    LINK_SKIPPED_EXISTS: "已存在(跳过)",
    LINK_SKIPPED_SELF: "本站(跳过)",
    LINK_DELETED: "已删除",
    LINK_NOT_FOUND: "未找到",
    LINK_FAILED: "失败",
    LINK_CANCELLED: "已取消",
}


class FriendLinkBatchPanel(ttk.Frame):
    """Batch add/remove friend links across multiple selected sites."""

    def __init__(
        self,
        master,
        sites_provider: Callable[[], Sequence[SiteConfig]] | Sequence[SiteConfig],
        service: FriendLinkService,
        runner: BackgroundRunner,
        settings: IntegrationSettings | None = None,
        *,
        confirm_fn: Callable[[str, str], bool] | None = None,
        warn_fn: Callable[[str, str], None] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(master, **kwargs)
        self._sites_provider = sites_provider
        self.service = service
        self.runner = runner
        self.settings = settings or IntegrationSettings()
        self._confirm_fn = confirm_fn
        self._warn_fn = warn_fn

        self._cancel_event: threading.Event | None = None
        self._running = False
        self._results: list[LinkOpResult] = []
        self._expected_total = 0
        self._done_count = 0

        self.skip_existing_var = tk.BooleanVar(value=True)
        self.nofollow_var = tk.BooleanVar(
            value=bool(self.settings.friendlink.default_nofollow)
        )
        self.status_var = tk.StringVar(
            value="请选择目标站点，并在下方直接粘贴 HTML <a> 友链代码或 标题|网址[|nofollow][|备注]"
        )
        self.progress_var = tk.DoubleVar(value=0.0)

        self._build()

    @property
    def is_running(self) -> bool:
        return self._running

    def _warn(self, title: str, msg: str) -> None:
        if self._warn_fn is not None:
            self._warn_fn(title, msg)
        else:
            messagebox.showwarning(title, msg, parent=self)

    def _confirm(self, title: str, msg: str) -> bool:
        if self._confirm_fn is not None:
            return bool(self._confirm_fn(title, msg))
        return bool(messagebox.askyesno(title, msg, parent=self))

    def _build(self) -> None:
        pane = ttk.Panedwindow(self, orient=tk.VERTICAL)
        pane.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # Top: SitePicker
        site_frame = ttk.LabelFrame(pane, text="1. 选择目标站点", padding=6)
        self.site_picker = SitePicker(
            site_frame,
            self._sites_provider,
            height=6,
            show_status=True,
        )
        self.site_picker.pack(fill=tk.BOTH, expand=True)
        pane.add(site_frame, weight=2)

        # Middle: Input & Actions
        input_frame = ttk.LabelFrame(
            pane,
            text='2. 友链列表（支持直接粘贴 HTML <a> 友链代码如 <li><a href="...">...</a></li>，或 标题|网址[|nofollow][|备注]）',
            padding=6,
        )
        self.input_hint_label = ttk.Label(
            input_frame,
            text='提示：可直接粘贴单行/多行 HTML <a> 代码（批量上链与批量下链均自动提取网址），或 标题|网址、网址|标题、TAB/逗号分隔、纯网址（# 开头为注释）',
            style="Muted.TLabel",
        )
        self.input_hint_label.pack(fill=tk.X, pady=(0, 4))

        text_wrap = ttk.Frame(input_frame)
        text_wrap.pack(fill=tk.BOTH, expand=True)

        self.input_text = tk.Text(text_wrap, height=5, wrap=tk.NONE, undo=True)
        text_yscroll = ttk.Scrollbar(
            text_wrap, orient=tk.VERTICAL, command=self.input_text.yview
        )
        self.input_text.configure(yscrollcommand=text_yscroll.set)
        self.input_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        text_yscroll.pack(side=tk.RIGHT, fill=tk.Y)

        action_bar = ttk.Frame(input_frame)
        action_bar.pack(fill=tk.X, pady=(6, 0))

        self.skip_existing_check = ttk.Checkbutton(
            action_bar,
            text="跳过已存在",
            variable=self.skip_existing_var,
        )
        self.skip_existing_check.pack(side=tk.LEFT, padx=(0, 10))

        self.nofollow_check = ttk.Checkbutton(
            action_bar,
            text="默认 nofollow",
            variable=self.nofollow_var,
        )
        self.nofollow_check.pack(side=tk.LEFT, padx=(0, 12))

        self.parse_button = ttk.Button(
            action_bar,
            text="识别预览",
            style="Compact.TButton",
            command=self.preview_input,
        )
        self.parse_button.pack(side=tk.LEFT, padx=(0, 6))

        self.batch_add_button = ttk.Button(
            action_bar,
            text="批量上链",
            style="Accent.TButton",
            command=self.start_batch_add,
        )
        self.batch_add_button.pack(side=tk.LEFT, padx=(0, 6))

        self.batch_remove_button = ttk.Button(
            action_bar,
            text="批量下链",
            command=self.start_batch_remove,
        )
        self.batch_remove_button.pack(side=tk.LEFT, padx=(0, 6))

        self.stop_button = ttk.Button(
            action_bar,
            text="停止",
            command=self.cancel,
            state="disabled",
        )
        self.stop_button.pack(side=tk.LEFT, padx=(0, 6))

        self.export_button = ttk.Button(
            action_bar,
            text="导出 CSV",
            style="Compact.TButton",
            command=self.export_csv,
        )
        self.export_button.pack(side=tk.RIGHT)

        pane.add(input_frame, weight=2)

        # Bottom: Progress & Results Treeview
        result_frame = ttk.LabelFrame(pane, text="3. 执行结果", padding=6)

        prog_row = ttk.Frame(result_frame)
        prog_row.pack(fill=tk.X, pady=(0, 6))
        self.progressbar = ttk.Progressbar(
            prog_row,
            orient=tk.HORIZONTAL,
            mode="determinate",
            variable=self.progress_var,
            maximum=100.0,
        )
        self.progressbar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        self.status_label = ttk.Label(
            prog_row, textvariable=self.status_var, style="Muted.TLabel"
        )
        self.status_label.pack(side=tk.RIGHT)

        tree_wrap = ttk.Frame(result_frame)
        tree_wrap.pack(fill=tk.BOTH, expand=True)

        columns = (
            ("site", "站点", 110, False),
            ("title", "标题", 120, False),
            ("link", "网址", 220, True),
            ("action", "动作", 60, False),
            ("status", "结果", 100, False),
            ("message", "说明", 220, True),
        )
        self.result_tree = ttk.Treeview(
            tree_wrap,
            columns=tuple(c[0] for c in columns),
            show="headings",
            height=8,
        )
        for col_id, heading, width, stretch in columns:
            self.result_tree.heading(col_id, text=heading)
            self.result_tree.column(col_id, width=width, minwidth=48, stretch=stretch)

        res_yscroll = ttk.Scrollbar(
            tree_wrap, orient=tk.VERTICAL, command=self.result_tree.yview
        )
        self.result_tree.configure(yscrollcommand=res_yscroll.set)
        self.result_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        res_yscroll.pack(side=tk.RIGHT, fill=tk.Y)

        pane.add(result_frame, weight=3)

    def reload_sites(self) -> None:
        self.site_picker.reload()

    def get_input_text(self) -> str:
        return self.input_text.get("1.0", tk.END)

    def set_input_text(self, text: str) -> None:
        self.input_text.delete("1.0", tk.END)
        self.input_text.insert("1.0", text)

    def parse_input(self) -> tuple[list[FriendLinkSpec], list[str]]:
        return parse_batch_link_input(
            self.get_input_text(),
            default_nofollow=bool(self.nofollow_var.get()),
        )

    def preview_input(self) -> tuple[list[FriendLinkSpec], list[str]]:
        specs, errors = self.parse_input()
        if not self._running:
            self._results = []
            self._done_count = 0
            self._expected_total = 0
            self.progress_var.set(0.0)
            for item_id in self.result_tree.get_children():
                self.result_tree.delete(item_id)
            for spec in specs:
                self.result_tree.insert(
                    "",
                    tk.END,
                    values=(
                        "—",
                        spec.title,
                        spec.link,
                        "预览",
                        "nofollow" if spec.nofollow else "dofollow",
                        spec.remark or "已识别待操作",
                    ),
                )
        if not specs and not errors:
            self.status_var.set("请输入至少一条友链或粘贴 HTML <a> 代码")
            return specs, errors
        msg = f"识别完成：有效 {len(specs)} 条"
        if errors:
            msg += f"，无法识别 {len(errors)} 行（{errors[0]}）"
        self.status_var.set(msg)
        return specs, errors

    def _set_running(self, running: bool) -> None:
        self._running = bool(running)
        state = "disabled" if running else "normal"
        self.site_picker.set_enabled(not running)
        self.input_text.configure(state=state)
        self.skip_existing_check.configure(state=state)
        self.nofollow_check.configure(state=state)
        self.parse_button.configure(state=state)
        self.batch_add_button.configure(state=state)
        self.batch_remove_button.configure(state=state)
        self.export_button.configure(state=state)
        self.stop_button.configure(state="normal" if running else "disabled")

    def _clear_results(self) -> None:
        self._results = []
        self._done_count = 0
        self._expected_total = 0
        self.progress_var.set(0.0)
        for item_id in self.result_tree.get_children():
            self.result_tree.delete(item_id)
        self.site_picker.clear_statuses()

    def _append_result(self, res: LinkOpResult) -> None:
        self._results.append(res)
        self._done_count += 1
        status_label = _STATUS_LABELS.get(res.status, res.status)
        action_label = _ACTION_LABELS.get(res.action, res.action)
        self.result_tree.insert(
            "",
            tk.END,
            values=(
                res.site_name,
                res.title,
                res.link,
                action_label,
                status_label,
                res.message,
            ),
        )
        self.site_picker.set_row_status(res.site_id or res.site_name, status_label)
        if self._expected_total > 0:
            pct = min(100.0, (self._done_count / self._expected_total) * 100.0)
            self.progress_var.set(pct)
            self.status_var.set(
                f"执行中：{self._done_count} / {self._expected_total}（{res.site_name}：{status_label}）"
            )

    def _format_summary(self, results: Sequence[LinkOpResult], action_label: str) -> str:
        counts: dict[str, int] = {}
        for r in results:
            counts[r.status] = counts.get(r.status, 0) + 1
        parts = [f"{action_label}完成，共 {len(results)} 条"]
        for key, label in (
            (LINK_CREATED, "已添加"),
            (LINK_DELETED, "已删除"),
            (LINK_SKIPPED_EXISTS, "已存在"),
            (LINK_SKIPPED_SELF, "跳过本站"),
            (LINK_NOT_FOUND, "未找到"),
            (LINK_FAILED, "失败"),
            (LINK_CANCELLED, "已取消"),
        ):
            if counts.get(key, 0):
                parts.append(f"{label} {counts[key]}")
        return "，".join(parts)

    def start_batch_add(self) -> bool:
        if self._running:
            return False
        sites = self.site_picker.selected_sites()
        if not sites:
            self._warn("提示", "请先勾选至少一个目标站点")
            return False
        specs, errors = self.parse_input()
        if not specs:
            detail = f"\n{errors[0]}" if errors else ""
            self._warn("提示", f"未解析到有效友链，请检查输入格式{detail}")
            return False
        if errors:
            if not self._confirm(
                "存在格式错误行",
                f"解析到 {len(specs)} 条有效友链，但有 {len(errors)} 行格式无效（如：{errors[0]}）。\n是否忽略错误行并继续批量上链？",
            ):
                return False

        self._clear_results()
        self._expected_total = len(sites) * len(specs)
        self._cancel_event = threading.Event()
        self._set_running(True)
        self.status_var.set(f"正在批量上链（{len(sites)} 站 × {len(specs)} 条）…")

        cancel_event = self._cancel_event
        progress_cb = self.runner.make_progress(self._append_result)

        def _work() -> list[LinkOpResult]:
            return self.service.batch_add(
                sites,
                specs,
                progress=progress_cb,
                cancel_event=cancel_event,
            )

        def _on_done(results: list[LinkOpResult]) -> None:
            self._set_running(False)
            self._cancel_event = None
            if not self._results and results:
                for res in results:
                    self._append_result(res)
            self.progress_var.set(100.0 if results else 0.0)
            self.status_var.set(self._format_summary(results, "批量上链"))

        def _on_error(exc: Exception) -> None:
            self._set_running(False)
            self._cancel_event = None
            self.status_var.set(f"批量上链出错：{exc}")

        self.runner.submit(_work, _on_done, _on_error)
        return True

    def start_batch_remove(self) -> bool:
        if self._running:
            return False
        sites = self.site_picker.selected_sites()
        if not sites:
            self._warn("提示", "请先勾选至少一个目标站点")
            return False
        specs, errors = self.parse_input()
        urls = [spec.link for spec in specs]
        if not urls:
            # Also allow raw URL lines if parse_batch_link_input returned nothing
            seen: set[str] = set()
            for raw_line in self.get_input_text().splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    norm = normalize_link_url(line)
                except ValueError:
                    continue
                k = link_key(norm)
                if k and k not in seen:
                    seen.add(k)
                    urls.append(norm)
        if not urls:
            detail = f"\n{errors[0]}" if errors else ""
            self._warn("提示", f"未解析到可下链的网址{detail}")
            return False

        if not self._confirm(
            "确认批量下链",
            f"即将从 {len(sites)} 个站点删除 {len(urls)} 条友链，确定继续吗？",
        ):
            return False

        self._clear_results()
        self._expected_total = len(sites) * len(urls)
        self._cancel_event = threading.Event()
        self._set_running(True)
        self.status_var.set(f"正在批量下链（{len(sites)} 站 × {len(urls)} 条）…")

        cancel_event = self._cancel_event
        progress_cb = self.runner.make_progress(self._append_result)

        def _work() -> list[LinkOpResult]:
            return self.service.batch_remove(
                sites,
                urls,
                progress=progress_cb,
                cancel_event=cancel_event,
            )

        def _on_done(results: list[LinkOpResult]) -> None:
            self._set_running(False)
            self._cancel_event = None
            if not self._results and results:
                for res in results:
                    self._append_result(res)
            self.progress_var.set(100.0 if results else 0.0)
            self.status_var.set(self._format_summary(results, "批量下链"))

        def _on_error(exc: Exception) -> None:
            self._set_running(False)
            self._cancel_event = None
            self.status_var.set(f"批量下链出错：{exc}")

        self.runner.submit(_work, _on_done, _on_error)
        return True

    def cancel(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()
            self.status_var.set("正在停止剩余任务…")

    def export_csv(self, file_path: str | None = None) -> str | None:
        if not self._results:
            self._warn("提示", "当前没有可导出的执行结果")
            return None
        path = file_path or filedialog.asksaveasfilename(
            parent=self,
            title="导出批量友链结果",
            defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
        )
        if not path:
            return None
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(["站点", "标题", "网址", "动作", "结果", "说明"])
                for res in self._results:
                    writer.writerow(
                        [
                            res.site_name,
                            res.title,
                            res.link,
                            _ACTION_LABELS.get(res.action, res.action),
                            _STATUS_LABELS.get(res.status, res.status),
                            res.message,
                        ]
                    )
        except OSError as exc:
            self._warn("导出失败", f"无法写入 CSV 文件：{exc}")
            return None
        self.status_var.set(f"已导出 {len(self._results)} 条结果")
        return path
