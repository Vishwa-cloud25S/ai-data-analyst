"""The guardrail tests. If these pass, the LLM cannot reach anything it shouldn't."""
import pytest

from app.pipeline.validator import validate_sql

GOOD = """
SELECT dim_products.product_name, SUM(fct_orders.net_revenue) AS total_revenue
FROM main.fct_orders AS fct_orders
LEFT JOIN main.dim_products AS dim_products
  ON fct_orders.product_id = dim_products.product_id
WHERE fct_orders.order_status NOT IN ('cancelled','returned')
GROUP BY dim_products.product_name
ORDER BY total_revenue DESC
LIMIT 10
"""

ATTACKS = [
    ("drop table", "DROP TABLE fct_orders"),
    ("delete", "DELETE FROM fct_orders WHERE 1=1"),
    ("insert", "INSERT INTO fct_orders VALUES (1)"),
    ("update", "UPDATE fct_orders SET net_revenue = 0"),
    ("truncate", "TRUNCATE TABLE fct_orders"),
    ("create", "CREATE TABLE evil AS SELECT 1 AS x"),
    ("alter", "ALTER TABLE fct_orders ADD COLUMN x int"),
    ("stacked", "SELECT fct_orders.order_id FROM fct_orders LIMIT 1; DROP TABLE fct_orders"),
    ("unlisted table", "SELECT employee_salaries.salary_usd FROM employee_salaries LIMIT 10"),
    ("system catalog", "SELECT table_name FROM information_schema.tables LIMIT 5"),
    ("duckdb catalog", "SELECT name FROM duckdb_settings() LIMIT 5"),
    ("file read", "SELECT quantity FROM read_csv_auto('/etc/passwd') LIMIT 1"),
    ("parquet read", "SELECT quantity FROM read_parquet('s3://bucket/x.parquet') LIMIT 1"),
    ("unknown column", "SELECT fct_orders.credit_card_number FROM fct_orders LIMIT 5"),
    ("select star", "SELECT * FROM fct_orders LIMIT 5"),
    ("cross join", "SELECT fct_orders.order_id FROM fct_orders, dim_products LIMIT 5"),
    ("cte to unlisted", """
        WITH leak AS (SELECT salary_usd FROM employee_salaries)
        SELECT leak.salary_usd FROM leak LIMIT 5"""),
    ("attach db", "SELECT quantity FROM fct_orders WHERE attach('evil.db') LIMIT 1"),
    ("union to unlisted",
     "SELECT fct_orders.quantity FROM fct_orders UNION ALL "
     "SELECT employee_salaries.salary_usd FROM employee_salaries"),
]


def test_valid_sql_passes(semantic_layer):
    vr = validate_sql(GOOD, semantic_layer)
    assert vr.ok, vr.errors
    assert "fct_orders" in vr.tables
    assert vr.checks["tables_allowed"]
    assert vr.checks["columns_allowed"]


@pytest.mark.parametrize("name,sql", ATTACKS, ids=[a[0] for a in ATTACKS])
def test_attacks_are_blocked(name, sql, semantic_layer):
    vr = validate_sql(sql, semantic_layer)
    assert not vr.ok, f"{name!r} should have been blocked but passed: {vr.sql}"
    assert vr.errors


def test_missing_limit_is_injected(semantic_layer):
    sql = "SELECT fct_orders.order_id FROM fct_orders"
    vr = validate_sql(sql, semantic_layer, max_rows=100)
    assert vr.ok
    assert "LIMIT 100" in vr.sql.upper()
    assert vr.checks["limit_present"] is False


def test_oversized_limit_is_clamped(semantic_layer):
    sql = "SELECT fct_orders.order_id FROM fct_orders LIMIT 999999"
    vr = validate_sql(sql, semantic_layer, max_rows=250)
    assert vr.ok
    assert "250" in vr.sql
    assert any("reduced" in w for w in vr.warnings)


def test_too_many_joins_blocked(semantic_layer):
    sql = """
    SELECT fct_orders.order_id FROM fct_orders
    LEFT JOIN dim_products p1 ON fct_orders.product_id = p1.product_id
    LEFT JOIN dim_products p2 ON fct_orders.product_id = p2.product_id
    LEFT JOIN dim_products p3 ON fct_orders.product_id = p3.product_id
    LEFT JOIN dim_products p4 ON fct_orders.product_id = p4.product_id
    LEFT JOIN dim_products p5 ON fct_orders.product_id = p5.product_id
    LIMIT 10
    """
    vr = validate_sql(sql, semantic_layer, max_joins=4)
    assert not vr.ok
    assert any("Too many joins" in e for e in vr.errors)


def test_empty_sql_is_rejected(semantic_layer):
    assert not validate_sql("", semantic_layer).ok


def test_unparseable_sql_is_rejected(semantic_layer):
    vr = validate_sql("SELECT FROM WHERE GROUP", semantic_layer)
    assert not vr.ok
