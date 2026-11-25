"""Admin backend direct-jump service with tiered handoff & safe fallbacks.

Tier chain:
1. ``ticket``: MCP one-time login ticket via ``McpClientFactory`` (if enabled &
   token available), opened through the local 127.0.0.1 ``JumpBridge``.
2. ``bridge``: Silent admin login via AnQiCMS admin login API (if enabled &
   admin username/password configured) + local ``JumpBridge`` redirect or
   auto-POST handoff (when configured in ``integration_settings.json``).
3. ``direct``: Direct browser open of ``{url}{admin_path}`` — never carries
   secrets and always works as long as the site has a valid URL.
"""

from __future__ import annotations

import json
import re
import threading
import time
import webbrowser
from collections.abc import Callable, Iterable, Mapping
from urllib.parse import quote, urlsplit

import requests

from core.admin_proxy import AdminProxyServer
from core.integration_settings import IntegrationSettings
from core.jump_bridge import JumpBridge
from core.mcp_contracts import (
    McpAuthError,
    McpClientFactory,
    McpError,
    McpScopeError,
    McpUnavailableError,
    is_jump_candidate_tool,
)
from core.redaction import SecretRedactor
from core.site_credentials import (
    admin_url as build_admin_url,
    effective_mcp_token,
    has_admin_credentials,
    site_origin_key,
)
from models.sitegroup import (
    JUMP_TIER_BRIDGE,
    JUMP_TIER_DIRECT,
    JUMP_TIER_TICKET,
    AdminJumpResult,
    JumpPlan,
    McpToolInfo,
    McpToolResult,
)

_SUCCESS_CODES = {0, "0", 200, "200"}
_URL_ARG_KEYS = ("url", "redirect", "redirect_url", "target_url", "admin_url")
_URL_RESULT_KEYS = (
    "login_url",
    "jump_url",
    "ticket_url",
    "redirect_url",
    "admin_url",
    "url",
    "link",
    "href",
)
_TICKET_RESULT_KEYS = (
    "ticket",
    "login_ticket",
    "one_time_ticket",
    "jump_ticket",
    "code",
    "token",
)
_AUTH_ERROR_KEYWORDS = (
    "密码",
    "账号",
    "用户名",
    "用户不存在",
    "password",
    "username",
    "user_name",
    "credential",
    "invalid",
    "incorrect",
    "unauthorized",
    "登录失败",
)
_HTTP_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")
_TEMPLATE_VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _is_valid_http_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    cleaned = url.strip()
    if not cleaned or any(ord(ch) < 32 or ord(ch) == 127 or ch.isspace() for ch in cleaned):
        return False
    try:
        parsed = urlsplit(cleaned)
    except ValueError:
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname) and bool(parsed.netloc)


def _render_url_template(template: str, values: dict[str, str]) -> str:
    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return values.get(key, match.group(0))

    return _TEMPLATE_VAR_RE.sub(_replace, template.strip())


def _build_ticket_tool_args(tool: McpToolInfo, target_admin_url: str) -> dict[str, str]:
    schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    args: dict[str, str] = {}
    for prop_name in properties:
        if isinstance(prop_name, str) and prop_name.casefold() in _URL_ARG_KEYS:
            args[prop_name] = target_admin_url
    return args


def _iter_structured_urls(payload: object) -> tuple[list[str], list[str]]:
    """Return ``(priority_urls, fallback_urls)`` discovered in structured data."""
    priority: list[str] = []
    other: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            for key in _URL_RESULT_KEYS:
                for actual_key, value in node.items():
                    if isinstance(actual_key, str) and actual_key.casefold() == key:
                        if isinstance(value, str) and value.strip().startswith(("http://", "https://")):
                            priority.append(value.strip())
            for value in node.values():
                if isinstance(value, str):
                    stripped = value.strip()
                    if stripped.startswith(("http://", "https://")):
                        other.append(stripped)
                elif isinstance(value, (dict, list, tuple)):
                    _walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                if isinstance(item, str):
                    stripped = item.strip()
                    if stripped.startswith(("http://", "https://")):
                        other.append(stripped)
                elif isinstance(item, (dict, list, tuple)):
                    _walk(item)
        elif isinstance(node, str):
            stripped = node.strip()
            if stripped.startswith(("http://", "https://")):
                other.append(stripped)

    _walk(payload)
    return priority, other


