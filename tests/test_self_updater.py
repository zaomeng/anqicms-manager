from __future__ import annotations

import hashlib
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import requests

from core.app_info import (
    APP_CHECKSUM_ASSET_NAME,
    APP_NAME,
    APP_RELEASE_ASSET_NAME,
    APP_VERSION,
    GITHUB_REPO,
)
from core.self_updater import (
    AppUpdateInfo,
    SelfUpdateError,
    check_app_update,
    create_windows_replace_script,
    download_and_verify_update,
    is_app_frozen,
    launch_replace_script,
    parse_sha256sums,
)


@dataclass
class FakeResponse:
    status_code: int = 200
    json_data: Any = None
    text: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    chunks: list[bytes] = field(default_factory=list)
    closed: bool = False

    def json(self) -> Any:
        if isinstance(self.json_data, Exception):
            raise self.json_data
        return self.json_data

    def iter_content(self, chunk_size: int = 65536):
        if self.chunks:
            yield from self.chunks
            return
        data = self.text.encode("utf-8")
        for idx in range(0, len(data), chunk_size):
            yield data[idx : idx + chunk_size]

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        result = self.handler(url, kwargs)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self) -> None:
        self.closed = True


def _sample_release_payload(
    *,
    tag_name: str = "v2.1.0",
    asset_name: str = APP_RELEASE_ASSET_NAME,
    checksum_name: str = APP_CHECKSUM_ASSET_NAME,
    asset_size: int = 1024,
) -> dict[str, Any]:
    assets: list[dict[str, Any]] = []
    if asset_name:
        assets.append(
            {
                "name": asset_name,
                "browser_download_url": f"https://github.com/{GITHUB_REPO}/releases/download/{tag_name}/{asset_name}",
                "size": asset_size,
            }
        )
    if checksum_name:
        assets.append(
            {
                "name": checksum_name,
                "browser_download_url": f"https://github.com/{GITHUB_REPO}/releases/download/{tag_name}/{checksum_name}",
                "size": 128,
            }
        )
    return {
        "tag_name": tag_name,
        "html_url": f"https://github.com/{GITHUB_REPO}/releases/tag/{tag_name}",
        "published_at": "2026-11-25T12:00:00Z",
        "body": "### 修复与优化\n- 支持自动检查更新",
        "assets": assets,
    }


def test_app_info_constants() -> None:
    assert APP_NAME == "AnqiCMS 站群管理工具"
    assert APP_VERSION == "2.0.2"
    assert GITHUB_REPO == "zaomeng/anqicms-manager"
    assert APP_RELEASE_ASSET_NAME == "anqicms-manager.exe"
    assert APP_CHECKSUM_ASSET_NAME == "SHA256SUMS-web.txt"


def test_is_frozen_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert is_app_frozen() is False

    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert is_app_frozen() is False

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert is_app_frozen() is True


def test_check_app_update_finds_newer_release_and_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    session = FakeSession(
        lambda url, kw: FakeResponse(
            status_code=200,
            json_data=_sample_release_payload(tag_name="v2.10.0", asset_size=4096),
        )
    )

    info = check_app_update(current_version="2.2.0", session=session, timeout=10.0)

    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    assert call["timeout"] == 10.0
    assert call["headers"]["Accept"] == "application/vnd.github+json"
    assert call["headers"]["User-Agent"] == "AnqiCMS-Manager/2.2.0"

    assert info.current_version == "2.2.0"
    assert info.latest_version == "2.10.0"
    assert info.update_available is True
    assert info.repo == GITHUB_REPO
    assert info.release_url.endswith("/releases/tag/v2.10.0")
    assert info.published_at == "2026-11-25T12:00:00Z"
    assert "自动检查更新" in info.release_notes
    assert info.asset_name == APP_RELEASE_ASSET_NAME
    assert info.download_url.endswith(APP_RELEASE_ASSET_NAME)
    assert info.asset_size == 4096
    assert info.checksum_url.endswith(APP_CHECKSUM_ASSET_NAME)
    assert info.is_frozen is True
    assert "2.10.0" in info.message


def test_check_app_update_same_or_older_version() -> None:
    same_session = FakeSession(
        lambda url, kw: FakeResponse(
            status_code=200,
            json_data=_sample_release_payload(tag_name="V2.0.0"),
        )
    )
    same_info = check_app_update(current_version="2.0.0", session=same_session)
    assert same_info.latest_version == "2.0.0"
    assert same_info.update_available is False
    assert "已是最新" in same_info.message

    older_session = FakeSession(
        lambda url, kw: FakeResponse(
            status_code=200,
            json_data=_sample_release_payload(tag_name="v1.9.9"),
        )
    )
    older_info = check_app_update(current_version="2.0.0", session=older_session)
    assert older_info.update_available is False


