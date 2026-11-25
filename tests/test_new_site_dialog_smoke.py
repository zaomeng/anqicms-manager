import queue
import threading
import tkinter as tk
from datetime import date
from tkinter import messagebox, ttk

import pytest

from core.category_service import Category, CategoryCache
from core.site_factory import create_site_from_key
from ui.new_site_dialog import NewSiteDialog


WINDOWS_RESERVED_SITE_KEYS = [
    "CON",
    "prn",
    "AuX",
    "nul",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
]


def make_root():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk environment unavailable: {exc}")
    root.withdraw()
    return root


def configure_modern_site(
    dialog,
    tmp_path,
    *,
    endpoint="https://www.example.com/api/import/archive?token=secret",
    category_id=2,
    category_name="行业新闻",
    gallery_name="默认图库",
    category_keywords=None,
):
    source = tmp_path / f"{gallery_name}.txt"
    source.write_text("[默认]\nhttps://img.example.com/a.jpg\n", encoding="utf-8")
    gallery = dialog.gallery_service.create_gallery(gallery_name, source)
    dialog._reload_galleries()
    dialog.gallery_box.set(gallery.name)
    dialog._gallery_selected()
    dialog.full_endpoint_var.set(endpoint)
    if dialog._category_after_job is not None:
        dialog.after_cancel(dialog._category_after_job)
        dialog._category_after_job = None
    dialog.category_picker.set_categories(
        [
            Category(id=1, parent_id=0, title="新闻"),
            Category(id=category_id, parent_id=1, title=category_name),
        ],
        selected_id=category_id,
        category_keywords=category_keywords,
    )
    return gallery


def test_new_site_dialog_instantiates_with_supported_modes():
    root = make_root()
    dialog = NewSiteDialog(root, [], lambda sites: None)

    assert dialog.winfo_exists() == 1
    assert dialog.mode_options == ["quick", "blank"]
    assert "clone" not in dialog.mode_options

    dialog.destroy()
    root.destroy()


def test_new_site_endpoint_immediately_restores_persisted_categories(tmp_path):
    endpoint = "https://www.example.com/api/import/archive?token=secret"
    categories = [Category(1, 0, "新闻"), Category(2, 1, "行业新闻")]
    CategoryCache(tmp_path / "设置" / "category_cache.json").put(
        endpoint, categories
    )
    root = make_root()
    dialog = NewSiteDialog(
        root,
        [],
        lambda sites: None,
        config_path=tmp_path / "config.ini",
    )

    dialog.full_endpoint_var.set(endpoint)

    assert dialog.category_picker.tree.exists("1")
    assert dialog.category_picker.tree.exists("2")
    assert dialog._category_after_job is None
    dialog.destroy()
    root.destroy()


def test_complete_mode_shows_endpoint_automation_and_all_advanced_fields():
    root = make_root()
    dialog = NewSiteDialog(root, [], lambda sites: None)
    dialog.mode_var.set("blank")
    dialog._refresh_mode()

    assert dialog.quick_frame.winfo_manager() == ""
    assert dialog.manual_frame.winfo_manager() != ""
    assert dialog.automation_frame.winfo_manager() != ""
    assert dialog.advanced_frame.winfo_manager() != ""
    assert not hasattr(dialog, "url_var")
    assert not hasattr(dialog, "api_key_var")
    assert not hasattr(dialog, "endpoint_var")
    assert not hasattr(dialog, "fid_var")
    assert not hasattr(dialog, "image_file_var")
    assert not hasattr(dialog, "image_enabled_var")

    dialog.destroy()
    root.destroy()


def test_quick_mode_keeps_advanced_fields_optional():
    root = make_root()
    dialog = NewSiteDialog(root, [], lambda sites: None)

    assert dialog.quick_frame.winfo_manager() != ""
    assert dialog.advanced_frame.winfo_manager() == ""
    dialog.advanced_visible.set(True)
    dialog._toggle_advanced()
    assert dialog.advanced_frame.winfo_manager() != ""
    assert dialog.manual_frame.winfo_manager() == ""

    dialog.destroy()
    root.destroy()


