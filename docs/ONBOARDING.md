# Onboarding a new customer

**Goal: a correct answer from their data in under an hour, without reading source code.**

```bash
pip install -e .

# 1. Draft a semantic layer from their warehouse
ai-analyst init --duckdb /path/to/their.duckdb --exclude hr_salaries,pii_customers
#   or  --postgres "postgresql://readonly:pw@host:5432/db" --schema analytics
#   or  --dbt      /path/to/dbt/target/manifest.json

# 2. Review the draft  <- the only step that needs a human
$EDITOR semantic_layer.yml

# 3. Verify every entity and metric actually executes
SEMANTIC_LAYER_PATH=semantic_layer.yml ai-analyst check

# 4. Ask something
ai-analyst ask "what were our highest revenue products last quarter?" --trace

# 5. Serve it
ai-analyst serve --port 8000
```

## Step 2 is not optional

`init` reads the whole schema, including tables you must never expose. That is
deliberate: a tool that silently decides what is sensitive will get it wrong.
The draft is annotated, and three things need a person:

**Delete what must not be seen.** Anything left in the file is queryable;
anything removed is invisible to the model *and* rejected by the validator.
This is the primary control for PII.

**Fix the metrics.** Generated metrics are mechanical sums, and some are
nonsense — `SUM(unit_price)` is meaningless, and every revenue metric probably
needs `WHERE status NOT IN ('cancelled','returned')`. Definitions are business
decisions; that is the whole point of a semantic layer.

**Check the joins.** Inferred from foreign keys, or from column naming when none
are declared. A join at the wrong grain silently inflates every number, and no
amount of SQL validation catches it — only someone who knows the data.

## Getting the descriptions right

Descriptions are what retrieval matches questions against, so they decide
whether the right tables are found. Two minutes per column is worth more than
any model upgrade.

Bad: `REVIEW: net revenue.`
Good: `Net revenue in USD after discount. Never sum unit_price directly.`

If the customer runs **dbt**, use `--dbt target/manifest.json`: their analytics
engineers have already written these descriptions, and they get pulled in
automatically.

## Production checklist

| | Why |
|---|---|
| `AUTH_ENABLED=true` with real keys | Refuses to start without them, but check anyway |
| Warehouse credentials are **read-only at the database** | Defence in depth; do not rely on the app alone |
| `LLM_BASE_URL` set, or no LLM at all | Confirm on `/health` that no prompt leaves the estate |
| `AUDIT_DB_PATH` on persistent storage | Ephemeral containers lose the audit trail on restart |
| `MAX_ROWS` sized for their data | Default 1000 |
| Backup/rotate the audit database | It is an evidence record |
| `ai-analyst check` in their CI | Catches schema drift breaking the semantic layer |

## Common questions in evaluations

**"Can it see our other tables?"** Only what is in the semantic layer. The
validator rejects anything else at the AST level, before execution, and the
read-only connection would reject writes even if it did not.

**"What if the model hallucinates a query?"** It is rejected and the
deterministic planner's SQL is used instead. Both paths face the same validator.
There is a test that drives a rogue model returning
`SELECT salary_usd FROM employee_salaries`.

**"Does our data go to OpenAI?"** Not if you set `LLM_BASE_URL` to a local
model, and not at all if you configure no model. `LLM_BASE_URL` overrides
`OPENAI_API_KEY` so a stray key cannot leak prompts. `/health` reports which
provider is live.

**"Who asked what?"** `/audit` — every question, answered or refused, with the
identity, the SQL, and which stage blocked it.

**"What happens when it does not know?"** It refuses. Out-of-scope questions are
blocked at stage 2 rather than answered with a plausible-looking default.
