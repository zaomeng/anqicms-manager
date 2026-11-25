from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import requests

from core.archive_service import ArchiveService, SourceIdentityChangedError
from core.content_service import (
    add_empty_line_between_paragraphs,
    auto_insert_image,
    auto_insert_image_from_groups,
    extract_high_freq_words,
    load_word_set,
)
from core.runtime_paths import resolve_external_or_bundled
from core.site_factory import is_valid_publish_endpoint, is_valid_site_url
from core.state_store import ensure_daily_file, get_published_count, update_published_count
from models.entities import ArticleRecord, PublishResult, SiteConfig


class Publisher:
    def __init__(
        self,
        session: requests.Session | None = None,
        timeout: int = 15,
        retries: int = 2,
        sleep_func: Callable[[float], None] = time.sleep,
        word_file: str | Path = "jieba_data/fenci.txt",
        archive_service: ArchiveService | None = None,
        now_func: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self.retries = retries
        self.sleep_func = sleep_func
        self.archive_service = archive_service or ArchiveService()
        self.now_func = now_func
        resolved_word_file = (
            resolve_external_or_bundled(str(word_file)) if isinstance(word_file, (str, Path)) else word_file
        )
        self.word_set, self.max_word_len = load_word_set(resolved_word_file)

    @staticmethod
    def is_success_response(text: str) -> bool:
        stripped = text.strip()
        try:
            payload = json.loads(stripped)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            if "code" in payload:
                return str(payload.get("code")).strip() in {"0", "200"}
            if isinstance(payload.get("success"), bool):
                return payload["success"] is True
        return stripped.lower() in {"success", "ok", "true"} or stripped in {"成功", "发布成功"}

    @staticmethod
    def redact_sensitive(value: str) -> str:
        text = re.sub(r"(?i)((?:token|api[_-]?key)=)[^&\s]+", r"\1***", str(value))
        return re.sub(r"(https?://)[^/@\s]+@", r"\1***@", text)

    @staticmethod
    def response_summary(text: str, status_code: int) -> str:
        details: list[str] = [f"HTTP {status_code}"]
        try:
            payload = json.loads(text.strip())
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            for key in ("code", "success"):
                value = payload.get(key)
                if isinstance(value, (str, int, float, bool)):
                    normalized = str(value)
                    if len(normalized) <= 32 and re.fullmatch(r"[\w.:-]+", normalized):
                        details.append(f"{key}={normalized}")
        return "; ".join(details)

    @classmethod
    def published_article_url(cls, text: str) -> str:
        """Return the safe article link supplied by a successful publish response."""
        try:
            payload = json.loads(text.strip())
        except ValueError:
            return ""
        if not isinstance(payload, dict) or not cls.is_success_response(text):
            return ""
        data = payload.get("data")
        if not isinstance(data, dict):
            return ""
        url = data.get("url")
        if not isinstance(url, str):
            return ""
        normalized = url.strip()
        return normalized if cls._is_allowed_publish_url(normalized) else ""

    @staticmethod
    def _is_allowed_publish_url(url: str) -> bool:
        parsed = urlsplit(url)
        try:
            parsed.port
        except ValueError:
            return False
        if parsed.fragment:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        return parsed.scheme in {"https", "http"} and bool(parsed.hostname)

    @staticmethod
    def _origin(url: str) -> tuple[str, str, int | None]:
        parsed = urlsplit(url)
        port = parsed.port or ({"https": 443, "http": 80}.get(parsed.scheme))
        return parsed.scheme, (parsed.hostname or "").casefold(), port

    @staticmethod
    def _response_failure_kind(text: str, status_code: int) -> str:
        try:
            payload = json.loads(text.strip())
        except ValueError:
            payload = None

        fragments: list[str] = []

        def collect_strings(value: object) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    fragments.append(str(key))
                    collect_strings(nested)
            elif isinstance(value, (list, tuple)):
                for nested in value:
                    collect_strings(nested)
            elif isinstance(value, str):
                fragments.append(value)

        if payload is None:
            fragments.append(text.strip())
        else:
            collect_strings(payload)
        normalized = " ".join(fragments).casefold()
        has_ascii_config_token = re.search(
            r"(?<![a-z0-9_])(?:token|category|fid)(?![a-z0-9_])",
            normalized,
        )
        if "栏目" in normalized or "分类" in normalized or has_ascii_config_token:
            return "configuration"
        if status_code in {408, 500}:
            return "uncertain"
        if status_code in {425, 429, 502, 503, 504}:
            return "transient"
        if 300 <= status_code < 400 or status_code in {401, 403, 404}:
            return "configuration"
        return "article_rejected"

    @staticmethod
    def _is_preconnect_connection_error(exc: requests.ConnectionError) -> bool:
        safe_type_names = {
            "ConnectionRefusedError",
            "ConnectTimeoutError",
            "NameResolutionError",
            "NewConnectionError",
            "gaierror",
        }
        current: BaseException | None = exc
        visited: set[int] = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            if type(current).__name__ in safe_type_names:
                return True
            nested = next(
                (item for item in getattr(current, "args", ()) if isinstance(item, BaseException)),
                None,
            )
            current = current.__cause__ or current.__context__ or nested
        message = str(exc).casefold()
        return any(
            token in message
            for token in (
                "connection refused",
                "dns",
                "failed to establish a new connection",
                "name resolution",
                "network is unreachable",
                "no route to host",
            )
        )

    def publish_article(self, site: SiteConfig, article: ArticleRecord) -> PublishResult:
        site_key = site.site_id or site.name
        if not is_valid_site_url(site.url) or not is_valid_publish_endpoint(site.jiekou):
            return PublishResult(
                site_name=site.name,
                title=article.title,
                success=False,
                message="站点网址必须使用有效 HTTP 或 HTTPS",
                article_path=article.path,
                failure_kind="configuration",
                stop_site=True,
            )
        publish_date = self.now_func().strftime("%Y-%m-%d")
        ensure_daily_file(site.daily_publish_filename, today=publish_date)
        published_count = get_published_count(site.daily_publish_filename, today=publish_date)
        archive_counter = getattr(self.archive_service, "success_count_today", None)
        if callable(archive_counter):
            published_count = max(
                published_count,
                archive_counter(site_key, publish_dir=site.publish_dir, today=publish_date),
            )
        if published_count >= site.daily_publish_limit:
            return PublishResult(
                site_name=site.name,
                title=article.title,
                success=False,
                message="已达到每日发布上限",
                article_path=article.path,
                failure_kind="configuration",
                stop_site=True,
            )
        source_bytes = article.path.read_bytes()
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        content = source_bytes.decode("utf-8")
        content = add_empty_line_between_paragraphs(content)
        selected_image_url = ""
        if site.image_groups_snapshot is not None:
            content, selected_image_url = auto_insert_image_from_groups(
                content,
                article.title,
                site.image_groups_snapshot,
            )
        elif site.image_file:
            content, selected_image_url = auto_insert_image(content, article.title, site.image_file)
        keywords = extract_high_freq_words(content, self.word_set, self.max_word_len)
        category_id = article.category_id if article.category_id is not None else site.fid
        publish_data = {
            "title": article.title,
            "content": content,
            "category_id": category_id,
            "keywords": keywords,
            "description": "",
            "images[]": selected_image_url,
            "publish_time": "",
            "tag": keywords,
            "draft": "false",
            "cover": "0",
        }
        publish_url = f"{site.url}{site.jiekou}"
        if not self._is_allowed_publish_url(publish_url) or self._origin(publish_url) != self._origin(site.url):
            return PublishResult(
                site_name=site.name,
                title=article.title,
                success=False,
                message="发布地址必须使用有效 HTTP 或 HTTPS",
                article_path=article.path,
                local_state_safe=False,
                failure_kind="configuration",
                stop_site=True,
            )
        attempt_marker: Path | None = None
        attempt_starter = getattr(self.archive_service, "begin_publish_attempt", None)
        if callable(attempt_starter):
            try:
                attempt_marker = attempt_starter(
                    article.path,
                    site_key,
                    source_hash,
                    today=publish_date,
                )
            except Exception as exc:
                message = self.redact_sensitive(f"无法持久化发布请求，已取消网络提交: {exc}")
                return PublishResult(
                    site_name=site.name,
                    title=article.title,
                    success=False,
                    message=message,
                    article_path=article.path,
                    response_text=message,
                    local_state_safe=False,
                    failure_kind="unsafe_local_state",
                    stop_site=True,
                )

        def uncertain_result(reason: object, *, response_text: str | None = None) -> PublishResult:
            quarantined, quarantine_message = self._quarantine_path(
                article.path,
                ".uncertain",
                expected_hash=source_hash,
            )
            attempt_cleared = True
            attempt_message = ""
            if quarantined:
                attempt_cleared, attempt_message = self._finish_attempt(attempt_marker)
            message = self.redact_sensitive(
                f"网络结果不确定，已停止自动重试: {reason}{quarantine_message}{attempt_message}"
            )
            return PublishResult(
                site_name=site.name,
                title=article.title,
                success=False,
                message=message,
                article_path=article.path,
                response_text=response_text or message,
                local_state_safe=quarantined and attempt_cleared,
                failure_kind="uncertain" if quarantined and attempt_cleared else "unsafe_local_state",
                stop_site=True,
                site_id=site.site_id,
            )

        try:
            response = self.session.post(
                publish_url,
                data=publish_data,
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.ConnectTimeout as exc:
            attempt_cleared, attempt_message = self._finish_attempt(attempt_marker)
            message = self.redact_sensitive(
                f"连接站点超时，已保留原文等待重试: {exc}{attempt_message}"
            )
            return PublishResult(
                site_name=site.name,
                title=article.title,
                success=False,
                message=message,
                article_path=article.path,
                response_text=message,
                local_state_safe=attempt_cleared,
                failure_kind="transient" if attempt_cleared else "unsafe_local_state",
                stop_site=True,
                retryable=attempt_cleared,
            )
        except requests.ConnectionError as exc:
            if not self._is_preconnect_connection_error(exc):
                return uncertain_result(exc)
            attempt_cleared, attempt_message = self._finish_attempt(attempt_marker)
            message = self.redact_sensitive(
                f"连接站点失败，已保留原文等待重试: {exc}{attempt_message}"
            )
            return PublishResult(
                site_name=site.name,
                title=article.title,
                success=False,
                message=message,
                article_path=article.path,
                response_text=message,
                local_state_safe=attempt_cleared,
                failure_kind="transient" if attempt_cleared else "unsafe_local_state",
                stop_site=True,
                retryable=attempt_cleared,
                site_id=site.site_id,
            )
        except requests.RequestException as exc:
            return uncertain_result(exc)

        status_code = int(getattr(response, "status_code", 200))
        response_text = self.response_summary(response.text, status_code)
        if not 200 <= status_code < 300 or not self.is_success_response(response.text):
            failure_kind = self._response_failure_kind(response.text, status_code)
            if failure_kind == "uncertain":
                return uncertain_result(
                    f"服务端返回 {response_text}",
                    response_text=response_text,
                )
            if failure_kind in {"transient", "configuration"}:
                attempt_cleared, attempt_message = self._finish_attempt(attempt_marker)
                local_state_safe = attempt_cleared
                if failure_kind == "transient":
                    prefix = "服务端暂时不可用，已保留原文等待重试"
                else:
                    prefix = "站点配置被服务端拒绝，已停止该站点"
                message = f"{prefix} ({response_text}){attempt_message}"
                return PublishResult(
                    site_name=site.name,
                    title=article.title,
                    success=False,
                    message=message,
                    article_path=article.path,
                    response_text=response_text,
                    local_state_safe=local_state_safe,
                    failure_kind=(failure_kind if local_state_safe else "unsafe_local_state"),
                    stop_site=True,
                    retryable=failure_kind == "transient" and local_state_safe,
                )
            archive_error = ""
            local_state_safe = True
            try:
                source_exists = article.path.is_file()
                source_matches = (
                    source_exists
                    and hashlib.sha256(article.path.read_bytes()).hexdigest() == source_hash
                )
                if source_matches:
                    self.archive_service.archive_failure(
                        article.path,
                        site_key,
                        source_hash=source_hash,
                    )
                elif source_exists:
                    archive_error = "; 请求期间源文件已变化，当前文件已保留"
                else:
                    archive_error = "; 请求期间源文件已移除，未执行失败归档"
                attempt_cleared, attempt_message = self._finish_attempt(attempt_marker)
                if not attempt_cleared:
                    archive_error += attempt_message
                    local_state_safe = False
            except SourceIdentityChangedError as exc:
                archive_error = self.redact_sensitive(
                    f"; 请求期间源文件已变化，当前文件已保留: {exc}"
                )
                attempt_cleared, attempt_message = self._finish_attempt(attempt_marker)
                archive_error += attempt_message
                source_preserved = article.path.is_file()
                if not source_preserved:
                    archive_error += "; 原文件未能恢复，需从失败归档中人工取回"
                local_state_safe = attempt_cleared and source_preserved
            except Exception as exc:
                archive_error = self.redact_sensitive(f"; 失败归档异常: {exc}")
                local_state_safe = False
            return PublishResult(
                site_name=site.name,
                title=article.title,
                success=False,
                message=f"服务端拒绝发布 ({response_text}){archive_error}",
                article_path=article.path,
                response_text=response_text,
                local_state_safe=local_state_safe,
                failure_kind="article_rejected" if local_state_safe else "unsafe_local_state",
                stop_site=not local_state_safe,
            )

        local_errors: list[str] = []
        local_state_safe = True
        success_evidence_recorded = False
        try:
            self.archive_service.archive_success(
                article.path,
                site_key,
                source_hash=source_hash,
                attempt_marker=attempt_marker,
            )
            success_evidence_recorded = True
        except Exception as exc:
            marker_checker = getattr(self.archive_service, "has_pending_success_marker", None)
            success_recorded = callable(marker_checker) and marker_checker(
                article.path,
                site_key,
                source_hash,
                attempt_marker,
            )
            if success_recorded:
                quarantined = False
                quarantine_message = "; 已保留成功恢复标记和原文件，需确认文件内容后再处理"
                success_evidence_recorded = True
            else:
                quarantined, quarantine_message = self._quarantine_success_without_archive(
                    article.path,
                    source_hash,
                )
                success_evidence_recorded = quarantined
            local_state_safe = quarantined and not success_recorded
            local_errors.append(self.redact_sensitive(f"成功归档失败: {exc}{quarantine_message}"))
        if success_evidence_recorded:
            attempt_cleared, attempt_message = self._finish_attempt(attempt_marker)
            if not attempt_cleared:
                local_state_safe = False
                local_errors.append(attempt_message)
        try:
            completion_date = self.now_func().strftime("%Y-%m-%d")
            ensure_daily_file(site.daily_publish_filename, today=completion_date)
            completion_count = get_published_count(
                site.daily_publish_filename,
                today=completion_date,
            )
            archived_count = 0
            if callable(archive_counter):
                archived_count = archive_counter(
                    site_key,
                    publish_dir=site.publish_dir,
                    today=completion_date,
                )
            update_published_count(
                site.daily_publish_filename,
                max(completion_count + 1, archived_count),
                today=completion_date,
            )
        except Exception as exc:
            local_state_safe = False
            local_errors.append(self.redact_sensitive(f"发布计数写入失败: {exc}"))
        return PublishResult(
            site_name=site.name,
            title=article.title,
            success=True,
            message="发布成功" if not local_errors else f"发布成功，但本地收尾异常: {'; '.join(local_errors)}",
            article_path=article.path,
            response_text=response_text,
            local_state_safe=local_state_safe,
            failure_kind="" if local_state_safe else "unsafe_local_state",
            stop_site=not local_state_safe,
            site_id=site.site_id,
            article_url=self.published_article_url(response.text),
        )

    def _finish_attempt(self, attempt_marker: Path | None) -> tuple[bool, str]:
        if attempt_marker is None:
            return True, ""
        finisher = getattr(self.archive_service, "finish_publish_attempt", None)
        if not callable(finisher):
            return False, "; 发布请求凭证无法清除"
        try:
            finisher(attempt_marker)
            return True, ""
        except Exception as exc:
            return False, self.redact_sensitive(f"; 发布请求凭证清除失败: {exc}")

    @staticmethod
    def _quarantine_success_without_archive(
        article_path: Path,
        source_hash: str | None = None,
    ) -> tuple[bool, str]:
        return Publisher._quarantine_path(
            article_path,
            ".published",
            expected_hash=source_hash,
        )

    @staticmethod
    def _quarantine_path(
        article_path: Path,
        suffix: str,
        expected_hash: str | None = None,
    ) -> tuple[bool, str]:
        source = Path(article_path)
        if not source.exists():
            if expected_hash is not None:
                return False, "; 原文件已移除，已保留未确认发布凭证"
            return True, ""
        if expected_hash is not None:
            try:
                if hashlib.sha256(source.read_bytes()).hexdigest() != expected_hash:
                    return False, "; 原文件身份已变化，已保留当前文件并停止自动重试"
            except OSError as exc:
                return False, f"; 原文件身份无法确认，已停止自动重试: {exc}"
        target = source.with_name(f"{source.stem}{suffix}")
        index = 1
        while target.exists():
            target = source.with_name(f"{source.stem}-{index}{suffix}")
            index += 1
        try:
            source.rename(target)
            if (
                expected_hash is not None
                and hashlib.sha256(target.read_bytes()).hexdigest() != expected_hash
            ):
                try:
                    target.rename(source)
                except OSError:
                    pass
                return False, "; 隔离时原文件身份变化，已停止自动重试"
            target.touch()
            return True, f"; 原文件已改名为 {target.name}，避免自动重复发布"
        except Exception as exc:
            return False, f"; 原文件隔离失败，必须手动移走: {exc}"
