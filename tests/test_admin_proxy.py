from __future__ import annotations

import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from core.admin_jump_service import AdminJumpService
from core.admin_proxy import AdminProxyServer
from core.integration_settings import AdminJumpSettings, IntegrationSettings
from models.sitegroup import JUMP_TIER_BRIDGE


class _MockUpstreamState:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.lock = threading.Lock()


def _start_mock_upstream():
    state = _MockUpstreamState()

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

        def _handle(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length > 0 else b""
            with state.lock:
                state.requests.append(
                    {
                        "method": method,
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body.decode("utf-8", errors="replace"),
                    }
                )

            path_only = urlsplit(self.path).path
            if path_only in {"/system/", "/manage/"}:
                payload = (
                    "<!doctype html><html><head><title>AnQiCMS Admin</title></head>"
                    "<body><div id='root'>Admin SPA</div></body></html>"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Security-Policy", "script-src 'self'")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if method != "HEAD":
                    self.wfile.write(payload)
                return

            if path_only == "/article/1.html":
                payload = b"<html><head><title>Article</title></head><body>Post</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            if path_only == "/system/api/login" and method == "POST":
                data = json.dumps({"code": 0, "data": {"token": "jwt-token-xyz"}}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", "anqi_sess=cookie-val-1; Path=/; HttpOnly")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            if path_only == "/system/api/echo":
                data = json.dumps(
                    {
                        "method": method,
                        "authorization": self.headers.get("Authorization", ""),
                        "cookie": self.headers.get("Cookie", ""),
                        "origin": self.headers.get("Origin", ""),
                        "referer": self.headers.get("Referer", ""),
                        "body": body.decode("utf-8", errors="replace"),
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            if path_only == "/system/redirect-local":
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://127.0.0.1:{self.server.server_address[1]}/system/?from=redir",
                )
                self.send_header(
                    "Set-Cookie",
                    "sid=abc123; Path=/; Domain=upstream.example; Secure; HttpOnly",
                )
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if path_only == "/system/redirect-external":
                self.send_response(302)
                self.send_header("Location", "https://external.example.org/help")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):  # noqa: N802
            self._handle("GET")

        def do_POST(self):  # noqa: N802
            self._handle("POST")

        def do_PUT(self):  # noqa: N802
            self._handle("PUT")

        def do_DELETE(self):  # noqa: N802
            self._handle("DELETE")

        def do_PATCH(self):  # noqa: N802
            self._handle("PATCH")

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, state


def _proxy_request(
    port: int,
    method: str,
    path: str,
    *,
    host: str,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], list[str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
    try:
        req_headers = {"Host": host}
        if headers:
            req_headers.update(headers)
        conn.putrequest(method, path, skip_host=True)
        for key, value in req_headers.items():
            conn.putheader(key, value)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        resp = conn.getresponse()
        raw = resp.read()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        set_cookies = resp.headers.get_all("Set-Cookie") or []
        return resp.status, resp_headers, set_cookies, raw
    finally:
        conn.close()


def test_admin_proxy_lifecycle_slugs_and_html_injection():
    upstream, thread, state = _start_mock_upstream()
    upstream_port = upstream.server_address[1]
    try:
        site = SimpleNamespace(
            name="中文站点_01",
            url=f"http://127.0.0.1:{upstream_port}",
            admin_path="/manage/",
        )
        with AdminProxyServer() as proxy:
            assert proxy.is_running is True
            assert proxy.port > 0

            proxy_url = proxy.register_site(
                site,
                auth_token='tok"</script>123',
                cookies={"session_id": "s-999"},
                token_storage_keys=("token", "anqicms_token"),
            )
            parsed = urlsplit(proxy_url)
            assert parsed.scheme == "http"
            assert parsed.hostname is not None
            assert parsed.hostname.endswith(".localhost")
            assert parsed.port == proxy.port
            assert parsed.path == "/manage/"
            slug = parsed.hostname[: -len(".localhost")]
            assert slug.replace("-", "").isalnum()

            host_header = parsed.netloc
            status, headers, _, raw = _proxy_request(proxy.port, "GET", "/manage/", host=host_header)
            assert status == 200
            assert "content-security-policy" not in headers
            html_text = raw.decode("utf-8")
            assert "window.localStorage.setItem" in html_text
            assert '"anqicms_token"' in html_text
            assert "</script>123" not in html_text  # safely escaped as <\/script>
            assert "<\\/script>123" in html_text
            assert html_text.index("<script>") < html_text.index("</head>")

            # Non-admin HTML page must NOT have token script injected.
            status_art, _, _, raw_art = _proxy_request(
                proxy.port, "GET", "/article/1.html", host=host_header
            )
            assert status_art == 200
            assert b"localStorage" not in raw_art

            # Upstream received Bearer token + injected cookie.
            assert state.requests[0]["headers"]["authorization"] == 'Bearer tok"</script>123'
            assert "session_id=s-999" in state.requests[0]["headers"]["cookie"]

        assert proxy.is_running is False
        assert proxy.port == 0
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2.0)


def test_admin_proxy_forwarding_methods_redirects_cookies_and_security():
    upstream, thread, _ = _start_mock_upstream()
    upstream_port = upstream.server_address[1]
    try:
        site = SimpleNamespace(
            name="site-a",
            url=f"http://127.0.0.1:{upstream_port}",
            admin_path="/system/",
        )
        with AdminProxyServer() as proxy:
            proxy_url = proxy.register_site(
                site,
                auth_token="secret-bearer-token",
                cookies={"up": "val1"},
            )
            parsed = urlsplit(proxy_url)
            host_header = parsed.netloc
            slug = parsed.hostname[: -len(".localhost")]

            # Test GET/POST/PUT/DELETE/PATCH forwarding + Origin/Referer/Cookie merging
            for method in ("POST", "PUT", "DELETE", "PATCH"):
                payload = json.dumps({"action": method}).encode("utf-8")
                status, _, _, raw = _proxy_request(
                    proxy.port,
                    method,
                    "/system/api/echo",
                    host=host_header,
                    headers={
                        "Content-Type": "application/json",
                        "Cookie": "browser=b1",
                        "Origin": f"http://{host_header}",
                        "Referer": f"http://{host_header}/system/dashboard",
                    },
                    body=payload,
                )
                assert status == 200
                echo = json.loads(raw.decode("utf-8"))
                assert echo["method"] == method
                assert echo["authorization"] == "Bearer secret-bearer-token"
                assert "up=val1" in echo["cookie"]
                assert "browser=b1" in echo["cookie"]
                assert echo["origin"] == f"http://127.0.0.1:{upstream_port}"
                assert echo["referer"] == f"http://127.0.0.1:{upstream_port}/system/dashboard"
                assert json.loads(echo["body"]) == {"action": method}

            # Same-origin redirect rewritten back to proxy origin, Set-Cookie cleaned.
            status_r, headers_r, set_cookies, _ = _proxy_request(
                proxy.port, "GET", "/system/redirect-local", host=host_header
            )
            assert status_r == 302
            assert headers_r["location"] == f"http://{host_header}/system/?from=redir"
            assert len(set_cookies) == 1
            assert "Domain=" not in set_cookies[0]
            assert "Secure" not in set_cookies[0]
            assert "sid=abc123" in set_cookies[0]

            # External redirect preserved untouched.
            status_ext, headers_ext, _, _ = _proxy_request(
                proxy.port, "GET", "/system/redirect-external", host=host_header
            )
            assert status_ext == 302
            assert headers_ext["location"] == "https://external.example.org/help"

            # Fallback /__site/<slug>/... routing + redirect rewrite.
            fb_url = proxy.fallback_url(slug)
            fb_parsed = urlsplit(fb_url)
            status_fb, _, _, raw_fb = _proxy_request(
                proxy.port, "GET", fb_parsed.path, host=f"127.0.0.1:{proxy.port}"
            )
            assert status_fb == 200
            assert b"window.localStorage.setItem" in raw_fb

            status_fb_redir, headers_fb_redir, _, _ = _proxy_request(
                proxy.port,
                "GET",
                f"/__site/{slug}/system/redirect-local",
                host=f"127.0.0.1:{proxy.port}",
            )
            assert status_fb_redir == 302
            assert headers_fb_redir["location"] == (
                f"http://127.0.0.1:{proxy.port}/__site/{slug}/system/?from=redir"
            )

            # Security: reject external Host (403) and unknown slug (404).
            s403, _, _, _ = _proxy_request(proxy.port, "GET", "/system/", host="evil.example.com")
            assert s403 == 403
            s404, _, _, _ = _proxy_request(
                proxy.port, "GET", "/system/", host=f"missing.localhost:{proxy.port}"
            )
            assert s404 == 404
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2.0)


def test_admin_jump_service_with_external_and_owned_local_proxy():
    upstream, thread, _ = _start_mock_upstream()
    upstream_port = upstream.server_address[1]
    try:
        site = SimpleNamespace(
            name="site-jump",
            url=f"http://127.0.0.1:{upstream_port}",
            jiekou="",
            mcp_token="",
            admin_username="admin",
            admin_password="secret-password",
            admin_path="/system/",
        )
        settings = IntegrationSettings(
            admin_jump=AdminJumpSettings(enable_ticket=False)
        )

        # External proxy stays alive after AdminJumpService exits.
        with AdminProxyServer() as external_proxy:
            opened: list[str] = []
            with AdminJumpService(
                settings=settings,
                proxy=external_proxy,
                opener=lambda url: (opened.append(url), True)[1],
            ) as svc:
                res = svc.open(site)
                assert res.success is True
                assert res.tier == JUMP_TIER_BRIDGE
                assert res.message == "已通过本地代理注入后台登录态"
                assert res.display_url == f"http://127.0.0.1:{upstream_port}/system/"
                assert "jwt-token-xyz" not in opened[0]
                assert "secret-password" not in opened[0]

            assert external_proxy.is_running is True
            parsed = urlsplit(opened[0])
            status, _, _, raw = _proxy_request(
                external_proxy.port, "GET", parsed.path, host=parsed.netloc
            )
            assert status == 200
            assert b"jwt-token-xyz" in raw

            # Verify upstream also receives the session cookie captured during login!
            status_api, _, _, raw_api = _proxy_request(
                external_proxy.port, "GET", "/system/api/echo", host=parsed.netloc
            )
            assert status_api == 200
            echo = json.loads(raw_api.decode("utf-8"))
            assert echo["authorization"] == "Bearer jwt-token-xyz"
            assert "anqi_sess=cookie-val-1" in echo["cookie"]

        # Owned local proxy (use_local_proxy=True) stops cleanly on shutdown.
        opened2: list[str] = []
        with AdminJumpService(
            settings=settings,
            use_local_proxy=True,
            opener=lambda url: (opened2.append(url), True)[1],
        ) as svc2:
            res2 = svc2.open(site)
            assert res2.success is True
            assert res2.tier == JUMP_TIER_BRIDGE
            assert res2.message == "已通过本地代理注入后台登录态"
            assert svc2.proxy is not None
            assert svc2.proxy.is_running is True
            assert urlsplit(opened2[0]).hostname.endswith(".localhost")
        assert svc2.proxy.is_running is False
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2.0)


def test_admin_proxy_upstream_error_returns_502_without_leaking_token():
    # Point to an unbound port to trigger connection failure.
    with AdminProxyServer(request_timeout=1.0) as proxy:
        site = SimpleNamespace(name="down", url="http://127.0.0.1:1", admin_path="/system/")
        url = proxy.register_site(site, auth_token="super-secret-token-999")
        parsed = urlsplit(url)
        status, _, _, raw = _proxy_request(proxy.port, "GET", "/system/", host=parsed.netloc)
        assert status == 502
        text = raw.decode("utf-8")
        assert "super-secret-token-999" not in text
