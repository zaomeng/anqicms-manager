from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core.config_service import load_config, save_config
from core.site_onboarding import (
    SiteDiscoveryResult,
    auto_discover_site,
    auto_discover_sites,
    parse_bulk_new_sites,
)
from models.entities import SiteConfig
from models.sitegroup import McpProbeResult, McpToolInfo


def _make_existing_site(tmp_path: Path, name: str = "existing", url: str = "https://existing.example.com") -> SiteConfig:
    return SiteConfig(
        name=name,
        fid=1,
        url=url,
        jiekou="/api/import/archive?token=old-tok",
        publish_dir=tmp_path / "文章目录" / name,
        daily_publish_filename=tmp_path / "设置" / f"{name}_daily.txt",
        publish_time="06:00:00",
        interval_time=60,
        daily_publish_limit=60,
    )


def test_parse_bulk_new_sites_supports_delimiters_and_config_roundtrip(tmp_path):
    cfg_path = tmp_path / "config.ini"
    existing = [_make_existing_site(tmp_path)]

    text = "\n".join(
        [
            "# comment line",
            "站点名|网址或导入接口|分组|MCP_Token|后台账号|后台密码|站点名称",
            "|https://site-a.example.com/api/import/archive?token=imp-tok-1|医疗|mcp-1|adminA|passA|站A",
            "custom-b\tb.example.com\t金融\tmcp-2\tadminB\tpassB\t站B",
            "bare-c,http://c.example.com:8080,教育,,,,站C",
            "https://solo-d.example.com",
        ]
    )

    new_sites, warnings = parse_bulk_new_sites(text, existing, cfg_path)
    assert len(warnings) == 1
    assert "表头" in warnings[0]["reason"]
    assert len(new_sites) == 4

    s_a, s_b, s_c, s_d = new_sites
    assert s_a.name == "site-a-example-com"
    assert s_a.url == "https://site-a.example.com"
    assert s_a.jiekou == "/api/import/archive?token=imp-tok-1"
    assert s_a.site_group == "医疗"
    assert s_a.mcp_token == "mcp-1"
    assert s_a.admin_username == "adminA"
    assert s_a.admin_password == "passA"
    assert s_a.site_title == "站A"
    assert s_a.fid == 0
    assert s_a.enabled is False
    assert s_a.image_file is None

    assert s_b.name == "custom-b"
    assert s_b.url == "https://b.example.com"
    assert s_b.jiekou == "/api/import/archive?token=mcp-2"

    assert s_c.name == "bare-c"
    assert s_c.url == "http://c.example.com:8080"
    assert s_c.jiekou == ""
    assert s_c.site_title == "站C"

    assert s_d.name == "solo-d-example-com"
    assert s_d.url == "https://solo-d.example.com"
    assert s_d.jiekou == ""

    # Must save and reload cleanly via config_service with fid=0, enabled=False, image_file=None.
    save_config(cfg_path, existing + new_sites)
    loaded = load_config(cfg_path)
    assert len(loaded) == 5
    loaded_by_name = {s.name: s for s in loaded}
    assert loaded_by_name["bare-c"].fid == 0
    assert loaded_by_name["bare-c"].enabled is False
    assert loaded_by_name["bare-c"].image_file is None
    assert loaded_by_name["site-a-example-com"].site_title == "站A"


def test_parse_bulk_new_sites_deduplicates_and_redacts_secrets_in_warnings(tmp_path):
    cfg_path = tmp_path / "config.ini"
    existing = [_make_existing_site(tmp_path, name="alpha", url="https://www.alpha.example.com")]

    text = "\n".join(
        [
            "|http://alpha.example.com|g1|secret-mcp-1|admin|secret-pwd-1|DupOrigin",
            "alpha|https://other.example.com|g1|secret-mcp-2|admin|secret-pwd-2|DupName",
            "ok-one|https://one.example.com|g1",
            "|https://one.example.com|g1",
            "bad-url|not a valid url||tok|admin|secret-pwd-3|",
            "CON|https://con.example.com",
            "too-long-group|https://group.example.com|" + ("x" * 65),
        ]
    )

    new_sites, warnings = parse_bulk_new_sites(text, existing, cfg_path)
    assert [s.name for s in new_sites] == ["ok-one"]
    assert len(warnings) == 6
    joined_warnings = " ".join(str(w) for w in warnings)
    assert "secret-pwd-1" not in joined_warnings
    assert "secret-pwd-2" not in joined_warnings
    assert "secret-pwd-3" not in joined_warnings
    assert "secret-mcp-1" not in joined_warnings


