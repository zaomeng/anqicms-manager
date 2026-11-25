"""GitHub Releases self-updater engine for AnqiCMS Manager (pure stdlib + requests)."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from core.app_info import (
    APP_CHECKSUM_ASSET_NAME,
    APP_RELEASE_ASSET_NAME,
    APP_VERSION,
    GITHUB_REPO,
)
from core.upgrade_service import compare_versions

__all__ = [
    "AppUpdateInfo",
    "SelfUpdateError",
    "check_app_update",
    "create_windows_replace_script",
    "download_and_verify_update",
    "is_app_frozen",
    "launch_replace_script",
    "parse_sha256sums",
]

_CHUNK_SIZE = 64 * 1024
_GITHUB_API_BASE = "https://api.github.com"
_SHA256_LINE_RE = re.compile(r"^\s*([0-9a-fA-F]{64})\s+\*?(.+?)\s*$")
_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")
_SINGLE_INT_RE = re.compile(r"^\d+$")
_CMD_UNSAFE_META_CHARS = frozenset('&|<>^()%!"')

ProgressCallback = Callable[[int, int, str], None]


class SelfUpdateError(RuntimeError):
    """Raised when checking, downloading, verifying, or applying a self-update fails."""


@dataclass(frozen=True)
class AppUpdateInfo:
    """Metadata describing the current app version and latest GitHub Release."""

    current_version: str
    latest_version: str
    update_available: bool
    repo: str
    release_url: str
    published_at: str
    release_notes: str
    asset_name: str
    download_url: str
    asset_size: int
    checksum_url: str
    is_frozen: bool
    message: str


def is_app_frozen() -> bool:
    """Return True when running inside a frozen executable bundle."""
    return bool(getattr(sys, "frozen", False))


def _strip_version_prefix(raw: str) -> str:
    cleaned = (raw or "").strip()
    if cleaned[:1].casefold() == "v":
        cleaned = cleaned[1:].strip()
    return cleaned


def _to_comparable_version(raw: str) -> str:
    cleaned = _strip_version_prefix(raw)
    if _SINGLE_INT_RE.match(cleaned):
        return f"{cleaned}.0"
    return cleaned


def _is_newer_version(current_version: str, latest_version: str) -> bool:
    cur = _to_comparable_version(current_version)
    lat = _to_comparable_version(latest_version)
    if not cur or not lat:
        return False
    return compare_versions(cur, lat) < 0


def _lower_headers(headers: Any) -> dict[str, str]:
    if not headers or not hasattr(headers, "items"):
        return {}
    return {str(k).casefold(): str(v) for k, v in headers.items()}


def _close_quietly(resource: Any) -> None:
    close_fn = getattr(resource, "close", None)
    if callable(close_fn):
        try:
            close_fn()
        except Exception:  # noqa: BLE001
            pass


def _open_session(session: Any) -> tuple[Any, bool]:
    if session is not None:
        return session, False
    return requests.Session(), True


def _format_network_error(prefix: str, exc: Exception) -> str:
    if isinstance(exc, requests.Timeout):
        return f"{prefix}：请求超时，请检查网络连接后重试"
    if isinstance(exc, requests.exceptions.SSLError):
        return f"{prefix}：HTTPS 证书校验失败"
    if isinstance(exc, requests.ConnectionError):
        return f"{prefix}：无法连接到服务器，请检查网络设置"
    return f"{prefix}：{exc}"


def _is_rate_limit_response(response: Any) -> bool:
    status = int(getattr(response, "status_code", 0) or 0)
    if status == 429:
        return True
    if status != 403:
        return False
    headers = _lower_headers(getattr(response, "headers", None))
    if headers.get("x-ratelimit-remaining", "").strip() == "0":
        return True
    body_text = str(getattr(response, "text", "") or "").casefold()
    return "rate limit" in body_text


def _select_release_assets(assets: Any) -> tuple[str, str, int, str]:
    """Locate the release installer asset and optional checksum asset."""
    if not isinstance(assets, list):
        return "", "", 0, ""

    valid_assets: list[dict[str, Any]] = []
    for item in assets:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        url = str(item.get("browser_download_url") or "").strip()
        if name and url:
            valid_assets.append(item)

    chosen_exe: dict[str, Any] | None = None
    for item in valid_assets:
        if str(item["name"]).strip() == APP_RELEASE_ASSET_NAME:
            chosen_exe = item
            break
    if chosen_exe is None:
        target_cf = APP_RELEASE_ASSET_NAME.casefold()
        for item in valid_assets:
            if str(item["name"]).strip().casefold() == target_cf:
                chosen_exe = item
                break
    if chosen_exe is None:
        for item in valid_assets:
            if str(item["name"]).strip().casefold().endswith(".exe"):
                chosen_exe = item
                break

    chosen_checksum: dict[str, Any] | None = None
    for item in valid_assets:
        if str(item["name"]).strip() == APP_CHECKSUM_ASSET_NAME:
            chosen_checksum = item
            break
    if chosen_checksum is None:
        checksum_cf = APP_CHECKSUM_ASSET_NAME.casefold()
        for item in valid_assets:
            if str(item["name"]).strip().casefold() == checksum_cf:
                chosen_checksum = item
                break
    if chosen_checksum is None:
        for item in valid_assets:
            lowered = str(item["name"]).strip().casefold()
            if "sha256sums" in lowered and lowered.endswith(".txt"):
                chosen_checksum = item
                break

    asset_name = str(chosen_exe["name"]).strip() if chosen_exe else ""
    download_url = str(chosen_exe["browser_download_url"]).strip() if chosen_exe else ""
    try:
        asset_size = int(chosen_exe.get("size") or 0) if chosen_exe else 0
    except (TypeError, ValueError):
        asset_size = 0
    checksum_url = (
        str(chosen_checksum["browser_download_url"]).strip() if chosen_checksum else ""
    )
    return asset_name, download_url, max(0, asset_size), checksum_url


def parse_sha256sums(text: str, asset_name: str) -> str:
    """Extract the 64-char lowercase SHA256 hex digest for ``asset_name``.

    Supports both binary mode (``<hash> *<filename>``) and text mode
    (``<hash>  <filename>``), strips UTF-8 BOMs, ignores blank/comment lines,
    and falls back to basename / case-insensitive matching if needed. Returns an
    empty string if no matching entry is found.
    """
    if not isinstance(text, str) or not isinstance(asset_name, str):
        return ""
    target = asset_name.strip()
    if not target:
        return ""
    target_basename = target.replace("\\", "/").rsplit("/", 1)[-1]
    target_cf = target_basename.casefold()

    casefold_fallback = ""
    exe_digests: list[str] = []
    for raw_line in text.lstrip("\ufeff").splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        match = _SHA256_LINE_RE.match(line)
        if not match:
            continue
        digest = match.group(1).lower()
        entry_name = match.group(2).strip()
        if len(entry_name) >= 2 and (
            (entry_name[0] == '"' and entry_name[-1] == '"')
            or (entry_name[0] == "'" and entry_name[-1] == "'")
        ):
            entry_name = entry_name[1:-1].strip()
        entry_basename = entry_name.replace("\\", "/").rsplit("/", 1)[-1]
        if entry_name == target or entry_basename == target_basename:
            return digest
        if not casefold_fallback and entry_basename.casefold() == target_cf:
            casefold_fallback = digest
        if entry_basename.casefold().endswith(".exe") and digest not in exe_digests:
            exe_digests.append(digest)
    if casefold_fallback:
        return casefold_fallback
    # GitHub Releases sanitizes non-ASCII asset filenames (e.g. anqicms站群控制台.exe -> anqicms.exe);
    # fall back to the sole .exe digest when target is an .exe and only one unique .exe hash exists.
    if target_cf.endswith(".exe") and len(exe_digests) == 1:
        return exe_digests[0]
    return ""


def check_app_update(
    repo: str = GITHUB_REPO,
    current_version: str = APP_VERSION,
    *,
    session: Any = None,
    timeout: float = 15.0,
) -> AppUpdateInfo:
    """Query GitHub Releases ``latest`` endpoint and return update metadata."""
    cleaned_repo = (repo or "").strip().strip("/")
    if not _REPO_SLUG_RE.match(cleaned_repo):
        raise SelfUpdateError("GitHub 仓库配置无效")

    current_clean = _strip_version_prefix(current_version) or APP_VERSION
    url = f"{_GITHUB_API_BASE}/repos/{cleaned_repo}/releases/latest"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"AnqiCMS-Manager/{current_clean}",
    }

    sess, owns_session = _open_session(session)
    try:
        try:
            response = sess.get(url, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            raise SelfUpdateError(_format_network_error("检查应用更新失败", exc)) from exc

        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code == 404:
            return AppUpdateInfo(
                current_version=current_clean,
                latest_version="",
                update_available=False,
                repo=cleaned_repo,
                release_url="",
                published_at="",
                release_notes="",
                asset_name="",
                download_url="",
                asset_size=0,
                checksum_url="",
                is_frozen=is_app_frozen(),
                message="仓库暂未发布 Release 版本",
            )

        if _is_rate_limit_response(response):
            raise SelfUpdateError("GitHub API 请求过于频繁（已触发限流），请稍后再试")

        if status_code < 200 or status_code >= 300:
            raise SelfUpdateError(f"检查应用更新失败（GitHub 返回 HTTP {status_code}）")

        try:
            payload = response.json()
        except ValueError as exc:
            raise SelfUpdateError("GitHub Release 响应 JSON 解析失败") from exc

        if not isinstance(payload, dict):
            raise SelfUpdateError("GitHub Release 响应格式无效")
    finally:
        if owns_session:
            _close_quietly(sess)

    raw_tag = str(payload.get("tag_name") or payload.get("name") or "").strip()
    latest_clean = _strip_version_prefix(raw_tag)
    release_url = str(payload.get("html_url") or "").strip()
    published_at = str(payload.get("published_at") or "").strip()
    release_notes = str(payload.get("body") or "").strip()
    asset_name, download_url, asset_size, checksum_url = _select_release_assets(
        payload.get("assets")
    )

    update_available = _is_newer_version(current_clean, latest_clean)
    if update_available and download_url:
        message = f"发现新版本 {latest_clean}（当前版本 {current_clean}）"
    elif update_available:
        message = f"发现新版本 {latest_clean}，但当前 Release 暂未附带可执行安装包"
    else:
        display_cur = current_clean or latest_clean
        message = f"当前已是最新版本（{display_cur}）"

    return AppUpdateInfo(
        current_version=current_clean,
        latest_version=latest_clean,
        update_available=update_available,
        repo=cleaned_repo,
        release_url=release_url,
        published_at=published_at,
        release_notes=release_notes,
        asset_name=asset_name,
        download_url=download_url,
        asset_size=asset_size,
        checksum_url=checksum_url,
        is_frozen=is_app_frozen(),
        message=message,
    )


def _is_cancelled(cancel_event: Any) -> bool:
    return bool(cancel_event is not None and cancel_event.is_set())


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def download_and_verify_update(
    info: AppUpdateInfo,
    target_dir: Path,
    *,
    session: Any = None,
    progress: ProgressCallback | None = None,
    cancel_event: Any = None,
    timeout: float = 60.0,
) -> Path:
    """Stream the release executable to ``<target_dir>/<asset_name>.new`` and verify SHA256."""
    if _is_cancelled(cancel_event):
        raise SelfUpdateError("更新下载已取消")
    if not info.download_url:
        raise SelfUpdateError("当前版本没有可用的安装包下载地址")

    raw_name = (info.asset_name or APP_RELEASE_ASSET_NAME).strip()
    safe_name = raw_name.replace("\\", "/").rsplit("/", 1)[-1]
    if not safe_name or safe_name in {".", ".."}:
        raise SelfUpdateError("安装包文件名无效")

    dest_dir = Path(target_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{safe_name}.new"

    headers = {
        "User-Agent": f"AnqiCMS-Manager/{info.current_version or APP_VERSION}",
    }

    sess, owns_session = _open_session(session)
    downloaded = 0
    hasher = hashlib.sha256()

    try:
        response = None
        try:
            response = sess.get(
                info.download_url,
                headers=headers,
                stream=True,
                timeout=timeout,
            )
            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code < 200 or status_code >= 300:
                raise SelfUpdateError(f"下载安装包失败（HTTP {status_code}）")

            resp_headers = _lower_headers(getattr(response, "headers", None))
            total_bytes = max(0, int(info.asset_size or 0))
            raw_len = resp_headers.get("content-length", "").strip()
            if raw_len.isdigit() and int(raw_len) > 0:
                total_bytes = int(raw_len)

            if progress is not None:
                progress(0, total_bytes, "正在下载更新安装包...")

            with dest_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                    if _is_cancelled(cancel_event):
                        raise SelfUpdateError("更新下载已取消")
                    if not chunk:
                        continue
                    handle.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    if progress is not None:
                        progress(downloaded, total_bytes, "正在下载更新安装包...")
        except SelfUpdateError:
            _safe_unlink(dest_path)
            raise
        except requests.RequestException as exc:
            _safe_unlink(dest_path)
            raise SelfUpdateError(_format_network_error("下载安装包失败", exc)) from exc
        except OSError as exc:
            _safe_unlink(dest_path)
            raise SelfUpdateError(f"写入更新文件失败：{exc}") from exc
        finally:
            if response is not None:
                _close_quietly(response)

        if _is_cancelled(cancel_event):
            _safe_unlink(dest_path)
            raise SelfUpdateError("更新下载已取消")

        if downloaded <= 0:
            _safe_unlink(dest_path)
            raise SelfUpdateError("下载的安装包为空")

        if info.checksum_url:
            if progress is not None:
                progress(downloaded, downloaded, "正在校验安装包 SHA256...")
            try:
                checksum_resp = sess.get(info.checksum_url, headers=headers, timeout=timeout)
            except requests.RequestException as exc:
                _safe_unlink(dest_path)
                raise SelfUpdateError(_format_network_error("下载 SHA256 校验文件失败", exc)) from exc

            checksum_status = int(getattr(checksum_resp, "status_code", 0) or 0)
            if checksum_status < 200 or checksum_status >= 300:
                _safe_unlink(dest_path)
                raise SelfUpdateError(f"下载 SHA256 校验文件失败（HTTP {checksum_status}）")

            expected_hash = parse_sha256sums(str(getattr(checksum_resp, "text", "") or ""), safe_name)
            if not expected_hash and info.asset_name and info.asset_name != safe_name:
                expected_hash = parse_sha256sums(
                    str(getattr(checksum_resp, "text", "") or ""),
                    info.asset_name,
                )
            actual_hash = hasher.hexdigest().lower()
            if not expected_hash or actual_hash != expected_hash:
                _safe_unlink(dest_path)
                raise SelfUpdateError("安装包 SHA256 校验失败")

        if progress is not None:
            done_msg = "更新安装包下载并校验完成" if info.checksum_url else "更新安装包下载完成"
            progress(downloaded, downloaded, done_msg)
        return dest_path
    finally:
        if owns_session:
            _close_quietly(sess)


def _sanitize_cmd_value(value: str) -> str:
    if "\r" in value or "\n" in value or "\x00" in value:
        raise SelfUpdateError("路径或参数包含非法控制字符")
    return value.replace("%", "%%")


def _quote_cmd_arg(arg: str) -> str:
    cleaned = _sanitize_cmd_value(str(arg))
    if not cleaned:
        return '""'
    if any(ch.isspace() or ch in _CMD_UNSAFE_META_CHARS for ch in cleaned):
        escaped = cleaned.replace('"', '\\"')
        return f'"{escaped}"'
    return cleaned


def create_windows_replace_script(
    new_exe: Path,
    current_exe: Path,
    *,
    current_pid: int,
    relaunch_args: Sequence[str] = (),
    script_dir: Path | None = None,
) -> Path:
    """Write a UTF-8 Windows ``.cmd`` script that replaces the running exe and restarts it."""
    pid = int(current_pid)
    if pid <= 0:
        raise SelfUpdateError("无效的当前进程 PID")

    new_path = Path(new_exe)
    current_path = Path(current_exe)
    backup_path = Path(f"{current_path}.bak")

    new_val = _sanitize_cmd_value(str(new_path))
    cur_val = _sanitize_cmd_value(str(current_path))
    bak_val = _sanitize_cmd_value(str(backup_path))

    args_str = " ".join(_quote_cmd_arg(str(item)) for item in relaunch_args)
    launch_line = f'start "" "%CUR_EXE%" {args_str}' if args_str else 'start "" "%CUR_EXE%"'

    target_dir = Path(script_dir) if script_dir is not None else Path(tempfile.gettempdir())
    target_dir.mkdir(parents=True, exist_ok=True)
    script_path = target_dir / f"anqicms-self-update-{pid}.cmd"

    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "setlocal EnableExtensions",
        f"set \"PID={pid}\"",
        f"set \"NEW_EXE={new_val}\"",
        f"set \"CUR_EXE={cur_val}\"",
        f"set \"BAK_EXE={bak_val}\"",
        "",
        "set /a RETRIES=0",
        ":wait_exit",
        'tasklist /FI "PID eq %PID%" /NH /FO CSV 2>nul | find """%PID%""" >nul',
        "if not errorlevel 1 (",
        "    set /a RETRIES+=1",
        "    if %RETRIES% GEQ 60 goto :replace_file",
        "    ping -n 2 127.0.0.1 >nul",
        "    goto :wait_exit",
        ")",
        "",
        ":replace_file",
        'if not exist "%NEW_EXE%" goto :cleanup',
        "set /a MOVE_TRIES=0",
        ":retry_replace",
        'if exist "%BAK_EXE%" del /f /q "%BAK_EXE%" >nul 2>nul',
        'if exist "%CUR_EXE%" (',
        '    move /y "%CUR_EXE%" "%BAK_EXE%" >nul 2>nul',
        "    if errorlevel 1 (",
        "        set /a MOVE_TRIES+=1",
        "        if %MOVE_TRIES% GEQ 15 goto :cleanup",
        "        ping -n 2 127.0.0.1 >nul",
        "        goto :retry_replace",
        "    )",
        ")",
        'move /y "%NEW_EXE%" "%CUR_EXE%" >nul 2>nul',
        "if errorlevel 1 (",
        '    if exist "%BAK_EXE%" if not exist "%CUR_EXE%" move /y "%BAK_EXE%" "%CUR_EXE%" >nul 2>nul',
        "    set /a MOVE_TRIES+=1",
        "    if %MOVE_TRIES% GEQ 15 goto :cleanup",
        "    ping -n 2 127.0.0.1 >nul",
        "    goto :retry_replace",
        ")",
        "",
        launch_line,
        "",
        ":cleanup",
        '(goto) 2>nul & del /f /q "%~f0" >nul 2>nul',
    ]

    content = "\r\n".join(lines) + "\r\n"
    script_path.write_bytes(content.encode("utf-8"))
    return script_path


def launch_replace_script(script_path: Path) -> None:
    """Start the Windows replacement script in a detached background process."""
    path = Path(script_path)
    if not path.is_file():
        raise SelfUpdateError(f"更新替换脚本不存在：{path}")

    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
    else:
        kwargs["start_new_session"] = True

    try:
        subprocess.Popen(["cmd", "/c", str(path)], **kwargs)
    except OSError as exc:
        raise SelfUpdateError(f"启动更新替换脚本失败：{exc}") from exc
