"""Semantic-layer editor tests.

Editing the layer decides what the model can reach, so the important assertions
here are negative: a layer that does not load, or does not execute, must never
be persisted - because the failure mode is either a system that refuses
everything, or one that exposes something it should not.
"""
import pytest
import yaml
from fastapi.testclient import TestClient

from app.semantic import editor

ADMIN = "editor-admin-key-000000000000"
ANALYST = "editor-analyst-key-1111111111"


@pytest.fixture
def layer_client(warehouse, retriever, tmp_path, monkeypatch):
    """API client whose semantic layer is a disposable copy."""
    import shutil

    import app.core.security as security
    import app.pipeline.orchestrator as orch
    from app.core.audit import AuditLog, set_audit_log
    from app.core.config import settings
    from app.main import app as fastapi_app
    from app.pipeline.executor import DuckDBExecutor
    from app.pipeline.orchestrator import Analyst

    layer_copy = tmp_path / "semantic_layer.yml"
    shutil.copy2(settings.semantic_layer_path, layer_copy)

    monkeypatch.setattr(settings, "semantic_layer_path", str(layer_copy))
    monkeypatch.setattr(settings, "duckdb_path", warehouse)
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "api_keys",
                        f"{ADMIN}:admin:alice,{ANALYST}:analyst:bob")
    monkeypatch.setattr(settings, "audit_enabled", True)
    security.reset_keyring()
    audit = AuditLog(str(tmp_path / "audit.sqlite"))
    set_audit_log(audit)
    orch._analyst = Analyst(executor=DuckDBExecutor(path=warehouse),
                            retriever=retriever, use_llm=False)
    with TestClient(fastapi_app) as c:
        yield c, layer_copy, audit
    security.reset_keyring()
    set_audit_log(None)
    editor.reload_caches()


def _h(key):
    return {"X-API-Key": key}


# ---------------------------------------------------------------- validation
def test_invalid_yaml_is_rejected(warehouse):
    r = editor.validate_yaml_text("entities: [oops")
    assert not r.ok and "YAML is not valid" in r.errors[0]


def test_layer_without_entities_is_rejected():
    assert not editor.validate_yaml_text("version: 1\nmetrics: []\n").ok


def test_duplicate_metric_names_are_caught():
    text = yaml.safe_dump({
        "entities": [{"name": "t", "physical_table": "main.fct_orders",
                      "dimensions": [{"name": "region", "type": "varchar"}]}],
        "metrics": [
            {"name": "dupe", "entity": "t", "expression": "COUNT(*)"},
            {"name": "dupe", "entity": "t", "expression": "SUM(1)"},
        ],
    })
    report = editor.validate_yaml_text(text)
    assert not report.ok
    assert any("Duplicate metric name" in e for e in report.errors)


def test_metric_referencing_unknown_entity_is_caught():
    text = yaml.safe_dump({
        "entities": [{"name": "t", "physical_table": "main.fct_orders",
                      "dimensions": [{"name": "region", "type": "varchar"}]}],
        "metrics": [{"name": "m", "entity": "ghost", "expression": "COUNT(*)"}],
    })
    assert any("unknown entity" in e for e in editor.validate_yaml_text(text).errors)


def test_nonexecutable_metric_is_caught_against_the_warehouse(warehouse):
    from app.pipeline.executor import DuckDBExecutor

    text = yaml.safe_dump({
        "entities": [{"name": "fct_orders", "physical_table": "main.fct_orders",
                      "primary_key": "order_line_id",
                      "dimensions": [{"name": "region", "type": "varchar"}],
                      "measures": [{"name": "net_revenue", "type": "decimal"}]}],
        "metrics": [{"name": "bad", "entity": "fct_orders",
                     "expression": "SUM(fct_orders.does_not_exist)"}],
    })
    report = editor.validate_yaml_text(text, executor=DuckDBExecutor(path=warehouse))
    assert not report.ok
    assert report.checked_against_warehouse
    assert any("does not execute" in e for e in report.errors)