def test_category_worker_only_writes_to_queue_after_dialog_closes(tmp_path, monkeypatch):
    root = make_root()
    dialog = NewSiteDialog(root, [], lambda sites: None, config_path=tmp_path / "config.ini")
    started = threading.Event()
    release = threading.Event()
    queued = threading.Event()
    worker_tk_calls = []

    class RecordingQueue:
        def __init__(self):
            self.values = queue.SimpleQueue()

        def put(self, value):
            self.values.put(value)
            queued.set()

        def get_nowait(self):
            return self.values.get_nowait()

    class BlockingCategoryService:
        def fetch(self, _endpoint):
            started.set()
            assert release.wait(2)
            return [Category(id=1, parent_id=0, title="新闻")]

    original_after = dialog.after

    def guarded_after(*args):
        if threading.current_thread() is not threading.main_thread():
            worker_tk_calls.append(args)
            return None
        return original_after(*args)

    dialog._category_results = RecordingQueue()
    dialog.category_service = BlockingCategoryService()
    monkeypatch.setattr(dialog, "after", guarded_after)
    dialog.full_endpoint_var.set(
        "https://www.example.com/api/import/archive?token=secret"
    )
    if dialog._category_after_job is not None:
        dialog.after_cancel(dialog._category_after_job)

    dialog._fetch_categories()
    assert started.wait(1)
    dialog.destroy()
    release.set()

    assert queued.wait(1)
    assert worker_tk_calls == []
    root.destroy()


def test_category_result_poll_ignores_stale_generation(tmp_path):
    root = make_root()
    dialog = NewSiteDialog(root, [], lambda sites: None, config_path=tmp_path / "config.ini")
    endpoint = "https://www.example.com/api/import/archive?token=secret"
    dialog.full_endpoint_var.set(endpoint)
    if dialog._category_after_job is not None:
        dialog.after_cancel(dialog._category_after_job)
        dialog._category_after_job = None
    generation = dialog._category_generation

    dialog._category_results.put(
        (generation - 1, endpoint, [Category(id=1, parent_id=0, title="过期")], "")
    )
    dialog._category_results.put(
        (generation, endpoint, [Category(id=2, parent_id=0, title="当前")], "")
    )
    dialog._poll_category_results()

    assert dialog.category_picker.tree.exists("2")
    assert not dialog.category_picker.tree.exists("1")
    dialog.destroy()
    root.destroy()


def test_category_worker_reports_sanitized_unexpected_error_and_finishes_polling(tmp_path):
    root = make_root()
    dialog = NewSiteDialog(root, [], lambda sites: None, config_path=tmp_path / "config.ini")
    endpoint = "https://www.example.com/api/import/archive?token=secret"
    queued = threading.Event()

    class ExplodingCategoryService:
        def fetch(self, _endpoint):
            raise RuntimeError("database password leaked")

    class RecordingQueue:
        def __init__(self):
            self.values = queue.SimpleQueue()

        def put(self, value):
            self.values.put(value)
            queued.set()

        def get_nowait(self):
            return self.values.get_nowait()

    dialog.category_service = ExplodingCategoryService()
    dialog._category_results = RecordingQueue()
    dialog.full_endpoint_var.set(endpoint)
    if dialog._category_after_job is not None:
        dialog.after_cancel(dialog._category_after_job)
        dialog._category_after_job = None

    dialog._fetch_categories()
    assert queued.wait(1)
    dialog._poll_category_results()

    assert dialog._category_requests_pending == set()
    assert dialog._category_poll_job is None
    assert "栏目获取失败" in dialog.message_var.get()
    assert "database password" not in dialog.message_var.get()
    dialog.destroy()
    root.destroy()


