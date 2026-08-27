from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    app_name: str = "ai-data-analyst"
    environment: str = "local"

    # --- warehouse ---
    warehouse: Literal["duckdb", "postgres"] = "duckdb"
    duckdb_path: str = str(ROOT / "data" / "warehouse.duckdb")
    postgres_dsn: str = "postgresql://analyst:analyst@localhost:5432/warehouse"

    # --- LLM ---
    # provider: auto (openai if a key is set) | openai | local | none
    llm_provider: Literal["auto", "openai", "local", "none"] = "auto"
    # Set for any OpenAI-compatible local server and the app talks to it instead:
    #   Ollama    http://localhost:11434/v1
    #   vLLM      http://localhost:8000/v1
    #   LM Studio http://localhost:1234/v1
    llm_base_url: str = ""
    llm_model: str = ""          # defaults to openai_model when empty
    llm_timeout_seconds: int = 60
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.0
    # When no API key is configured the app falls back to a deterministic
    # rule-based planner so the whole pipeline still runs offline / in CI.
    allow_offline_llm: bool = True

    # --- auth ---
    # "key1:admin:alice,key2:analyst:bob,key3:viewer:dashboard"
    auth_enabled: bool = False
    api_keys: str = ""

    # --- audit ---
    audit_enabled: bool = True
    audit_db_path: str = str(ROOT / "data" / "audit.sqlite")

    # --- guardrails ---
    max_rows: int = 1000
    query_timeout_seconds: int = 20
    max_joins: int = 4

    # --- semantic layer / RAG ---
    semantic_layer_path: str = str(ROOT / "app" / "semantic" / "semantic_layer.yml")
    dbt_schema_path: str = str(ROOT / "dbt" / "models" / "marts" / "schema.yml")
    retrieval_top_k: int = 6

    api_url: str = "http://localhost:8000"


settings = Settings()
