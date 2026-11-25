import tkinter as tk
import json
from datetime import datetime
from types import SimpleNamespace
from tkinter import ttk

import pytest

from core.automation_state import AutomationStateStore
from core.config_service import load_config, save_config
from core.site_factory import create_site_from_key, ensure_site_files
from models.entities import (
    ArticleRecord,
    PlannedRun,
    PublishResult,
    SiteStatusSummary,
    TaskBuildDiagnostic,
)
from ui.main_window import MainWindow


@pytest.fixture(autouse=True)
def suppress_unexpected_modal_errors(monkeypatch):
    monkeypatch.setattr("ui.main_window.messagebox.showerror", lambda *args, **kwargs: None)


def make_root():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk environment unavailable: {exc}")
    root.withdraw()
    return root


def write_config(tmp_path):
    site = create_site_from_key("demo", fid=1, base_dir=tmp_path)
    ensure_site_files(site)
    config_path = tmp_path / "config.ini"
    save_config(config_path, [site])
    return config_path


def apply_initial_scan(window):
    summaries, articles = window.site_service.scan_all_sites(window.sites)
    window._apply_scan_result(summaries, articles, replace_all=True)


def mark_scan_healthy(window, site, pending_count=0):
    window.site_summaries[site.name] = SiteStatusSummary(
        site_name=site.name,
        pending_count=pending_count,
        article_count=pending_count,
        daily_limit=site.daily_publish_limit,
        remaining_quota=site.daily_publish_limit,
        status="idle",
    )


def test_main_window_instantiates(tmp_path):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    assert set(window.metric_vars) == {"总文章", "成功", "失败", "耗时"}
    assert hasattr(window, "plan_tree")
    assert window.root.winfo_exists() == 1
    root.destroy()


def test_main_window_has_toolbar_tabs(tmp_path):
    root = make_root()
    MainWindow(root, config_path=write_config(tmp_path))
    notebooks = []

    def collect(widget):
        if isinstance(widget, ttk.Notebook):
            notebooks.append(widget)
        for child in widget.winfo_children():
            collect(child)

    collect(root)
    tab_sets = [[item.tab(tab_id, "text") for tab_id in item.tabs()] for item in notebooks]

    assert ["发布", "管理", "工具"] in tab_sets
    assert ["站点状态", "今日计划", "详情", "日志", "汇总"] in tab_sets
    root.destroy()


def test_main_toolbar_preserves_existing_action_labels(tmp_path):
    root = make_root()
    MainWindow(root, config_path=write_config(tmp_path))
    labels = []

    def collect(widget):
        if isinstance(widget, ttk.Button):
            labels.append(widget.cget("text"))
        for child in widget.winfo_children():
            collect(child)

    collect(root)

    assert set(labels) >= {
        "全部发布", "立即发布选中", "暂停", "继续", "停止",
        "建站", "配置", "图库", "刷新", "失败归档", "日志目录",
    }
    assert "调度" not in labels
    root.destroy()


def test_tools_tab_has_startup_toggle(tmp_path):
    root = make_root()
    MainWindow(root, config_path=write_config(tmp_path))
    labels = []

    def collect(widget):
        if isinstance(widget, ttk.Checkbutton):
            labels.append(widget.cget("text"))
        for child in widget.winfo_children():
            collect(child)

    collect(root)

    assert "登录后自动启动" in labels
    root.destroy()


def test_startup_default_only_enables_when_registry_value_is_absent(tmp_path):
    class FakeStartup:
        supported = True

        def __init__(self):
            self.command = None
            self.enable_calls = 0
            self.saved_preference = None

        def preference(self):
            return self.saved_preference

        def registered_command(self):
            return self.command

        def build_command(self):
            return "expected"

        def ensure_default_enabled(self):
            if self.command not in {None, "expected"}:
                return False
            self.enable_calls += 1
            self.command = "expected"
            self.saved_preference = True
            return True

    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    fake = FakeStartup()
    window.startup_service = fake
    window.startup_var.set(False)

    window._load_startup_setting()
    assert window.startup_status == "pending_default"
    assert fake.enable_calls == 0

    window._ensure_default_startup()
    assert fake.enable_calls == 1
    assert window.startup_var.get() is True

    fake.command = "another-program"
    fake.saved_preference = None
    fake.enable_calls = 0
    window._load_startup_setting()
    window._ensure_default_startup()
    assert window.startup_status == "foreign"
    assert fake.enable_calls == 0
    root.destroy()


def test_main_window_migrates_legacy_gallery_before_use(tmp_path):
    legacy = tmp_path / "旧图库.txt"
    legacy.write_text("[默认]\nhttps://img.example.com/a.jpg\n", encoding="utf-8")
    site = create_site_from_key("demo", fid=1, base_dir=tmp_path)
    site.image_file = legacy
    site.gallery_id = ""
    config_path = tmp_path / "config.ini"
    save_config(config_path, [site])
    root = make_root()

    window = MainWindow(root, config_path=config_path)

    assert window.sites[0].gallery_id
    gallery = window.gallery_service.require_gallery(window.sites[0].gallery_id)
    assert gallery.file_path == window.sites[0].image_file
    assert gallery.file_path.parent == tmp_path / "设置" / "图库"
    root.destroy()


def test_plan_tree_uses_total_count_column(tmp_path):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))

    assert window.plan_tree["columns"] == (
        "time", "site", "status", "total", "interval", "earliest_finish", "summary"
    )
    root.destroy()


def test_automation_batch_is_running_before_task_start(tmp_path):
    root = make_root()
    config_path = write_config(tmp_path)
    window = MainWindow(root, config_path=config_path)
    run_date = "2026-06-30"
    site = window.sites[0]
    observed = []

    def fake_run_tasks(tasks, on_log=None, on_result=None, on_site_complete=None):
        observed.append(window.automation_store.get(run_date, site.site_id).status)

    window.task_manager.run_tasks = fake_run_tasks

    window._run_tasks(
        [(site, object())],
        "scheduled",
        batch_context={site.name: (run_date, site.site_id)},
    )

    assert observed == ["running"]
    root.destroy()


def test_schedule_tick_builds_one_full_site_batch(tmp_path):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    today = datetime.now().strftime("%Y-%m-%d")
    site = window.sites[0]
    site.enabled = True
    site.activation_date = "1970-01-01"
    site.daily_publish_limit = 10
    first_article = SimpleNamespace(status="pending", title="first")
    second_article = SimpleNamespace(status="pending", title="second")
    scheduled = []
    window.task_manager = SimpleNamespace(
        worker=None,
        build_tasks=lambda sites, articles: [
            (sites[0], article) for article in articles[sites[0].name]
        ],
        last_build_diagnostics={
            "demo": TaskBuildDiagnostic("ready", "已生成 2 个任务")
        },
    )
    window.daily_plan = [
        PlannedRun(site_name="demo", scheduled_time="00:00:00"),
        PlannedRun(site_name="demo", scheduled_time="00:00:00"),
    ]
    window.articles_by_site = {"demo": [first_article, second_article]}
    window._run_tasks = lambda tasks, label, batch_context=None: scheduled.append(
        (tasks, batch_context)
    )
    window._gallery_block_reason = lambda _site: ""
    mark_scan_healthy(window, site, pending_count=2)
    window._fresh_due_scan_ready.add(f"{today}|{site.site_id}")

    window._schedule_tick()

    assert len(scheduled) == 1
    tasks, batch_context = scheduled[0]
    assert tasks == [(site, first_article), (site, second_article)]
    assert batch_context == {site.site_id: (today, site.site_id)}
    assert window.automation_store.get(today, site.site_id).status == "queued"
    root.destroy()


def test_schedule_tick_rescans_before_building_due_batch(tmp_path):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    today = datetime.now().strftime("%Y-%m-%d")
    site = window.sites[0]
    site.enabled = True
    site.activation_date = "1970-01-01"
    stale = SimpleNamespace(status="pending", title="stale")
    fresh = SimpleNamespace(status="pending", title="fresh")
    window.daily_plan = [PlannedRun(site_name=site.name, scheduled_time="00:00:00")]
    window.articles_by_site = {site.name: [stale]}
    scans = []
    built_from = []
    scheduled = []

    def build_tasks(sites, articles):
        built_from.extend(articles[site.name])
        return [(sites[0], article) for article in articles[site.name]]

    window.task_manager = SimpleNamespace(
        worker=None,
        build_tasks=build_tasks,
        last_build_diagnostics={
            site.name: TaskBuildDiagnostic("ready", "已生成 1 个任务")
        },
    )
    window._request_scan = lambda sites, **kwargs: scans.append((sites, kwargs))
    window._gallery_block_reason = lambda _site: ""
    window._run_tasks = lambda tasks, label, batch_context=None: scheduled.extend(tasks)

    window._schedule_tick(schedule_next=False)

    assert len(scans) == 1
    assert built_from == []
    window.articles_by_site = {site.name: [fresh]}
    mark_scan_healthy(window, site, pending_count=1)
    window._due_scan_keys.clear()
    window._fresh_due_scan_ready.add(f"{today}|{site.site_id}")
    window._schedule_tick(schedule_next=False)

    assert built_from == [fresh]
    assert scheduled == [(site, fresh)]
    root.destroy()