@pytest.mark.parametrize("mode", ["quick", "blank"])
def test_modern_create_persists_endpoint_category_gallery_and_advanced_values(
    tmp_path, mode
):
    root = make_root()
    added = []
    dialog = NewSiteDialog(root, [], added.extend, config_path=tmp_path / "config.ini")
    dialog.mode_var.set(mode)
    dialog._refresh_mode()
    gallery = configure_modern_site(
        dialog,
        tmp_path,
        endpoint="https://www.example.com/api/import/archive?token=secret",
    )
    dialog.name_var.set("manual-site")
    dialog.interval_time_var.set("7")
    dialog.daily_limit_var.set("11")
    dialog.warning_threshold_var.set("123")
    dialog.publish_times_var.set("09:30:00")
    dialog.time_picker.set("09:30:00")
    dialog.start_today_var.set(True)

    dialog._create_sites()

    site = added[0]
    assert site.name == "manual-site"
    assert site.url == "https://www.example.com"
    assert site.jiekou == "/api/import/archive?token=secret"
    assert (site.fid, site.category_name) == (2, "行业新闻")
    assert site.gallery_id == gallery.gallery_id
    assert site.image_file == gallery.file_path
    assert (site.interval_time, site.daily_publish_limit) == (7, 11)
    assert site.article_warning_threshold == 123
    assert site.publish_times == ["09:30:00"]
    assert site.activation_date == date.today().isoformat()
    assert site.publish_dir.exists()
    assert site.daily_publish_filename.exists()
    root.destroy()


def test_create_persists_edited_category_keywords(tmp_path):
    root = make_root()
    added = []
    dialog = NewSiteDialog(root, [], added.extend, config_path=tmp_path / "config.ini")
    configure_modern_site(
        dialog,
        tmp_path,
        category_keywords={1: ("新闻",), 2: ("行业新闻", "行业")},
    )

    dialog._create_sites()

    assert added[0].category_keywords == {
        1: ("新闻",),
        2: ("行业新闻", "行业"),
    }
    root.destroy()


def test_clone_fetch_keeps_only_keywords_for_category_ids_returned_by_new_endpoint(tmp_path):
    root = make_root()
    template = create_site_from_key(
        "demo",
        url="https://original.example.com",
        jiekou="/api/import/archive?token=old",
        fid=1,
        base_dir=tmp_path,
    )
    template.category_keywords = {1: ("新闻",), 8: ("产品",), 9: ("旧栏目",)}
    dialog = NewSiteDialog(
        root,
        [template],
        lambda _sites: None,
        template_site=template,
        config_path=tmp_path / "config.ini",
    )
    endpoint = dialog.full_endpoint_var.get()
    if dialog._category_after_job is not None:
        dialog.after_cancel(dialog._category_after_job)
        dialog._category_after_job = None
    generation = dialog._category_generation
    dialog._category_results.put(
        (
            generation,
            endpoint,
            [Category(1, 0, "新闻"), Category(8, 0, "产品")],
            "",
        )
    )

    dialog._poll_category_results()

    assert dialog.category_picker.keyword_mapping() == {1: ("新闻",), 8: ("产品",)}
    assert "ID 9" in dialog.message_var.get()
    dialog.destroy()
    root.destroy()


def test_create_blocks_without_full_endpoint(tmp_path):
    root = make_root()
    added = []
    dialog = NewSiteDialog(root, [], added.extend, config_path=tmp_path / "config.ini")

    dialog._create_sites()

    assert added == []
    assert "完整发布接口" in dialog.message_var.get()
    dialog.destroy()
    root.destroy()


def test_create_blocks_without_selected_category(tmp_path):
    root = make_root()
    added = []
    dialog = NewSiteDialog(root, [], added.extend, config_path=tmp_path / "config.ini")
    source = tmp_path / "source.txt"
    source.write_text("[默认]\nhttps://img.example.com/a.jpg\n", encoding="utf-8")
    gallery = dialog.gallery_service.create_gallery("默认图库", source)
    dialog._reload_galleries()
    dialog.gallery_box.set(gallery.name)
    dialog._gallery_selected()
    dialog.full_endpoint_var.set(
        "https://www.example.com/api/import/archive?token=secret"
    )
    if dialog._category_after_job is not None:
        dialog.after_cancel(dialog._category_after_job)

    dialog._create_sites()

    assert added == []
    assert "栏目" in dialog.message_var.get()
    dialog.destroy()
    root.destroy()


def test_create_blocks_non_positive_category_id(tmp_path):
    root = make_root()
    added = []
    dialog = NewSiteDialog(root, [], added.extend, config_path=tmp_path / "config.ini")
    source = tmp_path / "source.txt"
    source.write_text("[默认]\nhttps://img.example.com/a.jpg\n", encoding="utf-8")
    gallery = dialog.gallery_service.create_gallery("默认图库", source)
    dialog._reload_galleries()
    dialog.gallery_box.set(gallery.name)
    dialog._gallery_selected()
    dialog.full_endpoint_var.set(
        "https://www.example.com/api/import/archive?token=secret"
    )
    if dialog._category_after_job is not None:
        dialog.after_cancel(dialog._category_after_job)
    dialog.category_picker.set_categories(
        [Category(id=0, parent_id=0, title="无效栏目")],
        selected_id=0,
    )

    dialog._create_sites()

    assert added == []
    assert "栏目" in dialog.message_var.get()
    dialog.destroy()
    root.destroy()