def test_check_app_update_fallback_assets() -> None:
    payload = {
        "tag_name": "v2.1.0",
        "html_url": "https://github.com/zaomeng/anqicms-manager/releases/tag/v2.1.0",
        "published_at": "2026-11-25T12:00:00Z",
        "body": "",
        "assets": [
            {"name": "source.zip", "browser_download_url": "https://example/source.zip", "size": 10},
            {"name": "AnqiCMS-Console.exe", "browser_download_url": "https://example/AnqiCMS-Console.exe", "size": 2048},
            {"name": "release-SHA256SUMS.txt", "browser_download_url": "https://example/release-SHA256SUMS.txt", "size": 99},
        ],
    }
    session = FakeSession(lambda url, kw: FakeResponse(status_code=200, json_data=payload))

    info = check_app_update(current_version="2.0.0", session=session)

    assert info.update_available is True
    assert info.asset_name == "AnqiCMS-Console.exe"
    assert info.download_url == "https://example/AnqiCMS-Console.exe"
    assert info.asset_size == 2048
    assert info.checksum_url == "https://example/release-SHA256SUMS.txt"


def test_check_app_update_404_returns_no_release() -> None:
    session = FakeSession(lambda url, kw: FakeResponse(status_code=404, json_data={"message": "Not Found"}))

    info = check_app_update(session=session)

    assert info.update_available is False
    assert info.latest_version == ""
    assert info.download_url == ""
    assert info.message == "仓库暂未发布 Release 版本"


@pytest.mark.parametrize(
    "response_or_exc,expected_fragment",
    [
        (
            FakeResponse(
                status_code=403,
                headers={"X-RateLimit-Remaining": "0"},
                text='{"message": "API rate limit exceeded"}',
            ),
            "限流",
        ),
        (
            FakeResponse(status_code=429, text="Too Many Requests"),
            "限流",
        ),
        (
            FakeResponse(status_code=502, text="Bad Gateway"),
            "HTTP 502",
        ),
        (
            requests.Timeout("timed out"),
            "超时",
        ),
        (
            requests.ConnectionError("dns failed"),
            "无法连接",
        ),
    ],
)
def test_check_app_update_rate_limit_and_network_errors(
    response_or_exc: FakeResponse | Exception,
    expected_fragment: str,
) -> None:
    session = FakeSession(lambda url, kw: response_or_exc)

    with pytest.raises(SelfUpdateError) as exc_info:
        check_app_update(session=session)

    assert expected_fragment in str(exc_info.value)


def test_parse_sha256sums_formats() -> None:
    digest_a = "A" * 64
    digest_b = "b" * 64
    text = (
        "\ufeff"
        "# generated by build-release.ps1\n"
        "\n"
        f"{digest_a} *anqicms站群控制台.exe\r\n"
        f"{digest_b}  other-tool.zip\n"
        f"{'c' * 64} *./dist/nested.exe\n"
    )

    assert parse_sha256sums(text, "anqicms站群控制台.exe") == digest_a.lower()
    assert parse_sha256sums(text, "other-tool.zip") == digest_b
    assert parse_sha256sums(text, "nested.exe") == "c" * 64
    assert parse_sha256sums(text, "missing.exe") == ""
    assert parse_sha256sums("", "anqicms站群控制台.exe") == ""


