# Security

## Threat model

The central assumption: **the language model is untrusted**. It is a planner whose
output is treated as hostile input until validated. It never holds credentials,
never opens a connection, and never sees a row of data it has not been handed
after validation.

| Threat | Control |
|---|---|
| Model emits destructive SQL (`DROP`, `DELETE`, `UPDATE`) | AST validation rejects any non-`SELECT` node anywhere in the tree, before execution. The connection is read-only regardless. |
| Model queries tables it should not see | Table allow-list derived from the semantic layer. Undeclared tables are unreachable even if the model names them correctly. |
| Model queries sensitive columns | Column allow-list, same mechanism. |
| Prompt injection in the question | Intent detection refuses known patterns; more importantly, injection cannot widen the allow-list, so the worst case is a refusal. |
| Data exfiltration via SQL functions | `read_csv_auto`, `read_parquet`, `attach`, `pg_read_file`, `system`, network functions are all blocked by name. |
| Stacked statements / comment tricks | Validation runs on the parsed AST, not on text. `; DROP TABLE` is a parse of two statements and is rejected. |
| System catalogue enumeration | `pg_*`, `information_schema`, `duckdb_*`, `sqlite_*` rejected. |
| Cartesian blow-up / resource exhaustion | Join cap, mandatory `LIMIT`, row cap, statement timeout. |
| Answering a question it should not | Scope gate refuses questions whose vocabulary does not match the semantic layer, rather than defaulting to a metric. |
| Silently wrong results | Result validation: null share, negative values, fan-out magnitude, duplicate group keys; low confidence surfaces to the caller. |
| Unauthorised API access | API keys, three roles, constant-time comparison, SHA-256 digests at rest. |
| Repudiation ("who ran that?") | Append-only audit log of every question, answered or refused, with identity and blocking stage. |
| Prompt content leaving the estate | `LLM_BASE_URL` routes to a self-hosted model and overrides `OPENAI_API_KEY`; with neither set, no model is called at all. |

## What this project does *not* protect against

Stated plainly, because a security document that claims completeness is not
credible:

- **A misconfigured semantic layer.** If you publish a table containing personal
  data, the system will happily query it. `init` flags likely personal data but
  deleting it is a human decision.
- **A compromised database credential.** Use a read-only role. The application's
  read-only enforcement is defence in depth, not a substitute.
- **Inference attacks.** Someone with legitimate access can aggregate their way
  toward individual records if the semantic layer exposes fine enough grain.
  There is no k-anonymity or differential privacy here.
- **A malicious operator.** Anyone who can change `semantic_layer.yml` or the
  environment can change what is reachable — including through the admin editor
  endpoints. Every such change is validated, backed up and audited with a diff
  that flags newly exposed tables and columns, so the action is *detectable and
  reversible*, but an admin is by definition trusted. Grant the admin role
  sparingly and review `/audit` for `EDIT semantic layer` entries.
- **Denial of service.** Row caps and timeouts limit single queries; there is no
  rate limiting. Put it behind a gateway that has some.
- **Secrets in questions.** Questions are stored verbatim in the audit log. That
  is the point, but it means the audit database inherits the sensitivity of what
  people type.

## Deployment requirements

**Database.** Create a dedicated read-only role granted `SELECT` on exactly the
tables in your semantic layer, and nothing else. `scripts/postgres_init.sql`
shows the pattern. This is the single most important control.

**Authentication.** Set `AUTH_ENABLED=true` and real keys. The service refuses to
start with auth enabled and no keys, but it will happily run open if you never
enable it. Generate keys with `ai-analyst keygen`.

**Transport.** Terminate TLS in front of the service. Keys travel in the
`X-API-Key` header and must not cross plaintext links.

**Audit storage.** Put `AUDIT_DB_PATH` on persistent, backed-up storage.
Containers with ephemeral disks lose the evidence trail on restart.

**Model.** Decide deliberately. `/health` reports the active provider; confirm it
says what you expect before going live.

## Key handling

Keys are held only as SHA-256 digests in memory and compared with
`hmac.compare_digest`. No endpoint returns key material — `/principals` returns
names and roles only. Plaintext is unrecoverable after generation, by design, so
rotation means issuing a new key and removing the old entry.

## Reporting a vulnerability

Open a private security advisory on the GitHub repository, or raise an issue
without exploit details and ask for a contact channel. Please do not post working
exploits publicly before a fix exists.

## Verifying the claims

None of the above needs to be taken on trust:

```bash
pytest tests/test_validator.py -v        # 19 hostile queries
pytest tests/test_auth.py -v             # role boundaries, fail-closed startup
pytest tests/test_audit.py -v            # every outcome recorded
pytest tests/test_local_llm.py -v        # rogue local model is still refused
pytest tests/test_scope.py -v            # out-of-scope refused, not guessed
```

CI runs a dedicated guardrails job on every push, plus a container smoke test
asserting a live deployment refuses `drop table fct_orders`.