def _extract_ticket_value(payload: object) -> str:
    if isinstance(payload, dict):
        for wanted in _TICKET_RESULT_KEYS:
            for key, value in payload.items():
                if isinstance(key, str) and key.casefold() == wanted:
                    if isinstance(value, str):
                        cleaned = value.strip()
                        if cleaned and not cleaned.startswith(("http://", "https://")):
                            return cleaned
        for value in payload.values():
            if isinstance(value, (dict, list, tuple)):
                nested = _extract_ticket_value(value)
                if nested:
                    return nested
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            nested = _extract_ticket_value(item)
            if nested:
                return nested
    return ""


def _extract_candidate_urls_and_ticket(result: McpToolResult) -> tuple[list[str], str]:
    structured = result.structured
    if structured is None and isinstance(result.text, str) and result.text.strip().startswith(("{", "[")):
        try:
            structured = json.loads(result.text)
        except ValueError:
            structured = None

    urls: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str) -> None:
        cleaned = candidate.strip().rstrip(".,;")
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            urls.append(cleaned)

    if structured is not None:
        priority, other = _iter_structured_urls(structured)
        for item in priority:
            _add(item)
        for item in other:
            _add(item)

    if isinstance(result.text, str) and result.text:
        for match in _HTTP_URL_RE.findall(result.text):
            _add(match)

    ticket = _extract_ticket_value(structured) if structured is not None else ""
    return urls, ticket


