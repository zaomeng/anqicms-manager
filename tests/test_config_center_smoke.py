import tkinter as tk
from tkinter import ttk

import pytest

from core.category_service import Category, CategoryCache
from core.config_service import load_config
from core.gallery_service import GalleryService
from core.site_factory import create_site_from_key, parse_publish_endpoint
from ui.config_center import ConfigCenter


FULL_ENDPOINT = "https://www.example.com/api/import/archive?token=secret-token"


class FakeCategoryService:
    def __init__(self, categories=None, error=None):
        self.categories = categories or []
        self.error = error
        self.calls = []

    def fetch(self, endpoint):
        self.calls.append(endpoint)
        if self.error:
            raise self.error
        return list(self.categories)


def make_root():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk environment unavailable: {exc}")
    root.withdraw()
    return root


def make_gallery_service(tmp_path):
    source = tmp_path / "source-images.txt"
    source.write_text("[默认]\nhttps://img.example.com/a.jpg\n", encoding="utf-8")
    service = GalleryService(tmp_path / "设置")
    gallery = service.create_gallery("默认图库", source)
    return service, gallery


def make_site(tmp_path, gallery=None, *, name="demo", endpoint=FULL_ENDPOINT, fid=7):
    parsed = parse_publish_endpoint(endpoint)
    site = create_site_from_key(
        name,
        url=parsed.base_url,
        jiekou=parsed.publish_endpoint,
        fid=fid,
        base_dir=tmp_path,
    )
    site.category_name = "新闻"
    if gallery is not None:
        site.gallery_id = gallery.gallery_id
        site.image_file = gallery.file_path
    return site


def make_dialog(root, tmp_path, sites, *, category_service=None, gallery_service=None, on_updated=None):
    return ConfigCenter(
        root,
        tmp_path / "config.ini",
        sites,
        on_updated or (lambda _sites: None),
        lambda *_: None,
        lambda *_: None,
        category_service=category_service or FakeCategoryService(),
        gallery_service=gallery_service or GalleryService(tmp_path / "设置"),
    )


def child_texts(widget):
    values = []
    for child in widget.winfo_children():
        try:
            text = child.cget("text")
        except tk.TclError:
            text = ""
        if text:
            values.append(str(text))
        values.extend(child_texts(child))
    return values


def test_config_center_shows_integrated_controls_without_schedule_action(tmp_path):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    dialog = make_dialog(root, tmp_path, [make_site(tmp_path, gallery)], gallery_service=service)

    labels = child_texts(dialog)
    assert "完整发布接口" in labels
    assert "发布栏目" in labels
    assert "插图图库" in labels
    assert "每日启动时间" in labels
    assert "库存提醒（篇）" in labels
    assert "编辑发布时间" not in labels
    assert "站点网址" not in labels
    assert "默认分类 ID" not in labels
    assert "图片映射文件" not in labels
    assert str(dialog.gallery_box.cget("state")) == "readonly"
    assert str(dialog.time_picker.hour_box.cget("state")) == "readonly"
    dialog.destroy()
    root.destroy()


def test_config_center_selection_binds_plaintext_full_endpoint_and_gallery(tmp_path):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    sites = [make_site(tmp_path, gallery, name="first"), make_site(tmp_path, gallery, name="second")]
    dialog = make_dialog(root, tmp_path, sites, gallery_service=service)

    dialog._select_site(1)

    assert dialog.current_index == 1
    assert dialog.editor_entries["full_endpoint_display"].get() == FULL_ENDPOINT
    assert dialog.gallery_box.get() == "默认图库"
    assert dialog.category_picker.default_category_var.get() == "新闻（ID 7）"
    assert dialog.site_tree.selection() == ("1",)
    dialog.destroy()
    root.destroy()


def test_config_center_full_endpoint_is_always_plaintext_and_editable(tmp_path):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    dialog = make_dialog(root, tmp_path, [make_site(tmp_path, gallery)], gallery_service=service)

    entry = dialog.editor_entries["full_endpoint_display"]
    assert entry.get() == FULL_ENDPOINT
    assert str(entry.cget("state")) == "normal"
    assert "显示并编辑" not in child_texts(dialog)
    dialog.destroy()
    root.destroy()


def test_selecting_site_automatically_schedules_category_fetch(tmp_path):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    dialog = make_dialog(root, tmp_path, [make_site(tmp_path, gallery)], gallery_service=service)

    assert dialog._category_after_job is not None

    dialog.destroy()
    root.destroy()


