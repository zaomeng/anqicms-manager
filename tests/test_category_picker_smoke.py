import tkinter as tk
from tkinter import ttk

import pytest

from core.category_service import Category
from ui.category_picker import CategoryPicker


def make_root():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk environment unavailable: {exc}")
    root.withdraw()
    return root


def test_keyword_normalization_accepts_both_commas_and_deduplicates():
    assert CategoryPicker.normalize_keywords(" 实验室，搬家,实验室,, ") == ("实验室", "搬家")


def test_picker_displays_keyword_column_and_exposes_ordered_mapping():
    root = make_root()
    picker = CategoryPicker(root)
    picker.set_categories(
        [Category(3, 0, "实验室"), Category(9, 0, "普通")],
        category_keywords={9: ("普通搬家",), 3: ("实验室搬家", "实验室")},
    )

    assert picker.tree.heading("keywords", "text") == "标题关键词"
    assert picker.tree.item("3", "values")[1] == "实验室搬家, 实验室"
    assert picker.keyword_mapping() == {
        3: ("实验室搬家", "实验室"),
        9: ("普通搬家",),
    }
    picker.destroy()
    root.destroy()


def test_picker_always_shows_current_default_category_and_updates_on_selection():
    root = make_root()
    picker = CategoryPicker(root)
    picker.set_categories(
        [Category(3, 0, "新闻"), Category(9, 0, "产品")],
        selected_id=3,
    )

    assert picker.default_category_var.get() == "新闻（ID 3）"

    picker.tree.selection_set("9")
    picker.tree.event_generate("<<TreeviewSelect>>")
    root.update()

    assert picker.default_category_var.get() == "产品（ID 9）"
    picker.clear()
    assert picker.default_category_var.get() == "未选择"
    picker.destroy()
    root.destroy()


def test_picker_marks_saved_default_category_as_invalid_when_missing():
    root = make_root()
    picker = CategoryPicker(root)

    picker.set_categories(
        [Category(3, 0, "新闻")],
        selected_id=9,
        selected_title="旧产品",
    )

    assert picker.default_category_var.get() == "已失效：旧产品（ID 9）"
    picker.destroy()
    root.destroy()


def test_keyword_mapping_uses_visible_tree_order():
    root = make_root()
    picker = CategoryPicker(root)
    picker.set_categories(
        [
            Category(1, 0, "根栏目"),
            Category(2, 0, "第二栏目"),
            Category(3, 1, "子栏目"),
        ],
        category_keywords={2: ("乙",), 3: ("甲",)},
    )

    assert picker.keyword_mapping() == {3: ("甲",), 2: ("乙",)}
    picker.destroy()
    root.destroy()


def test_keyword_edit_rejects_cross_category_duplicate_with_clear_error():
    root = make_root()
    errors = []
    changed = []
    picker = CategoryPicker(root, on_error=errors.append, on_keywords_changed=changed.append)
    picker.set_categories(
        [Category(3, 0, "实验室"), Category(9, 0, "普通")],
        category_keywords={3: ("实验室",)},
    )
    editor = ttk.Entry(picker.tree)
    editor.insert(0, "普通, 实验室")
    picker._keyword_editor = editor
    picker._editing_item = "9"

    assert picker.commit_pending_edit() is False

    assert picker.keyword_mapping() == {3: ("实验室",)}
    assert changed == []
    assert "实验室" in errors[0]
    assert "ID 3" in errors[0]
    assert "ID 9" in errors[0]
    assert picker._keyword_editor is editor
    assert picker._editing_item == "9"
    assert editor.get() == "普通, 实验室"
    assert editor.winfo_exists() == 1
    picker.destroy()
    root.destroy()


def test_keyword_edit_commits_normalized_mapping():
    root = make_root()
    changed = []
    picker = CategoryPicker(root, on_keywords_changed=changed.append)
    picker.set_categories([Category(3, 0, "实验室")])
    editor = ttk.Entry(picker.tree)
    editor.insert(0, " 实验室，搬家,实验室 ")
    picker._keyword_editor = editor
    picker._editing_item = "3"

    assert picker.commit_pending_edit() is True

    assert picker.keyword_mapping() == {3: ("实验室", "搬家")}
    assert changed == [{3: ("实验室", "搬家")}]
    picker.destroy()
    root.destroy()
