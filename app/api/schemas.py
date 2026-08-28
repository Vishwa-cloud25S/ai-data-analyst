from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=1000,
                          examples=["What were our highest revenue products last quarter?"])
    use_llm: bool = Field(True, description="Set false to force the deterministic planner.")


class TraceStage(BaseModel):
    name: str
    status: str
    duration_ms: float
    detail: dict[str, Any] = {}


class AskResponse(BaseModel):
    request_id: str
    question: str
    status: str
    answer: str
    sql: str | None = None
    chart: dict[str, Any] = {}
    columns: list[str] = []
    rows: list[list[Any]] = []
    row_count: int = 0
    confidence: float = 0.0
    intent: dict[str, Any] = {}
    warnings: list[str] = []
    issues: list[str] = []
    trace: list[TraceStage] = []


class ValidateRequest(BaseModel):
    sql: str = Field(..., min_length=1, max_length=20000)


class ValidateResponse(BaseModel):
    ok: bool
    sql: str
    errors: list[str] = []
    warnings: list[str] = []
    tables: list[str] = []
    columns: list[str] = []
    checks: dict[str, bool] = {}


class LayerValidateRequest(BaseModel):
    yaml: str = Field(..., min_length=1, max_length=500_000)
    check_warehouse: bool = Field(True, description="Execute entities and metrics too.")


class LayerSaveRequest(BaseModel):
    yaml: str = Field(..., min_length=1, max_length=500_000)
    message: str | None = Field(None, max_length=300, description="Why this changed.")


class HealthResponse(BaseModel):
    status: str
    warehouse: str
    llm: str
    llm_provider: str = "none"
    entities: int
    metrics: int
    auth_enabled: bool = False
    anonymous_role: str = "analyst"
    audit_enabled: bool = True


class WhoAmIResponse(BaseModel):
    name: str
    role: str
    authenticated: bool


class AuditStatsResponse(BaseModel):
    total_questions: int
    by_status: dict[str, int] = {}
    refusals_by_stage: dict[str, int] = {}
    refusal_rate: float = 0.0
    top_users: list[dict[str, Any]] = []
    first_event: str | None = None
    last_event: str | None = None
