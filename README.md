# AI Data Analyst

**Ask a business question in English. Get a chart, a number, and the SQL that produced it — without ever giving the LLM database access.**

```
"What were our highest revenue products last quarter?"
```
```
Total Revenue for Q2 2026 was led by Vertex 16 Pro Laptop at $665,937, followed by
Vertex 14 Laptop ($557,135) and Aurora Studio Headphones ($104,601). The leader accounts
for 40.6% of the $1,642,224 across the 10 rows returned.
```

---

## The point of this project

Most "chat with your database" demos hand an LLM a connection string and hope for the best.
That is a data breach and a wrong-number generator wearing a trench coat.

Here the LLM is a **planner, not an executor**. It never sees credentials, never sees
tables that aren't published, and its output is treated as untrusted input that has to
pass an AST-level validator before a **read-only** connection will look at it.

```
User
 ↓
FastAPI
 ↓
LLM  ─────────── never gets a DB connection
 ↓
Semantic layer   the only schema the model can see
 ↓
SQL generation
 ↓
DuckDB / PostgreSQL
 ↓
Validation
 ↓
Result
 ↓
Chart + explanation
```

## The seven-stage pipeline

| # | Stage | What it does | What it stops |
|---|-------|--------------|---------------|
| 1 | **Intent detection** | Classifies the question (`ranking`, `trend`, `comparison`, `metric_query`, `metadata`, `unsupported`) and extracts metrics, dimensions, grain, time range and `top N`. | Write requests, HR/salary fishing, prompt injection — refused before any SQL exists. |
| 2 | **Schema retrieval (RAG) + scope gate** | TF-IDF retrieval (stopword-filtered, de-pluralised) over the semantic layer **and dbt descriptions**, returning only the relevant slice of schema. A question sharing no vocabulary with the layer is refused here. | Prompt bloat; the model seeing tables outside the contract; and *confidently wrong answers to unrelated questions* — see below. |
| 3 | **SQL generation** | LLM writes SQL from the retrieved slice, seeded with a deterministic planner's SQL as a reference. Certified metric expressions are supplied verbatim. | Invented metric maths ("revenue = SUM(unit_price)"), missing `WHERE status <> 'cancelled'`. |
| 4 | **SQL validation** | `sqlglot` AST checks: single statement, SELECT-only, table allow-list, column allow-list, banned functions, no `SELECT *`, no cross joins, join cap, mandatory `LIMIT`. | DDL/DML, stacked statements, `read_csv_auto('/etc/passwd')`, `information_schema`, hallucinated columns. |
| 5 | **Read-only execution** | DuckDB opened with `read_only=True`; Postgres runs as a least-privilege role with `default_transaction_read_only` and a statement timeout. | Anything that somehow survived stage 4. |
| 6 | **Result validation** | Shape, nullness, negative-value, fan-out magnitude and duplicate-group-key checks; produces a confidence score. | Legal SQL that returns nonsense — the failure mode SQL linting can't catch. |
| 7 | **Explanation** | LLM narrates **only** the validated result set (never the database) and a chart spec is derived. | Hallucinated figures — the model is given the numbers and told to use nothing else. |

Every stage is recorded in a `trace` returned with each answer, so the UI shows exactly
what happened, in what order, and how long it took.

### Refusing beats guessing

An early version answered `"what is the weather in Hyderabad"` with **total revenue**.
Nothing leaked — the guardrails held — but with no metric keyword matched, intent
detection quietly fell back to the default metric and reported a real number for an
unrelated question. A governed system that invents relevance is not governed.

The scope gate now derives a vocabulary from the semantic layer itself (entity names,
columns, metric labels, synonyms; stopwords and generic terms like `total`/`average`
removed) and refuses anything that shares no term with it:

```
"what is the weather in Hyderabad"   -> refused, stage 2, no SQL, no execution
"how much do we pay our staff"       -> refused, stage 2
"which categories sell best"         -> answered  (matched: category)
"top 5 skus by sales"                -> answered  (matched: sku, product, sales)
```

`tests/test_scope.py` pins 8 out-of-scope and 10 in-scope questions so the boundary
cannot drift.

## Guardrails, concretely

```python
>>> POST /validate-sql {"sql": "SELECT salary_usd FROM employee_salaries"}
{"ok": false,
 "errors": ["Table(s) not in the semantic layer: employee_salaries.
             Allowed: fct_orders, dim_products, dim_customers"]}
```

`employee_salaries` physically exists in the warehouse. It is simply not in
`app/semantic/semantic_layer.yml`, so it does not exist as far as this system is concerned.
`tests/test_validator.py` fires 19 hostile queries at the validator on every push and CI
fails if a single one gets through.

## Quick start

