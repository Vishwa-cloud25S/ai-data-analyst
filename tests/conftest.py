import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("WAREHOUSE", "duckdb")


@pytest.fixture(scope="session")
def warehouse(tmp_path_factory) -> str:
    from app.db.seed import build

    path = tmp_path_factory.mktemp("wh") / "test.duckdb"
    return build(str(path), days=800, n_orders=1500)


@pytest.fixture(scope="session")
def semantic_layer():
    from app.semantic.layer import load_semantic_layer

    return load_semantic_layer()


@pytest.fixture(scope="session")
def retriever(semantic_layer):
    from app.pipeline.retrieval import SchemaRetriever

    return SchemaRetriever(semantic_layer)


@pytest.fixture(scope="session")
def analyst(warehouse, retriever):
    from app.pipeline.executor import DuckDBExecutor
    from app.pipeline.orchestrator import Analyst

    return Analyst(executor=DuckDBExecutor(path=warehouse), retriever=retriever, use_llm=False)