def test_download_and_verify_update_streams_and_passes(tmp_path: Path) -> None:
    payload_chunks = [b"MZ" + b"0" * 65534, b"1" * 1024]
    full_bytes = b"".join(payload_chunks)
    expected_sha256 = hashlib.sha256(full_bytes).hexdigest()

    info = AppUpdateInfo(
        current_version="2.0.0",
        latest_version="2.1.0",
        update_available=True,
        repo=GITHUB_REPO,
        release_url="https://github.com/zaomeng/anqicms-manager/releases/tag/v2.1.0",
        published_at="2026-11-25T12:00:00Z",
        release_notes="",
        asset_name=APP_RELEASE_ASSET_NAME,
        download_url="https://example.invalid/download/anqicms.exe",
        asset_size=len(full_bytes),
        checksum_url="https://example.invalid/download/SHA256SUMS-web.txt",
        is_frozen=True,
        message="发现新版本",
    )

    def handler(url: str, kw: dict[str, Any]) -> FakeResponse:
        if url == info.download_url:
            assert kw.get("stream") is True
            return FakeResponse(
                status_code=200,
                headers={"Content-Length": str(len(full_bytes))},
                chunks=payload_chunks,
            )
        if url == info.checksum_url:
            return FakeResponse(
                status_code=200,
                text=f"{expected_sha256} *{APP_RELEASE_ASSET_NAME}\n",
            )
        raise AssertionError(f"unexpected url: {url}")

    progress_events: list[tuple[int, int, str]] = []
    session = FakeSession(handler)

    result_path = download_and_verify_update(
        info,
        tmp_path,
        session=session,
        progress=lambda done, total, msg: progress_events.append((done, total, msg)),
    )

    assert result_path == tmp_path / f"{APP_RELEASE_ASSET_NAME}.new"
    assert result_path.is_file()
    assert result_path.read_bytes() == full_bytes
    assert progress_events[0][0] == 0
    assert progress_events[0][1] == len(full_bytes)
    assert any(done == len(full_bytes) for done, _, _ in progress_events)


def test_download_and_verify_update_mismatch_deletes_file(tmp_path: Path) -> None:
    payload = b"corrupted-binary-payload"
    wrong_hash = "f" * 64

    info = AppUpdateInfo(
        current_version="2.0.0",
        latest_version="2.1.0",
        update_available=True,
        repo=GITHUB_REPO,
        release_url="",
        published_at="",
        release_notes="",
        asset_name=APP_RELEASE_ASSET_NAME,
        download_url="https://example.invalid/download/app.exe",
        asset_size=len(payload),
        checksum_url="https://example.invalid/download/SHA256SUMS-web.txt",
        is_frozen=True,
        message="",
    )

    session = FakeSession(
        lambda url, kw: (
            FakeResponse(status_code=200, chunks=[payload])
            if url == info.download_url
            else FakeResponse(status_code=200, text=f"{wrong_hash} *{APP_RELEASE_ASSET_NAME}\n")
        )
    )

    with pytest.raises(SelfUpdateError, match="安装包 SHA256 校验失败"):
        download_and_verify_update(info, tmp_path, session=session)

    assert not (tmp_path / f"{APP_RELEASE_ASSET_NAME}.new").exists()


def test_download_and_verify_update_cancellation_cleans_partial_file(tmp_path: Path) -> None:
    cancel_event = threading.Event()
    info = AppUpdateInfo(
        current_version="2.0.0",
        latest_version="2.1.0",
        update_available=True,
        repo=GITHUB_REPO,
        release_url="",
        published_at="",
        release_notes="",
        asset_name=APP_RELEASE_ASSET_NAME,
        download_url="https://example.invalid/download/app.exe",
        asset_size=100,
        checksum_url="",
        is_frozen=True,
        message="",
    )

    def on_progress(done: int, total: int, msg: str) -> None:
        if done > 0:
            cancel_event.set()

    session = FakeSession(
        lambda url, kw: FakeResponse(status_code=200, chunks=[b"part1", b"part2"])
    )

    with pytest.raises(SelfUpdateError, match="取消"):
        download_and_verify_update(
            info,
            tmp_path,
            session=session,
            progress=on_progress,
            cancel_event=cancel_event,
        )

    assert not (tmp_path / f"{APP_RELEASE_ASSET_NAME}.new").exists()


def test_create_windows_replace_script_utf8_and_commands(tmp_path: Path) -> None:
    app_dir = tmp_path / "安装目录 with space"
    app_dir.mkdir()
    current_exe = app_dir / "anqicms站群控制台.exe"
    new_exe = app_dir / "anqicms站群控制台.exe.new"
    script_dir = tmp_path / "scripts"

    script_path = create_windows_replace_script(
        new_exe,
        current_exe,
        current_pid=43210,
        relaunch_args=("--port", "8765", "--data-dir", "C:\\data path"),
        script_dir=script_dir,
    )

    assert script_path.parent == script_dir
    assert script_path.suffix == ".cmd"
    raw = script_path.read_bytes()
    # Must be UTF-8 WITHOUT BOM so cmd.exe can parse line 1 cleanly
    assert not raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8")

    assert "@echo off" in text
    assert "chcp 65001 >nul" in text
    assert 'set "PID=43210"' in text
    assert str(new_exe) in text
    assert str(current_exe) in text
    assert f"{current_exe}.bak" in text
    assert 'tasklist /FI "PID eq %PID%"' in text
    assert 'find """%PID%"""' in text
    assert 'move /y "%CUR_EXE%" "%BAK_EXE%"' in text
    assert 'move /y "%NEW_EXE%" "%CUR_EXE%"' in text
    assert 'start "" "%CUR_EXE%" --port 8765 --data-dir "C:\\data path"' in text
    assert 'del /f /q "%~f0"' in text


