from __future__ import annotations

import http.client
import re
import time
from urllib.parse import urlsplit

import pytest

from core.jump_bridge import JumpBridge


def _raw_get(
    port: int,
    path: str,
    *,
    host: str | None = None,
    method: str = "GET",
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3.0)
    try:
        headers = {"Host": host if host is not None else f"127.0.0.1:{port}"}
        conn.putrequest(method, path, skip_host=True)
        for key, value in headers.items():
            conn.putheader(key, value)
        conn.endheaders()
        resp = conn.getresponse()
        body = resp.read()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, resp_headers, body
    finally:
        conn.close()


def test_bridge_binds_loopback_and_single_use_redirect():
    with JumpBridge(ttl_s=60.0) as bridge:
        assert bridge.port > 0
        url = bridge.register_redirect("https://site.example.com/system/?ticket=secret-123")
        parsed = urlsplit(url)
        assert parsed.scheme == "http"
        assert parsed.hostname == "127.0.0.1"
        assert parsed.port == bridge.port
        assert parsed.path.startswith("/j/")
        assert "secret-123" not in url
        assert bridge.pending_count() == 1

        status, headers, _ = _raw_get(bridge.port, parsed.path)
        assert status == 302
        assert headers["location"] == "https://site.example.com/system/?ticket=secret-123"
        assert headers["cache-control"] == "no-store"
        assert headers["referrer-policy"] == "no-referrer"
        assert headers["x-content-type-options"] == "nosniff"
        assert bridge.pending_count() == 0

        # Second request on the same single-use path returns 404.
        status2, headers2, body2 = _raw_get(bridge.port, parsed.path)
        assert status2 == 404
        assert "链接已失效" in body2.decode("utf-8")
        assert headers2["cache-control"] == "no-store"

    assert bridge.port == 0


def test_bridge_ttl_expiry_with_injected_clock():
    now = [1000.0]
    with JumpBridge(ttl_s=30.0, clock=lambda: now[0], idle_timeout_s=0) as bridge:
        url = bridge.register_redirect("https://site.example.com/system/")
        path = urlsplit(url).path
        assert bridge.pending_count() == 1

        now[0] = 1029.9
        assert bridge.pending_count() == 1

        now[0] = 1030.0
        assert bridge.pending_count() == 0
        status, _, body = _raw_get(bridge.port, path)
        assert status == 404
        assert "链接已失效" in body.decode("utf-8")


def test_bridge_rejects_bad_host_header_without_consuming_token():
    with JumpBridge(ttl_s=60.0) as bridge:
        url = bridge.register_redirect("https://site.example.com/system/")
        path = urlsplit(url).path

        for bad_host in ("evil.example.com", f"localhost:{bridge.port}", "127.0.0.1:1"):
            status, headers, _ = _raw_get(bridge.port, path, host=bad_host)
            assert status == 403
            assert headers["cache-control"] == "no-store"

        # Token must still be usable with the real loopback Host header.
        assert bridge.pending_count() == 1
        status_ok, headers_ok, _ = _raw_get(bridge.port, path)
        assert status_ok == 302
        assert headers_ok["location"] == "https://site.example.com/system/"


def test_bridge_rejects_unknown_paths_and_non_get():
    with JumpBridge(ttl_s=60.0) as bridge:
        for path in ("/", "/j/", "/j/unknown", "/j/a/b", "/other"):
            status, _, body = _raw_get(bridge.port, path)
            assert status == 404
            assert "链接已失效" in body.decode("utf-8")

        status_post, _, _ = _raw_get(bridge.port, "/j/anything", method="POST")
        assert status_post == 501


def test_bridge_autopost_escaping_and_csp():
    action = "https://site.example.com:8443/system/api/login?a=1&b=2"
    fields = {
        "user_name": 'admin" onload="alert(1)',
        "password": "<script>evil()</script>&'pass'",
    }
    with JumpBridge(ttl_s=60.0) as bridge:
        url = bridge.register_autopost(action, fields)
        path = urlsplit(url).path

        status, headers, body_bytes = _raw_get(bridge.port, path)
        assert status == 200
        assert "text/html" in headers["content-type"]
        assert headers["cache-control"] == "no-store"
        assert headers["referrer-policy"] == "no-referrer"
        assert headers["x-content-type-options"] == "nosniff"

        csp = headers["content-security-policy"]
        assert "default-src 'none'" in csp
        assert "form-action https://site.example.com:8443" in csp
        nonce_match = re.search(r"script-src 'nonce-([^']+)'", csp)
        assert nonce_match is not None
        nonce = nonce_match.group(1)

        html_text = body_bytes.decode("utf-8")
        assert f"<script nonce='{nonce}'>" in html_text
        assert "action='https://site.example.com:8443/system/api/login?a=1&amp;b=2'" in html_text
        assert "<script>evil()</script>" not in html_text
        assert "&lt;script&gt;evil()&lt;/script&gt;&amp;&#x27;pass&#x27;" in html_text
        assert 'admin&quot; onload=&quot;alert(1)' in html_text


def test_bridge_validates_urls_and_constructor_args():
    with pytest.raises(ValueError):
        JumpBridge(ttl_s=0)
    with pytest.raises(ValueError):
        JumpBridge(idle_timeout_s=-1)

    with JumpBridge() as bridge:
        for bad_url in (
            "",
            "javascript:alert(1)",
            "file:///etc/passwd",
            "https://site.example.com/a\r\nX-Injected: 1",
            "/relative/path",
        ):
            with pytest.raises(ValueError):
                bridge.register_redirect(bad_url)
            with pytest.raises(ValueError):
                bridge.register_autopost(bad_url, {"k": "v"})


def test_bridge_silent_logging_never_prints_token(capsys):
    with JumpBridge() as bridge:
        url = bridge.register_redirect("https://site.example.com/system/")
        path = urlsplit(url).path
        status, _, _ = _raw_get(bridge.port, path)
        assert status == 302
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_bridge_auto_stops_when_idle():
    bridge = JumpBridge(ttl_s=60.0, idle_timeout_s=0.05)
    try:
        bridge.start()
        assert bridge.port > 0
        deadline = time.monotonic() + 2.0
        while bridge.port != 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert bridge.port == 0
    finally:
        bridge.stop()
