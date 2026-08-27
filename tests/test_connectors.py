"""Generic warehouse connector tests.

Exercised against SQLite through SQLAlchemy: a warehouse the pipeline was never
built for, which is the point. If the generic path works on an engine nobody
tuned it for, the Snowflake/BigQuery/Databricks claims are credible.
"""
import pytest

from app.db.connectors import SQLAlchemyExecutor, dialect_for_url, introspect_sqlalchemy
from app.pipeline.executor import ExecutionError


@pytest.fixture(scope="module")
def sqlite_url(tmp_path_factory):
    import sqlite3

    path = tmp_path_factory.mktemp("wh") / "shop.sqlite"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE Customer (
            CustomerId INTEGER PRIMARY KEY, CompanyName TEXT, Country TEXT);
        CREATE TABLE SalesOrder (
            OrderId INTEGER PRIMARY KEY, CustomerId INTEGER, OrderDate DATE,
            Status TEXT, FOREIGN KEY (CustomerId) REFERENCES Customer(CustomerId));
        CREATE TABLE OrderItem (
            OrderItemId INTEGER PRIMARY KEY, OrderId INTEGER, ProductName TEXT,
            UnitPrice NUMERIC, Quantity INTEGER,
            FOREIGN KEY (OrderId) REFERENCES SalesOrder(OrderId));
        INSERT INTO Customer VALUES (1,'Acme','UK'),(2,'Globex','US');
        INSERT INTO SalesOrder VALUES
            (1,1,'2026-05-02','shipped'),(2,2,'2026-05-11','shipped'),
            (3,1,'2026-06-20','cancelled');
        INSERT INTO OrderItem VALUES
            (1,1,'Widget',10.0,3),(2,1,'Gadget',25.0,1),
            (3,2,'Widget',10.0,10),(4,3,'Gizmo',99.0,1);
    """)
    con.commit()
    con.close()
    return f"sqlite:///{path}"


@pytest.mark.parametrize("url,expected", [
    ("snowflake://u:p@acct/db/schema", "snowflake"),
    ("bigquery://project/dataset", "bigquery"),
    ("databricks://token:x@host", "databricks"),
    ("postgresql+psycopg://u@h/db", "postgres"),
    ("mysql+pymysql://u@h/db", "mysql"),
    ("redshift+psycopg2://u@h/db", "redshift"),
    ("trino://u@h:8080/hive", "trino"),
    ("sqlite:////tmp/x.db", "sqlite"),
])
def test_dialect_mapping(url, expected):
    """A wrong dialect renders SQL the warehouse rejects, so this is explicit."""
    assert dialect_for_url(url) == expected


def test_executes_and_returns_rows(sqlite_url):
    ex = SQLAlchemyExecutor(sqlite_url)
    r = ex.execute("SELECT Country, COUNT(*) AS n FROM Customer GROUP BY Country")
    assert r.row_count == 2
    assert set(r.columns) == {"Country", "n"}
    assert r.engine == "sqlite"


def test_row_cap_is_enforced(sqlite_url):
    ex = SQLAlchemyExecutor(sqlite_url, max_rows=2)
    r = ex.execute("SELECT OrderItemId FROM OrderItem")
    assert r.row_count == 2 and r.truncated


def test_results_are_json_serialisable(sqlite_url):
    import json

    ex = SQLAlchemyExecutor(sqlite_url)
    json.dumps(ex.execute("SELECT OrderDate, UnitPrice FROM SalesOrder, OrderItem "
                          "LIMIT 2").rows)


def test_bad_sql_raises_execution_error(sqlite_url):
    with pytest.raises(ExecutionError):
        SQLAlchemyExecutor(sqlite_url).execute("SELECT * FROM does_not_exist")


def test_missing_url_is_actionable():
    with pytest.raises(ExecutionError, match="WAREHOUSE_URL"):
        SQLAlchemyExecutor("")


def test_introspection_reads_columns_keys_and_foreign_keys(sqlite_url):
    tables = {t.name: t for t in introspect_sqlalchemy(sqlite_url)}
    assert {"Customer", "SalesOrder", "OrderItem"} <= set(tables)
    assert tables["Customer"].primary_key == "CustomerId"
    fks = tables["OrderItem"].foreign_keys
    assert ("OrderId", "SalesOrder", "OrderId") in fks


def test_full_pipeline_on_an_unfamiliar_warehouse(sqlite_url, tmp_path):
    """End to end on SQLite: introspect, review, ask - nothing tuned for it."""
    import yaml

    from app.pipeline.orchestrator import Analyst
    from app.pipeline.retrieval import SchemaRetriever
    from app.semantic.bootstrap import to_yaml
    from app.semantic.layer import load_semantic_layer

    tables = introspect_sqlalchemy(sqlite_url)
    draft = tmp_path / "sl.yml"
    draft.write_text(to_yaml(tables))

    # the human review step: define the real revenue metric
    doc = yaml.safe_load(draft.read_text())
    doc["metrics"] = [m for m in doc["metrics"] if "count" in m["name"]]
    doc["metrics"].append({
        "name": "revenue", "label": "Revenue",
        "description": "Net sales, excluding cancelled orders.",
        "entity": "OrderItem",
        "expression": "SUM(OrderItem.UnitPrice * OrderItem.Quantity)",
        "filters": ["SalesOrder.Status <> 'cancelled'"], "format": "currency",
    })
    yaml.safe_dump(doc, draft.open("w"), sort_keys=False)

    sl = load_semantic_layer(str(draft), dbt_path=str(tmp_path / "none.yml"))
    analyst = Analyst(executor=SQLAlchemyExecutor(sqlite_url),
                      retriever=SchemaRetriever(sl), use_llm=False)

    res = analyst.ask("what is our revenue by country")
    assert res.status == "answered", res.answer
    assert "Country" in res.columns
    # two hops: OrderItem -> SalesOrder -> Customer
    assert res.sql.count("JOIN") == 2
    # the certified filter travelled with the metric
    assert "cancelled" in res.sql
    revenue = {row[0]: row[1] for row in res.rows}
    assert revenue["UK"] == pytest.approx(55.0)   # 10*3 + 25*1, cancelled excluded
    assert revenue["US"] == pytest.approx(100.0)  # 10*10


def test_guardrails_apply_on_the_generic_connector(sqlite_url, tmp_path):
    from app.pipeline.orchestrator import Analyst
    from app.pipeline.retrieval import SchemaRetriever
    from app.semantic.bootstrap import to_yaml
    from app.semantic.layer import load_semantic_layer

    draft = tmp_path / "sl2.yml"
    draft.write_text(to_yaml([t for t in introspect_sqlalchemy(sqlite_url)
                              if t.name != "Customer"]))
    sl = load_semantic_layer(str(draft), dbt_path=str(tmp_path / "none.yml"))
    analyst = Analyst(executor=SQLAlchemyExecutor(sqlite_url),
                      retriever=SchemaRetriever(sl), use_llm=False)

    # Customer was withheld from the layer, so it is unreachable.
    from app.pipeline.validator import validate_sql

    vr = validate_sql("SELECT CompanyName FROM Customer LIMIT 5", sl, dialect="sqlite")
    assert not vr.ok
    assert any("Customer" in e for e in vr.errors)

    assert analyst.ask("delete all orders").status == "refused"
