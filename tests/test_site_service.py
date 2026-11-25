import hashlib
import os
from datetime import datetime, timedelta

from core.archive_service import ArchiveService
from core.site_service import SiteService
from core.state_store import update_published_count
from models.entities import SiteConfig


def build_site(tmp_path):
    publish_dir = tmp_path / "articles"
    publish_dir.mkdir()
    (publish_dir / "实验室搬家.txt").write_text("实验室搬家实验室搬家", encoding="utf-8")
    return SiteConfig(
        name="demo",
        fid=9,
        url="http://example.com",
        jiekou="/api",
        publish_dir=publish_dir,
        daily_publish_filename=tmp_path / "daily.txt",
        publish_time="00:00:00",
        interval_time=0,
        daily_publish_limit=7,
        category_keywords={3: ("实验室",)},
    )


def test_list_articles_and_summary(tmp_path):
    site = build_site(tmp_path)
    update_published_count(site.daily_publish_filename, 2)
    service = SiteService()
    articles = service.list_articles(site)
    assert len(articles) == 1
    summary = service.summarize_site(site)
    assert summary.pending_count == 1
    assert summary.remaining_quota == 5


def test_summary_initializes_empty_legacy_daily_count_file(tmp_path):
    site = build_site(tmp_path)
    site.daily_publish_filename.write_text("", encoding="utf-8")

    summary = SiteService().summarize_site(site)

    assert summary.status == "idle"
    assert summary.pending_count == 1
    assert summary.published_today == 0
    assert site.daily_publish_filename.read_text(encoding="utf-8").splitlines() == [
        datetime.now().strftime("%Y-%m-%d"),
        "0",
    ]


def test_list_articles_marks_bad_encoding_without_crashing(tmp_path):
    site = build_site(tmp_path)
    bad_file = site.publish_dir / "坏编码.txt"
    bad_file.write_bytes(b"\xff")
    service = SiteService()

    articles = service.list_articles(site)

    bad_article = next(item for item in articles if item.title == "坏编码")
    assert bad_article.status == "error"
    assert bad_article.last_error


def test_summary_excludes_error_articles_from_pending_count(tmp_path):
    site = build_site(tmp_path)
    bad_file = site.publish_dir / "坏编码.txt"
    bad_file.write_bytes(b"\xff")
    service = SiteService()

    summary = service.summarize_site(site)

    assert summary.pending_count == 1
    assert summary.article_count == 2


def test_summary_recovers_today_count_from_success_archive(tmp_path):
    site = build_site(tmp_path)
    site.daily_publish_limit = 1
    archive_service = ArchiveService(tmp_path / "归档")
    success_dir = archive_service.success_dir(site.name)
    success_dir.mkdir(parents=True)
    (success_dir / "published.txt").write_text("done", encoding="utf-8")

    summary = SiteService(
        word_file=tmp_path / "missing-words.txt",
        archive_service=archive_service,
    ).summarize_site(site)

    assert summary.published_today == 1
    assert summary.remaining_quota == 0


def test_summary_does_not_count_success_archive_from_another_day(tmp_path):
    site = build_site(tmp_path)
    site.daily_publish_limit = 1
    archive_service = ArchiveService(tmp_path / "归档")
    success_dir = archive_service.success_dir(site.name)
    success_dir.mkdir(parents=True)
    archived = success_dir / "published-yesterday.txt"
    archived.write_text("done", encoding="utf-8")
    yesterday = (datetime.now() - timedelta(days=1)).timestamp()
    os.utime(archived, (yesterday, yesterday))

    summary = SiteService(
        word_file=tmp_path / "missing-words.txt",
        archive_service=archive_service,
    ).summarize_site(site)

    assert summary.published_today == 0
    assert summary.remaining_quota == 1


def test_list_articles_skips_non_txt_quarantine_files(tmp_path):
    site = build_site(tmp_path)
    (site.publish_dir / "已发布.published").write_text("already published", encoding="utf-8")
    service = SiteService()

    articles = service.list_articles(site)

    assert [item.title for item in articles] == ["实验室搬家"]


