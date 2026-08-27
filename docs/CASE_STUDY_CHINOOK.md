# Case study: pointing this at a schema it had never seen

Everything up to this point was validated against a warehouse I generated myself,
which is a form of cheating: the semantic layer, the heuristics and the tests all
shared my assumptions. So I ran `ai-analyst init` against
[**Chinook**](https://github.com/lerocha/chinook-database) — a widely used sample
database with 11 tables, CamelCase naming, no `fct_`/`dim_` conventions, and a
genuine multi-hop star schema.

It failed comprehensively. Six distinct bugs, all of them invisible on my own data.

## What broke

### 1. Zero facts, zero joins, zero metrics

```
Read 11 tables from DuckDB /tmp/cust/chinook.duckdb.
  fact tables:      (none detected)
  dimension tables: Album, Artist, Customer, Employee, Genre, Invoice,
                    InvoiceLine, MediaType, Playlist, PlaylistTrack, Track
entities: 11 | joins: 0 | metrics: 0
```

Fact detection required either a `fct_` prefix or declared foreign keys. Real
schemas frequently have neither. The generated layer was unusable.

### 2. `SUM(TrackId)` — the exact disaster the heuristics existed to prevent

```
InvoiceLine  measures = ['InvoiceId', 'TrackId', 'UnitPrice', 'Quantity']
Track        measures = ['AlbumId', 'MediaTypeId', 'GenreId', ...]
```

The "is this a key?" test matched `_id$`. `TrackId` does not end in `_id`.

**I had a passing test asserting this could not happen.** It used snake_case
fixtures, so it tested my assumption rather than the world. That is the most
useful thing this exercise produced.

### 3. Primary keys were lowercased

`pk='customerid'` against a column actually named `CustomerId`. On any
case-sensitive engine every generated query would fail.

### 4. Duplicate metric names silently collided

`InvoiceLine.UnitPrice` and `Track.UnitPrice` both produced `total_UnitPrice`.
The layer loads into a dict keyed by name, so one definition would have silently
overwritten the other.

### 5. The planner hardcoded `order_date`

```sql
WHERE Invoice.order_date BETWEEN DATE '2012-01-01' AND DATE '2012-12-31'
```

Chinook's date column is `InvoiceDate`. The validator caught it and refused —
the guardrail worked — but every time-based question was unanswerable.

### 6. The worst one: a confidently wrong answer

> **"top 5 genres by revenue"** → *"Revenue was $2,329."*

```sql
SELECT SUM(InvoiceLine.UnitPrice * InvoiceLine.Quantity) AS revenue
FROM main.InvoiceLine LIMIT 5
```

No grouping at all. Dimension words came from a hardcoded dictionary
(`product`, `category`, `region`, `channel`…) built around my demo schema.
`genre` was not in it, so the grouping was silently dropped and a total returned
as though it were the answer.

No guardrail catches this. The SQL is valid, touches only permitted tables,
passes every one of the thirteen validator checks, and returns a real number.
It simply answers a different question than the one asked. This is precisely the
failure mode the scope gate was built for, reappearing one level down.

Related: `Customer.country` was emitted without a join, because join resolution
only handled a single hop. `InvoiceLine` reaches `Customer` only via `Invoice`.

## Root cause

Two modules had quietly become schema-specific. `intent.py` held a dictionary of
dimension words and `generator.py` assumed a column named `order_date`. The
semantic layer was supposed to be the single contract, and for tables and columns
it was — but *vocabulary* and *time* had leaked into code.

## What changed

| Fix | Effect |
|---|---|
| `snake()` normalisation everywhere | `TrackId` → `track_id`, so key/measure/PII heuristics work on any casing |
| Original case preserved | Generated SQL runs on case-sensitive engines |
| Fact detection from shape | Foreign-key-like columns + measures, no naming convention needed |
| Metric names namespaced by entity | `invoice_line_unit_price` vs `track_unit_price` — no silent collisions |
| `time_dimension()` on the layer | Declared, else first date-typed column. No `order_date` anywhere |
| `join_path()` — breadth-first | Multi-hop: `InvoiceLine → Track → Genre` |
| `resolve_grouping()` on the layer | Words resolve against the customer's schema, preferring labels over keys |
| Unresolvable dimensions recorded | The planner reports them instead of dropping them |
| PII flagging | 17 columns marked `LIKELY PERSONAL DATA` for human review |

`resolve_grouping` also prefers a human-readable label over a surrogate key:
`product` becomes `dim_products.product_name`, not `fct_orders.product_id`.
Grouping by a surrogate key is technically correct and humanly useless.

## After

```
fact tables: Invoice, InvoiceLine, Track   |   joins: 9   |   metrics: 9
17 columns flagged as likely personal data
```

All four failing questions now work, on a schema the code has never been tuned for:

**"total revenue by country"** — two hops:
```sql
SELECT Customer.Country, SUM(InvoiceLine.UnitPrice * InvoiceLine.Quantity) AS revenue
FROM main.InvoiceLine AS InvoiceLine
LEFT JOIN main.Invoice  AS Invoice  ON InvoiceLine.InvoiceId = Invoice.InvoiceId
LEFT JOIN main.Customer AS Customer ON Invoice.CustomerId = Customer.CustomerId
GROUP BY Customer.Country ORDER BY revenue DESC
```
> Revenue was led by USA at $523.06, followed by Canada ($303.96), France ($195.10).

**"top 5 genres by revenue"** — three hops, `Genre.Name` chosen automatically:
> Revenue was led by Rock at $826.65, followed by Latin ($382.14), Metal ($261.36).

**"revenue by month over time"** — uses `Invoice.InvoiceDate`, discovered not assumed.

**"how many invoices did we have in 2012"** — correct SQL, zero rows, and it says so
at 35% confidence rather than implying the business had no invoices.

## The honest lesson

Every bug here came from testing against data I had designed. The test suite was
large and green and still missed all six, because tests written by the person who
wrote the assumptions inherit those assumptions. Real, unfamiliar data found them
in about ten minutes.

The reassuring part: **no guardrail was breached.** Nothing leaked, nothing
executed that should not have, and the invalid `order_date` query was refused by
the validator exactly as designed. The failures were about *usefulness on
unfamiliar schemas*, not safety — which is the better of the two problems to
discover this way.

## Reproducing

```bash
curl -sSL -o chinook.sqlite \
  https://github.com/lerocha/chinook-database/raw/master/ChinookDatabase/DataSources/Chinook_Sqlite.sqlite
# load into DuckDB, then:
ai-analyst init --duckdb chinook.duckdb -o chinook.yml
SEMANTIC_LAYER_PATH=chinook.yml DUCKDB_PATH=chinook.duckdb ai-analyst check
SEMANTIC_LAYER_PATH=chinook.yml DUCKDB_PATH=chinook.duckdb \
  ai-analyst ask "top 5 genres by revenue" --no-llm
```

The review step took about five minutes: delete `Employee`, delete the flagged
personal-data columns, drop the meaningless metrics (`SUM(Bytes)`), and define
one real metric — revenue at line grain as `UnitPrice * Quantity`, which no
generator can infer because it is a business decision.
