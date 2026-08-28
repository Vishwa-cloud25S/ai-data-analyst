from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.schemas import (
    AskRequest,
    AskResponse,
    AuditStatsResponse,
    HealthResponse,
    LayerSaveRequest,
    LayerValidateRequest,
    ValidateRequest,
    ValidateResponse,
    WhoAmIResponse,
)
from app.core.audit import AuditEvent, get_audit_log
from app.core.config import settings
from app.core.security import (
    Principal,
    get_keyring,
    require,
    require_authenticated,
)
from app.llm.client import get_llm
from app.pipeline.orchestrator import Analyst, get_analyst, semantic_layer_summary
from app.pipeline.retrieval import get_retriever
from app.pipeline.validator import validate_sql

log = logging.getLogger(__name__)
router = APIRouter()


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


@router.get("/health", response_model=HealthResponse, tags=["meta"])
def health() -> HealthResponse:
    """Unauthenticated on purpose: platform health checks must reach it."""
    sl = get_retriever().sl
    return HealthResponse(
        status="ok",
        warehouse=(settings.warehouse_url.split("://")[0]
                   if settings.warehouse_url else settings.warehouse),
        llm=get_llm().describe(),
        llm_provider=get_llm().provider,
        entities=len(sl.entities),
        metrics=len(sl.metrics),
        auth_enabled=settings.auth_enabled,
        audit_enabled=settings.audit_enabled,
    )


@router.get("/whoami", response_model=WhoAmIResponse, tags=["meta"])
def whoami(principal: Principal = Depends(require("viewer"))) -> WhoAmIResponse:
    return WhoAmIResponse(
        name=principal.name, role=principal.role,
        authenticated=principal.authenticated,
    )


@router.get("/semantic-layer", tags=["meta"])
def semantic_layer(principal: Principal = Depends(require("viewer"))) -> dict:
    """The full contract the LLM is allowed to see. Useful for debugging + docs."""
    return semantic_layer_summary()


@router.post("/ask", response_model=AskResponse, tags=["analyst"])
def ask(
    req: AskRequest,
    request: Request,
    principal: Principal = Depends(require("viewer")),
) -> AskResponse:
    analyst = get_analyst()
    if not req.use_llm:
        # Reuse the configured executor/retriever; only the LLM is switched off.
        analyst = Analyst(executor=analyst.executor, retriever=analyst.retriever,
                          use_llm=False)
    started = time.perf_counter()
    try:
        result = analyst.ask(req.question)
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("pipeline failure")
        if settings.audit_enabled:
            get_audit_log().record(AuditEvent(
                request_id="-", principal=principal.name, role=principal.role,
                question=req.question, status="error", client_ip=_client_ip(request),
                issues=[str(exc)],
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            ))
        raise HTTPException(status_code=500, detail=f"Pipeline failure: {exc}") from exc

    if settings.audit_enabled:
        blocked = next((s.name for s in result.trace if s.status == "blocked"), None)
        tables = next(
            (s.detail.get("tables", []) for s in result.trace if s.name == "sql_validation"),
            [],
        )
        get_audit_log().record(AuditEvent(
            request_id=result.request_id,
            principal=principal.name,
            role=principal.role,
            client_ip=_client_ip(request),
            question=req.question,
            status=result.status,
            blocked_stage=blocked,
            sql=result.sql,
            tables=list(tables),
            row_count=result.row_count,
            confidence=result.confidence,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            llm_used=req.use_llm and get_llm().available,
            issues=result.issues,
        ))

    return AskResponse(**result.dict())


@router.post("/validate-sql", response_model=ValidateResponse, tags=["analyst"])
def validate(
    req: ValidateRequest,
    principal: Principal = Depends(require("analyst")),
) -> ValidateResponse:
    """Expose the guardrail directly so it can be tested / audited in isolation."""
    from app.pipeline.executor import get_executor

    vr = validate_sql(
        req.sql, get_retriever().sl, dialect=get_executor().engine,
        max_rows=settings.max_rows, max_joins=settings.max_joins,
    )
    return ValidateResponse(ok=vr.ok, sql=vr.sql, errors=vr.errors, warnings=vr.warnings,
                            tables=vr.tables, columns=vr.columns, checks=vr.checks)


@router.get("/audit", tags=["audit"])
def audit_events(
    principal: Principal = Depends(require("admin")),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None, pattern="^(answered|refused|error)$"),
    user: str | None = Query(None, max_length=120),
    since: str | None = Query(None, description="ISO-8601 timestamp"),
) -> dict:
    """Who asked what, what SQL ran, and what was refused. Admin only."""
    events = get_audit_log().query(
        limit=limit, offset=offset, principal=user, status=status, since=since
    )
    return {"count": len(events), "events": events}


