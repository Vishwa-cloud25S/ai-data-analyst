"""Step 3 - SQL generation.

Two paths, same output contract:
  * LLM path   - prompt contains ONLY the retrieved semantic-layer slice.
  * Planner    - deterministic template compiler from Intent + semantic layer.
The planner always runs: it produces the fallback used offline/in CI, and its
SQL is what we fall back to if the LLM's SQL fails validation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.pipeline.intent import Intent
from app.pipeline.retrieval import SchemaRetriever
from app.pipeline.timeframe import TimeWindow, resolve_time_range
from app.semantic.layer import SemanticLayer

SYSTEM = """You are a senior analytics engineer that writes ONE read-only SQL SELECT
statement for a {dialect} warehouse.

Hard rules (violating any of them makes the answer useless):
1. Use ONLY the tables and columns listed in the provided schema. Never invent names.
2. Use the certified metric expressions verbatim, including their mandatory filters.
3. SELECT statements only. No DDL, DML, CTE-with-write, transactions, or multiple statements.
4. Always add an explicit LIMIT (max {max_rows}).
5. Qualify every column with its table name. Use the approved joins only.
6. Return dates as dates, not strings. Alias aggregates with readable snake_case names.
7. If the question cannot be answered from the schema, return sql as an empty string.

Respond with JSON: {{"sql": "...", "rationale": "one short sentence", "chart": "bar|line|table"}}"""


@dataclass
class GeneratedSQL:
    sql: str
    rationale: str
    chart: str
    source: str
    time_window: TimeWindow | None = None
    metric: str | None = None


# --- column -> entity resolution -------------------------------------------------
def _find_entity_for_column(sl: SemanticLayer, entities: list[str], column: str) -> str | None:
    for name in entities:
        e = sl.entities.get(name)
        if e and e.column(column):
            return name
    for name, e in sl.entities.items():  # widen search to joinable entities
        if e.column(column):
            return name
    return None


def plan_sql(
    question: str,
    intent: Intent,
    context: dict,
    sl: SemanticLayer,
    today: date | None = None,
    max_rows: int = 1000,
) -> GeneratedSQL:
    """Deterministic Intent -> SQL compiler over the semantic layer."""
    metric_name = next((m for m in intent.metrics if m in sl.metrics), None) \
        or next((m for m in context["metrics"] if m in sl.metrics), "total_revenue")
    metric = sl.metrics[metric_name]
    base = sl.entities[metric.entity]

    needed_entities = [metric.entity]
    group_cols: list[tuple[str, str]] = []  # (entity, column)
    for dim in intent.dimensions:
        ent = _find_entity_for_column(sl, [metric.entity] + context["entities"], dim)
        if ent and (ent, dim) not in group_cols:
            group_cols.append((ent, dim))
            if ent not in needed_entities:
                needed_entities.append(ent)

    window = resolve_time_range(intent.time_range, today=today)

    select_parts: list[str] = []
    group_by: list[str] = []
    order_by: str

    if intent.time_grain and intent.intent in ("trend", "comparison"):
        bucket = f"DATE_TRUNC('{intent.time_grain}', {base.name}.order_date)"
        select_parts.append(f"{bucket} AS period")
        group_by.append(bucket)

    for ent, col in group_cols:
        select_parts.append(f"{ent}.{col} AS {col}")
        group_by.append(f"{ent}.{col}")

    select_parts.append(f"{metric.expression} AS {metric.name}")

    joins: list[str] = []
    for ent in needed_entities:
        if ent == metric.entity:
            continue
        j = sl.join_clause(metric.entity, ent)
        if j:
            joins.append(f"{j.type.upper()} JOIN {sl.entities[ent].physical_table} AS {ent} ON {j.on}")

    where: list[str] = list(metric.filters)
    if window:
        where.append(window.sql(f"{base.name}.order_date"))

    if group_by:
        order_by = "period ASC" if intent.time_grain and intent.intent in ("trend", "comparison") \
            else f"{metric.name} DESC"
    else:
        order_by = ""

    limit = min(intent.limit or (10 if intent.intent == "ranking" else max_rows), max_rows)
    if intent.intent == "trend":
        limit = min(max(limit, 200), max_rows)

    sql_lines = [f"SELECT {', '.join(select_parts)}",
                 f"FROM {base.physical_table} AS {base.name}"]
    sql_lines += joins
    if where:
        sql_lines.append("WHERE " + "\n  AND ".join(where))
    if group_by:
        sql_lines.append("GROUP BY " + ", ".join(group_by))
    if order_by:
        sql_lines.append("ORDER BY " + order_by)
    sql_lines.append(f"LIMIT {limit}")
    sql = "\n".join(sql_lines)

    chart = "line" if intent.intent in ("trend",) else ("bar" if group_cols else "table")
    rationale = (
        f"Computed {metric.label} from {base.physical_table}"
        + (f" grouped by {', '.join(c for _, c in group_cols)}" if group_cols else "")
        + (f" for {window.label}" if window else "")
        + "; certified metric filters applied."
    )
    return GeneratedSQL(sql, rationale, chart, "planner", window, metric_name)


def generate_sql(
    question: str,
    intent: Intent,
    context: dict,
    retriever: SchemaRetriever,
    *,
    dialect: str = "duckdb",
    max_rows: int = 1000,
    today: date | None = None,
    use_llm: bool = True,
) -> GeneratedSQL:
    sl = retriever.sl
    planned = plan_sql(question, intent, context, sl, today=today, max_rows=max_rows)

    from app.llm.client import get_llm

    llm = get_llm()
    if not use_llm or not llm.available:
        return planned

    schema_prompt = retriever.render_schema_prompt(context)
    window = planned.time_window
    user = (
        f"Today is {(today or date.today()).isoformat()}.\n"
        f"Question: {question}\n\n"
        f"Detected intent: {intent.intent}; metrics={intent.metrics}; "
        f"dimensions={intent.dimensions}; grain={intent.time_grain}; range={intent.time_range}\n"
        + (f"Resolved time window: {window.start} to {window.end} ({window.label}). "
           f"Use these literal dates.\n" if window else "")
        + f"\n{schema_prompt}\n\n"
        f"A deterministic planner produced this reference SQL - improve on it only if the "
        f"question needs it, otherwise return it unchanged:\n{planned.sql}\n"
    )
    resp = llm.complete_json(
        SYSTEM.format(dialect=dialect, max_rows=max_rows),
        user,
        fallback={"sql": planned.sql, "rationale": planned.rationale, "chart": planned.chart},
    )
    sql = (resp.data.get("sql") or "").strip().rstrip(";")
    if not sql:
        return planned
    return GeneratedSQL(
        sql=sql,
        rationale=resp.data.get("rationale") or planned.rationale,
        chart=resp.data.get("chart") or planned.chart,
        source="offline-rules" if resp.offline else resp.model,
        time_window=window,
        metric=planned.metric,
    )
