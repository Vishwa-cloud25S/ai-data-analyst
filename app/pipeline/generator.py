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
    #: dimensions the user asked for that the semantic layer cannot provide.
    #: Answering without them would silently answer a different question.
    unresolved: tuple[str, ...] = ()


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
    """Deterministic Intent -> SQL compiler over the semantic layer.

    Everything schema-specific is read from the layer: which column carries
    time, what a word like "genre" means, and how to reach another entity.
    Nothing here may assume the demo schema.
    """
    metric_name = next((m for m in intent.metrics if m in sl.metrics), None) \
        or next((m for m in context["metrics"] if m in sl.metrics), None) \
        or next(iter(sl.metrics), "")
    metric = sl.metrics[metric_name]
    base = sl.entities[metric.entity]

    # --- resolve requested groupings against the layer, not a hardcoded list
    needed_entities = [metric.entity]
    group_cols: list[tuple[str, str]] = []
    unresolved: list[str] = []
    for dim in intent.dimensions:
        hit = sl.resolve_grouping(dim, prefer=[metric.entity] + context["entities"])
        if hit is None:
            unresolved.append(dim)
            continue
        ent, col = hit
        if (ent, col) in group_cols:
            continue
        if ent != metric.entity and sl.join_path(metric.entity, ent) is None:
            unresolved.append(dim)
            continue
        group_cols.append((ent, col))
        if ent not in needed_entities:
            needed_entities.append(ent)

    window = resolve_time_range(intent.time_range, today=today)
    wants_time = bool(window) or bool(
        intent.time_grain and intent.intent in ("trend", "comparison")
    )
    time_col = sl.time_dimension(metric.entity)
    time_entity = metric.entity
    if time_col is None and wants_time:
        # The fact table may carry no date; borrow one from a joinable entity.
        # Only when time is actually needed, or we join a table for nothing.
        for name in sl.entities:
            if name == metric.entity:
                continue
            candidate = sl.time_dimension(name)
            if candidate and sl.join_path(metric.entity, name) is not None:
                time_col, time_entity = candidate, name
                if name not in needed_entities:
                    needed_entities.append(name)
                break

    if wants_time and time_col is None:
        unresolved.append(intent.time_range or intent.time_grain or "time")
        window = None

    select_parts: list[str] = []
    group_by: list[str] = []

    if intent.time_grain and intent.intent in ("trend", "comparison") and time_col:
        bucket = f"DATE_TRUNC('{intent.time_grain}', {time_entity}.{time_col})"
        select_parts.append(f"{bucket} AS period")
        group_by.append(bucket)

    for ent, col in group_cols:
        alias = col if col.lower() != col else col
        select_parts.append(f"{ent}.{col} AS {alias}")
        group_by.append(f"{ent}.{col}")

    select_parts.append(f"{metric.expression} AS {metric.name}")

    # --- multi-hop joins: InvoiceLine reaches Customer only through Invoice
    joins: list[str] = []
    joined: set[str] = {metric.entity}
    for ent in needed_entities:
        if ent in joined:
            continue
        path = sl.join_path(metric.entity, ent)
        if path is None:
            continue
        for j in path:
            other = j.right if j.left in joined else j.left
            if other in joined:
                continue
            joins.append(
                f"{j.type.upper()} JOIN {sl.entities[other].physical_table} AS {other} ON {j.on}"
            )
            joined.add(other)

    where: list[str] = list(metric.filters)
    if window and time_col:
        where.append(window.sql(f"{time_entity}.{time_col}"))

    order_by = ""
    if group_by:
        order_by = ("period ASC"
                    if intent.time_grain and intent.intent in ("trend", "comparison")
                    else f"{metric.name} DESC")

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

    chart = "line" if intent.intent == "trend" else ("bar" if group_cols else "table")
    rationale = (
        f"Computed {metric.label} from {base.physical_table}"
        + (f" grouped by {', '.join(c for _, c in group_cols)}" if group_cols else "")
        + (f" for {window.label}" if window else "")
        + "; certified metric filters applied."
    )
    return GeneratedSQL(sql, rationale, chart, "planner", window, metric_name,
                        tuple(dict.fromkeys(unresolved)))


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