@router.get("/audit/stats", response_model=AuditStatsResponse, tags=["audit"])
def audit_stats(principal: Principal = Depends(require("admin"))) -> AuditStatsResponse:
    return AuditStatsResponse(**get_audit_log().stats())


@router.get("/principals", tags=["audit"])
def principals(principal: Principal = Depends(require("admin"))) -> dict:
    """Configured identities and roles. Never returns key material."""
    return {"auth_enabled": settings.auth_enabled, "principals": get_keyring().describe()}


# --------------------------------------------------------------------------
# Semantic layer editing. Admin only: whoever can edit the layer can decide
# what the model is able to reach, so this is the highest-privilege surface
# in the system. Every change is validated, backed up and audited.
# --------------------------------------------------------------------------
@router.get("/semantic-layer/raw", tags=["semantic-layer"])
def layer_raw(principal: Principal = Depends(require("admin"))) -> dict:
    from app.semantic import editor

    return {"path": str(editor.layer_path()), "yaml": editor.read_raw()}


@router.post("/semantic-layer/validate", tags=["semantic-layer"])
def layer_validate(
    req: LayerValidateRequest,
    principal: Principal = Depends(require("admin")),
) -> dict:
    """Dry run: parse, load, and execute every entity and metric."""
    from app.pipeline.executor import get_executor
    from app.semantic import editor

    executor = get_executor() if req.check_warehouse else None
    report = editor.validate_yaml_text(req.yaml, executor=executor)
    return {**report.dict(), "diff": editor.diff_summary(editor.read_raw(), req.yaml)}


@router.put("/semantic-layer/raw", tags=["semantic-layer"])
def layer_save(
    req: LayerSaveRequest,
    request: Request,
    principal: Principal = Depends(require_authenticated("admin")),
) -> dict:
    from app.pipeline.executor import get_executor
    from app.semantic import editor

    previous = editor.read_raw()
    report = editor.validate_yaml_text(req.yaml, executor=get_executor())
    diff = editor.diff_summary(previous, req.yaml)

    if not report.ok:
        # Never persist a layer that does not load and execute.
        raise HTTPException(
            status_code=422,
            detail={"message": "Semantic layer rejected; nothing was saved.",
                    **report.dict(), "diff": diff},
        )

    editor.save(req.yaml, author=principal.name)
    editor.reload_caches()

    if settings.audit_enabled:
        exposed = diff["entities_added"] + diff["columns_added"]
        get_audit_log().record(AuditEvent(
            request_id="layer-edit",
            principal=principal.name,
            role=principal.role,
            client_ip=_client_ip(request),
            question=f"EDIT semantic layer: {req.message or '(no message)'}",
            status="answered",
            sql=None,
            tables=diff["entities_added"] + diff["entities_removed"],
            issues=([f"exposed: {', '.join(exposed)}"] if exposed else []),
        ))
    log.warning("semantic layer edited by %s: %s", principal.name, diff)
    return {"saved": True, "diff": diff, **report.dict()}


@router.get("/semantic-layer/versions", tags=["semantic-layer"])
def layer_versions(principal: Principal = Depends(require("admin"))) -> dict:
    from app.semantic import editor

    return {"versions": editor.list_versions()}


@router.post("/semantic-layer/restore/{version_id}", tags=["semantic-layer"])
def layer_restore(
    version_id: str,
    request: Request,
    principal: Principal = Depends(require_authenticated("admin")),
) -> dict:
    from app.pipeline.executor import get_executor
    from app.semantic import editor

    try:
        text = editor.read_version(version_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"No such version: {version_id}") from None

    report = editor.validate_yaml_text(text, executor=get_executor())
    if not report.ok:
        raise HTTPException(status_code=422, detail={
            "message": "That version no longer validates against the warehouse.",
            **report.dict()})

    diff = editor.diff_summary(editor.read_raw(), text)
    editor.save(text, author=f"{principal.name}-restore")
    editor.reload_caches()
    if settings.audit_enabled:
        get_audit_log().record(AuditEvent(
            request_id="layer-restore", principal=principal.name, role=principal.role,
            client_ip=_client_ip(request),
            question=f"RESTORE semantic layer version {version_id}", status="answered",
        ))
    return {"restored": version_id, "diff": diff}


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
