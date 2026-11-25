import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import requests

from core.archive_service import ArchiveService
from core.publisher import Publisher
from core.state_store import get_published_count, update_published_count
from core.task_manager import TaskManager
from models.entities import ArticleRecord, SiteConfig


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class SuccessArchiveFailingService:
    def archive_success(self, article_path, site_name):
        raise OSError("archive locked")

    def archive_failure(self, article_path, site_name):
        raise AssertionError("success publish must not archive as failure")


def build_site(tmp_path: Path) -> SiteConfig:
    image_file = tmp_path / "gallery.txt"
    image_file.write_text(
        "[默认]\nhttps://img.example.com/default.jpg\n",
        encoding="utf-8",
    )
    return SiteConfig(
        name="demo",
        fid=9,
        url="https://example.com",
        jiekou="/api",
        publish_dir=tmp_path / "articles",
        daily_publish_filename=tmp_path / "daily.txt",
        publish_time="00:00:00",
        interval_time=0,
        daily_publish_limit=7,
        image_file=image_file,
        gallery_id="gallery-demo",
    )


def test_successful_publish_archives_article_and_updates_count(tmp_path):
    article_path = tmp_path / "article.txt"
    article_path.write_text("实验室搬家实验室搬家", encoding="utf-8")
    article = ArticleRecord(site_name="demo", title="实验室搬家", path=article_path)
    session = FakeSession([FakeResponse("success")])
    archive_service = ArchiveService(tmp_path / "归档", tmp_path / "设置" / "archive_cleanup_state.txt")
    publisher = Publisher(session=session, sleep_func=lambda _: None, archive_service=archive_service)
    site = build_site(tmp_path)
    result = publisher.publish_article(site, article)
    assert result.success is True
    assert article_path.exists() is False
    assert (tmp_path / "归档" / "发布成功" / "demo" / "article.txt").exists()
    assert get_published_count(site.daily_publish_filename) == 1


def test_successful_publish_extracts_article_url_from_response(tmp_path):
    article_path = tmp_path / "article.txt"
    article_path.write_text("实验室搬家实验室搬家", encoding="utf-8")
    result = Publisher(
        session=FakeSession(
            [
                FakeResponse(
                    '{"code":200,"msg":"发布成功","data":{"url":"https://example.com/article/demo"}}'
                )
            ]
        ),
        archive_service=ArchiveService(tmp_path / "归档"),
    ).publish_article(
        build_site(tmp_path),
        ArticleRecord(site_name="demo", title="实验室搬家", path=article_path),
    )

    assert result.success is True
    assert result.article_url == "https://example.com/article/demo"


def test_published_article_url_rejects_invalid_or_failed_response_links():
    assert Publisher.published_article_url('{"code":1,"data":{"url":"https://example.com/article"}}') == ""
    assert Publisher.published_article_url('{"code":200,"data":{"url":"file:///article"}}') == ""


def test_success_response_requires_strict_success_signal():
    assert Publisher.is_success_response('{"code":0,"msg":"ok"}') is True
    assert Publisher.is_success_response('{"code":"0","msg":"ok"}') is True
    assert Publisher.is_success_response('{"code":200,"msg":"ok"}') is True
    assert Publisher.is_success_response('{"code":"200","msg":"ok"}') is True
    assert Publisher.is_success_response('{"code":1,"msg":"success false"}') is False
    assert Publisher.is_success_response('{"success":true}') is True
    assert Publisher.is_success_response('{"success":false}') is False
    assert Publisher.is_success_response("success") is True
    assert Publisher.is_success_response("未成功") is False
    assert Publisher.is_success_response("success:false") is False


def test_successful_publish_with_archive_failure_quarantines_source(tmp_path):
    article_path = tmp_path / "article.txt"
    article_path.write_text("实验室搬家实验室搬家", encoding="utf-8")
    article = ArticleRecord(site_name="demo", title="实验室搬家", path=article_path)
    session = FakeSession([FakeResponse("success")])
    publisher = Publisher(
        session=session,
        sleep_func=lambda _: None,
        archive_service=SuccessArchiveFailingService(),
    )
    site = build_site(tmp_path)

    result = publisher.publish_article(site, article)

    assert result.success is True
    assert "成功归档失败" in result.message
    assert article_path.exists() is False
    assert (tmp_path / "article.published").exists()
    assert get_published_count(site.daily_publish_filename) == 1


def test_failed_publish_archives_article(tmp_path):
    article_path = tmp_path / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    article = ArticleRecord(site_name="demo", title="普通文章", path=article_path)
    session = FakeSession([FakeResponse("error")])
    archive_service = ArchiveService(tmp_path / "归档", tmp_path / "设置" / "archive_cleanup_state.txt")
    publisher = Publisher(session=session, retries=0, sleep_func=lambda _: None, archive_service=archive_service)
    site = build_site(tmp_path)
    result = publisher.publish_article(site, article)
    assert result.success is False
    assert article_path.exists() is False
    assert (tmp_path / "归档" / "发布失败" / "demo" / "article.txt").exists()


@pytest.mark.parametrize("source_action", ["rewrite", "delete"])
def test_definitive_rejection_does_not_archive_post_time_new_or_missing_source(
    tmp_path, source_action
):
    site = build_site(tmp_path)
    site.publish_dir.mkdir(parents=True)
    article_path = site.publish_dir / "article.txt"
    article_path.write_text("original", encoding="utf-8")

    class ChangingSession:
        def __init__(self):
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if source_action == "rewrite":
                article_path.write_text("rewritte", encoding="utf-8")
            else:
                article_path.unlink()
            return FakeResponse("error")

    archive_root = tmp_path / "归档"
    session = ChangingSession()
    result = Publisher(
        session=session,
        archive_service=ArchiveService(archive_root),
    ).publish_article(
        site,
        ArticleRecord(site_name="demo", title="文章", path=article_path),
    )

    assert result.success is False
    assert result.local_state_safe is True
    assert len(session.calls) == 1
    assert not (archive_root / "发布失败" / "demo" / "article.txt").exists()
    assert list((archive_root / "发布中" / "demo").glob(".attempt-*.marker")) == []
    if source_action == "rewrite":
        assert article_path.read_text(encoding="utf-8") == "rewritte"
    else:
        assert article_path.exists() is False