def test_create_blocks_without_named_gallery(tmp_path):
    root = make_root()
    added = []
    dialog = NewSiteDialog(root, [], added.extend, config_path=tmp_path / "config.ini")
    dialog.full_endpoint_var.set(
        "https://www.example.com/api/import/archive?token=secret"
    )
    if dialog._category_after_job is not None:
        dialog.after_cancel(dialog._category_after_job)
    dialog.category_picker.set_categories(
        [Category(id=3, parent_id=0, title="新闻")],
        selected_id=3,
    )

    dialog._create_sites()

    assert added == []
    assert "图库" in dialog.message_var.get()
    dialog.destroy()
    root.destroy()


def test_create_rejects_negative_advanced_values(tmp_path):
    root = make_root()
    added = []
    dialog = NewSiteDialog(root, [], added.extend, config_path=tmp_path / "config.ini")
    configure_modern_site(
        dialog,
        tmp_path,
        endpoint="https://example.com/api/import/archive?token=secret",
    )
    dialog.interval_time_var.set("-1")

    dialog._create_sites()

    assert added == []
    assert "不能为负数" in dialog.message_var.get()
    dialog.destroy()
    root.destroy()


@pytest.mark.parametrize("site_key", WINDOWS_RESERVED_SITE_KEYS)
def test_create_rejects_windows_reserved_site_keys(tmp_path, site_key):
    root = make_root()
    added = []
    dialog = NewSiteDialog(root, [], added.extend, config_path=tmp_path / "config.ini")
    configure_modern_site(dialog, tmp_path)
    dialog.name_var.set(site_key)

    dialog._create_sites()

    assert added == []
    assert "站点标识无效" in dialog.message_var.get()
    dialog.destroy()
    root.destroy()


def test_duplicate_site_url_is_rejected(tmp_path):
    root = make_root()
    existing = create_site_from_key(
        "existing",
        url="https://example.com",
        jiekou="/api/import/archive?token=old",
        fid=1,
        base_dir=tmp_path,
    )
    added = []
    dialog = NewSiteDialog(
        root,
        [existing],
        added.extend,
        config_path=tmp_path / "config.ini",
    )
    configure_modern_site(
        dialog,
        tmp_path,
        endpoint="https://example.com/api/import/archive?token=secret",
    )

    dialog._create_sites()

    assert added == []
    assert "网址重复" in dialog.message_var.get()
    dialog.destroy()
    root.destroy()


def test_add_callback_failure_rolls_back_created_files(tmp_path):
    root = make_root()
    dialog = NewSiteDialog(
        root,
        [],
        lambda sites: False,
        config_path=tmp_path / "config.ini",
    )
    configure_modern_site(dialog, tmp_path)

    dialog._create_sites()

    assert "保存失败" in dialog.message_var.get()
    assert not (tmp_path / "文章目录" / "example").exists()
    dialog.destroy()
    root.destroy()


def test_clone_uses_complete_endpoint_category_gallery_and_advanced_values(tmp_path):
    root = make_root()
    template = create_site_from_key(
        "demo",
        url="https://original.example.com",
        jiekou="/api/import/archive?token=old",
        fid=1,
        publish_times=["09:00:00"],
        base_dir=tmp_path,
    )
    added = []
    dialog = NewSiteDialog(
        root,
        [template],
        added.extend,
        template_site=template,
        config_path=tmp_path / "config.ini",
    )
    gallery = configure_modern_site(
        dialog,
        tmp_path,
        endpoint="https://copy.example.com/api/import/archive?token=new",
        category_id=8,
        category_name="产品",
    )
    dialog.name_var.set("copy")
    dialog.interval_time_var.set("3")
    dialog.daily_limit_var.set("4")
    dialog.warning_threshold_var.set("20")
    dialog.time_picker.set("12:00:00")

    dialog._create_sites()

    copied = added[0]
    assert copied.url == "https://copy.example.com"
    assert copied.jiekou == "/api/import/archive?token=new"
    assert (copied.fid, copied.category_name) == (8, "产品")
    assert copied.gallery_id == gallery.gallery_id
    assert copied.image_file == gallery.file_path
    assert (copied.interval_time, copied.daily_publish_limit) == (3, 4)
    assert copied.article_warning_threshold == 20
    assert copied.publish_times == ["12:00:00"]
    root.destroy()


