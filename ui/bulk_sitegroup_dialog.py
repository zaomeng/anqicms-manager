"""Bulk configuration dialog for site-group metadata and admin credentials."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from tkinter import messagebox, ttk
from urllib.parse import urlsplit

from core.config_service import SITE_GROUP_MAX_LENGTH
from core.site_credentials import mask_secret, site_origin_key
from core.site_factory import normalize_admin_path
from models.entities import SiteConfig
from ui.site_picker import SitePicker


FIELD_LABELS: dict[str, str] = {
    "site_title": "站点名称",
    "site_group": "站点分组",
    "mcp_token": "MCP Token",
    "admin_username": "后台账号",
    "admin_password": "后台密码",
    "admin_path": "后台路径",
}

_TEXT_IMPORT_FIELD_KEYS: tuple[str, ...] = (
    "site_group",
    "mcp_token",
    "admin_username",
    "admin_password",
    "site_title",
)

_DEFAULT_PORTS = {80, 443}


@dataclass(frozen=True, slots=True)
class BulkTextLineMatch:
    """Parsed result of a single non-empty, non-comment line in bulk text import."""

    line_no: int
    raw_identifier: str
    matched_site_name: str
    updates: dict[str, str]
    status: str  # "matched" | "unchanged" | "unmatched" | "error"
    message: str


def has_control_chars(value: str) -> bool:
    """Return True if *value* contains ASCII control characters or DEL."""
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def validate_bulk_fields(fields: Mapping[str, object]) -> dict[str, str]:
    """Validate and normalize non-blank bulk field values.

    Blank/whitespace-only entries are omitted so existing per-site values are preserved.
    """
    cleaned: dict[str, str] = {}
    for key, raw_value in fields.items():
        text = str(raw_value if raw_value is not None else "").strip()
        if not text:
            continue
        label = FIELD_LABELS.get(key, key)
        if has_control_chars(text):
            raise ValueError(f"{label}不能包含换行或控制字符")
        if key == "site_group" and len(text) > SITE_GROUP_MAX_LENGTH:
            raise ValueError(f"{label}不能超过 {SITE_GROUP_MAX_LENGTH} 个字符")
        if key == "admin_path":
            text = normalize_admin_path(text)
        cleaned[key] = text
    return cleaned


def _scheme_host_port_key(url: str) -> tuple[str, str, int | None] | None:
    text = (url or "").strip()
    if "://" not in text:
        return None
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.casefold()
    host = (parsed.hostname or "").strip(".").casefold()
    if not scheme or not host:
        return None
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    if host.startswith("www."):
        host = host[4:]
    if port is None:
        port = 443 if scheme == "https" else 80 if scheme == "http" else None
    return (scheme, host, port)


def match_site_by_identifier(
    identifier: str,
    sites: Sequence[SiteConfig],
) -> tuple[SiteConfig | None, str]:
    """Match *identifier* against *sites* by exact name, case-folded name, or site URL origin."""
    key = (identifier or "").strip()
    if not key:
        return None, "站点标识为空"

    exact = [site for site in sites if site.name == key]
    if len(exact) == 1:
        return exact[0], ""

    folded = key.casefold()
    ci_matches = [site for site in sites if site.name.casefold() == folded]
    if len(ci_matches) == 1:
        return ci_matches[0], ""
    if len(ci_matches) > 1:
        return None, f"站点名“{key}”存在多个匹配"

    scheme_key = _scheme_host_port_key(key)
    if scheme_key is not None:
        scheme_matches = [
            site for site in sites if _scheme_host_port_key(getattr(site, "url", "")) == scheme_key
        ]
        if len(scheme_matches) == 1:
            return scheme_matches[0], ""

    origin_key = site_origin_key(key)
    if origin_key:
        origin_matches = [
            site for site in sites if site_origin_key(getattr(site, "url", "")) == origin_key
        ]
        if len(origin_matches) == 1:
            return origin_matches[0], ""
        if len(origin_matches) > 1:
            return None, f"域名“{origin_key}”匹配到多个站点，请改用精确站点标识"

    return None, "未找到匹配站点"


def _split_bulk_line(line: str) -> list[str]:
    if "\t" in line:
        parts = line.split("\t")
    elif "|" in line:
        parts = line.split("|")
    elif "," in line:
        parts = line.split(",")
    else:
        parts = [line]
    return [part.strip() for part in parts]


def _format_updates_summary(updates: Mapping[str, str]) -> str:
    tokens: list[str] = []
    for key, value in updates.items():
        label = FIELD_LABELS.get(key, key)
        if key in {"mcp_token", "admin_password"}:
            tokens.append(f"{label}={mask_secret(value)}")
        else:
            tokens.append(f"{label}={value}")
    return "，".join(tokens)


def parse_bulk_text_lines(
    raw_text: str,
    sites: Sequence[SiteConfig],
) -> tuple[list[BulkTextLineMatch], dict[str, dict[str, str]]]:
    """Parse pasted bulk text lines into preview records and aggregated per-site updates."""
    records: list[BulkTextLineMatch] = []
    aggregated: dict[str, dict[str, str]] = {}

    cleaned_text = (raw_text or "").lstrip("\ufeff")
    for line_no, raw_line in enumerate(cleaned_text.splitlines(), start=1):
        line = raw_line.rstrip("\r\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        parts = _split_bulk_line(line)
        if len(parts) > 6:
            if any(parts[6:]):
                records.append(
                    BulkTextLineMatch(
                        line_no=line_no,
                        raw_identifier=parts[0],
                        matched_site_name="",
                        updates={},
                        status="error",
                        message="列数超过 6 列（格式：站点标识|分组|MCP_Token|后台账号|后台密码|站点名称）",
                    )
                )
                continue
            parts = parts[:6]

        identifier = parts[0]
        site, match_error = match_site_by_identifier(identifier, sites)
        if site is None:
            records.append(
                BulkTextLineMatch(
                    line_no=line_no,
                    raw_identifier=identifier,
                    matched_site_name="",
                    updates={},
                    status="unmatched",
                    message=match_error,
                )
            )
            continue

        raw_fields = {
            key: (parts[idx] if idx < len(parts) else "")
            for idx, key in enumerate(_TEXT_IMPORT_FIELD_KEYS, start=1)
        }
        try:
            updates = validate_bulk_fields(raw_fields)
        except ValueError as exc:
            records.append(
                BulkTextLineMatch(
                    line_no=line_no,
                    raw_identifier=identifier,
                    matched_site_name=site.name,
                    updates={},
                    status="error",
                    message=str(exc),
                )
            )
            continue

        if not updates:
            records.append(
                BulkTextLineMatch(
                    line_no=line_no,
                    raw_identifier=identifier,
                    matched_site_name=site.name,
                    updates={},
                    status="unchanged",
                    message="未填写需要更新的字段（跳过）",
                )
            )
            continue

        aggregated.setdefault(site.name, {}).update(updates)
        records.append(
            BulkTextLineMatch(
                line_no=line_no,
                raw_identifier=identifier,
                matched_site_name=site.name,
                updates=updates,
                status="matched",
                message=_format_updates_summary(updates),
            )
        )

    return records, aggregated


class BulkSiteGroupDialog(tk.Toplevel):
    """Modal dialog for batch-assigning or text-importing site-group credentials."""

    MASK_CHAR = "•"

    def __init__(
        self,
        master,
        sites: Sequence[SiteConfig],
        on_apply: Callable[[dict[str, dict[str, str]]], bool | str | None],
        *,
        initial_selected_names: Sequence[str] | None = None,
    ) -> None:
        super().__init__(master)
        self.title("批量配置站群信息")
        self.geometry("880x650")
        self.minsize(760, 560)
        self.transient(master)
        self.grab_set()

        self._sites = list(sites)
        self._on_apply = on_apply
        self._initial_selected_names = list(initial_selected_names or ())

        self.site_group_var = tk.StringVar(value="")
        self.mcp_token_var = tk.StringVar(value="")
        self.admin_username_var = tk.StringVar(value="")
        self.admin_password_var = tk.StringVar(value="")
        self.admin_path_var = tk.StringVar(value="")
        self.show_token_var = tk.BooleanVar(value=False)
        self.show_password_var = tk.BooleanVar(value=False)

        self.import_summary_var = tk.StringVar(value="粘贴文本后可先点击“预览匹配”检查结果")
        self.message_var = tk.StringVar(value="留空字段不会覆盖站点已有值；应用后请在站点管理点击“保存更改”")

        self.notebook: ttk.Notebook | None = None
        self.assign_tab: ttk.Frame | None = None
        self.import_tab: ttk.Frame | None = None
        self.site_picker: SitePicker | None = None
        self.group_box: ttk.Combobox | None = None
        self.mcp_token_entry: ttk.Entry | None = None
        self.admin_password_entry: ttk.Entry | None = None
        self.import_text: tk.Text | None = None
        self.preview_tree: ttk.Treeview | None = None

        self._build()
        self.bind("<Escape>", lambda _event: self.destroy())

    def _collect_group_choices(self) -> tuple[str, ...]:
        groups: list[str] = []
        seen: set[str] = set()
        for site in self._sites:
            group = (getattr(site, "site_group", "") or "").strip()
            if group and group not in seen:
                seen.add(group)
                groups.append(group)
        return tuple(groups)

    def _build(self) -> None:
        container = ttk.Frame(self, padding=14)
        container.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(container)
        header.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(header, text="批量配置站群信息", style="Metric.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="支持勾选多站点统一赋值，或通过文本行批量导入各站点的分组、Token 与后台信息。",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        self.notebook = ttk.Notebook(container)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        self.assign_tab = ttk.Frame(self.notebook, padding=10)
        self.import_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.assign_tab, text="批量赋值")
        self.notebook.add(self.import_tab, text="批量文本导入")

        self._build_assign_tab()
        self._build_import_tab()

        footer = ttk.Frame(container)
        footer.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(footer, textvariable=self.message_var, style="Muted.TLabel").pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        ttk.Button(footer, text="关闭", command=self.destroy).pack(side=tk.RIGHT)

    def _build_assign_tab(self) -> None:
        assert self.assign_tab is not None
        self.site_picker = SitePicker(
            self.assign_tab,
            lambda: self._sites,
            height=9,
            show_status=False,
        )
        self.site_picker.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        available_names = {site.name for site in self._sites}
        preselected = [name for name in self._initial_selected_names if name in available_names]
        if preselected:
            self.site_picker.set_selected(preselected)
        else:
            self.site_picker.select_all()

        form = ttk.LabelFrame(
            self.assign_tab,
            text="批量赋值字段（留空表示不覆盖站点原有值）",
            padding=10,
        )
        form.pack(fill=tk.X)
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)

        ttk.Label(form, text="站点分组").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        self.group_box = ttk.Combobox(
            form,
            textvariable=self.site_group_var,
            values=self._collect_group_choices(),
        )
        self.group_box.grid(row=0, column=1, sticky="ew", padx=(0, 14), pady=4)

        ttk.Label(form, text="后台路径").grid(row=0, column=2, sticky="w", padx=(0, 8), pady=4)
        path_wrap = ttk.Frame(form)
        path_wrap.grid(row=0, column=3, sticky="ew", pady=4)
        path_wrap.columnconfigure(0, weight=1)
        ttk.Entry(path_wrap, textvariable=self.admin_path_var).grid(row=0, column=0, sticky="ew", ipady=2)
        ttk.Label(path_wrap, text="如 /system/", style="Muted.TLabel").grid(
            row=0, column=1, sticky="w", padx=(6, 0)
        )

        ttk.Label(form, text="MCP Token").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        token_wrap = ttk.Frame(form)
        token_wrap.grid(row=1, column=1, columnspan=3, sticky="ew", pady=4)
        token_wrap.columnconfigure(0, weight=1)
        self.mcp_token_entry = ttk.Entry(
            token_wrap, textvariable=self.mcp_token_var, show=self.MASK_CHAR
        )
        self.mcp_token_entry.grid(row=0, column=0, sticky="ew", ipady=2)
        ttk.Checkbutton(
            token_wrap,
            text="显示",
            variable=self.show_token_var,
            command=self._sync_secret_masks,
        ).grid(row=0, column=1, sticky="w", padx=(6, 0))

        ttk.Label(form, text="后台账号").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(form, textvariable=self.admin_username_var).grid(
            row=2, column=1, sticky="ew", padx=(0, 14), pady=4, ipady=2
        )

        ttk.Label(form, text="后台密码").grid(row=2, column=2, sticky="w", padx=(0, 8), pady=4)
        pwd_wrap = ttk.Frame(form)
        pwd_wrap.grid(row=2, column=3, sticky="ew", pady=4)
        pwd_wrap.columnconfigure(0, weight=1)
        self.admin_password_entry = ttk.Entry(
            pwd_wrap, textvariable=self.admin_password_var, show=self.MASK_CHAR
        )
        self.admin_password_entry.grid(row=0, column=0, sticky="ew", ipady=2)
        ttk.Checkbutton(
            pwd_wrap,
            text="显示",
            variable=self.show_password_var,
            command=self._sync_secret_masks,
        ).grid(row=0, column=1, sticky="w", padx=(6, 0))

        actions = ttk.Frame(self.assign_tab)
        actions.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(
            actions,
            text="应用到所选站点",
            style="Accent.TButton",
            command=self._apply_assign_tab,
        ).pack(side=tk.RIGHT)

    def _build_import_tab(self) -> None:
        assert self.import_tab is not None
        hint = (
            "每行一条（支持 |、Tab 或逗号分隔，# 开头为注释，留空列保留原值，尾部可省略）：\n"
            "站点标识(站点名或域名)|分组|MCP_Token|后台账号|后台密码|站点名称"
        )
        ttk.Label(self.import_tab, text=hint, style="Muted.TLabel", justify=tk.LEFT).pack(
            anchor="w", pady=(0, 6)
        )

        text_wrap = ttk.Frame(self.import_tab)
        text_wrap.pack(fill=tk.BOTH, expand=True)
        self.import_text = tk.Text(text_wrap, height=8, wrap=tk.NONE, undo=True)
        text_yscroll = ttk.Scrollbar(text_wrap, orient=tk.VERTICAL, command=self.import_text.yview)
        self.import_text.configure(yscrollcommand=text_yscroll.set)
        self.import_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        text_yscroll.pack(side=tk.RIGHT, fill=tk.Y)

        action_bar = ttk.Frame(self.import_tab)
        action_bar.pack(fill=tk.X, pady=8)
        ttk.Button(action_bar, text="预览匹配", command=self._preview_text_import).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(action_bar, text="清空", command=self._clear_text_import).pack(
            side=tk.LEFT, padx=(0, 12)
        )
        ttk.Label(action_bar, textvariable=self.import_summary_var, style="Muted.TLabel").pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        ttk.Button(
            action_bar,
            text="确认批量应用",
            style="Accent.TButton",
            command=self._apply_text_import,
        ).pack(side=tk.RIGHT)

        preview_wrap = ttk.Frame(self.import_tab)
        preview_wrap.pack(fill=tk.BOTH, expand=True)
        columns = (
            ("line", "行号", 50, False, tk.CENTER),
            ("input", "输入标识", 160, False, tk.W),
            ("site", "匹配站点", 120, False, tk.W),
            ("status", "状态", 80, False, tk.CENTER),
            ("details", "变更 / 说明", 360, True, tk.W),
        )
        self.preview_tree = ttk.Treeview(
            preview_wrap,
            columns=tuple(col[0] for col in columns),
            show="headings",
            height=7,
        )
        for col_id, heading, width, stretch, anchor in columns:
            self.preview_tree.heading(col_id, text=heading)
            self.preview_tree.column(
                col_id,
                width=width,
                minwidth=min(width, 45),
                stretch=stretch,
                anchor=anchor,
            )
        tree_yscroll = ttk.Scrollbar(preview_wrap, orient=tk.VERTICAL, command=self.preview_tree.yview)
        self.preview_tree.configure(yscrollcommand=tree_yscroll.set)
        self.preview_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_yscroll.pack(side=tk.RIGHT, fill=tk.Y)

    def _sync_secret_masks(self) -> None:
        if self.mcp_token_entry is not None:
            self.mcp_token_entry.configure(
                show="" if self.show_token_var.get() else self.MASK_CHAR
            )
        if self.admin_password_entry is not None:
            self.admin_password_entry.configure(
                show="" if self.show_password_var.get() else self.MASK_CHAR
            )

    def _dispatch_updates(self, updates: dict[str, dict[str, str]]) -> bool:
        try:
            result = self._on_apply(updates)
        except Exception as exc:  # noqa: BLE001
            self.message_var.set(f"应用失败：{exc}")
            return False
        if result is False:
            return False
        if isinstance(result, str) and result.strip():
            self.message_var.set(result.strip())
            return False
        self.destroy()
        return True

    def _apply_assign_tab(self) -> bool:
        assert self.site_picker is not None
        selected = self.site_picker.selected_sites()
        if not selected:
            self.message_var.set("请先勾选至少一个目标站点")
            return False

        raw_fields = {
            "site_group": self.site_group_var.get(),
            "mcp_token": self.mcp_token_var.get(),
            "admin_username": self.admin_username_var.get(),
            "admin_password": self.admin_password_var.get(),
            "admin_path": self.admin_path_var.get(),
        }
        try:
            cleaned = validate_bulk_fields(raw_fields)
        except ValueError as exc:
            self.message_var.set(str(exc))
            return False

        if not cleaned:
            self.message_var.set("请至少填写一项要批量赋值的字段")
            return False

        updates = {site.name: dict(cleaned) for site in selected}
        return self._dispatch_updates(updates)

    def _clear_text_import(self) -> None:
        if self.import_text is not None:
            self.import_text.delete("1.0", tk.END)
        if self.preview_tree is not None:
            for item in self.preview_tree.get_children():
                self.preview_tree.delete(item)
        self.import_summary_var.set("已清空")
        self.message_var.set("请粘贴批量文本后点击“预览匹配”")

    def _render_preview(
        self,
        records: Sequence[BulkTextLineMatch],
    ) -> tuple[int, int, int, int]:
        assert self.preview_tree is not None
        for item in self.preview_tree.get_children():
            self.preview_tree.delete(item)

        status_labels = {
            "matched": "匹配",
            "unchanged": "跳过",
            "unmatched": "未匹配",
            "error": "错误",
        }
        matched_count = 0
        unchanged_count = 0
        unmatched_count = 0
        error_count = 0
        for rec in records:
            if rec.status == "matched":
                matched_count += 1
            elif rec.status == "unchanged":
                unchanged_count += 1
            elif rec.status == "unmatched":
                unmatched_count += 1
            else:
                error_count += 1
            self.preview_tree.insert(
                "",
                tk.END,
                values=(
                    rec.line_no,
                    rec.raw_identifier or "-",
                    rec.matched_site_name or "-",
                    status_labels.get(rec.status, rec.status),
                    rec.message,
                ),
            )
        self.import_summary_var.set(
            f"匹配 {matched_count} 条，跳过 {unchanged_count} 条，未匹配 {unmatched_count} 条，错误 {error_count} 条"
        )
        return matched_count, unchanged_count, unmatched_count, error_count

    def _preview_text_import(
        self,
    ) -> tuple[list[BulkTextLineMatch], dict[str, dict[str, str]]]:
        raw_text = self.import_text.get("1.0", tk.END) if self.import_text is not None else ""
        records, aggregated = parse_bulk_text_lines(raw_text, self._sites)
        matched_count, _unchanged, unmatched_count, error_count = self._render_preview(records)
        if not records:
            self.message_var.set("未检测到有效数据行")
        elif error_count:
            self.message_var.set(f"发现 {error_count} 条格式或字段校验错误，请修正后再应用")
        elif unmatched_count:
            self.message_var.set(
                f"已匹配 {matched_count} 条；另有 {unmatched_count} 条未匹配到站点"
            )
        else:
            self.message_var.set(f"预览完成：共匹配 {len(aggregated)} 个站点，可点击“确认批量应用”")
        return records, aggregated

    def _apply_text_import(self) -> bool:
        records, aggregated = self._preview_text_import()
        error_records = [rec for rec in records if rec.status == "error"]
        if error_records:
            first_err = error_records[0]
            self.message_var.set(f"第 {first_err.line_no} 行校验失败：{first_err.message}")
            return False

        if not aggregated:
            self.message_var.set("没有可应用的匹配站点更新")
            return False

        unmatched_records = [rec for rec in records if rec.status == "unmatched"]
        if unmatched_records and not messagebox.askyesno(
            "存在未匹配行",
            f"有 {len(unmatched_records)} 行未匹配到站点。\n"
            f"是否仅应用已匹配的 {len(aggregated)} 个站点？",
            parent=self,
        ):
            return False

        return self._dispatch_updates(aggregated)