def _extract_login_token(body: dict) -> str:
    data = body.get("data")
    if isinstance(data, dict):
        for key in ("token", "access_token", "admin_token"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    elif isinstance(data, str) and data.strip():
        return data.strip()
    for key in ("token", "access_token", "admin_token"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_session_cookies(session: object, response: object) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for jar in (getattr(session, "cookies", None), getattr(response, "cookies", None)):
        if jar is None:
            continue
        if hasattr(jar, "items"):
            for key, value in jar.items():
                if key is not None and value is not None:
                    cookies[str(key)] = str(value)
        elif isinstance(jar, Mapping):
            for key, value in jar.items():
                if value is not None:
                    cookies[str(key)] = str(value)
    return cookies


def _looks_like_wrong_credentials(message: str) -> bool:
    if not message:
        return True
    lowered = message.casefold()
    return any(keyword in lowered for keyword in _AUTH_ERROR_KEYWORDS)


class AdminJumpService:
    """Resolve and open AnQiCMS admin backends with safe tiered fallbacks."""

    def __init__(
        self,
        settings: IntegrationSettings | None = None,
        *,
        mcp_client_factory: McpClientFactory | None = None,
        session_factory: Callable[[], requests.Session] = requests.Session,
        opener: Callable[[str], bool] = webbrowser.open_new_tab,
        bridge: JumpBridge | None = None,
        proxy: AdminProxyServer | None = None,
        use_local_proxy: bool = False,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings if settings is not None else IntegrationSettings()
        self._mcp_client_factory = mcp_client_factory
        self._session_factory = session_factory
        self._opener = opener
        self._bridge = bridge
        self._bridge_lock = threading.Lock()
        self.proxy = proxy
        self._use_local_proxy = bool(use_local_proxy)
        self._owns_proxy = False
        self._proxy_lock = threading.Lock()
        self._sleep = sleep

    def admin_url(self, site) -> str:
        """Return the clean ``{url}{admin_path}`` backend URL for ``site``."""
        base = getattr(site, "url", "")
        if not isinstance(base, str) or not _is_valid_http_url(base):
            raise ValueError("站点未配置有效网址")
        return build_admin_url(site)

    def resolve(self, site) -> JumpPlan:
        """Determine the jump tier and target URL without opening a browser."""
        site_name = str(getattr(site, "name", "") or "")
        base_admin_url = self.admin_url(site)
        base_site_url = str(getattr(site, "url", "")).strip().rstrip("/")
        expected_origin = site_origin_key(base_admin_url)
        redactor = SecretRedactor.from_sites([site])
        reasons: list[str] = []

        # Tier 1: MCP one-time login ticket
        ticket_plan = self._try_ticket_tier(
            site=site,
            site_name=site_name,
            base_site_url=base_site_url,
            base_admin_url=base_admin_url,
            expected_origin=expected_origin,
            redactor=redactor,
            reasons=reasons,
        )
        if ticket_plan is not None:
            return ticket_plan

        # Tier 2: Silent admin login + local 127.0.0.1 bridge
        bridge_plan = self._try_bridge_tier(
            site=site,
            site_name=site_name,
            base_site_url=base_site_url,
            base_admin_url=base_admin_url,
            expected_origin=expected_origin,
            redactor=redactor,
            reasons=reasons,
        )
        if bridge_plan is not None:
            return bridge_plan

        # Tier 3: Direct browser open (never contains secrets)
        return JumpPlan(
            site_name=site_name,
            tier=JUMP_TIER_DIRECT,
            target_url=base_admin_url,
            display_url=base_admin_url,
            fallback_reasons=tuple(redactor(reason) for reason in reasons),
        )

    def open(self, site) -> AdminJumpResult:
        """Resolve and launch the browser for ``site``. Never raises."""
        site_name = str(getattr(site, "name", "") or "")
        redactor = SecretRedactor.from_sites([site])
        try:
            plan = self.resolve(site)
        except Exception as exc:  # noqa: BLE001
            message = redactor(str(exc)) or "无法解析站点后台地址"
            return AdminJumpResult(
                site_name=site_name,
                tier=JUMP_TIER_DIRECT,
                success=False,
                display_url="",
                message=message,
                fallback_reasons=(message,),
            )

        try:
            opened = self._opener(plan.target_url)
        except Exception as exc:  # noqa: BLE001
            return AdminJumpResult(
                site_name=plan.site_name,
                tier=plan.tier,
                success=False,
                display_url=plan.display_url,
                message=f"打开浏览器失败：{redactor(str(exc))}",
                fallback_reasons=plan.fallback_reasons,
            )

        if opened is False:
            return AdminJumpResult(
                site_name=plan.site_name,
                tier=plan.tier,
                success=False,
                display_url=plan.display_url,
                message="浏览器未能打开后台页面",
                fallback_reasons=plan.fallback_reasons,
            )

        if plan.tier == JUMP_TIER_TICKET:
            message = "已通过一次性票据打开后台"
        elif plan.tier == JUMP_TIER_BRIDGE:
            target_host = (urlsplit(plan.target_url).hostname or "").casefold()
            if target_host.endswith(".localhost"):
                message = "已通过本地代理注入后台登录态"
            else:
                message = "已通过本地桥接免密打开后台"
        elif plan.fallback_reasons:
            message = "已回退直接打开后台登录页"
        else:
            message = "已直接打开后台登录页"

        return AdminJumpResult(
            site_name=plan.site_name,
            tier=plan.tier,
            success=True,
            display_url=plan.display_url,
            message=redactor(message),
            fallback_reasons=plan.fallback_reasons,
        )

    def open_many(
        self,
        sites: Iterable,
        *,
        progress: Callable | None = None,
        cancel_event: threading.Event | None = None,
    ) -> list[AdminJumpResult]:
        """Open multiple site backends with batch cap, stagger and cancellation."""
        site_list = list(sites)
        batch_max = max(1, int(self.settings.admin_jump.batch_max))
        capped = site_list[:batch_max]
        stagger_s = max(0, int(self.settings.admin_jump.batch_stagger_ms)) / 1000.0
        total = len(capped)

        results: list[AdminJumpResult] = []
        for index, site in enumerate(capped):
            if cancel_event is not None and cancel_event.is_set():
                break
            if index > 0 and stagger_s > 0:
                self._sleep(stagger_s)
                if cancel_event is not None and cancel_event.is_set():
                    break
            result = self.open(site)
            results.append(result)
            if progress is not None:
                progress(index + 1, total, result)
        return results

    def shutdown(self) -> None:
        """Stop the local bridge (and owned local proxy, if created) if running."""
        with self._bridge_lock:
            bridge = self._bridge
        if bridge is not None:
            bridge.stop()
        with self._proxy_lock:
            owned_proxy = self.proxy if self._owns_proxy else None
        if owned_proxy is not None:
            owned_proxy.stop()

    def __enter__(self) -> AdminJumpService:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown()

    # ------------------------------------------------------------------ internals

    def _get_bridge(self) -> JumpBridge:
        with self._bridge_lock:
            if self._bridge is None:
                self._bridge = JumpBridge(ttl_s=self.settings.admin_jump.bridge_ttl_s)
            return self._bridge

    def _get_proxy(self) -> AdminProxyServer | None:
        with self._proxy_lock:
            if self.proxy is not None:
                return self.proxy
            if not self._use_local_proxy:
                return None
            self.proxy = AdminProxyServer(
                session_factory=self._session_factory,
                verify_ssl=self.settings.mcp.verify_ssl,
            )
            self._owns_proxy = True
            return self.proxy

    def _try_ticket_tier(
        self,
        *,
        site,
        site_name: str,
        base_site_url: str,
        base_admin_url: str,
        expected_origin: str,
        redactor: SecretRedactor,
        reasons: list[str],
    ) -> JumpPlan | None:
        jump_cfg = self.settings.admin_jump
        if not jump_cfg.enable_ticket or self._mcp_client_factory is None:
            return None
        if not effective_mcp_token(site):
            return None

        client = None
        try:
            client = self._mcp_client_factory(site)
            client.initialize()
            tools = [
                tool
                for tool in client.find_tools(
                    jump_cfg.ticket_keywords,
                    explicit_names=jump_cfg.ticket_tool_names,
                )
                if is_jump_candidate_tool(tool, explicit_names=jump_cfg.ticket_tool_names)
            ]
            if not tools:
                reasons.append("站点 MCP 未提供免密登录票据工具")
                return None

            tool = tools[0]
            args = _build_ticket_tool_args(tool, base_admin_url)
            result = client.call_tool(tool.name, args or None)
        except McpScopeError as exc:
            reasons.append(f"MCP 票据功能未开放：{redactor(exc.hint or str(exc))}")
            return None
        except McpAuthError as exc:
            reasons.append(f"MCP 鉴权失败：{redactor(str(exc))}")
            return None
        except McpUnavailableError as exc:
            reasons.append(f"MCP 不可用：{redactor(str(exc))}")
            return None
        except McpError as exc:
            reasons.append(f"MCP 获取登录票据失败：{redactor(str(exc))}")
            return None
        except Exception as exc:  # noqa: BLE001
            reasons.append(f"MCP 免密跳转异常：{redactor(str(exc))}")
            return None
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass

        if result.is_error:
            detail = redactor(result.text.strip()) if isinstance(result.text, str) else ""
            reasons.append(f"MCP 票据工具返回错误：{detail or '未知错误'}")
            return None

        candidate_urls, ticket = _extract_candidate_urls_and_ticket(result)
        if ticket:
            redactor.add(ticket)

        origin_mismatch = False
        for candidate in candidate_urls:
            if not _is_valid_http_url(candidate):
                continue
            if site_origin_key(candidate) == expected_origin:
                redactor.add(candidate)
                bridge_url = self._get_bridge().register_redirect(candidate)
                return JumpPlan(
                    site_name=site_name,
                    tier=JUMP_TIER_TICKET,
                    target_url=bridge_url,
                    display_url=base_admin_url,
                    fallback_reasons=tuple(redactor(reason) for reason in reasons),
                )
            origin_mismatch = True

        if ticket:
            template = (jump_cfg.ticket_url_template or "").strip()
            if not template:
                reasons.append("MCP 已返回登录票据，但未配置票据跳转地址模板")
                return None
            encoded_ticket = quote(ticket, safe="")
            formatted = _render_url_template(
                template,
                {
                    "admin_url": base_admin_url,
                    "site_url": base_site_url,
                    "ticket": encoded_ticket,
                    "code": encoded_ticket,
                },
            )
            if not _is_valid_http_url(formatted) or site_origin_key(formatted) != expected_origin:
                reasons.append("票据模板生成的跳转链接域名与当前站点不一致，已拒绝")
                return None
            redactor.add(formatted)
            bridge_url = self._get_bridge().register_redirect(formatted)
            return JumpPlan(
                site_name=site_name,
                tier=JUMP_TIER_TICKET,
                target_url=bridge_url,
                display_url=base_admin_url,
                fallback_reasons=tuple(redactor(reason) for reason in reasons),
            )

        if origin_mismatch:
            reasons.append("MCP 返回的跳转链接域名与当前站点不一致，已拒绝")
        else:
            reasons.append("MCP 票据工具未返回有效的跳转链接或票据")
        return None

    def _try_bridge_tier(
        self,
        *,
        site,
        site_name: str,
        base_site_url: str,
        base_admin_url: str,
        expected_origin: str,
        redactor: SecretRedactor,
        reasons: list[str],
    ) -> JumpPlan | None:
        jump_cfg = self.settings.admin_jump
        if not jump_cfg.enable_silent_login or not has_admin_credentials(site):
            return None

        username = str(getattr(site, "admin_username", "")).strip()
        password = str(getattr(site, "admin_password", "")).strip()
        redactor.add(password)

        login_urls = self._build_login_urls(base_site_url, base_admin_url, expected_origin)
        if not login_urls:
            reasons.append("未配置有效的后台登录接口路径")
            return None

        timeout = (self.settings.mcp.connect_timeout, self.settings.mcp.read_timeout)
        verify_ssl = self.settings.mcp.verify_ssl
        payload = {"user_name": username, "password": password}

        session = self._session_factory()
        try:
            not_found_count = 0
            for login_url in login_urls:
                try:
                    response = session.post(
                        login_url,
                        json=payload,
                        timeout=timeout,
                        allow_redirects=False,
                        verify=verify_ssl,
                    )
                except requests.exceptions.SSLError:
                    reasons.append("后台登录证书校验失败")
                    return None
                except requests.exceptions.Timeout:
                    reasons.append("后台静默登录超时")
                    return None
                except requests.RequestException as exc:
                    reasons.append(f"后台静默登录连接失败：{redactor(str(exc))}")
                    return None

                status = int(getattr(response, "status_code", 0) or 0)
                if status in {404, 405}:
                    not_found_count += 1
                    continue
                if 300 <= status < 400:
                    reasons.append("后台登录接口发生重定向，请检查站点网址 http/https")
                    return None
                if status in {401, 403}:
                    reasons.append("后台账号或密码错误")
                    return None
                if not (200 <= status < 300):
                    reasons.append(f"后台静默登录失败（HTTP {status}）")
                    return None

                try:
                    body = response.json()
                except ValueError:
                    not_found_count += 1
                    continue
                if not isinstance(body, dict):
                    not_found_count += 1
                    continue

                if "code" in body and body["code"] not in _SUCCESS_CODES:
                    msg = str(body.get("msg") or body.get("message") or "").strip()
                    if _looks_like_wrong_credentials(msg):
                        reasons.append("后台账号或密码错误")
                    else:
                        reasons.append(f"后台登录失败：{redactor(msg)}")
                    return None

                token = _extract_login_token(body)
                if token:
                    redactor.add(token)
                cookies = _extract_session_cookies(session, response)
                for cookie_val in cookies.values():
                    redactor.add(cookie_val)

                template = (jump_cfg.token_handoff_url_template or "").strip()
                if template:
                    if not token:
                        reasons.append("后台登录成功但未返回 token")
                        return None
                    handoff_url = _render_url_template(
                        template,
                        {
                            "admin_url": base_admin_url,
                            "site_url": base_site_url,
                            "token": quote(token, safe=""),
                        },
                    )
                    if not _is_valid_http_url(handoff_url) or site_origin_key(handoff_url) != expected_origin:
                        reasons.append("免密交接链接域名与当前站点不一致，已拒绝")
                        return None
                    redactor.add(handoff_url)
                    bridge_url = self._get_bridge().register_redirect(handoff_url)
                    return JumpPlan(
                        site_name=site_name,
                        tier=JUMP_TIER_BRIDGE,
                        target_url=bridge_url,
                        display_url=base_admin_url,
                        fallback_reasons=tuple(redactor(reason) for reason in reasons),
                    )

                if jump_cfg.autopost_login:
                    bridge_url = self._get_bridge().register_autopost(
                        login_url,
                        {"user_name": username, "password": password},
                    )
                    return JumpPlan(
                        site_name=site_name,
                        tier=JUMP_TIER_BRIDGE,
                        target_url=bridge_url,
                        display_url=base_admin_url,
                        fallback_reasons=tuple(redactor(reason) for reason in reasons),
                    )

                proxy = self._get_proxy()
                if proxy is not None:
                    if not token and not cookies:
                        reasons.append("后台登录成功但未返回登录凭证")
                        return None
                    proxy_url = proxy.register_site(
                        site,
                        auth_token=token,
                        cookies=cookies,
                        verify_ssl=verify_ssl,
                    )
                    return JumpPlan(
                        site_name=site_name,
                        tier=JUMP_TIER_BRIDGE,
                        target_url=proxy_url,
                        display_url=base_admin_url,
                        fallback_reasons=tuple(redactor(reason) for reason in reasons),
                    )

                reasons.append("账号已校验，站点未配置免密交接方式")
                return None

            if not_found_count:
                reasons.append("后台登录接口不存在或响应格式不兼容")
            return None
        finally:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass

    def _build_login_urls(
        self,
        base_site_url: str,
        base_admin_url: str,
        expected_origin: str,
    ) -> list[str]:
        urls: list[str] = []
        for raw_path in self.settings.admin_jump.login_api_paths:
            if not isinstance(raw_path, str):
                continue
            rel = raw_path.strip()
            if not rel:
                continue
            if "://" in rel:
                candidate = rel
            elif rel.startswith("/"):
                candidate = f"{base_site_url}{rel}"
            else:
                candidate = f"{base_admin_url}{rel.lstrip('/')}"
            if _is_valid_http_url(candidate) and site_origin_key(candidate) == expected_origin:
                if candidate not in urls:
                    urls.append(candidate)
        return urls
