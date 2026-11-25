from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.archive_service import ArchiveService


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


def test_cleanup_once_per_day_runs_only_once(tmp_path):
    clock = Clock(datetime(2026, 6, 30, 9, 0, 0))
    service = ArchiveService(
        archive_root=tmp_path / "归档",
        cleanup_state_file=tmp_path / "设置" / "archive_cleanup_state.txt",
        now_func=clock.now,
    )
    assert service.cleanup_once_per_day() is True
    assert service.cleanup_once_per_day() is False
    clock.value = datetime(2026, 7, 1, 9, 0, 0)
    assert service.cleanup_once_per_day() is True


def test_cleanup_removes_files_older_than_retention(tmp_path):
    now = datetime(2026, 6, 30, 9, 0, 0)
    service = ArchiveService(
        archive_root=tmp_path / "归档",
        cleanup_state_file=tmp_path / "设置" / "archive_cleanup_state.txt",
        now_func=lambda: now,
    )
    old_file = tmp_path / "归档" / "发布成功" / "demo" / "old.txt"
    fresh_file = tmp_path / "归档" / "发布成功" / "demo" / "fresh.txt"
    old_file.parent.mkdir(parents=True)
    old_file.write_text("old", encoding="utf-8")
    fresh_file.write_text("fresh", encoding="utf-8")
    old_timestamp = (now - timedelta(days=8)).timestamp()
    fresh_timestamp = (now - timedelta(days=2)).timestamp()
    os.utime(old_file, (old_timestamp, old_timestamp))
    os.utime(fresh_file, (fresh_timestamp, fresh_timestamp))

    service.cleanup_old_archives()

    assert old_file.exists() is False
    assert fresh_file.exists() is True


def test_archive_move_refreshes_mtime_to_archive_time(tmp_path):
    now = datetime(2026, 6, 30, 9, 0, 0)
    service = ArchiveService(
        archive_root=tmp_path / "归档",
        cleanup_state_file=tmp_path / "设置" / "archive_cleanup_state.txt",
        now_func=lambda: now,
    )
    article = tmp_path / "article.txt"
    article.write_text("old content", encoding="utf-8")
    old_timestamp = (now - timedelta(days=30)).timestamp()
    os.utime(article, (old_timestamp, old_timestamp))

    archived = service.archive_success(article, "demo")

    assert archived.exists()
    assert datetime.fromtimestamp(archived.stat().st_mtime) == now


@pytest.mark.parametrize(
    "marker_payload",
    [
        "not-a-date\narticle.txt",
        "2026-07-13\n../outside.txt",
        "2026-07-13\narticle.txt\n../source.txt",
        "2026-07-13\n\nsource.txt",
        "2026-07-13\narticle.txt\n\n",
    ],
)
def test_success_count_rejects_corrupt_or_escaping_recovery_marker(
    tmp_path, marker_payload
):
    service = ArchiveService(tmp_path / "归档")
    success_dir = service.success_dir("demo")
    success_dir.mkdir(parents=True)
    (success_dir / "article.txt").write_text("archived", encoding="utf-8")
    (success_dir / ".success-article.txt.marker").write_text(
        marker_payload, encoding="utf-8"
    )

    with pytest.raises(OSError, match="标记|损坏|路径|日期"):
        service.success_count_today("demo", today="2026-07-13")


def test_success_count_rejects_unfinished_atomic_marker(tmp_path):
    service = ArchiveService(tmp_path / "归档")
    success_dir = service.success_dir("demo")
    success_dir.mkdir(parents=True)
    (success_dir / ".success-article.txt.marker.123.456.tmp").write_text(
        "2026-07-13\narticle.txt\narticle.txt", encoding="utf-8"
    )

    with pytest.raises(OSError, match="未完成|恢复标记"):
        service.success_count_today("demo", today="2026-07-13")