def test_diff_summary_reports_exposure():
    old = yaml.safe_dump({"entities": [{"name": "a", "physical_table": "x",
                                        "dimensions": [{"name": "c1"}]}]})
    new = yaml.safe_dump({"entities": [
        {"name": "a", "physical_table": "x", "dimensions": [{"name": "c1"},
                                                            {"name": "c2"}]},
        {"name": "secrets", "physical_table": "y", "dimensions": [{"name": "s"}]},
    ]})
    d = editor.diff_summary(old, new)
    assert d["entities_added"] == ["secrets"]
    assert "a.c2" in d["columns_added"]


# ---------------------------------------------------------------- API
def test_editing_requires_admin(layer_client):
    client, _, _ = layer_client
    assert client.get("/semantic-layer/raw", headers=_h(ANALYST)).status_code == 403
    assert client.get("/semantic-layer/raw", headers=_h(ADMIN)).status_code == 200
    assert client.put("/semantic-layer/raw", json={"yaml": "x"},
                      headers=_h(ANALYST)).status_code == 403


def test_unauthenticated_cannot_read_the_layer_source(layer_client):
    client, _, _ = layer_client
    assert client.get("/semantic-layer/raw").status_code == 401


def test_bad_layer_is_not_persisted(layer_client):
    client, path, _ = layer_client
    before = path.read_text()
    r = client.put("/semantic-layer/raw",
                   json={"yaml": "entities: [broken"}, headers=_h(ADMIN))
    assert r.status_code == 422
    assert path.read_text() == before, "a rejected layer must not be written"


def test_metric_that_does_not_execute_is_not_persisted(layer_client):
    client, path, _ = layer_client
    before = path.read_text()
    doc = yaml.safe_load(before)
    doc["metrics"].append({"name": "broken", "label": "Broken", "description": "x",
                           "entity": "fct_orders",
                           "expression": "SUM(fct_orders.nope)", "filters": [],
                           "format": "number"})
    r = client.put("/semantic-layer/raw", json={"yaml": yaml.safe_dump(doc)},
                   headers=_h(ADMIN))
    assert r.status_code == 422
    assert "does not execute" in str(r.json()["detail"])
    assert path.read_text() == before


def test_valid_edit_saves_and_takes_effect_immediately(layer_client):
    """A new metric must be answerable without restarting the service."""
    client, path, _ = layer_client
    doc = yaml.safe_load(path.read_text())
    doc["metrics"].append({
        "name": "discount_total", "label": "Total Discount",
        "description": "Total discount given, in USD.",
        "entity": "fct_orders", "expression": "SUM(fct_orders.discount_amount)",
        "filters": [], "format": "currency"})
    r = client.put("/semantic-layer/raw",
                   json={"yaml": yaml.safe_dump(doc), "message": "add discount"},
                   headers=_h(ADMIN))
    assert r.status_code == 200, r.text
    assert r.json()["diff"]["metrics_added"] == ["discount_total"]

    # The caches were reloaded, so the new metric is live without a restart.
    body = client.get("/semantic-layer", headers=_h(ADMIN)).json()
    assert "discount_total" in [m["name"] for m in body["metrics"]]


def test_removing_a_table_makes_it_unreachable(layer_client):
    """The core promise: delete it from the layer and it stops existing."""
    client, path, _ = layer_client
    doc = yaml.safe_load(path.read_text())
    doc["entities"] = [e for e in doc["entities"] if e["name"] != "dim_customers"]
    doc["joins"] = [j for j in doc["joins"] if "dim_customers" not in (j["left"], j["right"])]
    doc["metrics"] = [m for m in doc["metrics"] if m["entity"] != "dim_customers"]
    r = client.put("/semantic-layer/raw", json={"yaml": yaml.safe_dump(doc)},
                   headers=_h(ADMIN))
    assert r.status_code == 200
    assert r.json()["diff"]["entities_removed"] == ["dim_customers"]

    vr = client.post("/validate-sql",
                     json={"sql": "SELECT dim_customers.segment FROM dim_customers LIMIT 5"},
                     headers=_h(ADMIN)).json()
    assert vr["ok"] is False


