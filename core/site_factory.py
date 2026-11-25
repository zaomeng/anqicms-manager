from __future__ import annotations

import ipaddress
import re
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote_plus, unquote_plus, urlparse, urlsplit, urlunsplit

from core.state_store import ensure_daily_file
from models.entities import SiteConfig


_INVALID_SITE_KEY = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_DEVICE_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{index}" for prefix in ("COM", "LPT") for index in range(1, 10)
}
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")

DEFAULT_ADMIN_PATH = "/system/"
_ADMIN_PATH_PATTERN = re.compile(r"/[A-Za-z0-9._~\-/]*")


def normalize_admin_path(raw: str) -> str:
    """Normalize an AnQiCMS admin path such as ``/system/``.

    Empty → ``/system/``; leading/trailing ``/`` are forced.  Schemes, hosts,
    queries, fragments, backslashes, whitespace, ``..`` and ``//`` are rejected
    with ``ValueError``.
    """

    value = str(raw if raw is not None else "").strip()
    if not value:
        return DEFAULT_ADMIN_PATH
    invalid = ValueError("后台路径无效：只能包含字母、数字和 . _ ~ - /，例如 /system/")
    if (
        "://" in value
        or any(character in value for character in "?#\\:@%")
        or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value)
        or ".." in value
        or "//" in value
    ):
        raise invalid
    if not value.startswith("/"):
        value = f"/{value}"
    if not value.endswith("/"):
        value = f"{value}/"
    if "//" in value or not _ADMIN_PATH_PATTERN.fullmatch(value):
        raise invalid
    return value


@dataclass(frozen=True, slots=True)
class ParsedPublishEndpoint:
    base_url: str
    publish_endpoint: str
    categories_url: str
    site_key: str
    full_url: str
    raw_query: str

    @property
    def site_url(self) -> str:
        return self.base_url

    @property
    def origin(self) -> str:
        return self.base_url


def validate_site_key(site_key: str) -> str:
    value = site_key.strip()
    device_stem = value.split(".", 1)[0].rstrip(" .").upper()
    if (
        not value
        or value in {".", ".."}
        or _INVALID_SITE_KEY.search(value)
        or value.endswith((".", " "))
        or device_stem in _WINDOWS_DEVICE_NAMES
    ):
        raise ValueError("站点标识无效：包含非法路径字符或 Windows 保留名称")
    return value


def derive_site_paths(
    site_key: str,
    base_dir: str | Path = ".",
) -> dict[str, Path]:
    site_key = validate_site_key(site_key)
    root = Path(base_dir)
    return {
        "publish_dir": root / "文章目录" / site_key,
        "daily_publish_filename": root / "设置" / f"{site_key}_daily.txt",
        "image_file": root / f"{site_key}图片.txt",
    }


def normalize_site_url(url: str) -> str:
    value = url.strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"https://{value}"
    return value.rstrip("/")


def site_url_identity(url: str) -> tuple[str, str, int | None, str]:
    """Return the publishing origin/path identity used for duplicate detection."""
    parsed = urlsplit(normalize_site_url(url))
    try:
        port = parsed.port
    except ValueError:
        return (parsed.scheme.casefold(), f"!invalid:{parsed.netloc.casefold()}", None, parsed.path)
    if port is None:
        port = 443 if parsed.scheme.casefold() == "https" else 80 if parsed.scheme.casefold() == "http" else None
    return (parsed.scheme.casefold(), (parsed.hostname or "").casefold(), port, parsed.path)


def _is_valid_hostname(hostname: str) -> bool:
    if not hostname or any(character.isspace() or character == "\\" or ord(character) < 32 for character in hostname):
        return False
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass
    if all(character.isdigit() or character == "." for character in hostname):
        return False
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    if len(ascii_hostname) > 253 or ascii_hostname.endswith("."):
        return False
    label_pattern = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
    return all(label_pattern.fullmatch(label) for label in ascii_hostname.split("."))