def test_success_count_links_existing_quarantine_to_old_marker_once(tmp_path):
    service = ArchiveService(tmp_path / "归档")
    success_dir = service.success_dir("demo")
    publish_dir = tmp_path / "待发布"
    success_dir.mkdir(parents=True)
    publish_dir.mkdir()
    marker = success_dir / ".success-article.txt.marker"
    marker.write_text("2026-07-13\narticle.txt\narticle.txt", encoding="utf-8")
    (publish_dir / "article.published").write_text("done", encoding="utf-8")

    assert service.success_count_today("demo", publish_dir, "2026-07-13") == 1
    assert service.success_count_today("demo", publish_dir, "2026-07-13") == 1
    assert marker.read_text(encoding="utf-8").splitlines()[-2:] == [
        "article.published",
        "complete",
    ]


def test_completed_recovery_does_not_move_new_same_name_source(tmp_path):
    service = ArchiveService(tmp_path / "归档")
    success_dir = service.success_dir("demo")
    publish_dir = tmp_path / "待发布"
    success_dir.mkdir(parents=True)
    publish_dir.mkdir()
    marker = success_dir / ".success-article.txt.marker"
    marker.write_text(
        "2026-07-13\narticle.txt\narticle.txt\narticle.published\ncomplete",
        encoding="utf-8",
    )
    source = publish_dir / "article.txt"
    source.write_text("new article", encoding="utf-8")

    assert service.success_count_today("demo", publish_dir, "2026-07-13") == 1
    assert source.read_text(encoding="utf-8") == "new article"
    assert not (publish_dir / "article.published").exists()


def test_completed_recovery_rejects_directory_at_quarantine_path(tmp_path):
    service = ArchiveService(tmp_path / "归档")
    success_dir = service.success_dir("demo")
    publish_dir = tmp_path / "待发布"
    success_dir.mkdir(parents=True)
    publish_dir.mkdir()
    (success_dir / ".success-article.txt.marker").write_text(
        "2026-07-13\narticle.txt\narticle.txt\narticle.published\ncomplete",
        encoding="utf-8",
    )
    (publish_dir / "article.published").mkdir()

    with pytest.raises(OSError, match="目标异常"):
        service.success_count_today("demo", publish_dir, "2026-07-13")


def test_incomplete_recovery_rejects_same_size_rewritten_source(tmp_path, monkeypatch):
    service = ArchiveService(tmp_path / "归档")
    publish_dir = tmp_path / "待发布"
    publish_dir.mkdir()
    source = publish_dir / "article.txt"
    source.write_text("original", encoding="utf-8")

    import core.archive_service as archive_module

    monkeypatch.setattr(
        archive_module.shutil,
        "move",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit("crash")),
    )
    with pytest.raises(SystemExit, match="crash"):
        service.archive_success(source, "demo")

    source.write_text("rewritte", encoding="utf-8")
    with pytest.raises(OSError, match="无法确认源文件身份"):
        service.success_count_today("demo", publish_dir=publish_dir)

    assert source.read_text(encoding="utf-8") == "rewritte"
    assert not (publish_dir / "article.published").exists()


def test_archive_rechecks_identity_after_move_and_restores_changed_source(
    tmp_path, monkeypatch
):
    service = ArchiveService(tmp_path / "归档")
    publish_dir = tmp_path / "待发布"
    publish_dir.mkdir()
    source = publish_dir / "article.txt"
    source.write_text("original", encoding="utf-8")
    expected_hash = service._file_sha256(source)

    import core.archive_service as archive_module

    real_move = archive_module.shutil.move

    def move_then_rewrite(src, dst):
        result = real_move(src, dst)
        if Path(dst).suffix == ".txt":
            Path(dst).write_text("rewritte", encoding="utf-8")
        return result

    monkeypatch.setattr(archive_module.shutil, "move", move_then_rewrite)

    with pytest.raises(OSError, match="目标文件身份不一致"):
        service.archive_success(source, "demo", source_hash=expected_hash)

    assert source.read_text(encoding="utf-8") == "rewritte"
    assert list(service.success_dir("demo").glob(".success-*.marker"))
    with pytest.raises(OSError, match="无法确认源文件身份"):
        service.success_count_today("demo", publish_dir=publish_dir)


