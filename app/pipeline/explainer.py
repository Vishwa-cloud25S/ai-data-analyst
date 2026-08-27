"""Step 7 - Natural language explanation + chart spec.

The LLM never sees the database; it only sees a compact, already-validated
result summary. With no API key a deterministic template narrator is used.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.pipeline.executor import QueryResult
from app.pipeline.generator import GeneratedSQL
from app.pipeline.intent import Intent
from app.semantic.layer import SemanticLayer

SYSTEM = """You are a data analyst writing the answer to a business question.
You are given the question and the FULL result of a validated SQL query.
Rules: use only numbers present in the result; never invent figures; be concise
(2-4 sentences); lead with the direct answer; mention the time window and one
notable pattern. No markdown headers, no bullet lists longer than 3 items."""


@dataclass
class Explanation:
    text: str
    chart: dict[str, Any]
    source: str


def _fmt(value: Any, fmt: str = "number") -> str:
    if not isinstance(value, (int, float)):
        return str(value)
    if fmt == "currency":
        return f"${value:,.0f}" if abs(value) >= 1000 else f"${value:,.2f}"
    if fmt == "percent":
        return f"{value * 100:.1f}%"
    return f"{value:,.0f}" if abs(value) >= 1000 else f"{value:,.2f}"


def build_chart_spec(result: QueryResult, gen: GeneratedSQL, intent: Intent) -> dict[str, Any]:
    if not result.columns or result.row_count == 0:
        return {"type": "table", "x": None, "y": None}
    numeric = [
        c for i, c in enumerate(result.columns)
        if any(isinstance(r[i], (int, float)) and not isinstance(r[i], bool) for r in result.rows)
    ]
    categorical = [c for c in result.columns if c not in numeric]
    y = numeric[-1] if numeric else None
    x = None
    if "period" in result.columns:
        x, ctype = "period", "line"
    elif categorical:
        x, ctype = categorical[0], "bar"
    else:
        ctype = "table"
    if gen.chart in ("bar", "line", "table") and x:
        ctype = gen.chart if gen.chart != "table" else ctype
    if len(result.columns) == 1:
        ctype = "table"
    return {"type": ctype, "x": x, "y": y,
            "title": f"{y or 'result'} by {x}" if x and y else "Result"}


def _template_explanation(
    question: str, result: QueryResult, gen: GeneratedSQL, intent: Intent, sl: SemanticLayer
) -> str:
    if result.row_count == 0:
        return ("The query ran successfully but returned no rows for "
                f"{gen.time_window.label if gen.time_window else 'the requested period'}. "
                "Try widening the time range or removing filters.")

    metric_name = gen.metric or (intent.metrics[0] if intent.metrics else None)
    metric = sl.metrics.get(metric_name) if metric_name else None
    fmt = metric.format if metric else "number"
    label = metric.label if metric else (result.columns[-1] if result.columns else "value")
    window = f" for {gen.time_window.label}" if gen.time_window else ""

    metric_idx = result.columns.index(metric_name) if metric_name in result.columns else len(result.columns) - 1
    key_idx = 0 if result.columns[0] != metric_name else None

    if result.row_count == 1 and len(result.columns) == 1:
        return f"{label}{window} was {_fmt(result.rows[0][0], fmt)}."

    values = [r[metric_idx] for r in result.rows if isinstance(r[metric_idx], (int, float))]
    total = sum(values)
    additive = fmt in ("currency", "number") and metric_name not in (
        "average_order_value", "return_rate"
    )
    parts: list[str] = []

    def key(row):
        v = row[key_idx]
        return str(v)[:10] if isinstance(v, str) and "T00:00:00" in str(v) else v

    if intent.intent == "trend" and key_idx is not None and len(result.rows) >= 2:
        first, last = result.rows[0], result.rows[-1]
        change = ((last[metric_idx] - first[metric_idx]) / first[metric_idx] * 100
                  if first[metric_idx] else 0)
        peak = max(result.rows, key=lambda r: r[metric_idx])
        direction = "up" if change >= 0 else "down"
        parts.append(
            f"{label}{window} moved from {_fmt(first[metric_idx], fmt)} in {key(first)} to "
            f"{_fmt(last[metric_idx], fmt)} in {key(last)}, {direction} {abs(change):.1f}%"
        )
        parts.append(f"The peak period was {key(peak)} at {_fmt(peak[metric_idx], fmt)}")
        if additive:
            parts.append(f"Total across the {result.row_count} periods was {_fmt(total, fmt)}")
    elif key_idx is not None:
        top = result.rows[0]
        parts.append(f"{label}{window} was led by {key(top)} at {_fmt(top[metric_idx], fmt)}")
        if result.row_count >= 3:
            runners = ", ".join(
                f"{key(r)} ({_fmt(r[metric_idx], fmt)})" for r in result.rows[1:3]
            )
            parts.append(f"followed by {runners}")
        if additive and total:
            share = top[metric_idx] / total * 100
            parts.append(
                f"The leader accounts for {share:.1f}% of the {_fmt(total, fmt)} "
                f"across the {result.row_count} rows returned"
            )
        else:
            parts.append(
                f"The lowest was {key(result.rows[-1])} at "
                f"{_fmt(result.rows[-1][metric_idx], fmt)}"
            )
    else:
        parts.append(f"{label}{window} totalled {_fmt(total, fmt)} across "
                     f"{result.row_count} rows")
    return ". ".join(p.rstrip(".") for p in parts) + "."


def explain(
    question: str,
    result: QueryResult,
    gen: GeneratedSQL,
    intent: Intent,
    sl: SemanticLayer,
    *,
    use_llm: bool = True,
    validation_notes: list[str] | None = None,
) -> Explanation:
    chart = build_chart_spec(result, gen, intent)
    fallback = _template_explanation(question, result, gen, intent, sl)

    from app.llm.client import get_llm

    llm = get_llm()
    if not use_llm or not llm.available:
        return Explanation(fallback, chart, "template")

    preview = result.to_records()[:25]
    user = (
        f"Question: {question}\n"
        f"Time window: {gen.time_window.label if gen.time_window else 'not specified'}\n"
        f"SQL executed:\n{gen.sql}\n\n"
        f"Columns: {result.columns}\n"
        f"Rows ({result.row_count} returned, showing up to 25):\n{preview}\n"
        + (f"Validation notes: {validation_notes}\n" if validation_notes else "")
    )
    text = llm.complete_text(SYSTEM, user, fallback=fallback)
    return Explanation(text, chart, "template" if text == fallback else llm.model)
