from models.entities import ArticleRecord, SiteConfig, SiteStatusSummary


def test_site_config_defaults(tmp_path):
    site = SiteConfig(
        name="demo",
        fid=9,
        url="http://example.com",
        jiekou="/api",
        publish_dir=tmp_path / "articles",
        daily_publish_filename=tmp_path / "daily.txt",
        publish_time="00:00:00",
        interval_time=10,
        daily_publish_limit=7,
    )
    assert site.name == "demo"
    assert site.image_file is None
    assert site.category_keywords == {}


def test_article_record_defaults(tmp_path):
    article = ArticleRecord(site_name="demo", title="hello", path=tmp_path / "a.txt")
    assert article.status == "pending"
    assert article.keywords == ""


def test_site_status_summary_defaults():
    summary = SiteStatusSummary(site_name="demo")
    assert summary.pending_count == 0
    assert summary.status == "idle"