def test_auto_discover_sites_utf8_gbk_friendlinks_and_mcp(tmp_path):
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

        def do_GET(self):  # noqa: N802
            if self.path == "/":
                ua = self.headers.get("User-Agent", "")
                assert "Mozilla/5.0" in ua
                html = "<html><head><title>  测试UTF8站点 &amp; 门户  </title></head><body>ok</body></html>"
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if self.path.startswith("/api/friendlink/list"):
                data = json.dumps(
                    {"code": 0, "data": [{"id": 1, "link": "https://p1.example.com"}, {"id": 2, "link": "https://p2.example.com"}]}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    class _GbkHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

        def do_GET(self):  # noqa: N802
            if self.path == "/":
                html = "<html><head><meta charset='gbk'><title>中文GBK站点</title></head><body>hi</body></html>"
                body = html.encode("gbk")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=gbk")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    srv1 = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv2 = ThreadingHTTPServer(("127.0.0.1", 0), _GbkHandler)
    t1 = threading.Thread(target=srv1.serve_forever, daemon=True)
    t2 = threading.Thread(target=srv2.serve_forever, daemon=True)
    t1.start()
    t2.start()

    try:
        s1 = _make_existing_site(tmp_path, name="utf8-site", url=f"http://127.0.0.1:{srv1.server_address[1]}")
        s2 = _make_existing_site(tmp_path, name="gbk-site", url=f"http://127.0.0.1:{srv2.server_address[1]}")
        s2.jiekou = ""
        s2.site_title = "保留原标题"
        s3 = _make_existing_site(tmp_path, name="down-site", url="http://127.0.0.1:1")
        s3.jiekou = ""

        def _fake_probe(site):
            return McpProbeResult(
                site_name=site.name,
                ok=(site.name == "utf8-site"),
                tools=(McpToolInfo(name="check_update"), McpToolInfo(name="friendlink_list")),
            )

        events: list[tuple[int, int, str]] = []
        results = auto_discover_sites(
            [s1, s2, s3],
            max_concurrency=3,
            probe_fn=_fake_probe,
            timeout=1.5,
            progress=lambda done, total, r: events.append((done, total, r.site_name)),
        )

        assert len(results) == 3
        assert len(events) == 3
        r1, r2, r3 = results

        assert isinstance(r1, SiteDiscoveryResult)
        assert r1.reachable is True
        assert r1.discovered_title == "测试UTF8站点 & 门户"
        assert r1.site_title == "测试UTF8站点 & 门户"
        assert s1.site_title == "测试UTF8站点 & 门户"
        assert r1.link_count == 2
        assert r1.mcp_ok is True
        assert r1.mcp_tools == ("check_update", "friendlink_list")

        assert r2.reachable is True
        assert r2.discovered_title == "中文GBK站点"
        assert r2.site_title == "保留原标题"
        assert s2.site_title == "保留原标题"
        assert r2.link_count is None
        assert r2.mcp_ok is False

        assert r3.reachable is False
        assert r3.http_status is None

        # Single-site helper with cancellation
        cancel = threading.Event()
        cancel.set()
        cancelled = auto_discover_sites([s1, s2], cancel_event=cancel)
        assert cancelled == []

        single = auto_discover_site(s2, update_site=False)
        assert single.discovered_title == "中文GBK站点"
    finally:
        srv1.shutdown()
        srv1.server_close()
        srv2.shutdown()
        srv2.server_close()
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)
