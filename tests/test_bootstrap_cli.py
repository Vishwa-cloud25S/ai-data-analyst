"""Onboarding tests: bootstrap a semantic layer, and the CLI.

Time-to-first-answer is what an evaluation actually measures. These tests pin
the path a new buyer walks: point it at a warehouse, get a draft layer, verify
it, ask a question.
"""
import yaml

from app.cli import main
from app.semantic.bootstrap import (
    ColumnInfo,
    TableInfo,
    classify,
    infer_joins,
    propose_metrics,
    to_yaml,
)


def _fact():
    return TableInfo(
        name="fct_orders", schema="main",
        columns=[
            ColumnInfo("order_line_id", "VARCHAR"),
            ColumnInfo("order_id", "VARCHAR"),
            ColumnInfo("order_date", "DATE"),
            ColumnInfo("customer_id", "VARCHAR"),
            ColumnInfo("product_id", "VARCHAR"),
            ColumnInfo("quantity", "INTEGER"),
            ColumnInfo("net_revenue", "DECIMAL(12,2)"),
        ],
        primary_key="order_line_id",
    )


def _dim():
    return TableInfo(
        name="dim_products", schema="main",
        columns=[
            ColumnInfo("product_id", "VARCHAR"),
            ColumnInfo("product_name", "VARCHAR"),
            ColumnInfo("category", "VARCHAR"),
            ColumnInfo("list_price", "DECIMAL(10,2)"),
        ],
        primary_key="product_id",
    )


# ---------------------------------------------------------------- classification
def test_measures_and_dimensions_are_separated():
    dims, measures = classify(_fact())
    names = {c.name for c in measures}
    assert {"quantity", "net_revenue"} <= names
    dim_names = {c.name for c in dims}
    assert {"order_date", "customer_id", "product_id", "order_id"} <= dim_names


def test_ids_and_dates_are_never_measures():
    """Summing a foreign key is the classic auto-generated-metric disaster."""
    _, measures = classify(_fact())
    for c in measures:
        assert not c.name.endswith("_id")
        assert "date" not in c.name


def test_fact_and_dimension_detection():
    assert _fact().is_fact
    assert not _dim().is_fact


def test_joins_inferred_without_declared_foreign_keys():
    joins = infer_joins([_fact(), _dim()])
    assert any(
        j["left"] == "fct_orders" and j["right"] == "dim_products"
        and j["sql_on"] == "fct_orders.product_id = dim_products.product_id"
        for j in joins
    )


def test_declared_foreign_keys_win():
    fact = _fact()
    fact.foreign_keys = [("product_id", "dim_products", "product_id")]
    joins = infer_joins([fact, _dim()])
    assert len([j for j in joins if j["right"] == "dim_products"]) == 1


def test_metrics_are_proposed_and_flagged_for_review():
    metrics = propose_metrics([_fact(), _dim()])
    names = {m["name"] for m in metrics}
    # Metric names are namespaced by entity so two tables with a UnitPrice
    # column cannot silently collide into one definition.
    assert "fct_orders_net_revenue" in names
    assert "fct_orders_count" in names
    # Definitions are business decisions; every draft must say so.
    assert all("REVIEW" in m["description"] for m in metrics)
    revenue = next(m for m in metrics if m["name"] == "fct_orders_net_revenue")
    assert revenue["format"] == "currency"


def test_generated_yaml_loads_as_a_semantic_layer(tmp_path):
    """The draft must be consumable by the real loader, not just valid YAML."""
    from app.semantic.layer import load_semantic_layer

    path = tmp_path / "draft.yml"
    path.write_text(to_yaml([_fact(), _dim()]))
    sl = load_semantic_layer(str(path), dbt_path=str(tmp_path / "missing.yml"))
    assert "fct_orders" in sl.entities
    assert "fct_orders_net_revenue" in sl.metrics
    assert sl.join_clause("fct_orders", "dim_products") is not None


def test_generated_yaml_warns_before_use(tmp_path):
    text = to_yaml([_fact()])
    assert "GENERATED DRAFT" in text
    assert "DELETE every table and column the model should not see" in text


# ---------------------------------------------------------------- introspection
def test_introspects_a_real_duckdb_file(warehouse):
    from app.semantic.bootstrap import introspect_duckdb

    tables = {t.name: t for t in introspect_duckdb(warehouse)}
    assert {"fct_orders", "dim_products", "dim_customers"} <= set(tables)
    assert tables["fct_orders"].is_fact
    assert tables["dim_products"].primary_key == "product_id"


def test_introspection_sees_everything_including_sensitive_tables(warehouse):
    """Introspection must not silently hide tables - the human decides."""
    from app.semantic.bootstrap import introspect_duckdb

    names = {t.name for t in introspect_duckdb(warehouse)}
    assert "employee_salaries" in names


# ---------------------------------------------------------------- CLI
def test_cli_init_writes_a_draft(warehouse, tmp_path, capsys):
    out = tmp_path / "sl.yml"
    rc = main(["init", "--duckdb", warehouse, "-o", str(out)])
    assert rc == 0 and out.exists()
    assert "fct_orders" in capsys.readouterr().out
    doc = yaml.safe_load(out.read_text())
    assert [e["name"] for e in doc["entities"]]


