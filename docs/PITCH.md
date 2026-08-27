# AI Data Analyst — one page

## The problem

Executives ask the data team the same twelve questions every week. The data team
answers them by hand. Meanwhile every "chat with your database" tool on the market
solves this by handing a language model a connection string and hoping — which is
simultaneously a data-exfiltration risk and a wrong-number generator.

Two failure modes make those tools unusable in any regulated environment:

1. **The model can reach anything the credential can reach.** Publishing a
   read-only connection to an LLM makes every table in the warehouse one prompt
   away.
2. **A wrong answer is indistinguishable from a right one.** The output is fluent
   either way. Nobody can tell which number to trust, so nobody trusts any of them.

## What this is

Natural-language analytics where **the model plans and a semantic layer decides**.

```
Question → Intent → Schema retrieval (RAG) → SQL generation
         → AST validation → Read-only execution → Result validation → Answer
```

The model never receives credentials, never opens a connection, and only ever sees
the slice of schema you publish. Its SQL is parsed and checked against an
allow-list before anything runs.

## Why it is different

**A table you do not publish does not exist.** The demo warehouse contains an
`employee_salaries` table. Ask for it and you get a refusal at stage one — not
because "salaries" is a blocked word, but because the table is not in the semantic
layer, so the validator rejects any query naming it. 19 hostile queries test this
on every commit.

**It refuses rather than guesses.** Ask something outside the data and it says so,
instead of returning a plausible number. That single behaviour is what makes the
output trustworthy enough to act on.

**Every number is auditable.** Each answer ships with the SQL that produced it and
a seven-stage trace. `/audit` records every question — answered or refused — with
who asked and which stage blocked it.

**Nothing has to leave your estate.** Runs against a self-hosted model (Ollama,
vLLM, Databricks) or with no model at all, using a deterministic planner. `/health`
tells an operator which is live.

**Metrics are defined once.** "Revenue" means the same thing here as in the
dashboard, because both read the same certified definition — including its
mandatory filters.

## Proof

| Claim | Evidence |
|---|---|
| Guardrails hold | 198 tests; a dedicated CI job; container smoke test asserting a live deploy refuses `drop table` |
| Works on unfamiliar schemas | [Chinook case study](CASE_STUDY_CHINOOK.md) — pointed at an 11-table schema it had never seen, found and fixed six bugs, now answers three-hop questions correctly |
| Fast onboarding | `ai-analyst init` drafts a semantic layer from your warehouse or dbt manifest in seconds |
| Real deployment | Live on Render; `docker compose up` locally |

## Who it is for

- **Mid-market companies with a BI team** drowning in ad-hoc requests
- **Regulated sectors** — public sector, health, financial services — that cannot
  send data to a hosted model
- **Data teams with dbt** who already have the semantic definitions and want a
  natural-language front door

## Deployment shapes

| | |
|---|---|
| **Self-hosted** | `docker compose up`. Your infrastructure, your model, your data. |
| **Managed** | Hosted for you, pointed at a read-only role in your warehouse. |
| **Embedded** | The API behind your own product's UI. |

## Honest limitations

- The semantic layer needs an hour of a data person's time per warehouse. That is
  the work, and no tool removes it.
- Complex analytical questions — cohort retention, attribution, window functions —
  are outside what the planner writes today.
- It answers questions about data you have modelled. It is not a data catalogue,
  and it will not fix a warehouse that nobody understands.
- Accuracy depends on metric definitions being right. Garbage definitions produce
  confidently wrong answers; the semantic layer makes them *reviewable*, not
  automatically correct.
