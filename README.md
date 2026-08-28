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

## Access control and audit

Auth is off by default so `make api` just works. Turn it on with two environment
variables and the API refuses to start if you enable auth without configuring keys:

```bash
AUTH_ENABLED=true
API_KEYS="k-director-...:admin:director,k-officer-...:viewer:field_officer"
```

| Role | Can |
|---|---|
| `viewer` | ask questions |
| `analyst` | + call `/validate-sql` to inspect the guardrail |
| `admin` | + read `/audit`, `/audit/stats`, `/principals` |

Keys are compared in constant time and held only as SHA-256 digests; no endpoint
ever returns key material.

**With auth off, callers are anonymous with the `analyst` role — not admin.** An
open deployment therefore still answers questions and exposes the guardrail for
inspection, while `/audit`, `/principals` and the semantic-layer editor stay
closed. Editing the layer additionally requires an authenticated key whatever
`ANONYMOUS_ROLE` says, because that endpoint decides what the assistant can reach.

`/ask`, `/validate-sql` and `/semantic-layer` are rate limited per IP
(`RATE_LIMIT_PER_MINUTE`, default 60); `/health` is exempt.

**Every question is recorded — answered, refused or errored** — in a SQLite log kept
separate from the (read-only) warehouse:

```
14:55:29  field_officer  refused   blocked=intent_detection   rows=0   show me employee salaries
14:55:29  field_officer  answered  blocked=None               rows=10  What were our highest revenue products...
```

```json
{"total_questions": 2, "by_status": {"answered": 1, "refused": 1},
 "refusals_by_stage": {"intent_detection": 1}, "refusal_rate": 0.5}
```

The refusals are the point. A log of successes proves nothing; a log showing *what was
asked, what was denied and which stage denied it* is what an auditor, a data-protection
officer or a public-sector buyer actually needs. Audit writes can never fail a user
request — the backend is best-effort by design.

## Editing the semantic layer without touching YAML

Metric definitions are business decisions, so the people who own them should not
need an engineer. Admins get an editor in the UI (and `/semantic-layer/raw`,
`/semantic-layer/validate`, `/semantic-layer/versions` on the API):

- **Metrics tab** — a form: label, description, expression, mandatory filters, format
- **Advanced tab** — the raw file, for those who prefer it
- **History tab** — every save is backed up and restorable

Nothing is saved until it **loads and executes against the warehouse**, so a typo in
an expression is rejected rather than breaking every future question. Saves take
effect immediately — no restart — and each one is written to the audit log with a
diff, flagging any change that *exposes new tables or columns*.

This is the highest-privilege surface in the system: whoever edits the layer decides
what the assistant can reach. It is admin-only for that reason, and
[SECURITY.md](SECURITY.md) says so plainly.

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

## Point it at your own data

```bash
pip install -e .
ai-analyst init --duckdb ./your.duckdb --exclude hr_salaries   # draft a semantic layer
$EDITOR semantic_layer.yml                                     # review: the one human step
ai-analyst check                                               # verify it against the warehouse
ai-analyst ask "what were our highest revenue products last quarter?" --trace
ai-analyst serve
```

`init` introspects DuckDB, PostgreSQL or a **dbt `manifest.json`**, classifies columns
into dimensions and measures, infers joins from foreign keys (or naming when none are
declared), and proposes metrics — every one flagged `REVIEW`, because
`SUM(unit_price)` is meaningless and revenue almost certainly needs a
`status NOT IN ('cancelled','returned')` filter. Metric definitions are business
decisions; the generator drafts, a human signs off.

`check` executes every declared entity and metric against the live warehouse, so a
broken definition or schema drift fails loudly instead of at demo time. Put it in CI.

See [docs/ONBOARDING.md](docs/ONBOARDING.md) for the full path from a customer's
warehouse to a served answer, and
[docs/CASE_STUDY_CHINOOK.md](docs/CASE_STUDY_CHINOOK.md) for what happened the
first time this was pointed at a schema it had never seen — six bugs in ten
minutes, including a passing test that only passed because its fixtures shared
my assumptions.

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

### Three LLM modes, including fully self-hosted

