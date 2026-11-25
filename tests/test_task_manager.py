import re
import threading
import time
from collections import defaultdict
from datetime import datetime

import pytest

from core.archive_service import ArchiveService
from core.gallery_service import GalleryService
from core.state_store import StateFileError
from core.task_manager import TaskManager
from models.entities import ArticleRecord, PublishResult, SiteConfig


class FakePublisher:
    def __init__(self):
        self.calls = []

    def publish_article(self, site, article):
        self.calls.append((site.name, article.title))
        return PublishResult(
            site_name=site.name,
            title=article.title,
            success=True,
            message="ok",
            article_path=article.path,
        )


class FailingOncePublisher:
    def __init__(self):
        self.calls = []
        self.archive_service = None

    def publish_article(self, site, article):
        self.calls.append((site.name, article.title))
        if article.title == "bad":
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
        return PublishResult(
            site_name=site.name,
            title=article.title,
            success=True,
            message="ok",
            article_path=article.path,
        )


class CleanupFailingArchiveService:
    def cleanup_once_per_day(self):
        raise OSError("cleanup locked")


class CleanupFailingPublisher(FakePublisher):
    def __init__(self):
        super().__init__()
        self.archive_service = CleanupFailingArchiveService()


class ConcurrencyObservingPublisher:
    def __init__(self, delay=0.2):
        self.delay = delay
        self.lock = threading.Lock()
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.same_site_overlap = False
        self.per_site_active = defaultdict(int)

    def publish_article(self, site, article):
        with self.lock:
            self.calls.append((site.name, article.title))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.per_site_active[site.name] += 1
            if self.per_site_active[site.name] > 1:
                self.same_site_overlap = True
        time.sleep(self.delay)
        with self.lock:
            self.per_site_active[site.name] -= 1
            self.active -= 1
        return PublishResult(
            site_name=site.name,
            title=article.title,
            success=True,
            message="ok",
            article_path=article.path,
        )


def build_site(tmp_path, name="demo"):
    image_file = tmp_path / f"{name}-gallery.txt"
    image_file.write_text(
        "[默认]\nhttps://img.example.com/default.jpg\n",
        encoding="utf-8",
    )
    return SiteConfig(
        name=name,
        fid=9,
        url="https://example.com",
        jiekou="/api",
        publish_dir=tmp_path / name,
        daily_publish_filename=tmp_path / f"{name}_daily.txt",
        publish_time="00:00:00",
        interval_time=0,
        daily_publish_limit=7,
        image_file=image_file,
        gallery_id=f"gallery-{name}",
    )


def test_build_tasks_for_selected_site(tmp_path):
    site_a = build_site(tmp_path, "a")
    site_b = build_site(tmp_path, "b")
    articles = {
        "a": [ArticleRecord(site_name="a", title="x", path=tmp_path / "x.txt")],
        "b": [ArticleRecord(site_name="b", title="y", path=tmp_path / "y.txt")],
    }
    manager = TaskManager(FakePublisher())
    tasks = manager.build_tasks([site_a, site_b], articles, selected_sites={"b"})
    assert len(tasks) == 1
    assert tasks[0][0].name == "b"


def test_build_tasks_randomizes_candidates_before_applying_quota(tmp_path, monkeypatch):
    site = build_site(tmp_path, "a")
    site.daily_publish_limit = 2
    articles = {
        site.name: [
            ArticleRecord(site_name=site.name, title=title, path=tmp_path / f"{title}.txt")
            for title in ("one", "two", "three")
        ]
    }
    shuffled = []

    def reverse_candidates(candidates):
        shuffled.append([article.title for article in candidates])
        candidates.reverse()

    monkeypatch.setattr("core.task_manager.random.shuffle", reverse_candidates)

    tasks = TaskManager(FakePublisher()).build_tasks([site], articles)

    assert shuffled == [["one", "two", "three"]]
    assert [article.title for _site, article in tasks] == ["three", "two"]