def is_valid_site_url(url: str) -> bool:
    normalized = normalize_site_url(url)
    if any(character.isspace() or character == "\\" or ord(character) < 32 for character in normalized):
        return False
    try:
        parsed = urlparse(normalized)
        parsed.port
    except ValueError:
        return False
    if parsed.query or parsed.fragment:
        return False
    if not _is_valid_hostname(parsed.hostname or "") or parsed.username is not None or parsed.password is not None:
        return False
    return parsed.scheme in {"https", "http"}


def site_key_from_url(url: str) -> str:
    normalized = normalize_site_url(url)
    try:
        host = (urlparse(normalized).hostname or "").lower()
    except ValueError:
        return "site"
    if host.startswith("www."):
        host = host[4:]
    key = re.sub(r"[^a-z0-9]+", "-", host).strip("-")
    return key or "site"


def _site_key_from_endpoint_host(hostname: str) -> str:
    host = hostname.casefold()
    if host.startswith("www."):
        host = host[4:]
    first_label = host.split(".", 1)[0]
    key = re.sub(r"[^a-z0-9]+", "-", first_label).strip("-")
    return key or site_key_from_url(f"https://{hostname}")


def redact_sensitive_url(value: str) -> str:
    text = re.sub(r"(?i)((?:token|api[_-]?key)=)[^&\s]+", r"\1***", str(value))
    return re.sub(r"(https?://)[^/@\s]+@", r"\1***@", text)


def parse_publish_endpoint(
    endpoint: str,
    existing_names: set[str] | None = None,
) -> ParsedPublishEndpoint:
    """Parse a full AnQiCMS archive endpoint without normalizing its raw query."""
    value = endpoint.strip()
    if not value or any(
        character.isspace() or character == "\\" or ord(character) < 32
        for character in value
    ):
        raise ValueError("完整发布接口格式无效")
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError as exc:
        raise ValueError("完整发布接口格式无效") from exc
    hostname = parsed.hostname or ""
    if (
        parsed.scheme.casefold() not in {"https", "http"}
        or not _is_valid_hostname(hostname)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.path
        or parsed.path.rsplit("/", 1)[-1] != "archive"
    ):
        raise ValueError("完整发布接口必须使用 HTTP 或 HTTPS，且路径必须以 /archive 结尾")
    if not parsed.query or _INVALID_PERCENT_ESCAPE.search(parsed.query):
        raise ValueError("完整发布接口必须包含一个非空 token")

    token_values: list[str] = []
    for item in parsed.query.split("&"):
        if not item:
            raise ValueError("完整发布接口查询参数格式无效")
        raw_name, separator, raw_value = item.partition("=")
        if unquote_plus(raw_name) == "token":
            if not separator:
                raw_value = ""
            token_values.append(unquote_plus(raw_value))
    if len(token_values) != 1 or not token_values[0].strip():
        raise ValueError("完整发布接口必须包含一个非空 token")

    site_url = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    publish_endpoint = urlunsplit(("", "", parsed.path, parsed.query, ""))
    category_path = f"{parsed.path.rsplit('/', 1)[0]}/categories"
    categories_url = urlunsplit((parsed.scheme, parsed.netloc, category_path, parsed.query, ""))
    base_key = _site_key_from_endpoint_host(hostname)
    site_key = unique_site_key(base_key, existing_names or set())
    return ParsedPublishEndpoint(
        base_url=site_url,
        publish_endpoint=publish_endpoint,
        categories_url=categories_url,
        site_key=site_key,
        full_url=value,
        raw_query=parsed.query,
    )


def derive_categories_url(endpoint: str) -> str:
    return parse_publish_endpoint(endpoint).categories_url


def is_valid_publish_endpoint(endpoint: str) -> bool:
    value = endpoint.strip()
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        value.startswith("/")
        and not value.startswith("//")
        and not parsed.scheme
        and not parsed.netloc
        and not parsed.fragment
        and not any(
            character.isspace() or character == "\\" or ord(character) < 32
            for character in value
        )
    )


def unique_site_key(base_key: str, existing_names: set[str]) -> str:
    existing_identities = {name.casefold() for name in existing_names}
    if base_key.casefold() not in existing_identities:
        return base_key
    index = 2
    while f"{base_key}-{index}".casefold() in existing_identities:
        index += 1
    return f"{base_key}-{index}"


