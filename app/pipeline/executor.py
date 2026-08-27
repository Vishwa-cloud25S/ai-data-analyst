"""Step 5 - Read-only execution.

Defence in depth: even though the SQL is already validated, the connection
itself is opened read-only (DuckDB `read_only=True`; Postgres sets the session
to READ ONLY and uses a statement timeout + a least-privilege role).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings


class ExecutionError(RuntimeError):
    pass


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    duration_ms: float
    truncated: bool = False
    engine: str = "duckdb"
    column_types: dict[str, str] = field(default_factory=dict)

    def to_records(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, r, strict=False)) for r in self.rows]


def _jsonable(v: Any) -> Any:
    import datetime
    import decimal

    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    return v


class DuckDBExecutor:
    engine = "duckdb"

    def __init__(self, path: str | None = None, max_rows: int | None = None):
        self.path = path or settings.duckdb_path
        self.max_rows = max_rows or settings.max_rows

    def execute(self, sql: str) -> QueryResult:
        import duckdb

        start = time.perf_counter()
        try:
            con = duckdb.connect(self.path, read_only=True)
        except Exception as exc:
            raise ExecutionError(
                f"Cannot open warehouse read-only at {self.path}: {exc}. "
                "Run `python -m app.db.seed` first."
            ) from exc
        try:
            cur = con.execute(sql)
            rows = cur.fetchmany(self.max_rows + 1)
            columns = [d[0] for d in cur.description]
            types = {d[0]: str(d[1]) for d in cur.description}
        except Exception as exc:
            raise ExecutionError(str(exc)) from exc
        finally:
            con.close()

        truncated = len(rows) > self.max_rows
        rows = rows[: self.max_rows]
        return QueryResult(
            columns=columns,
            rows=[[_jsonable(v) for v in r] for r in rows],
            row_count=len(rows),
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
            truncated=truncated,
            engine=self.engine,
            column_types=types,
        )


class PostgresExecutor:
    engine = "postgres"

    def __init__(self, dsn: str | None = None, max_rows: int | None = None):
        self.dsn = dsn or settings.postgres_dsn
        self.max_rows = max_rows or settings.max_rows

    def execute(self, sql: str) -> QueryResult:
        import psycopg  # type: ignore

        start = time.perf_counter()
        with psycopg.connect(self.dsn, autocommit=False) as con:
            with con.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute(
                    f"SET LOCAL statement_timeout = {settings.query_timeout_seconds * 1000}"
                )
                cur.execute(sql)
                rows = cur.fetchmany(self.max_rows + 1)
                columns = [d.name for d in cur.description]
                types = {d.name: str(d.type_code) for d in cur.description}
            con.rollback()  # never leave a transaction open

        truncated = len(rows) > self.max_rows
        rows = rows[: self.max_rows]
        return QueryResult(
            columns=columns,
            rows=[[_jsonable(v) for v in r] for r in rows],
            row_count=len(rows),
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
            truncated=truncated,
            engine=self.engine,
            column_types=types,
        )


def get_executor():
    """Pick the executor. An explicit WAREHOUSE_URL always wins."""
    if settings.warehouse_url:
        from app.db.connectors import SQLAlchemyExecutor

        return SQLAlchemyExecutor()
    if settings.warehouse == "postgres":
        return PostgresExecutor()
    return DuckDBExecutor()