def test_build_tasks_skips_disabled_sites(tmp_path):
    site_a = build_site(tmp_path, "a")
    site_b = build_site(tmp_path, "b")
    site_b.enabled = False
    articles = {
        "a": [ArticleRecord(site_name="a", title="x", path=tmp_path / "x.txt")],
        "b": [ArticleRecord(site_name="b", title="y", path=tmp_path / "y.txt")],
    }
    manager = TaskManager(FakePublisher())
    tasks = manager.build_tasks([site_a, site_b], articles)
    assert len(tasks) == 1
    assert tasks[0][0].name == "a"


def test_build_tasks_allows_disabled_sites_when_explicitly_requested(tmp_path):
    site = build_site(tmp_path, "a")
    site.enabled = False
    article = ArticleRecord(site_name="a", title="x", path=tmp_path / "x.txt")
    manager = TaskManager(FakePublisher())

    tasks = manager.build_tasks(
        [site],
        {site.name: [article]},
        allow_disabled=True,
    )

    assert len(tasks) == 1
    assert tasks[0][0].name == site.name
    assert manager.last_build_diagnostics[site.name].code == "ready"


def test_build_tasks_allow_disabled_still_enforces_daily_limit(tmp_path):
    site = build_site(tmp_path, "a")
    site.enabled = False
    site.daily_publish_limit = 0
    article = ArticleRecord(site_name="a", title="x", path=tmp_path / "x.txt")
    manager = TaskManager(FakePublisher())

    tasks = manager.build_tasks(
        [site],
        {site.name: [article]},
        allow_disabled=True,
    )

    assert tasks == []
    assert manager.last_build_diagnostics[site.name].code == "quota_exhausted"


def test_build_tasks_allow_disabled_still_validates_gallery(tmp_path):
    site = build_site(tmp_path, "a")
    site.enabled = False
    gallery_service = GalleryService(tmp_path / "设置")
    gallery = gallery_service.create_gallery("默认图库", site.image_file)
    site.gallery_id = gallery.gallery_id
    site.image_file = gallery.file_path
    gallery.file_path.unlink()
    article = ArticleRecord(site_name="a", title="x", path=tmp_path / "x.txt")
    manager = TaskManager(FakePublisher(), gallery_service=gallery_service)

    tasks = manager.build_tasks(
        [site],
        {site.name: [article]},
        allow_disabled=True,
    )

    assert tasks == []
    assert manager.last_build_diagnostics[site.name].code == "gallery_invalid"


def test_build_tasks_allow_disabled_rejects_invalid_endpoint_before_task_creation(tmp_path):
    site = build_site(tmp_path, "a")
    site.enabled = False
    site.url = ""
    site.jiekou = ""
    article = ArticleRecord(site_name="a", title="x", path=tmp_path / "x.txt")
    manager = TaskManager(FakePublisher())

    tasks = manager.build_tasks(
        [site],
        {site.name: [article]},
        allow_disabled=True,
    )

    assert tasks == []
    diagnostic = manager.last_build_diagnostics[site.name]
    assert diagnostic.code == "configuration_blocked"
    assert "网址" in diagnostic.message


def test_build_tasks_empty_selected_sites_means_no_tasks(tmp_path):
    site_a = build_site(tmp_path, "a")
    articles = {"a": [ArticleRecord(site_name="a", title="x", path=tmp_path / "x.txt")]}
    manager = TaskManager(FakePublisher())
    tasks = manager.build_tasks([site_a], articles, selected_sites=set())
    assert tasks == []


def test_build_tasks_skips_error_articles(tmp_path):
    site = build_site(tmp_path, "a")
    articles = {
        "a": [
            ArticleRecord(site_name="a", title="bad", path=tmp_path / "bad.txt", status="error", last_error="decode failed"),
            ArticleRecord(site_name="a", title="good", path=tmp_path / "good.txt", status="pending"),
        ]
    }
    manager = TaskManager(FakePublisher())

    tasks = manager.build_tasks([site], articles)

    assert [article.title for _, article in tasks] == ["good"]


