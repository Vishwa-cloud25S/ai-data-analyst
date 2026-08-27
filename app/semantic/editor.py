"""Editing the semantic layer at runtime.

This is the most dangerous endpoint in the system, and it is worth being blunt
about why: the semantic layer decides what the model can reach, so whoever can
edit it can expose any table in the warehouse. It is therefore admin-only, every
change is validated before it lands, every save is backed up, and every edit is
written to the audit log.

Validation is not "does the YAML parse". A layer that parses but references a
column that does not exist produces a system that refuses every question at
execution time, so each entity and metric is executed against the live warehouse
before the change is accepted.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from app.core.config import settings


@dataclass
class ValidationReport:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    checked_against_warehouse: bool = False

    def dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "errors": self.errors, "warnings": self.warnings,
            "entities": self.entities, "metrics": self.metrics,
            "checked_against_warehouse": self.checked_against_warehouse,
        }


def layer_path() -> Path:
    return Path(settings.semantic_layer_path)


def backups_dir() -> Path:
    d = layer_path().parent / "semantic_layer_backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_raw() -> str:
    return layer_path().read_text()


# ---------------------------------------------------------------- validation
def validate_yaml_text(text: str, *, executor=None) -> ValidationReport:
    """Parse, load and - when an executor is given - run it against the warehouse."""
    report = ValidationReport(ok=False)

    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        report.errors.append(f"YAML is not valid: {exc}")
        return report
    if not isinstance(doc, dict):
        report.errors.append("Top level of the semantic layer must be a mapping.")
        return report
    if not doc.get("entities"):
        report.errors.append("At least one entity is required.")
        return report

    # Structural checks that produce confusing failures much later otherwise.
    names: set[str] = set()
    for e in doc.get("entities") or []:
        name = e.get("name")
        if not name:
            report.errors.append("An entity is missing 'name'.")
            continue
        if name in names:
            report.errors.append(f"Duplicate entity name: {name}")
        names.add(name)
        if not e.get("physical_table"):
            report.errors.append(f"Entity '{name}' is missing 'physical_table'.")
        if not (e.get("dimensions") or e.get("measures")):
            report.errors.append(f"Entity '{name}' declares no columns.")

    metric_names: set[str] = set()
    for m in doc.get("metrics") or []:
        mname = m.get("name")
        if not mname:
            report.errors.append("A metric is missing 'name'.")
            continue
        if mname in metric_names:
            report.errors.append(
                f"Duplicate metric name '{mname}' - the later one silently wins."
            )
        metric_names.add(mname)
        if not m.get("expression"):
            report.errors.append(f"Metric '{mname}' is missing 'expression'.")
        if m.get("entity") and m["entity"] not in names:
            report.errors.append(
                f"Metric '{mname}' references unknown entity '{m['entity']}'."
            )

    for j in doc.get("joins") or []:
        for side in ("left", "right"):
            if j.get(side) and j[side] not in names:
                report.errors.append(
                    f"Join references unknown entity '{j[side]}'."
                )
        if not j.get("sql_on"):
            report.errors.append("A join is missing 'sql_on'.")

    if report.errors:
        return report

    # Load through the real loader, so the editor cannot accept something the
    # application itself would reject.
    tmp = Path(tempfile.mkdtemp()) / "candidate.yml"
    tmp.write_text(text)
    try:
        from app.semantic.layer import load_semantic_layer

        sl = load_semantic_layer(str(tmp), dbt_path=settings.dbt_schema_path)
    except Exception as exc:
        report.errors.append(f"Semantic layer failed to load: {exc}")
        return report
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)

    report.entities = sorted(sl.entities)
    report.metrics = sorted(sl.metrics)
    if not sl.metrics:
        report.warnings.append("No metrics defined; questions will have nothing to compute.")

    # Execute everything. A layer that parses but does not run is worse than a
    # layer that fails loudly here.
    if executor is not None:
        from app.pipeline.executor import ExecutionError

        report.checked_against_warehouse = True
        for name, entity in sl.entities.items():
            cols = ", ".join(c.name for c in entity.columns)
            try:
                executor.execute(f"SELECT {cols} FROM {entity.physical_table} LIMIT 1")
            except ExecutionError as exc:
                report.errors.append(f"Entity '{name}' does not execute: {str(exc)[:200]}")
        for mname, metric in sl.metrics.items():
            entity = sl.entities.get(metric.entity)
            if entity is None:
                continue
            where = f" WHERE {' AND '.join(metric.filters)}" if metric.filters else ""
            sql = (f"SELECT {metric.expression} AS m FROM {entity.physical_table} "
                   f"AS {entity.name}{where} LIMIT 1")
            try:
                executor.execute(sql)
            except ExecutionError as exc:
                report.errors.append(f"Metric '{mname}' does not execute: {str(exc)[:200]}")

    report.ok = not report.errors
    return report


# ---------------------------------------------------------------- persistence
def save(text: str, *, author: str = "unknown") -> Path:
    """Back up the current file, then write atomically."""
    path = layer_path()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    if path.exists():
        backup = backups_dir() / f"{stamp}__{author.replace('/', '_')}.yml"
        shutil.copy2(path, backup)

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp_name, path)   # atomic: never leaves a half-written layer
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return path


def list_versions() -> list[dict[str, Any]]:
    out = []
    for f in sorted(backups_dir().glob("*.yml"), reverse=True):
        stamp, _, author = f.stem.partition("__")
        out.append({
            "id": f.name,
            "saved_at": stamp,
            "author": author or "unknown",
            "size_bytes": f.stat().st_size,
        })
    return out


def read_version(version_id: str) -> str:
    # Defend against traversal: only plain filenames inside the backups dir.
    candidate = (backups_dir() / Path(version_id).name).resolve()
    if candidate.parent != backups_dir().resolve() or not candidate.exists():
        raise FileNotFoundError(version_id)
    return candidate.read_text()


def diff_summary(old_text: str, new_text: str) -> dict[str, Any]:
    """What actually changed, in terms a reviewer cares about."""
    def parse(t: str) -> tuple[set[str], set[str], set[str]]:
        try:
            d = yaml.safe_load(t) or {}
        except yaml.YAMLError:
            return set(), set(), set()
        ents = {e.get("name") for e in (d.get("entities") or []) if e.get("name")}
        mets = {m.get("name") for m in (d.get("metrics") or []) if m.get("name")}
        cols = {
            f"{e.get('name')}.{c.get('name')}"
            for e in (d.get("entities") or [])
            for group in ("dimensions", "measures")
            for c in (e.get(group) or [])
        }
        return ents, mets, cols

    old_e, old_m, old_c = parse(old_text)
    new_e, new_m, new_c = parse(new_text)
    return {
        "entities_added": sorted(new_e - old_e),
        "entities_removed": sorted(old_e - new_e),
        "metrics_added": sorted(new_m - old_m),
        "metrics_removed": sorted(old_m - new_m),
        "columns_added": sorted(new_c - old_c),
        "columns_removed": sorted(old_c - new_c),
    }


def reload_caches() -> None:
    """Make a saved change take effect without restarting the service."""
    import app.pipeline.orchestrator as orch
    from app.pipeline.retrieval import get_retriever
    from app.semantic.layer import get_semantic_layer

    get_semantic_layer.cache_clear()
    get_retriever.cache_clear()
    orch._analyst = None
