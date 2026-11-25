"""Reusable checkbox-style site picker with search, group filter and row status."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Iterable, Sequence
from tkinter import ttk

from core.site_credentials import display_name, effective_mcp_token, mask_secret
from models.entities import SiteConfig


class SitePicker(ttk.Frame):
    """Multi-select site list used by FriendLink, CrossLink and Upgrade windows."""

    ALL_GROUPS_LABEL = "全部分组"
    UNGROUPED_LABEL = "未分组"
    CHECKED_MARK = "☑"
    UNCHECKED_MARK = "☐"

    def __init__(
        self,
        master,
        sites_provider: Callable[[], Sequence[SiteConfig]] | Sequence[SiteConfig],
        *,
        height: int = 12,
        show_status: bool = True,
        on_selection_changed: Callable[[list[SiteConfig]], None] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(master, **kwargs)
        self._sites_provider = sites_provider
        self._show_status = bool(show_status)
        self._on_selection_changed = on_selection_changed
        self._enabled = True
        self._suppress_filter = False

        self._sites: list[SiteConfig] = []
        self._site_by_key: dict[str, SiteConfig] = {}
        self._alias_to_key: dict[str, str] = {}
        self._visible_keys: list[str] = []
        self._selected_keys: set[str] = set()
        self._statuses: dict[str, str] = {}

        self.search_var = tk.StringVar(value="")
        self.group_var = tk.StringVar(value=self.ALL_GROUPS_LABEL)
        self.count_var = tk.StringVar(value="已选 0 / 0")

        self._build(height)
        self.search_var.trace_add("write", self._on_filter_changed)
        self.group_var.trace_add("write", self._on_filter_changed)
        self.reload()

    @staticmethod
    def _site_key(site: SiteConfig) -> str:
        return (getattr(site, "site_id", "") or "").strip() or getattr(site, "name", "")

    def _resolve_key(self, site_id: str) -> str | None:
        if not site_id:
            return None
        if site_id in self._site_by_key:
            return site_id
        return self._alias_to_key.get(site_id)

    def _build(self, height: int) -> None:
        controls = ttk.Frame(self)
        controls.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(controls, text="搜索").pack(side=tk.LEFT, padx=(0, 4))
        self.search_entry = ttk.Entry(controls, textvariable=self.search_var, width=18)
        self.search_entry.pack(side=tk.LEFT, padx=(0, 8))

        ttk.Label(controls, text="分组").pack(side=tk.LEFT, padx=(0, 4))
        self.group_box = ttk.Combobox(
            controls,
            textvariable=self.group_var,
            state="readonly",
            width=14,
            values=(self.ALL_GROUPS_LABEL,),
        )
        self.group_box.pack(side=tk.LEFT, padx=(0, 8))
        self.group_box.bind("<<ComboboxSelected>>", self._on_filter_changed)

        self.select_all_button = ttk.Button(
            controls,
            text="全选",
            style="Compact.TButton",
            command=self.select_all,
        )
        self.select_all_button.pack(side=tk.LEFT, padx=(0, 4))

        self.select_none_button = ttk.Button(
            controls,
            text="全不选",
            style="Compact.TButton",
            command=self.select_none,
        )
        self.select_none_button.pack(side=tk.LEFT, padx=(0, 4))

        self.invert_button = ttk.Button(
            controls,
            text="反选",
            style="Compact.TButton",
            command=self.invert_selection,
        )
        self.invert_button.pack(side=tk.LEFT, padx=(0, 8))

        self.count_label = ttk.Label(controls, textvariable=self.count_var, style="Muted.TLabel")
        self.count_label.pack(side=tk.RIGHT)

        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill=tk.BOTH, expand=True)

        columns: list[tuple[str, str, int, bool, str]] = [
            ("checked", "选", 36, False, tk.CENTER),
            ("name", "站点", 110, False, tk.W),
            ("title", "名称", 110, False, tk.W),
            ("group", "分组", 90, False, tk.W),
            ("url", "网址", 190, True, tk.W),
            ("token", "Token", 110, False, tk.W),
        ]
        if self._show_status:
            columns.append(("status", "状态", 130, True, tk.W))

        self.tree = ttk.Treeview(
            tree_frame,
            columns=tuple(col[0] for col in columns),
            show="headings",
            selectmode="extended",
            height=height,
        )
        for col_id, heading, width, stretch, anchor in columns:
            if col_id == "checked":
                self.tree.heading(col_id, text=heading, command=self._toggle_all_visible)
            else:
                self.tree.heading(col_id, text=heading)
            self.tree.column(
                col_id,
                width=width,
                minwidth=min(width, 36),
                stretch=stretch,
                anchor=anchor,
            )

        yscroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Double-1>", self._on_tree_double_click)
        self.tree.bind("<space>", self._on_space_key)

    def _fetch_sites(self) -> list[SiteConfig]:
        raw = self._sites_provider() if callable(self._sites_provider) else self._sites_provider
        return list(raw or ())

    def reload(self) -> None:
        """Reload sites from ``sites_provider`` while preserving valid selections and statuses."""
        previous_selected = set(self._selected_keys)
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

        self._selected_keys = {k for k in previous_selected if k in self._site_by_key}
        self._statuses = {k: v for k, v in self._statuses.items() if k in self._site_by_key}

        groups = sorted(
            {
                (getattr(site, "site_group", "") or "").strip()
                for site in self._sites
                if (getattr(site, "site_group", "") or "").strip()
            }
        )
        has_ungrouped = any(
            not (getattr(site, "site_group", "") or "").strip() for site in self._sites
        )
        choices = [self.ALL_GROUPS_LABEL, *groups]
        if has_ungrouped and groups:
            choices.append(self.UNGROUPED_LABEL)

        self._suppress_filter = True
        try:
            self.group_box.configure(values=tuple(choices))
            if self.group_var.get() not in choices:
                self.group_var.set(self.ALL_GROUPS_LABEL)
        finally:
            self._suppress_filter = False

        self._apply_filter()
        if self._selected_keys != previous_selected:
            self._notify_selection_changed()

    def _matches_filter(self, site: SiteConfig, query: str, group_filter: str) -> bool:
        site_group = (getattr(site, "site_group", "") or "").strip()
        if group_filter == self.UNGROUPED_LABEL:
            if site_group:
                return False
        elif group_filter and group_filter != self.ALL_GROUPS_LABEL:
            if site_group != group_filter:
                return False

        if not query:
            return True
        haystack = " ".join(
            (
                getattr(site, "name", "") or "",
                getattr(site, "site_title", "") or "",
                site_group,
                getattr(site, "url", "") or "",
            )
        ).casefold()
        return query in haystack

    def _row_values(self, key: str, site: SiteConfig) -> tuple[str, ...]:
        mark = self.CHECKED_MARK if key in self._selected_keys else self.UNCHECKED_MARK
        values = [
            mark,
            getattr(site, "name", ""),
            display_name(site),
            (getattr(site, "site_group", "") or "").strip(),
            getattr(site, "url", ""),
            mask_secret(effective_mcp_token(site)),
        ]
        if self._show_status:
            values.append(self._statuses.get(key, ""))
        return tuple(values)

    def _apply_filter(self) -> None:
        query = self.search_var.get().strip().casefold()
        group_filter = self.group_var.get().strip() or self.ALL_GROUPS_LABEL
        self._visible_keys = []
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)
        for site in self._sites:
            if not self._matches_filter(site, query, group_filter):
                continue
            key = self._site_key(site)
            self._visible_keys.append(key)
            self.tree.insert("", tk.END, iid=key, values=self._row_values(key, site))
        self._update_count_label()

    def _on_filter_changed(self, *_args) -> None:
        if self._suppress_filter:
            return
        self._apply_filter()

    def _update_row(self, key: str) -> None:
        site = self._site_by_key.get(key)
        if site is None:
            return
        if self.tree.exists(key):
            self.tree.item(key, values=self._row_values(key, site))

    def _update_count_label(self) -> None:
        total = len(self._sites)
        selected = len(self._selected_keys)
        visible = len(self._visible_keys)
        if visible != total:
            self.count_var.set(f"已选 {selected} / {total}（显示 {visible}）")
        else:
            self.count_var.set(f"已选 {selected} / {total}")

    def _notify_selection_changed(self) -> None:
        self._update_count_label()
        if self._on_selection_changed is not None:
            self._on_selection_changed(self.selected_sites())

    def _on_tree_click(self, event) -> str | None:
        if not self._enabled:
            return "break"
        region = self.tree.identify_region(event.x, event.y)
        if region not in {"cell", "tree"}:
            return None
        row_id = self.tree.identify_row(event.y)
        col_id = self.tree.identify_column(event.x)
        if row_id and col_id == "#1":
            self.toggle_site(row_id)
            return "break"
        return None

    def _on_tree_double_click(self, event) -> str | None:
        if not self._enabled:
            return "break"
        row_id = self.tree.identify_row(event.y)
        if row_id:
            self.toggle_site(row_id)
            return "break"
        return None

    def _on_space_key(self, _event) -> str:
        if not self._enabled:
            return "break"
        targets = list(self.tree.selection())
        if not targets:
            focused = self.tree.focus()
            if focused:
                targets = [focused]
        changed = False
        for key in targets:
            if key in self._site_by_key:
                if key in self._selected_keys:
                    self._selected_keys.remove(key)
                else:
                    self._selected_keys.add(key)
                self._update_row(key)
                changed = True
        if changed:
            self._notify_selection_changed()
        return "break"

    def _toggle_all_visible(self) -> None:
        if not self._enabled or not self._visible_keys:
            return
        if all(key in self._selected_keys for key in self._visible_keys):
            self.select_none()
        else:
            self.select_all()

    def toggle_site(self, site_id: str) -> bool:
        """Toggle selection of *site_id* (accepts ``site_id`` or ``name``); returns new state."""
        key = self._resolve_key(site_id)
        if key is None:
            return False
        if key in self._selected_keys:
            self._selected_keys.remove(key)
            checked = False
        else:
            self._selected_keys.add(key)
            checked = True
        self._update_row(key)
        self._notify_selection_changed()
        return checked

    def select_all(self) -> None:
        """Check all currently visible (filtered) sites."""
        if not self._enabled:
            return
        before = set(self._selected_keys)
        for key in self._visible_keys:
            self._selected_keys.add(key)
            self._update_row(key)
        if self._selected_keys != before:
            self._notify_selection_changed()

    def select_none(self) -> None:
        """Uncheck all currently visible (filtered) sites."""
        if not self._enabled:
            return
        before = set(self._selected_keys)
        for key in self._visible_keys:
            self._selected_keys.discard(key)
            self._update_row(key)
        if self._selected_keys != before:
            self._notify_selection_changed()

    def invert_selection(self) -> None:
        """Invert checked state for all currently visible (filtered) sites."""
        if not self._enabled:
            return
        before = set(self._selected_keys)
        for key in self._visible_keys:
            if key in self._selected_keys:
                self._selected_keys.remove(key)
            else:
                self._selected_keys.add(key)
            self._update_row(key)
        if self._selected_keys != before:
            self._notify_selection_changed()

    def selected_sites(self) -> list[SiteConfig]:
        """Return checked ``SiteConfig`` objects in provider order."""
        return [site for site in self._sites if self._site_key(site) in self._selected_keys]

    def selected_site_ids(self) -> list[str]:
        """Return canonical keys of checked sites in provider order."""
        return [
            self._site_key(site)
            for site in self._sites
            if self._site_key(site) in self._selected_keys
        ]

    def visible_sites(self) -> list[SiteConfig]:
        """Return currently visible (filtered) sites in provider order."""
        return [self._site_by_key[key] for key in self._visible_keys if key in self._site_by_key]

    def all_sites(self) -> list[SiteConfig]:
        """Return all loaded sites in provider order."""
        return list(self._sites)

    def set_selected(self, site_ids: Iterable[str]) -> None:
        """Replace checked selection with *site_ids* (accepts ``site_id`` or ``name``)."""
        resolved: set[str] = set()
        for raw_id in site_ids or ():
            key = self._resolve_key(str(raw_id))
            if key is not None:
                resolved.add(key)
        changed = resolved != self._selected_keys
        self._selected_keys = resolved
        for key in self._visible_keys:
            self._update_row(key)
        if changed:
            self._notify_selection_changed()
        else:
            self._update_count_label()

    def set_row_status(self, site_id: str, text: str) -> None:
        """Update the status column text for *site_id* (accepts ``site_id`` or ``name``)."""
        key = self._resolve_key(site_id)
        if key is None:
            return
        self._statuses[key] = str(text or "")
        if self._show_status:
            self._update_row(key)

    def clear_statuses(self) -> None:
        """Clear status text for every row."""
        self._statuses.clear()
        if self._show_status:
            for key in self._visible_keys:
                self._update_row(key)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        state = "normal" if self._enabled else "disabled"
        self.search_entry.configure(state=state)
        self.group_box.configure(state="readonly" if self._enabled else "disabled")
        self.select_all_button.configure(state=state)
        self.select_none_button.configure(state=state)
        self.invert_button.configure(state=state)