def build_import_endpoint(api_key: str, endpoint_path: str = "/api/import/archive") -> str:
    token = api_key.strip()
    path = endpoint_path.strip() or "/api/import/archive"
    if not path.startswith("/"):
        path = f"/{path}"
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}token={quote_plus(token)}"


def create_site_from_url_api_key(
    url: str,
    api_key: str,
    existing_names: set[str] | None = None,
    fid: int = 0,
    interval_time: int = 60,
    daily_publish_limit: int = 60,
    publish_times: list[str] | None = None,
    endpoint_path: str = "/api/import/archive",
    site_key: str = "",
    base_dir: str | Path = ".",
    create_files: bool = True,
) -> SiteConfig:
    site_url = normalize_site_url(url)
    requested_key = site_key.strip() or site_key_from_url(site_url)
    site_key = unique_site_key(requested_key, existing_names or set())
    site = create_site_from_key(
        site_key,
        url=site_url,
        jiekou=build_import_endpoint(api_key, endpoint_path=endpoint_path),
        fid=fid,
        interval_time=interval_time,
        daily_publish_limit=daily_publish_limit,
        publish_times=publish_times,
        base_dir=base_dir,
    )
    if create_files:
        ensure_site_files(site)
    return site


def ensure_site_files(site: SiteConfig) -> None:
    site.publish_dir.mkdir(parents=True, exist_ok=True)
    site.daily_publish_filename.parent.mkdir(parents=True, exist_ok=True)
    ensure_daily_file(site.daily_publish_filename)
    if site.image_file is not None and not site.image_file.exists():
        site.image_file.write_text("", encoding="utf-8")


def clone_site_config(
    site: SiteConfig,
    new_name: str,
    new_url: str = "",
    new_jiekou: str = "",
    base_dir: str | Path | None = None,
) -> SiteConfig:
    target_base = base_dir
    if target_base is None:
        target_base = site.publish_dir.parents[1] if len(site.publish_dir.parents) > 1 else "."
    site_id = str(uuid.uuid4())
    paths = derive_site_paths(new_name, target_base)
    return SiteConfig(
        name=new_name,
        fid=site.fid,
        url=new_url or site.url,
        jiekou=new_jiekou or site.jiekou,
        publish_dir=paths["publish_dir"],
        daily_publish_filename=paths["daily_publish_filename"],
        publish_time=site.publish_time,
        publish_times=list(site.publish_times),
        interval_time=site.interval_time,
        daily_publish_limit=site.daily_publish_limit,
        enabled=site.enabled,
        image_file=paths["image_file"],
        site_id=site_id,
        category_name=site.category_name,
        gallery_id="",
        article_warning_threshold=site.article_warning_threshold,
        activation_date=(date.today() + timedelta(days=1)).isoformat(),
        category_keywords=dict(site.category_keywords),
        # 站群：只继承分组与后台路径；显示名与凭据（MCP Token、后台账号密码）绝不复制
        site_group=site.site_group,
        admin_path=site.admin_path or DEFAULT_ADMIN_PATH,
    )


def create_site_from_key(
    site_key: str,
    url: str = "",
    jiekou: str = "",
    fid: int = 0,
    interval_time: int = 60,
    daily_publish_limit: int = 60,
    publish_times: list[str] | None = None,
    base_dir: str | Path = ".",
) -> SiteConfig:
    site_key = validate_site_key(site_key)
    site_id = str(uuid.uuid4())
    paths = derive_site_paths(site_key, base_dir)
    times = publish_times or ["06:00:00"]
    return SiteConfig(
        name=site_key,
        fid=fid,
        url=normalize_site_url(url),
        jiekou=jiekou,
        publish_dir=paths["publish_dir"],
        daily_publish_filename=paths["daily_publish_filename"],
        publish_time=times[0],
        publish_times=times,
        interval_time=interval_time,
        daily_publish_limit=daily_publish_limit,
        enabled=True,
        image_file=paths["image_file"],
        site_id=site_id,
        activation_date=(date.today() + timedelta(days=1)).isoformat(),
    )
