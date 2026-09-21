"""Read-only API for privacy-safe SQLite agent-audit events."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query

router = APIRouter()

_DB_PATH = Path(__file__).resolve().parents[3] / "audit.db"
_MAX_LIMIT = 200
_SAFE_FIELDS = {
    "schema_version", "timestamp", "event", "profile_name", "session_id", "task_id",
    "turn_id", "action", "skill", "provenance", "use_count", "reused", "tool",
    "status", "duration_ms", "paths", "provider", "operation", "category", "reason",
    "rule_id", "expected_providers", "observed_providers", "trigger", "required_rules",
    "changed_paths", "generation", "validator", "rules", "attempt", "validation_status",
    "rule_statuses", "missing_rules", "failed_rules", "unresolved_mutation", "completed",
    "failed", "interrupted", "turn_exit_reason",
}


def initialize_database(path: Path) -> None:
    """Create the shared audit schema for the writer and read-only API tests."""
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY,
                timestamp TEXT NOT NULL,
                profile_name TEXT,
                session_id TEXT,
                task_id TEXT,
                turn_id TEXT,
                event_type TEXT NOT NULL,
                status TEXT,
                generation INTEGER,
                rule_id TEXT,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS audit_events_profile_timestamp_idx
                ON audit_events(profile_name, timestamp DESC);
            CREATE INDEX IF NOT EXISTS audit_events_session_timestamp_idx
                ON audit_events(session_id, timestamp DESC);
            CREATE INDEX IF NOT EXISTS audit_events_type_timestamp_idx
                ON audit_events(event_type, timestamp DESC);
            CREATE INDEX IF NOT EXISTS audit_events_status_timestamp_idx
                ON audit_events(status, timestamp DESC);
            """
        )
        connection.commit()
    finally:
        connection.close()


def _safe_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {key: decoded[key] for key in _SAFE_FIELDS if key in decoded}


def _event_projection(row: sqlite3.Row) -> dict[str, Any]:
    event = {
        "timestamp": row["timestamp"], "event": row["event_type"],
        "profile_name": row["profile_name"], "session_id": row["session_id"],
        "task_id": row["task_id"], "turn_id": row["turn_id"],
        "status": row["status"], "generation": row["generation"], "rule_id": row["rule_id"],
    }
    event.update(_safe_payload(row["payload_json"]))
    return {key: value for key, value in event.items() if value is not None}


def _read_connection(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(path, timeout=1)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 1000")
        return connection
    except sqlite3.Error:
        return None


def read_events(
    database_path: Path = _DB_PATH,
    *,
    profile_name: str | None = None,
    event_type: str | None = None,
    status: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    """Return one bounded, filterable SQLite page in timestamp order."""
    connection = _read_connection(database_path)
    if connection is None:
        return {"events": [], "total": 0}
    clauses: list[str] = []
    values: list[Any] = []
    for column, value in (("profile_name", profile_name), ("event_type", event_type), ("status", status)):
        if value:
            clauses.append(f"{column} = ?")
            values.append(value)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    bounded_offset = max(0, offset)
    bounded_limit = min(_MAX_LIMIT, max(1, limit))
    try:
        total = connection.execute(f"SELECT COUNT(*) FROM audit_events{where}", values).fetchone()[0]
        rows = connection.execute(
            f"""
            SELECT timestamp, profile_name, session_id, task_id, turn_id,
                   event_type, status, generation, rule_id, payload_json
            FROM audit_events{where}
            ORDER BY timestamp DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            [*values, bounded_limit, bounded_offset],
        ).fetchall()
        return {"events": [_event_projection(row) for row in rows], "total": total}
    except sqlite3.Error:
        return {"events": [], "total": 0}
    finally:
        connection.close()


def audit_summary(database_path: Path = _DB_PATH) -> dict[str, Any]:
    """Return privacy-safe aggregate counts for the Audit Explorer summary."""
    connection = _read_connection(database_path)
    if connection is None:
        return {"total_events": 0, "profiles": {}, "event_types": {}, "statuses": {}}
    try:
        rows = connection.execute("SELECT profile_name, event_type, status FROM audit_events").fetchall()
        return {
            "total_events": len(rows),
            "profiles": dict(sorted(Counter(row["profile_name"] or "unknown" for row in rows).items())),
            "event_types": dict(sorted(Counter(row["event_type"] for row in rows).items())),
            "statuses": dict(sorted(Counter(row["status"] for row in rows if row["status"]).items())),
        }
    except sqlite3.Error:
        return {"total_events": 0, "profiles": {}, "event_types": {}, "statuses": {}}
    finally:
        connection.close()


@router.get("/summary")
def summary() -> dict[str, Any]:
    return audit_summary()


@router.get("/events")
def events(
    profile_name: str | None = None,
    event_type: str | None = None,
    status: str | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=_MAX_LIMIT),
) -> dict[str, Any]:
    return read_events(
        profile_name=profile_name,
        event_type=event_type,
        status=status,
        offset=offset,
        limit=limit,
    )
