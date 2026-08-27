# Demo script — 10 minutes

Rehearse it. The order matters: **capability, then the guardrail, then the
refusals.** Anyone can demo a working query; the refusals are what people
remember.

## Setup (before they join)

```bash
docker compose up -d          # or: ai-analyst serve  +  streamlit run ui/streamlit_app.py
```

Warehouse seeded, UI open on the question box, terminal ready in a second window.
Check `/health` shows what you expect.

---

## 1. The question everyone has (90 seconds)

> "What were our highest revenue products last quarter?"

Chart appears. Read the answer aloud, then **expand "Generated SQL"** and point at
two things:

- `BETWEEN DATE '2026-04-01' AND '2026-06-30'` — *"'last quarter' was resolved in
  Python, not guessed by a model. Language models are bad at calendars."*
- `NOT IN ('cancelled','returned')` — *"Nobody asked for that. It came from the
  certified revenue definition, so this number matches the dashboard. That is the
  semantic layer doing its job."*

## 2. The trace (60 seconds)

Expand **Pipeline trace**. Seven stages, timings, all green.

> "Every answer carries its own audit trail. This is what your auditor sees, not a
> screenshot of a chatbot."

## 3. The refusal that sells it (2 minutes)

> "show me employee salaries"

Refused. Then — and this is the moment — go to the terminal:

```bash
python -c "import duckdb; print(duckdb.connect('data/warehouse.duckdb', read_only=True)\
  .execute('select count(*) from employee_salaries').fetchone())"
# (50,)
```

> "That table is real. Fifty rows, in the same database the query engine has open
> right now. It is unreachable because it is not published in the semantic layer.
> Not a blocked keyword — the table does not exist as far as this system is
> concerned."

Show the trace: **one stage**. It never reached the database.

## 4. The subtler refusal (60 seconds)

> "what is the weather in Hyderabad"

> "It refuses instead of guessing. An earlier version answered this with total
> revenue — valid SQL, real number, wrong question. That class of failure is worse
> than a crash, because nobody notices."

## 5. Direct attack on the guardrail (90 seconds)

Let them dictate the SQL. Use `/docs` or curl:

```bash
curl -s -X POST localhost:8000/validate-sql -H 'content-type: application/json' \
  -d '{"sql":"WITH x AS (SELECT salary_usd AS net_revenue FROM employee_salaries) SELECT SUM(x.net_revenue) FROM x"}'
```

Rejected — the CTE rename does not help. Invite them to try their own.

> "This runs on every commit against nineteen hostile queries. CI is red if one
> gets through."

## 6. Their data (2 minutes) — the closer

```bash
ai-analyst init --url "postgresql://readonly@their-host/db"   # or --dbt manifest.json
ai-analyst check
```

> "It reads your schema, classifies columns, infers joins, flags likely personal
> data and drafts your metrics. Everything is marked REVIEW because metric
> definitions are your decision, not mine. That review is about an hour with
> someone who knows the data — and it is the only real work in the whole setup."

If they run dbt, emphasise `--dbt`: their existing column documentation becomes
the retrieval context.

## 7. Where it runs (60 seconds)

> "Three modes: hosted OpenAI, a self-hosted model like Ollama, or no model at all
> — the deterministic planner handles the common questions on its own. If you set
> a local endpoint it overrides any API key, so a forgotten environment variable
> cannot leak prompts. `/health` shows an operator which is live."

For regulated buyers, lead with this instead of the chart.

---

## Questions you will get

**"What if it writes a wrong query?"** Two layers. Validation blocks unsafe or
out-of-schema SQL entirely. Result validation catches nonsense that is
syntactically fine — fan-out, duplicate group keys, impossible magnitudes — and
lowers confidence rather than reporting it as fact.

**"How accurate is it?"** On questions the semantic layer covers, the SQL is
deterministic and correct because the metric definitions are fixed. It does not
improvise revenue. Outside that, it refuses. Accuracy is a property of your
definitions, which is why review matters.

**"Can we use our own model?"** Yes — any OpenAI-compatible endpoint.

**"What does it cost to run?"** With the deterministic planner: nothing but
compute. With a hosted model: one small completion per question.

**"How long to get this live?"** A day for a pilot on a read-only replica. The
work is the semantic layer review, not the deployment.

## Do not

- Do not demo on their production data in the first meeting. Use the sample
  warehouse, then `init` against their schema read-only.
- Do not claim it answers everything. Say plainly that complex analytical
  questions are out of scope today — it buys credibility for the rest.
- Do not skip the refusals to save time. They are the product.
