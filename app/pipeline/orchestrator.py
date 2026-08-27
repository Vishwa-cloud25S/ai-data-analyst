"""The governed pipeline.

Question -> intent -> schema retrieval (RAG) -> SQL generation -> SQL validation
-> read-only execution -> result validation -> natural language explanation.

Every stage is recorded in `trace` so the UI and the tests can show exactly
what the system did and why. If validation fails, the pipeline may retry once
with the validator's error message fed back to the generator; if it still
fails, it falls back to the deterministic planner SQL; if that also fails the
request is refused. The LLM never touches a database connection.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.core.config import settings
from app.pipeline.executor import ExecutionError, get_executor
from app.pipeline.explainer import explain
from app.pipeline.generator import GeneratedSQL, generate_sql, plan_sql
from app.pipeline.intent import detect_intent
from app.pipeline.result_validator import validate_result
from app.pipeline.retrieval import get_retriever
from app.pipeline.validator import validate_sql
from app.semantic.layer import as_dict


@dataclass
class Stage:
    name: str
    status: str  # ok | blocked | error | skipped
    duration_ms: float
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class AnalysisResult:
    request_id: str
    question: str
    status: str  # answered | refused | error
    answer: str
    sql: str | None
    chart: dict[str, Any]
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    confidence: float
    intent: dict[str, Any]
    trace: list[Stage]
    warnings: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    def dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "question": self.question,
            "status": self.status,
            "answer": self.answer,
            "sql": self.sql,
            "chart": self.chart,
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "confidence": self.confidence,
            "intent": self.intent,
            "warnings": self.warnings,
            "issues": self.issues,
            "trace": [
                {"name": s.name, "status": s.status, "duration_ms": s.duration_ms,
                 "detail": s.detail}
                for s in self.trace
            ],
        }


OUT_OF_SCOPE = (
    "That is not something I can answer from the sales data, and I will not guess. "
    "Available metrics: revenue, margin, units, orders, average order value, "
    "return rate, active customers - by product, category, brand, region, channel, "
    "segment or country."
)

REFUSAL = (
    "I will not run that. I answer read-only questions about the certified sales "
    "data only - orders, products and customers - and nothing else in the "
    "warehouse is reachable from here."
)


class Analyst:
    def __init__(self, executor=None, retriever=None, use_llm: bool = True):
        self.retriever = retriever or get_retriever()
        self.executor = executor or get_executor()
        self.use_llm = use_llm

    # ------------------------------------------------------------------
    def ask(self, question: str, *, today: date | None = None) -> AnalysisResult:
        request_id = uuid.uuid4().hex[:12]
        trace: list[Stage] = []
        sl = self.retriever.sl

        def stage(name, status, t0, **detail):
            trace.append(Stage(name, status, round((time.perf_counter() - t0) * 1000, 2), detail))

        # 1 - intent -----------------------------------------------------
        t0 = time.perf_counter()
        intent = detect_intent(question, use_llm=self.use_llm)
        stage("intent_detection", "blocked" if intent.intent == "unsupported" else "ok", t0,
              **intent.dict())

        if intent.intent == "unsupported":
            return AnalysisResult(
                request_id, question, "refused", REFUSAL, None, {"type": "table"},
                [], [], 0, 0.0, intent.dict(), trace,
                issues=[intent.reason],
            )

        # 2 - schema retrieval (RAG) + scope gate -------------------------
        t0 = time.perf_counter()
        in_scope, matched = self.retriever.scope_check(question)
        context = self.retriever.retrieve_context(question)
        gate_applies = intent.intent != "metadata"
        stage("schema_retrieval", "ok" if (in_scope or not gate_applies) else "blocked", t0,
              in_scope=in_scope, matched_terms=matched,
              entities=context["entities"], metrics=context["metrics"],
              hits=context["hits"])

        if not in_scope and intent.intent != "metadata":
            # Nothing in the question maps to the semantic layer. Without this
            # gate the pipeline would fall back to the default metric and answer
            # an unrelated question with a real, confidently wrong number.
            return AnalysisResult(
                request_id, question, "refused", OUT_OF_SCOPE, None, {"type": "table"},
                [], [], 0, 0.0, intent.dict(), trace,
                issues=["No term in the question maps to the semantic layer."],
            )

        # Dimensions come from the semantic layer, not a hardcoded word list.
        from app.pipeline.intent import extract_dimensions, metric_vocabulary

        discovered = extract_dimensions(question, sl, skip=metric_vocabulary(sl))
        merged = [d for d in intent.dimensions if sl.resolve_grouping(d)] + [
            d for d in discovered if d not in intent.dimensions
        ]
        intent.dimensions = list(dict.fromkeys(merged))

        if intent.intent == "metadata":
            answer = self._describe_schema(context)
            stage("metadata_answer", "ok", time.perf_counter())
            return AnalysisResult(
                request_id, question, "answered", answer, None, {"type": "table"},
                [], [], 0, 1.0, intent.dict(), trace,
            )

        # 3 - SQL generation ---------------------------------------------
        t0 = time.perf_counter()
        gen = generate_sql(
            question, intent, context, self.retriever,
            dialect=self.executor.engine, max_rows=settings.max_rows,
            today=today, use_llm=self.use_llm,
        )
        stage("sql_generation", "ok", t0, source=gen.source, rationale=gen.rationale,
              sql=gen.sql, time_window=(gen.time_window.label if gen.time_window else None))

        # 4 - SQL validation (+ one repair attempt, then planner fallback)
        t0 = time.perf_counter()
        vr = validate_sql(gen.sql, sl, dialect=self.executor.engine,
                          max_rows=settings.max_rows, max_joins=settings.max_joins)
        attempts = [{"attempt": 1, "ok": vr.ok, "errors": vr.errors}]

        if not vr.ok:
            planned = plan_sql(question, intent, context, sl, today=today,
                               max_rows=settings.max_rows)
            vr2 = validate_sql(planned.sql, sl, dialect=self.executor.engine,
                               max_rows=settings.max_rows, max_joins=settings.max_joins)
            attempts.append({"attempt": 2, "strategy": "deterministic-planner",
                             "ok": vr2.ok, "errors": vr2.errors})
            if vr2.ok:
                gen = planned
                vr = vr2

        stage("sql_validation", "ok" if vr.ok else "blocked", t0,
              checks=vr.checks, errors=vr.errors, warnings=vr.warnings,
              tables=vr.tables, columns=vr.columns, attempts=attempts)

        if not vr.ok:
            return AnalysisResult(
                request_id, question, "refused",
                "I generated a query but it failed the safety and schema checks, so I did not "
                "run it. " + REFUSAL,
                gen.sql, {"type": "table"}, [], [], 0, 0.0, intent.dict(), trace,
                issues=vr.errors,
            )

        safe_sql = vr.sql
        gen = GeneratedSQL(safe_sql, gen.rationale, gen.chart, gen.source,
                           gen.time_window, gen.metric)

        # 5 - read-only execution ----------------------------------------
        t0 = time.perf_counter()
        try:
            result = self.executor.execute(safe_sql)
        except ExecutionError as exc:
            stage("execution", "error", t0, error=str(exc), read_only=True)
            return AnalysisResult(
                request_id, question, "error",
                f"The validated query could not be executed: {exc}",
                safe_sql, {"type": "table"}, [], [], 0, 0.0, intent.dict(), trace,
                issues=[str(exc)],
            )
        stage("execution", "ok", t0, engine=result.engine, row_count=result.row_count,
              duration_ms=result.duration_ms, read_only=True, truncated=result.truncated)

        # 6 - result validation ------------------------------------------
        t0 = time.perf_counter()
        rv = validate_result(result, sl, metric=gen.metric)
        stage("result_validation", "ok" if rv.ok else "blocked", t0,
              confidence=rv.confidence, issues=rv.issues, notes=rv.notes, checks=rv.checks)

        if not rv.ok:
            return AnalysisResult(
                request_id, question, "refused",
                "The query returned results that failed sanity checks, so I am not reporting "
                "them as an answer. Issues: " + "; ".join(rv.issues),
                safe_sql, {"type": "table"}, result.columns, result.rows, result.row_count,
                rv.confidence, intent.dict(), trace, warnings=vr.warnings, issues=rv.issues,
            )

        # 7 - explanation --------------------------------------------------
        t0 = time.perf_counter()
        exp = explain(question, result, gen, intent, sl, use_llm=self.use_llm,
                      validation_notes=rv.issues + rv.notes)
        stage("explanation", "ok", t0, source=exp.source, chart=exp.chart)

        return AnalysisResult(
            request_id=request_id, question=question, status="answered",
            answer=exp.text, sql=safe_sql, chart=exp.chart,
            columns=result.columns, rows=result.rows, row_count=result.row_count,
            confidence=rv.confidence, intent=intent.dict(), trace=trace,
            warnings=vr.warnings + rv.notes, issues=rv.issues,
        )

    # ------------------------------------------------------------------
    def _describe_schema(self, context: dict) -> str:
        sl = self.retriever.sl
        lines = ["Here is what I can answer questions about:", "", "Metrics:"]
        for m in sl.metrics.values():
            lines.append(f"  - {m.label} ({m.name}): {m.description}")
        lines += ["", "Tables and dimensions:"]
        for e in sl.entities.values():
            lines.append(f"  - {e.name}: {', '.join(c.name for c in e.dimensions)}")
        return "\n".join(lines)


_analyst: Analyst | None = None


def get_analyst() -> Analyst:
    global _analyst
    if _analyst is None:
        _analyst = Analyst()
    return _analyst


def semantic_layer_summary() -> dict:
    return as_dict(get_retriever().sl)