def test_same_time_sites_share_one_due_scan_and_one_parallel_task_run(tmp_path):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    today = datetime.now().strftime("%Y-%m-%d")
    first = window.sites[0]
    first.enabled = True
    first.activation_date = "1970-01-01"
    second = create_site_from_key("second", fid=1, base_dir=tmp_path)
    second.activation_date = "1970-01-01"
    window.sites.append(second)
    window.daily_plan = [
        PlannedRun(site_name=first.name, scheduled_time="00:00:00"),
        PlannedRun(site_name=second.name, scheduled_time="00:00:00"),
    ]
    articles = {
        first.name: [SimpleNamespace(status="pending", title="first")],
        second.name: [SimpleNamespace(status="pending", title="second")],
    }
    window.articles_by_site = articles
    scans = []
    runs = []
    window._request_scan = lambda sites, **kwargs: scans.append((sites, kwargs))
    window._gallery_block_reason = lambda _site: ""
    window.task_manager = SimpleNamespace(
        worker=None,
        build_tasks=lambda sites, current: [
            (sites[0], article) for article in current[sites[0].name]
        ],
        last_build_diagnostics={
            first.name: TaskBuildDiagnostic("ready", "ready"),
            second.name: TaskBuildDiagnostic("ready", "ready"),
        },
    )
    window._run_tasks = lambda tasks, label, batch_context=None: runs.append(
        (tasks, batch_context)
    )

    window._schedule_tick(schedule_next=False)

    assert len(scans) == 1
    assert {site.site_id for site in scans[0][0]} == {first.site_id, second.site_id}
    for site in (first, second):
        mark_scan_healthy(window, site, pending_count=1)
        window._due_scan_keys.discard(f"{today}|{site.site_id}")
        window._fresh_due_scan_ready.add(f"{today}|{site.site_id}")
    window._schedule_tick(schedule_next=False)

    assert len(runs) == 1
    assert {site.site_id for site, _article in runs[0][0]} == {
        first.site_id,
        second.site_id,
    }
    assert set(runs[0][1]) == {first.site_id, second.site_id}
    root.destroy()


