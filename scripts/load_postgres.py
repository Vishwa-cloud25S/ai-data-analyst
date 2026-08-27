"""Copy the DuckDB warehouse into PostgreSQL (for WAREHOUSE=postgres runs).

Usage:
    python -m app.db.seed              # build DuckDB first
    python scripts/load_postgres.py    # then mirror it into Postgres
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duckdb  # noqa: E402

from app.core.config import settings  # noqa: E402

TABLES = ["dim_products", "dim_customers", "fct_orders", "employee_salaries"]

DSN = os.getenv("POSTGRES_ADMIN_DSN", "postgresql://postgres:postgres@localhost:5432/warehouse")


def main() -> None:
    import psycopg

    con = duckdb.connect(settings.duckdb_path, read_only=True)
    with psycopg.connect(DSN, autocommit=True) as pg:
        for table in TABLES:
            rows = con.execute(f"SELECT * FROM {table}").fetchall()
            ncols = len(con.execute(f"SELECT * FROM {table} LIMIT 0").description)
            placeholders = ",".join(["%s"] * ncols)
            with pg.cursor() as cur:
                cur.execute(f"TRUNCATE main.{table}")
                cur.executemany(
                    f"INSERT INTO main.{table} VALUES ({placeholders})", rows
                )
            print(f"loaded {len(rows):>6} rows into main.{table}")
    con.close()


if __name__ == "__main__":
    main()
