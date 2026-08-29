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


def test_bare_host_port_gets_a_scheme():
    """Render injects 'host:port' with no scheme; requests rejects that outright."""
    from ui.api_client import normalise_base_url

    assert normalise_base_url("ai-data-analyst-api:10000") == "http://ai-data-analyst-api:10000"
    assert normalise_base_url("http://x:1") == "http://x:1"
    assert normalise_base_url("https://x.onrender.com/") == "https://x.onrender.com"
    assert normalise_base_url("") == ""


def test_schemeless_url_raises_apierror_not_a_traceback():
    client = ApiClient("does-not-resolve-xyz:10000", timeout=5)
    with pytest.raises(ApiError):
        client.health()


# ---------------------------------------------------------------- fallback
@pytest.mark.parametrize("url,expected", [
    ("ai-data-analyst-api-krvg:10000", ["https://ai-data-analyst-api-krvg.onrender.com"]),
    ("http://ai-data-analyst-api-krvg:10000", ["https://ai-data-analyst-api-krvg.onrender.com"]),
    ("http://localhost:8000", []),      # local dev is never rewritten
    ("http://api:8000", []),            # docker compose service name
    ("https://real.example.com", []),   # a real hostname is never second-guessed
])
def test_fallback_candidates(url, expected):
    from ui.api_client import fallback_base_urls

    assert fallback_base_urls(url) == expected


def test_unreachable_internal_address_falls_back_to_a_working_one(monkeypatch):
    """Render's private hostport does not resolve on free instances.

    Rather than leave the UI dead until someone edits an environment variable,
    the client tries the public equivalent once and keeps it if it works.
    """
    import ui.api_client as mod

    healthy = {"status": "ok", "warehouse": "duckdb", "llm": "offline-rules",
               "entities": 3, "metrics": 7}
    srv, url = _serve(_json_handler(200, healthy))
    public = url.rsplit("/", 0)[0]

    def fake_candidates(_):
        return [public]

    monkeypatch.setattr(mod, "fallback_base_urls", fake_candidates)
    try:
        client = mod.ApiClient("unreachable-internal-name:10000", timeout=3)
        assert client.health() == healthy
        assert client.base_url == public, "the working URL should stick for the session"
    finally:
        srv.shutdown()


def test_fallback_is_attempted_only_once(monkeypatch):
    import ui.api_client as mod

    calls = []

    def fake_candidates(u):
        calls.append(u)
        return []

    monkeypatch.setattr(mod, "fallback_base_urls", fake_candidates)
    client = mod.ApiClient("unreachable-internal-name:10000", timeout=2)
    for _ in range(3):
        with pytest.raises(ApiError):
            client.health()
    assert len(calls) == 1, "a dead fallback must not be retried on every request"


def test_sleeping_service_is_retried_before_giving_up(monkeypatch):
    """A sleeping free-tier host answers nothing at all, not a 502.

    The first probe therefore fails on exactly the occasion the fallback is
    most needed, so one attempt was never enough.
    """
    import requests

    import ui.api_client as mod

    attempts = {"n": 0}
    healthy = {"status": "ok", "warehouse": "duckdb", "llm": "offline-rules",
               "entities": 3, "metrics": 7}
    srv, url = _serve(_json_handler(200, healthy))
    real_get = requests.get

    def flaky_get(u, **kw):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise requests.exceptions.ConnectionError("asleep")
        return real_get(u, **kw)

    monkeypatch.setattr(mod, "fallback_base_urls", lambda _: [url])
    monkeypatch.setattr(mod.requests, "get", flaky_get)
    try:
        client = mod.ApiClient("unreachable-internal:10000", timeout=5)
        assert client.health() == healthy
        assert attempts["n"] >= 2, "the probe must retry a sleeping host"
    finally:
        srv.shutdown()


def test_any_http_response_proves_the_host_exists(monkeypatch):
    """A 502 from a waking instance still means the hostname routes."""
    import ui.api_client as mod

    srv, bad_url = _serve(_json_handler(502, {"detail": "waking"}))
    try:
        monkeypatch.setattr(mod, "fallback_base_urls", lambda _: [bad_url])
        client = mod.ApiClient("unreachable-internal:10000", timeout=5)
        assert client._resolve_fallback() == bad_url
    finally:
        srv.shutdown()


def test_hosted_platforms_mention_cold_starts():
    """Advice must fit where the app runs: a Render operator cannot run uvicorn."""
    client = ApiClient("https://does-not-resolve-xyz.onrender.com", timeout=3)
    assert "sleep after inactivity" in client._unreachable_message()
    assert "uvicorn" in ApiClient("http://localhost:9999")._unreachable_message()
    assert "API_URL is probably wrong" in ApiClient(
        "http://some-internal-host:10000")._unreachable_message()


# ------------------------------------------------------------- cold starts
class _FakeResp:
    def __init__(self, status: int, payload=None, text: str = ""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


def test_waking_hosted_service_is_retried(monkeypatch):
    """Render's load balancer answers 502 while a sleeping instance wakes.

    The first hit keeps the boot in flight; a couple of retries later the
    same request goes through instead of the UI declaring 'Not connected'.
    """
    import ui.api_client as mod

    healthy = {"status": "ok", "warehouse": "duckdb", "llm": "offline-rules",
               "entities": 3, "metrics": 7}
    calls = {"n": 0}
    sleeps: list[float] = []

    def fake_request(method, url, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            return _FakeResp(502, text="<html>502</html>")
        return _FakeResp(200, payload=healthy)

    monkeypatch.setattr(mod.requests, "request", fake_request)
    monkeypatch.setattr(mod.time, "sleep", sleeps.append)
    assert ApiClient("https://waking-api.onrender.com", timeout=5).health()["status"] == "ok"
    assert calls["n"] == 3, "a waking instance should get a few chances"
    assert len(sleeps) == 2, "retries should be spaced, not hammered"


def test_persistently_waking_service_says_waking_not_broken(monkeypatch):
    """If the instance never wakes, say so - never dump the 502 HTML page."""
    import ui.api_client as mod

    calls = {"n": 0}

    def fake_request(method, url, **kw):
        calls["n"] += 1
        return _FakeResp(502, text="<html><title>502</title></html>")

    monkeypatch.setattr(mod.requests, "request", fake_request)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    with pytest.raises(ApiError) as exc:
        ApiClient("https://waking-api.onrender.com", timeout=5).health()
    assert calls["n"] == 3, "the retry budget should be used up"
    msg = str(exc.value)
    assert "waking up" in msg
    assert "Retry connection" in msg
    assert "<html" not in msg


def test_local_502_is_not_a_cold_start(monkeypatch):
    """A local server returning 502 is a real error - no sleeping around."""
    import ui.api_client as mod

    calls = {"n": 0}

    def fake_request(method, url, **kw):
        calls["n"] += 1
        return _FakeResp(502, payload={"detail": "bad upstream"})

    monkeypatch.setattr(mod.requests, "request", fake_request)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    with pytest.raises(ApiError) as exc:
        ApiClient("http://localhost:8000", timeout=5).health()
    assert calls["n"] == 1
    assert "HTTP 502" in str(exc.value)
