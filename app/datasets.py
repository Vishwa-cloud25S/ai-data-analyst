"""Bring your own data: upload a CSV, get a queryable dataset.

The demo warehouse is built-in, but the product promise is "ask questions
about YOUR business". This module closes that loop in three steps:

  1. ingest the CSV into a new table in the DuckDB warehouse (a temporary,
     write-capable connection - the query path itself stays read-only);
  2. expose the table through the semantic layer: auto-classified columns,
     one draft metric per measure, joins to existing entities where a
     key column matches;
  3. reload the caches so the very next question can be asked against it.

Safety posture
--------------
* Columns that look like personal data are NOT declared in the layer, so the
  model cannot see them and the validator cannot let them into SQL. They are
  reported back to the uploader, who can expose them deliberately (semantic
  layer editor) if they know what they are doing.
* The merged layer is validated end-to-end (load + execute every entity and
  metric) before it is saved; if validation fails the table is dropped again,
  so a failed upload leaves the system exactly as it found it.
* Built-in tables can never be removed through this surface.
* Limits are hard: 10 MB, 200k rows, 100 columns - a demo on a free tier,
  not a data platform.
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

import yaml

from app.semantic.bootstrap import (
    ColumnInfo,
    TableInfo,
    classify,
    infer_joins,
    introspect_duckdb,
    is_pii_like,
    snake,
)

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_ROWS = 200_000
MAX_COLUMNS = 100
UPLOAD_PREFIX = "upload_"


class DatasetError(Exception):
    """An upload/removal problem, with the HTTP status it deserves."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _executor():
    """The pipeline's own DuckDB executor - the single source of truth for
    which warehouse the questions run against."""
    from app.pipeline.executor import DuckDBExecutor
    from app.pipeline.orchestrator import get_analyst

    ex = get_analyst().executor
    if not isinstance(ex, DuckDBExecutor):
        raise DatasetError(
            400, "Dataset upload requires a local DuckDB warehouse; this "
                 "deployment points at a different database.")
    return ex


def upload_supported() -> tuple[bool, str]:
    try:
        _executor()
        return True, ""
    except DatasetError as exc:
        return False, exc.message


def _rw_connection():
    import duckdb

    ex = _executor()
    return duckdb.connect(ex.path), ex.path


def _drop(table: str) -> None:
    con, _ = _rw_connection()
    try:
        con.execute(f"DROP TABLE IF EXISTS {table}")
    finally:
        con.close()


def slug_table(filename: str) -> str:
    """'July Sales (1).CSV' -> 'upload_july_sales_1'. Stable across uploads,
    so re-uploading the same file replaces the dataset."""
    stem = Path(filename).stem
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", stem)
    slug = re.sub(r"_+", "_", slug).strip("_").lower()[:40]
    if not slug or slug[0].isdigit():
        slug = ("data_" + slug).strip("_") or "data"
    return UPLOAD_PREFIX + slug


def _quote(table: str) -> str:
    # slugs and built-in names are the only things that may ever reach SQL
    if not re.fullmatch(r"[A-Za-z0-9_]+", table):
        raise DatasetError(400, f"Unsafe table name: {table!r}")
    return table


# ------------------------------------------------------------------ listing
def list_datasets() -> dict[str, Any]:
    """Every table in the warehouse, with size, columns, and whether the
    model is allowed to see it."""
    import duckdb

    from app.pipeline.retrieval import get_retriever

    ex = _executor()
    sl = get_retriever().sl
    con = duckdb.connect(ex.path, read_only=True)
    try:
        raw_cols = con.execute(
            "SELECT table_name, column_name, data_type "
            "FROM information_schema.columns WHERE table_schema = 'main' "
            "ORDER BY table_name, ordinal_position"
        ).fetchall()
        tables: dict[str, list[dict[str, str]]] = {}
        for tname, cname, dtype in raw_cols:
            _quote(str(tname))
            tables.setdefault(str(tname), []).append(
                {"name": str(cname), "type": str(dtype).upper()})

        out = []
        for name in sorted(tables):
            rows = con.execute(f"SELECT COUNT(*) FROM {_quote(name)}").fetchone()[0]
            out.append({
                "name": name,
                "rows": int(rows),
                "columns": tables[name],
                "source": "upload" if name.startswith(UPLOAD_PREFIX) else "demo",
                "in_layer": name.lower() in sl.allowed_tables,
            })
    finally:
        con.close()

    _, reason = upload_supported()
    return {
        "upload": {"supported": not reason, "reason": reason,
                   "max_file_mb": MAX_FILE_BYTES // (1024 * 1024),
                   "max_rows": MAX_ROWS, "max_columns": MAX_COLUMNS},
        "datasets": out,
    }