def test_build_tasks_fail_closed_when_publish_state_is_corrupt(tmp_path, monkeypatch):
    site = build_site(tmp_path, "a")
    articles = {"a": [ArticleRecord(site_name="a", title="one", path=tmp_path / "one.txt")]}
    manager = TaskManager(FakePublisher())
    monkeypatch.setattr(
        "core.task_manager.get_published_count",
        lambda _path: (_ for _ in ()).throw(StateFileError("corrupt state")),
    )

    assert manager.build_tasks([site], articles) == []


@pytest.mark.parametrize("empty_legacy_file", [False, True])
def test_build_tasks_initializes_missing_or_empty_daily_state(
    tmp_path, empty_legacy_file
):
    site = build_site(tmp_path, "a")
    if empty_legacy_file:
        site.daily_publish_filename.write_text("", encoding="utf-8")
    article = ArticleRecord(site_name="a", title="one", path=tmp_path / "one.txt")

    tasks = TaskManager(FakePublisher()).build_tasks([site], {"a": [article]})

    assert len(tasks) == 1
    assert tasks[0][0].site_id == site.site_id
    assert tasks[0][0].image_groups_snapshot == {
        "默认": ("https://img.example.com/default.jpg",)
    }
    assert tasks[0][1] is article
    assert site.daily_publish_filename.read_text(encoding="utf-8").splitlines() == [
        datetime.now().strftime("%Y-%m-%d"),
        "0",
    ]


def test_build_tasks_snapshots_ungrouped_root_relative_gallery_links(tmp_path):
    site = build_site(tmp_path, "relative")
    site.image_file.write_text(
        "```text\n/uploads/a.webp?v=1\n//img.example/rejected.jpg\n```\n",
        encoding="utf-8",
    )
    article = ArticleRecord(
        site_name=site.name,
        title="one",
        path=tmp_path / "one.txt",
    )

    tasks = TaskManager(FakePublisher()).build_tasks(
        [site], {site.name: [article]}
    )

    assert tasks[0][0].image_groups_snapshot == {
        "默认": ("/uploads/a.webp?v=1",)
    }


def test_build_tasks_uses_today_success_archive_to_enforce_daily_limit(tmp_path):
    site = build_site(tmp_path, "a")
    site.daily_publish_limit = 1
    site.daily_publish_filename.write_text("2000-01-01\n0", encoding="utf-8")
    article = ArticleRecord(site_name="a", title="next", path=tmp_path / "next.txt")
    archive_service = ArchiveService(
        archive_root=tmp_path / "归档",
        cleanup_state_file=tmp_path / "设置" / "archive_cleanup_state.txt",
    )
    success_dir = archive_service.success_dir(site.name)
    success_dir.mkdir(parents=True)
    (success_dir / "already-published.txt").write_text("done", encoding="utf-8")
    publisher = FakePublisher()
    publisher.archive_service = archive_service

    tasks = TaskManager(publisher).build_tasks([site], {site.name: [article]})

    assert tasks == []


def test_run_tasks_reports_unexpected_exception_and_stops_site(tmp_path):
    site = build_site(tmp_path, "a")
    bad_article = ArticleRecord(site_name="a", title="bad", path=tmp_path / "bad.txt")
    good_article = ArticleRecord(site_name="a", title="good", path=tmp_path / "good.txt")
    publisher = FailingOncePublisher()
    manager = TaskManager(publisher)
    results = []
    logs = []

    manager.run_tasks(
        [(site, bad_article), (site, good_article)],
        on_log=logs.append,
        on_result=results.append,
    )
    manager.worker.join(timeout=3)

    assert [call[1] for call in publisher.calls] == ["bad"]
    assert [result.success for result in results] == [False]
    assert "invalid" in results[0].message
    assert results[0].failure_kind == "unsafe_local_state"
    assert results[0].stop_site is True
    assert any("发布异常" in item for item in logs)


