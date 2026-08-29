"""Bring-your-own-data tests.

Upload a CSV -> new table + semantic-layer entries -> queryable via /ask
-> removable. The negative assertions are the point: PII-like columns must
never reach the semantic layer, built-in datasets can never be removed,
and a failed upload must leave the system exactly as it found it.
"""
from tests.test_layer_editor import ANALYST, _h


def _upload(client, name, content: str | bytes):
    body = content if isinstance(content, bytes) else content.encode()
    return client.post(
        "/datasets/upload",
        files={"file": (name, body, "text/csv")},
        headers=_h(ANALYST),
    )


def _tables(client):
    return {d["name"] for d in
            client.get("/datasets", headers=_h(ANALYST)).json()["datasets"]}


# ------------------------------------------------------------------ listing
def test_list_shows_demo_datasets(layer_client):
    client, _, _ = layer_client
    r = client.get("/datasets", headers=_h(ANALYST))
    assert r.status_code == 200
    data = r.json()
    assert data["upload"]["supported"] is True
    by_name = {d["name"]: d for d in data["datasets"]}
    assert by_name["fct_orders"]["source"] == "demo"
    assert by_name["fct_orders"]["in_layer"] is True
    assert by_name["dim_products"]["rows"] == 14
    # The salaries table exists in the warehouse but must be visible as
    # deliberately hidden - that is the guardrail demo, not an accident.
    assert by_name["employee_salaries"]["in_layer"] is False


# ------------------------------------------------------------------ uploading
def test_upload_csv_and_ask_about_it(layer_client):
    client, _, _ = layer_client
    csv = ("zone,tickets_sold\n"
           "North,120\nSouth,80\nNorth,60\nEast,95\nWest,40\n")
    r = _upload(client, "tickets.csv", csv)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["table"] == "upload_tickets"
    assert d["rows"] == 5
    assert "zone" in d["columns"] and "tickets_sold" in d["columns"]
    assert not d["hidden_columns"]

    assert "upload_tickets" in _tables(client)

    # A brand-new question about brand-new data must come back answered.
    r = client.post("/ask", headers=_h(ANALYST),
                    json={"question": "total tickets sold by zone",
                          "use_llm": False})
    assert r.status_code == 200
    a = r.json()
    assert a["status"] == "answered", a
    assert a["row_count"] == 4  # North, South, East, West
    assert a["confidence"] >= 0.8


def test_upload_exposes_entity_and_metrics(layer_client):
    client, _, _ = layer_client
    _upload(client, "revenue.csv", "month,revenue\n2026-01,100\n2026-02,150\n")
    sem = client.get("/semantic-layer", headers=_h(ANALYST)).json()
    entities = {e["name"]: e for e in sem["entities"]}
    assert "upload_revenue" in entities
    metric_names = {m["name"] for m in sem["metrics"]}
    assert any(n.startswith("upload_revenue_") for n in metric_names)
    # The new metric must not collide with the built-in total_revenue.
    assert sem and any(m["name"] == "total_revenue" for m in sem["metrics"])


def test_upload_links_to_existing_entities(layer_client):
    client, _, _ = layer_client
    # product_id matches dim_products.product_id -> a join should be inferred.
    csv = ("product_id,units\nP001,5\nP002,9\n")
    r = _upload(client, "stock.csv", csv)
    assert r.status_code == 200, r.text
    assert any("dim_products" in j for j in r.json()["joins_added"])
    sem = client.get("/semantic-layer", headers=_h(ANALYST)).json()
    assert any({"upload_stock", "dim_products"} <= {j["left"], j["right"]}
               for j in sem["joins"])


def test_upload_hides_pii_columns(layer_client):
    client, _, _ = layer_client
    csv = "email,amount\na@b.c,1.5\nx@y.z,2.0\nn@p.q,3.0\n"
    r = _upload(client, "cust.csv", csv)
    assert r.status_code == 200, r.json()
    assert "email" in r.json()["hidden_columns"]

    sem = client.get("/semantic-layer", headers=_h(ANALYST)).json()
    ent = next(e for e in sem["entities"] if e["name"] == "upload_cust")
    assert "email" not in ent["dimensions"] + ent["measures"]

    # The model cannot reach the withheld column, whatever it is asked.
    r = client.post("/ask", headers=_h(ANALYST),
                    json={"question": "show me all the emails",
                          "use_llm": False})
    assert r.json()["status"] != "answered"


def test_upload_all_pii_is_rejected_cleanly(layer_client):
    client, _, _ = layer_client
    csv = "ssn,full_name\n123-45-6789,Ann A.\n987-65-4321,Bob B.\n"
    r = _upload(client, "people.csv", csv)
    assert r.status_code == 422
    # Failed upload leaves no table behind.
    assert "upload_people" not in _tables(client)


def test_upload_replaces_same_name(layer_client):
    client, _, _ = layer_client
    assert _upload(client, "dup.csv", "a\n1\n2\n").status_code == 200
    r = _upload(client, "dup.csv", "a\n1\n2\n3\n4\n")
    assert r.status_code == 200
    assert r.json()["rows"] == 4


def test_upload_rejects_bad_files(layer_client):
    client, _, _ = layer_client
    assert _upload(client, "notes.txt", "hello").status_code == 400
    assert _upload(client, "empty.csv", "").status_code == 400
    # Header only: a table of nothing is not a dataset.
    r = _upload(client, "nope.csv", "a,b\n")
    assert r.status_code == 400
    assert "upload_nope" not in _tables(client)
    # Garbage that is not a CSV.
    assert _upload(client, "junk.csv", b"\x00\x01\x02 not csv").status_code == 400


# ------------------------------------------------------------------ removal
def test_remove_upload(layer_client):
    client, _, _ = layer_client
    _upload(client, "gone.csv", "x,y\n1,2\n")
    r = client.delete("/datasets/upload_gone", headers=_h(ANALYST))
    assert r.status_code == 200
    assert "upload_gone" not in _tables(client)
    sem = client.get("/semantic-layer", headers=_h(ANALYST)).json()
    assert not any(e["name"] == "upload_gone" for e in sem["entities"])
    assert not any(m["name"].startswith("upload_gone_") for m in sem["metrics"])


def test_builtin_datasets_cannot_be_removed(layer_client):
    client, _, _ = layer_client
    r = client.delete("/datasets/fct_orders", headers=_h(ANALYST))
    assert r.status_code == 403
    assert "fct_orders" in _tables(client)
    r = client.delete("/datasets/employee_salaries", headers=_h(ANALYST))
    assert r.status_code == 403
    assert "employee_salaries" in _tables(client)


def test_remove_unknown_upload_is_not_an_error(layer_client):
    client, _, _ = layer_client
    r = client.delete("/datasets/upload_never_existed", headers=_h(ANALYST))
    assert r.status_code == 200
    assert r.json()["existed"] is False
