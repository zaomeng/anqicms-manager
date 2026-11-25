from __future__ import annotations

import re
import tkinter as tk
from collections.abc import Callable, Mapping
from tkinter import ttk

from core.category_service import Category


class CategoryPicker(ttk.Frame):
    def __init__(
        self,
        master,
        height: int = 7,
        *,
        on_keywords_changed: Callable[[dict[int, tuple[str, ...]]], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(master, **kwargs)
        self._on_keywords_changed = on_keywords_changed
        self._on_error = on_error
        self._category_keywords: dict[int, tuple[str, ...]] = {}
        self._category_order: list[int] = []
        self._enabled = True
        self._keyword_editor: ttk.Entry | None = None
        self._editing_item: str | None = None
        self.default_category_var = tk.StringVar(value="未选择")
        status_row = ttk.Frame(self)
        status_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(status_row, text="当前默认栏目").pack(side=tk.LEFT, padx=(0, 8))
        self.default_category_entry = ttk.Entry(
            status_row,
            textvariable=self.default_category_var,
            state="readonly",
        )
        self.default_category_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tree_area = ttk.Frame(self)
        tree_area.pack(fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(
            tree_area,
            columns=("id", "keywords"),
            displaycolumns=("id", "keywords"),
            show="tree headings",
            selectmode="browse",
            height=height,
        )
        self.tree.heading("#0", text="栏目")
        self.tree.heading("id", text="ID")
        self.tree.heading("keywords", text="标题关键词")
        self.tree.column("#0", minwidth=150, width=220, stretch=True)
        self.tree.column("id", minwidth=50, width=60, stretch=False, anchor=tk.W)
        self.tree.column("keywords", minwidth=180, width=300, stretch=True, anchor=tk.W)
        scrollbar = ttk.Scrollbar(
            tree_area,
            orient=tk.VERTICAL,
            command=self.tree.yview,
        )
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self._selection_changed, add="+")
        self.tree.bind("<Double-1>", self._begin_keyword_edit, add="+")

    @staticmethod
    def normalize_keywords(value: str | tuple[str, ...] | list[str]) -> tuple[str, ...]:
        parts = re.split(r"[,，]", value) if isinstance(value, str) else value
        result: list[str] = []
        seen: set[str] = set()
        for part in parts:
            keyword = str(part).strip()
            if keyword and keyword not in seen:
                seen.add(keyword)
                result.append(keyword)
        return tuple(result)

    @classmethod
    def normalize_keyword_mapping(
        cls,
        mapping: Mapping[int, tuple[str, ...] | list[str] | str] | None,
    ) -> dict[int, tuple[str, ...]]:
        normalized: dict[int, tuple[str, ...]] = {}
        for raw_id, raw_keywords in (mapping or {}).items():
            try:
                category_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            keywords = cls.normalize_keywords(raw_keywords)
            if category_id > 0 and keywords:
                normalized[category_id] = keywords
        return normalized

    @staticmethod
    def find_duplicate_keyword(
        mapping: Mapping[int, tuple[str, ...] | list[str]],
    ) -> tuple[str, int, int] | None:
        owners: dict[str, int] = {}
        for category_id, keywords in mapping.items():
            for keyword in keywords:
                previous = owners.setdefault(keyword, category_id)
                if previous != category_id:
                    return keyword, previous, category_id
        return None

    def set_categories(
        self,
        categories: list[Category],
        selected_id: int | None = None,
        category_keywords: Mapping[int, tuple[str, ...] | list[str] | str] | None = None,
        selected_title: str = "",
    ) -> None:
        self.clear()
        self._category_keywords = self.normalize_keyword_mapping(category_keywords)
        pending = {category.id: category for category in categories}
        while pending:
            inserted = False
            for category_id, category in list(pending.items()):
                if category.parent_id and not self.tree.exists(str(category.parent_id)):
                    continue
                parent = str(category.parent_id) if category.parent_id else ""
                keywords = self._category_keywords.get(category.id, ())
                self.tree.insert(
                    parent,
                    tk.END,
                    iid=str(category.id),
                    text=category.title,
                    values=(category.id, ", ".join(keywords)),
                )
                del pending[category_id]
                inserted = True
            if not inserted:
                break
        self._category_order = self._visible_category_order()
        if selected_id is not None and self.tree.exists(str(selected_id)):
            item = str(selected_id)
            self.tree.selection_set(item)
            self.tree.focus(item)
            self.tree.see(item)
            self._update_default_from_selection()
        elif selected_id is not None:
            self.set_default_category(
                selected_id,
                selected_title,
                invalid=True,
            )

    def set_keyword_mapping(
        self,
        mapping: Mapping[int, tuple[str, ...] | list[str] | str] | None,
    ) -> None:
        self._category_keywords = self.normalize_keyword_mapping(mapping)
        for category_id in self._category_order:
            item = str(category_id)
            if self.tree.exists(item):
                values = list(self.tree.item(item, "values"))
                values[1] = ", ".join(self._category_keywords.get(category_id, ()))
                self.tree.item(item, values=values)

    def keyword_mapping(self) -> dict[int, tuple[str, ...]]:
        return {
            category_id: self._category_keywords[category_id]
            for category_id in self._category_order
            if self._category_keywords.get(category_id)
        }

    def commit_pending_edit(self) -> bool:
        editor = self._keyword_editor
        item = self._editing_item
        if editor is None or item is None:
            return True
        category_id = int(item)
        keywords = self.normalize_keywords(editor.get())
        proposed = self.keyword_mapping()
        if keywords:
            proposed[category_id] = keywords
        else:
            proposed.pop(category_id, None)
        duplicate = self.find_duplicate_keyword(proposed)
        if duplicate is not None:
            keyword, first_id, second_id = duplicate
            self._show_error(f"标题关键词“{keyword}”已分配给栏目 ID {first_id}，不能再分配给栏目 ID {second_id}")
            self.refocus_pending_edit()
            return False
        self._category_keywords = proposed
        values = list(self.tree.item(item, "values"))
        values[1] = ", ".join(keywords)
        self.tree.item(item, values=values)
        self._cancel_keyword_edit()
        if self._on_keywords_changed is not None:
            self._on_keywords_changed(self.keyword_mapping())
        return True

    def refocus_pending_edit(self) -> None:
        editor = self._keyword_editor
        if editor is None or not editor.winfo_exists():
            return

        def restore_focus() -> None:
            if self._keyword_editor is editor and editor.winfo_exists():
                editor.focus_set()

        editor.after_idle(restore_focus)

    def clear(self) -> None:
        self._cancel_keyword_edit()
        for item in self.tree.get_children(""):
            self.tree.delete(item)
        self._category_order = []
        self._category_keywords = {}
        self.default_category_var.set("未选择")

    def set_default_category(
        self,
        category_id: int | None,
        title: str = "",
        *,
        invalid: bool = False,
    ) -> None:
        if category_id is None or category_id <= 0:
            self.default_category_var.set("未选择")
            return
        category_title = title.strip() or "未知栏目"
        value = f"{category_title}（ID {category_id}）"
        self.default_category_var.set(f"已失效：{value}" if invalid else value)

    def selected(self) -> tuple[int, str] | None:
        selection = self.tree.selection()
        if not selection:
            return None
        item_id = selection[0]
        return int(item_id), str(self.tree.item(item_id, "text"))

    def _selection_changed(self, _event=None) -> None:
        self._update_default_from_selection()

    def _update_default_from_selection(self) -> None:
        selected = self.selected()
        if selected is None:
            self.default_category_var.set("未选择")
            return
        self.set_default_category(selected[0], selected[1])

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        if not enabled:
            self._cancel_keyword_edit()
        self.tree.configure(selectmode="browse" if enabled else "none")

    def _begin_keyword_edit(self, event) -> str | None:
        if not self._enabled or self.tree.identify_column(event.x) != "#2":
            return None
        item = self.tree.identify_row(event.y)
        if not item:
            return None
        bbox = self.tree.bbox(item, "keywords")
        if not bbox:
            return None
        self._cancel_keyword_edit()
        x, y, width, height = bbox
        editor = ttk.Entry(self.tree)
        current = self._category_keywords.get(int(item), ())
        editor.insert(0, ", ".join(current))
        editor.place(x=x, y=y, width=width, height=height)
        editor.bind("<Return>", self._commit_keyword_edit)
        editor.bind("<FocusOut>", self._commit_keyword_edit)
        editor.bind("<Escape>", self._cancel_keyword_edit)
        self._keyword_editor = editor
        self._editing_item = item
        editor.focus_set()
        editor.selection_range(0, tk.END)
        return "break"

    def _commit_keyword_edit(self, _event=None) -> str:
        self.commit_pending_edit()
        return "break"

    def _cancel_keyword_edit(self, _event=None) -> str:
        editor = self._keyword_editor
        self._keyword_editor = None
        self._editing_item = None
        if editor is not None:
            editor.destroy()
        return "break"

    def _show_error(self, message: str) -> None:
        if self._on_error is not None:
            self._on_error(message)

    def _visible_category_order(self) -> list[int]:
        ordered: list[int] = []

        def visit(parent: str) -> None:
            for item in self.tree.get_children(parent):
                ordered.append(int(item))
                visit(item)

        visit("")
        return ordered