def test_run_tasks_preserves_source_on_unexpected_exception(tmp_path):
    site = build_site(tmp_path, "a")
    bad_path = tmp_path / "bad.txt"
    bad_path.write_text("bad", encoding="utf-8")
    good_path = tmp_path / "good.txt"
    good_path.write_text("good", encoding="utf-8")
    bad_article = ArticleRecord(site_name="a", title="bad", path=bad_path)
    good_article = ArticleRecord(site_name="a", title="good", path=good_path)
    publisher = FailingOncePublisher()
    publisher.archive_service = ArchiveService(
        archive_root=tmp_path / "归档",
        cleanup_state_file=tmp_path / "设置" / "archive_cleanup_state.txt",
    )
    manager = TaskManager(publisher)
    results = []

    manager.run_tasks(
        [(site, bad_article), (site, good_article)],
        on_result=results.append,
    )
    manager.worker.join(timeout=3)

    assert [result.success for result in results] == [False]
    assert bad_path.exists() is True
    assert good_path.exists() is True
    assert not (tmp_path / "归档" / "发布失败").exists()


def test_article_log_places_publish_time_after_title(tmp_path):
    site = build_site(tmp_path, "a")
    article = ArticleRecord(site_name=site.name, title="timed", path=tmp_path / "timed.txt")
    logs = []
    manager = TaskManager(FakePublisher())

    manager.run_tasks([(site, article)], on_log=logs.append)
    manager.worker.join(timeout=2)

    article_log = next(item for item in logs if item.startswith("a -> timed"))
    assert re.fullmatch(r"a -> timed \[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", article_log)


def test_cleanup_failure_does_not_block_publish_queue(tmp_path):
    site = build_site(tmp_path, "a")
    article = ArticleRecord(site_name="a", title="good", path=tmp_path / "good.txt")
    publisher = CleanupFailingPublisher()
    manager = TaskManager(publisher)
    results = []
    logs = []

    manager.run_tasks([(site, article)], on_log=logs.append, on_result=results.append)
    manager.worker.join(timeout=3)

    assert [result.success for result in results] == [True]
    assert publisher.calls == [("a", "good")]
    assert any("每日归档清理失败" in item for item in logs)


def test_run_tasks_parallelizes_across_sites_but_keeps_site_serial(tmp_path):
    site_a = build_site(tmp_path, "a")
    site_b = build_site(tmp_path, "b")
    tasks = [
        (site_a, ArticleRecord(site_name="a", title="a1", path=tmp_path / "a1.txt")),
        (site_a, ArticleRecord(site_name="a", title="a2", path=tmp_path / "a2.txt")),
        (site_b, ArticleRecord(site_name="b", title="b1", path=tmp_path / "b1.txt")),
        (site_b, ArticleRecord(site_name="b", title="b2", path=tmp_path / "b2.txt")),
    ]
    publisher = ConcurrencyObservingPublisher(delay=0.15)
    manager = TaskManager(publisher, max_concurrent_sites=2)
    results = []

    manager.run_tasks(tasks, on_result=results.append)
    manager.worker.join(timeout=5)

    assert len(results) == 4
    assert publisher.max_active == 2
    assert publisher.same_site_overlap is False


def test_run_tasks_respects_max_concurrent_sites_limit(tmp_path):
    site_a = build_site(tmp_path, "a")
    site_b = build_site(tmp_path, "b")
    site_c = build_site(tmp_path, "c")
    tasks = [
        (site_a, ArticleRecord(site_name="a", title="a1", path=tmp_path / "a1.txt")),
        (site_b, ArticleRecord(site_name="b", title="b1", path=tmp_path / "b1.txt")),
        (site_c, ArticleRecord(site_name="c", title="c1", path=tmp_path / "c1.txt")),
    ]
    publisher = ConcurrencyObservingPublisher(delay=0.15)
    manager = TaskManager(publisher, max_concurrent_sites=2)

    manager.run_tasks(tasks)
    manager.worker.join(timeout=5)

    assert publisher.max_active <= 2


def test_wait_until_runnable_returns_false_when_stopped_while_paused():
    manager = TaskManager(FakePublisher())
    manager.pause()
    finished = threading.Event()
    result = []

    thread = threading.Thread(target=lambda: (result.append(manager._wait_until_runnable()), finished.set()))
    thread.start()
    assert finished.wait(0.05) is False

    manager.stop()
    thread.join(timeout=1)

    assert result == [False]


