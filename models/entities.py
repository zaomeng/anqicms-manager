from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass(slots=True)
class SiteConfig:
    name: str
    fid: int
    url: str
    jiekou: str
    publish_dir: Path
    daily_publish_filename: Path
    publish_time: str
    interval_time: int
    daily_publish_limit: int
    enabled: bool = True
    publish_times: list[str] = field(default_factory=list)
    image_file: Path | None = None
    site_id: str = ""
    category_name: str = ""
    gallery_id: str = ""
    article_warning_threshold: int = 500
    activation_date: str = ""
    category_keywords: dict[int, tuple[str, ...]] = field(default_factory=dict)
    image_groups_snapshot: dict[str, tuple[str, ...]] | None = None
    # 站群字段（全部可选；默认值不会写入 config.ini，保持旧配置逐字节兼容）
    site_title: str = ""  # 友链锚文本/显示名；空 → name
    site_group: str = ""  # 单值分组标签，≤64 字符，无换行
    mcp_token: str = field(default="", repr=False)  # 空 → 回退 jiekou 中的 import token
    admin_username: str = ""
    admin_password: str = field(default="", repr=False)
    admin_path: str = "/system/"


@dataclass(slots=True)
class ArticleRecord:
    site_name: str
    title: str
    path: Path
    category_id: int | None = None
    keywords: str = ""
    status: str = "pending"
    last_error: str = ""
    selected: bool = False


@dataclass(slots=True)
class PublishResult:
    site_name: str
    title: str
    success: bool
    message: str
    article_path: Path
    response_text: str = ""
    local_state_safe: bool = True
    failure_kind: str = ""
    stop_site: bool = False
    retryable: bool = False
    site_id: str = ""
    article_url: str = ""
    published_at: datetime = field(default_factory=datetime.now)


@dataclass(frozen=True, slots=True)
class TaskBuildDiagnostic:
    code: str
    message: str
    severity: str = "info"
    recoverable: bool = False


@dataclass(slots=True)
class SiteStatusSummary:
    site_name: str
    pending_count: int = 0
    article_count: int = 0
    selected_count: int = 0
    published_today: int = 0
    daily_limit: int = 0
    remaining_quota: int = 0
    status: str = "idle"
    last_result: str = ""
    last_error: str = ""


@dataclass(slots=True)
class PlannedRun:
    site_name: str
    scheduled_time: str
    status: str = "pending"
    total_count: int = 0
    result_summary: str = ""