def test_switching_sites_restores_categories_cached_for_each_endpoint(tmp_path):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    sites = [
        make_site(tmp_path, gallery, name="first"),
        make_site(tmp_path, gallery, name="second", fid=8),
    ]
    dialog = make_dialog(root, tmp_path, sites, gallery_service=service)
    cached = [Category(7, 0, "新闻"), Category(9, 0, "产品")]
    row = dialog.rows[0]
    row["categories"] = cached
    row["category_fetch_state"] = "success"
    row["category_fetch_endpoint"] = FULL_ENDPOINT

    dialog._select_site(1)
    dialog._select_site(0)

    assert dialog.category_picker.tree.exists("7")
    assert dialog.category_picker.tree.exists("9")
    assert dialog.category_picker.selected() == (7, "新闻")
    dialog.destroy()
    root.destroy()


def test_reopening_config_center_restores_persisted_categories_without_refetch(tmp_path):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    site = make_site(tmp_path, gallery)
    categories = [Category(7, 0, "新闻"), Category(9, 0, "产品")]
    CategoryCache(tmp_path / "设置" / "category_cache.json").put(
        FULL_ENDPOINT, categories
    )

    dialog = make_dialog(root, tmp_path, [site], gallery_service=service)

    assert dialog.category_picker.tree.exists("7")
    assert dialog.category_picker.tree.exists("9")
    assert dialog._category_after_job is None
    dialog.destroy()
    root.destroy()


def test_category_result_for_background_site_is_cached_after_selection_changes(tmp_path):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    second_endpoint = (
        "https://www.second.example/api/import/archive?token=second-token"
    )
    sites = [
        make_site(tmp_path, gallery, name="first"),
        make_site(
            tmp_path,
            gallery,
            name="second",
            endpoint=second_endpoint,
            fid=8,
        ),
    ]
    dialog = make_dialog(root, tmp_path, sites, gallery_service=service)
    request_id = 17
    dialog.rows[0]["category_request_id"] = request_id
    dialog._select_site(1)

    dialog._apply_category_result(
        0,
        request_id,
        FULL_ENDPOINT,
        [Category(7, 0, "新闻")],
        "",
    )

    assert dialog.current_index == 1
    assert dialog.rows[0]["categories"] == [Category(7, 0, "新闻")]
    assert CategoryCache(
        tmp_path / "设置" / "category_cache.json"
    ).get(FULL_ENDPOINT) == [Category(7, 0, "新闻")]
    dialog.destroy()
    root.destroy()


def test_save_parses_full_endpoint_and_persists_automation_fields(tmp_path):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    site = make_site(tmp_path, gallery)
    dialog = make_dialog(root, tmp_path, [site], gallery_service=service)
    dialog.rows[0]["interval_time"].set("75")
    dialog.rows[0]["daily_publish_limit"].set("80")
    dialog.rows[0]["article_warning_threshold"].set("600")
    dialog.time_picker.set("08:30:00")

    dialog._save_all()

    saved = load_config(tmp_path / "config.ini")[0]
    assert saved.url == "https://www.example.com"
    assert saved.jiekou == "/api/import/archive?token=secret-token"
    assert saved.fid == 7
    assert saved.category_name == "新闻"
    assert saved.gallery_id == gallery.gallery_id
    assert saved.image_file == gallery.file_path
    assert saved.interval_time == 75
    assert saved.daily_publish_limit == 80
    assert saved.article_warning_threshold == 600
    assert saved.publish_time == "08:30:00"
    assert saved.publish_times == ["08:30:00"]
    dialog.destroy()
    root.destroy()


def test_save_all_persists_external_article_directory(tmp_path):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    dialog = make_dialog(root, tmp_path, [make_site(tmp_path, gallery)], gallery_service=service)
    external_articles = tmp_path.parent / "producer-output"
    dialog.rows[0]["publish_dir"].set(str(external_articles))

    dialog._save_all()

    assert load_config(tmp_path / "config.ini")[0].publish_dir == external_articles.resolve()
    dialog.destroy()
    root.destroy()


def test_editing_endpoint_immediately_invalidates_stored_category(tmp_path):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    dialog = make_dialog(root, tmp_path, [make_site(tmp_path, gallery)], gallery_service=service)
    dialog.rows[0]["full_endpoint_display"].set(
        "https://new.example.com/api/import/archive?token=new-token"
    )

    dialog._endpoint_changed()

    assert dialog.rows[0]["fid"].get() == ""
    assert dialog.rows[0]["category_name"].get() == ""
    assert dialog._category_after_job is not None
    dialog.destroy()
    root.destroy()


