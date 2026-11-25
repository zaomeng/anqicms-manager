from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any, Callable

import pytest
import requests

from core.anqicms_mcp_client import AnqiMcpClient, MAX_TOOL_PAGES, probe_site
from core.integration_settings import IntegrationSettings, McpSettings
from core.mcp_contracts import (
    AUTH_HINT,
    REDIRECT_HINT,
    SCOPE_HINT,
    SSL_HINT,
    McpAuthError,
    McpClientLike,
    McpProtocolError,
    McpScopeError,
    McpToolError,
    McpUnavailableError,
)


class _FakeMcpServer:
    """In-process 127.0.0.1 HTTP server for hermetic MCP client tests."""

    def __init__(self, handler: Callable[[str, str, dict[str, str], bytes], tuple[int, dict[str, str], bytes]]) -> None:
        self.handler = handler
        self.requests_log: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        parent = self

        class _RequestHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                return

            def _dispatch(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length > 0 else b""
                headers = {k.lower(): v for k, v in self.headers.items()}
                with parent._lock:
                    parent.requests_log.append(
                        {
                            "method": method,
                            "path": self.path,
                            "headers": headers,
                            "body": body,
                        }
                    )
                status, resp_headers, resp_body = parent.handler(method, self.path, headers, body)
                self.send_response(status)
                has_length = any(k.lower() == "content-length" for k in resp_headers)
                for key, value in resp_headers.items():
                    self.send_header(key, value)
                if not has_length:
                    self.send_header("Content-Length", str(len(resp_body)))
                self.send_header("Connection", "close")
                self.end_headers()
                if resp_body:
                    self.wfile.write(resp_body)
                self.close_connection = True

            def do_POST(self) -> None:
                self._dispatch("POST")

            def do_DELETE(self) -> None:
                self._dispatch("DELETE")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _RequestHandler)
        self.base_url = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )

    def __enter__(self) -> "_FakeMcpServer":
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)


