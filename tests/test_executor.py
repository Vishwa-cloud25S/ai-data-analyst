import pytest

from app.pipeline.executor import DuckDBExecutor, ExecutionError


def test_read_only_connection_rejects_writes(warehouse):
    ex = DuckDBExecutor(path=warehouse)
    with pytest.raises(ExecutionError):
        ex.execute("CREATE TABLE evil AS SELECT 1 AS x")


def test_select_works(warehouse):
    ex = DuckDBExecutor(path=warehouse)
    r = ex.execute("SELECT COUNT(*) AS n FROM main.fct_orders")
    assert r.row_count == 1 and r.rows[0][0] > 0
    assert r.engine == "duckdb"


def test_max_rows_truncation(warehouse):
    ex = DuckDBExecutor(path=warehouse, max_rows=5)
    r = ex.execute("SELECT order_line_id FROM main.fct_orders LIMIT 100")
    assert r.row_count == 5 and r.truncated


def test_values_are_json_serialisable(warehouse):
    import json

    ex = DuckDBExecutor(path=warehouse)
    r = ex.execute("SELECT order_date, net_revenue FROM main.fct_orders LIMIT 3")
    json.dumps(r.rows)  # must not raise


def test_missing_warehouse_raises():
    with pytest.raises(ExecutionError):
        DuckDBExecutor(path="/tmp/definitely-not-here.duckdb").execute("SELECT 1")
