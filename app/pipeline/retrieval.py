"""RAG over the semantic layer + dbt metadata.

Deliberately dependency-free: a TF-IDF/BM25-lite retriever over documents built
from entities, columns, metrics and dbt descriptions. It is deterministic,
fast, offline-friendly and good enough for a schema of this size. Swap in
pgvector/FAISS by re-implementing `SchemaRetriever.search`.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache

from app.semantic.layer import SemanticLayer, get_semantic_layer

_TOKEN = re.compile(r"[a-z0-9]+")

SYNONYMS = {
    "revenue": ["sales", "turnover", "income", "gmv", "money", "earned", "top", "highest"],
    "product": ["sku", "item", "goods"],
    "customer": ["client", "buyer", "account", "user"],
    "quarter": ["q1", "q2", "q3", "q4", "quarterly"],
    "region": ["geo", "geography", "market", "territory"],
    "channel": ["source", "platform"],
    "margin": ["profit", "profitability"],
    "returned": ["refund", "return", "rma"],
}


def tokenize(text: str) -> list[str]:
    toks = _TOKEN.findall(text.lower())
    out = list(toks)
    for tok in toks:
        for canon, alts in SYNONYMS.items():
            if tok == canon:
                out.extend(alts)
            elif tok in alts:
                out.append(canon)
    return out


@dataclass
class Doc:
    id: str
    kind: str  # entity | column | metric | join | dbt
    text: str
    payload: dict


@dataclass
class Hit:
    doc: Doc
    score: float


def build_documents(sl: SemanticLayer) -> list[Doc]:
    docs: list[Doc] = []
    for e in sl.entities.values():
        docs.append(Doc(
            id=f"entity:{e.name}", kind="entity",
            text=f"{e.name} {e.physical_table} {e.description} "
                 f"{sl.dbt_docs.get(e.name, '')}",
            payload={"entity": e.name},
        ))
        for c in e.columns:
            docs.append(Doc(
                id=f"column:{e.name}.{c.name}", kind="column",
                text=f"{e.name}.{c.name} {c.kind} {c.type} {c.description} "
                     f"{sl.dbt_docs.get(f'{e.name}.{c.name}', '')}",
                payload={"entity": e.name, "column": c.name, "kind": c.kind},
            ))
    for m in sl.metrics.values():
        docs.append(Doc(
            id=f"metric:{m.name}", kind="metric",
            text=f"metric {m.name} {m.label} {m.description} {m.expression}",
            payload={"metric": m.name, "entity": m.entity},
        ))
    for j in sl.joins:
        docs.append(Doc(
            id=f"join:{j.left}->{j.right}", kind="join",
            text=f"join {j.left} {j.right} on {j.on}",
            payload={"left": j.left, "right": j.right},
        ))
    return docs


class SchemaRetriever:
    """TF-IDF cosine retriever with entity/metric expansion."""

    def __init__(self, sl: SemanticLayer | None = None):
        self.sl = sl or get_semantic_layer()
        self.docs = build_documents(self.sl)
        self.tf: list[Counter] = [Counter(tokenize(d.text)) for d in self.docs]
        df: Counter = Counter()
        for c in self.tf:
            df.update(c.keys())
        n = len(self.docs)
        self.idf = {t: math.log((n + 1) / (v + 1)) + 1 for t, v in df.items()}
        self.norms = [
            math.sqrt(sum((f * self.idf.get(t, 0.0)) ** 2 for t, f in c.items())) or 1.0
            for c in self.tf
        ]

    def search(self, question: str, top_k: int = 6) -> list[Hit]:
        q = Counter(tokenize(question))
        qnorm = math.sqrt(sum((f * self.idf.get(t, 0.0)) ** 2 for t, f in q.items())) or 1.0
        hits: list[Hit] = []
        for i, doc in enumerate(self.docs):
            dot = sum(
                f * self.idf.get(t, 0.0) * self.tf[i].get(t, 0) * self.idf.get(t, 0.0)
                for t, f in q.items()
            )
            if dot > 0:
                hits.append(Hit(doc, dot / (qnorm * self.norms[i])))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    def retrieve_context(self, question: str, top_k: int | None = None) -> dict:
        """Return the pruned schema slice that will be shown to the LLM."""
        from app.core.config import settings

        top_k = top_k or settings.retrieval_top_k
        hits = self.search(question, top_k=max(top_k * 3, 18))

        entities: list[str] = []
        metrics: list[str] = []
        for h in hits:
            ent = h.doc.payload.get("entity")
            if ent and ent not in entities:
                entities.append(ent)
            met = h.doc.payload.get("metric")
            if met and met not in metrics:
                metrics.append(met)

        if not entities:
            entities = ["fct_orders"]
        if "fct_orders" not in entities and any(
            m in self.sl.metrics for m in metrics
        ):
            entities.insert(0, "fct_orders")
        entities = entities[:3]
        if not metrics:
            metrics = ["total_revenue"]
        metrics = metrics[:4]

        return {
            "entities": entities,
            "metrics": metrics,
            "hits": [{"id": h.doc.id, "kind": h.doc.kind, "score": round(h.score, 4)}
                     for h in hits[:top_k]],
        }

    def render_schema_prompt(self, context: dict) -> str:
        """Render only the retrieved slice of the semantic layer as prompt text."""
        lines: list[str] = ["## Available tables (you may not reference anything else)"]
        for name in context["entities"]:
            e = self.sl.entities[name]
            lines.append(f"\n### {e.physical_table}  (alias: {e.name})")
            lines.append(f"{e.description}")
            dbt = self.sl.dbt_docs.get(e.name)
            if dbt:
                lines.append(dbt)
            for c in e.columns:
                extra = self.sl.dbt_docs.get(f"{e.name}.{c.name}", "")
                lines.append(f"  - {c.name} ({c.type}, {c.kind}): {c.description} {extra}".rstrip())
        joins = [
            j for j in self.sl.joins
            if j.left in context["entities"] and j.right in context["entities"]
        ]
        if joins:
            lines.append("\n## Approved joins")
            for j in joins:
                lines.append(f"  - {j.type} join {j.right} on {j.on}")
        lines.append("\n## Certified metrics (prefer these expressions verbatim)")
        for m in context["metrics"]:
            metric = self.sl.metrics[m]
            filt = " AND ".join(metric.filters) if metric.filters else "none"
            lines.append(f"  - {metric.name}: {metric.expression}")
            lines.append(f"      meaning: {metric.description}")
            lines.append(f"      mandatory filters: {filt}")
        return "\n".join(lines)


@lru_cache(maxsize=1)
def get_retriever() -> SchemaRetriever:
    return SchemaRetriever()
