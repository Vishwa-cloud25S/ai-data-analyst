# Changelog

All notable changes to this project. Format follows
[Keep a Changelog](https://keepachangelog.com/); versioning is
[semantic](https://semver.org/).

## [1.0.0] — 2026-08-27

First release considered deployable against a real customer warehouse.

### Added

**Governed pipeline** — intent detection → schema retrieval (RAG over the
semantic layer and dbt metadata) → SQL generation → AST validation →
read-only execution → result validation → natural-language explanation, with a
full per-stage trace on every response.

**Guardrails** — `sqlglot` AST validation: single statement, `SELECT`-only,
table and column allow-lists, banned functions, no `SELECT *`, no cross joins,
join cap, enforced `LIMIT`. 19 hostile queries tested on every commit.

**Scope gate** — questions sharing no vocabulary with the semantic layer are
refused rather than answered with a default metric.

**Access control** — API keys with `viewer`/`analyst`/`admin` roles, SHA-256
digests at rest, constant-time comparison, fail-closed startup.

**Audit log** — append-only record of every question (answered, refused or
errored) with identity, executed SQL, blocking stage, row count and duration;
`/audit` and `/audit/stats`, admin only.

**Local LLM support** — any OpenAI-compatible endpoint (Ollama, vLLM, LM Studio,
llama.cpp, TGI) via `LLM_BASE_URL`, which takes precedence over `OPENAI_API_KEY`
so a stray key cannot send prompts off-host. JSON mode is negotiated and cached;
markdown-fenced and prose-wrapped JSON is extracted defensively.

**Deterministic planner** — the entire pipeline runs with no model at all, which
is how CI runs it and how the LLM path fails over.

**Warehouse connectors** — DuckDB and PostgreSQL natively; Snowflake, BigQuery,
Databricks, MySQL, Redshift and Trino through SQLAlchemy, with per-dialect SQL
rendering and read-only session setup.

**Onboarding** — `ai-analyst init` drafts a semantic layer from a warehouse or a
dbt `manifest.json`: classifies dimensions and measures, infers joins from
foreign keys or naming, flags likely personal data, and proposes metrics marked
`REVIEW`. `ai-analyst check` executes every entity and metric against the live
warehouse.

**CLI** — `init`, `check`, `ask`, `keygen`, `serve`.

**Interfaces** — FastAPI service with OpenAPI docs; Streamlit UI with chart, SQL,
confidence, pipeline trace and an admin audit panel.

**Deployment** — Docker images honouring `$PORT` with the warehouse baked at
build time, compose (including an Ollama profile), Render blueprint, Fly config.

**CI** — lint, 3.11/3.12 test matrix, a dedicated guardrails job, semantic-layer
verification, CLI smoke test, and a container smoke test asserting a live
deployment refuses `drop table`.

**Documentation** — architecture, onboarding, security, pitch, demo script, and a
case study of the first run against an unfamiliar schema.

**Semantic-layer editor** — admin-only endpoints and a UI panel for editing
entities, columns and metrics. Changes are validated by loading the layer *and*
executing every entity and metric against the warehouse before anything is
written; saves are atomic, backed up, restorable, applied without a restart, and
audited with a diff flagging newly exposed data.

### Fixed during development

Bugs worth recording because each came from contact with reality rather than
planning:

- `/ask` with `use_llm=false` discarded the configured executor, breaking any
  deployment whose warehouse was not at the default path.
- Bar charts crashed: `markers` was passed to `px.bar`, which does not accept it.
- The UI reported "API healthy" against any JSON response, including a `404` from
  an unrelated service on the same port.
- Out-of-scope questions were answered with total revenue instead of refused.
- Containers bound a hardcoded port, so the first cloud deploy never served.
- Metric selection was a hardcoded word list, so a metric defined at runtime was
  unreachable by name and the question silently fell back to revenue - the same
  failure as the dimension list, one field over. Found by using the new editor.
- Six schema-specific assumptions found by running against Chinook: keys
  classified as measures on CamelCase columns, lowercased primary keys, colliding
  metric names, a hardcoded `order_date`, single-hop-only joins, and a hardcoded
  dimension vocabulary that silently dropped groupings.

[1.0.0]: https://github.com/Vishwa-cloud25S/ai-data-analyst/releases/tag/v1.0.0