def test_definitive_rejection_marks_unsafe_when_identity_rollback_fails(
    tmp_path, monkeypatch
):
    site = build_site(tmp_path)
    site.publish_dir.mkdir(parents=True)
    article_path = site.publish_dir / "article.txt"
    article_path.write_text("original", encoding="utf-8")
    archive_root = tmp_path / "归档"

    import core.archive_service as archive_module

    real_move = archive_module.shutil.move
    move_calls = 0

    def corrupt_then_block_rollback(src, dst):
        nonlocal move_calls
        move_calls += 1
        if move_calls > 1:
            raise OSError("rollback locked")
        result = real_move(src, dst)
        Path(dst).write_text("rewritte", encoding="utf-8")
        return result

    monkeypatch.setattr(archive_module.shutil, "move", corrupt_then_block_rollback)
    result = Publisher(
        session=FakeSession([FakeResponse("error")]),
        archive_service=ArchiveService(archive_root),
    ).publish_article(
        site,
        ArticleRecord(site_name="demo", title="文章", path=article_path),
    )

    assert result.success is False
    assert result.local_state_safe is False
    assert article_path.exists() is False
    assert (archive_root / "发布失败" / "demo" / "article.txt").exists()
    assert "人工取回" in result.message


def test_timeout_posts_exactly_once_and_returns_uncertain_failure(tmp_path):
    article_path = tmp_path / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    article = ArticleRecord(site_name="demo", title="普通文章", path=article_path)
    session = FakeSession([requests.Timeout("request timed out at https://example.com/api?token=secret")])
    archive_service = ArchiveService(tmp_path / "归档", tmp_path / "设置" / "archive_cleanup_state.txt")
    publisher = Publisher(session=session, retries=2, sleep_func=lambda _: None, archive_service=archive_service)
    site = build_site(tmp_path)
    result = publisher.publish_article(site, article)
    assert result.success is False
    assert len(session.calls) == 1
    assert "secret" not in result.message
    assert article_path.exists() is False
    assert (tmp_path / "article.uncertain").exists()


def test_timeout_quarantine_consumes_today_quota_but_not_tomorrow(tmp_path):
    site = build_site(tmp_path)
    site.publish_dir.mkdir(parents=True)
    site.daily_publish_limit = 1
    first_path = site.publish_dir / "first.txt"
    first_path.write_text("普通内容", encoding="utf-8")
    archive_root = tmp_path / "归档"
    first_session = FakeSession([requests.Timeout("response timeout")])

    first_result = Publisher(
        session=first_session,
        archive_service=ArchiveService(archive_root),
    ).publish_article(
        site,
        ArticleRecord(site_name="demo", title="第一篇", path=first_path),
    )

    second_path = site.publish_dir / "second.txt"
    second_path.write_text("普通内容", encoding="utf-8")
    second_article = ArticleRecord(site_name="demo", title="第二篇", path=second_path)
    second_session = FakeSession([FakeResponse("success")])
    restarted = Publisher(
        session=second_session,
        archive_service=ArchiveService(archive_root),
    )

    assert first_result.success is False
    assert TaskManager(restarted).build_tasks([site], {"demo": [second_article]}) == []
    second_result = restarted.publish_article(site, second_article)
    assert second_result.success is False
    assert second_session.calls == []

    uncertain = site.publish_dir / "first.uncertain"
    yesterday = (datetime.now() - timedelta(days=1)).timestamp()
    os.utime(uncertain, (yesterday, yesterday))
    site.daily_publish_filename.write_text("2000-01-01\n0", encoding="utf-8")

    rebuilt_tasks = TaskManager(restarted).build_tasks(
        [site], {"demo": [second_article]}
    )
    assert [article for _site, article in rebuilt_tasks] == [second_article]
    assert rebuilt_tasks[0][0].image_groups_snapshot


def test_timeout_with_quarantine_failure_keeps_durable_attempt_and_blocks_restart(
    tmp_path, monkeypatch
):
    site = build_site(tmp_path)
    site.publish_dir.mkdir(parents=True)
    first_path = site.publish_dir / "first.txt"
    first_path.write_text("普通内容", encoding="utf-8")
    archive_root = tmp_path / "归档"
    first_session = FakeSession([requests.Timeout("response timeout")])
    monkeypatch.setattr(
        Path,
        "rename",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("file locked")),
    )

    first_result = Publisher(
        session=first_session,
        archive_service=ArchiveService(archive_root),
    ).publish_article(
        site,
        ArticleRecord(site_name="demo", title="第一篇", path=first_path),
    )

    assert first_result.success is False
    assert first_result.local_state_safe is False
    assert first_path.exists()
    assert list((archive_root / "发布中" / "demo").glob(".attempt-*.marker"))

    second_path = site.publish_dir / "second.txt"
    second_path.write_text("普通内容", encoding="utf-8")
    second_article = ArticleRecord(site_name="demo", title="第二篇", path=second_path)
    second_session = FakeSession([FakeResponse("success")])
    restarted = Publisher(
        session=second_session,
        archive_service=ArchiveService(archive_root),
    )

    assert TaskManager(restarted).build_tasks([site], {"demo": [second_article]}) == []
    with pytest.raises(OSError, match="未确认的发布请求"):
        restarted.publish_article(site, second_article)
    assert second_session.calls == []


