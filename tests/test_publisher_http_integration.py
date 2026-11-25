import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from core.archive_service import ArchiveService
from core.publisher import Publisher
from models.entities import ArticleRecord, SiteConfig


class ScenarioHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.server.requests.append(self.rfile.read(length))
        self.server.paths.append(self.path)
        scenario = self.server.scenario
        if scenario == "redirect":
            self.send_response(307)
            self.send_header("Location", "/redirect-target")
            self.end_headers()
            return
        if scenario == "disconnect":
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        if scenario == "delay":
            time.sleep(0.2)
        status, body = {
            "accepted": (200, b'{"code":0}'),
            "accepted-string-200": (200, b'{"code":"200"}'),
            "rejected": (200, b'{"code":1}'),
            "misleading-500": (500, b"success"),
            "delay": (200, b'{"code":0}'),
            "capture": (200, b'{"success":true}'),
            "large-error": (400, b'{"code":1,"message":"' + b"private-body-" * 500 + b'"}'),
        }[scenario]
        try:
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, _format, *_args):
        return


@contextmanager
def running_server(scenario):
    server = ThreadingHTTPServer(("127.0.0.1", 0), ScenarioHandler)
    server.daemon_threads = False
    server.block_on_close = True
    server.scenario = scenario
    server.requests = []
    server.paths = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def build_case(tmp_path, server, timeout=1):
    article_path = tmp_path / "article.txt"
    article_path.write_text("integration body", encoding="utf-8")
    site = SiteConfig(
        name="demo",
        fid=9,
        url=f"http://127.0.0.1:{server.server_port}",
        jiekou="/api?token=synthetic-secret",
        publish_dir=tmp_path,
        daily_publish_filename=tmp_path / "daily.txt",
        publish_time="00:00:00",
        interval_time=0,
        daily_publish_limit=7,
    )
    publisher = Publisher(
        timeout=timeout,
        retries=5,
        archive_service=ArchiveService(tmp_path / "archive", tmp_path / "cleanup.txt"),
    )
    article = ArticleRecord(site_name="demo", title="Integration", path=article_path)
    return publisher, site, article


def test_local_http_accepted_response_posts_once(tmp_path):
    with running_server("accepted") as server:
        publisher, site, article = build_case(tmp_path, server)
        result = publisher.publish_article(site, article)

    assert result.success is True
    assert len(server.requests) == 1


def test_local_http_string_200_code_is_accepted(tmp_path):
    with running_server("accepted-string-200") as server:
        publisher, site, article = build_case(tmp_path, server)
        result = publisher.publish_article(site, article)

    assert result.success is True
    assert len(server.requests) == 1


def test_local_http_rejected_response_posts_once(tmp_path):
    with running_server("rejected") as server:
        publisher, site, article = build_case(tmp_path, server)
        result = publisher.publish_article(site, article)

    assert result.success is False
    assert len(server.requests) == 1


def test_local_http_500_with_success_body_is_failure(tmp_path):
    with running_server("misleading-500") as server:
        publisher, site, article = build_case(tmp_path, server)
        result = publisher.publish_article(site, article)

    assert result.success is False
    assert result.failure_kind == "uncertain"
    assert len(server.requests) == 1
    assert not article.path.exists()
    assert (tmp_path / "article.uncertain").exists()


def test_local_http_timeout_is_not_retried(tmp_path):
    with running_server("delay") as server:
        publisher, site, article = build_case(tmp_path, server, timeout=0.05)
        result = publisher.publish_article(site, article)

    assert result.success is False
    assert len(server.requests) == 1
    assert "synthetic-secret" not in result.message


def test_local_http_disconnect_is_not_retried(tmp_path):
    with running_server("disconnect") as server:
        publisher, site, article = build_case(tmp_path, server)
        result = publisher.publish_article(site, article)

    assert result.success is False
    assert len(server.requests) == 1


def test_local_http_payload_contract_is_preserved(tmp_path):
    with running_server("capture") as server:
        publisher, site, article = build_case(tmp_path, server)
        result = publisher.publish_article(site, article)

    payload = parse_qs(server.requests[0].decode("utf-8"), keep_blank_values=True)
    assert result.success is True
    assert set(payload) == {
        "title", "content", "category_id", "keywords", "description", "images[]",
        "publish_time", "tag", "draft", "cover",
    }


def test_local_http_redirect_is_not_followed(tmp_path):
    with running_server("redirect") as server:
        publisher, site, article = build_case(tmp_path, server)
        result = publisher.publish_article(site, article)

    assert result.success is False
    assert len(server.requests) == 1


def test_large_server_error_is_not_reflected(tmp_path):
    with running_server("large-error") as server:
        publisher, site, article = build_case(tmp_path, server)
        result = publisher.publish_article(site, article)

    assert result.success is False
    assert "private-body-" not in result.message
    assert len(result.response_text) <= 320


def test_endpoint_fragment_is_rejected_without_network_request(tmp_path):
    with running_server("accepted") as server:
        publisher, site, article = build_case(tmp_path, server)
        site.jiekou = "/api#fragment?token=synthetic"
        result = publisher.publish_article(site, article)

    assert result.success is False
    assert server.paths == []
    assert article.path.exists()