def _json_resp(payload: Any, *, status: int = 200, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    merged = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        merged.update(headers)
    return status, merged, body


def _sse_resp(events: list[str], *, status: int = 200, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    body = "".join(events).encode("utf-8")
    merged = {"Content-Type": "text/event-stream"}
    if headers:
        merged.update(headers)
    return status, merged, body


def _make_site(
    *,
    name: str = "demo",
    url: str = "http://127.0.0.1:9999",
    jiekou: str = "",
    mcp_token: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(name=name, url=url, jiekou=jiekou, mcp_token=mcp_token)


def test_for_site_token_resolution_and_protocol_conformance() -> None:
    site_with_mcp = _make_site(
        url="https://example.com/",
        jiekou="https://example.com/api/import?token=import_tok_1234",
        mcp_token="mcp_tok_9876",
    )
    client = AnqiMcpClient.for_site(site_with_mcp)
    try:
        assert isinstance(client, McpClientLike)
        assert client.endpoint_url == "https://example.com/api/mcp"
        assert "mcp_tok_9876" not in repr(client)
    finally:
        client.close()

    site_fallback = _make_site(
        url="https://example.com",
        jiekou="https://example.com/api/import?token=fallback_tok_5555",
        mcp_token="",
    )
    client2 = AnqiMcpClient.for_site(site_fallback)
    try:
        assert "fallback_tok_5555" not in repr(client2)
    finally:
        client2.close()

    site_no_token = _make_site(url="https://example.com", jiekou="https://example.com/api/import")
    with pytest.raises(McpAuthError) as exc_info:
        AnqiMcpClient.for_site(site_no_token)
    assert exc_info.value.hint == AUTH_HINT

    site_no_url = _make_site(url="", mcp_token="secret_tok_1234")
    with pytest.raises(McpUnavailableError):
        AnqiMcpClient.for_site(site_no_url)


def test_initialize_session_headers_idempotency_ping_and_close_delete() -> None:
    def handler(method: str, path: str, headers: dict[str, str], body: bytes):
        if method == "DELETE":
            assert headers.get("mcp-session-id") == "sess-abc-1"
            assert headers.get("authorization") == "Bearer secret_token_1234"
            return 204, {}, b""
        data = json.loads(body.decode("utf-8"))
        rpc_method = data["method"]
        if rpc_method == "initialize":
            return _json_resp(
                {
                    "jsonrpc": "2.0",
                    "id": data["id"],
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "anqicms-mcp", "version": "3.6.7"},
                    },
                },
                headers={"Mcp-Session-Id": "sess-abc-1"},
            )
        if rpc_method == "notifications/initialized":
            assert "id" not in data
            assert headers.get("mcp-session-id") == "sess-abc-1"
            assert headers.get("mcp-protocol-version") == "2025-06-18"
            return 202, {}, b""
        if rpc_method == "ping":
            assert headers.get("mcp-session-id") == "sess-abc-1"
            assert headers.get("mcp-protocol-version") == "2025-06-18"
            return _json_resp({"jsonrpc": "2.0", "id": data["id"], "result": {}})
        raise AssertionError(f"Unexpected method: {rpc_method}")

    with _FakeMcpServer(handler) as srv:
        with AnqiMcpClient(srv.base_url, "secret_token_1234") as client:
            res1 = client.initialize()
            res2 = client.initialize()
            assert res1 == res2
            assert client.session_id == "sess-abc-1"
            assert client.protocol_version == "2025-06-18"
            assert client.server_info == {"name": "anqicms-mcp", "version": "3.6.7"}
            assert client.ping() is True

        methods = [
            (r["method"], json.loads(r["body"].decode("utf-8"))["method"] if r["body"] else None)
            for r in srv.requests_log
        ]
        assert methods == [
            ("POST", "initialize"),
            ("POST", "notifications/initialized"),
            ("POST", "ping"),
            ("DELETE", None),
        ]


def test_initialize_protocol_version_fallback_on_rpc_error_and_http_400() -> None:
    attempted_versions: list[str] = []

    def handler(method: str, path: str, headers: dict[str, str], body: bytes):
        data = json.loads(body.decode("utf-8"))
        if data["method"] == "initialize":
            ver = data["params"]["protocolVersion"]
            attempted_versions.append(ver)
            if ver == "2025-06-18":
                return _json_resp(
                    {"jsonrpc": "2.0", "id": data["id"], "error": {"code": -32602, "message": "unsupported version"}}
                )
            if ver == "2025-03-26":
                return 400, {"Content-Type": "text/plain"}, b"bad protocolVersion"
            return _json_resp(
                {
                    "jsonrpc": "2.0",
                    "id": data["id"],
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {"name": "legacy-anqi", "version": "3.6.0"},
                    },
                }
            )
        if data["method"] == "notifications/initialized":
            assert headers.get("mcp-protocol-version") == "2024-11-05"
            return 204, {}, b""
        raise AssertionError(data)

    with _FakeMcpServer(handler) as srv:
        with AnqiMcpClient(srv.base_url, "tok_fallback_9999") as client:
            client.initialize()
            assert attempted_versions == ["2025-06-18", "2025-03-26", "2024-11-05"]
            assert client.protocol_version == "2024-11-05"


def test_sse_response_ignores_notifications_server_requests_and_decodes_utf8() -> None:
    def handler(method: str, path: str, headers: dict[str, str], body: bytes):
        if method == "DELETE":
            return 204, {}, b""
        data = json.loads(body.decode("utf-8"))
        rpc_method = data["method"]
        if rpc_method == "initialize":
            reply = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": data["id"],
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "serverInfo": {"name": "安企MCP", "version": "3.6.7"},
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
            multiline_event = "event: message\n" + "".join(f"data: {line}\n" for line in reply.splitlines()) + "\n"
            return _sse_resp(
                [
                    ": keep-alive comment\n\n",
                    'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress","params":{"p":1}}\n\n',
                    'data: {"jsonrpc":"2.0","id":999,"method":"roots/list","params":{}}\n\n',
                    multiline_event,
                ],
                headers={"Mcp-Session-Id": "sse-sess"},
            )
        if rpc_method == "notifications/initialized":
            return 200, {}, b""
        if rpc_method == "tools/call":
            reply = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": data["id"],
                    "result": {
                        "isError": False,
                        "content": [
                            {"type": "text", "text": '{"current_version": "3.6.7", "note": "中文正常"}'},
                            {"type": "image", "data": "ignored"},
                        ],
                    },
                },
                ensure_ascii=False,
            )
            return _sse_resp([f"data: {reply}\n\n"])
        raise AssertionError(rpc_method)

    with _FakeMcpServer(handler) as srv:
        with AnqiMcpClient(srv.base_url, "tok_sse_1234") as client:
            client.initialize()
            assert client.server_info["name"] == "安企MCP"
            res = client.call_tool("check_update", {"force": False})
            assert res.is_error is False
            assert res.structured == {"current_version": "3.6.7", "note": "中文正常"}
            assert "中文正常" in res.text


