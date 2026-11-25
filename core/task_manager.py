from __future__ import annotations

import random
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from core.content_service import parse_image_groups
from core.gallery_service import GalleryError
from core.site_factory import is_valid_publish_endpoint, is_valid_site_url
from core.state_store import StateFileError, ensure_daily_file, get_published_count
from models.entities import ArticleRecord, PublishResult, SiteConfig, TaskBuildDiagnostic


class TaskManager:
    def __init__(self, publisher, max_concurrent_sites: int = 3, gallery_service=None) -> None:
        self.publisher = publisher
        self.gallery_service = gallery_service
        self.max_concurrent_sites = max(1, int(max_concurrent_sites))
        self.pause_event = threading.Event()
        self.stop_event = threading.Event()
        self.fatal_event = threading.Event()
        self.pause_event.set()
        self.worker: threading.Thread | None = None
        self.last_build_diagnostics: dict[str, TaskBuildDiagnostic] = {}

    def pause(self) -> None:
        self.pause_event.clear()

    def resume(self) -> None:
        self.pause_event.set()

    def stop(self) -> None:
        self.stop_event.set()
        self.pause_event.set()

    def reset(self) -> None:
        self.stop_event.clear()
        self.fatal_event.clear()
        self.pause_event.set()

    def _wait_until_runnable(self) -> bool:
        self.pause_event.wait()
        return not self.stop_event.is_set()

    def _wait_interval(self, seconds: float) -> bool:
        if seconds <= 0:
            return not self.stop_event.is_set()
        return not self.stop_event.wait(seconds)

    @staticmethod
    def _group_tasks_by_site(tasks: list[tuple[SiteConfig, ArticleRecord]]) -> list[tuple[SiteConfig, list[ArticleRecord]]]:
        grouped: dict[str, tuple[SiteConfig, list[ArticleRecord]]] = {}
        for site, article in tasks:
            site_key = site.site_id or site.name
            if site_key not in grouped:
                grouped[site_key] = (site, [])
            grouped[site_key][1].append(article)
        return list(grouped.values())

    def _snapshot_site(self, site: SiteConfig) -> SiteConfig:
        if site.fid <= 0:
            raise StateFileError("发布栏目 ID 必须为正整数")
        if not site.gallery_id or site.image_file is None:
            raise StateFileError("站点未配置命名图库")
        image_file = site.image_file
        if self.gallery_service is not None:
            try:
                gallery = self.gallery_service.require_gallery(site.gallery_id)
                validation = self.gallery_service.validate_gallery(site.gallery_id)
            except GalleryError as exc:
                raise StateFileError(f"图库注册记录不可用: {exc}") from exc
            if not validation.valid:
                raise StateFileError(f"图库内容无效: {validation.message}")
            if Path(image_file).resolve() != gallery.file_path.resolve():
                raise StateFileError("图库 ID 与配置文件路径不一致")
            image_file = gallery.file_path
        try:
            groups = parse_image_groups(image_file)
        except (OSError, UnicodeError) as exc:
            raise StateFileError(f"图库文件无法读取: {image_file.name}") from exc
        if not groups:
            raise StateFileError(
                "未读取到图片链接：请每行填写一个 http(s) 或 /uploads/... 链接；[分组名] 可选"
            )
        image_groups = {name: tuple(urls) for name, urls in groups.items()}
        return replace(
            site,
            publish_times=list(site.publish_times),
            image_file=image_file,
            image_groups_snapshot=image_groups,
        )

    def build_tasks(
        self,
        sites: list[SiteConfig],
        articles_by_site: dict[str, list[ArticleRecord]],
        selected_sites: set[str] | None = None,
        selected_articles: set[str] | None = None,
        on_diagnostic: Callable[[SiteConfig, TaskBuildDiagnostic], None] | None = None,
        allow_disabled: bool = False,
    ) -> list[tuple[SiteConfig, ArticleRecord]]:
        tasks: list[tuple[SiteConfig, ArticleRecord]] = []
        diagnostics: dict[str, TaskBuildDiagnostic] = {}

        def diagnose(
            site: SiteConfig,
            code: str,
            message: str,
            *,
            severity: str = "info",
            recoverable: bool = False,
        ) -> None:
            diagnostic = TaskBuildDiagnostic(code, message, severity, recoverable)
            diagnostics[site.name] = diagnostic
            if on_diagnostic:
                on_diagnostic(site, diagnostic)

        for site in sites:
            if not site.enabled and not allow_disabled:
                diagnose(site, "configuration_blocked", "站点未启用", severity="error", recoverable=True)
                continue
            if selected_sites is not None and site.name not in selected_sites:
                continue
            if not is_valid_site_url(site.url) or not is_valid_publish_endpoint(site.jiekou):
                diagnose(
                    site,
                    "configuration_blocked",
                    "站点网址或发布接口无效",
                    severity="error",
                    recoverable=True,
                )
                continue
            try:
                ensure_daily_file(site.daily_publish_filename)
                published_count = get_published_count(site.daily_publish_filename)
                archive_service = getattr(self.publisher, "archive_service", None)
                archive_counter = getattr(archive_service, "success_count_today", None)
                if callable(archive_counter):
                    published_count = max(
                        published_count,
                        archive_counter(site.site_id or site.name, publish_dir=site.publish_dir),
                    )
            except (StateFileError, OSError) as exc:
                code = "pending_attempt" if "未确认的发布请求" in str(exc) else "state_error"
                diagnose(site, code, f"发布状态不可用: {exc}", severity="error")
                continue
            remaining_quota = max(site.daily_publish_limit - published_count, 0)
            if remaining_quota <= 0:
                diagnose(site, "quota_exhausted", "已达到每日发布上限")
                continue
            try:
                site_snapshot = self._snapshot_site(site)
            except (StateFileError, OSError) as exc:
                diagnose(
                    site,
                    "gallery_invalid" if "图库" in str(exc) else "configuration_blocked",
                    str(exc),
                    severity="error",
                    recoverable=True,
                )
                continue
            candidates: list[ArticleRecord] = []
            for article in articles_by_site.get(site.name, []):
                recovered_source = getattr(archive_service, "was_recovered_source", None)
                if article.status != "pending" or (
                    callable(recovered_source) and recovered_source(article.path)
                ):
                    continue
                key = str(article.path)
                if selected_articles is not None and key not in selected_articles:
                    continue
                candidates.append(article)

            random.shuffle(candidates)
            selected_candidates = candidates[:remaining_quota]
            for article in selected_candidates:
                tasks.append((site_snapshot, article))
            added_for_site = len(selected_candidates)
            if added_for_site:
                diagnose(site, "ready", f"已生成 {added_for_site} 个任务")
            else:
                diagnose(site, "empty", "没有可发布文章")
        self.last_build_diagnostics = diagnostics
        return tasks

    def run_tasks(
        self,
        tasks: list[tuple[SiteConfig, ArticleRecord]],
        on_log: Callable[[str], None] | None = None,
        on_result: Callable[[PublishResult], bool | None] | None = None,
        on_site_complete: Callable[[SiteConfig, list[PublishResult], str], None] | None = None,
    ) -> None:
        self.reset()

        def worker() -> None:
            archive_service = getattr(self.publisher, "archive_service", None)
            if archive_service is not None:
                try:
                    cleaned = archive_service.cleanup_once_per_day()
                    if cleaned and on_log:
                        on_log("已执行每日归档清理")
                except Exception as exc:
                    if on_log:
                        on_log(f"每日归档清理失败，继续发布: {exc}")

            def run_site_queue(site: SiteConfig, articles: list[ArticleRecord]) -> None:
                site_results: list[PublishResult] = []
                completion_reason = "completed"
                consecutive_rejection = ""
                consecutive_rejection_count = 0
                for index, article in enumerate(articles):
                    if self.fatal_event.is_set():
                        completion_reason = "blocked"
                        break
                    if self.stop_event.is_set():
                        completion_reason = "stopped"
                        break
                    if not self._wait_until_runnable():
                        completion_reason = "blocked" if self.fatal_event.is_set() else "stopped"
                        break
                    if self.fatal_event.is_set():
                        completion_reason = "blocked"
                        break
                    if on_log:
                        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        on_log(f"{site.name} -> {article.title} [{started_at}]")
                    try:
                        result = self.publisher.publish_article(site, article)
                    except (StateFileError, OSError) as exc:
                        result = PublishResult(
                            site_name=site.name,
                            title=article.title,
                            success=False,
                            message=str(exc),
                            article_path=article.path,
                            response_text=str(exc),
                            local_state_safe=False,
                            failure_kind="unsafe_local_state",
                            stop_site=True,
                        )
                    except Exception as exc:
                        result = PublishResult(
                            site_name=site.name,
                            title=article.title,
                            success=False,
                            message=f"未预期的发布异常，已保留原文并停止站点: {exc}",
                            article_path=article.path,
                            response_text=str(exc),
                            local_state_safe=False,
                            failure_kind="unsafe_local_state",
                            stop_site=True,
                            site_id=site.site_id,
                        )
                        if on_log:
                            on_log(f"发布异常: {site.name} -> {article.title}: {exc}; 已保留原文")
                    if not result.site_id:
                        result.site_id = site.site_id
                    if result.failure_kind == "article_rejected":
                        rejection_key = result.response_text or result.message
                        if rejection_key == consecutive_rejection:
                            consecutive_rejection_count += 1
                        else:
                            consecutive_rejection = rejection_key
                            consecutive_rejection_count = 1
                        if consecutive_rejection_count >= 3:
                            result.stop_site = True
                            if on_log:
                                on_log(f"{site.name} 连续 3 次相同拒绝，已停止该站点后续任务")
                    else:
                        consecutive_rejection = ""
                        consecutive_rejection_count = 0
                    site_results.append(result)
                    result_persisted = True
                    if on_result:
                        result_persisted = on_result(result) is not False
                    if not result_persisted:
                        self.fatal_event.set()
                        self.stop_event.set()
                        self.pause_event.set()
                        completion_reason = "blocked"
                        if on_log:
                            on_log(
                                f"{site.name} 的批次结果无法持久化，已停止后续任务"
                            )
                        break
                    if result.stop_site or not result.local_state_safe:
                        if result.retryable:
                            completion_reason = "retry_wait"
                        else:
                            completion_reason = "blocked"
                        if on_log:
                            on_log(f"{site.name} 已停止该站点后续任务: {result.message}")
                        break
                    if index < len(articles) - 1 and not self._wait_interval(site.interval_time):
                        completion_reason = "blocked" if self.fatal_event.is_set() else "stopped"
                        break
                if on_site_complete:
                    try:
                        on_site_complete(site, site_results, completion_reason)
                    except Exception as exc:
                        if on_log:
                            on_log(f"站点完成回调失败: {site.name}: {exc}")

            grouped_tasks = self._group_tasks_by_site(tasks)
            max_workers = min(self.max_concurrent_sites, len(grouped_tasks)) or 1
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="publish-site") as executor:
                futures = [executor.submit(run_site_queue, site, articles) for site, articles in grouped_tasks]
                for future in futures:
                    future.result()
            if on_log:
                on_log("任务执行完成")

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()
