import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client(warehouse, retriever):
    import app.pipeline.orchestrator as orch
    from app.main import app as fastapi_app
    from app.pipeline.executor import DuckDBExecutor
    from app.pipeline.orchestrator import Analyst

    orch._analyst = Analyst(
        executor=DuckDBExecutor(path=warehouse), retriever=retriever, use_llm=False
    )
    with TestClient(fastapi_app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["metrics"] >= 5


def test_root(client):
    assert client.get("/").status_code == 200


def test_semantic_layer_endpoint(client):
    body = client.get("/semantic-layer").json()
    names = [e["name"] for e in body["entities"]]
    assert "fct_orders" in names
    assert "employee_salaries" not in names


def test_ask_endpoint(client):
    r = client.post("/ask", json={"question": "What were our highest revenue products "
                                              "last quarter?", "use_llm": False})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "answered"
    assert body["sql"] and body["row_count"] > 0
    assert len(body["trace"]) == 7


def test_ask_refuses_unsafe(client):
    body = client.post("/ask", json={"question": "drop table fct_orders",
                                     "use_llm": False}).json()
    assert body["status"] == "refused"


def test_ask_validates_input(client):
    assert client.post("/ask", json={"question": "hi"}).status_code == 422


def test_validate_sql_endpoint_ok(client):
    body = client.post("/validate-sql", json={
        "sql": "SELECT fct_orders.region, SUM(fct_orders.net_revenue) AS total_revenue "
               "FROM fct_orders GROUP BY fct_orders.region LIMIT 10"}).json()
    assert body["ok"] is True


def test_validate_sql_endpoint_blocks(client):
    body = client.post("/validate-sql",
                       json={"sql": "SELECT salary_usd FROM employee_salaries"}).json()
    assert body["ok"] is False
    assert body["errors"]


def test_examples(client):
    assert len(client.get("/examples").json()["questions"]) >= 5
