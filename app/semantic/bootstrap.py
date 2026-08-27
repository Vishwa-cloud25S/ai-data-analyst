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

_CAMEL_1 = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_2 = re.compile(r"([a-z0-9])([A-Z])")


def snake(name: str) -> str:
    """CamelCase -> snake_case, so heuristics work on real-world schemas.

    Chinook, most .NET-era schemas and plenty of enterprise warehouses use
    InvoiceLineId, not invoice_line_id. Matching on the raw name classified
    TrackId as a measure and proposed SUM(TrackId) - the exact disaster the
    measure heuristics exist to prevent.
    """
    return _CAMEL_2.sub(r"\1_\2", _CAMEL_1.sub(r"\1_\2", name)).lower()


def _singularise(word: str) -> str:
    w = word.lower()
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("ses") or w.endswith("xes"):
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


# Columns whose names suggest an additive business measure.
MEASURE_HINTS = re.compile(
    r"(amount|amt|revenue|sales|price|cost|qty|quantity|total|value|units?|"
    r"margin|profit|discount|balance|spend|budget|score|count|duration|weight|height)$",
    re.I,
)
# Numeric columns that are really identifiers or flags, not measures.
NOT_MEASURE = re.compile(
    r"((^|_)(id|key|code|no|number|year|month|day|flag|rank|position|zip|postal)$|"
    r"^(is|has)_)", re.I,
)

# Columns that usually carry personal data. Flagged loudly, never auto-deleted:
# a tool that silently decides what is sensitive will eventually miss something.
PII_HINTS = re.compile(
    r"(email|phone|fax|mobile|address|postcode|postal|zip|dob|birth|ssn|nino|aadhaar|"
    r"passport|salary|pay|compensation|password|token|secret|first_name|last_name|"
    r"full_name|surname|gender|ethnic|religion|latitude|longitude)", re.I,
)

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

    #: set by infer_joins/to_yaml when the surrounding schema is known
    fk_like: int = 0

    @property
    def is_fact(self) -> bool:
        sname = self.name.lower()
        if sname.startswith(FACT_PREFIXES):
            return True
        if sname.startswith(DIM_PREFIXES):
            return False
        # No naming convention: a table with foreign keys and real measures is
        # a fact. Real schemas rarely use fct_/dim_ prefixes.
        keys = max(self.fk_like, len(self.foreign_keys))
        return keys >= 1 and len(classify(self)[1]) >= 1


def is_key_like(name: str) -> bool:
    """True for identifiers, in snake_case or CamelCase."""
    return bool(NOT_MEASURE.search(snake(name)))


def is_pii_like(name: str) -> bool:
    return bool(PII_HINTS.search(snake(name)))


def classify(table: TableInfo) -> tuple[list[ColumnInfo], list[ColumnInfo]]:
    """Split columns into (dimensions, measures). Never treat a key as a measure."""
    dims, measures = [], []
    pk = (table.primary_key or "").lower()
    for c in table.columns:
        numeric = bool(NUMERIC_TYPES.search(c.data_type))
        dated = bool(DATE_TYPES.search(c.data_type))
        if numeric and not dated and not is_key_like(c.name) and c.name.lower() != pk:
            measures.append(c)
        else:
            dims.append(c)
    return dims, measures


def _guess_primary_key(table: TableInfo) -> str | None:
    """Return the ORIGINAL column name - lowercasing it breaks case-sensitive SQL."""
    by_snake = {snake(c.name): c.name for c in table.columns}
    stem = snake(table.name)
    for prefix in ("fct_", "fact_", "f_", "dim_", "d_"):
        stem = stem.removeprefix(prefix)
    candidates = (f"{stem}_id", f"{_singularise(stem)}_id", "id", f"{snake(table.name)}_id")
    for cand in candidates:
        if cand in by_snake:
            return by_snake[cand]
    return table.columns[0].name if table.columns else None


