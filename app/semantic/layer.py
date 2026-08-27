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
    default_time_dimension: str | None = None

    @property
    def dimensions(self) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if c.kind == "dimension")

    @property
    def measures(self) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if c.kind == "measure")

    def column(self, name: str) -> Column | None:
        """Case-insensitive lookup; the returned name keeps the customer's casing."""
        lowered = name.lower()
        return next((c for c in self.columns if c.name.lower() == lowered), None)

    @property
    def display_column(self) -> str | None:
        """The human-readable label for this entity - what to group by.

        'top genres' means Genre.Name, not Genre.GenreId.
        """
        pk = (self.primary_key or "").lower()
        preferred = ("name", "title", "label", "description", "code")
        dims = [c for c in self.dimensions if c.name.lower() != pk]
        for want in preferred:
            for c in dims:
                if c.name.lower() == want:
                    return c.name
        for want in preferred:
            for c in dims:
                if want in c.name.lower():
                    return c.name
        for c in dims:
            if "char" in c.type.lower() or "text" in c.type.lower() or "string" in c.type.lower():
                return c.name
        return dims[0].name if dims else None


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

    # ---------- resolution helpers (schema-driven, not hardcoded) ----------
    def column_index(self) -> dict[str, list[tuple[str, str]]]:
        """snake(column) -> [(entity, original_column_name)].

        Lets a question word be matched to whatever the customer actually
        called the column, in whatever case they used.
        """
        from app.semantic.bootstrap import snake

        index: dict[str, list[tuple[str, str]]] = {}
        for e in self.entities.values():
            for c in e.columns:
                index.setdefault(snake(c.name), []).append((e.name, c.name))
        return index

    def resolve_column(self, word: str, prefer: list[str] | None = None,
                       dimensions_only: bool = False) -> tuple[str, str] | None:
        """Map a user's word to (entity, column). Case- and plural-insensitive."""
        from app.semantic.bootstrap import _singularise, snake

        idx = self.column_index()
        if dimensions_only:
            allowed = {
                (e.name, c.name) for e in self.entities.values() for c in e.dimensions
            }
            idx = {k: [x for x in v if x in allowed] for k, v in idx.items()}
            idx = {k: v for k, v in idx.items() if v}
        w = snake(word)
        candidates = idx.get(w) or idx.get(_singularise(w)) or []
        if not candidates:
            # 'country' -> BillingCountry; 'product' -> product_name
            sw = _singularise(w)
            for key, cols in idx.items():
                if (key.endswith(f"_{w}") or key.endswith(f"_{sw}")
                        or key.startswith(f"{w}_") or key.startswith(f"{sw}_")):
                    candidates.extend(cols)
        if not candidates:
            return None
        if prefer:
            for entity in prefer:
                for ent, col in candidates:
                    if ent == entity:
                        return (ent, col)
        return candidates[0]

    def resolve_grouping(self, word: str, prefer: list[str] | None = None
                         ) -> tuple[str, str] | None:
        """Resolve a word the user wants to group by, preferring labels over keys.

        'product' should mean dim_products.product_name, not fct_orders.product_id.
        Grouping by a surrogate key produces technically-correct, humanly-useless
        output, so an entity match beats a key-column match.
        """
        from app.semantic.bootstrap import _singularise, is_key_like, snake

        w = snake(word)
        sw = _singularise(w)

        # 1. An entity of that name -> its display column.
        for name, entity in self.entities.items():
            stem = snake(name)
            for prefix in ("dim_", "fct_", "fact_", "d_", "f_"):
                stem = stem.removeprefix(prefix)
            if w in (stem, _singularise(stem)) or sw == _singularise(stem):
                col = entity.display_column
                if col:
                    return (name, col)

        # 2. A non-key dimension of that name (never a measure: grouping by
        #    SUM-able values is meaningless).
        hit = self.resolve_column(word, prefer=prefer, dimensions_only=True)
        if hit and not is_key_like(hit[1]):
            return hit

        # 3. A key column: follow it to the entity it references, if any.
        if hit:
            stem = snake(hit[1]).removesuffix("_id")
            for name, entity in self.entities.items():
                ename = snake(name)
                for prefix in ("dim_", "fct_", "fact_", "d_", "f_"):
                    ename = ename.removeprefix(prefix)
                if stem in (ename, _singularise(ename)):
                    col = entity.display_column
                    if col:
                        return (name, col)
            return hit
        return None

    def time_dimension(self, entity_name: str) -> str | None:
        """The column to use for time filters and grain on this entity.

        Declared via `default_time_dimension`, otherwise the first date/time
        typed column. Nothing here may assume a column called `order_date`.
        """
        e = self.entities.get(entity_name)
        if e is None:
            return None
        if e.default_time_dimension:
            return e.default_time_dimension
        for c in e.columns:
            if c.kind == "dimension" and any(
                t in c.type.lower() for t in ("date", "time", "timestamp")
            ):
                return c.name
        return None

    def join_path(self, start: str, target: str, max_hops: int = 3) -> list[Join] | None:
        """Shortest join path between two entities (breadth-first).

        Real schemas need more than one hop: InvoiceLine reaches Customer only
        through Invoice.
        """
        if start == target:
            return []
        from collections import deque

        adjacency: dict[str, list[Join]] = {}
        for j in self.joins:
            adjacency.setdefault(j.left, []).append(j)
            adjacency.setdefault(j.right, []).append(j)

        queue: deque[tuple[str, list[Join]]] = deque([(start, [])])
        seen = {start}
        while queue:
            node, path = queue.popleft()
            if len(path) >= max_hops:
                continue
            for j in adjacency.get(node, []):
                nxt = j.right if j.left == node else j.left
                if nxt in seen:
                    continue
                new_path = path + [j]
                if nxt == target:
                    return new_path
                seen.add(nxt)
                queue.append((nxt, new_path))
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
            cols.append(Column(d["name"], d.get("type", "varchar"),
                               d.get("description", ""), "dimension"))
        for m in e.get("measures", []) or []:
            cols.append(Column(m["name"], m.get("type", "decimal"),
                               m.get("description", ""), "measure"))
        sl.entities[e["name"]] = Entity(
            name=e["name"], description=e.get("description", ""),
            physical_table=e["physical_table"], primary_key=e.get("primary_key", ""),
            columns=tuple(cols),
            default_time_dimension=e.get("default_time_dimension"),
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
