"""Local loopback reverse proxy for per-site isolated passwordless admin jump.

Browsers automatically resolve ``*.localhost`` to ``127.0.0.1`` without
modifying ``hosts``.  Each site receives its own ``<slug>.localhost:<port>``
origin so ``localStorage`` tokens and upstream session cookies stay strictly
isolated per site while booting the AnQiCMS ``/system/`` SPA authenticated.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from core.redaction import SecretRedactor
from core.site_credentials import site_origin_key
from core.site_factory import DEFAULT_ADMIN_PATH, normalize_admin_path

DEFAULT_TOKEN_STORAGE_KEYS: tuple[str, ...] = ("token", "anqicms_token", "admin_token")

_HOP_BY_HOP_HEADERS: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "proxy-connection",
    }
)
_STRIP_REQUEST_HEADERS: frozenset[str] = _HOP_BY_HOP_HEADERS | frozenset(
    {
        "host",
        "content-length",
        "accept-encoding",
    }
)
_STRIP_RESPONSE_HEADERS: frozenset[str] = _HOP_BY_HOP_HEADERS | frozenset(
    {
        "content-length",
        "content-encoding",
    }
)
_CSP_HEADER_NAMES: frozenset[str] = frozenset(
    {
        "content-security-policy",
        "content-security-policy-report-only",
    }
)
_HEAD_END_RE = re.compile(rb"</head>", re.IGNORECASE)
_BODY_START_RE = re.compile(rb"<body(?:\s[^>]*)?>", re.IGNORECASE)
_CHARSET_RE = re.compile(r"charset\s*=\s*['\"]?\s*([A-Za-z0-9._-]+)", re.IGNORECASE)
_SET_COOKIE_DOMAIN_RE = re.compile(r";\s*domain\s*=[^;]*", re.IGNORECASE)
_SET_COOKIE_SECURE_RE = re.compile(r";\s*secure(?=\s*;|\s*$)", re.IGNORECASE)
_SLUG_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


@dataclass(frozen=True, slots=True)
class _ProxySiteEntry:
    slug: str
    site_name: str
    site_url: str
    upstream_origin: str
    admin_path: str
    auth_token: str
    cookies: dict[str, str]
    token_storage_keys: tuple[str, ...]
    verify_ssl: bool | None


class _ProxyHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, RequestHandlerClass, *, reuse_address: bool = False) -> None:
        self.allow_reuse_address = reuse_address
        super().__init__(server_address, RequestHandlerClass)


def _validate_http_url(url: str) -> tuple[str, str]:
    cleaned = str(url or "").strip().rstrip("/")
    if not cleaned or any(ord(ch) < 32 or ord(ch) == 127 or ch.isspace() for ch in cleaned):
        raise ValueError("站点网址无效")
    parsed = urlsplit(cleaned)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or not parsed.netloc:
        raise ValueError("站点网址必须是有效的 http/https 地址")
    origin = f"{parsed.scheme.lower()}://{parsed.netloc}"
    return cleaned, origin


def _slugify_site(site_name: str, site_url: str) -> str:
    hostname = (urlsplit(site_url).hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    raw_label = re.sub(r"[^a-z0-9-]", "-", (site_name or "").lower()).strip("-")
    if not raw_label:
        raw_label = re.sub(r"[^a-z0-9-]", "-", hostname).strip("-") or "site"
    prefix = re.sub(r"-{2,}", "-", raw_label)[:40].strip("-") or "site"
    digest = hashlib.sha256(f"{site_name}\0{site_url}".encode("utf-8")).hexdigest()[:8]
    return f"{prefix}-{digest}"


def _coerce_cookies(cookies: object) -> dict[str, str]:
    if cookies is None:
        return {}
    result: dict[str, str] = {}
    if hasattr(cookies, "items"):
        for key, value in cookies.items():
            if key is not None and value is not None:
                result[str(key)] = str(value)
        return result
    if isinstance(cookies, Iterable) and not isinstance(cookies, (str, bytes)):
        for item in cookies:
            if isinstance(item, tuple) and len(item) == 2:
                result[str(item[0])] = str(item[1])
    return result


def _parse_cookie_header(raw_header: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    if not raw_header:
        return parsed
    cookie = SimpleCookie()
    try:
        cookie.load(raw_header)
    except Exception:  # noqa: BLE001
        cookie = None
    if cookie:
        for name, morsel in cookie.items():
            parsed[name] = morsel.value
        if parsed:
            return parsed
    for segment in raw_header.split(";"):
        name, sep, value = segment.partition("=")
        if sep and name.strip():
            parsed[name.strip()] = value.strip()
    return parsed


def _format_cookie_header(cookies: Mapping[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items() if name)


def _rewrite_set_cookie_for_proxy(header_value: str) -> str:
    cleaned = _SET_COOKIE_DOMAIN_RE.sub("", header_value)
    cleaned = _SET_COOKIE_SECURE_RE.sub("", cleaned)
    return cleaned.strip().rstrip(";")


def _is_loopback_host(hostname: str) -> bool:
    if hostname in {"127.0.0.1", "localhost", "::1", "[::1]"}:
        return True
    if hostname.endswith(".localhost"):
        label = hostname[: -len(".localhost")]
        return bool(_SLUG_LABEL_RE.fullmatch(label))
    return False


def _is_admin_entry_path(req_path: str, admin_path: str) -> bool:
    clean = (req_path or "/").rstrip("/") or "/"
    admin_clean = admin_path.rstrip("/") or "/"
    candidates = {
        admin_clean,
        "/index.html" if admin_clean == "/" else f"{admin_clean}/index.html",
        "/system",
        "/system/index.html",
    }
    return clean in candidates


def _build_seed_script(auth_token: str, token_storage_keys: tuple[str, ...]) -> str:
    token_js = json.dumps(auth_token, ensure_ascii=False).replace("</", "<\\/")
    keys_js = json.dumps(list(token_storage_keys), ensure_ascii=False).replace("</", "<\\/")
    return (
        "<script>(function(){try{"
        f"var t={token_js};var ks={keys_js};"
        "for(var i=0;i<ks.length;i++){window.localStorage.setItem(ks[i],t);}"
        "}catch(e){}})();</script>"
    )


def _inject_token_script_bytes(
    raw_html: bytes,
    content_type: str,
    auth_token: str,
    token_storage_keys: tuple[str, ...],
) -> bytes:
    charset = "utf-8"
    match = _CHARSET_RE.search(content_type or "")
    if match:
        candidate = match.group(1).strip()
        try:
            b"".decode(candidate)
            charset = candidate
        except LookupError:
            charset = "utf-8"

    script_bytes = _build_seed_script(auth_token, token_storage_keys).encode(
        charset,
        errors="replace",
    )

    head_match = _HEAD_END_RE.search(raw_html)
    if head_match is not None:
        idx = head_match.start()
        return raw_html[:idx] + script_bytes + raw_html[idx:]

    body_match = _BODY_START_RE.search(raw_html)
    if body_match is not None:
        idx = body_match.end()
        return raw_html[:idx] + script_bytes + raw_html[idx:]

    return script_bytes + raw_html


def _extract_set_cookies(response: requests.Response) -> list[str]:
    raw = getattr(response, "raw", None)
    raw_headers = getattr(raw, "headers", None)
    if raw_headers is not None and hasattr(raw_headers, "getlist"):
        values = raw_headers.getlist("Set-Cookie")
        if values:
            return list(values)
    values = []
    headers = getattr(response, "headers", None)
    if headers is not None and hasattr(headers, "items"):
        for key, value in headers.items():
            if str(key).casefold() == "set-cookie":
                values.append(str(value))
    return values


class AdminProxyServer:
    """Local reverse proxy providing per-site origin isolation and auto-login."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        session_factory: Callable[[], requests.Session] = requests.Session,
        verify_ssl: bool = True,
        request_timeout: float = 30.0,
    ) -> None:
        if port < 0 or port > 65535:
            raise ValueError("port 必须在 0-65535 之间")
        self._host = host
        self._configured_port = int(port)
        self._session_factory = session_factory
        self._verify_ssl = bool(verify_ssl)
        self._request_timeout = float(request_timeout)
        self._lock = threading.RLock()
        self._entries: dict[str, _ProxySiteEntry] = {}
        self._site_key_to_slug: dict[tuple[str, str], str] = {}
        self._server: _ProxyHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._port: int = 0
        self._thread_local = threading.local()
        self._sessions: list[requests.Session] = []

    @property
    def port(self) -> int:
        with self._lock:
            return self._port

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._server is not None

    def start(self) -> None:
        """Start the local reverse proxy server if not already running."""
        with self._lock:
            self._start_locked()

    def stop(self) -> None:
        """Stop the local reverse proxy server and release resources."""
        with self._lock:
            server = self._server
            thread = self._thread
            sessions = list(self._sessions)
            self._server = None
            self._thread = None
            self._port = 0
            self._sessions.clear()
            self._thread_local = threading.local()
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        for sess in sessions:
            try:
                sess.close()
            except Exception:  # noqa: BLE001
                pass

    def register_site(
        self,
        site,
        *,
        auth_token: str = "",
        cookies: object = None,
        token_storage_keys: Iterable[str] = DEFAULT_TOKEN_STORAGE_KEYS,
        verify_ssl: bool | None = None,
    ) -> str:
        """Register ``site`` and return its ``http://<slug>.localhost:<port><admin_path>`` URL."""
        site_name = str(getattr(site, "name", "") or "").strip()
        raw_url = str(getattr(site, "url", "") or "").strip()
        site_url, upstream_origin = _validate_http_url(raw_url)
        admin_path = normalize_admin_path(getattr(site, "admin_path", DEFAULT_ADMIN_PATH))
        clean_token = str(auth_token or "").strip()
        clean_cookies = _coerce_cookies(cookies)
        keys = tuple(str(k).strip() for k in (token_storage_keys or ()) if str(k).strip())

        with self._lock:
            self._start_locked()
            identity = (site_name, site_url)
            slug = self._site_key_to_slug.get(identity)
            if slug is None:
                base_slug = _slugify_site(site_name, site_url)
                slug = base_slug
                suffix = 2
                while slug in self._entries:
                    slug = f"{base_slug[:55]}-{suffix}"
                    suffix += 1
                self._site_key_to_slug[identity] = slug

            self._entries[slug] = _ProxySiteEntry(
                slug=slug,
                site_name=site_name,
                site_url=site_url,
                upstream_origin=upstream_origin,
                admin_path=admin_path,
                auth_token=clean_token,
                cookies=clean_cookies,
                token_storage_keys=keys,
                verify_ssl=verify_ssl,
            )
            port = self._port

        return f"http://{slug}.localhost:{port}{admin_path}"

    def unregister_site(self, slug_or_site) -> bool:
        """Remove a registered site by slug or site object."""
        with self._lock:
            if isinstance(slug_or_site, str):
                entry = self._entries.pop(slug_or_site, None)
                if entry is not None:
                    self._site_key_to_slug.pop((entry.site_name, entry.site_url), None)
                    return True
                return False
            site_name = str(getattr(slug_or_site, "name", "") or "").strip()
            site_url = str(getattr(slug_or_site, "url", "") or "").strip().rstrip("/")
            slug = self._site_key_to_slug.pop((site_name, site_url), None)
            if slug is not None:
                self._entries.pop(slug, None)
                return True
            return False

    def fallback_url(self, slug: str, path: str = "") -> str:
        """Return the ``127.0.0.1`` path-prefixed fallback URL for ``slug``."""
        with self._lock:
            entry = self._entries.get(slug)
            if entry is None:
                raise KeyError(f"未注册的代理站点标识: {slug}")
            port = self._port
            admin_path = path or entry.admin_path
        if not admin_path.startswith("/"):
            admin_path = f"/{admin_path}"
        return f"http://127.0.0.1:{port}/__site/{slug}{admin_path}"

    def __enter__(self) -> AdminProxyServer:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    # ------------------------------------------------------------------ internals

    def _start_locked(self) -> None:
        if self._server is not None:
            return
        handler_cls = self._build_handler_class()
        server = _ProxyHTTPServer(
            (self._host, self._configured_port),
            handler_cls,
            reuse_address=(self._configured_port != 0),
        )
        port = int(server.server_address[1])
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name=f"admin-proxy-{port}",
            daemon=True,
        )
        self._server = server
        self._thread = thread
        self._port = port
        thread.start()

    def _get_entry(self, slug: str) -> _ProxySiteEntry | None:
        with self._lock:
            return self._entries.get(slug)

    def _get_session(self) -> requests.Session:
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = self._session_factory()
            self._thread_local.session = session
            with self._lock:
                self._sessions.append(session)
        return session

    def _build_handler_class(self) -> type[BaseHTTPRequestHandler]:
        proxy = self

        class _ProxyHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args) -> None:  # noqa: A002
                # Silent: never expose tokens or cookies via stderr logs.
                return

            def do_GET(self) -> None:  # noqa: N802
                self._forward("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._forward("POST")

            def do_PUT(self) -> None:  # noqa: N802
                self._forward("PUT")

            def do_DELETE(self) -> None:  # noqa: N802
                self._forward("DELETE")

            def do_PATCH(self) -> None:  # noqa: N802
                self._forward("PATCH")

            def do_HEAD(self) -> None:  # noqa: N802
                self._forward("HEAD")

            def do_OPTIONS(self) -> None:  # noqa: N802
                self._forward("OPTIONS")

            def _write_error(self, status: int, message: str) -> None:
                body = message.encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return

            def _resolve_target(self) -> tuple[_ProxySiteEntry, str, str, str] | None:
                raw_host = (self.headers.get("Host") or "").strip()
                if not raw_host:
                    self._write_error(403, "Forbidden")
                    return None
                host_only = raw_host.split(":", 1)[0].strip().lower()
                if not _is_loopback_host(host_only):
                    self._write_error(403, "Forbidden")
                    return None

                proxy_origin = f"http://{raw_host}"
                parsed_req = urlsplit(self.path or "/")

                if host_only.endswith(".localhost"):
                    slug = host_only[: -len(".localhost")]
                    entry = proxy._get_entry(slug)
                    if entry is None:
                        self._write_error(404, "Site proxy target not found")
                        return None
                    upstream_path = self.path or "/"
                    if not upstream_path.startswith("/"):
                        upstream_path = f"/{upstream_path}"
                    return entry, upstream_path, proxy_origin, ""

                if parsed_req.path.startswith("/__site/"):
                    remainder_path = parsed_req.path[len("/__site/") :]
                    slug, sep, tail = remainder_path.partition("/")
                    entry = proxy._get_entry(slug)
                    if not slug or entry is None:
                        self._write_error(404, "Site proxy target not found")
                        return None
                    up_path = f"/{tail}" if sep else "/"
                    if parsed_req.query:
                        up_path = f"{up_path}?{parsed_req.query}"
                    return entry, up_path, proxy_origin, f"/__site/{slug}"

                self._write_error(404, "Not Found")
                return None

            def _read_request_body(self) -> bytes | None:
                te = (self.headers.get("Transfer-Encoding") or "").casefold()
                if "chunked" in te:
                    chunks: list[bytes] = []
                    while True:
                        size_line = self.rfile.readline()
                        if not size_line:
                            break
                        size_str = size_line.split(b";", 1)[0].strip()
                        if not size_str:
                            continue
                        chunk_size = int(size_str, 16)
                        if chunk_size == 0:
                            # Consume trailing CRLF / trailers
                            while True:
                                trailer = self.rfile.readline()
                                if not trailer or trailer in (b"\r\n", b"\n"):
                                    break
                            break
                        chunks.append(self.rfile.read(chunk_size))
                        self.rfile.read(2)
                    return b"".join(chunks)

                raw_length = self.headers.get("Content-Length")
                if raw_length is None:
                    return None
                try:
                    length = int(raw_length)
                except ValueError:
                    return None
                if length <= 0:
                    return b""
                return self.rfile.read(length)

            def _forward(self, method: str) -> None:
                resolved = self._resolve_target()
                if resolved is None:
                    return
                entry, upstream_path, proxy_origin, path_prefix = resolved
                upstream_url = f"{entry.upstream_origin}{upstream_path}"

                fwd_headers: dict[str, str] = {}
                incoming_auth = ""
                incoming_cookies: dict[str, str] = {}
                for key, value in self.headers.items():
                    lower = key.casefold()
                    if lower in _STRIP_REQUEST_HEADERS:
                        continue
                    if lower == "authorization":
                        incoming_auth = value.strip()
                        fwd_headers[key] = value
                        continue
                    if lower == "cookie":
                        incoming_cookies.update(_parse_cookie_header(value))
                        continue
                    if lower == "origin":
                        if value.strip().rstrip("/").casefold() == proxy_origin.casefold():
                            fwd_headers[key] = entry.upstream_origin
                        else:
                            fwd_headers[key] = value
                        continue
                    if lower == "referer":
                        ref_prefix = f"{proxy_origin}{path_prefix}"
                        if value.casefold().startswith(ref_prefix.casefold()):
                            suffix = value[len(ref_prefix) :]
                            if not suffix.startswith("/"):
                                suffix = f"/{suffix}"
                            fwd_headers[key] = f"{entry.upstream_origin}{suffix}"
                        elif value.casefold().startswith(proxy_origin.casefold()):
                            suffix = value[len(proxy_origin) :]
                            fwd_headers[key] = f"{entry.upstream_origin}{suffix}"
                        else:
                            fwd_headers[key] = value
                        continue
                    fwd_headers[key] = value

                if entry.auth_token and not incoming_auth:
                    fwd_headers["Authorization"] = f"Bearer {entry.auth_token}"

                merged_cookies = dict(entry.cookies)
                merged_cookies.update(incoming_cookies)
                if merged_cookies:
                    fwd_headers["Cookie"] = _format_cookie_header(merged_cookies)

                body = self._read_request_body()
                verify = proxy._verify_ssl if entry.verify_ssl is None else entry.verify_ssl
                session = proxy._get_session()

                try:
                    resp = session.request(
                        method=method,
                        url=upstream_url,
                        headers=fwd_headers,
                        data=body,
                        timeout=proxy._request_timeout,
                        allow_redirects=False,
                        verify=verify,
                    )
                except requests.RequestException as exc:
                    redactor = SecretRedactor([entry.auth_token, *entry.cookies.values()])
                    self._write_error(502, f"代理请求上游失败：{redactor(str(exc))}")
                    return
                finally:
                    cookie_jar = getattr(session, "cookies", None)
                    if cookie_jar is not None and hasattr(cookie_jar, "clear"):
                        try:
                            cookie_jar.clear()
                        except Exception:  # noqa: BLE001
                            pass

                raw_content = b"" if method == "HEAD" else bytes(resp.content or b"")
                resp_headers = getattr(resp, "headers", None)
                content_type = ""
                if resp_headers is not None:
                    for key, value in resp_headers.items():
                        if str(key).casefold() == "content-type":
                            content_type = str(value)
                            break

                req_path_only = urlsplit(upstream_path).path
                should_inject = (
                    method != "HEAD"
                    and 200 <= int(resp.status_code) < 300
                    and bool(entry.auth_token)
                    and bool(entry.token_storage_keys)
                    and ("text/html" in content_type.casefold() or "application/xhtml+xml" in content_type.casefold())
                    and _is_admin_entry_path(req_path_only, entry.admin_path)
                )
                if should_inject:
                    raw_content = _inject_token_script_bytes(
                        raw_content,
                        content_type,
                        entry.auth_token,
                        entry.token_storage_keys,
                    )

                try:
                    self.send_response(int(resp.status_code))
                    set_cookies = _extract_set_cookies(resp)
                    if resp_headers is not None:
                        for key, value in resp_headers.items():
                            lower = str(key).casefold()
                            if lower in _STRIP_RESPONSE_HEADERS:
                                continue
                            if lower == "set-cookie":
                                continue
                            if should_inject and lower in _CSP_HEADER_NAMES:
                                continue
                            if lower == "location":
                                rewritten = self._rewrite_location(
                                    str(value),
                                    upstream_url=upstream_url,
                                    entry=entry,
                                    proxy_origin=proxy_origin,
                                    path_prefix=path_prefix,
                                )
                                self.send_header(key, rewritten)
                                continue
                            self.send_header(key, str(value))
                    for cookie_hdr in set_cookies:
                        self.send_header("Set-Cookie", _rewrite_set_cookie_for_proxy(cookie_hdr))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(raw_content)))
                    self.end_headers()
                    if method != "HEAD" and raw_content:
                        self.wfile.write(raw_content)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return

            @staticmethod
            def _rewrite_location(
                location: str,
                *,
                upstream_url: str,
                entry: _ProxySiteEntry,
                proxy_origin: str,
                path_prefix: str,
            ) -> str:
                abs_loc = urljoin(upstream_url, location.strip())
                parsed_loc = urlsplit(abs_loc)
                loc_origin = f"{parsed_loc.scheme.lower()}://{parsed_loc.netloc}"
                if (
                    loc_origin.casefold() == entry.upstream_origin.casefold()
                    or site_origin_key(abs_loc) == site_origin_key(entry.site_url)
                ):
                    new_path = f"{path_prefix}{parsed_loc.path or '/'}"
                    return urlunsplit(
                        (
                            "http",
                            urlsplit(proxy_origin).netloc,
                            new_path,
                            parsed_loc.query,
                            parsed_loc.fragment,
                        )
                    )
                return location

        return _ProxyHandler
