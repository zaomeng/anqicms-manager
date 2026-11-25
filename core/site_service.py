from __future__ import annotations

from pathlib import Path

from core.archive_service import ArchiveService
from core.category_keywords import resolve_category_id
from core.content_service import (
    extract_high_freq_words,
    load_word_set,
)
from core.runtime_paths import resolve_external_or_bundled
from core.state_store import StateFileError, ensure_daily_file, get_published_count
from models.entities import ArticleRecord, SiteConfig, SiteStatusSummary


class SiteService:
    def __init__(
        self,
        word_file: str | Path = "jieba_data/fenci.txt",
        archive_service: ArchiveService | None = None,
    ) -> None:
        self.word_set, self.max_word_len = load_word_set(resolve_external_or_bundled(str(word_file)))
        self.archive_service = archive_service

    @staticmethod
    def _pending_count(articles: list[ArticleRecord]) -> int:
        return sum(1 for article in articles if article.status == "pending")

    def _published_count(self, site: SiteConfig) -> int:
        ensure_daily_file(site.daily_publish_filename)
        published_count = get_published_count(site.daily_publish_filename)
        if self.archive_service is None:
            return published_count
        return max(
            published_count,
            self.archive_service.success_count_today(
                site.site_id or site.name,
                publish_dir=site.publish_dir,
            ),
        )

    def list_articles(self, site: SiteConfig) -> list[ArticleRecord]:
        ensure_daily_file(site.daily_publish_filename)
        site.publish_dir.mkdir(parents=True, exist_ok=True)
        articles: list[ArticleRecord] = []
        for path in sorted(site.publish_dir.iterdir()):
            if not path.is_file():
                continue
            if path.suffix.lower() != ".txt":
                continue
            title = path.stem
            try:
                content = path.read_text(encoding="utf-8")
                keywords = extract_high_freq_words(
                    content,
                    self.word_set,
                    self.max_word_len,
                )
                status = "pending"
                last_error = ""
            except Exception as exc:
                keywords = ""
                status = "error"
                last_error = str(exc)
            articles.append(
                ArticleRecord(
                    site_name=site.name,
                    title=title,
                    path=path,
                    category_id=resolve_category_id(
                        title, site.category_keywords, site.fid
                    ),
                    keywords=keywords,
                    status=status,
                    last_error=last_error,
                )
            )
        return articles

    def summarize_site(self, site: SiteConfig) -> SiteStatusSummary:
        try:
            published_today = self._published_count(site)
            articles = self.list_articles(site)
        except (StateFileError, OSError) as exc:
            return SiteStatusSummary(
                site_name=site.name,
                daily_limit=site.daily_publish_limit,
                remaining_quota=0,
                status="error",
                last_error=str(exc),
            )
        remaining = max(site.daily_publish_limit - published_today, 0)
        return SiteStatusSummary(
            site_name=site.name,
            pending_count=self._pending_count(articles),
            article_count=len(articles),
            published_today=published_today,
            daily_limit=site.daily_publish_limit,
            remaining_quota=remaining,
        )

    def scan_all_sites(
        self, sites: list[SiteConfig]
    ) -> tuple[dict[str, SiteStatusSummary], dict[str, list[ArticleRecord]]]:
        summaries: dict[str, SiteStatusSummary] = {}
        articles_by_site: dict[str, list[ArticleRecord]] = {}
        for site in sites:
            try:
                published_today = self._published_count(site)
                articles = self.list_articles(site)
            except (StateFileError, OSError) as exc:
                articles_by_site[site.name] = []
                summaries[site.name] = SiteStatusSummary(
                    site_name=site.name,
                    daily_limit=site.daily_publish_limit,
                    remaining_quota=0,
                    status="error",
                    last_error=str(exc),
                )
                continue
            articles_by_site[site.name] = articles
            summaries[site.name] = SiteStatusSummary(
                site_name=site.name,
                pending_count=self._pending_count(articles),
                article_count=len(articles),
                published_today=published_today,
                daily_limit=site.daily_publish_limit,
                remaining_quota=max(site.daily_publish_limit - published_today, 0),
            )
        return summaries, articles_by_site
