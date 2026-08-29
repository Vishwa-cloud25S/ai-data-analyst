from __future__ import annotations

import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.core.config import settings
from app.core.ratelimit import RateLimiter
from app.core.security import verify_startup_config

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
    version="1.1.2",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


#: Paths worth throttling: they cost real work. /health must stay free so
#: platform health checks are never rate limited into failure.
THROTTLED_PREFIXES = ("/ask", "/validate-sql", "/semantic-layer", "/datasets/upload")

limiter = RateLimiter(settings.rate_limit_per_minute)


def _client_key(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    if limiter.enabled and request.url.path.startswith(THROTTLED_PREFIXES):
        allowed, remaining, retry_after = limiter.check(_client_key(request))
        if not allowed:
            log.warning("rate limited %s on %s", _client_key(request), request.url.path)
            return JSONResponse(
                status_code=429,
                content={"detail": f"Rate limit exceeded: "
                                   f"{settings.rate_limit_per_minute} requests per "
                                   f"minute. Retry in {retry_after}s."},
                headers={"Retry-After": str(retry_after)},
            )
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(settings.rate_limit_per_minute)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
    return await call_next(request)


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


@app.on_event("startup")
def _startup() -> None:
    # Refuse to serve an unauthenticated API when auth was meant to be on.
    verify_startup_config()
    log.info(
        "auth_enabled=%s anonymous_role=%s audit_enabled=%s warehouse=%s "
        "rate_limit=%s/min",
        settings.auth_enabled, settings.anonymous_role, settings.audit_enabled,
        settings.warehouse, settings.rate_limit_per_minute,
    )
    if not settings.auth_enabled:
        log.warning(
            "AUTH_ENABLED is false: callers are anonymous with the '%s' role. "
            "Set AUTH_ENABLED=true and API_KEYS before exposing this publicly.",
            settings.anonymous_role,
        )


app.include_router(router)


@app.get("/", tags=["meta"])
def root() -> dict:
    return {
        "name": settings.app_name,
        "docs": "/docs",
        "endpoints": ["/health", "/ask", "/validate-sql", "/semantic-layer", "/examples"],
    }
