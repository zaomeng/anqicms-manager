"""Cross-link (站群互链) planning, preview, execution and rollback panel."""

from __future__ import annotations

import threading
import tkinter as tk
from collections.abc import Callable, Sequence
from tkinter import messagebox, ttk

from core.friendlink_ledger import FriendLinkLedger
from core.friendlink_service import FriendLinkService
from core.integration_settings import IntegrationSettings
from models.entities import SiteConfig
from models.sitegroup import (
    CROSSLINK_HUB,
    CROSSLINK_MESH,
    CROSSLINK_RING,
    LINK_CANCELLED,
    LINK_CREATED,
    LINK_DELETED,
    LINK_FAILED,
    LINK_NOT_FOUND,
    LINK_PLANNED,
    LINK_SKIPPED_EXISTS,
    LINK_SKIPPED_SELF,
    CrossLinkItem,
    CrossLinkPlan,
    CrossLinkReport,
    LedgerPlanSummary,
    LinkOpResult,
)
from ui.site_picker import SitePicker
from ui.ui_async import BackgroundRunner

_MODE_LABELS = {
    CROSSLINK_MESH: "两两全互链",
    CROSSLINK_HUB: "主从互链",
    CROSSLINK_RING: "轮链",
}

_STATE_LABELS = {
    LINK_PLANNED: "待创建",
    LINK_CREATED: "已创建",
    LINK_SKIPPED_EXISTS: "已存在(跳过)",
    LINK_SKIPPED_SELF: "本站(跳过)",
    LINK_DELETED: "已删除",
    LINK_NOT_FOUND: "未找到",
    LINK_FAILED: "失败",
    LINK_CANCELLED: "已取消",
}


