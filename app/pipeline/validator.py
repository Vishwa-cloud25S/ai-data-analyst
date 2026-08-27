"""Step 4 - SQL validation. The security boundary of the system.

Nothing reaches the warehouse without passing every check here. Validation is
AST-based (sqlglot), not regex-based, so comment tricks and casing games do not
help an attacker. Regex checks exist only as a cheap pre-filter.

Checks:
  1. Parses as exactly ONE statement.
  2. Statement is a SELECT (CTEs allowed, no write DDL/DML anywhere in the tree).
  3. Every referenced table is in the semantic-layer allow-list.
  4. Every referenced column exists on an allowed entity.
  5. No banned functions (file/system/network access, sleep, etc.).
  6. No subquery-free cartesian explosion: join count is capped.
  7. A LIMIT <= max_rows is present, or one is injected.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from app.semantic.layer import SemanticLayer

WRITE_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
    exp.TruncateTable, exp.Merge, exp.Grant,
)

BANNED_FUNCTIONS = {
    "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto",
    "read_text", "read_blob", "glob", "copy", "install", "load", "attach",
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_sleep", "lo_import",
    "lo_export", "dblink", "system", "shell", "sleep", "getenv", "url",
    "httpfs", "parquet_scan", "csv_scan", "query", "duckdb_settings",
}

BANNED_TABLE_PREFIXES = (
    "pg_", "information_schema", "duckdb_", "sqlite_", "pg_catalog",
)

_CHEAP_BLOCK = re.compile(
    r";\s*\S|--\s*\bdrop\b|/\*.*\b(drop|delete|insert|update)\b.*\*/",
    re.I | re.S,
)


class ValidationError(Exception):
    pass


@dataclass
class ValidationResult:
    ok: bool
    sql: str                       # normalised / limit-injected SQL
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)


def _table_key(t: exp.Table) -> str:
    return (t.name or "").lower()


def validate_sql(
    sql: str,
    sl: SemanticLayer,
    *,
    dialect: str = "duckdb",
    max_rows: int = 1000,
    max_joins: int = 4,
) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, bool] = {}
    sql = (sql or "").strip().rstrip(";")

    if not sql:
        return ValidationResult(False, sql, ["Empty SQL: the question could not be answered "
                                             "from the semantic layer."], checks={"non_empty": False})

    checks["no_stacked_statements_regex"] = not bool(_CHEAP_BLOCK.search(sql))
    if not checks["no_stacked_statements_regex"]:
        errors.append("SQL contains stacked statements or suspicious comments.")

    try:
        statements = sqlglot.parse(sql, read=dialect)
    except Exception as exc:
        return ValidationResult(False, sql, [f"SQL failed to parse: {exc}"],
                                checks={**checks, "parses": False})
    checks["parses"] = True

    statements = [s for s in statements if s is not None]
    checks["single_statement"] = len(statements) == 1
    if not checks["single_statement"]:
        errors.append(f"Expected exactly 1 statement, found {len(statements)}.")
        return ValidationResult(False, sql, errors, warnings, checks=checks)

    tree = statements[0]

    checks["is_select"] = isinstance(tree, (exp.Select, exp.Union, exp.Subquery))
    if not checks["is_select"]:
        errors.append(f"Only SELECT statements are allowed, got {type(tree).__name__.upper()}.")

    checks["no_write_nodes"] = not any(tree.find_all(*WRITE_NODES))
    if not checks["no_write_nodes"]:
        errors.append("Statement contains a write / DDL operation.")

    if errors:
        # Fail fast: a non-SELECT statement must never be analysed further.
        return ValidationResult(False, sql, errors, warnings, checks=checks)

    # --- tables ---
    tables = sorted({_table_key(t) for t in tree.find_all(exp.Table) if t.name})
    allowed = sl.allowed_tables
    # CTE names are legal virtual tables.
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    bad_tables = [
        t for t in tables
        if t not in allowed and t not in cte_names
    ]
    checks["tables_allowed"] = not bad_tables
    if bad_tables:
        errors.append(
            "Table(s) not in the semantic layer: " + ", ".join(sorted(bad_tables))
            + ". Allowed: " + ", ".join(sorted(e.name for e in sl.entities.values()))
        )
    sys_tables = [t for t in tables if t.startswith(BANNED_TABLE_PREFIXES)]
    checks["no_system_tables"] = not sys_tables
    if sys_tables:
        errors.append("System catalog access is forbidden: " + ", ".join(sys_tables))

    # --- functions ---
    used_funcs = set()
    for node in tree.find_all(exp.Anonymous):
        used_funcs.add((node.this or "").lower())
    for node in tree.find_all(exp.Func):
        name = node.sql_name().lower() if hasattr(node, "sql_name") else ""
        if name:
            used_funcs.add(name)
    bad_funcs = sorted(used_funcs & BANNED_FUNCTIONS)
    checks["no_banned_functions"] = not bad_funcs
    if bad_funcs:
        errors.append("Forbidden function(s): " + ", ".join(bad_funcs))

    # --- columns ---
    known_columns = sl.allowed_columns
    alias_targets: set[str] = {
        (a.alias or "").lower() for a in tree.find_all(exp.Alias) if a.alias
    }
    referenced: list[str] = []
    bad_columns: list[str] = []
    for col in tree.find_all(exp.Column):
        cname = (col.name or "").lower()
        if not cname or cname == "*":
            continue
        referenced.append(cname)
        if cname in known_columns or cname in alias_targets or cname in cte_names:
            continue
        bad_columns.append(cname)
    bad_columns = sorted(set(bad_columns))
    checks["columns_allowed"] = not bad_columns
    if bad_columns:
        errors.append("Unknown column(s) for the semantic layer: " + ", ".join(bad_columns))

    # --- star projection ---
    checks["no_select_star"] = not any(
        isinstance(e, exp.Star) for s in tree.find_all(exp.Select) for e in s.expressions
    )
    if not checks["no_select_star"]:
        errors.append("SELECT * is not allowed; project explicit columns.")

    # --- joins ---
    n_joins = len(list(tree.find_all(exp.Join)))
    checks["join_count_ok"] = n_joins <= max_joins
    if not checks["join_count_ok"]:
        errors.append(f"Too many joins ({n_joins} > {max_joins}).")
    for j in tree.find_all(exp.Join):
        if not (j.args.get("on") or j.args.get("using")):
            warnings.append("Join without an ON/USING clause (possible cartesian product).")
            errors.append("Cross join / join without ON clause is not allowed.")
            checks["no_cross_join"] = False
    checks.setdefault("no_cross_join", True)

    # --- limit ---
    root_select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    limit_node = tree.args.get("limit") or (root_select.args.get("limit") if root_select else None)
    if limit_node is None:
        tree = tree.limit(max_rows)
        warnings.append(f"No LIMIT present; injected LIMIT {max_rows}.")
        checks["limit_present"] = False
    else:
        try:
            current = int(limit_node.expression.this)
            if current > max_rows:
                tree = tree.limit(max_rows)
                warnings.append(f"LIMIT {current} exceeded max_rows; reduced to {max_rows}.")
        except Exception:
            warnings.append("Non-literal LIMIT; replaced with max_rows.")
            tree = tree.limit(max_rows)
        checks["limit_present"] = True

    normalised = tree.sql(dialect=dialect, pretty=True)
    return ValidationResult(
        ok=not errors,
        sql=normalised,
        errors=errors,
        warnings=warnings,
        tables=tables,
        columns=sorted(set(referenced)),
        checks=checks,
    )
