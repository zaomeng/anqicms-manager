from __future__ import annotations

import configparser
import io
import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from models.entities import SiteConfig
from core.category_keywords import CategoryKeywordsError, normalize_category_keywords
from core.runtime_paths import app_dir
from core.schedule_service import is_valid_publish_time, normalize_publish_time
from core.state_store import (
    atomic_write_text,
    ensure_daily_file,
)
from core.site_factory import (
    DEFAULT_ADMIN_PATH,
    derive_site_paths,
    is_valid_publish_endpoint,
    is_valid_site_url,
    normalize_admin_path,
    normalize_site_url,
    site_url_identity,
    validate_site_key,
)


class ConfigLoadError(RuntimeError):
    pass


SITE_GROUP_MAX_LENGTH = 64
# (INI key, 中文名称)。凭据（mcp_token / admin_password）与 jiekou 中的 token 一样以明文保存。
_SITEGROUP_TEXT_FIELDS: tuple[tuple[str, str], ...] = (
    ("site_title", "站点显示名"),
    ("site_group", "站点分组"),
    ("mcp_token", "MCP Token"),
    ("admin_username", "后台账号"),
    ("admin_password", "后台密码"),
)


def _has_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _validate_sitegroup_text(site_name: str, key: str, label: str, value: object) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ConfigLoadError(f"站点 {site_name} 的{label}无效")
    value = value.strip()
    if _has_control_characters(value):
        raise ConfigLoadError(f"站点 {site_name} 的{label}不能包含换行或控制字符")
    if key == "site_group" and len(value) > SITE_GROUP_MAX_LENGTH:
        raise ConfigLoadError(
            f"站点 {site_name} 的{label}不能超过 {SITE_GROUP_MAX_LENGTH} 个字符"
        )
    return value


def _normalize_admin_path_or_error(site_name: str, raw_value: object) -> str:
    try:
        return normalize_admin_path("" if raw_value is None else str(raw_value))
    except ValueError as exc:
        raise ConfigLoadError(f"站点 {site_name} 的后台路径无效") from exc


def _load_sitegroup_fields(item: configparser.SectionProxy, section: str) -> dict[str, str]:
    values = {
        key: _validate_sitegroup_text(section, key, label, item.get(key, ""))
        for key, label in _SITEGROUP_TEXT_FIELDS
    }
    values["admin_path"] = _normalize_admin_path_or_error(section, item.get("admin_path", ""))
    return values


def _sitegroup_ini_values(site: SiteConfig) -> dict[str, str]:
    """Return only the non-default sitegroup keys (legacy key set stays exact)."""

    values: dict[str, str] = {}
    for key, label in _SITEGROUP_TEXT_FIELDS:
        value = _validate_sitegroup_text(site.name, key, label, getattr(site, key, ""))
        if value:
            values[key] = value
    admin_path = _normalize_admin_path_or_error(site.name, getattr(site, "admin_path", ""))
    if admin_path != DEFAULT_ADMIN_PATH:
        values["admin_path"] = admin_path
    return values


def _is_legacy_external_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and bool(parsed.hostname)
        and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def _parse_publish_times(raw_value: str, fallback: str) -> list[str]:
    values = [item.strip() for item in raw_value.split(",") if item.strip()]
    if values:
        return values
    return [fallback] if fallback else []