def test_list_tools_pagination_caching_and_find_tools() -> None:
    list_calls = 0

    def handler(method: str, path: str, headers: dict[str, str], body: bytes):
        nonlocal list_calls
        data = json.loads(body.decode("utf-8"))
        rpc_method = data["method"]
        if rpc_method == "initialize":
            return _json_resp(
                {"jsonrpc": "2.0", "id": data["id"], "result": {"protocolVersion": "2025-06-18"}}
            )
        if rpc_method == "notifications/initialized":
            return 202, {}, b""
        if rpc_method == "tools/list":
            list_calls += 1
            cursor = (data.get("params") or {}).get("cursor")
            if not cursor:
                return _json_resp(
                    {
                        "jsonrpc": "2.0",
                        "id": data["id"],
                        "result": {
                            "tools": [
                                {"name": "friendlink_list", "description": "列出友情链接", "inputSchema": {"type": "object"}},
                                {"name": "system_upgrade", "description": "升级系统版本"},
                            ],
                            "nextCursor": "page-2",
                        },
                    }
                )
            if cursor == "page-2":
                return _json_resp(
                    {
                        "jsonrpc": "2.0",
                        "id": data["id"],
                        "result": {
                            "tools": [
                                {"name": "admin_login_ticket", "description": "生成后台登录跳转票据"},
                            ],
                        },
                    }
                )
        raise AssertionError(data)

    with _FakeMcpServer(handler) as srv:
        with AnqiMcpClient(srv.base_url, "tok_page_1234") as client:
            tools = client.list_tools()
            assert [t.name for t in tools] == ["friendlink_list", "system_upgrade", "admin_login_ticket"]
            assert list_calls == 2

            # Cached on second call
            tools_cached = client.list_tools()
            assert len(tools_cached) == 3
            assert list_calls == 2

            # Refreshed when requested
            client.list_tools(refresh=True)
            assert list_calls == 4

            found = client.find_tools(["upgrade", "升级"], explicit_names=["admin_login_ticket"])
            assert [t.name for t in found] == ["admin_login_ticket", "system_upgrade"]


def test_list_tools_caps_pagination_at_max_pages() -> None:
    page_counter = 0

    def handler(method: str, path: str, headers: dict[str, str], body: bytes):
        nonlocal page_counter
        data = json.loads(body.decode("utf-8"))
        if data["method"] == "initialize":
            return _json_resp({"jsonrpc": "2.0", "id": data["id"], "result": {"protocolVersion": "2025-06-18"}})
        if data["method"] == "notifications/initialized":
            return 202, {}, b""
        if data["method"] == "tools/list":
            page_counter += 1
            return _json_resp(
                {
                    "jsonrpc": "2.0",
                    "id": data["id"],
                    "result": {
                        "tools": [{"name": f"tool_{page_counter}"}],
                        "nextCursor": f"cursor_{page_counter}",
                    },
                }
            )
        raise AssertionError(data)

    with _FakeMcpServer(handler) as srv:
        with AnqiMcpClient(srv.base_url, "tok_cap_1234") as client:
            tools = client.list_tools()
            assert page_counter == MAX_TOOL_PAGES
            assert len(tools) == MAX_TOOL_PAGES