def test_interval_wait_is_interrupted_by_stop():
    manager = TaskManager(FakePublisher())
    finished = threading.Event()
    result = []
    thread = threading.Thread(target=lambda: (result.append(manager._wait_interval(30)), finished.set()))
    thread.start()

    manager.stop()
    assert finished.wait(1) is True
    assert result == [False]


def test_unsafe_local_state_stops_remaining_site_queue(tmp_path):
    class UnsafePublisher(FakePublisher):
        def publish_article(self, site, article):
            result = super().publish_article(site, article)
            result.local_state_safe = False
            return result

    site = build_site(tmp_path, "a")
    publisher = UnsafePublisher()
    manager = TaskManager(publisher)
    articles = [
        ArticleRecord(site_name="a", title="one", path=tmp_path / "one.txt"),
        ArticleRecord(site_name="a", title="two", path=tmp_path / "two.txt"),
    ]
    tasks = manager.build_tasks([site], {site.name: articles})

    manager.run_tasks(tasks)
    manager.worker.join(timeout=2)

    assert len(publisher.calls) == 1
    assert publisher.calls[0][0] == "a"
    assert publisher.calls[0][1] in {"one", "two"}


def test_recovery_oserror_stops_queue_without_archiving_drafts(tmp_path):
    class RecoveryFailingPublisher:
        def __init__(self, archive_service):
            self.archive_service = archive_service
            self.network_calls = 0

        def publish_article(self, site, article):
            return self.archive_service.success_count_today(
                site.name, publish_dir=site.publish_dir
            )

    site = build_site(tmp_path, "a")
    site.publish_dir.mkdir(parents=True)
    one_path = site.publish_dir / "one.txt"
    two_path = site.publish_dir / "two.txt"
    one_path.write_text("one", encoding="utf-8")
    two_path.write_text("two", encoding="utf-8")
    articles = [
        ArticleRecord(site_name="a", title="one", path=one_path),
        ArticleRecord(site_name="a", title="two", path=two_path),
    ]
    archive_service = ArchiveService(tmp_path / "归档")
    success_dir = archive_service.success_dir("a")
    success_dir.mkdir(parents=True)
    (success_dir / ".success-old.txt.marker").write_text(
        "not-a-date\nold.txt", encoding="utf-8"
    )
    publisher = RecoveryFailingPublisher(archive_service)
    manager = TaskManager(publisher)
    results = []

    manager.run_tasks([(site, article) for article in articles], on_result=results.append)
    manager.worker.join(timeout=2)

    assert publisher.network_calls == 0
    assert one_path.exists() and two_path.exists()
    assert len(results) == 1
    assert results[0].local_state_safe is False
    assert not (tmp_path / "归档" / "发布失败").exists()


def test_single_article_does_not_wait_after_completion(tmp_path, monkeypatch):
    site = build_site(tmp_path, "a")
    site.interval_time = 60
    manager = TaskManager(FakePublisher())
    waits = []
    monkeypatch.setattr(manager, "_wait_interval", lambda seconds: waits.append(seconds) or True)

    manager.run_tasks(
        [(site, ArticleRecord(site_name="a", title="one", path=tmp_path / "one.txt"))]
    )
    manager.worker.join(timeout=2)

    assert waits == []


def test_two_articles_wait_only_between_articles(tmp_path, monkeypatch):
    site = build_site(tmp_path, "a")
    site.interval_time = 60
    manager = TaskManager(FakePublisher())
    waits = []
    monkeypatch.setattr(manager, "_wait_interval", lambda seconds: waits.append(seconds) or True)
    articles = [
        ArticleRecord(site_name="a", title="one", path=tmp_path / "one.txt"),
        ArticleRecord(site_name="a", title="two", path=tmp_path / "two.txt"),
    ]
    tasks = manager.build_tasks([site], {site.name: articles})

    manager.run_tasks(tasks)
    manager.worker.join(timeout=2)

    assert waits == [60]