def test_schedule_tick_completes_zero_task_batch_and_checks_inventory(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    today = datetime.now().strftime("%Y-%m-%d")
    site = window.sites[0]
    site.enabled = True
    site.activation_date = "1970-01-01"
    stale_article = SimpleNamespace(status="pending", title="stale")
    scheduled = []
    window.task_manager = SimpleNamespace(
        worker=None,
        build_tasks=lambda *_args: [],
        last_build_diagnostics={
            "demo": TaskBuildDiagnostic("empty", "没有可发布文章")
        },
    )
    window.daily_plan = [PlannedRun(site_name="demo", scheduled_time="00:00:00")]
    window.articles_by_site = {"demo": [stale_article]}
    window._run_tasks = lambda *args, **kwargs: scheduled.append((args, kwargs))
    window._gallery_block_reason = lambda _site: ""
    mark_scan_healthy(window, site, pending_count=1)
    window._fresh_due_scan_ready.add(f"{today}|{site.site_id}")
    monkeypatch.setattr(window, "_handle_site_complete_ui", lambda *args: None)
    monkeypatch.setattr(
        window,
        "_record_inventory",
        lambda _site, run_date, site_id, _reason: window.automation_store.record_inventory(
            run_date, site_id, count=0, threshold=500, reason="zero_tasks"
        ),
    )

    window._schedule_tick()

    assert scheduled == []
    batch = window.automation_store.get(today, site.site_id)
    assert batch.status == "completed"
    assert batch.stop_reason == "没有可发布文章"
    assert batch.inventory_count == 0
    root.destroy()


def test_zero_task_inventory_failure_does_not_leave_completed_batch(
    tmp_path, monkeypatch
):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    today = datetime.now().strftime("%Y-%m-%d")
    site = window.sites[0]
    site.enabled = True
    site.activation_date = "1970-01-01"
    window.daily_plan = [PlannedRun(site_name=site.name, scheduled_time="00:00:00")]
    window.task_manager = SimpleNamespace(
        worker=None,
        build_tasks=lambda *_args: [],
        last_build_diagnostics={
            site.name: TaskBuildDiagnostic("empty", "没有可发布文章")
        },
    )
    window._gallery_block_reason = lambda _site: ""
    mark_scan_healthy(window, site)
    window._fresh_due_scan_ready.add(f"{today}|{site.site_id}")
    monkeypatch.setattr(window, "_handle_site_complete_ui", lambda *args: None)
    monkeypatch.setattr(
        window,
        "_record_inventory",
        lambda *_args: (_ for _ in ()).throw(OSError("inventory unavailable")),
    )

    window._schedule_tick(schedule_next=False)

    batch = window.automation_store.get(today, site.site_id)
    assert batch.status == "blocked"
    assert batch.stop_code == "scan_error"
    assert "inventory unavailable" in batch.stop_reason
    root.destroy()


def test_schedule_tick_blocks_zero_task_batch_when_local_state_is_unsafe(
    tmp_path, monkeypatch
):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    today = datetime.now().strftime("%Y-%m-%d")
    site = window.sites[0]
    site.enabled = True
    site.activation_date = "1970-01-01"
    window.daily_plan = [PlannedRun(site_name=site.name, scheduled_time="00:00:00")]
    window.task_manager = SimpleNamespace(
        worker=None,
        build_tasks=lambda *_args: [],
        last_build_diagnostics={
            site.name: TaskBuildDiagnostic(
                "pending_attempt",
                "存在未确认的发布请求",
                severity="error",
            )
        },
    )
    window._gallery_block_reason = lambda _site: ""
    mark_scan_healthy(window, site)
    window._fresh_due_scan_ready.add(f"{today}|{site.site_id}")
    monkeypatch.setattr(window, "_handle_site_complete_ui", lambda *args: None)
    monkeypatch.setattr(
        window,
        "_record_inventory",
        lambda _site, run_date, site_id, _reason: window.automation_store.record_inventory(
            run_date, site_id, count=1, threshold=500, reason="zero_tasks"
        ),
    )

    window._schedule_tick(schedule_next=False)

    batch = window.automation_store.get(today, site.site_id)
    assert batch.status == "blocked"
    assert batch.stop_code == "pending_attempt"
    root.destroy()


def test_due_scan_error_blocks_batch_and_is_rechecked_same_day(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    today = datetime.now().strftime("%Y-%m-%d")
    site = window.sites[0]
    site.enabled = True
    site.activation_date = "1970-01-01"
    window.daily_plan = [PlannedRun(site_name=site.name, scheduled_time="00:00:00")]
    window.site_summaries[site.name] = SiteStatusSummary(
        site_name=site.name,
        status="error",
        last_error="分类规则文件无法读取",
    )
    window.task_manager = SimpleNamespace(
        worker=None,
        build_tasks=lambda *_args: (_ for _ in ()).throw(
            AssertionError("扫描失败时不得构建任务")
        ),
    )
    window._fresh_due_scan_ready.add(f"{today}|{site.site_id}")
    monkeypatch.setattr(window, "_handle_site_complete_ui", lambda *args: None)

    window._schedule_tick(schedule_next=False)

    batch = window.automation_store.get(today, site.site_id)
    assert batch.status == "blocked"
    assert batch.stop_code == "scan_error"

    scans = []
    window._request_scan = lambda sites, **kwargs: scans.append((sites, kwargs))
    window._schedule_tick(schedule_next=False)
    assert len(scans) == 1
    assert scans[0][0] == [site]
    root.destroy()


def test_schedule_tick_blocks_site_with_no_gallery(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    today = datetime.now().strftime("%Y-%m-%d")
    site = window.sites[0]
    site.enabled = True
    site.activation_date = "1970-01-01"
    site.gallery_id = ""
    window.daily_plan = [PlannedRun(site_name=site.name, scheduled_time="00:00:00")]
    window.task_manager = SimpleNamespace(
        worker=None,
        build_tasks=lambda *_args: (_ for _ in ()).throw(
            AssertionError("图库无效时不得构建发布任务")
        ),
    )
    mark_scan_healthy(window, site)
    window._fresh_due_scan_ready.add(f"{today}|{site.site_id}")
    monkeypatch.setattr(window, "_handle_site_complete_ui", lambda *args: None)
    monkeypatch.setattr(
        window,
        "_record_inventory",
        lambda _site, run_date, site_id, _reason: window.automation_store.record_inventory(
            run_date, site_id, count=0, threshold=500, reason="gallery_blocked"
        ),
    )

    window._schedule_tick()

    batch = window.automation_store.get(today, site.site_id)
    assert batch.status == "blocked"
    assert batch.stop_reason == "未配置图库"
    root.destroy()


def test_configuration_fix_requeues_same_day_blocked_batch(tmp_path):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    today = datetime.now().strftime("%Y-%m-%d")
    site = window.sites[0]
    site.enabled = True
    site.activation_date = "1970-01-01"
    article = SimpleNamespace(status="pending", title="fresh")
    window.daily_plan = [PlannedRun(site_name=site.name, scheduled_time="00:00:00")]
    window.articles_by_site = {site.name: [article]}
    window.automation_batches = {
        f"{today}|{site.site_id}": window.automation_store.transition(
            today,
            site.site_id,
            "blocked",
            stop_code="configuration",
            stop_reason="栏目错误",
            config_fingerprint="old-config",
        )
    }
    window.task_manager = SimpleNamespace(
        worker=None,
        build_tasks=lambda sites, articles: [(sites[0], articles[site.name][0])],
        last_build_diagnostics={
            site.name: TaskBuildDiagnostic("ready", "已生成 1 个任务")
        },
    )
    window._gallery_block_reason = lambda _site: ""
    mark_scan_healthy(window, site, pending_count=1)
    scheduled = []
    window._run_tasks = lambda tasks, label, batch_context=None: scheduled.extend(tasks)
    window._fresh_due_scan_ready.add(f"{today}|{site.site_id}")

    window._schedule_tick(schedule_next=False)

    assert scheduled == [(site, article)]
    assert window.automation_store.get(today, site.site_id).status == "queued"
    root.destroy()


def test_gallery_content_fix_requeues_without_changing_gallery_id(tmp_path):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    today = datetime.now().strftime("%Y-%m-%d")
    site = window.sites[0]
    site.enabled = True
    site.activation_date = "1970-01-01"
    source = tmp_path / "source-gallery.txt"
    source.write_text(
        "[默认]\nhttps://img.example.com/a.jpg\n",
        encoding="utf-8",
    )
    gallery = window.gallery_service.create_gallery("恢复图库", source)
    site.gallery_id = gallery.gallery_id
    site.image_file = gallery.file_path
    fingerprint = window._site_config_fingerprint(site)
    blocked = window.automation_store.transition(
        today,
        site.site_id,
        "blocked",
        stop_code="gallery_invalid",
        stop_reason="图库不可用",
        config_fingerprint=fingerprint,
    )
    window.automation_batches = {blocked.batch_key: blocked}
    window.daily_plan = [PlannedRun(site_name=site.name, scheduled_time="00:00:00")]
    scans = []
    window._request_scan = lambda sites, **kwargs: scans.append((sites, kwargs))

    gallery.file_path.write_text(
        "[默认]\nhttps://img.example.com/repaired.jpg\n",
        encoding="utf-8",
    )
    assert window._site_config_fingerprint(site) == fingerprint
    window._schedule_tick(schedule_next=False)

    assert len(scans) == 1
    assert window.automation_store.get(today, site.site_id).status == "queued"
    root.destroy()


def test_automation_state_write_failure_does_not_start_task(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    started = []
    window.task_manager.run_tasks = lambda *args, **kwargs: started.append(True)
    monkeypatch.setattr(
        window.automation_store,
        "transition_many",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    site = window.sites[0]

    window._run_tasks(
        [(site, object())],
        "scheduled",
        batch_context={site.name: (window.last_schedule_date, site.site_id)},
    )

    assert started == []
    assert "自动化状态" in window.status_var.get()
    root.destroy()


def test_automation_batches_load_across_window_restart(tmp_path):
    config_path = write_config(tmp_path)
    state_path = tmp_path / "设置" / "automation_state.json"
    today = datetime.now().strftime("%Y-%m-%d")
    site = load_config(config_path)[0]
    store = AutomationStateStore(state_path)
    batch = store.transition(today, site.site_id, "completed")
    root = make_root()

    window = MainWindow(root, config_path=config_path)

    assert batch.batch_key in window.automation_batches
    root.destroy()


def test_corrupt_automation_state_is_rebuilt_from_scan_evidence(tmp_path):
    config_path = write_config(tmp_path)
    state_path = tmp_path / "设置" / "automation_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{broken", encoding="utf-8")
    root = make_root()

    window = MainWindow(root, config_path=config_path)
    apply_initial_scan(window)

    site = window.sites[0]
    batch = window.automation_store.get(datetime.now().strftime("%Y-%m-%d"), site.site_id)
    assert batch is not None
    assert batch.status == "completed"
    assert batch.stop_code == "empty"
    assert batch.recovered_at
    assert list(state_path.parent.glob("automation_state.json.corrupt-*"))
    root.destroy()


def test_corrupt_state_recovery_blocks_site_when_scan_evidence_is_unavailable(tmp_path):
    config_path = write_config(tmp_path)
    site = load_config(config_path)[0]
    site.daily_publish_filename.write_text("broken", encoding="utf-8")
    state_path = tmp_path / "设置" / "automation_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{broken", encoding="utf-8")
    root = make_root()

    window = MainWindow(root, config_path=config_path)
    apply_initial_scan(window)

    batch = window.automation_store.get(
        datetime.now().strftime("%Y-%m-%d"),
        window.sites[0].site_id,
    )
    assert batch is not None
    assert batch.status == "blocked"
    assert batch.stop_code == "scan_error"
    assert batch.counts_trusted is False
    root.destroy()


def test_run_tasks_does_not_start_when_worker_is_running(tmp_path):
    class AliveWorker:
        def is_alive(self):
            return True

    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    window.task_manager.worker = AliveWorker()

    window._run_tasks([(window.sites[0], object())], "开始全部发布")

    assert window.status_var.get() == "已有发布任务正在执行"
    root.destroy()


def test_running_task_blocks_configuration_and_gallery_windows(tmp_path, monkeypatch):
    class AliveWorker:
        def is_alive(self):
            return True

    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    window.task_manager.worker = AliveWorker()
    opened = []
    monkeypatch.setattr("ui.main_window.ConfigCenter", lambda *args, **kwargs: opened.append("config"))
    monkeypatch.setattr("ui.main_window.GalleryManager", lambda *args, **kwargs: opened.append("gallery"))
    monkeypatch.setattr("ui.main_window.NewSiteDialog", lambda *args, **kwargs: opened.append("new"))

    window.open_config_center()
    window.open_gallery_manager()
    window.open_new_site_dialog()

    assert opened == []
    assert "任务运行中" in window.status_var.get()
    root.destroy()


def test_active_scan_blocks_configuration_and_gallery_windows(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    window._scan_pending = 1
    opened = []
    monkeypatch.setattr("ui.main_window.ConfigCenter", lambda *args, **kwargs: opened.append("config"))
    monkeypatch.setattr("ui.main_window.GalleryManager", lambda *args, **kwargs: opened.append("gallery"))
    monkeypatch.setattr("ui.main_window.NewSiteDialog", lambda *args, **kwargs: opened.append("new"))

    window.open_config_center()
    window.open_gallery_manager()
    window.open_new_site_dialog()

    assert opened == []
    assert "扫描" in window.status_var.get()
    root.destroy()


def test_publish_result_updates_metrics_without_full_rescan(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    monkeypatch.setattr(
        window,
        "refresh_data",
        lambda: (_ for _ in ()).throw(AssertionError("逐篇结果不得全量扫描")),
    )
    result = PublishResult(
        site_name="demo",
        title="文章",
        success=True,
        message="ok",
        article_path=tmp_path / "文章.txt",
    )

    window._handle_publish_result(result, 2)

    assert window.success_count == 1
    assert window.progress_value.get() == 50
    root.destroy()


def test_publish_result_log_makes_title_and_article_url_clickable(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    article_url = "https://example.com/article/demo"
    opened = []
    monkeypatch.setattr("ui.main_window.webbrowser.open_new_tab", opened.append)
    result = PublishResult(
        site_name="demo",
        title="可打开的文章",
        success=True,
        message="发布成功",
        article_path=tmp_path / "文章.txt",
        article_url=article_url,
    )

    window._handle_publish_result(result, 1)

    assert article_url in window.log_text.get("1.0", tk.END)
    link_tags = [tag for tag in window.log_text.tag_names() if tag.startswith("article-link-")]
    assert len(link_tags) == 1
    assert len(window.log_text.tag_ranges(link_tags[0])) == 4
    window._open_article_url(article_url)
    assert opened == [article_url]
    root.destroy()


def test_publish_result_updates_live_site_status_without_double_counting(tmp_path):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    site = window.sites[0]
    article = ArticleRecord(
        site_name=site.name,
        title="实时文章",
        path=site.publish_dir / "实时文章.txt",
    )
    window.articles_by_site[site.name] = [article]
    window.site_summaries[site.name] = SiteStatusSummary(
        site_name=site.name,
        pending_count=1,
        article_count=1,
        published_today=0,
        daily_limit=site.daily_publish_limit,
        remaining_quota=site.daily_publish_limit,
    )
    window._render_site_table()
    window._render_site_details(site.name)
    result = PublishResult(
        site_name=site.name,
        title=article.title,
        success=True,
        message="ok",
        article_path=article.path,
        site_id=site.site_id,
    )

    window._handle_publish_result(result, 1)

    summary = window.site_summaries[site.name]
    assert summary.published_today == 1
    assert summary.pending_count == 0
    assert window.site_tree.item(site.site_id, "values")[2] == f"1 / {site.daily_publish_limit}"
    assert window.detail_vars["published"].get() == f"1 / {site.daily_publish_limit}"
    expected_result = f"{article.title} [{result.published_at:%Y-%m-%d %H:%M:%S}]：ok"
    assert window.detail_vars["last_result"].get() == expected_result

    window._handle_site_complete_ui(site, [result], None)

    assert window.site_summaries[site.name].published_today == 1
    assert window.detail_vars["last_result"].get() == expected_result
    root.destroy()


def test_site_completion_before_result_callback_does_not_double_count(tmp_path):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    site = window.sites[0]
    article = ArticleRecord(
        site_name=site.name,
        title="反向回调文章",
        path=site.publish_dir / "反向回调文章.txt",
    )
    window.articles_by_site[site.name] = [article]
    window.site_summaries[site.name] = SiteStatusSummary(
        site_name=site.name,
        pending_count=1,
        article_count=1,
        published_today=0,
        daily_limit=site.daily_publish_limit,
        remaining_quota=site.daily_publish_limit,
    )
    result = PublishResult(
        site_name=site.name,
        title=article.title,
        success=True,
        message="ok",
        article_path=article.path,
        site_id=site.site_id,
    )

    window._handle_site_complete_ui(site, [result], None)
    window._handle_publish_result(result, 1)

    assert window.site_summaries[site.name].published_today == 1
    assert window.site_summaries[site.name].remaining_quota == site.daily_publish_limit - 1
    root.destroy()


def test_site_completion_ui_uses_inventory_snapshot_without_scanning(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    site = window.sites[0]
    article = ArticleRecord(
        site_name=site.name,
        title="文章",
        path=site.publish_dir / "文章.txt",
    )
    window.articles_by_site[site.name] = [article]
    window.site_summaries[site.name] = SiteStatusSummary(
        site_name=site.name,
        pending_count=1,
        published_today=0,
        daily_limit=site.daily_publish_limit,
        remaining_quota=site.daily_publish_limit,
    )
    monkeypatch.setattr(
        window.site_service,
        "list_articles",
        lambda *_args: (_ for _ in ()).throw(AssertionError("UI 不得扫描文章")),
    )
    monkeypatch.setattr(
        window.site_service,
        "summarize_site",
        lambda *_args: (_ for _ in ()).throw(AssertionError("UI 不得汇总站点")),
    )
    result = PublishResult(
        site_name=site.name,
        title=article.title,
        success=True,
        message="ok",
        article_path=article.path,
    )
    run_date = datetime.now().strftime("%Y-%m-%d")
    batch = window.automation_store.transition(
        run_date,
        site.site_id,
        "completed",
        success_count=1,
        planned_count=1,
    )
    batch = window.automation_store.record_inventory(
        run_date,
        site.site_id,
        count=499,
        threshold=500,
        reason="completed",
    )

    window._handle_site_complete_ui(site, [result], batch)

    assert window.site_summaries[site.name].pending_count == 499
    assert window.site_summaries[site.name].published_today == 1
    assert window.articles_by_site[site.name] == []
    root.destroy()


def test_site_double_click_routes_name_and_gallery_columns(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    apply_initial_scan(window)
    row_id = window.site_tree.get_children()[0]
    opened = []
    monkeypatch.setattr(window.site_tree, "identify_region", lambda _x, _y: "cell")
    monkeypatch.setattr(window.site_tree, "identify_row", lambda _y: row_id)
    monkeypatch.setattr("ui.main_window.subprocess.Popen", lambda args: opened.append(args))
    selected_column = {"value": "#2"}
    monkeypatch.setattr(
        window.site_tree,
        "identify_column",
        lambda _x: selected_column["value"],
    )

    window._handle_site_double_click(SimpleNamespace(x=10, y=10))

    assert opened == [["explorer", str(window.sites[0].publish_dir)]]
    assert window.sites[0].publish_dir.is_dir()

    source = tmp_path / "gallery-source.txt"
    source.write_text(
        "[默认]\nhttps://img.example.com/a.jpg\n",
        encoding="utf-8",
    )
    gallery = window.gallery_service.create_gallery("双击图库", source)
    window.sites[0].gallery_id = gallery.gallery_id
    window.sites[0].image_file = gallery.file_path
    selected_column["value"] = "#9"
    window._handle_site_double_click(SimpleNamespace(x=10, y=10))
    assert opened[-1] == ["explorer", str(gallery.file_path.parent)]

    selected_column["value"] = "#5"
    window._handle_site_double_click(SimpleNamespace(x=10, y=10))
    assert len(opened) == 2

    window.sites[0].gallery_id = ""
    selected_column["value"] = "#9"
    window._handle_site_double_click(SimpleNamespace(x=10, y=10))
    assert len(opened) == 2
    assert "未配置图库" in window.status_var.get()

    monkeypatch.setattr(window.site_tree, "identify_region", lambda _x, _y: "heading")
    window._handle_site_double_click(SimpleNamespace(x=10, y=10))
    assert len(opened) == 2
    root.destroy()


def test_inventory_text_warns_only_after_recorded_check(tmp_path):
    site = create_site_from_key("demo", fid=1, base_dir=tmp_path)
    site.article_warning_threshold = 500
    assert MainWindow._inventory_text(site, None) == "待批次后检查"
    batch = AutomationStateStore(tmp_path / "state.json").record_inventory(
        datetime.now().strftime("%Y-%m-%d"),
        site.site_id,
        count=499,
        threshold=500,
    )
    assert MainWindow._inventory_text(site, batch) == "库存不足：499/500"


def test_today_batch_without_inventory_keeps_latest_previous_warning(tmp_path):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    apply_initial_scan(window)
    site = window.sites[0]
    yesterday = "2026-07-14"
    today = datetime.now().strftime("%Y-%m-%d")
    window.automation_store.record_inventory(
        yesterday,
        site.site_id,
        count=20,
        threshold=500,
    )
    window.automation_store.transition(today, site.site_id, "queued")
    window.automation_batches = window.automation_store.load()

    window._render_site_table()

    row = window.site_tree.item(window.site_tree.get_children()[0], "values")
    assert row[9] == "库存不足：20/500"
    root.destroy()


def test_manual_publish_invalid_gallery_stops_before_worker(tmp_path):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    site = window.sites[0]
    article_path = site.publish_dir / "manual.txt"
    article_path.parent.mkdir(parents=True, exist_ok=True)
    article_path.write_text("manual", encoding="utf-8")
    window.articles_by_site = {
        site.name: [
            ArticleRecord(site_name=site.name, title="manual", path=article_path)
        ]
    }
    mark_scan_healthy(window, site, pending_count=1)
    site.enabled = True
    site.url = "https://example.com"
    site.jiekou = "/api/import/archive?token=test"
    site.gallery_id = ""
    site.image_file = None

    window._run_manual_after_scan({site.name})

    assert window.task_manager.worker is None
    assert article_path.exists()
    assert "图库" in window.status_var.get()
    root.destroy()


def test_manual_scan_includes_disabled_sites_without_enabling_them(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    site = window.sites[0]
    site.enabled = False
    requested = []
    monkeypatch.setattr(
        window,
        "_request_scan",
        lambda sites, **kwargs: requested.append((list(sites), kwargs)),
    )

    window._request_manual_scan({site.name})

    assert len(requested) == 1
    assert requested[0][0] == [site]
    assert requested[0][1]["reason"] == "manual"
    assert site.enabled is False
    root.destroy()


def test_publish_all_requests_every_site_including_disabled(tmp_path, monkeypatch):
    first = create_site_from_key("demo", fid=1, base_dir=tmp_path)
    second = create_site_from_key("disabled", fid=2, base_dir=tmp_path)
    second.enabled = False
    ensure_site_files(first)
    ensure_site_files(second)
    config_path = tmp_path / "config.ini"
    save_config(config_path, [first, second])
    root = make_root()
    window = MainWindow(root, config_path=config_path)
    requested = []
    monkeypatch.setattr(window, "_request_manual_scan", lambda names: requested.append(names))

    window.start_publish_all()

    assert requested == [{"demo", "disabled"}]
    assert next(site for site in window.sites if site.name == "disabled").enabled is False
    root.destroy()


def test_manual_publish_single_site_runs_without_confirmation(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    site = window.sites[0]
    article = SimpleNamespace(title="single")
    mark_scan_healthy(window, site, pending_count=1)
    captured = []
    monkeypatch.setattr(
        window,
        "_task_pairs",
        lambda selected_sites=None, allow_disabled=False: [(site, article)],
    )
    monkeypatch.setattr(window, "_run_tasks", lambda tasks, label: captured.append((tasks, label)))
    monkeypatch.setattr(
        "ui.main_window.messagebox.askyesno",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("单站点手动发布不应二次确认")
        ),
    )

    window._run_manual_after_scan({site.name})

    assert captured and captured[0][0] == [(site, article)]
    root.destroy()


def test_manual_publish_multiple_sites_requires_confirmation(tmp_path, monkeypatch):
    first = create_site_from_key("demo", fid=1, base_dir=tmp_path)
    second = create_site_from_key("second", fid=2, base_dir=tmp_path)
    ensure_site_files(first)
    ensure_site_files(second)
    config_path = tmp_path / "config.ini"
    save_config(config_path, [first, second])
    root = make_root()
    window = MainWindow(root, config_path=config_path)
    for site in window.sites:
        mark_scan_healthy(window, site, pending_count=1)
    tasks = [(site, SimpleNamespace(title=site.name)) for site in window.sites]
    captured = []
    monkeypatch.setattr(
        window,
        "_task_pairs",
        lambda selected_sites=None, allow_disabled=False: tasks,
    )
    monkeypatch.setattr(window, "_run_tasks", lambda items, label: captured.append((items, label)))
    monkeypatch.setattr("ui.main_window.messagebox.askyesno", lambda *args, **kwargs: False)

    window._run_manual_after_scan({site.name for site in window.sites})
    assert captured == []

    monkeypatch.setattr("ui.main_window.messagebox.askyesno", lambda *args, **kwargs: True)
    window._run_manual_after_scan({site.name for site in window.sites})
    assert captured == [(tasks, "开始手动发布")]
    root.destroy()


def test_manual_publish_builds_tasks_with_disabled_override(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    site = window.sites[0]
    site.enabled = False
    mark_scan_healthy(window, site, pending_count=1)
    calls = []

    def build_pairs(selected_sites=None, allow_disabled=False):
        calls.append((selected_sites, allow_disabled))
        return []

    monkeypatch.setattr(window, "_task_pairs", build_pairs)
    monkeypatch.setattr(window, "_record_inventory", lambda *args: SimpleNamespace(batch_key="x"))

    window._run_manual_after_scan({site.name})

    assert calls == [({site.name}, True)]
    assert site.enabled is False
    root.destroy()


def test_manual_scan_is_rejected_while_another_scan_is_active(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    window.automation_config_blocked = False
    window.config_error_message = ""
    window._scan_pending = 1
    requested = []
    monkeypatch.setattr(window, "_request_scan", lambda *args, **kwargs: requested.append(True))

    window._request_manual_scan({window.sites[0].name})

    assert requested == []
    assert "扫描" in window.status_var.get()
    root.destroy()


def test_full_refresh_is_rejected_while_manual_scan_is_active(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    window._scan_pending = 1
    started = []
    monkeypatch.setattr(window.site_service, "scan_all_sites", lambda *_args: started.append(True))

    window.refresh_data()

    assert started == []
    assert window._scan_pending == 1
    assert "扫描" in window.status_var.get()
    root.destroy()


def test_refresh_is_queued_while_publish_worker_is_running(tmp_path, monkeypatch):
    class Worker:
        def is_alive(self):
            return True

    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    window.task_manager.worker = Worker()
    requested = []
    monkeypatch.setattr(window, "_request_scan", lambda *args, **kwargs: requested.append((args, kwargs)))

    window.refresh_data()

    assert requested == []
    assert window._refresh_requested is True
    assert "任务" in window.status_var.get()
    root.destroy()


def test_refresh_is_queued_while_manual_workflow_is_active(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    window._manual_workflow_active = True
    requested = []
    monkeypatch.setattr(window, "_request_scan", lambda *args, **kwargs: requested.append((args, kwargs)))

    window.refresh_data()

    assert requested == []
    assert window._refresh_requested is True
    assert "手动发布" in window.status_var.get()
    root.destroy()


def test_multiple_refresh_requests_collapse_to_one_scan_after_idle(tmp_path, monkeypatch):
    class Worker:
        alive = True

        def is_alive(self):
            return self.alive

    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    worker = Worker()
    window.task_manager.worker = worker
    requested = []
    monkeypatch.setattr(window, "_request_scan", lambda *args, **kwargs: requested.append((args, kwargs)))

    window.refresh_data()
    window.refresh_data()
    window.refresh_data()
    worker.alive = False
    window._try_run_queued_refresh()
    window._try_run_queued_refresh()

    assert len(requested) == 1
    assert requested[0][1]["reason"] == "full"
    assert window._refresh_requested is False
    root.destroy()


def test_queued_refresh_waits_when_manual_scan_starts_publish_worker(tmp_path, monkeypatch):
    class Worker:
        def is_alive(self):
            return True

    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    site = window.sites[0]
    summaries = {site.name: SiteStatusSummary(site_name=site.name)}
    window._scan_pending = 1
    window._refresh_requested = True
    requested = []

    def start_worker(_site_names):
        window.task_manager.worker = Worker()
        window._manual_workflow_active = False

    monkeypatch.setattr(window, "_run_manual_after_scan", start_worker)
    monkeypatch.setattr(window, "_request_scan", lambda *args, **kwargs: requested.append((args, kwargs)))
    window._scan_results.put(
        (
            window._scan_generation,
            "manual",
            (),
            "",
            summaries,
            {site.name: []},
            "",
            (site.name,),
        )
    )

    window._poll_scan_results()

    assert requested == []
    assert window._refresh_requested is True
    root.destroy()


def test_queued_refresh_waits_when_due_scan_starts_automatic_worker(tmp_path, monkeypatch):
    class Worker:
        def is_alive(self):
            return True

    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    site = window.sites[0]
    today = datetime.now().strftime("%Y-%m-%d")
    summaries = {site.name: SiteStatusSummary(site_name=site.name)}
    window._scan_pending = 1
    window._refresh_requested = True
    window._due_scan_keys.add(f"{today}|{site.site_id}")
    requested = []

    def start_worker(schedule_next=False):
        assert schedule_next is False
        window.task_manager.worker = Worker()

    monkeypatch.setattr(window, "_schedule_tick", start_worker)
    monkeypatch.setattr(window, "_request_scan", lambda *args, **kwargs: requested.append((args, kwargs)))
    window._scan_results.put(
        (
            window._scan_generation,
            "due",
            (site.site_id,),
            today,
            summaries,
            {site.name: []},
            "",
            (),
        )
    )

    window._poll_scan_results()

    assert requested == []
    assert window._refresh_requested is True
    root.destroy()


def test_rollover_scan_success_commits_date_and_clears_pending_refresh(
    tmp_path, monkeypatch
):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    site = window.sites[0]
    today = datetime.now().strftime("%Y-%m-%d")
    window.last_schedule_date = "1970-01-01"
    window._pending_rollover_date = today
    window._refresh_requested = True
    window._scan_pending = 1
    monkeypatch.setattr(window, "_apply_scan_result", lambda *args, **kwargs: None)
    window._scan_results.put(
        (
            window._scan_generation,
            "full",
            (),
            today,
            {site.name: SiteStatusSummary(site_name=site.name)},
            {site.name: []},
            "",
            (),
        )
    )

    window._poll_scan_results()

    assert window.last_schedule_date == today
    assert window._pending_rollover_date == ""
    assert window._refresh_requested is False
    root.destroy()


def test_rollover_scan_failure_keeps_date_and_uses_retry_backoff(
    tmp_path, monkeypatch
):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    today = datetime.now().strftime("%Y-%m-%d")
    window.last_schedule_date = "1970-01-01"
    window._scan_pending = 1
    scheduled = []
    monkeypatch.setattr(
        window,
        "_schedule_queued_refresh_check",
        lambda delay=250, **kwargs: scheduled.append((delay, kwargs)),
    )
    window._scan_results.put(
        (
            window._scan_generation,
            "full",
            (),
            today,
            {},
            {},
            "disk unavailable",
            (),
        )
    )

    window._poll_scan_results()

    assert window.last_schedule_date == "1970-01-01"
    assert window._pending_rollover_date == today
    assert window._refresh_requested is True
    assert scheduled[0] == (15000, {"replace_existing": True})
    root.destroy()


def test_schedule_tick_defers_without_claiming_batch_while_scan_is_active(
    tmp_path, monkeypatch
):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    window.automation_config_blocked = False
    window.config_error_message = ""
    window._scan_pending = 1
    before = dict(window.automation_batches)
    monkeypatch.setattr(
        window,
        "_collect_due_sites",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("扫描期间不得认领自动批次")
        ),
    )

    window._schedule_tick(schedule_next=False)

    assert window.automation_batches == before
    assert "扫描" in window.status_var.get()
    root.destroy()


def test_schedule_tick_defers_while_manual_workflow_is_active(
    tmp_path, monkeypatch
):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    window.automation_config_blocked = False
    window.config_error_message = ""
    window._manual_workflow_active = True
    before = dict(window.automation_batches)
    monkeypatch.setattr(
        window,
        "_collect_due_sites",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("批量确认期间不得认领自动批次")
        ),
    )

    window._schedule_tick(schedule_next=False)

    assert window.automation_batches == before
    root.destroy()


def test_midnight_tick_defers_refresh_while_manual_workflow_is_active(
    tmp_path, monkeypatch
):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    window.automation_config_blocked = False
    window.config_error_message = ""
    window.last_schedule_date = "1970-01-01"
    window._manual_workflow_active = True
    generation = window._scan_generation
    refreshed = []
    monkeypatch.setattr(window, "refresh_data", lambda: refreshed.append(True))

    window._schedule_tick(schedule_next=False)

    assert refreshed == []
    assert window._scan_generation == generation
    assert window._refresh_requested is True
    root.destroy()


def test_midnight_tick_defers_refresh_while_worker_is_running(tmp_path, monkeypatch):
    class Worker:
        def is_alive(self):
            return True

    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    window.automation_config_blocked = False
    window.config_error_message = ""
    window.last_schedule_date = "1970-01-01"
    window.task_manager.worker = Worker()
    generation = window._scan_generation
    refreshed = []
    monkeypatch.setattr(window, "refresh_data", lambda: refreshed.append(True))

    window._schedule_tick(schedule_next=False)

    assert refreshed == []
    assert window._scan_generation == generation
    assert window._refresh_requested is True
    root.destroy()


def test_manual_no_task_does_not_create_or_update_automation_state(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    site = window.sites[0]
    before_batches = dict(window.automation_batches)
    before_state = window.automation_store.get(
        datetime.now().strftime("%Y-%m-%d"), site.site_id
    )
    monkeypatch.setattr(window, "_task_pairs", lambda **_kwargs: [])
    monkeypatch.setattr(
        window,
        "_record_inventory",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("手动无任务不得写自动库存状态")
        ),
    )

    window._run_manual_after_scan({site.name})

    assert window.automation_batches == before_batches
    assert window.automation_store.get(
        datetime.now().strftime("%Y-%m-%d"), site.site_id
    ) == before_state
    root.destroy()


def test_manual_completion_does_not_update_automation_state(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    site = window.sites[0]
    before_batches = dict(window.automation_batches)
    run_date = datetime.now().strftime("%Y-%m-%d")
    before_state = window.automation_store.get(run_date, site.site_id)
    captured = {}

    def capture_run_tasks(tasks, on_log=None, on_result=None, on_site_complete=None):
        captured["complete"] = on_site_complete

    monkeypatch.setattr(window.task_manager, "run_tasks", capture_run_tasks)
    monkeypatch.setattr(
        window,
        "_record_inventory",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("手动完成不得写自动库存状态")
        ),
    )
    window._run_tasks([(site, object())], "开始手动发布")
    captured["complete"](site, [], "completed")

    assert window.automation_batches == before_batches
    assert window.automation_store.get(run_date, site.site_id) == before_state
    root.destroy()


def test_enable_rejects_gallery_image_path_mismatch(tmp_path):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    site = window.sites[0]
    source = tmp_path / "registered-gallery.txt"
    source.write_text("[默认]\nhttps://img.example/a.jpg\n", encoding="utf-8")
    gallery = window.gallery_service.create_gallery("默认图库", source)
    site.gallery_id = gallery.gallery_id
    site.image_file = tmp_path / "different-gallery.txt"
    site.image_file.write_text("[默认]\nhttps://img.example/b.jpg\n", encoding="utf-8")

    reason = window._site_enable_block_reason(site)

    assert "图库" in reason
    assert "不一致" in reason
    root.destroy()


def test_config_commit_during_scan_invalidates_result_and_queues_refresh(
    tmp_path, monkeypatch
):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    window._scan_pending = 1
    before_generation = window._scan_generation
    monkeypatch.setattr(window, "_revalidate_automation_configuration", lambda: True)

    window._refresh_after_config_commit("配置已保存")

    assert window._scan_generation == before_generation + 1
    assert window._refresh_requested is True
    assert "扫描完成后刷新" in window.status_var.get()
    root.destroy()


def test_gallery_change_uses_serialized_config_refresh(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    calls = []
    monkeypatch.setattr(
        window,
        "_refresh_after_config_commit",
        lambda message: calls.append(message),
    )

    window._handle_gallery_changed()

    assert calls == ["图库已更新"]
    root.destroy()


def test_committed_config_is_rendered_when_revalidation_fails(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    replacement = create_site_from_key(
        "replacement",
        fid=1,
        base_dir=tmp_path,
    )
    window.config_error_message = "自动发布配置仍不可用: registry broken"
    window.automation_config_blocked = True
    rendered = []
    def render_detail(name):
        window.current_site_name = name
        rendered.append(("detail", name))

    monkeypatch.setattr(window, "_revalidate_automation_configuration", lambda: False)
    monkeypatch.setattr(window, "_render_site_table", lambda **_kwargs: rendered.append("sites"))
    monkeypatch.setattr(window, "_render_plan_table", lambda: rendered.append("plan"))
    monkeypatch.setattr(window, "_render_site_details", render_detail)

    window._apply_saved_sites([replacement])

    assert [site.name for site in window.sites] == ["replacement"]
    assert rendered == ["sites", "plan", ("detail", "replacement")]
    assert window.current_site_name == "replacement"
    assert window.automation_config_blocked is True
    assert "registry broken" in window.status_var.get()
    root.destroy()


def test_manual_publish_rejects_unregistered_gallery_with_valid_stale_file(tmp_path):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    site = window.sites[0]
    article_path = site.publish_dir / "manual.txt"
    article_path.parent.mkdir(parents=True, exist_ok=True)
    article_path.write_text("manual", encoding="utf-8")
    stale_gallery = tmp_path / "stale-gallery.txt"
    stale_gallery.write_text(
        "[默认]\nhttps://img.example/stale.jpg\n",
        encoding="utf-8",
    )
    window.articles_by_site = {
        site.name: [ArticleRecord(site_name=site.name, title="manual", path=article_path)]
    }
    mark_scan_healthy(window, site, pending_count=1)
    site.enabled = True
    site.url = "https://example.com"
    site.jiekou = "/api/import/archive?token=test"
    site.gallery_id = "00000000-0000-0000-0000-000000000001"
    site.image_file = stale_gallery

    window._run_manual_after_scan({site.name})

    assert window.task_manager.worker is None
    assert article_path.exists()
    assert "图库" in window.status_var.get()
    root.destroy()


def test_inventory_failure_blocks_automatic_batch_instead_of_completing(
    tmp_path, monkeypatch
):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    site = window.sites[0]
    run_date = datetime.now().strftime("%Y-%m-%d")
    captured = {}

    def capture_run_tasks(tasks, on_log=None, on_result=None, on_site_complete=None):
        captured["complete"] = on_site_complete

    monkeypatch.setattr(window.task_manager, "run_tasks", capture_run_tasks)
    monkeypatch.setattr(
        window,
        "_record_inventory",
        lambda *_args: (_ for _ in ()).throw(OSError("inventory unavailable")),
    )
    window._run_tasks(
        [(site, object())],
        "scheduled",
        batch_context={site.site_id: (run_date, site.site_id)},
    )

    captured["complete"](site, [], "completed")

    batch = window.automation_store.get(run_date, site.site_id)
    assert batch.status == "blocked"
    assert batch.stop_code == "scan_error"
    assert "inventory unavailable" in batch.stop_reason
    root.destroy()


def test_successful_revalidation_clears_global_automation_block(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    window.automation_config_blocked = True
    window.config_error_message = "old failure"
    monkeypatch.setattr("ui.main_window.migrate_site_metadata", lambda *args: False)
    monkeypatch.setattr(window.gallery_service, "list_galleries", lambda: [])
    monkeypatch.setattr(window.archive_service, "migrate_site_identity", lambda *args: False)

    assert window._revalidate_automation_configuration() is True
    assert window.automation_config_blocked is False
    assert window.config_error_message == ""
    root.destroy()


def test_site_tree_uses_status_columns_in_required_order(tmp_path):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    assert window.site_tree["columns"] == (
        "index",
        "name",
        "published_limit",
        "enabled",
        "category",
        "times",
        "interval",
        "pending",
        "gallery",
        "inventory",
    )
    root.destroy()


def test_site_table_renders_config_before_scan_and_uses_value_fallbacks(tmp_path):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    site = window.sites[0]
    assert window.site_tree.get_children() == (site.site_id,)
    assert "扫描中" in window.site_tree.item(site.site_id, "values")
    site.category_name = "新闻资讯"
    site.publish_time = "08:00:00"
    site.publish_times = ["08:00:00", "12:30:00"]
    site.interval_time = 15
    site.daily_publish_limit = 10
    source = tmp_path / "status-gallery.txt"
    source.write_text("[默认]\nhttps://img.example.com/a.jpg\n", encoding="utf-8")
    gallery = window.gallery_service.create_gallery("默认图库", source)
    site.gallery_id = gallery.gallery_id
    site.image_file = gallery.file_path
    window.site_summaries[site.name] = SiteStatusSummary(
        site_name=site.name,
        pending_count=7,
        published_today=3,
        daily_limit=10,
        remaining_quota=7,
    )

    window._render_site_table()

    rows = window.site_tree.get_children()
    assert rows == (site.site_id,)
    values = window.site_tree.item(site.site_id, "values")
    assert values[:9] == (
        "1",
        site.name,
        "3 / 10",
        "启用" if site.enabled else "停用",
        "新闻资讯",
        "08:00:00",
        "15 秒",
        "7",
        "默认图库",
    )

    site.category_name = ""
    window._render_site_table()
    assert window.site_tree.item(site.site_id, "values")[4] == "栏目 ID 1"
    site.fid = 0
    window._render_site_table()
    assert window.site_tree.item(site.site_id, "values")[4] == "未配置"
    root.destroy()


def test_site_table_restores_multiselection_and_focus_by_site_id(tmp_path):
    first = create_site_from_key("demo", fid=1, base_dir=tmp_path)
    second = create_site_from_key("second", fid=2, base_dir=tmp_path)
    ensure_site_files(first)
    ensure_site_files(second)
    config_path = tmp_path / "config.ini"
    save_config(config_path, [first, second])
    root = make_root()
    window = MainWindow(root, config_path=config_path)
    window._render_site_table()

    first_id, second_id = (site.site_id for site in window.sites)
    window.site_tree.selection_set((first_id, second_id))
    window.site_tree.focus(second_id)
    window._render_site_table()

    assert set(window.site_tree.selection()) == {first_id, second_id}
    assert window.site_tree.focus() == second_id
    root.destroy()


def test_selected_focus_site_populates_complete_details(tmp_path):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    site = window.sites[0]
    site.category_name = "默认栏目"
    site.publish_time = "09:00:00"
    site.publish_times = ["09:00:00"]
    site.interval_time = 12
    site.activation_date = "2026-07-01"
    window.site_summaries[site.name] = SiteStatusSummary(
        site_name=site.name,
        pending_count=6,
        published_today=4,
        daily_limit=site.daily_publish_limit,
        remaining_quota=max(site.daily_publish_limit - 4, 0),
        last_result="最近发布成功",
        last_error="最近一次异常",
    )
    window._render_site_table()
    window.site_tree.selection_set(site.site_id)
    window.site_tree.focus(site.site_id)

    window.on_site_selected()

    assert window.current_site_name == site.name
    assert window.detail_vars["name"].get() == site.name
    assert window.detail_vars["url"].get() == (site.url or "-")
    assert window.detail_vars["category"].get() == "默认栏目"
    assert window.detail_vars["times"].get() == "09:00:00"
    assert window.detail_vars["interval"].get() == "12 秒"
    assert window.detail_vars["published"].get() == f"4 / {site.daily_publish_limit}"
    assert window.detail_vars["pending"].get() == "6"
    assert window.detail_vars["last_result"].get() == "最近发布成功"
    assert window.detail_vars["error"].get() == "最近一次异常"
    root.destroy()


def test_double_click_updates_daily_limit_atomically(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    site = window.sites[0]
    old_site = site
    saved = []
    monkeypatch.setattr(window.site_tree, "identify_region", lambda _x, _y: "cell")
    monkeypatch.setattr(window.site_tree, "identify_row", lambda _y: site.site_id)
    monkeypatch.setattr(window.site_tree, "identify_column", lambda _x: "#3")
    monkeypatch.setattr("ui.main_window.simpledialog.askinteger", lambda *args, **kwargs: 17)
    monkeypatch.setattr(
        "ui.main_window.save_config",
        lambda _path, sites: saved.append(list(sites)),
    )
    monkeypatch.setattr(
        window,
        "refresh_data",
        lambda: (_ for _ in ()).throw(
            AssertionError("快速编辑不得触发全站扫描")
        ),
    )

    window._handle_site_double_click(SimpleNamespace(x=10, y=10))

    assert len(saved) == 1
    assert saved[0][0].daily_publish_limit == 17
    assert window.sites[0].daily_publish_limit == 17
    assert window.sites[0] is not old_site
    root.destroy()


def test_quick_number_edit_cancel_does_not_save(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    site = window.sites[0]
    old_interval = site.interval_time
    saved = []
    monkeypatch.setattr(window.site_tree, "identify_region", lambda _x, _y: "cell")
    monkeypatch.setattr(window.site_tree, "identify_row", lambda _y: site.site_id)
    monkeypatch.setattr(window.site_tree, "identify_column", lambda _x: "#7")
    monkeypatch.setattr("ui.main_window.simpledialog.askinteger", lambda *args, **kwargs: None)
    monkeypatch.setattr("ui.main_window.save_config", lambda *args: saved.append(True))

    window._handle_site_double_click(SimpleNamespace(x=10, y=10))

    assert saved == []
    assert window.sites[0].interval_time == old_interval
    root.destroy()


def test_quick_edit_save_failure_keeps_memory_and_table_value(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    site = window.sites[0]
    window._render_site_table()
    old_limit = site.daily_publish_limit
    old_row = window.site_tree.item(site.site_id, "values")
    monkeypatch.setattr(window.site_tree, "identify_region", lambda _x, _y: "cell")
    monkeypatch.setattr(window.site_tree, "identify_row", lambda _y: site.site_id)
    monkeypatch.setattr(window.site_tree, "identify_column", lambda _x: "#3")
    monkeypatch.setattr("ui.main_window.simpledialog.askinteger", lambda *args, **kwargs: 99)
    monkeypatch.setattr(
        "ui.main_window.save_config",
        lambda *_args: (_ for _ in ()).throw(OSError("disk full")),
    )

    window._handle_site_double_click(SimpleNamespace(x=10, y=10))

    assert window.sites[0].daily_publish_limit == old_limit
    assert window.site_tree.item(site.site_id, "values") == old_row
    assert "失败" in window.status_var.get()
    root.destroy()


@pytest.mark.parametrize("busy_attribute,busy_value", [("_scan_pending", 1), ("_config_center_open", True)])
def test_quick_edit_is_blocked_while_busy(
    tmp_path, monkeypatch, busy_attribute, busy_value
):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    site = window.sites[0]
    setattr(window, busy_attribute, busy_value)
    monkeypatch.setattr(window.site_tree, "identify_region", lambda _x, _y: "cell")
    monkeypatch.setattr(window.site_tree, "identify_row", lambda _y: site.site_id)
    monkeypatch.setattr(window.site_tree, "identify_column", lambda _x: "#3")
    monkeypatch.setattr(
        "ui.main_window.simpledialog.askinteger",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("忙碌状态不得打开编辑框")
        ),
    )

    window._handle_site_double_click(SimpleNamespace(x=10, y=10))

    assert any(
        text in window.status_var.get()
        for text in ("暂不能", "正在", "配置中心已打开")
    )
    root.destroy()


def test_quick_edit_rechecks_busy_state_after_number_dialog(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    site = window.sites[0]
    old_limit = site.daily_publish_limit
    saved = []
    monkeypatch.setattr(window.site_tree, "identify_region", lambda _x, _y: "cell")
    monkeypatch.setattr(window.site_tree, "identify_row", lambda _y: site.site_id)
    monkeypatch.setattr(window.site_tree, "identify_column", lambda _x: "#3")

    def become_busy(*_args, **_kwargs):
        window._scan_pending = 1
        return 30

    monkeypatch.setattr("ui.main_window.simpledialog.askinteger", become_busy)
    monkeypatch.setattr("ui.main_window.save_config", lambda *args: saved.append(True))

    window._handle_site_double_click(SimpleNamespace(x=10, y=10))

    assert saved == []
    assert window.sites[0].daily_publish_limit == old_limit
    root.destroy()


def test_quick_edit_is_blocked_while_publish_worker_is_running(tmp_path, monkeypatch):
    class AliveWorker:
        def is_alive(self):
            return True

    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    site = window.sites[0]
    window.task_manager.worker = AliveWorker()
    monkeypatch.setattr(window.site_tree, "identify_region", lambda _x, _y: "cell")
    monkeypatch.setattr(window.site_tree, "identify_row", lambda _y: site.site_id)
    monkeypatch.setattr(window.site_tree, "identify_column", lambda _x: "#7")
    monkeypatch.setattr(
        "ui.main_window.simpledialog.askinteger",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("发布任务期间不得打开编辑框")
        ),
    )

    window._handle_site_double_click(SimpleNamespace(x=10, y=10))

    assert "任务" in window.status_var.get()
    root.destroy()


def test_enable_double_click_rejects_invalid_site_configuration(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    site = window.sites[0]
    site.enabled = False
    site.url = ""
    site.jiekou = ""
    site.fid = 0
    site.gallery_id = ""
    saved = []
    window._render_site_table()
    monkeypatch.setattr(window.site_tree, "identify_region", lambda _x, _y: "cell")
    monkeypatch.setattr(window.site_tree, "identify_row", lambda _y: site.site_id)
    monkeypatch.setattr(window.site_tree, "identify_column", lambda _x: "#4")
    monkeypatch.setattr("ui.main_window.save_config", lambda *args: saved.append(True))

    window._handle_site_double_click(SimpleNamespace(x=10, y=10))

    assert saved == []
    assert window.sites[0].enabled is False
    assert any(word in window.status_var.get() for word in ("网址", "接口", "栏目", "图库"))
    root.destroy()


def test_enable_double_click_persists_when_validation_passes(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    site = window.sites[0]
    site.enabled = False
    saved = []
    window._render_site_table()
    monkeypatch.setattr(window.site_tree, "identify_region", lambda _x, _y: "cell")
    monkeypatch.setattr(window.site_tree, "identify_row", lambda _y: site.site_id)
    monkeypatch.setattr(window.site_tree, "identify_column", lambda _x: "#4")
    monkeypatch.setattr(window, "_site_enable_block_reason", lambda _site: "")
    monkeypatch.setattr(window, "_revalidate_automation_configuration", lambda: True)
    monkeypatch.setattr(
        "ui.main_window.save_config",
        lambda _path, sites: saved.append(list(sites)),
    )

    window._handle_site_double_click(SimpleNamespace(x=10, y=10))

    assert len(saved) == 1
    assert window.sites[0].enabled is True
    root.destroy()


def test_interval_quick_edit_accepts_zero(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    site = window.sites[0]
    dialog_options = []
    monkeypatch.setattr(window.site_tree, "identify_region", lambda _x, _y: "cell")
    monkeypatch.setattr(window.site_tree, "identify_row", lambda _y: site.site_id)
    monkeypatch.setattr(window.site_tree, "identify_column", lambda _x: "#7")

    def ask_integer(*_args, **kwargs):
        dialog_options.append(kwargs)
        return 0

    monkeypatch.setattr("ui.main_window.simpledialog.askinteger", ask_integer)
    monkeypatch.setattr("ui.main_window.save_config", lambda *_args: None)

    window._handle_site_double_click(SimpleNamespace(x=10, y=10))

    assert dialog_options[0]["minvalue"] == 0
    assert window.sites[0].interval_time == 0
    root.destroy()


def test_disable_double_click_is_always_allowed_and_persisted(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    site = window.sites[0]
    site.enabled = True
    site.url = ""
    site.jiekou = ""
    site.fid = 0
    site.gallery_id = ""
    saved = []
    window._render_site_table()
    monkeypatch.setattr(window.site_tree, "identify_region", lambda _x, _y: "cell")
    monkeypatch.setattr(window.site_tree, "identify_row", lambda _y: site.site_id)
    monkeypatch.setattr(window.site_tree, "identify_column", lambda _x: "#4")
    monkeypatch.setattr(
        "ui.main_window.save_config",
        lambda _path, sites: saved.append(list(sites)),
    )

    window._handle_site_double_click(SimpleNamespace(x=10, y=10))

    assert len(saved) == 1
    assert window.sites[0].enabled is False
    root.destroy()


def test_selected_site_surfaces_summary_error_without_expanding_site_table(tmp_path):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    apply_initial_scan(window)
    window.site_summaries["demo"] = SiteStatusSummary(
        site_name="demo",
        status="error",
        last_error="站点状态文件损坏",
    )
    window.articles_by_site["demo"] = []
    window._render_site_table()
    first_row = window.site_tree.get_children()[0]
    window.site_tree.selection_set(first_row)

    window.on_site_selected()

    assert len(window.site_tree["columns"]) == 10
    assert "异常" in window.status_var.get()
    assert "站点状态文件损坏" in window.status_var.get()
    root.destroy()


def test_selected_site_surfaces_article_error_in_status_bar(tmp_path):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    apply_initial_scan(window)
    window.site_summaries["demo"] = SiteStatusSummary(site_name="demo")
    window.articles_by_site["demo"] = [
        ArticleRecord(
            site_name="demo",
            title="损坏文章",
            path=tmp_path / "损坏文章.txt",
            status="error",
            last_error="文章文件读取失败",
        )
    ]
    window._render_site_table()
    first_row = window.site_tree.get_children()[0]
    window.site_tree.selection_set(first_row)

    window.on_site_selected()

    assert "异常" in window.status_var.get()
    assert "文章文件读取失败" in window.status_var.get()
    root.destroy()


def test_switching_from_error_site_clears_stale_status_feedback(tmp_path):
    config_path = write_config(tmp_path)
    second_site = create_site_from_key("healthy", fid=1, base_dir=tmp_path)
    save_config(config_path, [create_site_from_key("demo", fid=1, base_dir=tmp_path), second_site])
    root = make_root()
    window = MainWindow(root, config_path=config_path)
    apply_initial_scan(window)
    window.site_summaries["demo"] = SiteStatusSummary(
        site_name="demo",
        status="error",
        last_error="旧站点错误",
    )
    window.site_summaries["healthy"] = SiteStatusSummary(site_name="healthy")
    window.articles_by_site["demo"] = []
    window.articles_by_site["healthy"] = []
    window._render_site_table()
    rows = {window.site_tree.item(row_id, "values")[1]: row_id for row_id in window.site_tree.get_children()}

    window.site_tree.selection_set(rows["demo"])
    window.on_site_selected()
    assert "旧站点错误" in window.status_var.get()

    window.site_tree.selection_set(rows["healthy"])
    window.on_site_selected()

    assert "旧站点错误" not in window.status_var.get()
    assert "异常" not in window.status_var.get()
    root.destroy()


def test_detail_panel_exposes_complete_site_status(tmp_path):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))

    assert tuple(window.detail_vars) == (
        "name",
        "url",
        "enabled",
        "activation_date",
        "category",
        "gallery",
        "times",
        "interval",
        "published",
        "pending",
        "inventory",
        "last_result",
        "error",
    )
    assert set(window.detail_value_labels) == set(window.detail_vars)
    root.destroy()


def test_summary_panel_keeps_only_core_metrics(tmp_path):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))

    assert tuple(window.metric_vars.keys()) == ("总文章", "成功", "失败", "耗时")
    root.destroy()


def test_window_geometry_state_is_saved(tmp_path):
    root = make_root()
    config_path = write_config(tmp_path)
    window = MainWindow(root, config_path=config_path)
    window.root.deiconify()
    window.root.geometry("1234x777+50+60")
    window.root.update_idletasks()

    window._save_window_geometry()

    state_path = tmp_path / "设置" / "window_state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["geometry"] == "1234x777+50+60"
    root.destroy()


def test_window_geometry_state_is_loaded(tmp_path):
    config_path = write_config(tmp_path)
    state_path = tmp_path / "设置" / "window_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"geometry": "1200x800+33+44"}), encoding="utf-8")
    root = make_root()

    window = MainWindow(root, config_path=config_path)

    assert window._last_window_geometry == "1200x800+33+44"
    root.destroy()


def test_legacy_sash_positions_are_ignored(tmp_path):
    config_path = write_config(tmp_path)
    state_path = tmp_path / "设置" / "window_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"geometry": "1200x800+33+44", "sash_positions": [410, 980]}), encoding="utf-8")
    root = make_root()

    window = MainWindow(root, config_path=config_path)

    assert not hasattr(window, "_saved_sash_positions")
    assert not hasattr(window, "layout_panes")
    root.destroy()


def test_window_geometry_state_is_not_rewritten_when_unchanged(tmp_path):
    class FakeParent:
        def mkdir(self, parents=False, exist_ok=False):
            return None

    class FakeWindowStateFile:
        def __init__(self):
            self.parent = FakeParent()
            self.write_calls = 0

        def write_text(self, content, encoding="utf-8"):
            self.write_calls += 1
            return len(content)

    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    fake_file = FakeWindowStateFile()
    window.window_state_file = fake_file
    window.root.deiconify()
    window.root.geometry("1200x800+33+44")
    window.root.update_idletasks()
    window._last_window_geometry = "1200x800+33+44"

    window._save_window_geometry()

    assert fake_file.write_calls == 0
    root.destroy()


def test_detail_tooltip_text_returns_full_time_schedule(tmp_path):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    window.detail_vars["times"].set("08:00:00, 12:00:00, 18:00:00")

    assert window._tooltip_text_for_detail("times") == "08:00:00, 12:00:00, 18:00:00"
    root.destroy()


def test_active_close_waits_for_worker_before_destroy(tmp_path, monkeypatch):
    class Worker:
        alive = True

        def is_alive(self):
            return self.alive

    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    worker = Worker()
    window.task_manager.worker = worker
    monkeypatch.setattr("ui.main_window.messagebox.askyesno", lambda *args, **kwargs: True)
    destroyed = []
    real_destroy = root.destroy
    monkeypatch.setattr(root, "destroy", lambda: destroyed.append(True))

    window.on_close()
    assert destroyed == []

    worker.alive = False
    window._finish_close_when_idle()
    assert destroyed == [True]
    real_destroy()


def test_idle_shutdown_closes_without_background_wait(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, config_path=write_config(tmp_path))
    destroyed = []
    real_destroy = root.destroy
    monkeypatch.setattr(root, "destroy", lambda: destroyed.append(True))

    window.on_close()

    assert destroyed == [True]
    real_destroy()


def test_window_state_write_failure_does_not_block_shutdown(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, tmp_path / "config.ini")
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("file", encoding="utf-8")
    window.window_state_file = blocker / "window_state.json"
    destroyed = []
    real_destroy = root.destroy
    monkeypatch.setattr(root, "destroy", lambda: destroyed.append(True))

    window.on_close()

    assert destroyed == [True]
    assert window.closing is True
    real_destroy()


def test_add_site_refresh_failure_does_not_reverse_committed_config(tmp_path, monkeypatch):
    root = make_root()
    window = MainWindow(root, tmp_path / "config.ini")
    site = create_site_from_key(
        "demo",
        fid=1,
        url="https://example.com",
        jiekou="/api",
        base_dir=tmp_path,
    )
    monkeypatch.setattr(window, "refresh_data", lambda: (_ for _ in ()).throw(OSError("scan failed")))

    accepted = window._append_sites([site])

    assert accepted is True
    assert [item.name for item in window.sites] == ["demo"]
    assert [item.name for item in load_config(tmp_path / "config.ini")] == ["demo"]
    assert "刷新失败" in window.status_var.get()
    window.on_close()


def test_automation_load_failure_reschedules_without_running():
    class Status:
        value = ""

        def set(self, value):
            self.value = value

    after_calls = []
    window = MainWindow.__new__(MainWindow)
    window.closing = False
    window.automation_config_blocked = False
    window.automation_state_error = ""
    window.last_schedule_date = datetime.now().strftime("%Y-%m-%d")
    window.automation_batches = {}
    window.automation_store = SimpleNamespace(
        load=lambda: (_ for _ in ()).throw(OSError("disk full"))
    )
    window.status_var = Status()
    window.root = SimpleNamespace(after=lambda delay, callback: after_calls.append((delay, callback)))
    window.task_manager = SimpleNamespace(worker=None)

    window._schedule_tick()

    assert window.automation_state_error == "disk full"
    assert "不可用" in window.status_var.value
    assert after_calls == [(15000, window._schedule_tick)]
