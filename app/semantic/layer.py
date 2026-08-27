"""Load and index the semantic layer + dbt metadata.

The semantic layer is the contract: anything not declared here does not exist
as far as the LLM is concerned. The validator enforces that contract on the
generated SQL, so a hallucinated table/column can never reach the warehouse.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.core.config import settings


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    description: str
    kind: str  # "dimension" | "measure"


@dataclass(frozen=True)
class Entity:
    name: str
    description: str
    physical_table: str
    primary_key: str
    columns: tuple[Column, ...]

    @property
    def dimensions(self) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if c.kind == "dimension")

    @property
    def measures(self) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if c.kind == "measure")

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name.lower()), None)


@dataclass(frozen=True)
class Metric:
    name: str
    label: str
    description: str
    entity: str
    expression: str
    filters: tuple[str, ...]
    format: str


@dataclass(frozen=True)
class Join:
    left: str
    right: str
    on: str
    type: str


@dataclass
class SemanticLayer:
    entities: dict[str, Entity] = field(default_factory=dict)
    metrics: dict[str, Metric] = field(default_factory=dict)
    joins: list[Join] = field(default_factory=list)
    dbt_docs: dict[str, str] = field(default_factory=dict)

    # ---------- allow-lists used by the validator ----------
    @property
    def allowed_tables(self) -> set[str]:
        allowed: set[str] = set()
        for e in self.entities.values():
            allowed.add(e.name.lower())
            allowed.add(e.physical_table.lower())
            allowed.add(e.physical_table.lower().split(".")[-1])
        return allowed

    @property
    def allowed_columns(self) -> set[str]:
        return {c.name.lower() for e in self.entities.values() for c in e.columns}

    def columns_for(self, table: str) -> set[str]:
        table = table.lower().split(".")[-1]
        for e in self.entities.values():
            if table in (e.name.lower(), e.physical_table.lower().split(".")[-1]):
                return {c.name.lower() for c in e.columns}
        return set()

    def join_clause(self, left: str, right: str) -> Join | None:
        for j in self.joins:
            if {j.left, j.right} == {left, right}:
                return j
        return None


def _load_dbt_docs(path: str) -> dict[str, str]:
    """Flatten dbt schema.yml into 'model' and 'model.column' doc strings."""
    p = Path(path)
    if not p.exists():
        return {}
    raw = yaml.safe_load(p.read_text()) or {}
    docs: dict[str, str] = {}
    for model in raw.get("models", []):
        name = model["name"]
        meta = model.get("meta", {}) or {}
        meta_str = ", ".join(f"{k}={v}" for k, v in meta.items())
        desc = " ".join((model.get("description") or "").split())
        docs[name] = f"[dbt model {name}] {desc}" + (f" (meta: {meta_str})" if meta_str else "")
        for col in model.get("columns", []) or []:
            cdesc = " ".join((col.get("description") or "").split())
            if cdesc:
                docs[f"{name}.{col['name']}"] = f"[dbt column {name}.{col['name']}] {cdesc}"
    return docs


def load_semantic_layer(
    semantic_path: str | None = None, dbt_path: str | None = None
) -> SemanticLayer:
    raw = yaml.safe_load(Path(semantic_path or settings.semantic_layer_path).read_text())
    sl = SemanticLayer()

    for e in raw.get("entities", []):
        cols: list[Column] = []
        for d in e.get("dimensions", []) or []:
            cols.append(Column(d["name"].lower(), d.get("type", "varchar"),
                               d.get("description", ""), "dimension"))
        for m in e.get("measures", []) or []:
            cols.append(Column(m["name"].lower(), m.get("type", "decimal"),
                               m.get("description", ""), "measure"))
        sl.entities[e["name"]] = Entity(
            name=e["name"], description=e.get("description", ""),
            physical_table=e["physical_table"], primary_key=e.get("primary_key", ""),
            columns=tuple(cols),
        )

    for m in raw.get("metrics", []):
        sl.metrics[m["name"]] = Metric(
            name=m["name"], label=m.get("label", m["name"]),
            description=m.get("description", ""), entity=m["entity"],
            expression=m["expression"], filters=tuple(m.get("filters") or []),
            format=m.get("format", "number"),
        )

    for j in raw.get("joins", []):
        sl.joins.append(Join(j["left"], j["right"], j["sql_on"], j.get("type", "left")))

    sl.dbt_docs = _load_dbt_docs(dbt_path or settings.dbt_schema_path)
    return sl


@lru_cache(maxsize=1)
def get_semantic_layer() -> SemanticLayer:
    return load_semantic_layer()


def as_dict(sl: SemanticLayer) -> dict[str, Any]:
    return {
        "entities": [
            {
                "name": e.name,
                "table": e.physical_table,
                "description": e.description,
                "dimensions": [c.name for c in e.dimensions],
                "measures": [c.name for c in e.measures],
            }
            for e in sl.entities.values()
        ],
        "metrics": [
            {"name": m.name, "label": m.label, "description": m.description,
             "expression": m.expression, "format": m.format}
            for m in sl.metrics.values()
        ],
        "joins": [{"left": j.left, "right": j.right, "on": j.on, "type": j.type} for j in sl.joins],
    }
