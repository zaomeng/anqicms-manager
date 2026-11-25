"""Minimal loopback-only one-shot HTTP bridge for admin jump handoffs.

Used so that one-time login tickets, tokens or credentials never appear in
browser command-line arguments or desktop logs.
"""

from __future__ import annotations

import html
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

_SECURITY_HEADERS = (
    ("Cache-Control", "no-store"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Content-Type-Options", "nosniff"),
)

_EXPIRED_HTML = (
    "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
    "<title>链接已失效</title></head>"
    "<body><p>链接已失效，请返回管理工具重新发起跳转。</p></body></html>"
).encode("utf-8")


@dataclass(frozen=True, slots=True)
class _BridgeEntry:
    kind: str  # "redirect" | "autopost"
    target_url: str
    fields: tuple[tuple[str, str], ...]
    expires_at: float


class _LoopbackHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


def _validate_http_url(url: str) -> str:
    if not isinstance(url, str):
        raise ValueError("跳转目标链接无效")
    cleaned = url.strip()
    if not cleaned or any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise ValueError("跳转目标链接无效")
    parsed = urlsplit(cleaned)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or not parsed.netloc:
        raise ValueError("跳转目标链接必须是有效的 http/https 地址")
    return cleaned


def _url_origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme.lower()}://{parsed.netloc}"


def _render_autopost_html(
    action_url: str,
    fields: tuple[tuple[str, str], ...],
    script_nonce: str,
) -> bytes:
    escaped_action = html.escape(action_url, quote=True)
    inputs = "\n".join(
        f"<input type='hidden' name='{html.escape(name, quote=True)}' "
        f"value='{html.escape(value, quote=True)}'>"
        for name, value in fields
    )
    page = (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<title>正在跳转后台…</title></head><body>"
        f"<form id='jump-form' method='post' action='{escaped_action}'>"
        f"{inputs}"
        "<noscript><button type='submit'>点击继续登录后台</button></noscript>"
        "</form>"
        f"<script nonce='{script_nonce}'>"
        "document.getElementById('jump-form').submit();"
        "</script></body></html>"
    )
    return page.encode("utf-8")


