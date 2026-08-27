"""Append-only audit log.

Every question is recorded - answered, refused or errored - with who asked it,
what SQL (if any) was executed, and which stage blocked it. For a governed
system this is the difference between "we believe it is safe" and "here is what
it did on Tuesday".

Stored in its own SQLite database, deliberately separate from the analytical
warehouse: the warehouse is opened read-only, and audit writes must never be
able to touch business data.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import settings

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id    TEXT    NOT NULL,
    ts            TEXT    NOT NULL,
    principal     TEXT    NOT NULL,
    role          TEXT    NOT NULL,
    client_ip     TEXT,
    question      TEXT    NOT NULL,
    status        TEXT    NOT NULL,
    blocked_stage TEXT,
    sql           TEXT,
    tables        TEXT,
    row_count     INTEGER,
    confidence    REAL,
    duration_ms   REAL,
    llm_used      INTEGER NOT NULL DEFAULT 0,
    issues        TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts        ON audit_events (ts);
CREATE INDEX IF NOT EXISTS idx_audit_principal ON audit_events (principal);
CREATE INDEX IF NOT EXISTS idx_audit_status    ON audit_events (status);
"""

FIELDS = [
    "id", "request_id", "ts", "principal", "role", "client_ip", "question",
    "status", "blocked_stage", "sql", "tables", "row_count", "confidence",
    "duration_ms", "llm_used", "issues",
]


@dataclass
class AuditEvent:
    request_id: str
    principal: str
    role: str
    question: str
    status: str
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    client_ip: str | None = None
    blocked_stage: str | None = None
    sql: str | None = None
    tables: list[str] = field(default_factory=list)
    row_count: int = 0
    confidence: float = 0.0
    duration_ms: float = 0.0
    llm_used: bool = False
    issues: list[str] = field(default_factory=list)

    def dict(self) -> dict[str, Any]:
        return asdict(self)


class AuditLog:
    """Thread-safe, append-only. Never raises into the request path."""

    def __init__(self, path: str | None = None):
        self.path = str(path or settings.audit_db_path)
        self._lock = threading.Lock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._shared = sqlite3.connect(self.path, check_same_thread=False) \
            if self.path == ":memory:" else None
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        if self._shared is not None:
            return self._shared
        con = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def _close(self, con: sqlite3.Connection) -> None:
        if self._shared is None:
            con.close()

    def _init_schema(self) -> None:
        with self._lock:
            con = self._connect()
            try:
                con.executescript(SCHEMA)
                con.commit()
            finally:
                self._close(con)

    # ------------------------------------------------------------------
    def record(self, event: AuditEvent) -> None:
        """Write one event. Audit failure must never break a user's request."""
        try:
            with self._lock:
                con = self._connect()
                try:
                    con.execute(
                        "INSERT INTO audit_events (request_id, ts, principal, role, "
                        "client_ip, question, status, blocked_stage, sql, tables, "
                        "row_count, confidence, duration_ms, llm_used, issues) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            event.request_id, event.ts, event.principal, event.role,
                            event.client_ip, event.question, event.status,
                            event.blocked_stage, event.sql, json.dumps(event.tables),
                            event.row_count, event.confidence, event.duration_ms,
                            int(event.llm_used), json.dumps(event.issues),
                        ),
                    )
                    con.commit()
                finally:
                    self._close(con)
        except Exception:  # pragma: no cover - defensive
            log.exception("Failed to write audit event %s", event.request_id)

    # ------------------------------------------------------------------
    def query(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        principal: str | None = None,
        status: str | None = None,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        if principal:
            clauses.append("principal = ?")
            params.append(principal)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if since:
            clauses.append("ts >= ?")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (f"SELECT {', '.join(FIELDS)} FROM audit_events {where} "
               f"ORDER BY id DESC LIMIT ? OFFSET ?")
        params += [min(limit, 1000), max(offset, 0)]

        with self._lock:
            con = self._connect()
            try:
                rows = con.execute(sql, params).fetchall()
            finally:
                self._close(con)

        out = []
        for r in rows:
            rec = dict(zip(FIELDS, r, strict=True))
            rec["tables"] = json.loads(rec["tables"] or "[]")
            rec["issues"] = json.loads(rec["issues"] or "[]")
            rec["llm_used"] = bool(rec["llm_used"])
            out.append(rec)
        return out

    def stats(self) -> dict[str, Any]:
        with self._lock:
            con = self._connect()
            try:
                total = con.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
                by_status = dict(
                    con.execute(
                        "SELECT status, COUNT(*) FROM audit_events GROUP BY status"
                    ).fetchall()
                )
                by_stage = dict(
                    con.execute(
                        "SELECT blocked_stage, COUNT(*) FROM audit_events "
                        "WHERE blocked_stage IS NOT NULL GROUP BY blocked_stage"
                    ).fetchall()
                )
                top = con.execute(
                    "SELECT principal, COUNT(*) c FROM audit_events "
                    "GROUP BY principal ORDER BY c DESC LIMIT 10"
                ).fetchall()
                first, last = con.execute(
                    "SELECT MIN(ts), MAX(ts) FROM audit_events"
                ).fetchone()
            finally:
                self._close(con)

        refused = by_status.get("refused", 0)
        return {
            "total_questions": total,
            "by_status": by_status,
            "refusals_by_stage": by_stage,
            "refusal_rate": round(refused / total, 4) if total else 0.0,
            "top_users": [{"principal": p, "questions": c} for p, c in top],
            "first_event": first,
            "last_event": last,
        }


_audit: AuditLog | None = None


def get_audit_log() -> AuditLog:
    global _audit
    if _audit is None:
        _audit = AuditLog()
    return _audit


def set_audit_log(log_instance: AuditLog | None) -> None:
    """Test hook."""
    global _audit
    _audit = log_instance