def test_download_and_verify_update_missing_checksum_entry_deletes_file(tmp_path: Path) -> None:
    payload = b"valid-bytes"
    info = AppUpdateInfo(
        current_version="2.0.0",
        latest_version="2.1.0",
        update_available=True,
        repo=GITHUB_REPO,
        release_url="",
        published_at="",
        release_notes="",
        asset_name=APP_RELEASE_ASSET_NAME,
        download_url="https://example.invalid/download/app.exe",
        asset_size=len(payload),
        checksum_url="https://example.invalid/download/SHA256SUMS-web.txt",
        is_frozen=True,
        message="",
    )
    session = FakeSession(
        lambda url, kw: (
            FakeResponse(status_code=200, chunks=[payload])
            if url == info.download_url
            else FakeResponse(status_code=200, text=f"{'a' * 64} *unrelated.exe\n")
        )
    )

    with pytest.raises(SelfUpdateError, match="安装包 SHA256 校验失败"):
        download_and_verify_update(info, tmp_path, session=session)

    assert not (tmp_path / f"{APP_RELEASE_ASSET_NAME}.new").exists()


def test_download_and_verify_update_without_checksum_url_succeeds(tmp_path: Path) -> None:
    payload = b"installer-without-checksum"
    info = AppUpdateInfo(
        current_version="2.0.0",
        latest_version="2.1.0",
        update_available=True,
        repo=GITHUB_REPO,
        release_url="",
        published_at="",
        release_notes="",
        asset_name=APP_RELEASE_ASSET_NAME,
        download_url="https://example.invalid/download/app.exe",
        asset_size=len(payload),
        checksum_url="",
        is_frozen=False,
        message="",
    )
    session = FakeSession(lambda url, kw: FakeResponse(status_code=200, chunks=[payload]))

    out = download_and_verify_update(info, tmp_path, session=session)

    assert out == tmp_path / f"{APP_RELEASE_ASSET_NAME}.new"
    assert out.read_bytes() == payload


def test_download_and_verify_update_http_error_cleans_up(tmp_path: Path) -> None:
    info = AppUpdateInfo(
        current_version="2.0.0",
        latest_version="2.1.0",
        update_available=True,
        repo=GITHUB_REPO,
        release_url="",
        published_at="",
        release_notes="",
        asset_name=APP_RELEASE_ASSET_NAME,
        download_url="https://example.invalid/download/app.exe",
        asset_size=100,
        checksum_url="",
        is_frozen=True,
        message="",
    )
    session = FakeSession(lambda url, kw: FakeResponse(status_code=503, text="Unavailable"))

    with pytest.raises(SelfUpdateError, match="HTTP 503"):
        download_and_verify_update(info, tmp_path, session=session)

    assert not (tmp_path / f"{APP_RELEASE_ASSET_NAME}.new").exists()


def test_check_app_update_owns_and_closes_default_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[FakeSession] = []

    def fake_session_cls() -> FakeSession:
        sess = FakeSession(
            lambda url, kw: FakeResponse(
                status_code=200,
                json_data=_sample_release_payload(tag_name="v2.0.1"),
            )
        )
        created.append(sess)
        return sess

    monkeypatch.setattr(requests, "Session", fake_session_cls)

    info = check_app_update(current_version="2.0.0")

    assert info.update_available is True
    assert info.latest_version == "2.0.1"
    assert len(created) == 1
    assert created[0].closed is True


def test_launch_replace_script_uses_detached_popen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "update.cmd"
    script.write_text("@echo off\r\n", encoding="utf-8")
    captured: list[tuple[list[str], dict[str, Any]]] = []

    def fake_popen(cmd: list[str], **kwargs: Any) -> object:
        captured.append((cmd, kwargs))
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    launch_replace_script(script)

    assert len(captured) == 1
    cmd, kwargs = captured[0]
    assert cmd == ["cmd", "/c", str(script)]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["close_fds"] is True
    assert "creationflags" in kwargs
    assert kwargs["creationflags"] != 0