def _find_button_by_text(widget, text):
    for child in widget.winfo_children():
        if isinstance(child, ttk.Button) and child.cget("text") == text:
            return child
        found = _find_button_by_text(child, text)
        if found is not None:
            return found
    return None


@pytest.mark.parametrize("close_route", ["cancel", "escape", "wm_close"])
def test_dirty_draft_requires_confirmation_for_every_close_route(
    tmp_path, monkeypatch, close_route
):
    root = make_root()
    dialog = NewSiteDialog(
        root,
        [],
        lambda sites: None,
        config_path=tmp_path / "config.ini",
    )
    dialog.full_endpoint_var.set(
        "https://draft.example.com/api/import/archive?token=secret"
    )
    asked = []
    monkeypatch.setattr(
        messagebox,
        "askyesno",
        lambda *args, **kwargs: asked.append((args, kwargs)) and False,
    )

    if close_route == "cancel":
        cancel_button = _find_button_by_text(dialog, "取消")
        assert cancel_button is not None
        cancel_button.invoke()
    else:
        dialog._request_close()

    assert len(asked) == 1
    assert dialog.winfo_exists() == 1
    assert "draft.example.com" in dialog.full_endpoint_var.get()
    dialog.destroy()
    root.destroy()


@pytest.mark.parametrize("draft_change", ["category", "time", "keywords"])
def test_category_time_and_pending_keyword_drafts_require_close_confirmation(
    tmp_path, monkeypatch, draft_change
):
    root = make_root()
    dialog = NewSiteDialog(root, [], lambda sites: None, config_path=tmp_path / "config.ini")
    dialog.category_picker.set_categories(
        [Category(1, 0, "新闻"), Category(2, 0, "案例")],
    )
    dialog._initial_draft = dialog._draft_state()
    if draft_change == "category":
        dialog.category_picker.tree.selection_set("2")
        dialog.category_picker.tree.focus("2")
    elif draft_change == "time":
        dialog.time_picker.set("12:30:00")
    else:
        editor = ttk.Entry(dialog.category_picker.tree)
        editor.insert(0, "行业新闻")
        dialog.category_picker._keyword_editor = editor
        dialog.category_picker._editing_item = "1"
    asked = []
    monkeypatch.setattr(
        messagebox,
        "askyesno",
        lambda *args, **kwargs: asked.append((args, kwargs)) and False,
    )

    dialog._request_close()

    assert len(asked) == 1
    assert dialog.winfo_exists() == 1
    if draft_change == "keywords":
        assert dialog.category_picker.keyword_mapping() == {1: ("行业新闻",)}
    dialog.destroy()
    root.destroy()


def test_conflicting_pending_keyword_blocks_close_without_discarding_input(tmp_path, monkeypatch):
    root = make_root()
    dialog = NewSiteDialog(root, [], lambda sites: None, config_path=tmp_path / "config.ini")
    dialog.category_picker.set_categories(
        [Category(1, 0, "新闻"), Category(2, 0, "案例")],
        category_keywords={1: ("重复",)},
    )
    dialog._pending_category_keywords = {1: ("重复",)}
    dialog._initial_draft = dialog._draft_state()
    editor = ttk.Entry(dialog.category_picker.tree)
    editor.insert(0, "重复")
    dialog.category_picker._keyword_editor = editor
    dialog.category_picker._editing_item = "2"
    asked = []
    monkeypatch.setattr(
        messagebox,
        "askyesno",
        lambda *args, **kwargs: asked.append((args, kwargs)) and True,
    )

    dialog._request_close()

    assert asked == []
    assert dialog.winfo_exists() == 1
    assert dialog.category_picker._keyword_editor is editor
    assert editor.get() == "重复"
    dialog.destroy()
    root.destroy()
