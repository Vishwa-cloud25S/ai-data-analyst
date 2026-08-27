"""Command-line interface.

    ai-analyst init --duckdb ./warehouse.duckdb -o semantic_layer.yml
    ai-analyst init --dbt target/manifest.json  -o semantic_layer.yml
    ai-analyst ask  "what were our highest revenue products last quarter?"
    ai-analyst keygen --role admin --name alice
    ai-analyst check
    ai-analyst serve --port 8000

`init` and `ask` exist so an evaluator can point this at their own warehouse
and get an answer without reading any source code.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path


def _cmd_init(args: argparse.Namespace) -> int:
    from app.semantic import bootstrap

    if args.dbt:
        tables = bootstrap.from_dbt_manifest(args.dbt, schema=args.schema or "main")
        source = f"dbt manifest {args.dbt}"
    elif args.postgres:
        tables = bootstrap.introspect_postgres(args.postgres, schema=args.schema or "public")
        source = "PostgreSQL"
    else:
        path = args.duckdb or "data/warehouse.duckdb"
        if not Path(path).exists():
            print(f"error: no DuckDB file at {path}", file=sys.stderr)
            return 2
        tables = bootstrap.introspect_duckdb(path, schema=args.schema or "main")
        source = f"DuckDB {path}"

    if args.exclude:
        excluded = {e.strip().lower() for e in args.exclude.split(",")}
        tables = [t for t in tables if t.name.lower() not in excluded]
    if not tables:
        print("error: no tables found", file=sys.stderr)
        return 2

    yaml_text = bootstrap.to_yaml(
        tables, include=[t.strip() for t in args.include.split(",")] if args.include else None
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml_text)

    facts = [t.name for t in tables if t.is_fact]
    print(f"Read {len(tables)} tables from {source}.")
    print(f"  fact tables:      {', '.join(facts) or '(none detected)'}")
    print(f"  dimension tables: {', '.join(t.name for t in tables if not t.is_fact) or '(none)'}")
    print(f"\nWrote draft semantic layer to {out}")
    print("\nNext: open it and (1) delete anything the model must not see, "
          "(2) fix every metric marked REVIEW, (3) check the inferred joins.")
    print(f"Then: SEMANTIC_LAYER_PATH={out} ai-analyst check")
    return 0


def _cmd_ask(args: argparse.Namespace) -> int:
    from app.pipeline.orchestrator import Analyst

    analyst = Analyst(use_llm=not args.no_llm)
    result = analyst.ask(args.question)

    if args.json:
        print(json.dumps(result.dict(), indent=2, default=str))
        return 0 if result.status == "answered" else 1

    icon = {"answered": "✔", "refused": "✘", "error": "!"}.get(result.status, "?")
    print(f"\n{icon} {result.status.upper()}  ({result.confidence:.0%} confidence)\n")
    print(result.answer, "\n")
    if result.sql:
        print("--- SQL executed " + "-" * 44)
        print(result.sql)
        print("-" * 61)
    if result.rows:
        widths = [
            max(len(str(c)), max((len(str(r[i])) for r in result.rows[:20]), default=0))
            for i, c in enumerate(result.columns)
        ]
        print("  " + "  ".join(str(c).ljust(w) for c, w in zip(result.columns, widths, strict=True)))
        print("  " + "  ".join("-" * w for w in widths))
        for row in result.rows[:20]:
            print("  " + "  ".join(str(v).ljust(w) for v, w in zip(row, widths, strict=True)))
        if result.row_count > 20:
            print(f"  ... {result.row_count - 20} more rows")
    if args.trace:
        print("\n--- pipeline " + "-" * 48)
        for s in result.trace:
            mark = {"ok": "✔", "blocked": "■", "error": "!"}.get(s.status, "?")
            print(f"  {mark} {s.name:<20} {s.duration_ms:>8.2f} ms  {s.status}")
    return 0 if result.status == "answered" else 1


def _cmd_keygen(args: argparse.Namespace) -> int:
    key = f"ada_{args.role[:1]}_{secrets.token_urlsafe(24)}"
    print(f"\nAPI key for {args.name} ({args.role}):\n\n  {key}\n")
    print("Add it to the API environment (keep the plaintext safe - it is not recoverable):")
    print(f'\n  API_KEYS="{key}:{args.role}:{args.name}"')
    print("  AUTH_ENABLED=true\n")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    """Validate the configured semantic layer against the live warehouse."""
    from app.pipeline.executor import ExecutionError, get_executor
    from app.semantic.layer import load_semantic_layer

    problems: list[str] = []
    try:
        sl = load_semantic_layer()
    except Exception as exc:
        print(f"✘ semantic layer failed to load: {exc}", file=sys.stderr)
        return 2

    print(f"✔ semantic layer: {len(sl.entities)} entities, {len(sl.metrics)} metrics, "
          f"{len(sl.joins)} joins")

    executor = get_executor()
    for name, entity in sl.entities.items():
        cols = ", ".join(c.name for c in entity.columns)
        try:
            executor.execute(f"SELECT {cols} FROM {entity.physical_table} LIMIT 1")
            print(f"  ✔ {name:<22} {entity.physical_table}")
        except ExecutionError as exc:
            problems.append(f"{name}: {exc}")
            print(f"  ✘ {name:<22} {exc}")

    for mname, metric in sl.metrics.items():
        entity = sl.entities.get(metric.entity)
        if entity is None:
            problems.append(f"metric {mname} references unknown entity {metric.entity}")
            print(f"  ✘ metric {mname}: unknown entity {metric.entity}")
            continue
        where = f" WHERE {' AND '.join(metric.filters)}" if metric.filters else ""
        sql = (f"SELECT {metric.expression} AS m FROM {entity.physical_table} "
               f"AS {entity.name}{where} LIMIT 1")
        try:
            executor.execute(sql)
            print(f"  ✔ metric {mname}")
        except ExecutionError as exc:
            problems.append(f"metric {mname}: {exc}")
            print(f"  ✘ metric {mname}: {str(exc)[:90]}")

    if problems:
        print(f"\n{len(problems)} problem(s) found.", file=sys.stderr)
        return 1
    print("\nAll entities and metrics execute against the warehouse.")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:  # pragma: no cover - process launcher
    import uvicorn

    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ai-analyst",
        description="Governed natural-language analytics: the LLM plans, "
                    "the semantic layer decides, the warehouse stays read-only.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    i = sub.add_parser("init", help="generate a draft semantic layer from your warehouse")
    src = i.add_mutually_exclusive_group()
    src.add_argument("--duckdb", help="path to a DuckDB file")
    src.add_argument("--postgres", help="PostgreSQL DSN")
    src.add_argument("--dbt", help="path to dbt target/manifest.json")
    i.add_argument("--schema", help="schema to introspect")
    i.add_argument("--include", help="comma-separated table allow-list")
    i.add_argument("--exclude", help="comma-separated tables to leave out entirely")
    i.add_argument("-o", "--out", default="semantic_layer.yml")
    i.set_defaults(func=_cmd_init)

    a = sub.add_parser("ask", help="ask a question from the terminal")
    a.add_argument("question")
    a.add_argument("--no-llm", action="store_true", help="deterministic planner only")
    a.add_argument("--trace", action="store_true", help="show pipeline stages")
    a.add_argument("--json", action="store_true", help="machine-readable output")
    a.set_defaults(func=_cmd_ask)

    k = sub.add_parser("keygen", help="generate an API key")
    k.add_argument("--role", choices=["viewer", "analyst", "admin"], default="viewer")
    k.add_argument("--name", default="user")
    k.set_defaults(func=_cmd_keygen)

    c = sub.add_parser("check", help="verify the semantic layer against the warehouse")
    c.set_defaults(func=_cmd_check)

    s = sub.add_parser("serve", help="run the API")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--reload", action="store_true")
    s.set_defaults(func=_cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
