"""Local-LLM tests.

These run a real OpenAI-compatible HTTP server in-process, so the client is
exercised end to end without mocking the SDK. The awkward-server cases are
modelled on how local models actually misbehave: rejecting `response_format`,
fencing JSON in markdown, and padding it with prose.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.llm.client import LLMClient, extract_json


def make_server(handler_factory):
    srv = HTTPServer(("127.0.0.1", 0), handler_factory)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/v1"


def chat_server(reply: str, *, allow_json_mode: bool = True, record: list | None = None):
    """A minimal /v1/chat/completions implementation."""

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or "{}")
            if record is not None:
                record.append(body)

            if "response_format" in body and not allow_json_mode:
                payload = {"error": {"message": "response_format is not supported"}}
                data = json.dumps(payload).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            payload = {
                "id": "chatcmpl-test", "object": "chat.completion", "created": 0,
                "model": body.get("model", "local-model"),
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": reply}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    return H


# ---------------------------------------------------------------- extraction
@pytest.mark.parametrize("text,expected", [
    ('{"sql":"SELECT 1"}', {"sql": "SELECT 1"}),
    ('```json\n{"sql":"SELECT 1"}\n```', {"sql": "SELECT 1"}),
    ('```\n{"sql":"SELECT 1"}\n```', {"sql": "SELECT 1"}),
    ('Sure! Here is the query: {"sql":"SELECT 1"} Let me know.', {"sql": "SELECT 1"}),
    ('  \n {"a": {"b": 2}} \n ', {"a": {"b": 2}}),
])
def test_extract_json_handles_local_model_habits(text, expected):
    assert extract_json(text) == expected


@pytest.mark.parametrize("text", ["", "no json at all", "[1,2,3]", "{broken"])
def test_extract_json_returns_none_when_hopeless(text):
    assert extract_json(text) is None


# ---------------------------------------------------------------- provider wiring
def test_no_configuration_means_offline():
    c = LLMClient(api_key=None, base_url="")
    assert not c.available
    assert c.provider == "none"
    assert c.describe() == "offline-rules"


def test_base_url_selects_the_local_provider():
    c = LLMClient(api_key=None, base_url="http://localhost:11434/v1", model="llama3.1")
    assert c.available
    assert c.provider == "local"
    assert c.describe() == "llama3.1 (local)"


def test_api_key_alone_selects_openai():
    c = LLMClient(api_key="sk-test", base_url="", model="gpt-4o-mini")
    assert c.provider == "openai"
    assert c.describe() == "gpt-4o-mini"


def test_local_provider_wins_over_a_stray_openai_key():
    """Data residency: if a local endpoint is configured, nothing goes to OpenAI."""
    c = LLMClient(api_key="sk-should-not-be-used",
                  base_url="http://localhost:11434/v1", model="mistral")
    assert c.provider == "local"
    assert str(c._client.base_url).startswith("http://localhost:11434")


# ---------------------------------------------------------------- live local server
def test_json_against_a_local_server():
    srv, url = make_server(chat_server('{"sql":"SELECT 1","rationale":"r","chart":"bar"}'))
    try:
        c = LLMClient(api_key="x", base_url=url, model="local-model")
        r = c.complete_json("sys", "user", fallback={"sql": "FALLBACK"})
        assert r.data["sql"] == "SELECT 1"
        assert r.offline is False
    finally:
        srv.shutdown()


def test_server_rejecting_json_mode_is_retried_without_it():
    """Ollama and llama.cpp commonly reject response_format."""
    seen: list = []
    srv, url = make_server(chat_server('```json\n{"sql":"SELECT 2"}\n```',
                                       allow_json_mode=False, record=seen))
    try:
        c = LLMClient(api_key="x", base_url=url, model="local-model")
        r = c.complete_json("sys", "user", fallback={"sql": "FALLBACK"})
        assert r.data["sql"] == "SELECT 2"          # markdown fence survived
        assert any("response_format" in b for b in seen)      # first attempt
        assert any("response_format" not in b for b in seen)  # retry
        assert c._json_mode_supported is False

        # The negotiation is remembered: no second failed attempt.
        seen.clear()
        c.complete_json("sys", "user", fallback={})
        assert all("response_format" not in b for b in seen)
    finally:
        srv.shutdown()


def test_unparseable_reply_falls_back_to_the_planner():
    srv, url = make_server(chat_server("I'm afraid I can't do that."))
    try:
        c = LLMClient(api_key="x", base_url=url, model="local-model")
        r = c.complete_json("sys", "user", fallback={"sql": "PLANNER"})
        assert r.data["sql"] == "PLANNER"
        assert r.offline is True
    finally:
        srv.shutdown()


def test_unreachable_local_server_falls_back():
    c = LLMClient(api_key="x", base_url="http://127.0.0.1:1/v1", model="local-model")
    r = c.complete_json("sys", "user", fallback={"sql": "PLANNER"})
    assert r.data["sql"] == "PLANNER" and r.offline is True


def test_complete_text_against_a_local_server():
    srv, url = make_server(chat_server("Revenue rose 12% last quarter."))
    try:
        c = LLMClient(api_key="x", base_url=url, model="local-model")
        assert c.complete_text("sys", "user", fallback="fb").startswith("Revenue rose")
    finally:
        srv.shutdown()


def test_full_pipeline_runs_against_a_local_model(warehouse, retriever, monkeypatch):
    """The whole governed pipeline, with a local model and no internet."""
    import app.llm.client as llm_mod
    from app.pipeline.executor import DuckDBExecutor
    from app.pipeline.orchestrator import Analyst

    sql = ("SELECT dim_products.product_name AS product_name, "
           "SUM(fct_orders.net_revenue) AS total_revenue "
           "FROM main.fct_orders AS fct_orders "
           "LEFT JOIN main.dim_products AS dim_products "
           "ON fct_orders.product_id = dim_products.product_id "
           "WHERE fct_orders.order_status NOT IN ('cancelled','returned') "
           "GROUP BY dim_products.product_name ORDER BY total_revenue DESC LIMIT 5")
    reply = json.dumps({"sql": sql, "rationale": "local model", "chart": "bar"})
    srv, url = make_server(chat_server(reply, allow_json_mode=False))
    try:
        local = LLMClient(api_key="x", base_url=url, model="llama3.1")
        monkeypatch.setattr(llm_mod, "_default", local)
        analyst = Analyst(executor=DuckDBExecutor(path=warehouse),
                          retriever=retriever, use_llm=True)
        res = analyst.ask("What were our highest revenue products last quarter?")
        assert res.status == "answered"
        assert res.row_count == 5
        gen = next(s for s in res.trace if s.name == "sql_generation")
        assert gen.detail["source"] == "llama3.1"   # the local model wrote it
        val = next(s for s in res.trace if s.name == "sql_validation")
        assert val.status == "ok" and val.detail["checks"]["tables_allowed"]
    finally:
        srv.shutdown()
        llm_mod.reset_llm()


def test_local_model_sql_is_still_validated(warehouse, retriever, monkeypatch):
    """A local model is no more trusted than a hosted one."""
    import app.llm.client as llm_mod
    from app.pipeline.executor import DuckDBExecutor
    from app.pipeline.orchestrator import Analyst

    evil = json.dumps({"sql": "SELECT full_name, salary_usd FROM employee_salaries",
                       "rationale": "hijacked", "chart": "table"})
    srv, url = make_server(chat_server(evil))
    try:
        local = LLMClient(api_key="x", base_url=url, model="rogue-local")
        monkeypatch.setattr(llm_mod, "_default", local)
        analyst = Analyst(executor=DuckDBExecutor(path=warehouse),
                          retriever=retriever, use_llm=True)
        res = analyst.ask("What were our highest revenue products last quarter?")
        val = next(s for s in res.trace if s.name == "sql_validation")
        # First attempt rejected, deterministic planner used instead.
        assert val.detail["attempts"][0]["ok"] is False
        assert any("employee_salaries" in e for e in val.detail["attempts"][0]["errors"])
        assert res.status == "answered"
        assert "employee_salaries" not in (res.sql or "")
    finally:
        srv.shutdown()
        llm_mod.reset_llm()
