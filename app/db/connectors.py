"""Warehouse connectors.

DuckDB and PostgreSQL have hand-written executors because their read-only
enforcement is specific. Everything else goes through SQLAlchemy, which covers
Snowflake, BigQuery, Databricks, MySQL, Redshift and Trino with one code path.

Read-only posture, in order of reliability:

  1. The credentials you configure should be a read-only role at the database.
     Nothing in this application is a substitute for that.
  2. Where the engine supports it, the session is additionally set read-only
     and given a statement timeout.
  3. The SQL itself has already passed AST validation before arriving here.

Layer 1 is the one that matters. The other two are defence in depth.
"""
from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

from app.core.config import settings
from app.pipeline.executor import ExecutionError, QueryResult, _jsonable

#: SQLAlchemy URL scheme -> sqlglot dialect. Getting this wrong produces SQL the
#: warehouse rejects, so it is explicit rather than guessed.
DIALECTS = {
    "postgresql": "postgres",
    "postgres": "postgres",
    "mysql": "mysql",
    "mariadb": "mysql",
    "snowflake": "snowflake",
    "bigquery": "bigquery",
    "databricks": "databricks",
    "redshift": "redshift",
    "trino": "trino",
    "presto": "presto",
    "duckdb": "duckdb",
    "sqlite": "sqlite",
    "mssql": "tsql",
    "oracle": "oracle",
    "clickhouse": "clickhouse",
}

#: Statements that make a session read-only, per dialect. Absent = rely on the
#: database role, which is the correct control anyway.
READ_ONLY_SETUP = {
    "postgres": ["SET TRANSACTION READ ONLY"],
    "mysql": ["SET SESSION TRANSACTION READ ONLY"],
    "redshift": ["SET TRANSACTION READ ONLY"],
}

TIMEOUT_SETUP = {
    "postgres": "SET LOCAL statement_timeout = {ms}",
    "mysql": "SET SESSION MAX_EXECUTION_TIME = {ms}",
    "snowflake": "ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {s}",
    "databricks": None,
    "bigquery": None,
}


def dialect_for_url(url: str) -> str:
    scheme = urlparse(url).scheme.split("+")[0].lower()
    return DIALECTS.get(scheme, "postgres")


class SQLAlchemyExecutor:
    """Generic read-only executor for any SQLAlchemy-supported warehouse."""

    def __init__(self, url: str | None = None, max_rows: int | None = None,
                 timeout_seconds: int | None = None):
        self.url = url or settings.warehouse_url
        if not self.url:
            raise ExecutionError(
                "WAREHOUSE_URL is not set. Example: "
                "snowflake://user:pw@account/db/schema?warehouse=WH&role=ANALYST_RO"
            )
        self.engine_name = dialect_for_url(self.url)
        self.max_rows = max_rows or settings.max_rows
        self.timeout_seconds = timeout_seconds or settings.query_timeout_seconds
        self._engine = None

    # sqlglot dialect used to render and validate SQL for this warehouse
    @property
    def engine(self) -> str:
        return self.engine_name

    def _get_engine(self):
        if self._engine is None:
            try:
                from sqlalchemy import create_engine
            except ImportError as exc:  # pragma: no cover
                raise ExecutionError(
                    "SQLAlchemy is required for this warehouse. pip install sqlalchemy "
                    "plus the driver (snowflake-sqlalchemy, sqlalchemy-bigquery, "
                    "databricks-sqlalchemy, psycopg, pymysql)."
                ) from exc
            try:
                self._engine = create_engine(self.url, pool_pre_ping=True)
            except Exception as exc:
                raise ExecutionError(f"Could not create engine for warehouse: {exc}") from exc
        return self._engine

    def _session_setup(self) -> list[str]:
        stmts = list(READ_ONLY_SETUP.get(self.engine_name, []))
        template = TIMEOUT_SETUP.get(self.engine_name)
        if template:
            stmts.append(template.format(
                ms=self.timeout_seconds * 1000, s=self.timeout_seconds
            ))
        return stmts

    def execute(self, sql: str) -> QueryResult:
        from sqlalchemy import text

        engine = self._get_engine()
        start = time.perf_counter()
        try:
            with engine.connect() as con:
                for stmt in self._session_setup():
                    try:
                        con.execute(text(stmt))
                    except Exception:
                        # Not every deployment permits session settings; the
                        # database role remains the real control.
                        pass
                cursor = con.execute(text(sql))
                columns = list(cursor.keys())
                rows = cursor.fetchmany(self.max_rows + 1)
                con.rollback()
        except Exception as exc:
            raise ExecutionError(str(exc)) from exc

        truncated = len(rows) > self.max_rows
        rows = rows[: self.max_rows]
        return QueryResult(
            columns=columns,
            rows=[[_jsonable(v) for v in row] for row in rows],
            row_count=len(rows),
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
            truncated=truncated,
            engine=self.engine_name,
        )

    def describe(self) -> dict[str, Any]:
        parsed = urlparse(self.url)
        return {
            "engine": self.engine_name,
            "host": parsed.hostname,
            "database": (parsed.path or "").lstrip("/"),
            "read_only_session": bool(READ_ONLY_SETUP.get(self.engine_name)),
        }


def introspect_sqlalchemy(url: str, schema: str | None = None):
    """Introspect any SQLAlchemy-supported warehouse for `ai-analyst init`."""
    from sqlalchemy import create_engine, inspect

    from app.semantic.bootstrap import ColumnInfo, TableInfo, _guess_primary_key

    engine = create_engine(url)
    inspector = inspect(engine)
    schema = schema or (inspector.default_schema_name or None)

    tables: list[TableInfo] = []
    for name in inspector.get_table_names(schema=schema):
        cols = [
            ColumnInfo(c["name"], str(c["type"]), bool(c.get("nullable", True)))
            for c in inspector.get_columns(name, schema=schema)
        ]
        if not cols:
            continue
        t = TableInfo(name=name, schema=schema or "", columns=cols)
        try:
            pk = inspector.get_pk_constraint(name, schema=schema)
            if pk and pk.get("constrained_columns"):
                t.primary_key = pk["constrained_columns"][0]
        except Exception:
            pass
        try:
            for fk in inspector.get_foreign_keys(name, schema=schema):
                if fk.get("constrained_columns") and fk.get("referred_table"):
                    t.foreign_keys.append((
                        fk["constrained_columns"][0],
                        fk["referred_table"],
                        (fk.get("referred_columns") or [""])[0],
                    ))
        except Exception:
            pass
        if not t.primary_key:
            t.primary_key = _guess_primary_key(t)
        tables.append(t)
    return tables
