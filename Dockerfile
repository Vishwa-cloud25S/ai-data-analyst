FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DUCKDB_PATH=/srv/data/warehouse.duckdb

WORKDIR /srv

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-api.txt .
RUN pip install --no-cache-dir -r requirements-api.txt

COPY app ./app
COPY dbt ./dbt

# Bake the warehouse into the image: the container binds its port immediately
# instead of seeding for ~15s on every cold start (free tiers sleep and wake often).
RUN python -m app.db.seed

RUN useradd -m -u 10001 analyst && mkdir -p /srv/data && chown -R analyst /srv
USER analyst

# PORT is injected by most PaaS hosts (Render, Railway, Cloud Run); default for local.
ENV PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT}/health" || exit 1

# Re-seed only if the baked warehouse is missing (e.g. a volume is mounted over it).
CMD ["sh", "-c", "[ -f \"${DUCKDB_PATH:-/srv/data/warehouse.duckdb}\" ] || python -m app.db.seed; exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
