# Architecture

## Trust boundaries

```
┌──────────────┐        HTTP         ┌────────────────────────────────────┐
│  Streamlit   │ ──────────────────► │            FastAPI                 │
│  (no keys,   │                     │                                    │
│   no driver) │ ◄────────────────── │  ┌──────────────────────────────┐  │
└──────────────┘      JSON only      │  │        Orchestrator          │  │
                                     │  └───┬──────────────────────────┘  │
                                     │      │                             │
        ┌────────────────────────────┼──────┼─────────────────────────┐   │
        │  UNTRUSTED ZONE            │      │                         │   │
        │  ┌───────────┐   prompt    │      │   JSON (sql, rationale) │   │
        │  │  OpenAI   │ ◄───────────┼──────┤ ──────────────────────► │   │
        │  └───────────┘  semantic   │      │                         │   │
        │   no DSN, no driver, no    │      │                         │   │
        │   filesystem, no results   │      │                         │   │
        │   until they are validated │      │                         │   │
        └────────────────────────────┼──────┼─────────────────────────┘   │
                                     │      ▼                             │
                                     │  ┌──────────────────────────────┐  │
                                     │  │ Validator (sqlglot AST)      │  │
                                     │  │  allow-list · SELECT-only ·  │  │
                                     │  │  LIMIT · no banned funcs     │  │
                                     │  └───┬──────────────────────────┘  │
                                     │      ▼ only validated SQL          │
                                     │  ┌──────────────────────────────┐  │
                                     │  │ Read-only executor           │  │
                                     │  └───┬──────────────────────────┘  │
                                     └──────┼─────────────────────────────┘
                                            ▼
                                  DuckDB (read_only=True)
                                  Postgres (analyst_ro, READ ONLY txn,
                                            statement_timeout, SELECT
                                            granted on 3 tables only)
```

The LLM sits **outside** the trust boundary in both directions:
it receives a pruned schema slice (never data, never credentials) and it emits a
string that is treated as hostile until the validator says otherwise.

## Access control

```
X-API-Key ──► KeyRing (sha256 digests, constant-time compare)
                 │
                 ▼
            Principal(name, role)  viewer < analyst < admin
                 │
   ┌─────────────┼──────────────────────────┐
   ▼             ▼                          ▼
 /ask        /validate-sql        /audit · /audit/stats · /principals
 viewer         analyst                     admin
```

`AUTH_ENABLED=true` with no `API_KEYS` raises at startup: an API that was meant to be
authenticated must never come up open. `/health` stays public so platform health checks
work.

Every request - answered, refused or errored - is appended to a SQLite audit log with
principal, role, client IP, question, status, blocking stage, executed SQL, tables
touched, row count, confidence and duration. The log lives in its own database, because
the warehouse connection is read-only and audit writes must never be able to reach
business data.

## Request lifecycle

```
POST /ask {"question": "..."}
  │
  ├─ 1 intent_detection      → refuse here for writes / restricted data / injection
  ├─ 2 schema_retrieval      → RAG: top-k docs from semantic layer + dbt metadata
  │                            + scope gate: refuse if the question shares no
  │                              vocabulary with the layer
  ├─ 3 sql_generation        → planner SQL always built; LLM may improve on it
  ├─ 4 sql_validation        → AST checks; on failure retry with planner SQL; else refuse
  ├─ 5 execution             → read-only connection, row cap, timeout
  ├─ 6 result_validation     → sanity checks, confidence score; may refuse
  └─ 7 explanation           → LLM narrates validated rows only; chart spec derived
  │
  └─► {answer, sql, rows, chart, confidence, intent, warnings, issues, trace[7]}
```

## Failure and fallback matrix

| Condition | Behaviour |
|---|---|
| No `OPENAI_API_KEY` | Deterministic planner + template narrator. Full pipeline, no network. |
| OpenAI call errors / times out | Same offline fallback, logged as a warning. |
| LLM SQL fails validation | Retry with deterministic planner SQL; if that also fails → `refused`. |
| Query returns > `MAX_ROWS` | Truncated, flagged in warnings, confidence reduced. |
| Query returns implausible values | `refused` with the specific sanity check that fired. |
| Warehouse unreachable | `error` status with a remediation hint, never a stack trace to the client. |
| Question unrelated to the data | `refused` at stage 2 by the scope gate, rather than answered with the default metric. |
| UI cannot reach the API | The Streamlit client raises on connection refusal, 4xx/5xx, non-JSON and wrong-shaped `/health` payloads, and names the likely cause. |

## Validator checks (stage 4)

| Check | Implementation |
|---|---|
| Single statement | `len(sqlglot.parse(sql)) == 1` |
| SELECT only | root node type + no `Insert/Update/Delete/Drop/Create/Alter/Truncate/Merge/Grant` anywhere in the tree |
| Table allow-list | every `exp.Table` ∈ semantic layer entities ∪ CTE names |
| No system catalogs | reject `pg_*`, `information_schema`, `duckdb_*`, `sqlite_*` |
| Column allow-list | every `exp.Column` ∈ declared columns ∪ aliases ∪ CTE names |
| Banned functions | `read_csv_auto`, `read_parquet`, `attach`, `pg_read_file`, `system`, `sleep`, … |
| No `SELECT *` | no `exp.Star` in any projection |
| No cross joins | every `exp.Join` must carry `ON`/`USING` |
| Join cap | `MAX_JOINS` (default 4) |
| Row cap | `LIMIT` present and ≤ `MAX_ROWS`, otherwise injected/clamped |

Because the checks run on the parsed AST, comment injection, case tricks and
whitespace games have no effect.

## Extending it

- **New metric** → add it to `app/semantic/semantic_layer.yml`. The retriever indexes it,
  the planner can compile it, the validator allows its columns. No code change.
- **New table** → add the entity plus an approved join. Anything not added stays invisible.
- **Real vector search** → replace `SchemaRetriever.search` with pgvector/FAISS; the
  interface (`retrieve_context`, `render_schema_prompt`) is unchanged.
- **Another warehouse** → implement an executor with `.engine` and `.execute(sql)`;
  pass the matching sqlglot dialect through.