def test_retryable_failure_stops_site_and_reports_retry_wait(tmp_path):
    class RetryPublisher(FakePublisher):
        def publish_article(self, site, article):
            self.calls.append((site.name, article.title))
            return PublishResult(
                site_name=site.name,
                title=article.title,
                success=False,
                message="temporary",
                article_path=article.path,
                failure_kind="transient",
                stop_site=True,
                retryable=True,
            )

    site = build_site(tmp_path, "a")
    publisher = RetryPublisher()
    manager = TaskManager(publisher)
    completions = []
    tasks = [
        (site, ArticleRecord(site_name="a", title="one", path=tmp_path / "one.txt")),
        (site, ArticleRecord(site_name="a", title="two", path=tmp_path / "two.txt")),
    ]

    manager.run_tasks(tasks, on_site_complete=lambda *args: completions.append(args))
    manager.worker.join(timeout=2)

    assert publisher.calls == [("a", "one")]
    assert len(completions) == 1
    assert completions[0][2] == "retry_wait"


def test_result_persistence_failure_stops_remaining_site_queue(tmp_path):
    site = build_site(tmp_path, "a")
    publisher = FakePublisher()
    manager = TaskManager(publisher)
    tasks = [
        (site, ArticleRecord(site_name="a", title="one", path=tmp_path / "one.txt")),
        (site, ArticleRecord(site_name="a", title="two", path=tmp_path / "two.txt")),
    ]
    completions = []

    manager.run_tasks(
        tasks,
        on_result=lambda _result: False,
        on_site_complete=lambda *args: completions.append(args),
    )
    manager.worker.join(timeout=2)

    assert publisher.calls == [("a", "one")]
    assert completions[0][2] == "blocked"


def test_result_persistence_failure_stops_all_site_queues(tmp_path):
    site_a = build_site(tmp_path, "a")
    site_b = build_site(tmp_path, "b")
    publisher = FakePublisher()
    manager = TaskManager(publisher, max_concurrent_sites=2)
    b_result_started = threading.Event()
    fatal_result_recorded = threading.Event()
    completions = []
    tasks = [
        (site_a, ArticleRecord(site_name="a", title="one", path=tmp_path / "a-one.txt")),
        (site_a, ArticleRecord(site_name="a", title="two", path=tmp_path / "a-two.txt")),
        (site_b, ArticleRecord(site_name="b", title="one", path=tmp_path / "b-one.txt")),
        (site_b, ArticleRecord(site_name="b", title="two", path=tmp_path / "b-two.txt")),
    ]

    def persist_result(result):
        if result.site_name == "b":
            b_result_started.set()
            assert fatal_result_recorded.wait(1)
            return True
        assert b_result_started.wait(1)
        fatal_result_recorded.set()
        return False

    manager.run_tasks(
        tasks,
        on_result=persist_result,
        on_site_complete=lambda *args: completions.append(args),
    )
    manager.worker.join(timeout=2)

    assert sorted(publisher.calls) == [("a", "one"), ("b", "one")]
    assert sorted(item[2] for item in completions) == ["blocked", "blocked"]


def test_completion_callback_runs_once_for_each_site(tmp_path):
    site_a = build_site(tmp_path, "a")
    site_b = build_site(tmp_path, "b")
    manager = TaskManager(FakePublisher(), max_concurrent_sites=2)
    completions = []
    tasks = [
        (site_a, ArticleRecord(site_name="a", title="one", path=tmp_path / "a.txt")),
        (site_b, ArticleRecord(site_name="b", title="one", path=tmp_path / "b.txt")),
    ]

    manager.run_tasks(tasks, on_site_complete=lambda *args: completions.append(args))
    manager.worker.join(timeout=2)

    assert sorted(item[0].name for item in completions) == ["a", "b"]
    assert all(item[2] == "completed" for item in completions)


