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


@pytest.fixture(scope="session")
def live_api_url(warehouse, retriever):
    """Run the real FastAPI app on a background port for UI client tests."""
    import threading
    import time

    import uvicorn

    import app.pipeline.orchestrator as orch
    from app.main import app as fastapi_app
    from app.pipeline.executor import DuckDBExecutor
    from app.pipeline.orchestrator import Analyst

    orch._analyst = Analyst(
        executor=DuckDBExecutor(path=warehouse), retriever=retriever, use_llm=False
    )
    config = uvicorn.Config(fastapi_app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 30
    while time.time() < deadline:
        if server.started and server.servers and server.servers[0].sockets:
            port = server.servers[0].sockets[0].getsockname()[1]
            break
        time.sleep(0.1)
    else:  # pragma: no cover
        raise RuntimeError("test API server did not start")

    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def client(warehouse, retriever, tmp_path, monkeypatch):
    """TestClient with auth disabled (the local-development posture)."""
    from fastapi.testclient import TestClient

    import app.pipeline.orchestrator as orch
    from app.core.audit import AuditLog, set_audit_log
    from app.core.config import settings
    from app.main import app as fastapi_app
    from app.pipeline.executor import DuckDBExecutor
    from app.pipeline.orchestrator import Analyst

    monkeypatch.setattr(settings, "auth_enabled", False)
    set_audit_log(AuditLog(str(tmp_path / "audit.sqlite")))
    orch._analyst = Analyst(
        executor=DuckDBExecutor(path=warehouse), retriever=retriever, use_llm=False
    )
    with TestClient(fastapi_app) as c:
        yield c
    set_audit_log(None)