def test_timeout_after_source_rewrite_keeps_new_draft_and_attempt(tmp_path):
    site = build_site(tmp_path)
    site.publish_dir.mkdir(parents=True)
    article_path = site.publish_dir / "article.txt"
    article_path.write_text("original", encoding="utf-8")
    archive_root = tmp_path / "归档"

    class RewritingTimeoutSession:
        def __init__(self):
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            article_path.write_text("rewritte", encoding="utf-8")
            raise requests.Timeout("response timeout")

    result = Publisher(
        session=RewritingTimeoutSession(),
        archive_service=ArchiveService(archive_root),
    ).publish_article(
        site,
        ArticleRecord(site_name="demo", title="文章", path=article_path),
    )

    assert result.success is False
    assert result.local_state_safe is False
    assert article_path.read_text(encoding="utf-8") == "rewritte"
    assert not (site.publish_dir / "article.uncertain").exists()
    assert list((archive_root / "发布中" / "demo").glob(".attempt-*.marker"))

    second_session = FakeSession([FakeResponse("success")])
    restarted = Publisher(
        session=second_session,
        archive_service=ArchiveService(archive_root),
    )
    article = ArticleRecord(site_name="demo", title="文章", path=article_path)
    assert TaskManager(restarted).build_tasks([site], {"demo": [article]}) == []
    assert second_session.calls == []


def test_timeout_after_source_deletion_keeps_attempt_and_blocks_restart(tmp_path):
    site = build_site(tmp_path)
    site.publish_dir.mkdir(parents=True)
    article_path = site.publish_dir / "article.txt"
    article_path.write_text("original", encoding="utf-8")
    archive_root = tmp_path / "归档"

    class DeletingTimeoutSession:
        def post(self, url, **kwargs):
            article_path.unlink()
            raise requests.Timeout("response timeout")

    result = Publisher(
        session=DeletingTimeoutSession(),
        archive_service=ArchiveService(archive_root),
    ).publish_article(
        site,
        ArticleRecord(site_name="demo", title="文章", path=article_path),
    )

    assert result.success is False
    assert result.local_state_safe is False
    assert article_path.exists() is False
    assert list((archive_root / "发布中" / "demo").glob(".attempt-*.marker"))

    second_path = site.publish_dir / "second.txt"
    second_path.write_text("普通内容", encoding="utf-8")
    second_article = ArticleRecord(site_name="demo", title="第二篇", path=second_path)
    second_session = FakeSession([FakeResponse("success")])
    restarted = Publisher(
        session=second_session,
        archive_service=ArchiveService(archive_root),
    )
    assert TaskManager(restarted).build_tasks([site], {"demo": [second_article]}) == []
    assert second_session.calls == []


def test_post_uses_default_tls_verification(tmp_path):
    article_path = tmp_path / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    session = FakeSession([FakeResponse("error")])
    publisher = Publisher(session=session, retries=2, sleep_func=lambda _: None)
    site = build_site(tmp_path)
    site.url = "https://example.com"

    publisher.publish_article(site, ArticleRecord(site_name="demo", title="普通文章", path=article_path))

    assert "verify" not in session.calls[0][1]
    assert session.calls[0][1]["allow_redirects"] is False


def test_server_response_body_is_not_reflected_in_publish_result(tmp_path):
    article_path = tmp_path / "article.txt"
    article_body = "private article body " * 100
    article_path.write_text(article_body, encoding="utf-8")
    response_body = '{"code":1,"message":"' + article_body + '"}'
    publisher = Publisher(session=FakeSession([FakeResponse(response_body, status_code=400)]))
    site = build_site(tmp_path)

    result = publisher.publish_article(
        site, ArticleRecord(site_name="demo", title="普通文章", path=article_path)
    )

    assert result.success is False
    assert article_body not in result.message
    assert article_body not in result.response_text
    assert len(result.message) <= 320
    assert len(result.response_text) <= 320


def test_nonlocal_http_url_is_accepted_for_publish(tmp_path):
    article_path = tmp_path / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    session = FakeSession([FakeResponse("success")])
    publisher = Publisher(session=session)
    site = build_site(tmp_path)
    site.url = "http://example.com"

    result = publisher.publish_article(site, ArticleRecord(site_name="demo", title="普通文章", path=article_path))

    assert result.success is True
    assert len(session.calls) == 1


def test_url_with_embedded_credentials_is_rejected_before_post(tmp_path):
    article_path = tmp_path / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    article = ArticleRecord(site_name="demo", title="普通文章", path=article_path)
    session = FakeSession([FakeResponse("success")])
    publisher = Publisher(session=session)
    site = build_site(tmp_path)
    site.url = "https://user:password@example.com"

    result = publisher.publish_article(site, article)

    assert result.success is False
    assert session.calls == []


def test_malformed_port_is_rejected_without_quarantining_unsent_article(tmp_path):
    article_path = tmp_path / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    session = FakeSession([FakeResponse("success")])
    publisher = Publisher(session=session)
    site = build_site(tmp_path)
    site.url = "https://example.com:abc"

    result = publisher.publish_article(
        site,
        ArticleRecord(site_name="demo", title="普通文章", path=article_path),
    )

    assert result.success is False
    assert session.calls == []
    assert article_path.exists()
    assert not (tmp_path / "article.uncertain").exists()


@pytest.mark.parametrize(
    "url",
    ["https://exa mple.com", "https://example.com\\attacker", "https://example.com\x01", "https://."],
)
def test_obviously_invalid_hostname_is_rejected_before_post(tmp_path, url):
    article_path = tmp_path / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    session = FakeSession([FakeResponse("success")])
    publisher = Publisher(session=session)
    site = build_site(tmp_path)
    site.url = url

    result = publisher.publish_article(
        site, ArticleRecord(site_name="demo", title="普通文章", path=article_path)
    )

    assert result.success is False
    assert session.calls == []
    assert article_path.exists()