def test_attempt_is_only_superseded_by_success_marker_with_same_nonce(tmp_path):
    service = ArchiveService(tmp_path / "归档")
    publish_dir = tmp_path / "待发布"
    publish_dir.mkdir()
    source = publish_dir / "article.txt"
    source.write_text("original", encoding="utf-8")
    source_hash = service._file_sha256(source)
    attempt = service.begin_publish_attempt(source, "demo", source_hash)
    attempt_id = attempt.name.removeprefix(".attempt-").removesuffix(".marker")
    success_dir = service.success_dir("demo")
    success_dir.mkdir(parents=True)
    success_marker = success_dir / ".success-article.txt.marker"
    today = datetime.now().strftime("%Y-%m-%d")
    success_marker.write_text(
        f"{today}\narticle.txt\narticle.txt\nsha256:{source_hash}\nattempt:{'0' * 32}",
        encoding="utf-8",
    )

    with pytest.raises(OSError, match="未确认的发布请求"):
        service.success_count_today("demo", publish_dir=publish_dir)
    assert attempt.exists()

    success_marker.write_text(
        f"{today}\narticle.txt\narticle.txt\nsha256:{source_hash}\nattempt:{attempt_id}",
        encoding="utf-8",
    )

    assert service.success_count_today("demo", publish_dir=publish_dir) == 1
    assert attempt.exists() is False
    assert source.exists() is False


def test_success_evidence_checker_requires_current_attempt_nonce(tmp_path):
    service = ArchiveService(tmp_path / "归档")
    publish_dir = tmp_path / "待发布"
    publish_dir.mkdir()
    source = publish_dir / "article.txt"
    source.write_text("original", encoding="utf-8")
    source_hash = service._file_sha256(source)
    attempt = service.begin_publish_attempt(source, "demo", source_hash)
    attempt_id = attempt.name.removeprefix(".attempt-").removesuffix(".marker")
    success_dir = service.success_dir("demo")
    success_dir.mkdir(parents=True)
    success_marker = success_dir / ".success-article.txt.marker"
    today = datetime.now().strftime("%Y-%m-%d")
    success_marker.write_text(
        f"{today}\narticle.txt\narticle.txt\nsha256:{source_hash}\nattempt:{'0' * 32}",
        encoding="utf-8",
    )

    assert service.has_pending_success_marker(
        source, "demo", source_hash, attempt
    ) is False

    success_marker.write_text(
        f"{today}\narticle.txt\narticle.txt\nsha256:{source_hash}\nattempt:{attempt_id}",
        encoding="utf-8",
    )
    assert service.has_pending_success_marker(
        source, "demo", source_hash, attempt
    ) is True


def test_legacy_archive_identity_migrates_once_and_blocks_same_name_reuse(tmp_path):
    service = ArchiveService(
        tmp_path / "归档",
        identity_map_file=tmp_path / "设置" / "archive_site_ids.json",
    )
    legacy_success = service.success_dir("demo")
    legacy_success.mkdir(parents=True)
    (legacy_success / "old.txt").write_text("old", encoding="utf-8")

    assert service.migrate_site_identity("demo", "site-old") is True
    assert (service.success_dir("site-old") / "old.txt").exists()
    assert not legacy_success.exists()

    reused_name_dir = service.success_dir("demo")
    reused_name_dir.mkdir(parents=True)
    (reused_name_dir / "new.txt").write_text("new", encoding="utf-8")

    assert service.migrate_site_identity("demo", "site-new") is False
    assert (reused_name_dir / "new.txt").exists()
    assert not service.success_dir("site-new").exists()


def test_archive_identity_migration_fails_closed_on_conflict(tmp_path):
    service = ArchiveService(
        tmp_path / "归档",
        identity_map_file=tmp_path / "设置" / "archive_site_ids.json",
    )
    service.success_dir("demo").mkdir(parents=True)
    (service.success_dir("demo") / "legacy.txt").write_text("legacy", encoding="utf-8")
    service.success_dir("site-id").mkdir(parents=True)
    (service.success_dir("site-id") / "current.txt").write_text("current", encoding="utf-8")

    with pytest.raises(OSError, match="迁移冲突"):
        service.migrate_site_identity("demo", "site-id")

    assert (service.success_dir("demo") / "legacy.txt").exists()
    assert (service.success_dir("site-id") / "current.txt").exists()