def test_stale_category_result_cannot_overwrite_newer_endpoint(tmp_path):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    dialog = make_dialog(root, tmp_path, [make_site(tmp_path, gallery)], gallery_service=service)
    old_generation = dialog.rows[0]["category_request_id"]
    dialog.rows[0]["category_request_id"] += 1
    before = dialog.message_var.get()

    dialog._apply_category_result(
        0,
        old_generation,
        FULL_ENDPOINT,
        [Category(7, 0, "旧栏目")],
        "",
    )

    assert dialog.category_picker.tree.get_children() == ()
    assert dialog.message_var.get() == before
    dialog.destroy()
    root.destroy()


def test_offline_category_failure_preserves_existing_selection(tmp_path):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    site = make_site(tmp_path, gallery)
    site.category_keywords = {7: ("新闻",)}
    dialog = make_dialog(root, tmp_path, [site], gallery_service=service)
    generation = dialog._category_generation

    dialog._apply_category_result(0, generation, FULL_ENDPOINT, [], "栏目获取失败: offline")
    materialized = dialog._materialize_sites()

    assert materialized[0].fid == 7
    assert materialized[0].category_name == "新闻"
    assert materialized[0].category_keywords == {7: ("新闻",)}
    assert materialized[0].enabled is True
    assert "保留原有栏目" in dialog.message_var.get()
    dialog.destroy()
    root.destroy()


def test_successful_category_fetch_cleans_only_missing_keyword_category_ids(tmp_path):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    site = make_site(tmp_path, gallery)
    site.category_keywords = {7: ("新闻",), 9: ("旧案例",)}
    dialog = make_dialog(root, tmp_path, [site], gallery_service=service)
    generation = dialog._category_generation

    dialog._apply_category_result(
        0,
        generation,
        FULL_ENDPOINT,
        [Category(7, 0, "新闻")],
        "",
    )

    assert dialog.rows[0]["category_keywords"] == {7: ("新闻",)}
    assert dialog.category_picker.keyword_mapping() == {7: ("新闻",)}
    assert "ID 9" in dialog.message_var.get()
    dialog.destroy()
    root.destroy()


def test_successful_fetch_reorders_keywords_to_visible_tree_order(tmp_path):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    site = make_site(tmp_path, gallery)
    site.category_keywords = {2: ("第二",), 3: ("子栏目",)}
    dialog = make_dialog(root, tmp_path, [site], gallery_service=service)
    generation = dialog._category_generation

    dialog._apply_category_result(
        0,
        generation,
        FULL_ENDPOINT,
        [Category(1, 0, "根栏目"), Category(2, 0, "第二栏目"), Category(3, 1, "子栏目")],
        "",
    )

    assert dialog.rows[0]["category_keywords"] == {3: ("子栏目",), 2: ("第二",)}
    dialog.destroy()
    root.destroy()


def test_refresh_commits_active_keyword_editor_before_rebuilding_tree(tmp_path):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    dialog = make_dialog(root, tmp_path, [make_site(tmp_path, gallery)], gallery_service=service)
    generation = dialog._category_generation
    dialog.category_picker.set_categories([Category(7, 0, "新闻")])
    editor = ttk.Entry(dialog.category_picker.tree)
    editor.insert(0, "行业新闻，行业")
    dialog.category_picker._keyword_editor = editor
    dialog.category_picker._editing_item = "7"

    dialog._apply_category_result(0, generation, FULL_ENDPOINT, [Category(7, 0, "新闻")], "")

    assert dialog.rows[0]["category_keywords"] == {7: ("行业新闻", "行业")}
    dialog.destroy()
    root.destroy()


def test_category_keyword_drafts_are_independent_per_site(tmp_path):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    first = make_site(tmp_path, gallery, name="first")
    second = make_site(tmp_path, gallery, name="second")
    first.category_keywords = {7: ("甲",)}
    second.category_keywords = {7: ("乙",)}
    dialog = make_dialog(root, tmp_path, [first, second], gallery_service=service)

    dialog._category_keywords_changed({7: ("甲改",)})
    dialog._select_site(1)

    assert dialog.rows[0]["category_keywords"] == {7: ("甲改",)}
    assert dialog.rows[1]["category_keywords"] == {7: ("乙",)}
    dialog.destroy()
    root.destroy()