def test_call_tool_structured_content_and_error_mapping() -> None:
    secret = "super_secret_token_9999"

    def handler(method: str, path: str, headers: dict[str, str], body: bytes):
        data = json.loads(body.decode("utf-8"))
        if data["method"] == "initialize":
            return _json_resp({"jsonrpc": "2.0", "id": data["id"], "result": {"protocolVersion": "2025-06-18"}})
        if data["method"] == "notifications/initialized":
            return 202, {}, b""
        if data["method"] == "tools/call":
            name = data["params"]["name"]
            if name == "structured_ok":
                return _json_resp(
                    {
                        "jsonrpc": "2.0",
                        "id": data["id"],
                        "result": {
                            "isError": False,
                            "content": [{"type": "text", "text": "part1"}, {"type": "text", "text": "part2"}],
                            "structuredContent": {"version": "3.6.8"},
                        },
                    }
                )
            if name == "scope_denied":
                return _json_resp(
                    {
                        "jsonrpc": "2.0",
                        "id": data["id"],
                        "result": {
                            "isError": True,
                            "content": [{"type": "text", "text": f"系统升级未开放：请在开放范围开启 (token={secret})"}],
                        },
                    }
                )
            if name == "generic_tool_fail":
                return _json_resp(
                    {
                        "jsonrpc": "2.0",
                        "id": data["id"],
                        "result": {
                            "isError": True,
                            "content": [{"type": "text", "text": f"参数无效 Bearer {secret}"}],
                        },
                    }
                )
            if name == "rpc_scope_err":
                return _json_resp(
                    {
                        "jsonrpc": "2.0",
                        "id": data["id"],
                        "error": {"code": -32010, "message": f"permission denied for {secret}"},
                    }
                )
            if name == "rpc_generic_err":
                return _json_resp(
                    {
                        "jsonrpc": "2.0",
                        "id": data["id"],
                        "error": {"code": -32603, "message": "internal boom"},
                    }
                )
        raise AssertionError(data)

    with _FakeMcpServer(handler) as srv:
        with AnqiMcpClient(srv.base_url, secret) as client:
            ok_res = client.call_tool("structured_ok")
            assert ok_res.text == "part1\npart2"
            assert ok_res.structured == {"version": "3.6.8"}

            with pytest.raises(McpScopeError) as scope_exc:
                client.call_tool("scope_denied")
            assert scope_exc.value.hint == SCOPE_HINT
            assert scope_exc.value.result is not None
            assert scope_exc.value.result.is_error is True
            assert secret not in str(scope_exc.value)

            with pytest.raises(McpToolError) as tool_exc:
                client.call_tool("generic_tool_fail")
            assert not isinstance(tool_exc.value, McpScopeError)
            assert secret not in str(tool_exc.value)

            with pytest.raises(McpScopeError) as rpc_scope_exc:
                client.call_tool("rpc_scope_err")
            assert rpc_scope_exc.value.code == -32010
            assert secret not in str(rpc_scope_exc.value)

            with pytest.raises(McpToolError) as rpc_err_exc:
                client.call_tool("rpc_generic_err")
            assert not isinstance(rpc_err_exc.value, McpScopeError)
            assert rpc_err_exc.value.code == -32603


def test_session_expiry_404_reinitializes_once_and_retries() -> None:
    session_counter = 0
    call_attempts = 0

    def handler(method: str, path: str, headers: dict[str, str], body: bytes):
        nonlocal session_counter, call_attempts
        if method == "DELETE":
            return 204, {}, b""
        data = json.loads(body.decode("utf-8"))
        if data["method"] == "initialize":
            session_counter += 1
            return _json_resp(
                {"jsonrpc": "2.0", "id": data["id"], "result": {"protocolVersion": "2025-06-18"}},
                headers={"Mcp-Session-Id": f"sess-{session_counter}"},
            )
        if data["method"] == "notifications/initialized":
            return 202, {}, b""
        if data["method"] == "tools/call":
            call_attempts += 1
            if headers.get("mcp-session-id") == "sess-1":
                return 404, {"Content-Type": "text/plain"}, b"session expired"
            assert headers.get("mcp-session-id") == "sess-2"
            return _json_resp(
                {
                    "jsonrpc": "2.0",
                    "id": data["id"],
                    "result": {"isError": False, "content": [{"type": "text", "text": "recovered"}]},
                }
            )
        raise AssertionError(data)

    with _FakeMcpServer(handler) as srv:
        with AnqiMcpClient(srv.base_url, "tok_reinit_1234") as client:
            client.initialize()
            assert client.session_id == "sess-1"
            res = client.call_tool("do_work")
            assert res.text == "recovered"
            assert client.session_id == "sess-2"
            assert session_counter == 2
            assert call_attempts == 2


