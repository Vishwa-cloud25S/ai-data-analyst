"""API client tests.

The silent-404 failure (sidebar cheerfully reporting "API healthy" while every
field was blank) happened because the client checked the JSON body for an
"error" key instead of the HTTP status. These tests pin the failure paths
against real servers.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ui.api_client import ApiClient, ApiError


def _serve(handler_cls):
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def _json_handler(status: int, payload):
    class H(BaseHTTPRequestHandler):
        def _send(self):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = lambda self: self._send()  # noqa: E731

        def log_message(self, *a):
            pass
    return H


def test_json_404_raises_instead_of_looking_healthy():
    """The exact bug: another service on the port answering JSON 404."""
    srv, url = _serve(_json_handler(404, {"detail": "Not Found"}))
    try:
        with pytest.raises(ApiError) as exc:
            ApiClient(url).health()
        msg = str(exc.value)
        assert "404" in msg
        assert "not the AI Data Analyst API" in msg
    finally:
        srv.shutdown()


def test_wrong_shape_200_is_rejected():
    """A 200 from some other JSON API must not be accepted as health."""
    srv, url = _serve(_json_handler(200, {"hello": "world"}))
    try:
        with pytest.raises(ApiError, match="not from this API"):
            ApiClient(url).health()
    finally:
        srv.shutdown()


def test_server_error_is_reported():
    srv, url = _serve(_json_handler(500, {"detail": "boom"}))
    try:
        with pytest.raises(ApiError, match="HTTP 500"):
            ApiClient(url).get("/health")
    finally:
        srv.shutdown()


def test_non_json_response_is_reported():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"<html>hello</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv, url = _serve(H)
    try:
        with pytest.raises(ApiError, match="did not return JSON"):
            ApiClient(url).get("/health")
    finally:
        srv.shutdown()


def test_connection_refused_is_actionable():
    with pytest.raises(ApiError) as exc:
        ApiClient("http://127.0.0.1:1", timeout=2).health()
    assert "Is it running?" in str(exc.value)
    assert "uvicorn app.main:app" in str(exc.value)


def test_healthy_api_passes(live_api_url):
    data = ApiClient(live_api_url).health()
    assert data["status"] == "ok"
    assert data["entities"] >= 3 and data["metrics"] >= 5


def test_ask_against_live_api(live_api_url):
    res = ApiClient(live_api_url).ask(
        "What were our highest revenue products last quarter?", use_llm=False
    )
    assert res["status"] == "answered"
    assert res["row_count"] > 0
