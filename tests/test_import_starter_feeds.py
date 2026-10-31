"""docker/import_starter_feeds.py: フィードが 0 件のときだけ取り込み、失敗は警告に倒す。"""

from __future__ import annotations

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "docker" / "import_starter_feeds.py"
OPML = SCRIPT.parent / "feeds.opml"

spec = importlib.util.spec_from_file_location("import_starter_feeds", SCRIPT)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class FakeMiniflux:
    def __init__(self, feeds):
        self.feeds = feeds
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, payload, status=200):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append((self.command, self.path, self.headers.get("X-Auth-Token"), body))
                if self.path == "/v1/feeds":
                    self._reply(outer.feeds)
                elif self.path == "/v1/import":
                    outer.feeds = [{"id": 1}, {"id": 2}]
                    self._reply({"message": "Feeds imported successfully"}, 201)
                elif self.path.startswith("/v1/entries"):
                    self._reply({"total": 3, "entries": []})
                else:
                    self._reply({}, 204)

            do_GET = do_POST = do_PUT = _handle

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def miniflux():
    servers = []

    def make(feeds):
        server = FakeMiniflux(feeds)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def test_imports_when_user_has_no_feeds(miniflux, tmp_path):
    server = miniflux([])
    marker = tmp_path / "marker"
    helper.import_starter_feeds(server.url, "tok", OPML, marker, wait_seconds=5)
    imports = [r for r in server.requests if r[1] == "/v1/import"]
    assert len(imports) == 1
    assert imports[0][2] == "tok"
    assert b"publickey1.jp" in imports[0][3]
    assert marker.exists()


def test_does_nothing_when_feeds_exist(miniflux, tmp_path):
    server = miniflux([{"id": 9}])
    marker = tmp_path / "marker"
    helper.import_starter_feeds(server.url, "tok", OPML, marker, wait_seconds=5)
    assert not [r for r in server.requests if r[1] == "/v1/import"]
    assert marker.exists()


def test_failure_is_only_a_warning(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINIFLUX_API_KEY", "tok")
    monkeypatch.setenv("MINIFLUX_BASE_URL", "http://127.0.0.1:1")
    assert helper.main(["x", str(OPML), str(tmp_path / "marker")]) == 0
    assert "失敗しました" in capsys.readouterr().err
    assert not (tmp_path / "marker").exists()


def test_opml_has_valid_feed_urls():
    import xml.etree.ElementTree as ET

    urls = [o.get("xmlUrl") for o in ET.parse(OPML).iter("outline") if o.get("xmlUrl")]
    assert len(urls) == 5
    assert all(u.startswith("https://") for u in urls)


def test_returns_false_when_first_entries_do_not_arrive(miniflux, tmp_path):
    server = miniflux([])
    # entries を常に空にして待ちきれないケースを作る
    original = helper._request

    def fake(method, url, token, body=None, content_type="application/json"):
        if url.endswith("/v1/entries?status=unread&limit=1"):
            return {"total": 0}
        return original(method, url, token, body, content_type)

    helper._request = fake
    try:
        assert helper.import_starter_feeds(server.url, "tok", OPML, tmp_path / "m", wait_seconds=1) is False
    finally:
        helper._request = original