def test_session_expiry_404_retries_only_once_then_raises() -> None:
    session_counter = 0
    call_attempts = 0

    def handler(method: str, path: str, headers: dict[str, str], body: bytes):
        nonlocal session_counter, call_attempts
        if method == "DELETE":
            return 204, {}, b""
        data = json.loads(body.decode("utf-8"))
        if data["method"] == "initialize":
            session_counter += 1
            return _json_resp(
                {"jsonrpc": "2.0", "id": data["id"], "result": {"protocolVersion": "2025-06-18"}},
                headers={"Mcp-Session-Id": f"sess-{session_counter}"},
            )
        if data["method"] == "notifications/initialized":
            return 202, {}, b""
        if data["method"] == "tools/call":
            call_attempts += 1
            return 404, {"Content-Type": "text/plain"}, b"still missing"
        raise AssertionError(data)

    with _FakeMcpServer(handler) as srv:
        with AnqiMcpClient(srv.base_url, "tok_once_1234") as client:
            client.initialize()
            with pytest.raises(McpUnavailableError) as exc_info:
                client.call_tool("missing_forever")
            assert exc_info.value.http_status == 404
            assert session_counter == 2
            assert call_attempts == 2


@pytest.mark.parametrize(
    ("status", "headers", "body", "exc_cls", "expected_hint"),
    [
        (401, {"Content-Type": "text/plain"}, b"unauthorized tok_secret_9876", McpAuthError, AUTH_HINT),
        (403, {"Content-Type": "text/plain"}, b"forbidden", McpAuthError, AUTH_HINT),
        (
            302,
            {"Location": "https://other.example.com/api/mcp?token=tok_secret_9876"},
            b"",
            McpUnavailableError,
            REDIRECT_HINT,
        ),
        (404, {"Content-Type": "text/plain"}, b"not found", McpUnavailableError, ""),
        (405, {"Content-Type": "text/plain"}, b"method not allowed", McpUnavailableError, ""),
        (500, {"Content-Type": "text/plain"}, b"server error tok_secret_9876", McpUnavailableError, ""),
    ],
)
def test_http_status_mapping_and_secret_redaction(status, headers, body, exc_cls, expected_hint) -> None:
    def handler(method: str, path: str, req_headers: dict[str, str], req_body: bytes):
        return status, headers, body

    with _FakeMcpServer(handler) as srv:
        with AnqiMcpClient(srv.base_url, "tok_secret_9876") as client:
            with pytest.raises(exc_cls) as exc_info:
                client.initialize()
            assert exc_info.value.http_status == status
            if expected_hint:
                assert exc_info.value.hint == expected_hint
            assert "tok_secret_9876" not in str(exc_info.value)
            # For 302, verify we did NOT follow redirect (only 1 request received)
            assert len(srv.requests_log) == 1


def test_protocol_errors_and_non_tool_auth_rpc_error() -> None:
    mode = "bad_json"

    def handler(method: str, path: str, headers: dict[str, str], body: bytes):
        data = json.loads(body.decode("utf-8"))
        if mode == "bad_json":
            return 200, {"Content-Type": "application/json"}, b"{not-json"
        if mode == "id_mismatch":
            return _json_resp({"jsonrpc": "2.0", "id": data["id"] + 99, "result": {}})
        if mode == "missing_result":
            return _json_resp({"jsonrpc": "2.0", "id": data["id"]})
        if mode == "bad_content_type":
            return 200, {"Content-Type": "text/html"}, b"<html>nginx error</html>"
        if mode == "sse_empty":
            return _sse_resp([": only comment\n\n"])
        if mode == "rpc_auth":
            return _json_resp(
                {"jsonrpc": "2.0", "id": data["id"], "error": {"code": -32001, "message": "Invalid token tok_abc_9999"}}
            )
        raise AssertionError(mode)

    with _FakeMcpServer(handler) as srv:
        for test_mode in ("bad_json", "id_mismatch", "missing_result", "bad_content_type", "sse_empty"):
            mode = test_mode
            with AnqiMcpClient(srv.base_url, "tok_abc_9999") as client:
                with pytest.raises(McpProtocolError):
                    client.initialize()

        mode = "rpc_auth"
        with AnqiMcpClient(srv.base_url, "tok_abc_9999") as client:
            with pytest.raises(McpAuthError) as exc_info:
                client.initialize()
            assert "tok_abc_9999" not in str(exc_info.value)


