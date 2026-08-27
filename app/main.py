from __future__ import annotations

import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.core.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("ai-data-analyst")

DESCRIPTION = """
Governed natural-language analytics.

The LLM never receives database credentials and never executes SQL. Every
question flows through: intent detection -> schema retrieval (RAG over the
semantic layer + dbt metadata) -> SQL generation -> AST-based SQL validation
against an allow-list -> read-only execution -> result validation ->
natural-language explanation.
"""

app = FastAPI(
    title="AI Data Analyst",
    description=DESCRIPTION,
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_logger(request: Request, call_next):
    rid = uuid.uuid4().hex[:8]
    start = time.perf_counter()
    response = await call_next(request)
    ms = round((time.perf_counter() - start) * 1000, 2)
    log.info("rid=%s %s %s -> %s (%sms)", rid, request.method, request.url.path,
             response.status_code, ms)
    response.headers["X-Request-ID"] = rid
    return response


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):  # pragma: no cover
    log.exception("unhandled error")
    return JSONResponse(status_code=500, content={"detail": "Internal error"})


app.include_router(router)


@app.get("/", tags=["meta"])
def root() -> dict:
    return {
        "name": settings.app_name,
        "docs": "/docs",
        "endpoints": ["/health", "/ask", "/validate-sql", "/semantic-layer", "/examples"],
    }