class JumpBridge:
    """Loopback (``127.0.0.1``) single-use handoff server."""

    def __init__(
        self,
        *,
        ttl_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        idle_timeout_s: float = 300.0,
    ) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl_s 必须大于 0")
        if idle_timeout_s < 0:
            raise ValueError("idle_timeout_s 不能为负数")
        self._ttl_s = float(ttl_s)
        self._clock = clock
        self._idle_timeout_s = float(idle_timeout_s)
        self._lock = threading.Lock()
        self._entries: dict[str, _BridgeEntry] = {}
        self._server: _LoopbackHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._port: int = 0
        self._last_activity: float = 0.0
        self._idle_timer: threading.Timer | None = None

    @property
    def port(self) -> int:
        with self._lock:
            return self._port

    def start(self) -> None:
        """Start the loopback HTTP server if not already running."""
        with self._lock:
            self._start_locked()

    def register_redirect(self, target_url: str) -> str:
        """Register a single-use 302 redirect and return its loopback URL."""
        validated = _validate_http_url(target_url)
        return self._register("redirect", validated, ())

    def register_autopost(self, action_url: str, fields: Mapping[str, str]) -> str:
        """Register a single-use auto-submitting POST form and return its loopback URL."""
        validated = _validate_http_url(action_url)
        items = tuple((str(key), str(value)) for key, value in (fields or {}).items())
        return self._register("autopost", validated, items)

    def pending_count(self) -> int:
        """Number of unexpired, unconsumed entries currently held."""
        with self._lock:
            self._prune_locked(self._clock())
            return len(self._entries)

    def stop(self) -> None:
        """Stop the HTTP server and discard all pending entries."""
        with self._lock:
            self._cancel_idle_timer_locked()
            self._entries.clear()
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
            self._port = 0
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def __enter__(self) -> JumpBridge:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    # ------------------------------------------------------------------ internals

    def _register(
        self,
        kind: str,
        target_url: str,
        fields: tuple[tuple[str, str], ...],
    ) -> str:
        with self._lock:
            self._start_locked()
            now = self._clock()
            self._prune_locked(now)
            token = secrets.token_urlsafe(32)
            self._entries[token] = _BridgeEntry(
                kind=kind,
                target_url=target_url,
                fields=fields,
                expires_at=now + self._ttl_s,
            )
            self._last_activity = now
            self._schedule_idle_timer_locked()
            return f"http://127.0.0.1:{self._port}/j/{token}"

    def _start_locked(self) -> None:
        if self._server is not None:
            return
        handler_cls = self._build_handler_class()
        server = _LoopbackHTTPServer(("127.0.0.1", 0), handler_cls)
        port = int(server.server_address[1])
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name=f"jump-bridge-{port}",
            daemon=True,
        )
        self._server = server
        self._thread = thread
        self._port = port
        self._last_activity = self._clock()
        thread.start()
        self._schedule_idle_timer_locked()

    def _prune_locked(self, now: float) -> None:
        expired = [key for key, entry in self._entries.items() if now >= entry.expires_at]
        for key in expired:
            del self._entries[key]

    def _consume(self, token: str) -> _BridgeEntry | None:
        with self._lock:
            now = self._clock()
            entry = self._entries.pop(token, None)
            self._prune_locked(now)
            self._last_activity = now
            self._schedule_idle_timer_locked()
            if entry is None or now >= entry.expires_at:
                return None
            return entry

    def _cancel_idle_timer_locked(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _schedule_idle_timer_locked(self) -> None:
        self._cancel_idle_timer_locked()
        if self._idle_timeout_s <= 0 or self._server is None:
            return
        timer = threading.Timer(self._idle_timeout_s, self._maybe_idle_stop)
        timer.daemon = True
        timer.start()
        self._idle_timer = timer

    def _maybe_idle_stop(self) -> None:
        should_stop = False
        with self._lock:
            if self._server is None:
                return
            now = self._clock()
            self._prune_locked(now)
            if not self._entries and (now - self._last_activity) >= self._idle_timeout_s:
                should_stop = True
            else:
                self._schedule_idle_timer_locked()
        if should_stop:
            self.stop()

    def _build_handler_class(self) -> type[BaseHTTPRequestHandler]:
        bridge = self

        class _BridgeHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args) -> None:  # noqa: A002
                # Silent: never write one-time URLs or tokens to logs/stderr.
                return

            def _write_response(
                self,
                status: int,
                body: bytes,
                *,
                content_type: str = "text/plain; charset=utf-8",
                extra_headers: tuple[tuple[str, str], ...] = (),
            ) -> None:
                self.send_response(status)
                for key, value in _SECURITY_HEADERS:
                    self.send_header(key, value)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                for key, value in extra_headers:
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                expected_host = f"127.0.0.1:{bridge.port}"
                actual_host = (self.headers.get("Host") or "").strip()
                if actual_host != expected_host:
                    self._write_response(403, b"Forbidden")
                    return

                path = urlsplit(self.path).path
                if not path.startswith("/j/"):
                    self._write_response(
                        404,
                        _EXPIRED_HTML,
                        content_type="text/html; charset=utf-8",
                    )
                    return

                token = path[3:]
                if not token or "/" in token:
                    self._write_response(
                        404,
                        _EXPIRED_HTML,
                        content_type="text/html; charset=utf-8",
                    )
                    return

                entry = bridge._consume(token)
                if entry is None:
                    self._write_response(
                        404,
                        _EXPIRED_HTML,
                        content_type="text/html; charset=utf-8",
                    )
                    return

                if entry.kind == "redirect":
                    self._write_response(
                        302,
                        b"Redirecting...",
                        extra_headers=(("Location", entry.target_url),),
                    )
                    return

                origin = _url_origin(entry.target_url)
                script_nonce = secrets.token_urlsafe(16)
                csp = (
                    f"default-src 'none'; form-action {origin}; "
                    f"script-src 'nonce-{script_nonce}'"
                )
                body = _render_autopost_html(entry.target_url, entry.fields, script_nonce)
                self._write_response(
                    200,
                    body,
                    content_type="text/html; charset=utf-8",
                    extra_headers=(("Content-Security-Policy", csp),),
                )

        return _BridgeHandler