def test_edits_are_audited_with_what_was_exposed(layer_client):
    client, path, audit = layer_client
    doc = yaml.safe_load(path.read_text())
    doc["entities"][0]["dimensions"].append(
        {"name": "order_id", "type": "varchar", "description": "dup for test"})
    client.put("/semantic-layer/raw",
               json={"yaml": yaml.safe_dump(doc), "message": "widen"},
               headers=_h(ADMIN))
    events = [e for e in audit.query() if e["question"].startswith("EDIT")]
    assert events, "layer edits must be audited"
    assert events[0]["principal"] == "alice"
    assert "widen" in events[0]["question"]


def test_backup_is_written_and_restorable(layer_client):
    client, path, _ = layer_client
    original = path.read_text()
    doc = yaml.safe_load(original)
    doc["metrics"] = [m for m in doc["metrics"] if m["name"] != "return_rate"]
    client.put("/semantic-layer/raw", json={"yaml": yaml.safe_dump(doc)},
               headers=_h(ADMIN))

    versions = client.get("/semantic-layer/versions", headers=_h(ADMIN)).json()["versions"]
    assert versions and versions[0]["author"] == "alice"

    r = client.post(f"/semantic-layer/restore/{versions[0]['id']}", headers=_h(ADMIN))
    assert r.status_code == 200
    assert "return_rate" in path.read_text()


def test_restore_rejects_path_traversal(layer_client):
    client, _, _ = layer_client
    r = client.post("/semantic-layer/restore/..%2F..%2Fetc%2Fpasswd", headers=_h(ADMIN))
    assert r.status_code in (404, 422)


def test_dry_run_validate_does_not_save(layer_client):
    client, path, _ = layer_client
    before = path.read_text()
    doc = yaml.safe_load(before)
    doc["metrics"].append({"name": "tmp", "label": "T", "description": "d",
                           "entity": "fct_orders", "expression": "COUNT(*)",
                           "filters": [], "format": "number"})
    r = client.post("/semantic-layer/validate",
                    json={"yaml": yaml.safe_dump(doc)}, headers=_h(ADMIN))
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["diff"]["metrics_added"] == ["tmp"]
    assert path.read_text() == before


def test_a_metric_added_at_runtime_is_answerable_by_name(layer_client):
    """The point of the editor: define a metric, then ask for it by name.

    Metric selection used to be a hardcoded word list, so a newly defined
    metric was unreachable and the question silently fell back to revenue.
    """
    client, path, _ = layer_client
    doc = yaml.safe_load(path.read_text())
    doc["metrics"].append({
        "name": "discount_given", "label": "Discount Given",
        "description": "Total discount given to customers, in USD.",
        "entity": "fct_orders",
        "expression": "SUM(fct_orders.discount_amount)",
        "filters": ["fct_orders.order_status NOT IN ('cancelled', 'returned')"],
        "format": "currency"})
    assert client.put("/semantic-layer/raw", json={"yaml": yaml.safe_dump(doc)},
                      headers=_h(ADMIN)).status_code == 200

    body = client.post("/ask", json={"question": "discount given by region",
                                     "use_llm": False}, headers=_h(ADMIN)).json()
    assert body["status"] == "answered"
    assert "discount_given" in body["columns"], body["columns"]
    assert "discount_amount" in body["sql"]


@pytest.mark.parametrize("question,expected", [
    ("revenue by region", "total_revenue"),
    ("units sold by brand", "units_sold"),
    ("average order value by channel", "average_order_value"),
    ("return rate by segment", "return_rate"),
    ("gross margin by category", "gross_margin"),
])
def test_metric_selection_comes_from_the_layer(layer_client, question, expected):
    client, _, _ = layer_client
    body = client.post("/ask", json={"question": question, "use_llm": False},
                       headers=_h(ADMIN)).json()
    assert expected in body["columns"], f"{question!r} -> {body['columns']}"