class CrossLinkPanel(ttk.Frame):
    """Cross-link topology planner, dry-run previewer, executor and ledger rollback UI."""

    def __init__(
        self,
        master,
        sites_provider: Callable[[], Sequence[SiteConfig]] | Sequence[SiteConfig],
        service: FriendLinkService,
        ledger: FriendLinkLedger,
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
        self.ledger = ledger
        self.runner = runner
        self.settings = settings or IntegrationSettings()
        self._confirm_fn = confirm_fn
        self._warn_fn = warn_fn

        self._running = False
        self._cancel_event: threading.Event | None = None
        self._previewed_plan: CrossLinkPlan | None = None
        self._preview_sites: list[SiteConfig] = []
        self._preview_fingerprint: str = ""
        self._hub_label_to_id: dict[str, str] = {}
        self._history_by_id: dict[str, LedgerPlanSummary] = {}
        self._expected_total = 0
        self._done_count = 0

        self.mode_var = tk.StringVar(value=CROSSLINK_MESH)
        self.hub_var = tk.StringVar(value="")
        self.bidirectional_var = tk.BooleanVar(value=True)
        self.nofollow_var = tk.BooleanVar(
            value=bool(self.settings.friendlink.default_nofollow)
        )
        self.remark_var = tk.StringVar(
            value=str(self.settings.friendlink.default_remark or "站群互链")
        )
        self.progress_var = tk.DoubleVar(value=0.0)
        self.status_var = tk.StringVar(
            value="请选择至少 2 个站点，先“生成预览计划”，确认无误后再“执行互链”"
        )

        self._build()
        self.mode_var.trace_add("write", self._on_config_changed)
        self.hub_var.trace_add("write", self._on_config_changed)
        self.bidirectional_var.trace_add("write", self._on_config_changed)

        self._sync_hub_choices(self.site_picker.selected_sites())
        self._sync_mode_controls()
        self._sync_execute_button()
        self.reload_history()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def previewed_plan(self) -> CrossLinkPlan | None:
        return self._previewed_plan

    @staticmethod
    def _site_id(site: SiteConfig) -> str:
        sid = (getattr(site, "site_id", "") or "").strip()
        return sid or (getattr(site, "name", "") or "").strip()

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

        # 1. Site Picker
        site_frame = ttk.LabelFrame(pane, text="1. 选择参与互链的站点（至少 2 个）", padding=6)
        self.site_picker = SitePicker(
            site_frame,
            self._sites_provider,
            height=6,
            show_status=True,
            on_selection_changed=self._on_selection_changed,
        )
        self.site_picker.pack(fill=tk.BOTH, expand=True)
        pane.add(site_frame, weight=2)

        # 2. Mode & Options & Actions
        cfg_frame = ttk.LabelFrame(pane, text="2. 互链拓扑与选项", padding=6)

        row1 = ttk.Frame(cfg_frame)
        row1.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(row1, text="模式：").pack(side=tk.LEFT)
        self.mesh_radio = ttk.Radiobutton(
            row1,
            text="两两全互链 (Mesh)",
            variable=self.mode_var,
            value=CROSSLINK_MESH,
        )
        self.mesh_radio.pack(side=tk.LEFT, padx=(0, 10))
        self.hub_radio = ttk.Radiobutton(
            row1,
            text="主从互链 (Hub-Spoke)",
            variable=self.mode_var,
            value=CROSSLINK_HUB,
        )
        self.hub_radio.pack(side=tk.LEFT, padx=(0, 10))
        self.ring_radio = ttk.Radiobutton(
            row1,
            text="轮链 (Ring)",
            variable=self.mode_var,
            value=CROSSLINK_RING,
        )
        self.ring_radio.pack(side=tk.LEFT, padx=(0, 12))

        ttk.Label(row1, text="主站：").pack(side=tk.LEFT)
        self.hub_box = ttk.Combobox(
            row1,
            textvariable=self.hub_var,
            state="disabled",
            width=20,
        )
        self.hub_box.pack(side=tk.LEFT, padx=(0, 8))

        row2 = ttk.Frame(cfg_frame)
        row2.pack(fill=tk.X, pady=(0, 6))
        self.bidirectional_check = ttk.Checkbutton(
            row2,
            text="双向互链",
            variable=self.bidirectional_var,
        )
        self.bidirectional_check.pack(side=tk.LEFT, padx=(0, 10))
        self.nofollow_check = ttk.Checkbutton(
            row2,
            text="nofollow",
            variable=self.nofollow_var,
        )
        self.nofollow_check.pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(row2, text="备注：").pack(side=tk.LEFT)
        self.remark_entry = ttk.Entry(row2, textvariable=self.remark_var, width=20)
        self.remark_entry.pack(side=tk.LEFT, padx=(0, 12))

        self.preview_button = ttk.Button(
            row2,
            text="生成预览计划",
            command=self.start_preview,
        )
        self.preview_button.pack(side=tk.LEFT, padx=(0, 6))

        self.execute_button = ttk.Button(
            row2,
            text="执行互链",
            style="Accent.TButton",
            command=self.start_execute,
            state="disabled",
        )
        self.execute_button.pack(side=tk.LEFT, padx=(0, 6))

        self.stop_button = ttk.Button(
            row2,
            text="停止",
            command=self.cancel,
            state="disabled",
        )
        self.stop_button.pack(side=tk.LEFT, padx=(0, 6))

        self.unlink_button = ttk.Button(
            row2,
            text="解除所选站点间互链",
            style="Compact.TButton",
            command=self.start_unlink_between,
        )
        self.unlink_button.pack(side=tk.RIGHT)

        prog_row = ttk.Frame(cfg_frame)
        prog_row.pack(fill=tk.X)
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

        pane.add(cfg_frame, weight=0)

        # 3. Plan Preview Treeview
        plan_frame = ttk.LabelFrame(pane, text="3. 互链计划预览与执行结果", padding=6)
        plan_wrap = ttk.Frame(plan_frame)
        plan_wrap.pack(fill=tk.BOTH, expand=True)

        plan_cols = (
            ("source", "源站点", 110, False),
            ("target", "目标站点", 110, False),
            ("title", "锚文本", 120, False),
            ("link", "目标网址", 210, True),
            ("state", "状态", 100, False),
            ("message", "说明", 200, True),
        )
        self.plan_tree = ttk.Treeview(
            plan_wrap,
            columns=tuple(c[0] for c in plan_cols),
            show="headings",
            height=7,
        )
        for col_id, heading, width, stretch in plan_cols:
            self.plan_tree.heading(col_id, text=heading)
            self.plan_tree.column(col_id, width=width, minwidth=48, stretch=stretch)
        plan_yscroll = ttk.Scrollbar(
            plan_wrap, orient=tk.VERTICAL, command=self.plan_tree.yview
        )
        self.plan_tree.configure(yscrollcommand=plan_yscroll.set)
        self.plan_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        plan_yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        pane.add(plan_frame, weight=3)

        # 4. History / Ledger
        hist_frame = ttk.LabelFrame(pane, text="4. 互链历史记录与一键回滚", padding=6)
        hist_toolbar = ttk.Frame(hist_frame)
        hist_toolbar.pack(fill=tk.X, pady=(0, 4))

        self.refresh_history_button = ttk.Button(
            hist_toolbar,
            text="刷新记录",
            style="Compact.TButton",
            command=self.reload_history,
        )
        self.refresh_history_button.pack(side=tk.LEFT, padx=(0, 6))

        self.rollback_button = ttk.Button(
            hist_toolbar,
            text="一键回滚选中计划",
            command=self.start_rollback_selected,
        )
        self.rollback_button.pack(side=tk.LEFT)

        hist_wrap = ttk.Frame(hist_frame)
        hist_wrap.pack(fill=tk.BOTH, expand=True)

        hist_cols = (
            ("plan_id", "计划ID", 110, False),
            ("mode", "模式", 90, False),
            ("created_at", "时间", 145, False),
            ("sites", "涉及站点", 220, True),
            ("created", "已建", 60, False),
            ("rolled_back", "已回滚", 65, False),
        )
        self.history_tree = ttk.Treeview(
            hist_wrap,
            columns=tuple(c[0] for c in hist_cols),
            show="headings",
            selectmode="browse",
            height=5,
        )
        for col_id, heading, width, stretch in hist_cols:
            self.history_tree.heading(col_id, text=heading)
            self.history_tree.column(col_id, width=width, minwidth=48, stretch=stretch)
        hist_yscroll = ttk.Scrollbar(
            hist_wrap, orient=tk.VERTICAL, command=self.history_tree.yview
        )
        self.history_tree.configure(yscrollcommand=hist_yscroll.set)
        self.history_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        hist_yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        pane.add(hist_frame, weight=2)

    def reload_sites(self) -> None:
        self.site_picker.reload()
        self._sync_hub_choices(self.site_picker.selected_sites())
        self._sync_execute_button()

    def _resolved_hub_site_id(self) -> str:
        raw = self.hub_var.get().strip()
        if not raw:
            return ""
        return self._hub_label_to_id.get(raw, raw)

    def current_selection_fingerprint(self) -> str:
        """Compute the exact fingerprint matching ``CrossLinkPlan.fingerprint()``."""
        selected = self.site_picker.selected_sites()
        mode = self.mode_var.get().strip()
        hub_id = self._resolved_hub_site_id() if mode == CROSSLINK_HUB else ""
        bidir = "1" if self.bidirectional_var.get() else "0"
        site_ids = sorted(self._site_id(s) for s in selected if self._site_id(s))
        return "|".join((mode, hub_id, bidir, ",".join(site_ids)))

    def _is_preview_valid(self) -> bool:
        if self._running or self._previewed_plan is None or not self._preview_fingerprint:
            return False
        return self._preview_fingerprint == self.current_selection_fingerprint()

    def _sync_hub_choices(self, selected_sites: Sequence[SiteConfig]) -> None:
        labels: list[str] = []
        mapping: dict[str, str] = {}
        for site in selected_sites:
            sid = self._site_id(site)
            label = site.name or sid
            if label in mapping:
                label = f"{label} ({sid})"
            labels.append(label)
            mapping[label] = sid
        self._hub_label_to_id = mapping
        self.hub_box.configure(values=tuple(labels))
        current = self.hub_var.get().strip()
        if current not in mapping:
            self.hub_var.set(labels[0] if labels else "")

    def _sync_mode_controls(self) -> None:
        mode = self.mode_var.get().strip()
        if self._running:
            self.hub_box.configure(state="disabled")
            self.bidirectional_check.configure(state="disabled")
            return
        if mode == CROSSLINK_HUB:
            self.hub_box.configure(state="readonly")
        else:
            self.hub_box.configure(state="disabled")
        if mode == CROSSLINK_MESH:
            if not self.bidirectional_var.get():
                self.bidirectional_var.set(True)
            self.bidirectional_check.configure(state="disabled")
        else:
            self.bidirectional_check.configure(state="normal")

    def _sync_execute_button(self) -> None:
        if self._is_preview_valid():
            self.execute_button.configure(state="normal")
        else:
            self.execute_button.configure(state="disabled")

    def _on_selection_changed(self, selected_sites: list[SiteConfig]) -> None:
        self._sync_hub_choices(selected_sites)
        self._sync_execute_button()

    def _on_config_changed(self, *_args) -> None:
        self._sync_mode_controls()
        self._sync_execute_button()

    def _set_running(self, running: bool) -> None:
        self._running = bool(running)
        state = "disabled" if running else "normal"
        self.site_picker.set_enabled(not running)
        self.mesh_radio.configure(state=state)
        self.hub_radio.configure(state=state)
        self.ring_radio.configure(state=state)
        self.nofollow_check.configure(state=state)
        self.remark_entry.configure(state=state)
        self.preview_button.configure(state=state)
        self.unlink_button.configure(state=state)
        self.refresh_history_button.configure(state=state)
        self.rollback_button.configure(state=state)
        self.stop_button.configure(state="normal" if running else "disabled")
        self._sync_mode_controls()
        self._sync_execute_button()

    def _clear_plan_tree(self) -> None:
        for item_id in self.plan_tree.get_children():
            self.plan_tree.delete(item_id)

    def _render_plan_items(self, items: Sequence[CrossLinkItem]) -> None:
        self._clear_plan_tree()
        for item in items:
            self.plan_tree.insert(
                "",
                tk.END,
                values=(
                    item.source_name,
                    item.target_name,
                    item.spec.title,
                    item.spec.link,
                    _STATE_LABELS.get(item.state, item.state),
                    item.message,
                ),
            )

    def _append_op_result_row(self, res: LinkOpResult) -> None:
        self._done_count += 1
        label = _STATE_LABELS.get(res.status, res.status)
        self.plan_tree.insert(
            "",
            tk.END,
            values=(
                res.site_name,
                "",
                res.title,
                res.link,
                label,
                res.message,
            ),
        )
        self.site_picker.set_row_status(res.site_id or res.site_name, label)
        if self._expected_total > 0:
            self.progress_var.set(
                min(100.0, (self._done_count / self._expected_total) * 100.0)
            )

    def start_preview(self) -> bool:
        if self._running:
            return False
        sites = self.site_picker.selected_sites()
        if len(sites) < 2:
            self._warn("提示", "站群互链至少需要选择 2 个站点")
            return False

        mode = self.mode_var.get().strip()
        hub_site_id = self._resolved_hub_site_id() if mode == CROSSLINK_HUB else ""
        bidirectional = bool(self.bidirectional_var.get())
        nofollow = bool(self.nofollow_var.get())
        remark = self.remark_var.get().strip()

        try:
            plan = self.service.plan_crosslinks(
                sites,
                mode,
                hub_site_id=hub_site_id,
                bidirectional=bidirectional,
                nofollow=nofollow,
                remark=remark,
            )
        except ValueError as exc:
            self._warn("参数无效", str(exc))
            return False

        self._previewed_plan = None
        self._preview_fingerprint = ""
        self._preview_sites = list(sites)
        self._clear_plan_tree()
        self.site_picker.clear_statuses()
        self._expected_total = len(plan.items)
        self._done_count = 0
        self.progress_var.set(0.0)
        self._cancel_event = threading.Event()
        self._set_running(True)
        self.status_var.set(f"正在生成互链预览计划（共 {len(plan.items)} 条）…")

        cancel_event = self._cancel_event

        def _on_progress(item: CrossLinkItem) -> None:
            self._done_count += 1
            label = _STATE_LABELS.get(item.state, item.state)
            self.plan_tree.insert(
                "",
                tk.END,
                values=(
                    item.source_name,
                    item.target_name,
                    item.spec.title,
                    item.spec.link,
                    label,
                    item.message,
                ),
            )
            if self._expected_total > 0:
                self.progress_var.set(
                    min(100.0, (self._done_count / self._expected_total) * 100.0)
                )

        progress_cb = self.runner.make_progress(_on_progress)

        def _work() -> CrossLinkPlan:
            return self.service.preview_plan(
                plan,
                sites,
                progress=progress_cb,
                cancel_event=cancel_event,
            )

        def _on_done(previewed: CrossLinkPlan) -> None:
            self._set_running(False)
            self._cancel_event = None
            self._render_plan_items(previewed.items)
            self.progress_var.set(100.0)
            counts = previewed.counts
            if counts.get(LINK_CANCELLED, 0) > 0:
                self._previewed_plan = None
                self._preview_fingerprint = ""
                self.status_var.set("预览已取消")
            else:
                self._previewed_plan = previewed
                self._preview_fingerprint = previewed.fingerprint()
                planned = counts.get(LINK_PLANNED, 0)
                exists = counts.get(LINK_SKIPPED_EXISTS, 0)
                failed = counts.get(LINK_FAILED, 0)
                self.status_var.set(
                    f"预览完成：共 {len(previewed.items)} 条（待创建 {planned} / 已存在 {exists} / 失败 {failed}）"
                )
            self._sync_execute_button()

        def _on_error(exc: Exception) -> None:
            self._set_running(False)
            self._cancel_event = None
            self._previewed_plan = None
            self._preview_fingerprint = ""
            self.status_var.set(f"生成预览失败：{exc}")
            self._sync_execute_button()

        self.runner.submit(_work, _on_done, _on_error)
        return True

    def start_execute(self) -> bool:
        if not self._is_preview_valid() or self._previewed_plan is None:
            self._warn("提示", "请先生成并核对当前选择的互链预览计划")
            self._sync_execute_button()
            return False

        plan = self._previewed_plan
        counts = plan.counts
        planned = counts.get(LINK_PLANNED, 0)
        exists = counts.get(LINK_SKIPPED_EXISTS, 0)
        failed = counts.get(LINK_FAILED, 0)
        prompt = (
            f"即将执行互链计划 {plan.plan_id}（{_MODE_LABELS.get(plan.mode, plan.mode)}）：\n"
            f"- 计划总数：{len(plan.items)} 条\n"
            f"- 预计新创建：{planned} 条\n"
            f"- 已存在跳过：{exists} 条\n"
            f"- 预览失败：{failed} 条\n\n"
            "执行后将写入本地回滚台账，确定继续吗？"
        )
        if not self._confirm("确认执行互链", prompt):
            return False

        sites = list(self._preview_sites or self.site_picker.selected_sites())
        self._clear_plan_tree()
        self.site_picker.clear_statuses()
        self._expected_total = len(plan.items)
        self._done_count = 0
        self.progress_var.set(0.0)
        self._cancel_event = threading.Event()
        self._set_running(True)
        self.status_var.set(f"正在执行互链计划 {plan.plan_id}…")

        cancel_event = self._cancel_event
        progress_cb = self.runner.make_progress(self._append_op_result_row)

        def _work() -> CrossLinkReport:
            return self.service.execute_plan(
                plan,
                sites,
                ledger=self.ledger,
                progress=progress_cb,
                cancel_event=cancel_event,
            )

        def _on_done(report: CrossLinkReport) -> None:
            self._set_running(False)
            self._cancel_event = None
            self._render_plan_items(report.plan.items)
            self.progress_var.set(100.0)
            res_counts: dict[str, int] = {}
            for r in report.results:
                res_counts[r.status] = res_counts.get(r.status, 0) + 1
            self.status_var.set(
                f"执行完成（计划 {report.plan.plan_id}）："
                f"已建 {res_counts.get(LINK_CREATED, 0)}，"
                f"跳过 {res_counts.get(LINK_SKIPPED_EXISTS, 0) + res_counts.get(LINK_SKIPPED_SELF, 0)}，"
                f"失败 {res_counts.get(LINK_FAILED, 0)}，"
                f"取消 {res_counts.get(LINK_CANCELLED, 0)}"
            )
            self.reload_history()

        def _on_error(exc: Exception) -> None:
            self._set_running(False)
            self._cancel_event = None
            self.status_var.set(f"执行互链失败：{exc}")
            self.reload_history()

        self.runner.submit(_work, _on_done, _on_error)
        return True

    def start_unlink_between(self) -> bool:
        if self._running:
            return False
        sites = self.site_picker.selected_sites()
        if len(sites) < 2:
            self._warn("提示", "请先勾选至少 2 个站点再解除它们之间的互链")
            return False
        if not self._confirm(
            "确认解除互链",
            f"将检查并删除所选 {len(sites)} 个站点之间相互指向的友链，确定继续吗？",
        ):
            return False

        self._clear_plan_tree()
        self.site_picker.clear_statuses()
        self._expected_total = len(sites)
        self._done_count = 0
        self.progress_var.set(0.0)
        self._cancel_event = threading.Event()
        self._set_running(True)
        self.status_var.set(f"正在解除 {len(sites)} 个站点间的互链…")

        cancel_event = self._cancel_event
        progress_cb = self.runner.make_progress(self._append_op_result_row)

        def _work() -> list[LinkOpResult]:
            return self.service.unlink_between(
                sites,
                progress=progress_cb,
                cancel_event=cancel_event,
            )

        def _on_done(results: list[LinkOpResult]) -> None:
            self._set_running(False)
            self._cancel_event = None
            if not self.plan_tree.get_children() and results:
                for res in results:
                    self._append_op_result_row(res)
            self.progress_var.set(100.0)
            deleted = sum(1 for r in results if r.status == LINK_DELETED)
            failed = sum(1 for r in results if r.status == LINK_FAILED)
            self.status_var.set(
                f"解除互链完成：共处理 {len(results)} 条，已删除 {deleted}，失败 {failed}"
            )

        def _on_error(exc: Exception) -> None:
            self._set_running(False)
            self._cancel_event = None
            self.status_var.set(f"解除互链失败：{exc}")

        self.runner.submit(_work, _on_done, _on_error)
        return True

    def reload_history(self) -> list[LedgerPlanSummary]:
        plans = self.ledger.list_plans()
        self._history_by_id = {p.plan_id: p for p in plans}
        for item_id in self.history_tree.get_children():
            self.history_tree.delete(item_id)
        for p in plans:
            self.history_tree.insert(
                "",
                tk.END,
                iid=p.plan_id,
                values=(
                    p.plan_id,
                    _MODE_LABELS.get(p.mode, p.mode),
                    p.created_at,
                    ", ".join(p.site_names),
                    p.created_count,
                    p.rolled_back_count,
                ),
            )
        return plans

    def selected_history_plan_id(self) -> str:
        sel = self.history_tree.selection()
        return sel[0] if sel else ""

    def start_rollback_selected(self) -> bool:
        if self._running:
            return False
        plan_id = self.selected_history_plan_id()
        if not plan_id:
            self._warn("提示", "请先在下方历史记录中选中一条互链计划")
            return False
        summary = self._history_by_id.get(plan_id)
        pending = (
            max(0, summary.created_count - summary.rolled_back_count)
            if summary is not None
            else 0
        )
        if summary is not None and pending == 0:
            self._warn("提示", f"计划 {plan_id} 没有可回滚的新建友链记录")
            return False
        if not self._confirm(
            "确认一键回滚",
            f"将仅删除计划 {plan_id} 中由工具新建且尚未回滚的 {pending} 条友链，确定继续吗？",
        ):
            return False

        all_sites = self.site_picker.all_sites()
        self._clear_plan_tree()
        self._expected_total = max(1, pending)
        self._done_count = 0
        self.progress_var.set(0.0)
        self._cancel_event = threading.Event()
        self._set_running(True)
        self.status_var.set(f"正在回滚计划 {plan_id}…")

        cancel_event = self._cancel_event
        progress_cb = self.runner.make_progress(self._append_op_result_row)

        def _work() -> list[LinkOpResult]:
            return self.service.rollback_plan(
                plan_id,
                all_sites,
                ledger=self.ledger,
                progress=progress_cb,
                cancel_event=cancel_event,
            )

        def _on_done(results: list[LinkOpResult]) -> None:
            self._set_running(False)
            self._cancel_event = None
            if not self.plan_tree.get_children() and results:
                for res in results:
                    self._append_op_result_row(res)
            self.progress_var.set(100.0)
            deleted = sum(1 for r in results if r.status == LINK_DELETED)
            not_found = sum(1 for r in results if r.status == LINK_NOT_FOUND)
            failed = sum(1 for r in results if r.status == LINK_FAILED)
            self.status_var.set(
                f"回滚完成（计划 {plan_id}）：已删 {deleted}，已不存在 {not_found}，失败 {failed}"
            )
            self.reload_history()

        def _on_error(exc: Exception) -> None:
            self._set_running(False)
            self._cancel_event = None
            self.status_var.set(f"回滚失败：{exc}")
            self.reload_history()

        self.runner.submit(_work, _on_done, _on_error)
        return True

    def cancel(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()
            self.status_var.set("正在停止剩余任务…")