def _deduplicate_publish_times(values: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = normalize_publish_time(value)
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return unique


def _parse_enabled(raw_value: str) -> bool:
    return raw_value.strip().casefold() in {"1", "true", "yes"}


def _validate_iso_date(value: str, field_name: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise ConfigLoadError(f"{field_name}必须使用 YYYY-MM-DD 格式") from exc


def _normalize_path(base_dir: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path.resolve()
    return (base_dir / path).resolve()


def _validate_config_path(
    base_dir: Path,
    target: Path,
    field_name: str,
    *,
    allow_base: bool = True,
    allow_external: bool = False,
) -> Path:
    resolved_base = base_dir.resolve()
    resolved_target = Path(target).resolve()
    if not allow_external:
        try:
            resolved_target.relative_to(resolved_base)
        except ValueError as exc:
            raise ConfigLoadError(f"{field_name} 必须位于配置目录内") from exc
    if not allow_base and resolved_target == resolved_base:
        raise ConfigLoadError("文章目录不能是配置目录")
    return resolved_target


def _load_config_path(
    base_dir: Path,
    raw_path: str,
    field_name: str,
    *,
    allow_base: bool = True,
    allow_external: bool = False,
) -> Path:
    return _validate_config_path(
        base_dir,
        _normalize_path(base_dir, raw_path),
        field_name,
        allow_base=allow_base,
        allow_external=allow_external,
    )


def _to_raw_path(base_dir: Path, target: Path | None) -> str:
    if target is None:
        return ""
    candidate = Path(target)
    if not candidate.is_absolute():
        return candidate.as_posix().replace("/", "\\") if "\\" in str(candidate) else candidate.as_posix()
    try:
        rel = candidate.resolve().relative_to(base_dir.resolve())
        return f"./{rel.as_posix()}"
    except ValueError:
        return str(candidate)


def _path_identity(path: Path) -> str:
    return str(Path(path).resolve()).casefold()


def _path_is_within(target: Path, directory: Path) -> bool:
    try:
        Path(target).resolve().relative_to(Path(directory).resolve())
        return True
    except ValueError:
        return False


def _paths_overlap(left: Path, right: Path) -> bool:
    return _path_is_within(left, right) or _path_is_within(right, left)


def _validate_site_conflicts(
    sites: list[SiteConfig],
    base_dir: Path,
    config_path: Path,
) -> None:
    seen_names: set[str] = set()
    seen_site_ids: set[str] = set()
    seen_urls: set[tuple[str, str, int | None, str]] = set()
    seen_publish_dirs: set[str] = set()
    seen_daily_files: set[str] = set()
    publish_dirs = [Path(site.publish_dir).resolve() for site in sites]
    daily_identities = {_path_identity(site.daily_publish_filename) for site in sites}
    archive_roots = {(base_dir / "归档").resolve(), (app_dir() / "归档").resolve()}
    reserved_runtime_files = {
        config_path.resolve(),
        config_path.with_name(f".{config_path.name}.lock").resolve(),
        (base_dir / "fabu.log").resolve(),
        (base_dir / "设置" / "schedule_state.json").resolve(),
        (base_dir / "设置" / "automation_state.json").resolve(),
        (base_dir / "设置" / "image_galleries.json").resolve(),
        (base_dir / "设置" / "category_cache.json").resolve(),
        (base_dir / "设置" / "window_state.json").resolve(),
        (base_dir / "设置" / "archive_cleanup_state.txt").resolve(),
        (base_dir / "设置" / "friendlink_ledger.json").resolve(),
        (base_dir / "设置" / "integration_settings.json").resolve(),
    }
    for publish_dir in publish_dirs:
        if any(_paths_overlap(publish_dir, archive_root) for archive_root in archive_roots):
            raise ConfigLoadError(f"文章目录不能与归档目录重合: {publish_dir}")
    for site in sites:
        name_identity = validate_site_key(site.name).casefold()
        if name_identity in seen_names:
            raise ConfigLoadError(f"站点标识重复（大小写冲突）: {site.name}")
        seen_names.add(name_identity)

        if site.site_id:
            try:
                site_id = str(uuid.UUID(site.site_id))
            except ValueError as exc:
                raise ConfigLoadError(f"站点 ID 无效: {site.name}") from exc
            if site_id in seen_site_ids:
                raise ConfigLoadError(f"站点 ID 重复: {site.name}")
            seen_site_ids.add(site_id)

        if site.url:
            url_identity = site_url_identity(site.url)
            if url_identity in seen_urls:
                raise ConfigLoadError(f"站点网址重复: {site.url}")
            seen_urls.add(url_identity)

        publish_identity = _path_identity(site.publish_dir)
        if publish_identity in seen_publish_dirs:
            raise ConfigLoadError(f"文章目录被多个站点共用: {site.publish_dir}")
        seen_publish_dirs.add(publish_identity)

        daily_identity = _path_identity(site.daily_publish_filename)
        if daily_identity in seen_daily_files:
            raise ConfigLoadError(f"发布计数文件被多个站点共用: {site.daily_publish_filename}")
        seen_daily_files.add(daily_identity)

        publish_dir = Path(site.publish_dir)
        if publish_dir.exists() and not publish_dir.is_dir():
            raise ConfigLoadError(f"文章目录不是文件夹: {publish_dir}")
        readonly_files: list[Path] = []
        if site.image_file is not None:
            readonly_files.append(Path(site.image_file))
        for readonly_file in readonly_files:
            if _path_identity(readonly_file) in daily_identities:
                raise ConfigLoadError(f"发布计数文件不能与图片文件共用路径: {readonly_file}")
        managed_files = readonly_files + [Path(site.daily_publish_filename)]
        for managed_file in managed_files:
            resolved_managed_file = managed_file.resolve()
            if resolved_managed_file in reserved_runtime_files:
                raise ConfigLoadError(f"受管文件不能使用保留的配置、日志或状态路径: {managed_file}")
            if any(_path_is_within(managed_file, archive_root) for archive_root in archive_roots):
                raise ConfigLoadError(f"受管文件不能位于归档目录内: {managed_file}")
            if managed_file.exists() and not managed_file.is_file():
                raise ConfigLoadError(f"受管文件路径不是文件: {managed_file}")
            if any(_path_is_within(managed_file, publish_dir) for publish_dir in publish_dirs):
                raise ConfigLoadError(f"受管配置或状态文件不能位于文章目录内: {managed_file}")


def load_config(config_path: str | Path) -> list[SiteConfig]:
    config_path = Path(config_path).resolve()
    if not config_path.exists():
        return []
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(config_path, encoding="utf-8-sig")
    except (configparser.Error, OSError, UnicodeError) as exc:
        raise ConfigLoadError(f"配置文件格式错误: {config_path.name}") from exc
    base_dir = config_path.parent
    sites: list[SiteConfig] = []
    for section in parser.sections():
        try:
            normalized_section = validate_site_key(section)
        except ValueError as exc:
            raise ConfigLoadError(f"站点标识无效: {section}") from exc
        item = parser[section]
        required_paths = {
            key: item.get(key, "").strip()
            for key in ("publish_dir", "daily_publish_filename")
        }
        if any(not value for value in required_paths.values()):
            raise ConfigLoadError(f"站点 {section} 的关键路径不能为空")
        category_keywords_raw = item.get("category_keywords", "").strip()
        if category_keywords_raw:
            try:
                decoded_category_keywords = json.loads(category_keywords_raw)
            except json.JSONDecodeError as exc:
                raise ConfigLoadError(
                    f"站点 {section} 的栏目关键词不是有效 JSON"
                ) from exc
            try:
                category_keywords = normalize_category_keywords(
                    decoded_category_keywords
                )
            except CategoryKeywordsError as exc:
                raise ConfigLoadError(f"站点 {section} 的栏目关键词无效: {exc}") from exc
        else:
            category_keywords = {}
        image_raw = item.get("image_file", "").strip()
        image_file = _load_config_path(base_dir, image_raw, "图片文件") if image_raw else None
        site_url = normalize_site_url(item.get("url", ""))
        url_is_publishable = bool(site_url) and is_valid_site_url(site_url)
        if site_url and not url_is_publishable and not _is_legacy_external_http_url(site_url):
            raise ConfigLoadError(f"站点 {section} 的站点网址无效")
        endpoint = item.get("jiekou", "").strip()
        endpoint_is_publishable = bool(endpoint) and is_valid_publish_endpoint(endpoint)
        endpoint_has_unsafe_characters = any(
            character == "\\" or ord(character) < 32 for character in endpoint
        )
        if endpoint and not endpoint_is_publishable and endpoint_has_unsafe_characters:
            raise ConfigLoadError(f"站点 {section} 的发布接口无效")
        publish_time = item.get("publish_time", "06:00:00").strip()
        publish_times = _parse_publish_times(item.get("publish_times", "").strip(), publish_time)
        if (publish_time and not is_valid_publish_time(publish_time)) or any(
            not is_valid_publish_time(value) for value in publish_times
        ):
            raise ConfigLoadError(f"站点 {section} 的发布时间无效")
        publish_time = normalize_publish_time(publish_time) if publish_time else ""
        publish_times = _deduplicate_publish_times(publish_times)
        if publish_times:
            publish_times = [publish_times[0]]
            publish_time = publish_times[0]
        try:
            fid = int(item.get("fid", "0"))
            interval_time = int(item.get("interval_time", "60"))
            daily_publish_limit = int(item.get("daily_publish_limit", "60"))
            article_warning_threshold = int(item.get("article_warning_threshold", "500"))
        except ValueError as exc:
            raise ConfigLoadError(f"站点 {section} 的数字配置无效") from exc
        if interval_time < 0 or daily_publish_limit < 0 or article_warning_threshold < 0:
            raise ConfigLoadError(f"站点 {section} 的数字配置不能为负数")
        activation_date = item.get("activation_date", "1970-01-01").strip() or "1970-01-01"
        activation_date = _validate_iso_date(activation_date, f"站点 {section} 的启用日期")
        sitegroup_fields = _load_sitegroup_fields(item, normalized_section)
        sites.append(
            SiteConfig(
                name=normalized_section,
                fid=fid,
                url=site_url,
                jiekou=endpoint,
                publish_dir=_load_config_path(
                    base_dir,
                    required_paths["publish_dir"],
                    "文章目录",
                    allow_base=False,
                    allow_external=True,
                ),
                daily_publish_filename=_load_config_path(
                    base_dir, required_paths["daily_publish_filename"], "发布计数文件"
                ),
                publish_time=publish_time,
                publish_times=publish_times,
                interval_time=interval_time,
                daily_publish_limit=daily_publish_limit,
                enabled=(
                    _parse_enabled(item.get("enabled", "true"))
                    and fid > 0
                    and url_is_publishable
                    and endpoint_is_publishable
                ),
                image_file=image_file,
                site_id=item.get("site_id", "").strip(),
                category_name=item.get("category_name", "").strip(),
                gallery_id=item.get("gallery_id", "").strip(),
                article_warning_threshold=article_warning_threshold,
                activation_date=activation_date,
                category_keywords=category_keywords,
                **sitegroup_fields,
            )
        )
    _validate_site_conflicts(sites, base_dir, config_path)
    return sites


def _display_path(base_dir: Path, target: Path | None) -> str:
    return _to_raw_path(base_dir, target)


def save_config(config_path: str | Path, sites: list[SiteConfig]) -> None:
    config_path = Path(config_path).resolve()
    base_dir = config_path.parent
    parser = configparser.ConfigParser(interpolation=None)
    for site in sites:
        try:
            normalized_name = validate_site_key(site.name)
        except ValueError as exc:
            raise ConfigLoadError(f"站点标识无效: {site.name}") from exc
        if (
            site.interval_time < 0
            or site.daily_publish_limit < 0
            or site.article_warning_threshold < 0
        ):
            raise ConfigLoadError(f"站点 {site.name} 的数字配置不能为负数")
        if site.enabled and site.fid <= 0:
            raise ConfigLoadError(f"站点 {site.name} 的栏目 ID 必须为正整数")
        normalized_url = normalize_site_url(site.url)
        if normalized_url and not is_valid_site_url(normalized_url):
            raise ConfigLoadError(f"站点 {site.name} 的站点网址无效")
        if site.jiekou.strip() and not is_valid_publish_endpoint(site.jiekou):
            raise ConfigLoadError(f"站点 {site.name} 的发布接口无效")
        publish_times = list(site.publish_times or ([site.publish_time] if site.publish_time else []))
        if (site.publish_time and not is_valid_publish_time(site.publish_time)) or any(
            not is_valid_publish_time(value) for value in publish_times
        ):
            raise ConfigLoadError(f"站点 {site.name} 的发布时间无效")
        normalized_publish_time = normalize_publish_time(site.publish_time) if site.publish_time else ""
        normalized_publish_times = _deduplicate_publish_times(publish_times)
        if normalized_publish_times:
            normalized_publish_times = [normalized_publish_times[0]]
            normalized_publish_time = normalized_publish_times[0]
        activation_date = _validate_iso_date(
            site.activation_date or "1970-01-01",
            f"站点 {site.name} 的启用日期",
        )
        try:
            category_keywords = normalize_category_keywords(site.category_keywords)
        except CategoryKeywordsError as exc:
            raise ConfigLoadError(
                f"站点 {site.name} 的栏目关键词无效: {exc}"
            ) from exc
        safe_enabled = site.enabled and bool(normalized_url) and bool(site.jiekou.strip())
        _validate_config_path(
            base_dir,
            site.publish_dir,
            "文章目录",
            allow_base=False,
            allow_external=True,
        )
        _validate_config_path(base_dir, site.daily_publish_filename, "发布计数文件")
        if site.image_file is not None:
            _validate_config_path(base_dir, site.image_file, "图片文件")
        sitegroup_values = _sitegroup_ini_values(site)
        parser[normalized_name] = {
            "fid": str(site.fid),
            "category_keywords": json.dumps(
                category_keywords,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "url": normalized_url,
            "jiekou": site.jiekou,
            "publish_dir": _display_path(base_dir, site.publish_dir),
            "daily_publish_filename": _display_path(base_dir, site.daily_publish_filename),
            "publish_time": normalized_publish_time,
            "publish_times": ",".join(normalized_publish_times),
            "interval_time": str(site.interval_time),
            "daily_publish_limit": str(site.daily_publish_limit),
            "enabled": "true" if safe_enabled else "false",
            "image_file": _display_path(base_dir, site.image_file),
            "site_id": site.site_id,
            "category_name": site.category_name,
            "gallery_id": site.gallery_id,
            "article_warning_threshold": str(site.article_warning_threshold),
            "activation_date": activation_date,
            **sitegroup_values,
        }
    _validate_site_conflicts(sites, base_dir, config_path)
    serialized = io.StringIO()
    parser.write(serialized)
    atomic_write_text(config_path, serialized.getvalue())


def migrate_site_metadata(config_path: str | Path, sites: list[SiteConfig], gallery_service=None) -> bool:
    """Persist stable IDs and valid legacy galleries before automation starts."""

    config_path = Path(config_path).resolve()
    changed = False
    obsolete_uuid_daily_files: list[Path] = []
    seen_ids = {site.site_id for site in sites if site.site_id}
    for site in sites:
        if not site.site_id:
            candidate = str(uuid.uuid4())
            while candidate in seen_ids:
                candidate = str(uuid.uuid4())
            site.site_id = candidate
            seen_ids.add(candidate)
            changed = True
        readable_daily_path = derive_site_paths(
            site.name, config_path.parent
        )["daily_publish_filename"]
        if Path(site.daily_publish_filename).resolve() != readable_daily_path.resolve():
            legacy_daily_path = Path(site.daily_publish_filename)
            if legacy_daily_path.exists():
                try:
                    legacy_content = legacy_daily_path.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    raise ConfigLoadError(
                        f"无法迁移站点 {site.name} 的发布计数文件"
                    ) from exc
                atomic_write_text(readable_daily_path, legacy_content)
            elif not readable_daily_path.exists():
                ensure_daily_file(readable_daily_path)
            uuid_daily_path = (
                config_path.parent
                / "设置"
                / "站点状态"
                / f"{site.site_id}_daily.txt"
            )
            if legacy_daily_path.resolve() == uuid_daily_path.resolve():
                obsolete_uuid_daily_files.append(legacy_daily_path)
            site.daily_publish_filename = readable_daily_path
            changed = True
        if not site.activation_date:
            site.activation_date = "1970-01-01"
            changed = True
        if gallery_service is not None and not site.gallery_id and site.image_file is not None:
            try:
                gallery = gallery_service.migrate_legacy(
                    site.image_file,
                    f"{site.name}-图库",
                    base_dir=config_path.parent,
                )
            except Exception:
                if site.enabled:
                    site.enabled = False
                    changed = True
            else:
                site.gallery_id = gallery.gallery_id
                site.image_file = gallery.file_path
                changed = True
    if not changed:
        return False
    if config_path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = config_path.with_name(f"{config_path.name}.backup-{stamp}")
        shutil.copy2(config_path, backup)
    save_config(config_path, sites)
    for obsolete_path in obsolete_uuid_daily_files:
        try:
            obsolete_path.unlink(missing_ok=True)
            obsolete_path.parent.rmdir()
        except OSError:
            pass
    return True
