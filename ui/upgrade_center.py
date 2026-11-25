"""Unified version detection and staged upgrade center window (AnQiCMS 统一升级中心)."""

from __future__ import annotations

import inspect
import threading
import tkinter as tk
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from core.anqicms_mcp_client import AnqiMcpClient, probe_site
from core.integration_settings import IntegrationSettings
from core.mcp_contracts import AUTH_HINT, classify_scope_error
from core.upgrade_service import UpgradeService, scope_hint
from models.entities import SiteConfig
from models.sitegroup import (
    UPGRADE_ALREADY_LATEST,
    UPGRADE_AUTH_FAILED,
    UPGRADE_CANCELLED,
    UPGRADE_FAILED,
    UPGRADE_NO_TOOL,
    UPGRADE_SCOPE_DISABLED,
    UPGRADE_SKIPPED,
    UPGRADE_TIMEOUT,
    UPGRADE_UNREACHABLE,
    UPGRADE_UPGRADED,
    McpProbeResult,
    SiteVersionInfo,
    UpgradeProgress,
    UpgradeResult,
)
from ui.site_credentials_panel import format_probe_summary
from ui.site_picker import SitePicker
from ui.ui_async import BackgroundRunner

_MIN_CONCURRENCY = 1
_MAX_CONCURRENCY = 5

_STAGE_LABELS: dict[str, str] = {
    "connecting": "正在连接",
    "checking": "正在检测版本",
    "upgrading": "正在执行升级",
    "restarting": "等待站点重启",
    "verifying": "正在验证恢复",
    "done": "处理完成",
}

_NO_UPGRADE_TOOL_HINT = (
    "该站未通过 MCP 暴露升级工具：请在后台 AI 统一配置→接口开放范围 勾选系统升级"
)
_NO_CHECK_TOOL_HINT = (
    "该站未通过 MCP 暴露版本检测工具：请在后台 设置→AI 统一配置→接口开放范围 检查接口权限"
)

_STATUS_LABELS: dict[str, str] = {
    UPGRADE_UPGRADED: "升级成功",
    UPGRADE_ALREADY_LATEST: "已是最新",
    UPGRADE_SCOPE_DISABLED: "权限未开放 (scope_disabled)",
    UPGRADE_NO_TOOL: "无升级工具 (no_tool)",
    UPGRADE_AUTH_FAILED: "鉴权失败 (auth_failed)",
    UPGRADE_UNREACHABLE: "站点不可达",
    UPGRADE_TIMEOUT: "恢复超时",
    UPGRADE_FAILED: "升级失败",
    UPGRADE_CANCELLED: "已取消",
    UPGRADE_SKIPPED: "已跳过",
}

_STATUS_DEFAULT_HINTS: dict[str, str] = {
    UPGRADE_SCOPE_DISABLED: scope_hint("upgrade"),
    UPGRADE_NO_TOOL: _NO_UPGRADE_TOOL_HINT,
    UPGRADE_AUTH_FAILED: AUTH_HINT,
}

_WARN_STATUSES = frozenset(
    {
        UPGRADE_SCOPE_DISABLED,
        UPGRADE_NO_TOOL,
        UPGRADE_AUTH_FAILED,
    }
)
_ERROR_STATUSES = frozenset(
    {
        UPGRADE_UNREACHABLE,
        UPGRADE_TIMEOUT,
        UPGRADE_FAILED,
    }
)
_OK_STATUSES = frozenset(
    {
        UPGRADE_UPGRADED,
        UPGRADE_ALREADY_LATEST,
    }
)


