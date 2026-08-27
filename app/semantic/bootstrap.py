"""Bootstrap a semantic layer from an existing warehouse or a dbt project.

Time-to-first-answer is the thing that kills evaluations. Hand-writing a
semantic layer for someone else's schema is an afternoon of work before they
see a single answer, so this generates a reviewable starting point in seconds:
introspect the tables, classify columns into dimensions and measures, infer
joins from foreign keys (or naming), and propose certified metrics.

The output is deliberately a *draft*. Metric definitions are business
decisions, and the whole premise of the project is that a human owns them - so
the generated file is annotated with what was guessed and what must be checked.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Columns whose names suggest an additive business measure.
MEASURE_HINTS = re.compile(
    r"(amount|amt|revenue|sales|price|cost|qty|quantity|total|value|units?|"
    r"margin|profit|discount|balance|spend|budget|score|count|duration|weight|height)$",
    re.I,
)
# Numeric columns that are really identifiers or flags, not measures.
NOT_MEASURE = re.compile(r"(_id|_key|_code|_no|_number|year|month|day|flag|is_|has_)$", re.I)

NUMERIC_TYPES = re.compile(
    r"(int|numeric|decimal|double|float|real|money|bigint|smallint)", re.I
)
DATE_TYPES = re.compile(r"(date|time)", re.I)

FACT_PREFIXES = ("fct_", "fact_", "f_")
DIM_PREFIXES = ("dim_", "d_")


@dataclass
class ColumnInfo:
    name: str
    data_type: str
    nullable: bool = True


@dataclass
class TableInfo:
    name: str
    schema: str = "main"
    columns: list[ColumnInfo] = field(default_factory=list)
    primary_key: str | None = None
    foreign_keys: list[tuple[str, str, str]] = field(default_factory=list)  # (col, ref_tbl, ref_col)

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name

    @property
    def is_fact(self) -> bool:
        if self.name.lower().startswith(FACT_PREFIXES):
            return True
        if self.name.lower().startswith(DIM_PREFIXES):
            return False
        # Fall back on shape: several measures and at least one foreign key.
        return len(classify(self)[1]) >= 2 and len(self.foreign_keys) >= 1


def classify(table: TableInfo) -> tuple[list[ColumnInfo], list[ColumnInfo]]:
    """Split columns into (dimensions, measures)."""
    dims, measures = [], []
    for c in table.columns:
        numeric = bool(NUMERIC_TYPES.search(c.data_type))
        looks_like_measure = numeric and not NOT_MEASURE.search(c.name) and (
            bool(MEASURE_HINTS.search(c.name)) or c.name.lower() != (table.primary_key or "")
        )
        if numeric and not NOT_MEASURE.search(c.name) and looks_like_measure:
            measures.append(c)
        else:
            dims.append(c)
    return dims, measures


def _guess_primary_key(table: TableInfo) -> str | None:
    names = [c.name.lower() for c in table.columns]
    singular = table.name.lower()
    for prefix in FACT_PREFIXES + DIM_PREFIXES:
        singular = singular.removeprefix(prefix)
    for candidate in (f"{singular}_id", f"{singular.rstrip('s')}_id", "id", f"{table.name}_id"):
        if candidate in names:
            return candidate
    return names[0] if names else None


def infer_joins(tables: list[TableInfo]) -> list[dict[str, str]]:
    """Foreign keys where declared; otherwise match fact columns to dimension keys."""
    joins: list[dict[str, str]] = []
    by_name = {t.name.lower(): t for t in tables}
    seen: set[tuple[str, str]] = set()

    for t in tables:
        for col, ref_tbl, ref_col in t.foreign_keys:
            key = (t.name.lower(), ref_tbl.lower())
            if key in seen or ref_tbl.lower() not in by_name:
                continue
            seen.add(key)
            joins.append({"left": t.name, "right": ref_tbl, "type": "left",
                          "sql_on": f"{t.name}.{col} = {ref_tbl}.{ref_col}"})

    for t in tables:
        if not t.is_fact:
            continue
        for c in t.columns:
            if not c.name.lower().endswith("_id"):
                continue
            stem = c.name.lower()[:-3]
            for cand in (f"dim_{stem}", f"dim_{stem}s", stem, f"{stem}s"):
                other = by_name.get(cand)
                if not other or other is t:
                    continue
                key = (t.name.lower(), other.name.lower())
                if key in seen:
                    continue
                pk = other.primary_key or _guess_primary_key(other)
                if pk and pk.lower() == c.name.lower():
                    seen.add(key)
                    joins.append({"left": t.name, "right": other.name, "type": "left",
                                  "sql_on": f"{t.name}.{c.name} = {other.name}.{pk}"})
                break
    return joins


def propose_metrics(tables: list[TableInfo]) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    for t in tables:
        if not t.is_fact:
            continue
        _, measures = classify(t)
        for m in measures:
            label = m.name.replace("_", " ").title()
            metrics.append({
                "name": f"total_{m.name}",
                "label": f"Total {label}",
                "description": f"REVIEW: sum of {t.name}.{m.name}. "
                               f"Add any mandatory filters (e.g. excluding cancelled rows).",
                "entity": t.name,
                "expression": f"SUM({t.name}.{m.name})",
                "filters": [],
                "format": "currency" if re.search(
                    r"(revenue|amount|price|cost|sales|margin|profit|spend)", m.name, re.I
                ) else "number",
            })
        pk = t.primary_key or _guess_primary_key(t)
        if pk:
            metrics.append({
                "name": f"{t.name}_count",
                "label": f"{t.name.replace('_', ' ').title()} Count",
                "description": f"REVIEW: distinct count of {t.name}.{pk}.",
                "entity": t.name,
                "expression": f"COUNT(DISTINCT {t.name}.{pk})",
                "filters": [],
                "format": "number",
            })
    return metrics


HEADER = """# Semantic layer - GENERATED DRAFT, review before use.
#
# This file is the contract: the model can only see and query what is declared
# here. Anything you delete becomes invisible and unqueryable, which is the
# intended way to hide sensitive tables and columns.
#
# Before going live:
#   1. DELETE every table and column the model should not see (PII especially).
#   2. Fix each metric marked REVIEW - definitions are business decisions.
#      Add mandatory filters, e.g. status NOT IN ('cancelled','returned').
#   3. Check the inferred joins; wrong grain silently inflates every number.
#   4. Improve the descriptions. They are what retrieval matches against, so
#      vague descriptions produce vague answers.
version: 1
"""


def to_yaml(tables: list[TableInfo], *, include: list[str] | None = None) -> str:
    import yaml

    chosen = [t for t in tables if not include or t.name in include]
    entities = []
    for t in chosen:
        if not t.primary_key:
            t.primary_key = _guess_primary_key(t)
        dims, measures = classify(t)
        entities.append({
            "name": t.name,
            "description": f"REVIEW: {'fact' if t.is_fact else 'dimension'} table "
                           f"{t.qualified} ({len(t.columns)} columns).",
            "physical_table": t.qualified,
            "primary_key": t.primary_key or "",
            "dimensions": [
                {"name": c.name, "type": c.data_type.lower(),
                 "description": f"REVIEW: {c.name.replace('_', ' ')}."}
                for c in dims
            ],
            "measures": [
                {"name": c.name, "type": c.data_type.lower(),
                 "description": f"REVIEW: {c.name.replace('_', ' ')}."}
                for c in measures
            ],
        })

    doc = {
        "entities": entities,
        "joins": infer_joins(chosen),
        "metrics": propose_metrics(chosen),
    }
    body = yaml.safe_dump(doc, sort_keys=False, width=100, allow_unicode=True)
    return HEADER + body


# ---------------------------------------------------------------- introspection
def introspect_duckdb(path: str, schema: str = "main") -> list[TableInfo]:
    import duckdb

    con = duckdb.connect(path, read_only=True)
    try:
        rows = con.execute(
            "SELECT table_name, column_name, data_type, is_nullable "
            "FROM information_schema.columns WHERE table_schema = ? "
            "ORDER BY table_name, ordinal_position", [schema],
        ).fetchall()
    finally:
        con.close()

    tables: dict[str, TableInfo] = {}
    for tname, cname, dtype, nullable in rows:
        t = tables.setdefault(tname, TableInfo(name=tname, schema=schema))
        t.columns.append(ColumnInfo(cname, str(dtype), str(nullable).upper() == "YES"))
    for t in tables.values():
        t.primary_key = _guess_primary_key(t)
    return list(tables.values())


def introspect_postgres(dsn: str, schema: str = "public") -> list[TableInfo]:  # pragma: no cover
    import psycopg

    tables: dict[str, TableInfo] = {}
    with psycopg.connect(dsn) as con, con.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name, data_type, is_nullable "
            "FROM information_schema.columns WHERE table_schema = %s "
            "ORDER BY table_name, ordinal_position", (schema,),
        )
        for tname, cname, dtype, nullable in cur.fetchall():
            t = tables.setdefault(tname, TableInfo(name=tname, schema=schema))
            t.columns.append(ColumnInfo(cname, dtype, nullable == "YES"))

        cur.execute("""
            SELECT tc.table_name, kcu.column_name, ccu.table_name, ccu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
            JOIN information_schema.constraint_column_usage ccu
              ON ccu.constraint_name = tc.constraint_name
            WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = %s
        """, (schema,))
        for tname, col, ref_tbl, ref_col in cur.fetchall():
            if tname in tables:
                tables[tname].foreign_keys.append((col, ref_tbl, ref_col))

        cur.execute("""
            SELECT tc.table_name, kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
            WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = %s
        """, (schema,))
        for tname, col in cur.fetchall():
            if tname in tables:
                tables[tname].primary_key = col

    for t in tables.values():
        if not t.primary_key:
            t.primary_key = _guess_primary_key(t)
    return list(tables.values())


def from_dbt_manifest(manifest_path: str, *, schema: str = "main") -> list[TableInfo]:
    """Build tables from a dbt manifest.json - descriptions included.

    Preferred over raw introspection when the buyer already runs dbt: the
    column documentation their analytics engineers wrote becomes the retrieval
    context for free.
    """
    import json
    from pathlib import Path

    manifest = json.loads(Path(manifest_path).read_text())
    tables: list[TableInfo] = []
    for node in manifest.get("nodes", {}).values():
        if node.get("resource_type") != "model":
            continue
        cols = [
            ColumnInfo(name=c["name"], data_type=(c.get("data_type") or "varchar"))
            for c in (node.get("columns") or {}).values()
        ]
        if not cols:
            continue
        t = TableInfo(name=node["name"], schema=node.get("schema") or schema, columns=cols)
        t.primary_key = _guess_primary_key(t)
        tables.append(t)
    return tables