# ------------------------------------------------------------------ ingesting
def _build_entity(table: TableInfo, rows: int,
                  filename: str) -> tuple[dict, list[str], str]:
    """The entity for an uploaded table, with PII-like columns withheld -
    including the key if the key itself looks personal (a CSV keyed on
    email is still a CSV full of email)."""
    pk = table.primary_key or ""
    if is_pii_like(pk):
        pk = ""
    kept = [c for c in table.columns if not is_pii_like(c.name)]
    hidden = [c.name for c in table.columns if is_pii_like(c.name)]

    usable = TableInfo(name=table.name, schema=table.schema, columns=kept)
    usable.primary_key = pk
    dims, measures = classify(usable)

    if not dims and not measures:
        raise DatasetError(
            422, "Every column in that CSV looks like personal data, so none "
                 "of it is given to the model. Remove the sensitive columns "
                 "and upload again, or expose them deliberately through the "
                 "semantic layer editor.")

    entity = {
        "name": table.name,
        "description": (f"Uploaded CSV '{filename}' ({rows:,} rows). "
                        "REVIEW: auto-generated from the file header."),
        "physical_table": table.name,
        "primary_key": pk,
        "dimensions": [{"name": c.name, "type": c.data_type.lower(),
                        "description": "Column from uploaded CSV."} for c in dims],
        "measures": [{"name": c.name, "type": c.data_type.lower(),
                      "description": "Column from uploaded CSV."} for c in measures],
    }
    return entity, hidden, pk


def _draft_metrics(table: TableInfo, pk: str, taken: set[str]) -> list[dict]:
    """One SUM per measure plus a count - the same drafts the CLI bootstrap
    writes, but namespaced so they cannot collide with existing metrics. A
    standalone file is treated as a fact: a buyer uploading sales.csv wants
    to ask about sales."""

    _, measures = classify(table)
    used = set(taken)

    def unique(name: str) -> str:
        candidate, n = name, 2
        while candidate in used:
            candidate = f"{name}_{n}"
            n += 1
        used.add(candidate)
        return candidate

    stem = snake(table.name)
    pretty = table.name[len(UPLOAD_PREFIX):].replace("_", " ").title()
    out = []
    for m in measures:
        out.append({
            "name": unique(f"{stem}_{snake(m.name)}"),
            "label": f"Total {snake(m.name).replace('_', ' ').title()}",
            "description": (f"REVIEW: sum of {table.name}.{m.name} (uploaded "
                            "CSV). Add any mandatory filters."),
            "entity": table.name,
            "expression": f"SUM({table.name}.{m.name})",
            "filters": [],
            "format": ("currency" if re.search(
                r"(revenue|amount|price|cost|sales|margin|profit|spend|total)",
                snake(m.name), re.I) else "number"),
        })
    if pk:
        out.append({
            "name": unique(f"{stem}_count"),
            "label": f"{pretty} Count",
            "description": f"REVIEW: distinct count of {table.name}.{pk}.",
            "entity": table.name,
            "expression": f"COUNT(DISTINCT {table.name}.{pk})",
            "filters": [],
            "format": "number",
        })
    return out


def _draft_joins(new_table: TableInfo, doc: dict) -> list[dict]:
    """Joins from the uploaded table to existing entities, inferred the same
    way the CLI bootstrap infers them - only joins touching the new table are
    added, and never duplicates of joins the layer already declares."""
    existing: list[TableInfo] = []
    for e in doc.get("entities") or []:
        ptable = str(e.get("physical_table", "")).split(".")[-1]
        cols = [ColumnInfo(c["name"], c.get("type", "varchar"))
                for c in (e.get("dimensions") or []) + (e.get("measures") or [])]
        if not ptable or not cols:
            continue
        ti = TableInfo(name=ptable, columns=cols)
        ti.primary_key = e.get("primary_key") or None
        existing.append(ti)

    already = {frozenset((j.get("left", ""), j.get("right", "")))
               for j in doc.get("joins") or []}
    added = []
    for j in infer_joins([new_table] + existing):
        sides = {j.get("left"), j.get("right")}
        if new_table.name not in sides:
            continue
        if frozenset(sides) in already:
            continue
        already.add(frozenset(sides))
        added.append(j)
    return added


def _merge_layer(table: str, entity: dict, metrics: list[dict], joins: list[dict],
                 executor) -> None:
    """Validate the merged layer against the warehouse, then save + reload.
    A layer that does not execute is never persisted."""
    from app.semantic import editor

    raw = editor.read_raw()
    doc = yaml.safe_load(raw) or {}
    entities = doc.setdefault("entities", [])
    # Replace in place if this exact table was declared before (re-upload).
    doc["entities"] = [e for e in entities
                       if e.get("name") != table and
                       str(e.get("physical_table", "")).split(".")[-1] != table]
    doc["entities"].append(entity)
    doc["metrics"] = [m for m in (doc.get("metrics") or [])
                      if m.get("entity") != table]
    doc["metrics"].extend(metrics)
    doc["joins"] = [j for j in (doc.get("joins") or [])
                    if table not in (j.get("left"), j.get("right"))]
    doc["joins"].extend(joins)

    merged = yaml.safe_dump(doc, sort_keys=False, width=100, allow_unicode=True)
    report = editor.validate_yaml_text(merged, executor=executor)
    if not report.ok:
        raise DatasetError(422, "The data loaded, but the generated semantic "
                                f"layer failed validation: {report.errors[0]}")
    editor.save(merged, author=f"upload:{table}")
    editor.reload_caches()


