"""Authentication and authorisation tests.

The point of these: an unauthenticated analytics endpoint is not deployable,
and a role system that quietly lets a viewer read the audit log is worse than
no role system at all.
"""
import pytest
from fastapi.testclient import TestClient

from app.core.security import KeyRing, Principal, verify_startup_config

ADMIN_KEY = "admin-key-000000000000000000"
ANALYST_KEY = "analyst-key-11111111111111111"
VIEWER_KEY = "viewer-key-2222222222222222222"
SPEC = (f"{ADMIN_KEY}:admin:alice,{ANALYST_KEY}:analyst:bob,{VIEWER_KEY}:viewer:dashboard")


@pytest.fixture
def secured_client(warehouse, retriever, tmp_path, monkeypatch):
    import app.core.security as security
    import app.pipeline.orchestrator as orch
    from app.core.audit import AuditLog, set_audit_log
    from app.core.config import settings
    from app.main import app as fastapi_app
    from app.pipeline.executor import DuckDBExecutor
    from app.pipeline.orchestrator import Analyst

    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "api_keys", SPEC)
    monkeypatch.setattr(settings, "audit_enabled", True)
    security.reset_keyring()
    set_audit_log(AuditLog(str(tmp_path / "audit.sqlite")))
    orch._analyst = Analyst(executor=DuckDBExecutor(path=warehouse),
                            retriever=retriever, use_llm=False)
    with TestClient(fastapi_app) as c:
        yield c
    security.reset_keyring()
    set_audit_log(None)


def _h(key):
    return {"X-API-Key": key}


# ---------------------------------------------------------------- keyring
def test_keyring_parses_roles_and_names():
    kr = KeyRing(SPEC)
    assert len(kr) == 3
    assert kr.resolve(ADMIN_KEY) == Principal("alice", "admin")
    assert kr.resolve(VIEWER_KEY) == Principal("dashboard", "viewer")
    assert kr.resolve("nope") is None


def test_keyring_never_stores_plaintext():
    kr = KeyRing(SPEC)
    assert ADMIN_KEY not in repr(kr.__dict__)
    assert all(len(d) == 64 for d in kr._keys)  # sha256 hex digests


def test_keyring_rejects_short_and_malformed_entries():
    assert len(KeyRing("tiny:admin:x")) == 0          # too short to be a secret
    assert len(KeyRing("k" * 20 + ":wizard:x")) == 0  # unknown role
    assert len(KeyRing("no-role-here")) == 0


def test_describe_does_not_leak_keys():
    described = KeyRing(SPEC).describe()
    assert {"name": "alice", "role": "admin"} in described
    assert ADMIN_KEY not in str(described)


def test_role_hierarchy():
    assert Principal("a", "admin").can("viewer")
    assert Principal("a", "admin").can("admin")
    assert Principal("b", "analyst").can("viewer")
    assert not Principal("b", "analyst").can("admin")
    assert not Principal("c", "viewer").can("analyst")


def test_startup_fails_closed_when_auth_on_without_keys(monkeypatch):
    import app.core.security as security
    from app.core.config import settings

    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "api_keys", "")
    security.reset_keyring()
    with pytest.raises(RuntimeError, match="Refusing to start"):
        verify_startup_config()
    security.reset_keyring()


# ---------------------------------------------------------------- endpoints
def test_health_stays_public(secured_client):
    r = secured_client.get("/health")
    assert r.status_code == 200
    assert r.json()["auth_enabled"] is True


def test_ask_requires_a_key(secured_client):
    r = secured_client.post("/ask", json={"question": "revenue by region"})
    assert r.status_code == 401
    assert "Missing X-API-Key" in r.json()["detail"]


def test_ask_rejects_a_bad_key(secured_client):
    r = secured_client.post("/ask", json={"question": "revenue by region"},
                            headers=_h("wrong-key-aaaaaaaaaaaaaaaaaa"))
    assert r.status_code == 401


def test_viewer_can_ask(secured_client):
    r = secured_client.post("/ask", json={"question": "revenue by region",
                                          "use_llm": False}, headers=_h(VIEWER_KEY))
    assert r.status_code == 200 and r.json()["status"] == "answered"


def test_viewer_cannot_use_the_validator(secured_client):
    r = secured_client.post("/validate-sql", json={"sql": "SELECT 1"},
                            headers=_h(VIEWER_KEY))
    assert r.status_code == 403
    assert "requires the 'analyst' role" in r.json()["detail"]