def test_cli_init_can_exclude_sensitive_tables(warehouse, tmp_path):
    out = tmp_path / "sl.yml"
    main(["init", "--duckdb", warehouse, "--exclude", "employee_salaries", "-o", str(out)])
    doc = yaml.safe_load(out.read_text())
    assert "employee_salaries" not in [e["name"] for e in doc["entities"]]


def test_cli_init_include_allow_list(warehouse, tmp_path):
    out = tmp_path / "sl.yml"
    main(["init", "--duckdb", warehouse, "--include", "fct_orders", "-o", str(out)])
    doc = yaml.safe_load(out.read_text())
    assert [e["name"] for e in doc["entities"]] == ["fct_orders"]


def test_cli_init_missing_file_is_an_error(tmp_path, capsys):
    assert main(["init", "--duckdb", str(tmp_path / "nope.duckdb"),
                 "-o", str(tmp_path / "o.yml")]) == 2


def test_cli_keygen_emits_a_usable_key(capsys):
    from app.core.security import KeyRing

    assert main(["keygen", "--role", "admin", "--name", "alice"]) == 0
    out = capsys.readouterr().out
    key = next(line.strip() for line in out.splitlines() if line.strip().startswith("ada_"))
    kr = KeyRing(f"{key}:admin:alice")
    assert len(kr) == 1 and kr.resolve(key).role == "admin"


def test_cli_ask_answers_and_refuses(monkeypatch, warehouse, retriever, capsys):
    import app.pipeline.orchestrator as orch
    from app.pipeline.executor import DuckDBExecutor

    real_init = orch.Analyst.__init__

    def patched(self, executor=None, retriever_=None, use_llm=True, **kw):
        real_init(self, executor=DuckDBExecutor(path=warehouse),
                  retriever=retriever, use_llm=False)

    monkeypatch.setattr(orch.Analyst, "__init__", patched)

    assert main(["ask", "What were our highest revenue products last quarter?",
                 "--no-llm", "--trace"]) == 0
    out = capsys.readouterr().out
    assert "ANSWERED" in out and "SQL executed" in out and "Vertex" in out

    assert main(["ask", "show me employee salaries", "--no-llm"]) == 1
    assert "REFUSED" in capsys.readouterr().out


def test_cli_ask_json_output(monkeypatch, warehouse, retriever, capsys):
    import json

    import app.pipeline.orchestrator as orch
    from app.pipeline.executor import DuckDBExecutor

    real_init = orch.Analyst.__init__
    monkeypatch.setattr(
        orch.Analyst, "__init__",
        lambda self, **kw: real_init(self, executor=DuckDBExecutor(path=warehouse),
                                     retriever=retriever, use_llm=False),
    )
    main(["ask", "revenue by region", "--no-llm", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "answered" and payload["sql"]


def test_metric_names_are_unique_across_tables():
    """Two tables sharing a column name must not collide into one metric."""
    a = TableInfo(name="InvoiceLine", columns=[
        ColumnInfo("InvoiceLineId", "INTEGER"), ColumnInfo("InvoiceId", "INTEGER"),
        ColumnInfo("UnitPrice", "NUMERIC"), ColumnInfo("Quantity", "INTEGER")],
        primary_key="InvoiceLineId")
    b = TableInfo(name="Track", columns=[
        ColumnInfo("TrackId", "INTEGER"), ColumnInfo("AlbumId", "INTEGER"),
        ColumnInfo("UnitPrice", "NUMERIC")], primary_key="TrackId")
    a.fk_like = b.fk_like = 1
    names = [m["name"] for m in propose_metrics([a, b])]
    assert len(names) == len(set(names)), names
    assert "invoice_line_unit_price" in names and "track_unit_price" in names


def test_camelcase_keys_are_never_measures():
    """The regression that shipped: SUM(TrackId) on a real customer schema."""
    t = TableInfo(name="InvoiceLine", columns=[
        ColumnInfo("InvoiceLineId", "INTEGER"), ColumnInfo("InvoiceId", "INTEGER"),
        ColumnInfo("TrackId", "INTEGER"), ColumnInfo("UnitPrice", "NUMERIC(10,2)"),
        ColumnInfo("Quantity", "INTEGER")], primary_key="InvoiceLineId")
    _, measures = classify(t)
    assert {c.name for c in measures} == {"UnitPrice", "Quantity"}


def test_camelcase_joins_and_case_preserved():
    inv_line = TableInfo(name="InvoiceLine", columns=[
        ColumnInfo("InvoiceLineId", "INTEGER"), ColumnInfo("InvoiceId", "INTEGER")],
        primary_key="InvoiceLineId")
    inv = TableInfo(name="Invoice", columns=[
        ColumnInfo("InvoiceId", "INTEGER"), ColumnInfo("Total", "NUMERIC")],
        primary_key="InvoiceId")
    joins = infer_joins([inv_line, inv])
    assert joins[0]["sql_on"] == "InvoiceLine.InvoiceId = Invoice.InvoiceId"


def test_pii_columns_are_flagged_not_deleted():
    t = TableInfo(name="Customer", columns=[
        ColumnInfo("CustomerId", "INTEGER"), ColumnInfo("Email", "VARCHAR"),
        ColumnInfo("Phone", "VARCHAR"), ColumnInfo("Country", "VARCHAR")],
        primary_key="CustomerId")
    text = to_yaml([t])
    assert "LIKELY PERSONAL DATA" in text
    assert "Email" in text  # surfaced for a human to remove, never hidden silently