def test_site_switch_conflict_restores_previous_tree_row_and_keyword_editor(tmp_path):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    sites = [
        make_site(tmp_path, gallery, name="first"),
        make_site(tmp_path, gallery, name="second"),
    ]
    dialog = make_dialog(root, tmp_path, sites, gallery_service=service)
    dialog.category_picker.set_categories(
        [Category(7, 0, "新闻"), Category(9, 0, "案例")],
        category_keywords={7: ("重复",)},
    )
    editor = ttk.Entry(dialog.category_picker.tree)
    editor.insert(0, "案例, 重复")
    dialog.category_picker._keyword_editor = editor
    dialog.category_picker._editing_item = "9"

    dialog._select_site(1)
    root.update_idletasks()

    assert dialog.current_index == 0
    assert dialog.site_tree.selection() == ("0",)
    assert dialog.site_tree.focus() == "0"
    assert dialog.category_picker._keyword_editor is editor
    assert dialog.category_picker._editing_item == "9"
    assert editor.get() == "案例, 重复"
    assert editor.winfo_exists() == 1
    dialog.destroy()
    root.destroy()


def test_materialize_persists_category_keywords_and_rejects_cross_category_duplicate(tmp_path):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    dialog = make_dialog(root, tmp_path, [make_site(tmp_path, gallery)], gallery_service=service)
    dialog.rows[0]["category_keywords"] = {7: ("新闻",), 9: ("案例",)}

    materialized = dialog._materialize_sites()

    assert materialized[0].category_keywords == {7: ("新闻",), 9: ("案例",)}

    dialog.rows[0]["category_keywords"] = {7: ("重复",), 9: ("重复",)}
    assert dialog._materialize_sites() is None
    assert "同时分配" in dialog.message_var.get()
    dialog.destroy()
    root.destroy()


def test_successful_fetch_missing_stored_category_disables_site_on_save(tmp_path):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    site = make_site(tmp_path, gallery)
    dialog = make_dialog(root, tmp_path, [site], gallery_service=service)
    generation = dialog._category_generation

    dialog._apply_category_result(0, generation, FULL_ENDPOINT, [Category(9, 0, "案例")], "")
    dialog._save_all()

    saved = load_config(tmp_path / "config.ini")[0]
    assert saved.enabled is False
    assert "栏目已失效" in dialog.message_var.get()
    dialog.destroy()
    root.destroy()


def test_selecting_fetched_category_updates_id_and_name(tmp_path):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    site = make_site(tmp_path, gallery, fid=0)
    site.category_name = ""
    dialog = make_dialog(root, tmp_path, [site], gallery_service=service)
    generation = dialog._category_generation
    dialog._apply_category_result(
        0,
        generation,
        FULL_ENDPOINT,
        [Category(1, 0, "新闻"), Category(2, 1, "国内")],
        "",
    )

    dialog.category_picker.tree.selection_set("2")
    dialog._category_selected()

    assert dialog.rows[0]["fid"].get() == "2"
    assert dialog.rows[0]["category_name"].get() == "国内"
    dialog.destroy()
    root.destroy()


def test_enabled_site_without_gallery_is_saved_disabled_with_clear_reason(tmp_path):
    root = make_root()
    site = make_site(tmp_path, gallery=None)
    site.image_file = None
    dialog = make_dialog(root, tmp_path, [site])

    dialog._save_all()

    assert load_config(tmp_path / "config.ini")[0].enabled is False
    assert "未选择图库" in dialog.message_var.get()
    dialog.destroy()
    root.destroy()


def test_invalid_gallery_is_saved_disabled_without_losing_binding(tmp_path):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    site = make_site(tmp_path, gallery)
    gallery.file_path.write_text("[默认]\nnot-a-url\n", encoding="utf-8")
    dialog = make_dialog(root, tmp_path, [site], gallery_service=service)

    dialog._save_all()

    saved = load_config(tmp_path / "config.ini")[0]
    assert saved.enabled is False
    assert saved.gallery_id == gallery.gallery_id
    assert "图库不可用" in dialog.message_var.get()
    dialog.destroy()
    root.destroy()


def test_http_full_endpoint_is_materialized_without_mutating_shared_site(tmp_path):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    site = make_site(tmp_path, gallery)
    dialog = make_dialog(root, tmp_path, [site], gallery_service=service)
    dialog.rows[0]["full_endpoint"].set("http://example.com/api/import/archive?token=x")

    materialized = dialog._materialize_sites()

    assert materialized[0].url == "http://example.com"
    assert materialized[0].jiekou == "/api/import/archive?token=x"
    assert site.url == "https://www.example.com"
    assert site.jiekou == "/api/import/archive?token=secret-token"
    dialog.destroy()
    root.destroy()


def test_duplicate_base_urls_are_rejected(tmp_path):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    sites = [make_site(tmp_path, gallery, name="one"), make_site(tmp_path, gallery, name="two")]
    dialog = make_dialog(root, tmp_path, sites, gallery_service=service)

    assert dialog._materialize_sites() is None
    assert dialog.message_var.get() == "站点网址不能重复"
    dialog.destroy()
    root.destroy()