def test_unreadable_article_remains_error_after_rescan(tmp_path):
    site = build_site(tmp_path)
    bad_file = site.publish_dir / "坏编码.txt"
    bad_file.write_bytes(b"\xff")
    service = SiteService()

    first = service.list_articles(site)
    second = SiteService().list_articles(site)

    assert next(item for item in first if item.title == "坏编码").status == "error"
    assert next(item for item in second if item.title == "坏编码").status == "error"


def test_scan_all_sites_fails_closed_when_count_state_is_corrupt(tmp_path):
    site = build_site(tmp_path)
    today = datetime.now().strftime("%Y-%m-%d")
    site.daily_publish_filename.write_text(f"{today}\ninvalid", encoding="utf-8")

    summaries, articles = SiteService().scan_all_sites([site])

    assert articles["demo"] == []
    assert summaries["demo"].status == "error"
    assert summaries["demo"].remaining_quota == 0


def test_scan_recovers_success_marker_before_building_article_snapshot(tmp_path):
    site = build_site(tmp_path)
    article = site.publish_dir / "实验室搬家.txt"
    source_hash = hashlib.sha256(article.read_bytes()).hexdigest()
    archive_service = ArchiveService(tmp_path / "归档")
    success_dir = archive_service.success_dir(site.name)
    success_dir.mkdir(parents=True)
    today = datetime.now().strftime("%Y-%m-%d")
    (success_dir / ".success-实验室搬家.txt.marker").write_text(
        f"{today}\n实验室搬家.txt\n实验室搬家.txt\nsha256:{source_hash}",
        encoding="utf-8",
    )

    summaries, articles = SiteService(archive_service=archive_service).scan_all_sites(
        [site]
    )

    assert articles[site.name] == []
    assert summaries[site.name].pending_count == 0
    assert summaries[site.name].published_today == 1
    assert article.exists() is False
    assert (site.publish_dir / "实验室搬家.published").exists()


def test_scan_all_sites_isolates_publish_directory_that_is_a_file(tmp_path):
    bad_root = tmp_path / "bad"
    good_root = tmp_path / "good"
    bad_root.mkdir()
    good_root.mkdir()
    bad = build_site(bad_root)
    good = build_site(good_root)
    bad.name = "bad"
    good.name = "good"
    for article in bad.publish_dir.iterdir():
        article.unlink()
    bad.publish_dir.rmdir()
    bad.publish_dir.write_text("not a directory", encoding="utf-8")

    summaries, articles = SiteService().scan_all_sites([bad, good])

    assert summaries["bad"].status == "error"
    assert summaries["bad"].last_error
    assert articles["bad"] == []
    assert summaries["good"].status != "error"
    assert [article.title for article in articles["good"]] == ["实验室搬家"]


def test_list_articles_uses_longest_category_keyword_then_configuration_order(tmp_path):
    site = build_site(tmp_path)
    site.category_keywords = {
        3: ("实验室", "实验室搬家"),
        4: ("搬家",),
    }

    articles = SiteService().list_articles(site)

    assert articles[0].category_id == 3


def test_list_articles_uses_first_configured_category_for_equal_length_match(tmp_path):
    site = build_site(tmp_path)
    site.category_keywords = {4: ("室搬",), 3: ("实验",)}

    articles = SiteService().list_articles(site)

    assert articles[0].category_id == 4


def test_list_articles_falls_back_to_default_category(tmp_path):
    site = build_site(tmp_path)
    site.category_keywords = {3: ("新闻",)}

    articles = SiteService().list_articles(site)

    assert articles[0].category_id == 9


def test_legacy_category_file_no_longer_affects_scan(tmp_path):
    site = build_site(tmp_path)
    legacy_file = tmp_path / "fenlei.txt"
    legacy_file.write_bytes(b"\xff\xfe")

    summaries, articles = SiteService().scan_all_sites([site])

    assert summaries[site.name].status != "error"
    assert articles[site.name][0].category_id == 3