def test_analyst_can_use_the_validator(secured_client):
    r = secured_client.post(
        "/validate-sql",
        json={"sql": "SELECT fct_orders.region, SUM(fct_orders.net_revenue) AS total_revenue "
                     "FROM fct_orders GROUP BY fct_orders.region LIMIT 5"},
        headers=_h(ANALYST_KEY))
    assert r.status_code == 200 and r.json()["ok"] is True


@pytest.mark.parametrize("path", ["/audit", "/audit/stats", "/principals"])
def test_audit_endpoints_are_admin_only(secured_client, path):
    assert secured_client.get(path, headers=_h(VIEWER_KEY)).status_code == 403
    assert secured_client.get(path, headers=_h(ANALYST_KEY)).status_code == 403
    assert secured_client.get(path, headers=_h(ADMIN_KEY)).status_code == 200


def test_whoami_reports_identity(secured_client):
    body = secured_client.get("/whoami", headers=_h(ANALYST_KEY)).json()
    assert body == {"name": "bob", "role": "analyst", "authenticated": True}


def test_principals_endpoint_never_returns_keys(secured_client):
    body = secured_client.get("/principals", headers=_h(ADMIN_KEY)).text
    for key in (ADMIN_KEY, ANALYST_KEY, VIEWER_KEY):
        assert key not in body


def test_auth_disabled_keeps_local_dev_open(client):
    """The default posture: no keys, everything works, health says so."""
    assert client.get("/health").json()["auth_enabled"] is False
    assert client.post("/ask", json={"question": "revenue by region",
                                     "use_llm": False}).status_code == 200


# ---------------------------------------------------------------- secure defaults
def test_anonymous_is_not_admin_by_default():
    """Regression: 'auth disabled' once meant 'the internet is an admin'.

    The live deployment served /audit, /principals and the semantic-layer
    editor to anyone who found the URL.
    """
    from app.core.config import Settings

    assert Settings().anonymous_role == "analyst"


@pytest.fixture
def open_client(warehouse, retriever, tmp_path, monkeypatch):
    """Auth disabled, defaults untouched - i.e. the public-demo posture."""
    import app.core.security as security
    import app.pipeline.orchestrator as orch
    from app.core.audit import AuditLog, set_audit_log
    from app.core.config import settings
    from app.main import app as fastapi_app
    from app.pipeline.executor import DuckDBExecutor
    from app.pipeline.orchestrator import Analyst

    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "anonymous_role", "analyst")  # the default
    security.reset_keyring()
    set_audit_log(AuditLog(str(tmp_path / "a.sqlite")))
    orch._analyst = Analyst(executor=DuckDBExecutor(path=warehouse),
                            retriever=retriever, use_llm=False)
    with TestClient(fastapi_app) as c:
        yield c
    set_audit_log(None)


def test_open_deployment_still_answers_questions(open_client):
    r = open_client.post("/ask", json={"question": "revenue by region",
                                       "use_llm": False})
    assert r.status_code == 200 and r.json()["status"] == "answered"


@pytest.mark.parametrize("path", ["/audit", "/audit/stats", "/principals",
                                  "/semantic-layer/raw"])
def test_open_deployment_hides_admin_surfaces(open_client, path):
    """What a public URL must never hand out to a stranger."""
    assert open_client.get(path).status_code == 403


def test_anonymous_cannot_edit_the_layer_even_as_admin(warehouse, retriever,
                                                       tmp_path, monkeypatch):
    """Belt and braces: mutations need a key, whatever ANONYMOUS_ROLE says."""
    import app.core.security as security
    import app.pipeline.orchestrator as orch
    from app.core.config import settings
    from app.main import app as fastapi_app
    from app.pipeline.executor import DuckDBExecutor
    from app.pipeline.orchestrator import Analyst

    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "anonymous_role", "admin")
    security.reset_keyring()
    orch._analyst = Analyst(executor=DuckDBExecutor(path=warehouse),
                            retriever=retriever, use_llm=False)
    with TestClient(fastapi_app) as c:
        assert c.get("/audit").status_code == 200          # reading is allowed
        r = c.put("/semantic-layer/raw", json={"yaml": "entities: []"})
        assert r.status_code == 401                        # mutating is not
        assert "authenticated admin key" in r.json()["detail"]
        assert c.post("/semantic-layer/restore/x.yml").status_code == 401