def test_transport_exceptions_map_to_unavailable_with_ssl_hint_and_redaction() -> None:
    secret = "tok_transport_7777"

    class _RaisingSession:
        def __init__(self, exc: Exception) -> None:
            self._exc = exc
            self.closed = False

        def post(self, *args, **kwargs):
            assert kwargs.get("allow_redirects") is False
            assert kwargs.get("verify") is True
            raise self._exc

        def close(self) -> None:
            self.closed = True

    ssl_sess = _RaisingSession(requests.exceptions.SSLError(f"cert failed for Bearer {secret}"))
    client = AnqiMcpClient("https://site.example", secret, session=ssl_sess)
    with pytest.raises(McpUnavailableError) as ssl_exc:
        client.initialize()
    assert ssl_exc.value.hint == SSL_HINT
    assert secret not in str(ssl_exc.value)
    assert isinstance(ssl_exc.value.__cause__, requests.exceptions.SSLError)
    client.close()
    assert ssl_sess.closed is False  # injected session must not be closed by client

    to_sess = _RaisingSession(requests.exceptions.ReadTimeout("timed out"))
    client2 = AnqiMcpClient("https://site.example", secret, session=to_sess)
    with pytest.raises(McpUnavailableError) as to_exc:
        client2.initialize()
    assert isinstance(to_exc.value.__cause__, requests.exceptions.ReadTimeout)
    assert client2.ping() is False

    conn_sess = _RaisingSession(requests.exceptions.ConnectionError("refused"))
    client3 = AnqiMcpClient("https://site.example", secret, session=conn_sess)
    with pytest.raises(McpUnavailableError) as conn_exc:
        client3.initialize()
    assert isinstance(conn_exc.value.__cause__, requests.exceptions.ConnectionError)


def test_probe_site_success_and_never_raises_on_failures() -> None:
    def handler(method: str, path: str, headers: dict[str, str], body: bytes):
        if method == "DELETE":
            return 204, {}, b""
        data = json.loads(body.decode("utf-8"))
        if data["method"] == "initialize":
            return _json_resp(
                {
                    "jsonrpc": "2.0",
                    "id": data["id"],
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "serverInfo": {"name": "AnqiCMS", "version": "3.6.7"},
                    },
                },
                headers={"Mcp-Session-Id": "probe-sess"},
            )
        if data["method"] == "notifications/initialized":
            return 202, {}, b""
        if data["method"] == "tools/list":
            return _json_resp(
                {
                    "jsonrpc": "2.0",
                    "id": data["id"],
                    "result": {
                        "tools": [
                            {"name": "friendlink_create", "description": "新增友情链接"},
                            {"name": "system_check_update", "description": "检查更新与版本"},
                            {"name": "system_upgrade", "description": "执行系统升级"},
                            {"name": "create_login_ticket", "description": "生成后台登录跳转票据"},
                            {"name": "article_publish", "description": "发布文章"},
                        ]
                    },
                }
            )
        raise AssertionError(data)

    with _FakeMcpServer(handler) as srv:
        site = _make_site(name="站点A", url=srv.base_url, mcp_token="tok_probe_1234")
        result = probe_site(site, IntegrationSettings())
        assert result.ok is True
        assert result.site_name == "站点A"
        assert result.server_name == "AnqiCMS"
        assert result.server_version == "3.6.7"
        assert result.protocol_version == "2025-06-18"
        assert result.tool_count == 5
        assert result.friendlink_tools == ("friendlink_create",)
        assert set(result.upgrade_tools) == {"system_check_update", "system_upgrade"}
        assert result.jump_tools == ("create_login_ticket",)

    # Missing token → never raises
    no_tok_site = _make_site(name="无令牌站", url="https://example.com")
    res_no_tok = probe_site(no_tok_site)
    assert res_no_tok.ok is False
    assert res_no_tok.site_name == "无令牌站"
    assert res_no_tok.hint == AUTH_HINT

    # Unexpected session factory failure → never raises, redacts token
    def broken_factory():
        raise RuntimeError("boom with tok_leak_9999")

    bad_site = _make_site(name="异常站", url="https://example.com", mcp_token="tok_leak_9999")
    res_broken = probe_site(bad_site, session_factory=broken_factory)
    assert res_broken.ok is False
    assert "tok_leak_9999" not in res_broken.message