def infer_joins(tables: list[TableInfo]) -> list[dict[str, str]]:
    """Foreign keys where declared; otherwise match key columns to table names.

    Matching is done on snake(name) so CamelCase schemas work:
    InvoiceLine.InvoiceId -> Invoice.InvoiceId.
    """
    joins: list[dict[str, str]] = []
    by_stem: dict[str, TableInfo] = {}
    for t in tables:
        full = snake(t.name)
        stripped = full
        for prefix in FACT_PREFIXES + DIM_PREFIXES:
            stripped = stripped.removeprefix(prefix)
        for variant in (full, _singularise(full), stripped, _singularise(stripped)):
            by_stem.setdefault(variant, t)
    seen: set[tuple[str, str]] = set()

    for t in tables:
        for col, ref_tbl, ref_col in t.foreign_keys:
            target = by_stem.get(snake(ref_tbl)) or by_stem.get(_singularise(snake(ref_tbl)))
            if target is None:
                continue
            key = (t.name, target.name)
            if key in seen:
                continue
            seen.add(key)
            joins.append({"left": t.name, "right": target.name, "type": "left",
                          "sql_on": f"{t.name}.{col} = {target.name}.{ref_col}"})

    for t in tables:
        for c in t.columns:
            cs = snake(c.name)
            if not cs.endswith("_id") or cs == "id":
                continue
            stem = cs[:-3]
            target = by_stem.get(stem) or by_stem.get(_singularise(stem))
            if target is None or target is t:
                continue
            key = (t.name, target.name)
            if key in seen:
                continue
            pk = target.primary_key or _guess_primary_key(target)
            if not pk:
                continue
            # Only join when the target key actually matches the referencing column.
            if snake(pk) not in (cs, f"{stem}_id", "id"):
                continue
            seen.add(key)
            joins.append({"left": t.name, "right": target.name, "type": "left",
                          "sql_on": f"{t.name}.{c.name} = {target.name}.{pk}"})
    return joins


def count_fk_like(table: TableInfo, tables: list[TableInfo]) -> int:
    """How many of this table's columns point at another table in the schema."""
    stems = set()
    for t in tables:
        full = snake(t.name)
        stripped = full
        for prefix in FACT_PREFIXES + DIM_PREFIXES:
            stripped = stripped.removeprefix(prefix)
        stems.update({full, _singularise(full), stripped, _singularise(stripped)})
    n = 0
    for c in table.columns:
        cs = snake(c.name)
        if cs.endswith("_id") and cs != "id":
            stem = cs[:-3]
            if (stem in stems or _singularise(stem) in stems) and stem != snake(table.name):
                n += 1
    return n


def propose_metrics(tables: list[TableInfo]) -> list[dict[str, Any]]:
    """Draft one metric per measure, plus a count per fact table.

    Names are namespaced by entity: two tables both having UnitPrice would
    otherwise generate the same metric name twice and silently collide when
    the layer is loaded into a dict.
    """
    metrics: list[dict[str, Any]] = []
    used: set[str] = set()

    def unique(name: str) -> str:
        candidate, n = name, 2
        while candidate in used:
            candidate = f"{name}_{n}"
            n += 1
        used.add(candidate)
        return candidate

    for t in tables:
        if not t.is_fact:
            continue
        entity_stem = snake(t.name)
        _, measures = classify(t)
        for m in measures:
            label = snake(m.name).replace("_", " ").title()
            metrics.append({
                "name": unique(f"{entity_stem}_{snake(m.name)}"),
                "label": f"Total {label}",
                "description": f"REVIEW: sum of {t.name}.{m.name}. "
                               f"Add any mandatory filters (e.g. excluding cancelled rows), "
                               f"and check this measure is additive at this grain.",
                "entity": t.name,
                "expression": f"SUM({t.name}.{m.name})",
                "filters": [],
                "format": "currency" if re.search(
                    r"(revenue|amount|price|cost|sales|margin|profit|spend|total)",
                    snake(m.name), re.I,
                ) else "number",
            })
        pk = t.primary_key or _guess_primary_key(t)
        if pk:
            metrics.append({
                "name": unique(f"{entity_stem}_count"),
                "label": f"{snake(t.name).replace('_', ' ').title()} Count",
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
    for t in chosen:
        t.fk_like = count_fk_like(t, chosen)
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
                 "description": ("LIKELY PERSONAL DATA - delete unless needed. "
                                 if is_pii_like(c.name) else "REVIEW: ")
                                + snake(c.name).replace("_", " ") + "."}
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