| Set this | What happens | Data leaves the host? |
|---|---|---|
| *nothing* | Deterministic planner writes the SQL | No |
| `OPENAI_API_KEY` | Hosted OpenAI writes the SQL | Yes |
| `LLM_BASE_URL` | Any OpenAI-compatible **local** server does | **No** |

```bash
# fully self-hosted, nothing leaves the machine
docker compose --profile ollama up --build
docker compose exec ollama ollama pull llama3.1:8b
LLM_BASE_URL=http://ollama:11434/v1 LLM_MODEL=llama3.1:8b docker compose up api
```

`LLM_BASE_URL` **takes precedence over `OPENAI_API_KEY`**: once a local endpoint is
configured, a forgotten key in the environment cannot cause prompts to be sent
off-host. That ordering is deliberate — for buyers holding personal data, "we might
have called an external API" is a breach, not a bug.

Local models are less obedient than hosted ones, so the client negotiates JSON mode
once, remembers if the server rejects it, and extracts JSON from markdown fences or
surrounding prose. If a local model returns nonsense, the deterministic planner takes
over — and its SQL faces the same validator either way. A rogue local model asking for
`employee_salaries` is refused exactly like a rogue hosted one; there is a test for it.

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
| `GET /health` | Warehouse, active model, entity/metric counts, auth/audit flags. Public. |
| `GET /whoami` | The caller's identity and role. |
| `GET /audit` | Every question asked, with SQL, outcome and blocking stage. Admin. |
| `GET /audit/stats` | Volumes, refusal rate, refusals by stage, top users. Admin. |
| `GET/PUT /semantic-layer/raw` | Read and edit the layer. Validated, backed up, audited. Admin. |
| `POST /semantic-layer/validate` | Dry run against the warehouse; saves nothing. Admin. |
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
  cli.py        init · check · ask · keygen · serve
  api/          FastAPI routes + pydantic contracts
  core/         settings · API-key auth & roles · audit log
  db/seed.py    deterministic synthetic warehouse
  llm/          OpenAI wrapper with offline fallback
  pipeline/     intent · retrieval(RAG) · generator · validator ·
                executor · result_validator · explainer · orchestrator
  semantic/     semantic_layer.yml · loader · bootstrap (introspection + dbt import)
dbt/            staging + marts models, schema.yml (metadata source for RAG)
ui/             Streamlit app (HTTP only, no DB, no keys)
tests/          253 tests: guardrails, scope gate, auth & roles, audit log,
                intent, time parsing, retrieval, execution, result checks,
                pipeline, API, UI
.github/        lint · test matrix · guardrail suite · docker smoke test
```

## Testing

```bash
make test        # 253 tests, no network required
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

## Warehouses

DuckDB and PostgreSQL have native executors. Everything else goes through
SQLAlchemy with one `WAREHOUSE_URL`:

```bash
WAREHOUSE_URL="snowflake://user:pw@account/db/schema?warehouse=WH&role=ANALYST_RO"
WAREHOUSE_URL="bigquery://project/dataset"
WAREHOUSE_URL="databricks://token:<pat>@host?http_path=/sql/1.0/warehouses/<id>"
WAREHOUSE_URL="mysql+pymysql://readonly:pw@host/db"
```

The SQL dialect follows the connection, so sqlglot renders and validates against
the right grammar. Where the engine supports it the session is also set read-only
with a statement timeout — but **the credential should be a read-only role at the
database**. Everything else is defence in depth. See [SECURITY.md](SECURITY.md).

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

## For evaluators and buyers

| | |
|---|---|
| [docs/PITCH.md](docs/PITCH.md) | One page: the problem, why this differs, honest limitations |
| [SECURITY.md](SECURITY.md) | Threat model, controls, and what this does *not* protect against |
| [docs/ONBOARDING.md](docs/ONBOARDING.md) | Warehouse to served answer, with a production checklist |
| [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) | Ten-minute walkthrough |
| [docs/CASE_STUDY_CHINOOK.md](docs/CASE_STUDY_CHINOOK.md) | What broke on an unfamiliar schema, and why |
| [CHANGELOG.md](CHANGELOG.md) | Release history |

## Licence

MIT
