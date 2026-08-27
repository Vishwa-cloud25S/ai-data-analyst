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


class HealthResponse(BaseModel):
    status: str
    warehouse: str
    llm: str
    entities: int
    metrics: int
