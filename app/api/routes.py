from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app.api.schemas import (
    AskRequest,
    AskResponse,
    HealthResponse,
    ValidateRequest,
    ValidateResponse,
)
from app.core.config import settings
from app.llm.client import get_llm
from app.pipeline.orchestrator import Analyst, get_analyst, semantic_layer_summary
from app.pipeline.retrieval import get_retriever
from app.pipeline.validator import validate_sql

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health", response_model=HealthResponse, tags=["meta"])
def health() -> HealthResponse:
    sl = get_retriever().sl
    return HealthResponse(
        status="ok",
        warehouse=settings.warehouse,
        llm=settings.openai_model if get_llm().available else "offline-rules",
        entities=len(sl.entities),
        metrics=len(sl.metrics),
    )


@router.get("/semantic-layer", tags=["meta"])
def semantic_layer() -> dict:
    """The full contract the LLM is allowed to see. Useful for debugging + docs."""
    return semantic_layer_summary()


@router.post("/ask", response_model=AskResponse, tags=["analyst"])
def ask(req: AskRequest) -> AskResponse:
    analyst = get_analyst() if req.use_llm else Analyst(use_llm=False)
    try:
        result = analyst.ask(req.question)
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("pipeline failure")
        raise HTTPException(status_code=500, detail=f"Pipeline failure: {exc}") from exc
    return AskResponse(**result.dict())


@router.post("/validate-sql", response_model=ValidateResponse, tags=["analyst"])
def validate(req: ValidateRequest) -> ValidateResponse:
    """Expose the guardrail directly so it can be tested / audited in isolation."""
    vr = validate_sql(
        req.sql, get_retriever().sl,
        dialect=settings.warehouse if settings.warehouse != "postgres" else "postgres",
        max_rows=settings.max_rows, max_joins=settings.max_joins,
    )
    return ValidateResponse(ok=vr.ok, sql=vr.sql, errors=vr.errors, warnings=vr.warnings,
                            tables=vr.tables, columns=vr.columns, checks=vr.checks)


@router.get("/examples", tags=["meta"])
def examples() -> dict:
    return {
        "questions": [
            "What were our highest revenue products last quarter?",
            "Show me revenue by month for the last 12 months",
            "Which region had the best gross margin this year?",
            "Top 5 categories by units sold in 2025",
            "What is our average order value by channel?",
            "Which customer segment returns the most?",
        ]
    }
