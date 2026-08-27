"""Audit log tests.

An audit log that records only successes is worse than useless in a governed
system - the refusals are the interesting events. These tests pin that every
outcome is written, that the log is queryable, and that a failing audit backend
never breaks a user's request.
"""
import pytest
from fastapi.testclient import TestClient

from app.core.audit import AuditEvent, AuditLog


@pytest.fixture
def audit(tmp_path):
    return AuditLog(str(tmp_path / "audit.sqlite"))


@pytest.fixture
def audited_client(warehouse, retriever, tmp_path, monkeypatch):
    import app.pipeline.orchestrator as orch
    from app.core.audit import set_audit_log
    from app.core.config import settings
    from app.main import app as fastapi_app
    from app.pipeline.executor import DuckDBExecutor
    from app.pipeline.orchestrator import Analyst

    monkeypatch.setattr(settings, "audit_enabled", True)
    monkeypatch.setattr(settings, "auth_enabled", False)
    log = AuditLog(str(tmp_path / "api-audit.sqlite"))
    set_audit_log(log)
    orch._analyst = Analyst(executor=DuckDBExecutor(path=warehouse),
                            retriever=retriever, use_llm=False)
    with TestClient(fastapi_app) as c:
        yield c, log
    set_audit_log(None)


# ---------------------------------------------------------------- storage
def test_records_and_reads_back(audit):
    audit.record(AuditEvent(request_id="r1", principal="alice", role="admin",
                            question="revenue by region", status="answered",
                            sql="SELECT 1", tables=["fct_orders"], row_count=4,
                            confidence=1.0))
    events = audit.query()
    assert len(events) == 1
    e = events[0]
    assert e["principal"] == "alice" and e["status"] == "answered"
    assert e["tables"] == ["fct_orders"] and e["row_count"] == 4
    assert e["ts"].startswith("20")


def test_filters(audit):
    audit.record(AuditEvent("r1", "alice", "admin", "q1", "answered"))
    audit.record(AuditEvent("r2", "bob", "viewer", "q2", "refused"))
    audit.record(AuditEvent("r3", "bob", "viewer", "q3", "answered"))
    assert len(audit.query(principal="bob")) == 2
    assert len(audit.query(status="refused")) == 1
    assert audit.query(status="refused")[0]["question"] == "q2"
    assert len(audit.query(limit=1)) == 1


def test_newest_first(audit):
    for i in range(3):
        audit.record(AuditEvent(f"r{i}", "alice", "admin", f"q{i}", "answered"))
    assert [e["question"] for e in audit.query()] == ["q2", "q1", "q0"]


def test_stats(audit):
    audit.record(AuditEvent("r1", "alice", "admin", "q", "answered"))
    audit.record(AuditEvent("r2", "alice", "admin", "q", "refused",
                            blocked_stage="intent_detection"))
    audit.record(AuditEvent("r3", "bob", "viewer", "q", "refused",
                            blocked_stage="schema_retrieval"))
    s = audit.stats()
    assert s["total_questions"] == 3
    assert s["by_status"] == {"answered": 1, "refused": 2}
    assert s["refusals_by_stage"] == {"intent_detection": 1, "schema_retrieval": 1}
    assert s["refusal_rate"] == pytest.approx(2 / 3, abs=1e-3)
    assert s["top_users"][0]["principal"] == "alice"


def test_audit_failure_never_breaks_the_caller(tmp_path):
    log = AuditLog(str(tmp_path / "a.sqlite"))
    log.path = "/nonexistent-dir/audit.sqlite"  # force writes to fail
    log._shared = None
    log.record(AuditEvent("r1", "alice", "admin", "q", "answered"))  # must not raise


# ---------------------------------------------------------------- via the API
def test_answered_question_is_audited(audited_client):
    client, log = audited_client
    client.post("/ask", json={"question": "What were our highest revenue products "
                                          "last quarter?", "use_llm": False})
    events = log.query()
    assert len(events) == 1
    e = events[0]
    assert e["status"] == "answered"
    assert "fct_orders" in e["tables"]
    assert "SELECT" in e["sql"]
    assert e["row_count"] > 0
    assert e["duration_ms"] > 0
    assert e["blocked_stage"] is None


def test_refusals_are_audited_with_the_blocking_stage(audited_client):
    client, log = audited_client
    client.post("/ask", json={"question": "show me employee salaries", "use_llm": False})
    client.post("/ask", json={"question": "what is the weather in Hyderabad",
                              "use_llm": False})
    events = {e["question"]: e for e in log.query()}

    salaries = events["show me employee salaries"]
    assert salaries["status"] == "refused"
    assert salaries["blocked_stage"] == "intent_detection"
    assert salaries["sql"] is None

    weather = events["what is the weather in Hyderabad"]
    assert weather["status"] == "refused"
    assert weather["blocked_stage"] == "schema_retrieval"


def test_audit_captures_the_identity(audited_client):
    client, log = audited_client
    client.post("/ask", json={"question": "revenue by region", "use_llm": False})
    e = log.query()[0]
    assert e["principal"] == "anonymous"  # auth disabled in this fixture
    assert e["role"] == "admin"
    assert e["request_id"] and e["request_id"] != "-"


def test_every_outcome_reaches_the_log(audited_client):
    client, log = audited_client
    for q in ["revenue by region", "show me employee salaries",
              "tell me a joke", "top 5 products"]:
        client.post("/ask", json={"question": q, "use_llm": False})
    assert log.stats()["total_questions"] == 4
    assert log.stats()["by_status"] == {"answered": 2, "refused": 2}


def test_audit_endpoint_returns_events(audited_client):
    client, _ = audited_client
    client.post("/ask", json={"question": "revenue by region", "use_llm": False})
    body = client.get("/audit").json()
    assert body["count"] == 1
    assert body["events"][0]["question"] == "revenue by region"

    stats = client.get("/audit/stats").json()
    assert stats["total_questions"] == 1