@pytest.mark.parametrize("endpoint", ["/api\\attacker", "/api\x00attacker"])
def test_unsafe_endpoint_is_rejected_without_network_or_article_move(tmp_path, endpoint):
    article_path = tmp_path / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    session = FakeSession([FakeResponse("success")])
    archive_service = ArchiveService(tmp_path / "归档", tmp_path / "设置" / "archive_cleanup_state.txt")
    publisher = Publisher(session=session, archive_service=archive_service)
    site = build_site(tmp_path)
    site.jiekou = endpoint

    result = publisher.publish_article(
        site, ArticleRecord(site_name="demo", title="普通文章", path=article_path)
    )

    assert result.success is False
    assert session.calls == []
    assert article_path.exists()
    assert not (tmp_path / "article.uncertain").exists()
    assert not (tmp_path / "归档" / "发布失败" / "demo" / "article.txt").exists()


def test_fragment_in_base_url_is_rejected_before_post(tmp_path):
    article_path = tmp_path / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    session = FakeSession([FakeResponse("success")])
    publisher = Publisher(session=session)
    site = build_site(tmp_path)
    site.url = "https://example.com#fragment"

    result = publisher.publish_article(
        site,
        ArticleRecord(site_name="demo", title="普通文章", path=article_path),
    )

    assert result.success is False
    assert session.calls == []
    assert article_path.exists()


def test_fragment_in_endpoint_is_rejected_before_post(tmp_path):
    article_path = tmp_path / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    session = FakeSession([FakeResponse("success")])
    publisher = Publisher(session=session)
    site = build_site(tmp_path)
    site.jiekou = "#fragment"

    result = publisher.publish_article(
        site,
        ArticleRecord(site_name="demo", title="普通文章", path=article_path),
    )

    assert result.success is False
    assert session.calls == []
    assert article_path.exists()


def test_endpoint_cannot_change_destination_origin(tmp_path):
    article_path = tmp_path / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    session = FakeSession([FakeResponse("success")])
    publisher = Publisher(session=session)
    site = build_site(tmp_path)
    site.jiekou = ".attacker.test/api?token=synthetic"

    result = publisher.publish_article(
        site,
        ArticleRecord(site_name="demo", title="普通文章", path=article_path),
    )

    assert result.success is False
    assert session.calls == []
    assert article_path.exists()


def test_http_error_status_cannot_be_classified_as_success(tmp_path):
    article_path = tmp_path / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    session = FakeSession([FakeResponse("success", status_code=500)])
    publisher = Publisher(session=session, archive_service=ArchiveService(tmp_path / "归档"))
    site = build_site(tmp_path)
    site.url = "https://example.com"

    result = publisher.publish_article(site, ArticleRecord(site_name="demo", title="普通文章", path=article_path))

    assert result.success is False


def test_remote_success_stays_success_when_count_write_fails(tmp_path, monkeypatch):
    article_path = tmp_path / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    archive_service = ArchiveService(tmp_path / "归档")
    publisher = Publisher(session=FakeSession([FakeResponse("success")]), archive_service=archive_service)
    site = build_site(tmp_path)
    site.url = "https://example.com"
    monkeypatch.setattr("core.publisher.update_published_count", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))

    result = publisher.publish_article(site, ArticleRecord(site_name="demo", title="普通文章", path=article_path))

    assert result.success is True
    assert result.local_state_safe is False
    assert (tmp_path / "归档" / "发布成功" / "demo" / "article.txt").exists()