def _call_with_supported_kwargs(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Invoke *func* keeping only keyword arguments it accepts (or all if ``**kwargs`` exists)."""
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return func(*args, **kwargs)
    params = sig.parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return func(*args, **kwargs)
    filtered = {k: v for k, v in kwargs.items() if k in params}
    return func(*args, **filtered)


def _combine_message_and_hint(message: str, hint: str) -> str:
    msg = (message or "").strip()
    h = (hint or "").strip()
    if msg and h and h not in msg:
        return f"{msg} — {h}"
    return msg or h


class UpgradeCenter(tk.Toplevel):
    """Dedicated Toplevel window for batch version checks and staged AnqiCMS upgrades."""

    WINDOW_TITLE = "AnQiCMS 统一升级中心"

    def __init__(
        self,
        master,
        sites_provider: Callable[[], Sequence[SiteConfig]] | Sequence[SiteConfig],
        *,
        config_path: str | Path | None = None,
        settings: IntegrationSettings | None = None,
        service: UpgradeService | None = None,
        initial_site_names: Sequence[str] | None = None,
        probe_fn: Callable[[SiteConfig, IntegrationSettings], McpProbeResult] | None = None,
        confirm_fn: Callable[[str, str], bool] | None = None,
        warn_fn: Callable[[str, str], None] | None = None,
        info_fn: Callable[[str, str], None] | None = None,
    ) -> None:
        super().__init__(master)
        self.title(self.WINDOW_TITLE)
        self.geometry("1080x760")
        self.minsize(900, 620)

        self._sites_provider = sites_provider
        self.config_path = Path(config_path) if config_path is not None else Path("config.ini")
        self.settings = settings or IntegrationSettings()
        if service is not None:
            self.service = service
        else:
            self.service = UpgradeService(
                self.settings,
                mcp_client_factory=lambda site: AnqiMcpClient.for_site(site, self.settings),
            )
        self._probe_fn = probe_fn or (lambda site, cfg: probe_site(site, cfg))
        self._confirm_fn = confirm_fn
        self._warn_fn = warn_fn
        self._info_fn = info_fn

        self.runner = BackgroundRunner(self)
        self._active_task: str | None = None
        self._upgrade_running = False
        self._cancel_event: threading.Event | None = None
        self._closing_confirmed = False

        self._sites: list[SiteConfig] = []
        self._site_by_key: dict[str, SiteConfig] = {}
        self._alias_to_key: dict[str, str] = {}
        self._version_info_by_key: dict[str, SiteVersionInfo] = {}
        self._upgrade_result_by_key: dict[str, UpgradeResult] = {}
        self._row_detail_by_key: dict[str, str] = {}
        self._completed_keys: set[str] = set()
        self._batch_total = 0

        default_concurrency = max(
            _MIN_CONCURRENCY,
            min(_MAX_CONCURRENCY, int(getattr(self.settings.upgrade, "max_concurrency", 3) or 3)),
        )
        self.concurrency_var = tk.IntVar(value=default_concurrency)
        self.canary_var = tk.BooleanVar(value=True)
        self.stop_on_failure_var = tk.BooleanVar(value=True)
        self.progress_var = tk.DoubleVar(value=0.0)
        self.status_var = tk.StringVar(
            value="请勾选目标站点，可先执行“检测版本”或“测试 MCP”，确认后再执行升级"
        )
        self.detail_var = tk.StringVar(
            value="提示：双击下方站点结果行可查看完整状态说明与后台配置指引"
        )

        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.reload_sites()
        if initial_site_names:
            self.select_sites(initial_site_names)

    # ------------------------------------------------------------------ #
    # Dialog helpers
    # ------------------------------------------------------------------ #
    def _confirm(self, title: str, message: str) -> bool:
        if self._confirm_fn is not None:
            return bool(self._confirm_fn(title, message))
        return bool(messagebox.askyesno(title, message, parent=self))

    def _warn(self, title: str, message: str) -> None:
        if self._warn_fn is not None:
            self._warn_fn(title, message)
        else:
            messagebox.showwarning(title, message, parent=self)

    def _info(self, title: str, message: str) -> None:
        if self._info_fn is not None:
            self._info_fn(title, message)
        else:
            messagebox.showinfo(title, message, parent=self)

    @staticmethod
    def _site_key(site: SiteConfig) -> str:
        return (getattr(site, "site_id", "") or "").strip() or getattr(site, "name", "")

    def _resolve_key(self, site_id_or_name: str) -> str | None:
        if not site_id_or_name:
            return None
        if site_id_or_name in self._site_by_key:
            return site_id_or_name
        return self._alias_to_key.get(site_id_or_name)

    def _fetch_sites(self) -> list[SiteConfig]:
        raw = self._sites_provider() if callable(self._sites_provider) else self._sites_provider
        return list(raw or ())

    def _get_concurrency(self) -> int:
        try:
            raw = int(self.concurrency_var.get())
        except (tk.TclError, ValueError, TypeError):
            raw = int(getattr(self.settings.upgrade, "max_concurrency", 3) or 3)
        clamped = max(_MIN_CONCURRENCY, min(_MAX_CONCURRENCY, raw))
        try:
            self.concurrency_var.set(clamped)
        except tk.TclError:
            pass
        return clamped

    # ------------------------------------------------------------------ #
    # UI Construction
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        main_frame = ttk.Frame(self, padding=8)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 1. Top SitePicker section
        picker_box = ttk.LabelFrame(main_frame, text="选择目标站点", padding=8)
        picker_box.pack(fill=tk.BOTH, expand=False, pady=(0, 6))

        self.site_picker = SitePicker(
            picker_box,
            self._sites_provider,
            height=7,
            show_status=True,
        )
        self.site_picker.pack(fill=tk.BOTH, expand=True)
        self.picker = self.site_picker

        # 2. Action controls & staged upgrade options bar
        controls_box = ttk.LabelFrame(main_frame, text="升级控制与策略", padding=8)
        controls_box.pack(fill=tk.X, pady=(0, 6))

        btn_row = ttk.Frame(controls_box)
        btn_row.pack(fill=tk.X)

        self.check_button = ttk.Button(
            btn_row,
            text="检测版本",
            command=self.check_selected_versions,
        )
        self.check_button.pack(side=tk.LEFT, padx=(0, 6))

        self.upgrade_button = ttk.Button(
            btn_row,
            text="升级选中站点",
            style="Accent.TButton",
            command=self.upgrade_selected_sites,
        )
        self.upgrade_button.pack(side=tk.LEFT, padx=(0, 6))

        self.test_mcp_button = ttk.Button(
            btn_row,
            text="测试 MCP",
            command=self.test_selected_mcp,
        )
        self.test_mcp_button.pack(side=tk.LEFT, padx=(0, 6))

        self.stop_button = ttk.Button(
            btn_row,
            text="停止",
            command=self.stop_active_task,
            state="disabled",
        )
        self.stop_button.pack(side=tk.LEFT, padx=(0, 14))

        ttk.Separator(btn_row, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=(0, 12))

        ttk.Label(btn_row, text="并发数(1-5)：").pack(side=tk.LEFT, padx=(0, 4))
        self.concurrency_spinbox = ttk.Spinbox(
            btn_row,
            from_=_MIN_CONCURRENCY,
            to=_MAX_CONCURRENCY,
            textvariable=self.concurrency_var,
            width=4,
        )
        self.concurrency_spinbox.pack(side=tk.LEFT, padx=(0, 12))

        self.canary_check = ttk.Checkbutton(
            btn_row,
            text="金丝雀：先升级第 1 站验证",
            variable=self.canary_var,
        )
        self.canary_check.pack(side=tk.LEFT, padx=(0, 12))

        self.stop_on_failure_check = ttk.Checkbutton(
            btn_row,
            text="失败即停",
            variable=self.stop_on_failure_var,
        )
        self.stop_on_failure_check.pack(side=tk.LEFT, padx=(0, 8))

        # 3. Results Treeview
        results_box = ttk.LabelFrame(main_frame, text="版本检测与升级进度", padding=8)
        results_box.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        tree_wrap = ttk.Frame(results_box)
        tree_wrap.pack(fill=tk.BOTH, expand=True)

        columns: tuple[tuple[str, str, int, bool, str], ...] = (
            ("site", "站点", 120, False, tk.W),
            ("group", "分组", 90, False, tk.W),
            ("current_version", "当前版本", 100, False, tk.W),
            ("latest_version", "最新版本", 100, False, tk.W),
            ("upgradable", "可升级", 70, False, tk.CENTER),
            ("status", "状态", 170, False, tk.W),
            ("detail", "提示/详情", 340, True, tk.W),
        )
        self.result_tree = ttk.Treeview(
            tree_wrap,
            columns=tuple(c[0] for c in columns),
            show="headings",
            selectmode="browse",
            height=10,
        )
        self.results_tree = self.result_tree
        for col_id, heading, width, stretch, anchor in columns:
            self.result_tree.heading(col_id, text=heading)
            self.result_tree.column(
                col_id,
                width=width,
                minwidth=min(width, 48),
                stretch=stretch,
                anchor=anchor,
            )

        self.result_tree.tag_configure("ok", foreground="#15803d")
        self.result_tree.tag_configure("upgradable", foreground="#1d4ed8")
        self.result_tree.tag_configure("warn", foreground="#b45309", background="#fffbeb")
        self.result_tree.tag_configure("error", foreground="#b91c1c", background="#fef2f2")
        self.result_tree.tag_configure("muted", foreground="#64748b")

        yscroll = ttk.Scrollbar(tree_wrap, orient=tk.VERTICAL, command=self.result_tree.yview)
        self.result_tree.configure(yscrollcommand=yscroll.set)
        self.result_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.result_tree.bind("<<TreeviewSelect>>", self._on_result_row_selected)
        self.result_tree.bind("<Double-1>", self._on_result_row_double_click)

        # 4. Detail bar & progress/status bar
        footer = ttk.Frame(main_frame)
        footer.pack(fill=tk.X)

        self.detail_label = ttk.Label(
            footer,
            textvariable=self.detail_var,
            style="Muted.TLabel",
            wraplength=1020,
            justify=tk.LEFT,
        )
        self.detail_label.pack(fill=tk.X, pady=(0, 4))

        progress_row = ttk.Frame(footer)
        progress_row.pack(fill=tk.X)

        self.progressbar = ttk.Progressbar(
            progress_row,
            orient=tk.HORIZONTAL,
            mode="determinate",
            maximum=100.0,
            variable=self.progress_var,
            length=220,
        )
        self.progressbar.pack(side=tk.RIGHT, padx=(8, 0))

        self.status_label = ttk.Label(
            progress_row,
            textvariable=self.status_var,
        )
        self.status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

    # ------------------------------------------------------------------ #
    # Site loading & result table rendering
    # ------------------------------------------------------------------ #
    def reload_sites(self) -> None:
        """Reload sites into ``SitePicker`` and synchronize the results table."""
        self.site_picker.reload()
        self._sites = self._fetch_sites()
        self._site_by_key = {}
        self._alias_to_key = {}
        for site in self._sites:
            key = self._site_key(site)
            self._site_by_key[key] = site
            if getattr(site, "site_id", ""):
                self._alias_to_key[site.site_id] = key
            if getattr(site, "name", ""):
                self._alias_to_key.setdefault(site.name, key)

        existing_children = set(self.result_tree.get_children())
        valid_keys: set[str] = set()
        for site in self._sites:
            key = self._site_key(site)
            valid_keys.add(key)
            group = (getattr(site, "site_group", "") or "").strip()
            if key not in existing_children:
                self.result_tree.insert(
                    "",
                    tk.END,
                    iid=key,
                    values=(site.name, group, "-", "-", "-", "待检测", ""),
                )
            else:
                vals = list(self.result_tree.item(key, "values"))
                if len(vals) >= 7:
                    vals[0] = site.name
                    vals[1] = group
                    self.result_tree.item(key, values=tuple(vals))

        for stale in existing_children - valid_keys:
            self.result_tree.delete(stale)

    def select_sites(self, site_ids: Iterable[str]) -> None:
        """Select the specified site IDs or site names in ``SitePicker``."""
        self.site_picker.set_selected(site_ids)

    def _update_result_row(
        self,
        site_id_or_name: str,
        *,
        current_version: str | None = None,
        latest_version: str | None = None,
        upgradable: str | None = None,
        status: str | None = None,
        detail: str | None = None,
        tag: str | None = None,
    ) -> None:
        key = self._resolve_key(site_id_or_name)
        if key is None:
            return
        site = self._site_by_key.get(key)
        if site is None:
            return
        group = (getattr(site, "site_group", "") or "").strip()

        if self.result_tree.exists(key):
            old_vals = list(self.result_tree.item(key, "values"))
        else:
            old_vals = [site.name, group, "-", "-", "-", "待检测", ""]
            self.result_tree.insert("", tk.END, iid=key, values=tuple(old_vals))

        while len(old_vals) < 7:
            old_vals.append("")

        new_vals = (
            site.name,
            group,
            old_vals[2] if current_version is None else (current_version or "-"),
            old_vals[3] if latest_version is None else (latest_version or "-"),
            old_vals[4] if upgradable is None else (upgradable or "-"),
            old_vals[5] if status is None else status,
            old_vals[6] if detail is None else detail,
        )
        kwargs: dict[str, Any] = {"values": new_vals}
        if tag is not None:
            kwargs["tags"] = (tag,) if tag else ()
        self.result_tree.item(key, **kwargs)

        if detail is not None:
            self._row_detail_by_key[key] = (
                f"[{site.name}] 状态：{new_vals[5]} | 当前版本：{new_vals[2]} | "
                f"最新版本：{new_vals[3]} | 详情：{new_vals[6] or '无'}"
            )
        if status is not None:
            self.site_picker.set_row_status(key, status)

    def get_row_detail(self, site_id_or_name: str) -> str:
        key = self._resolve_key(site_id_or_name)
        if key is None:
            return ""
        if key in self._row_detail_by_key:
            return self._row_detail_by_key[key]
        if self.result_tree.exists(key):
            vals = self.result_tree.item(key, "values")
            if len(vals) >= 7:
                return f"[{vals[0]}] 状态：{vals[5]} | 详情：{vals[6] or '无'}"
        return ""

    def _on_result_row_selected(self, _event=None) -> None:
        selection = self.result_tree.selection()
        if not selection:
            return
        detail = self.get_row_detail(selection[0])
        if detail:
            self.detail_var.set(detail)

    def _on_result_row_double_click(self, event) -> None:
        row_id = self.result_tree.identify_row(event.y) if event is not None else ""
        if not row_id:
            selection = self.result_tree.selection()
            row_id = selection[0] if selection else ""
        if row_id:
            self.show_row_detail(row_id)

    def show_row_detail(self, site_id_or_name: str | None = None) -> str:
        target = site_id_or_name
        if not target:
            selection = self.result_tree.selection()
            target = selection[0] if selection else ""
        if not target:
            return ""
        detail = self.get_row_detail(target)
        if detail:
            self.detail_var.set(detail)
            if self._info_fn is not None:
                self._info("站点升级详情", detail)
        return detail

    # ------------------------------------------------------------------ #
    # Task state management
    # ------------------------------------------------------------------ #
    def is_upgrade_running(self) -> bool:
        """Return True if a batch upgrade task is currently active."""
        return bool(self._upgrade_running and self._active_task == "upgrade")

    def is_running(self) -> bool:
        """Return True if any background task (check, upgrade, MCP test) is active."""
        return bool(self._active_task is not None or self.runner.busy)

    def _set_busy(self, busy: bool, *, task: str | None = None) -> None:
        self._active_task = task if busy else None
        self._upgrade_running = bool(busy and task == "upgrade")
        btn_state = "disabled" if busy else "normal"
        self.site_picker.set_enabled(not busy)
        self.check_button.configure(state=btn_state)
        self.upgrade_button.configure(state=btn_state)
        self.test_mcp_button.configure(state=btn_state)
        self.concurrency_spinbox.configure(state=btn_state)
        self.canary_check.configure(state=btn_state)
        self.stop_on_failure_check.configure(state=btn_state)
        self.stop_button.configure(state="normal" if busy else "disabled")

    def _reset_batch_progress(self, sites: Sequence[SiteConfig], waiting_text: str) -> None:
        self._completed_keys.clear()
        self._batch_total = max(1, len(sites))
        self.progress_var.set(0.0)
        for site in sites:
            key = self._site_key(site)
            self._update_result_row(
                key,
                status=waiting_text,
                detail="",
                tag="muted",
            )

    def _mark_site_completed(self, site_id_or_name: str) -> None:
        key = self._resolve_key(site_id_or_name)
        if key is None:
            return
        self._completed_keys.add(key)
        pct = min(100.0, (len(self._completed_keys) / max(1, self._batch_total)) * 100.0)
        self.progress_var.set(pct)

    # ------------------------------------------------------------------ #
    # Progress & result application on Tk thread
    # ------------------------------------------------------------------ #
    def _on_upgrade_progress(self, progress: UpgradeProgress) -> None:
        if progress is None:
            return
        site_name = getattr(progress, "site_name", "") or ""
        stage = getattr(progress, "stage", "") or ""
        message = getattr(progress, "message", "") or ""
        stage_label = _STAGE_LABELS.get(stage, stage or "处理中")

        if stage != "done":
            self._update_result_row(
                site_name,
                status=stage_label,
                detail=message,
                tag="upgradable",
            )
            self.status_var.set(
                f"[{site_name}] {stage_label}：{message}" if message else f"[{site_name}] {stage_label}"
            )
        else:
            self._mark_site_completed(site_name)

    def _apply_version_info(self, info: SiteVersionInfo) -> None:
        key = self._resolve_key(info.site_id) or self._resolve_key(info.site_name)
        if key is None:
            return
        self._version_info_by_key[key] = info
        self._mark_site_completed(key)

        current_ver = info.current_version or "-"
        latest_ver = info.latest_version or "-"
        if info.upgrade_available is True:
            upgradable_str = "是"
        elif info.upgrade_available is False:
            upgradable_str = "否"
        else:
            upgradable_str = "-"

        msg_and_hint = f"{info.message} {info.hint}".strip()
        lowered = msg_and_hint.casefold()

        if not info.reachable:
            if info.message == "已取消":
                status_text = "已取消"
                hint_text = ""
                tag = "muted"
            elif (
                info.hint == AUTH_HINT
                or "鉴权" in msg_and_hint
                or "认证" in msg_and_hint
                or "token" in lowered
                or "401" in lowered
                or "403" in lowered
            ):
                status_text = _STATUS_LABELS[UPGRADE_AUTH_FAILED]
                hint_text = info.hint or AUTH_HINT
                tag = "warn"
            else:
                status_text = _STATUS_LABELS[UPGRADE_UNREACHABLE]
                hint_text = info.hint
                tag = "error"
        else:
            if classify_scope_error(msg_and_hint):
                status_text = _STATUS_LABELS[UPGRADE_SCOPE_DISABLED]
                hint_text = info.hint or scope_hint("upgrade")
                tag = "warn"
            elif not info.check_tool and info.source == "mcp" and "未找到版本检测工具" in info.message:
                status_text = "无检测工具 (no_tool)"
                hint_text = info.hint or _NO_CHECK_TOOL_HINT
                tag = "warn"
            elif info.upgrade_available is True:
                if not info.upgrade_tool and info.source == "mcp":
                    status_text = "可升级（无升级工具 no_tool）"
                    hint_text = info.hint or _NO_UPGRADE_TOOL_HINT
                    tag = "warn"
                else:
                    status_text = "可升级"
                    hint_text = info.hint
                    tag = "upgradable"
            elif info.upgrade_available is False:
                status_text = _STATUS_LABELS[UPGRADE_ALREADY_LATEST]
                hint_text = info.hint
                tag = "ok"
            else:
                status_text = info.message or "已检测"
                hint_text = info.hint
                tag = ""

        detail_text = _combine_message_and_hint(info.message, hint_text)
        self._update_result_row(
            key,
            current_version=current_ver,
            latest_version=latest_ver,
            upgradable=upgradable_str,
            status=status_text,
            detail=detail_text,
            tag=tag,
        )

    def _apply_upgrade_result(self, result: UpgradeResult) -> None:
        key = self._resolve_key(result.site_id) or self._resolve_key(result.site_name)
        if key is None:
            return
        self._upgrade_result_by_key[key] = result
        self._mark_site_completed(key)

        status_code = result.status
        status_text = _STATUS_LABELS.get(status_code, status_code or "未知状态")
        default_hint = _STATUS_DEFAULT_HINTS.get(status_code, "")
        effective_hint = result.hint or default_hint
        detail_text = _combine_message_and_hint(result.message, effective_hint)

        if status_code in _OK_STATUSES:
            tag = "ok"
        elif status_code in _WARN_STATUSES:
            tag = "warn"
        elif status_code in _ERROR_STATUSES:
            tag = "error"
        else:
            tag = "muted"

        current_ver: str | None = None
        latest_ver: str | None = None
        upgradable_str: str | None = None

        if status_code == UPGRADE_UPGRADED:
            if result.to_version:
                current_ver = result.to_version
                latest_ver = result.to_version
            upgradable_str = "否"
        elif status_code == UPGRADE_ALREADY_LATEST:
            if result.from_version:
                current_ver = result.from_version
            if result.to_version:
                latest_ver = result.to_version
            upgradable_str = "否"
        else:
            if result.from_version:
                current_ver = result.from_version
            if result.to_version:
                latest_ver = result.to_version

        self._update_result_row(
            key,
            current_version=current_ver,
            latest_version=latest_ver,
            upgradable=upgradable_str,
            status=status_text,
            detail=detail_text,
            tag=tag,
        )

    # ------------------------------------------------------------------ #
    # Action 1: Check Versions (检测版本)
    # ------------------------------------------------------------------ #
    def check_selected_versions(self) -> bool:
        if self.is_running():
            return False
        sites = self.site_picker.selected_sites()
        if not sites:
            self.status_var.set("请先勾选至少一个站点")
            self._warn("提示", "请先勾选至少一个站点")
            return False

        concurrency = self._get_concurrency()
        cancel_event = threading.Event()
        self._cancel_event = cancel_event
        self._reset_batch_progress(sites, "等待检测…")
        self._set_busy(True, task="check")
        self.status_var.set(f"正在检测 {len(sites)} 个站点的版本信息（并发 {concurrency}）…")

        progress_cb = self.runner.make_progress(self._on_upgrade_progress)

        def _work() -> list[SiteVersionInfo]:
            if hasattr(self.service, "check_versions"):
                return list(
                    _call_with_supported_kwargs(
                        self.service.check_versions,
                        sites,
                        progress=progress_cb,
                        cancel_event=cancel_event,
                        max_workers=concurrency,
                    )
                )
            out: list[SiteVersionInfo] = []
            for site in sites:
                if cancel_event.is_set():
                    out.append(
                        SiteVersionInfo(
                            site_name=site.name,
                            site_id=site.site_id,
                            reachable=False,
                            message="已取消",
                        )
                    )
                else:
                    out.append(self.service.check_version(site))
            return out

        def _on_done(infos: list[SiteVersionInfo]) -> None:
            self._cancel_event = None
            self._set_busy(False)
            for info in infos:
                self._apply_version_info(info)
            self.progress_var.set(100.0)

            upgradable_count = sum(1 for i in infos if i.reachable and i.upgrade_available is True)
            latest_count = sum(1 for i in infos if i.reachable and i.upgrade_available is False)
            warn_or_err = sum(
                1
                for i in infos
                if (not i.reachable)
                or classify_scope_error(f"{i.message} {i.hint}")
                or (not i.check_tool and i.source == "mcp")
            )
            self.status_var.set(
                f"版本检测完成（共 {len(infos)} 站）：可升级 {upgradable_count}，"
                f"已是最新 {latest_count}，异常/需配置 {warn_or_err}"
            )

        def _on_error(exc: Exception) -> None:
            self._cancel_event = None
            self._set_busy(False)
            self.status_var.set(f"版本检测出错：{exc}")

        self.runner.submit(_work, _on_done, _on_error)
        return True

    check_versions = check_selected_versions

    # ------------------------------------------------------------------ #
    # Action 2: Upgrade Selected Sites (升级选中站点)
    # ------------------------------------------------------------------ #
    def upgrade_selected_sites(self) -> bool:
        if self.is_running():
            return False
        sites = self.site_picker.selected_sites()
        if not sites:
            self.status_var.set("请先勾选至少一个站点")
            self._warn("提示", "请先勾选至少一个站点")
            return False

        concurrency = self._get_concurrency()
        canary_first = bool(self.canary_var.get())
        stop_on_failure = bool(self.stop_on_failure_var.get())

        preview_names = "、".join(s.name for s in sites[:5])
        if len(sites) > 5:
            preview_names += f" 等共 {len(sites)} 个站点"

        confirm_message = (
            f"即将对所选 {len(sites)} 个站点执行系统升级（{preview_names}）。\n\n"
            f"• 并发数：{concurrency}\n"
            f"• 金丝雀（先升级第 1 站验证）：{'开启' if canary_first else '关闭'}\n"
            f"• 失败即停：{'开启' if stop_on_failure else '关闭'}\n\n"
            "注意：升级过程中站点会自动重启并短暂中断访问，建议提前做好备份。\n"
            "确定立即开始升级吗？"
        )
        if not self._confirm("确认升级站点", confirm_message):
            self.status_var.set("已取消升级操作")
            return False

        cancel_event = threading.Event()
        self._cancel_event = cancel_event
        self._reset_batch_progress(sites, "等待升级…")
        self._set_busy(True, task="upgrade")
        self.status_var.set(
            f"正在升级 {len(sites)} 个站点（并发 {concurrency}，"
            f"{'金丝雀开启' if canary_first else '无金丝雀'}）…"
        )

        progress_cb = self.runner.make_progress(self._on_upgrade_progress)
        result_cb = self.runner.make_progress(self._apply_upgrade_result)

        def _work() -> list[UpgradeResult]:
            return list(
                _call_with_supported_kwargs(
                    self.service.upgrade_sites,
                    sites,
                    confirmed=True,
                    max_workers=concurrency,
                    canary_first=canary_first,
                    stop_on_failure=stop_on_failure,
                    progress=progress_cb,
                    result_callback=result_cb,
                    cancel_event=cancel_event,
                )
            )

        def _on_done(results: list[UpgradeResult]) -> None:
            self._cancel_event = None
            self._set_busy(False)
            for res in results:
                self._apply_upgrade_result(res)
            self.progress_var.set(100.0)

            upgraded = sum(1 for r in results if r.status == UPGRADE_UPGRADED)
            already = sum(1 for r in results if r.status == UPGRADE_ALREADY_LATEST)
            blocked = sum(
                1
                for r in results
                if r.status in (_WARN_STATUSES | _ERROR_STATUSES)
            )
            skipped_or_cancelled = sum(
                1
                for r in results
                if r.status in {UPGRADE_SKIPPED, UPGRADE_CANCELLED}
            )
            self.status_var.set(
                f"升级任务完成（共 {len(results)} 站）：升级成功 {upgraded}，"
                f"已是最新 {already}，失败/受阻 {blocked}，跳过/取消 {skipped_or_cancelled}"
            )

        def _on_error(exc: Exception) -> None:
            self._cancel_event = None
            self._set_busy(False)
            self.status_var.set(f"升级任务失败：{exc}")

        self.runner.submit(_work, _on_done, _on_error)
        return True

    upgrade_selected = upgrade_selected_sites

    # ------------------------------------------------------------------ #
    # Action 3: Test MCP (测试 MCP)
    # ------------------------------------------------------------------ #
    def _apply_probe_result(self, site: SiteConfig, result: McpProbeResult) -> None:
        key = self._site_key(site)
        self._mark_site_completed(key)
        summary = format_probe_summary(result)
        if result.ok:
            if result.upgrade_tools:
                status_text = "MCP 正常（含升级工具）"
                detail_text = f"{summary}（升级工具：{', '.join(result.upgrade_tools)}）"
                tag = "ok"
            else:
                status_text = "MCP 正常（无升级工具 no_tool）"
                detail_text = f"{summary} — {_NO_UPGRADE_TOOL_HINT}"
                tag = "warn"
        else:
            lowered = f"{result.message} {result.hint}".casefold()
            if "token" in lowered or "认证" in lowered or "鉴权" in lowered or "401" in lowered or "403" in lowered:
                status_text = _STATUS_LABELS[UPGRADE_AUTH_FAILED]
            else:
                status_text = "MCP 连接失败"
            detail_text = summary
            tag = "warn"

        self._update_result_row(
            key,
            status=status_text,
            detail=detail_text,
            tag=tag,
        )

    def test_selected_mcp(self) -> bool:
        if self.is_running():
            return False
        sites = self.site_picker.selected_sites()
        if not sites:
            self.status_var.set("请先勾选至少一个站点")
            self._warn("提示", "请先勾选至少一个站点")
            return False

        cancel_event = threading.Event()
        self._cancel_event = cancel_event
        self._reset_batch_progress(sites, "等待探测 MCP…")
        self._set_busy(True, task="probe")
        self.status_var.set(f"正在测试 {len(sites)} 个站点的 MCP 连接与升级工具权限…")

        on_single_probe = self.runner.make_progress(self._apply_probe_result)

        def _work() -> list[tuple[SiteConfig, McpProbeResult]]:
            results: list[tuple[SiteConfig, McpProbeResult]] = []
            for site in sites:
                if cancel_event.is_set():
                    res = McpProbeResult(
                        site_name=site.name,
                        ok=False,
                        message="已取消",
                    )
                else:
                    res = self._probe_fn(site, self.settings)
                results.append((site, res))
                on_single_probe(site, res)
            return results

        def _on_done(pairs: list[tuple[SiteConfig, McpProbeResult]]) -> None:
            self._cancel_event = None
            self._set_busy(False)
            self.progress_var.set(100.0)
            ok_with_up = sum(1 for _, r in pairs if r.ok and r.upgrade_tools)
            ok_no_up = sum(1 for _, r in pairs if r.ok and not r.upgrade_tools)
            failed = sum(1 for _, r in pairs if not r.ok)
            self.status_var.set(
                f"MCP 测试完成（共 {len(pairs)} 站）：支持升级 {ok_with_up}，"
                f"未暴露升级工具 {ok_no_up}，连接失败 {failed}"
            )

        def _on_error(exc: Exception) -> None:
            self._cancel_event = None
            self._set_busy(False)
            self.status_var.set(f"MCP 测试出错：{exc}")

        self.runner.submit(_work, _on_done, _on_error)
        return True

    test_mcp = test_selected_mcp

    # ------------------------------------------------------------------ #
    # Action 4: Stop / Cancel (停止) & Window Close
    # ------------------------------------------------------------------ #
    def stop_active_task(self) -> bool:
        if self._cancel_event is None:
            return False
        self._cancel_event.set()
        self.stop_button.configure(state="disabled")
        self.status_var.set("正在停止任务（未开始的站点将标记为已取消，已发出的请求将等待完成）…")
        return True

    cancel = stop_active_task

    def _confirm_close_if_upgrading(self) -> bool:
        if not self._closing_confirmed and self.is_upgrade_running():
            return self._confirm(
                "确认关闭",
                "当前有站点升级任务正在进行中，关闭窗口将取消后续尚未开始的升级任务。\n确定要取消并关闭吗？",
            )
        return True

    def on_close(self) -> bool:
        if not self._confirm_close_if_upgrading():
            return False
        self._closing_confirmed = True
        if self._cancel_event is not None:
            self._cancel_event.set()
        self._upgrade_running = False
        self._active_task = None
        self.runner.shutdown()
        try:
            if self.winfo_exists():
                super().destroy()
        except tk.TclError:
            pass
        return True

    def destroy(self) -> None:
        self._closing_confirmed = True
        if self._cancel_event is not None:
            self._cancel_event.set()
        self._upgrade_running = False
        self._active_task = None
        self.runner.shutdown()
        try:
            super().destroy()
        except tk.TclError:
            pass