def test_run_uses_immutable_gallery_snapshot_for_whole_site_queue(tmp_path):
    class SnapshotPublisher(FakePublisher):
        def __init__(self, image_file):
            super().__init__()
            self.image_file = image_file
            self.snapshots = []

        def publish_article(self, site, article):
            self.snapshots.append(site.image_groups_snapshot)
            if len(self.snapshots) == 1:
                self.image_file.write_text(
                    "[默认]\nhttps://img.example/changed.jpg\n",
                    encoding="utf-8",
                )
            return super().publish_article(site, article)

    site = build_site(tmp_path, "a")
    image_file = tmp_path / "gallery.txt"
    image_file.write_text(
        "[默认]\nhttps://img.example/original.jpg\n",
        encoding="utf-8",
    )
    site.image_file = image_file
    publisher = SnapshotPublisher(image_file)
    manager = TaskManager(publisher)
    articles = [
        ArticleRecord(site_name="a", title="one", path=tmp_path / "one.txt"),
        ArticleRecord(site_name="a", title="two", path=tmp_path / "two.txt"),
    ]
    tasks = manager.build_tasks([site], {site.name: articles})

    manager.run_tasks(tasks)
    manager.worker.join(timeout=2)

    expected = {"默认": ("https://img.example/original.jpg",)}
    assert publisher.snapshots == [expected, expected]


def test_build_tasks_rejects_gallery_registry_path_mismatch(tmp_path):
    source = tmp_path / "source-gallery.txt"
    source.write_text(
        "[默认]\nhttps://img.example/registered.jpg\n",
        encoding="utf-8",
    )
    gallery_service = GalleryService(tmp_path / "设置")
    gallery = gallery_service.create_gallery("默认图库", source)
    stale_file = tmp_path / "stale-gallery.txt"
    stale_file.write_text(
        "[默认]\nhttps://img.example/stale.jpg\n",
        encoding="utf-8",
    )
    site = build_site(tmp_path, "a")
    site.gallery_id = gallery.gallery_id
    site.image_file = stale_file
    article = ArticleRecord(site_name=site.name, title="one", path=tmp_path / "one.txt")
    manager = TaskManager(FakePublisher(), gallery_service=gallery_service)

    tasks = manager.build_tasks([site], {site.name: [article]})

    assert tasks == []
    assert manager.last_build_diagnostics[site.name].code == "gallery_invalid"
    assert "不一致" in manager.last_build_diagnostics[site.name].message


def test_three_identical_rejections_open_site_circuit(tmp_path):
    class RejectingPublisher(FakePublisher):
        def publish_article(self, site, article):
            self.calls.append((site.name, article.title))
            return PublishResult(
                site_name=site.name,
                title=article.title,
                success=False,
                message="rejected",
                article_path=article.path,
                response_text="HTTP 400; code=1",
                failure_kind="article_rejected",
            )

    site = build_site(tmp_path, "a")
    publisher = RejectingPublisher()
    manager = TaskManager(publisher)
    completions = []
    tasks = [
        (site, ArticleRecord(site_name="a", title=str(index), path=tmp_path / f"{index}.txt"))
        for index in range(4)
    ]

    manager.run_tasks(tasks, on_site_complete=lambda *args: completions.append(args))
    manager.worker.join(timeout=2)

    assert [title for _, title in publisher.calls] == ["0", "1", "2"]
    assert completions[0][2] == "blocked"


def test_build_tasks_exposes_diagnostics_for_state_failure(tmp_path, monkeypatch):
    site = build_site(tmp_path, "a")
    manager = TaskManager(FakePublisher())
    observed = []
    monkeypatch.setattr(
        "core.task_manager.get_published_count",
        lambda _path: (_ for _ in ()).throw(StateFileError("corrupt state")),
    )

    tasks = manager.build_tasks(
        [site],
        {"a": []},
        on_diagnostic=lambda current_site, diagnostic: observed.append(
            (current_site.name, diagnostic)
        ),
    )

    assert tasks == []
    assert manager.last_build_diagnostics["a"].code == "state_error"
    assert "发布状态不可用" in manager.last_build_diagnostics["a"].message
    assert observed == [("a", manager.last_build_diagnostics["a"])]