def test_numeric_validation_covers_warning_threshold(tmp_path):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    dialog = make_dialog(root, tmp_path, [make_site(tmp_path, gallery)], gallery_service=service)
    dialog.rows[0]["article_warning_threshold"].set("many")

    assert dialog._materialize_sites() is None
    assert "库存提醒必须是数字" in dialog.message_var.get()
    dialog.destroy()
    root.destroy()


def test_save_failure_keeps_shared_sites_unchanged(tmp_path, monkeypatch):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    site = make_site(tmp_path, gallery)
    dialog = make_dialog(root, tmp_path, [site], gallery_service=service)
    dialog.rows[0]["daily_publish_limit"].set("99")
    monkeypatch.setattr(
        "ui.config_center.save_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    dialog._save_all()

    assert site.daily_publish_limit == 60
    assert dialog.sites[0] is site
    assert "保存失败" in dialog.message_var.get()
    dialog.destroy()
    root.destroy()


def test_delete_cancel_leaves_config_unchanged(tmp_path, monkeypatch):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    site = make_site(tmp_path, gallery)
    dialog = make_dialog(root, tmp_path, [site], gallery_service=service)
    monkeypatch.setattr("ui.config_center.messagebox.askyesno", lambda *_args, **_kwargs: False)

    dialog._delete_selected_site()

    assert dialog.sites == [site]
    assert dialog.winfo_exists() == 1
    dialog.destroy()
    root.destroy()


def test_batch_edit_daily_limit_updates_every_selected_draft(tmp_path, monkeypatch):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    sites = [
        make_site(tmp_path, gallery, name="first"),
        make_site(tmp_path, gallery, name="second"),
    ]
    dialog = make_dialog(root, tmp_path, sites, gallery_service=service)
    dialog.site_tree.selection_set("0", "1")
    monkeypatch.setattr("ui.config_center.simpledialog.askinteger", lambda *_args, **_kwargs: 42)

    dialog._batch_edit_daily_limit()

    assert [row["daily_publish_limit"].get() for row in dialog.rows] == ["42", "42"]
    assert "点击“保存更改”后生效" in dialog.message_var.get()
    dialog.destroy()
    root.destroy()


def test_batch_stop_persists_all_selected_sites(tmp_path):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    sites = [
        make_site(tmp_path, gallery, name="first"),
        make_site(
            tmp_path,
            gallery,
            name="second",
            endpoint="https://www.second.example/api/import/archive?token=secret-token",
        ),
    ]
    dialog = make_dialog(root, tmp_path, sites, gallery_service=service)
    dialog.site_tree.selection_set("0", "1")

    dialog._batch_set_enabled(False)
    dialog._save_all()

    assert [site.enabled for site in load_config(tmp_path / "config.ini")] == [False, False]
    dialog.destroy()
    root.destroy()


def test_delete_selected_sites_ignores_invalid_unsaved_drafts(tmp_path, monkeypatch):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    sites = [
        make_site(tmp_path, gallery, name="first"),
        make_site(tmp_path, gallery, name="second"),
    ]
    dialog = make_dialog(root, tmp_path, sites, gallery_service=service)
    dialog.rows[0]["full_endpoint"].set("http://invalid.example/api/import/archive?token=bad")
    dialog.site_tree.selection_set("1")
    monkeypatch.setattr("ui.config_center.messagebox.askyesno", lambda *_args, **_kwargs: True)

    dialog._delete_selected_sites()

    assert [site.name for site in load_config(tmp_path / "config.ini")] == ["first"]
    assert [site.name for site in dialog.sites] == ["first"]
    assert "已删除 1 个站点" in dialog.message_var.get()
    dialog.destroy()
    root.destroy()


def test_schedule_callback_is_no_longer_opened(tmp_path):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    opened = []
    dialog = ConfigCenter(
        root,
        tmp_path / "config.ini",
        [make_site(tmp_path, gallery)],
        lambda _sites: None,
        lambda *_: opened.append(True),
        lambda *_: None,
        category_service=FakeCategoryService(),
        gallery_service=service,
    )

    dialog._open_schedule_editor()

    assert opened == []
    assert "已合并" in dialog.message_var.get()
    dialog.destroy()
    root.destroy()


def test_config_center_has_consistent_save_and_cancel_shortcuts(tmp_path):
    root = make_root()
    service, gallery = make_gallery_service(tmp_path)
    dialog = make_dialog(root, tmp_path, [make_site(tmp_path, gallery)], gallery_service=service)

    assert dialog.bind("<Control-s>")
    assert dialog.bind("<Escape>")
    assert dialog.cancel_button.winfo_manager() == "pack"
    dialog.destroy()
    root.destroy()