def test_new_publisher_recovers_today_count_from_success_archive_after_count_write_failure(
    tmp_path, monkeypatch
):
    first_article_path = tmp_path / "first.txt"
    first_article_path.write_text("普通内容", encoding="utf-8")
    archive_service = ArchiveService(tmp_path / "归档")
    first_session = FakeSession([FakeResponse("success")])
    first_publisher = Publisher(session=first_session, archive_service=archive_service)
    site = build_site(tmp_path)
    site.daily_publish_limit = 1

    import core.publisher as publisher_module

    real_update = publisher_module.update_published_count
    monkeypatch.setattr(
        publisher_module,
        "update_published_count",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    first_result = first_publisher.publish_article(
        site,
        ArticleRecord(site_name="demo", title="第一篇", path=first_article_path),
    )
    monkeypatch.setattr(publisher_module, "update_published_count", real_update)

    second_article_path = tmp_path / "second.txt"
    second_article_path.write_text("普通内容", encoding="utf-8")
    second_session = FakeSession([FakeResponse("success")])
    second_publisher = Publisher(
        session=second_session,
        archive_service=ArchiveService(tmp_path / "归档"),
    )
    second_result = second_publisher.publish_article(
        site,
        ArticleRecord(site_name="demo", title="第二篇", path=second_article_path),
    )

    assert first_result.success is True
    assert first_result.local_state_safe is False
    assert second_result.success is False
    assert second_session.calls == []
    assert second_article_path.exists()


def test_quarantine_refreshes_old_article_mtime_so_restart_blocks_second_post(
    tmp_path, monkeypatch
):
    site = build_site(tmp_path)
    site.publish_dir.mkdir(parents=True)
    site.daily_publish_limit = 1
    first_article_path = site.publish_dir / "first.txt"
    first_article_path.write_text("普通内容", encoding="utf-8")
    yesterday = (datetime.now() - timedelta(days=1)).timestamp()
    os.utime(first_article_path, (yesterday, yesterday))

    import core.publisher as publisher_module

    real_update = publisher_module.update_published_count
    monkeypatch.setattr(
        publisher_module,
        "update_published_count",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    first_session = FakeSession([FakeResponse("success")])
    first_result = Publisher(
        session=first_session,
        archive_service=SuccessArchiveFailingService(),
    ).publish_article(
        site,
        ArticleRecord(site_name="demo", title="第一篇", path=first_article_path),
    )
    monkeypatch.setattr(publisher_module, "update_published_count", real_update)

    quarantined = site.publish_dir / "first.published"
    assert first_result.success is True
    assert first_result.local_state_safe is False
    assert quarantined.exists()
    assert datetime.fromtimestamp(quarantined.stat().st_mtime).date() == datetime.now().date()

    second_article_path = site.publish_dir / "second.txt"
    second_article_path.write_text("普通内容", encoding="utf-8")
    second_session = FakeSession([FakeResponse("success")])
    second_result = Publisher(
        session=second_session,
        archive_service=ArchiveService(tmp_path / "归档"),
    ).publish_article(
        site,
        ArticleRecord(site_name="demo", title="第二篇", path=second_article_path),
    )

    assert second_result.success is False
    assert second_session.calls == []
    assert second_article_path.exists()


def test_restart_recovers_remote_success_when_archive_mtime_and_count_writes_fail(
    tmp_path, monkeypatch
):
    site = build_site(tmp_path)
    site.publish_dir.mkdir(parents=True)
    site.daily_publish_limit = 1
    first_article_path = site.publish_dir / "first.txt"
    first_article_path.write_text("普通内容", encoding="utf-8")
    yesterday = (datetime.now() - timedelta(days=1)).timestamp()
    os.utime(first_article_path, (yesterday, yesterday))

    import core.archive_service as archive_module
    import core.publisher as publisher_module

    real_utime = archive_module.os.utime
    real_update = publisher_module.update_published_count
    monkeypatch.setattr(
        archive_module.os,
        "utime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("mtime denied")),
    )
    monkeypatch.setattr(
        publisher_module,
        "update_published_count",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    archive_root = tmp_path / "归档"
    first_session = FakeSession([FakeResponse("success")])
    first_result = Publisher(
        session=first_session,
        archive_service=ArchiveService(archive_root),
    ).publish_article(
        site,
        ArticleRecord(site_name="demo", title="第一篇", path=first_article_path),
    )

    monkeypatch.setattr(archive_module.os, "utime", real_utime)
    monkeypatch.setattr(publisher_module, "update_published_count", real_update)

    assert first_result.success is True
    assert first_result.local_state_safe is False
    assert first_session.calls
    assert first_article_path.exists() is False

    second_article_path = site.publish_dir / "second.txt"
    second_article_path.write_text("普通内容", encoding="utf-8")
    second_article = ArticleRecord(
        site_name="demo", title="第二篇", path=second_article_path
    )
    second_session = FakeSession([FakeResponse("success")])
    restarted_publisher = Publisher(
        session=second_session,
        archive_service=ArchiveService(archive_root),
    )

    assert TaskManager(restarted_publisher).build_tasks(
        [site], {"demo": [second_article]}
    ) == []

    second_result = restarted_publisher.publish_article(site, second_article)

    assert second_result.success is False
    assert second_session.calls == []
    assert second_article_path.exists()


def test_restart_recovers_marker_written_before_archive_move_crash(
    tmp_path, monkeypatch
):
    site = build_site(tmp_path)
    site.publish_dir.mkdir(parents=True)
    site.daily_publish_limit = 2
    first_article_path = site.publish_dir / "first.txt"
    first_article_path.write_text("普通内容", encoding="utf-8")
    first_article = ArticleRecord(
        site_name="demo", title="第一篇", path=first_article_path
    )
    archive_root = tmp_path / "归档"

    import core.archive_service as archive_module

    real_move = archive_module.shutil.move
    monkeypatch.setattr(
        archive_module.shutil,
        "move",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit("crash")),
    )
    first_session = FakeSession([FakeResponse("success")])

    with pytest.raises(SystemExit, match="crash"):
        Publisher(
            session=first_session,
            archive_service=ArchiveService(archive_root),
        ).publish_article(site, first_article)

    monkeypatch.setattr(archive_module.shutil, "move", real_move)

    assert first_session.calls
    assert first_article_path.exists()
    assert list((archive_root / "发布成功" / "demo").glob(".success-*.marker"))

    restarted_archive = ArchiveService(archive_root)
    assert restarted_archive.success_count_today(
        "demo", publish_dir=site.publish_dir
    ) == 1
    assert first_article_path.exists() is False
    assert any(path.suffix == ".published" for path in site.publish_dir.iterdir())

    second_session = FakeSession([FakeResponse("success")])
    restarted_publisher = Publisher(
        session=second_session,
        archive_service=restarted_archive,
    )
    assert TaskManager(restarted_publisher).build_tasks(
        [site], {"demo": [first_article]}
    ) == []

    first_article_path.write_text("新的同名文章", encoding="utf-8")
    rebuilt_tasks = TaskManager(restarted_publisher).build_tasks(
        [site], {"demo": [first_article]}
    )
    assert [article for _site, article in rebuilt_tasks] == [first_article]
    assert rebuilt_tasks[0][0].image_groups_snapshot
    first_article_path.unlink()

    try:
        restarted_publisher.publish_article(site, first_article)
    except OSError:
        pass

    assert second_session.calls == []


def test_corrupt_success_marker_fails_closed_before_any_network_call(tmp_path):
    site = build_site(tmp_path)
    site.publish_dir.mkdir(parents=True)
    article_path = site.publish_dir / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    article = ArticleRecord(site_name="demo", title="文章", path=article_path)
    archive_service = ArchiveService(tmp_path / "归档")
    success_dir = archive_service.success_dir("demo")
    success_dir.mkdir(parents=True)
    (success_dir / "old.txt").write_text("archived", encoding="utf-8")
    (success_dir / ".success-old.txt.marker").write_text(
        "not-a-date\nold.txt", encoding="utf-8"
    )
    session = FakeSession([FakeResponse("success")])
    publisher = Publisher(session=session, archive_service=archive_service)

    assert TaskManager(publisher).build_tasks([site], {"demo": [article]}) == []
    with pytest.raises(OSError, match="标记|损坏|日期"):
        publisher.publish_article(site, article)

    assert session.calls == []
    assert article_path.exists()


def test_unfinished_success_marker_fails_closed_before_any_network_call(tmp_path):
    site = build_site(tmp_path)
    site.publish_dir.mkdir(parents=True)
    article_path = site.publish_dir / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    article = ArticleRecord(site_name="demo", title="文章", path=article_path)
    archive_service = ArchiveService(tmp_path / "归档")
    success_dir = archive_service.success_dir("demo")
    success_dir.mkdir(parents=True)
    (success_dir / ".success-old.txt.marker.123.456.tmp").write_text(
        "2026-07-13\nold.txt\nold.txt", encoding="utf-8"
    )
    session = FakeSession([FakeResponse("success")])
    publisher = Publisher(session=session, archive_service=archive_service)

    assert TaskManager(publisher).build_tasks([site], {"demo": [article]}) == []
    with pytest.raises(OSError, match="未完成|恢复标记"):
        publisher.publish_article(site, article)

    assert session.calls == []
    assert article_path.exists()


def test_post_time_source_rewrite_is_not_archived_or_quarantined(tmp_path):
    site = build_site(tmp_path)
    site.publish_dir.mkdir(parents=True)
    article_path = site.publish_dir / "article.txt"
    article_path.write_text("original", encoding="utf-8")

    class RewritingSession:
        def __init__(self):
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            article_path.write_text("rewritte", encoding="utf-8")
            return FakeResponse("success")

    session = RewritingSession()
    archive_service = ArchiveService(tmp_path / "归档")
    publisher = Publisher(session=session, archive_service=archive_service)
    article = ArticleRecord(site_name="demo", title="文章", path=article_path)

    result = publisher.publish_article(site, article)

    assert result.success is True
    assert result.local_state_safe is False
    assert len(session.calls) == 1
    assert article_path.read_text(encoding="utf-8") == "rewritte"
    assert list(archive_service.success_dir("demo").glob(".success-*.marker"))
    assert not (site.publish_dir / "article.published").exists()
    with pytest.raises(OSError, match="无法确认源文件身份"):
        archive_service.success_count_today("demo", site.publish_dir)


def test_post_time_source_deletion_keeps_recoverable_success_marker(
    tmp_path, monkeypatch
):
    site = build_site(tmp_path)
    site.publish_dir.mkdir(parents=True)
    article_path = site.publish_dir / "article.txt"
    article_path.write_text("original", encoding="utf-8")

    class DeletingSession:
        def __init__(self):
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            article_path.unlink()
            return FakeResponse("success")

    session = DeletingSession()
    archive_service = ArchiveService(tmp_path / "归档")
    publisher = Publisher(session=session, archive_service=archive_service)
    article = ArticleRecord(site_name="demo", title="文章", path=article_path)
    monkeypatch.setattr(
        "core.publisher.update_published_count",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("count locked")),
    )

    result = publisher.publish_article(site, article)

    assert result.success is True
    assert result.local_state_safe is False
    assert len(session.calls) == 1
    assert article_path.exists() is False
    assert archive_service.success_count_today("demo", site.publish_dir) == 1


def test_old_success_marker_cannot_clear_new_attempt_when_new_evidence_fails(
    tmp_path, monkeypatch
):
    site = build_site(tmp_path)
    site.publish_dir.mkdir(parents=True)
    article_path = site.publish_dir / "article.txt"
    article_path.write_text("original", encoding="utf-8")
    archive_root = tmp_path / "归档"
    archive_service = ArchiveService(archive_root)
    source_hash = archive_service._file_sha256(article_path)
    success_dir = archive_service.success_dir("demo")
    success_dir.mkdir(parents=True)
    today = datetime.now().strftime("%Y-%m-%d")
    (success_dir / ".success-old.txt.marker").write_text(
        f"{today}\nold.txt\narticle.txt\nsha256:{source_hash}\nattempt:{'0' * 32}"
        "\narticle.published\ncomplete",
        encoding="utf-8",
    )

    import core.archive_service as archive_module

    real_atomic_write = archive_module.atomic_write_text

    def fail_new_success_marker(path, content):
        if "发布成功" in Path(path).parts:
            raise OSError("success marker locked")
        return real_atomic_write(path, content)

    monkeypatch.setattr(archive_module, "atomic_write_text", fail_new_success_marker)
    publisher = Publisher(
        session=FakeSession([FakeResponse("success")]),
        archive_service=archive_service,
    )
    monkeypatch.setattr(
        publisher,
        "_quarantine_success_without_archive",
        lambda *_args: (False, "; quarantine locked"),
    )

    result = publisher.publish_article(
        site,
        ArticleRecord(site_name="demo", title="文章", path=article_path),
    )

    assert result.success is True
    assert result.local_state_safe is False
    assert article_path.exists()
    assert list((archive_root / "发布中" / "demo").glob(".attempt-*.marker"))

    second_session = FakeSession([FakeResponse("success")])
    restarted = Publisher(
        session=second_session,
        archive_service=ArchiveService(archive_root),
    )
    second_article = ArticleRecord(site_name="demo", title="文章", path=article_path)
    assert TaskManager(restarted).build_tasks([site], {"demo": [second_article]}) == []
    assert second_session.calls == []


def test_publish_crossing_midnight_starts_new_day_count_at_one(tmp_path):
    class Clock:
        def __init__(self):
            self.value = datetime(2026, 7, 13, 23, 59, 59)

        def now(self):
            return self.value

    clock = Clock()
    site = build_site(tmp_path)
    site.publish_dir.mkdir(parents=True)
    site.daily_publish_limit = 10
    update_published_count(site.daily_publish_filename, 9, today="2026-07-13")
    article_path = site.publish_dir / "article.txt"
    article_path.write_text("original", encoding="utf-8")

    class MidnightSession:
        def __init__(self):
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            clock.value = datetime(2026, 7, 14, 0, 0, 1)
            return FakeResponse("success")

    session = MidnightSession()
    archive_service = ArchiveService(tmp_path / "归档", now_func=clock.now)
    publisher = Publisher(
        session=session,
        archive_service=archive_service,
        now_func=clock.now,
    )

    result = publisher.publish_article(
        site,
        ArticleRecord(site_name="demo", title="文章", path=article_path),
    )

    assert result.success is True
    assert len(session.calls) == 1
    assert get_published_count(site.daily_publish_filename, today="2026-07-14") == 1
    assert site.daily_publish_filename.read_text(encoding="utf-8").splitlines() == [
        "2026-07-14",
        "1",
    ]


def test_archive_and_quarantine_failure_marks_local_state_unsafe(tmp_path, monkeypatch):
    article_path = tmp_path / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    publisher = Publisher(session=FakeSession([FakeResponse("success")]), archive_service=SuccessArchiveFailingService())
    site = build_site(tmp_path)
    site.url = "https://example.com"
    monkeypatch.setattr(
        publisher,
        "_quarantine_success_without_archive",
        lambda *_args: (False, "; quarantine failed"),
    )

    result = publisher.publish_article(site, ArticleRecord(site_name="demo", title="普通文章", path=article_path))

    assert result.success is True
    assert result.local_state_safe is False


def test_archive_conflict_appends_suffix(tmp_path):
    archive_service = ArchiveService(tmp_path / "归档", tmp_path / "设置" / "archive_cleanup_state.txt")
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")
    target = tmp_path / "归档" / "发布失败" / "demo"
    target.mkdir(parents=True)
    existing = target / "article.txt"
    existing.write_text("old", encoding="utf-8")
    first.rename(tmp_path / "article.txt")
    archived_first = archive_service.archive_failure(tmp_path / "article.txt", "demo")
    second.rename(tmp_path / "article.txt")
    archived_second = archive_service.archive_failure(tmp_path / "article.txt", "demo")
    assert archived_first.name == "article-1.txt"
    assert archived_second.name == "article-2.txt"


def test_publish_uses_scanned_article_category_id(tmp_path):
    article_path = tmp_path / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    session = FakeSession([FakeResponse("success")])
    publisher = Publisher(
        session=session,
        archive_service=ArchiveService(tmp_path / "归档"),
    )

    result = publisher.publish_article(
        build_site(tmp_path),
        ArticleRecord(
            site_name="demo", title="普通文章", path=article_path, category_id=42
        ),
    )

    assert result.success is True
    assert session.calls[0][1]["data"]["category_id"] == 42


def test_publish_sends_root_relative_image_in_content_and_images_field(tmp_path):
    article_path = tmp_path / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    session = FakeSession([FakeResponse("success")])
    publisher = Publisher(
        session=session,
        archive_service=ArchiveService(tmp_path / "归档"),
    )
    site = build_site(tmp_path)
    site.image_groups_snapshot = {"默认": ("/uploads/a.webp?v=1&size=large",)}

    result = publisher.publish_article(
        site,
        ArticleRecord(site_name="demo", title="普通文章", path=article_path),
    )

    assert result.success is True
    data = session.calls[0][1]["data"]
    assert data["images[]"] == "/uploads/a.webp?v=1&size=large"
    assert 'src="/uploads/a.webp?v=1&amp;size=large"' in data["content"]


def test_server_503_is_retryable_and_preserves_source(tmp_path):
    article_path = tmp_path / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    session = FakeSession([FakeResponse("maintenance", status_code=503)])
    publisher = Publisher(
        session=session,
        archive_service=ArchiveService(tmp_path / "归档"),
    )

    result = publisher.publish_article(
        build_site(tmp_path),
        ArticleRecord(site_name="demo", title="普通文章", path=article_path),
    )

    assert result.failure_kind == "transient"
    assert result.retryable is True
    assert result.stop_site is True
    assert article_path.exists()
    assert not (tmp_path / "归档" / "发布失败").exists()


@pytest.mark.parametrize("status_code", [425, 429, 502, 503, 504])
def test_only_explicit_backoff_statuses_are_retryable(tmp_path, status_code):
    article_path = tmp_path / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    publisher = Publisher(
        session=FakeSession([FakeResponse("temporary failure", status_code=status_code)]),
        archive_service=ArchiveService(tmp_path / "归档"),
    )

    result = publisher.publish_article(
        build_site(tmp_path),
        ArticleRecord(site_name="demo", title="普通文章", path=article_path),
    )

    assert result.failure_kind == "transient"
    assert result.retryable is True
    assert result.stop_site is True
    assert article_path.exists()
    assert not (tmp_path / "article.uncertain").exists()


@pytest.mark.parametrize("status_code", [408, 500])
def test_ambiguous_http_status_is_quarantined_as_uncertain(tmp_path, status_code):
    article_path = tmp_path / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    publisher = Publisher(
        session=FakeSession([FakeResponse("server failure", status_code=status_code)]),
        archive_service=ArchiveService(tmp_path / "归档"),
    )

    result = publisher.publish_article(
        build_site(tmp_path),
        ArticleRecord(site_name="demo", title="普通文章", path=article_path),
    )

    assert result.failure_kind == "uncertain"
    assert result.retryable is False
    assert result.stop_site is True
    assert not article_path.exists()
    assert (tmp_path / "article.uncertain").exists()


def test_token_rejection_blocks_site_and_preserves_source(tmp_path):
    article_path = tmp_path / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    session = FakeSession(
        [FakeResponse('{"code":-1,"msg":"Token错误"}', status_code=200)]
    )
    publisher = Publisher(
        session=session,
        archive_service=ArchiveService(tmp_path / "归档"),
    )

    result = publisher.publish_article(
        build_site(tmp_path),
        ArticleRecord(site_name="demo", title="普通文章", path=article_path),
    )

    assert result.failure_kind == "configuration"
    assert result.retryable is False
    assert result.stop_site is True
    assert article_path.exists()


@pytest.mark.parametrize(
    "response_body",
    [
        "Token 无效",
        "栏目不存在",
        "分类参数错误",
        "category invalid",
        "fid missing",
        '{"code":-1,"error":{"detail":"Token 无效"}}',
        '{"code":-1,"errors":["栏目不存在"]}',
    ],
)
def test_configuration_rejection_in_text_or_json_blocks_on_first_failure(
    tmp_path, response_body
):
    article_path = tmp_path / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    session = FakeSession([FakeResponse(response_body, status_code=200)])
    publisher = Publisher(
        session=session,
        archive_service=ArchiveService(tmp_path / "归档"),
    )

    result = publisher.publish_article(
        build_site(tmp_path),
        ArticleRecord(site_name="demo", title="普通文章", path=article_path),
    )

    assert len(session.calls) == 1
    assert result.failure_kind == "configuration"
    assert result.retryable is False
    assert result.stop_site is True
    assert article_path.exists()
    assert not (tmp_path / "归档" / "发布失败").exists()


def test_network_timeout_is_uncertain_and_stops_site(tmp_path):
    article_path = tmp_path / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    publisher = Publisher(
        session=FakeSession([requests.Timeout("timeout")]),
        archive_service=ArchiveService(tmp_path / "归档"),
    )

    result = publisher.publish_article(
        build_site(tmp_path),
        ArticleRecord(site_name="demo", title="普通文章", path=article_path),
    )

    assert result.failure_kind == "uncertain"
    assert result.retryable is False
    assert result.stop_site is True
    assert not article_path.exists()
    assert (tmp_path / "article.uncertain").exists()


def test_connect_timeout_is_retryable_and_preserves_source(tmp_path):
    article_path = tmp_path / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    publisher = Publisher(
        session=FakeSession([requests.ConnectTimeout("connect timeout")]),
        archive_service=ArchiveService(tmp_path / "归档"),
    )

    result = publisher.publish_article(
        build_site(tmp_path),
        ArticleRecord(site_name="demo", title="普通文章", path=article_path),
    )

    assert result.failure_kind == "transient"
    assert result.retryable is True
    assert result.stop_site is True
    assert article_path.exists()
    assert not (tmp_path / "article.uncertain").exists()


def test_connection_error_is_retryable_and_preserves_source(tmp_path):
    article_path = tmp_path / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    publisher = Publisher(
        session=FakeSession([requests.ConnectionError("dns lookup failed")]),
        archive_service=ArchiveService(tmp_path / "归档"),
    )

    result = publisher.publish_article(
        build_site(tmp_path),
        ArticleRecord(site_name="demo", title="普通文章", path=article_path),
    )

    assert result.failure_kind == "transient"
    assert result.retryable is True
    assert result.stop_site is True
    assert article_path.exists()
    assert not (tmp_path / "article.uncertain").exists()


def test_post_submit_connection_drop_is_uncertain_and_not_retried(tmp_path):
    article_path = tmp_path / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    publisher = Publisher(
        session=FakeSession(
            [requests.ConnectionError("remote end closed connection without response")]
        ),
        archive_service=ArchiveService(tmp_path / "归档"),
    )

    result = publisher.publish_article(
        build_site(tmp_path),
        ArticleRecord(site_name="demo", title="普通文章", path=article_path),
    )

    assert result.failure_kind == "uncertain"
    assert result.retryable is False
    assert result.stop_site is True
    assert not article_path.exists()
    assert (tmp_path / "article.uncertain").exists()


@pytest.mark.parametrize("status_code", [302, 404])
def test_redirect_and_missing_publish_endpoint_are_configuration_errors(
    tmp_path, status_code
):
    article_path = tmp_path / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    publisher = Publisher(
        session=FakeSession([FakeResponse("", status_code=status_code)]),
        archive_service=ArchiveService(tmp_path / "归档"),
    )

    result = publisher.publish_article(
        build_site(tmp_path),
        ArticleRecord(site_name="demo", title="普通文章", path=article_path),
    )

    assert result.failure_kind == "configuration"
    assert result.stop_site is True
    assert result.retryable is False
    assert article_path.exists()


def test_archive_identity_uses_site_id_instead_of_display_name(tmp_path):
    article_path = tmp_path / "article.txt"
    article_path.write_text("普通内容", encoding="utf-8")
    site = build_site(tmp_path)
    site.site_id = "stable-site-id"
    publisher = Publisher(
        session=FakeSession([FakeResponse("success")]),
        archive_service=ArchiveService(tmp_path / "归档"),
    )

    result = publisher.publish_article(
        site,
        ArticleRecord(site_name="demo", title="普通文章", path=article_path),
    )

    assert result.success is True
    assert result.site_id == "stable-site-id"
    assert (tmp_path / "归档" / "发布成功" / "stable-site-id" / "article.txt").exists()
    assert not (tmp_path / "归档" / "发布成功" / "demo").exists()
