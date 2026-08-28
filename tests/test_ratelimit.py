"""Rate-limiting tests.

A public endpoint with no throttle is an invitation - to cost, to noise in the
audit log, and to trivial denial of service.
"""
import pytest
from fastapi.testclient import TestClient

from app.core.ratelimit import RateLimiter


def test_allows_up_to_the_limit_then_blocks():
    limiter = RateLimiter(3)
    assert [limiter.check("1.1.1.1")[0] for _ in range(5)] == [True, True, True, False, False]


def test_clients_are_independent():
    limiter = RateLimiter(1)
    assert limiter.check("a")[0] is True
    assert limiter.check("a")[0] is False
    assert limiter.check("b")[0] is True


def test_window_slides():
    limiter = RateLimiter(2)
    assert limiter.check("x", now=0.0)[0]
    assert limiter.check("x", now=1.0)[0]
    assert not limiter.check("x", now=2.0)[0]
    assert limiter.check("x", now=61.5)[0], "the window should have moved on"


def test_retry_after_is_sensible():
    limiter = RateLimiter(1)
    limiter.check("y", now=0.0)
    allowed, _, retry_after = limiter.check("y", now=10.0)
    assert not allowed
    assert 1 <= retry_after <= 61


def test_zero_disables_the_limiter():
    limiter = RateLimiter(0)
    assert not limiter.enabled
    assert all(limiter.check("z")[0] for _ in range(500))


def test_memory_is_bounded():
    """A limiter that grows one entry per hostile IP is its own denial of service."""
    limiter = RateLimiter(5, max_clients=50)
    for i in range(500):
        limiter.check(f"10.0.0.{i}")
    assert len(limiter._hits) <= 50


@pytest.fixture
def throttled_client(warehouse, retriever, tmp_path, monkeypatch):
    import app.main as main_mod
    import app.pipeline.orchestrator as orch
    from app.core.audit import AuditLog, set_audit_log
    from app.core.config import settings
    from app.pipeline.executor import DuckDBExecutor
    from app.pipeline.orchestrator import Analyst

    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "rate_limit_per_minute", 3)
    monkeypatch.setattr(main_mod, "limiter", RateLimiter(3))
    set_audit_log(AuditLog(str(tmp_path / "a.sqlite")))
    orch._analyst = Analyst(executor=DuckDBExecutor(path=warehouse),
                            retriever=retriever, use_llm=False)
    with TestClient(main_mod.app) as c:
        yield c
    set_audit_log(None)


def test_ask_is_throttled(throttled_client):
    payload = {"question": "revenue by region", "use_llm": False}
    codes = [throttled_client.post("/ask", json=payload).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3:] == [429, 429]


def test_throttled_response_is_helpful(throttled_client):
    payload = {"question": "revenue by region", "use_llm": False}
    for _ in range(4):
        r = throttled_client.post("/ask", json=payload)
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    assert "per minute" in r.json()["detail"]


def test_health_is_never_throttled(throttled_client):
    """Platform health checks must not be rate limited into a false outage."""
    for _ in range(30):
        assert throttled_client.get("/health").status_code == 200


def test_remaining_header_is_exposed(throttled_client):
    r = throttled_client.post("/ask", json={"question": "revenue by region",
                                            "use_llm": False})
    assert r.headers["X-RateLimit-Limit"] == "3"
    assert int(r.headers["X-RateLimit-Remaining"]) == 2
