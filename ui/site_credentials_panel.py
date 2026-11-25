"""Per-site group/credential editor panel embedded in ConfigCenter."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Iterable, Mapping
from tkinter import ttk

from models.sitegroup import McpProbeResult


def format_probe_summary(result: McpProbeResult) -> str:
    """Render a concise, secret-free Chinese status line for an MCP probe result."""
    if result is None:
        return "MCP 探测无结果"
    if result.ok:
        server = " ".join(
            part for part in (result.server_name.strip(), result.server_version.strip()) if part
        )
        tool_count = result.tool_count if result.tool_count > 0 else len(result.tools)
        capabilities: list[str] = []
        if result.friendlink_tools:
            capabilities.append("友链")
        if result.upgrade_tools:
            capabilities.append("升级")
        if result.jump_tools:
            capabilities.append("跳转")

        details: list[str] = []
        if server:
            details.append(server)
        details.append(f"共 {tool_count} 个工具")
        if capabilities:
            details.append("支持：" + "、".join(capabilities))
        else:
            details.append("未发现站群工具")
        return f"MCP 正常（{'，'.join(details)}）"

    message = (result.message or "").strip()
    hint = (result.hint or "").strip()
    if message and hint and hint not in message:
        return f"MCP 连接失败：{message}（{hint}）"
    if message:
        return f"MCP 连接失败：{message}"
    if hint:
        return f"MCP 连接失败：{hint}"
    return "MCP 连接失败"


class SiteCredentialsFrame(ttk.LabelFrame):
    """Editor section for site-group metadata, MCP token and admin login fields.

    Per project policy (plain-text personal tool), tokens and passwords are
    bound directly to plain ``tk.StringVar`` instances and visually masked with
    ``show="•"`` unless the user toggles "显示".
    """

    FIELDS = (
        "site_title",
        "site_group",
        "mcp_token",
        "admin_username",
        "admin_password",
        "admin_path",
    )
    MASK_CHAR = "•"

    def __init__(
        self,
        master,
        *,
        text: str = "站群与后台",
        on_test_mcp: Callable[[], None] | None = None,
        on_open_admin: Callable[[], None] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(master, text=text, padding=10, **kwargs)
        self._on_test_mcp = on_test_mcp
        self._on_open_admin = on_open_admin
        self._enabled = True

        self._fallback_vars: dict[str, tk.StringVar] = {
            name: tk.StringVar(value="") for name in self.FIELDS
        }
        self._bound_vars: dict[str, tk.StringVar] = dict(self._fallback_vars)

        self.show_token_var = tk.BooleanVar(value=False)
        self.show_password_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="")

        self.entries: dict[str, ttk.Entry] = {}
        self._build()
        self.bind_vars(None)

    def _build(self) -> None:
        self.columnconfigure(1, weight=1)
        self.columnconfigure(3, weight=1)

        # Row 0: site_title & site_group
        ttk.Label(self, text="站点名称").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        title_entry = ttk.Entry(self)
        title_entry.grid(row=0, column=1, sticky="ew", padx=(0, 12), pady=4, ipady=2)
        self.entries["site_title"] = title_entry

        ttk.Label(self, text="站点分组").grid(row=0, column=2, sticky="w", padx=(0, 8), pady=4)
        self.group_box = ttk.Combobox(self)
        self.group_box.grid(row=0, column=3, sticky="ew", pady=4)
        self.entries["site_group"] = self.group_box

        # Row 1: mcp_token + 显示 toggle
        ttk.Label(self, text="MCP Token").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        token_wrap = ttk.Frame(self)
        token_wrap.grid(row=1, column=1, columnspan=3, sticky="ew", pady=4)
        token_wrap.columnconfigure(0, weight=1)
        token_entry = ttk.Entry(token_wrap, show=self.MASK_CHAR)
        token_entry.grid(row=0, column=0, sticky="ew", ipady=2)
        self.entries["mcp_token"] = token_entry
        self.show_token_check = ttk.Checkbutton(
            token_wrap,
            text="显示",
            variable=self.show_token_var,
            command=self._sync_secret_masks,
        )
        self.show_token_check.grid(row=0, column=1, sticky="w", padx=(6, 0))

        # Row 2: admin_username & admin_path
        ttk.Label(self, text="后台账号").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        user_entry = ttk.Entry(self)
        user_entry.grid(row=2, column=1, sticky="ew", padx=(0, 12), pady=4, ipady=2)
        self.entries["admin_username"] = user_entry

        ttk.Label(self, text="后台路径").grid(row=2, column=2, sticky="w", padx=(0, 8), pady=4)
        path_entry = ttk.Entry(self)
        path_entry.grid(row=2, column=3, sticky="ew", pady=4, ipady=2)
        self.entries["admin_path"] = path_entry

        # Row 3: admin_password + 显示 toggle
        ttk.Label(self, text="后台密码").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=4)
        password_wrap = ttk.Frame(self)
        password_wrap.grid(row=3, column=1, columnspan=3, sticky="ew", pady=4)
        password_wrap.columnconfigure(0, weight=1)
        password_entry = ttk.Entry(password_wrap, show=self.MASK_CHAR)
        password_entry.grid(row=0, column=0, sticky="ew", ipady=2)
        self.entries["admin_password"] = password_entry
        self.show_password_check = ttk.Checkbutton(
            password_wrap,
            text="显示",
            variable=self.show_password_var,
            command=self._sync_secret_masks,
        )
        self.show_password_check.grid(row=0, column=1, sticky="w", padx=(6, 0))

        # Row 4: Actions + inline status
        actions = ttk.Frame(self)
        actions.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        self.test_mcp_button = ttk.Button(
            actions,
            text="测试 MCP",
            style="Compact.TButton",
            command=self._handle_test_mcp,
        )
        self.test_mcp_button.pack(side=tk.LEFT, padx=(0, 6))
        self.open_admin_button = ttk.Button(
            actions,
            text="跳转后台",
            style="Compact.TButton",
            command=self._handle_open_admin,
        )
        self.open_admin_button.pack(side=tk.LEFT, padx=(0, 10))
        self.status_label = ttk.Label(actions, textvariable=self.status_var, style="Muted.TLabel")
        self.status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.show_token_var.trace_add("write", lambda *_: self._sync_secret_masks())
        self.show_password_var.trace_add("write", lambda *_: self._sync_secret_masks())

    def _sync_secret_masks(self) -> None:
        token_show = "" if self.show_token_var.get() else self.MASK_CHAR
        password_show = "" if self.show_password_var.get() else self.MASK_CHAR
        self.entries["mcp_token"].configure(show=token_show)
        self.entries["admin_password"].configure(show=password_show)

    def _handle_test_mcp(self) -> None:
        if self._enabled and self._on_test_mcp is not None:
            self._on_test_mcp()

    def _handle_open_admin(self) -> None:
        if self._enabled and self._on_open_admin is not None:
            self._on_open_admin()

    def bind_vars(self, vars_map: Mapping[str, tk.StringVar] | None) -> None:
        """Bind input widgets to matching ``StringVar``s in *vars_map* (extra keys ignored)."""
        source = vars_map or {}
        self.show_token_var.set(False)
        self.show_password_var.set(False)
        self._sync_secret_masks()
        for field_name in self.FIELDS:
            var = source.get(field_name)
            if not isinstance(var, tk.StringVar):
                var = self._fallback_vars[field_name]
                var.set("")
            self._bound_vars[field_name] = var
            self.entries[field_name].configure(textvariable=var)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        state = "normal" if self._enabled else "disabled"
        for widget in self.entries.values():
            widget.configure(state=state)
        self.show_token_check.configure(state=state)
        self.show_password_check.configure(state=state)
        self.test_mcp_button.configure(state=state)
        self.open_admin_button.configure(state=state)

    def set_status(self, text: str) -> None:
        self.status_var.set(str(text or ""))

    def set_group_choices(self, groups: Iterable[str]) -> None:
        seen: set[str] = set()
        choices: list[str] = []
        for raw in groups or ():
            value = str(raw or "").strip()
            if value and value not in seen:
                seen.add(value)
                choices.append(value)
        self.group_box.configure(values=tuple(choices))