def ingest_csv(csv_path: Path, filename: str, principal: str = "anonymous") -> dict:
    """CSV file on disk -> table + semantic layer entries. Atomic enough for
    a demo: if anything fails after the table exists, the table is dropped."""
    table = slug_table(filename)
    _quote(table)
    con, db_path = _rw_connection()
    try:
        con.execute(f"DROP TABLE IF EXISTS {table}")
        con.execute(f"CREATE TABLE {table} AS "
                    f"SELECT * FROM read_csv_auto('{csv_path.as_posix()}')")
        n_rows = int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        n_cols = len(con.execute(f"DESCRIBE {table}").fetchall())
    except Exception as exc:
        try:
            con.execute(f"DROP TABLE IF EXISTS {table}")
        except Exception:
            pass
        raise DatasetError(
            400, f"Could not read that file as a CSV table: {str(exc)[:200]}") from exc
    finally:
        con.close()

    try:
        if n_rows == 0:
            raise DatasetError(400, "The CSV has a header but no data rows.")
        if n_rows > MAX_ROWS:
            raise DatasetError(413, f"That CSV has {n_rows:,} rows; the limit "
                                    f"is {MAX_ROWS:,}.")
        if not n_cols or n_cols > MAX_COLUMNS:
            raise DatasetError(400, f"That CSV has {n_cols} columns; the limit "
                                    f"is {MAX_COLUMNS}.")

        tables = introspect_duckdb(db_path)
        info = next(t for t in tables if t.name == table)

        doc = yaml.safe_load(editor_raw()) or {}
        entity, hidden, pk = _build_entity(info, n_rows, filename)
        executor = _executor()
        taken = {m.get("name") for m in doc.get("metrics", [])}
        metrics = _draft_metrics(info, pk, taken)
        joins = _draft_joins(info, doc)

        _merge_layer(table, entity, metrics, joins, executor)
    except DatasetError:
        _drop(table)
        raise

    return {
        "table": table,
        "rows": n_rows,
        "n_columns": n_cols,
        "columns": [c.name for c in info.columns],
        "hidden_columns": hidden,
        "metrics_added": [m["label"] for m in metrics],
        "joins_added": [f"{j['left']} -> {j['right']}" for j in joins],
        "replaced": True,  # the table was dropped first; see remove_dataset
    }


def editor_raw() -> str:
    from app.semantic import editor
    return editor.read_raw()


# ------------------------------------------------------------------ removal
def remove_dataset(table: str, principal: str = "anonymous") -> dict:
    if not table.startswith(UPLOAD_PREFIX):
        raise DatasetError(
            403, "Built-in datasets cannot be removed through the UI. Only "
                 f"uploaded tables (names starting with '{UPLOAD_PREFIX}') "
                 "can be deleted.")
    _quote(table)

    from app.semantic import editor

    raw = editor.read_raw()
    doc = yaml.safe_load(raw) or {}
    had_entity = any(e.get("name") == table or
                     str(e.get("physical_table", "")).split(".")[-1] == table
                     for e in doc.get("entities") or [])
    if not had_entity:
        # Nothing declared - just drop the table if it exists.
        _drop(table)
        return {"removed": table, "existed": False}

    removed_metrics = [m["name"] for m in doc.get("metrics", [])
                       if m.get("entity") == table]
    removed_joins = [j for j in doc.get("joins", [])
                     if table in (j.get("left"), j.get("right"))]

    doc["entities"] = [e for e in doc["entities"]
                       if e.get("name") != table and
                       str(e.get("physical_table", "")).split(".")[-1] != table]
    doc["metrics"] = [m for m in doc["metrics"] if m.get("entity") != table]
    doc["joins"] = [j for j in doc["joins"]
                    if table not in (j.get("left"), j.get("right"))]

    merged = yaml.safe_dump(doc, sort_keys=False, width=100, allow_unicode=True)
    executor = _executor()
    report = editor.validate_yaml_text(merged, executor=executor)
    if not report.ok:
        raise DatasetError(409, f"The layer would not validate after removal: "
                                f"{report.errors[0]}")
    editor.save(merged, author=f"remove:{principal}")
    editor.reload_caches()
    _drop(table)

    return {"removed": table, "existed": True,
            "metrics_removed": removed_metrics,
            "joins_removed": len(removed_joins)}