```bash
git clone https://github.com/<you>/ai-data-analyst.git
cd ai-data-analyst
pip install -r requirements-dev.txt
cp .env.example .env          # optional: add OPENAI_API_KEY

make seed                     # build the local DuckDB warehouse (~15k order lines)
make api                      # FastAPI on :8000  -> http://localhost:8000/docs
make ui                       # Streamlit on :8501
```

Or with Docker:

```bash
docker compose up --build     # API on :8000, UI on :8501
docker compose --profile postgres up --build   # adds a least-privilege Postgres warehouse
```

### It runs with no API key

If `OPENAI_API_KEY` is empty, a deterministic **planner** compiles the detected intent into
SQL against the semantic layer, and a template narrator writes the answer. Same pipeline,
same guardrails, zero network calls — which is exactly how the test suite and CI run it.
Set the key and the LLM path takes over, with the planner still acting as the fallback
whenever the model's SQL fails validation.

## API

```bash
curl -X POST localhost:8000/ask -H 'content-type: application/json' -d '{
  "question": "What were our highest revenue products last quarter?"
}'
```

| Endpoint | Purpose |
|---|---|
| `POST /ask` | The full pipeline. Returns answer, SQL, rows, chart spec, confidence, trace. |
| `POST /validate-sql` | The guardrail on its own — auditable in isolation. |
| `GET /semantic-layer` | The published contract: entities, metrics, approved joins. |
| `GET /health` | Warehouse, active model, entity/metric counts. |
| `GET /docs` | OpenAPI UI. |

## The semantic layer

Metrics are defined once, in YAML, with their mandatory filters — the model cannot
redefine revenue:

```yaml
metrics:
  - name: total_revenue
    label: Total Revenue
    description: Net revenue in USD, excluding cancelled and returned orders.
    expression: SUM(fct_orders.net_revenue)
    filters:
      - fct_orders.order_status NOT IN ('cancelled', 'returned')
    format: currency
```

Column and model descriptions are pulled from **dbt** (`dbt/models/marts/schema.yml`)
and folded into the retrieval index, so the documentation your analytics engineers
already write becomes the context the model reasons over.

## Layout

```
app/
  api/          FastAPI routes + pydantic contracts
  core/         settings
  db/seed.py    deterministic synthetic warehouse
  llm/          OpenAI wrapper with offline fallback
  pipeline/     intent · retrieval(RAG) · generator · validator ·
                executor · result_validator · explainer · orchestrator
  semantic/     semantic_layer.yml + loader
dbt/            staging + marts models, schema.yml (metadata source for RAG)
ui/             Streamlit app (HTTP only, no DB, no keys)
tests/          127 tests: guardrails, scope gate, intent, time parsing,
                retrieval, execution, result checks, pipeline, API, UI
.github/        lint · test matrix · guardrail suite · docker smoke test
```

## Testing

```bash
make test        # 127 tests, no network required
make lint
```

CI runs four jobs on every push: **lint**, a **test matrix** (3.11 / 3.12),
a dedicated **guardrails** job that must pass for the build to be green, and a
**docker** job that builds both images and smoke-tests a real container — asserting
both that a legitimate question is `answered` and that `drop table fct_orders` is `refused`.

## Design notes

- **Time is resolved deterministically, not by the LLM.** "last quarter" becomes
  `BETWEEN DATE '2026-04-01' AND DATE '2026-06-30'` in Python, and the literal dates are
  handed to the model. Language models are bad at calendars.
- **The planner is a first-class citizen**, not a stub. It gives the LLM a strong reference
  query, gives CI a deterministic path, and gives production a fallback when generated
  SQL fails validation.
- **Refusals are informative.** A blocked question returns the reason, the stage that
  blocked it and the full trace, not a shrug.
- **Trust boundary is one process.** Streamlit talks HTTP to FastAPI; it holds no
  credentials and no database driver.

## Deploying

**Render** (both services, free tier) - `render.yaml` is a blueprint:
push the repo, then Render → New → Blueprint → select it. Both images honour
`$PORT`, and the API bakes its warehouse at build time so it binds immediately
instead of seeding on every cold start. The UI is wired to the
API over the private network automatically. Set `OPENAI_API_KEY` on the API service
if you want the LLM path; it runs without one.

**Fly.io** (API) - `fly.toml` is included:

```bash
fly launch --no-deploy --copy-config
fly secrets set OPENAI_API_KEY=sk-...   # optional
fly deploy
```

**Anywhere with Docker**:

```bash
docker compose up --build          # API :8000, UI :8501
```

The container seeds its own DuckDB warehouse on start, so a fresh deploy is
answering questions within seconds of boot with no external database required.

## Licence

MIT
