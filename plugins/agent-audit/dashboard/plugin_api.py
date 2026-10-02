"""Read-only API for privacy-safe SQLite agent-audit events."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query

try:
    from ..audit_storage import database_path
except ImportError:
    import sys

    plugin_root = str(Path(__file__).resolve().parents[1])
    if plugin_root not in sys.path:
        sys.path.insert(0, plugin_root)
    from audit_storage import database_path

router = APIRouter()

_MAX_LIMIT = 200
_SAFE_FIELDS = {
    "schema_version", "timestamp", "event", "project_name", "profile_name", "session_id", "task_id",
    "turn_id", "action", "skill", "provenance", "use_count", "reused", "tool",
    "status", "duration_ms", "paths", "provider", "operation", "category", "reason",
    "rule_id", "expected_providers", "observed_providers", "trigger", "required_rules",
    "changed_paths", "generation", "validator", "rules", "attempt", "validation_status",
    "rule_statuses", "missing_rules", "failed_rules", "unresolved_mutation", "completed",
    "failed", "interrupted", "turn_exit_reason",
    "model", "model_provider", "model_kind",
}

_PROMOTED_FIELDS = {
    "schema_version", "timestamp", "event", "project_name", "profile_name", "session_id", "task_id",
    "turn_id", "status", "duration_ms", "paths", "provider", "operation", "tool",
    "skill", "validator", "rules", "required_rules", "rule_id", "generation", "action",
    "category", "reason", "validation_status", "model", "model_provider", "model_kind",
}

_TOOL_ACTIVITIES = {
    "read_file": ("filesystem.read", "project_file", "read"),
    "search_files": ("filesystem.search", "project_files", "search"),
    "write_file": ("filesystem.modify", "project_file", "modify"),
    "patch": ("filesystem.modify", "project_files", "modify"),
    "terminal": ("system.command", "local_environment", "execute"),
    "computer_use": ("desktop.interact", "desktop_application", "interact"),
    "browser_exec": ("web.interact", "web_page", "interact"),
    "web_extract": ("web.research", "web_page", "read"),
    "skill_view": ("skill.inspect", "skill", "inspect"),
    "todo_list": ("workflow.plan", "task_plan", "update"),
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
        columns = {row[1] for row in connection.execute("PRAGMA table_info(audit_events)")}
        if "project_name" not in columns:
            connection.execute("ALTER TABLE audit_events ADD COLUMN project_name TEXT")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS audit_events_project_timestamp_idx "
            "ON audit_events(project_name, timestamp DESC)"
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


def _compact(mapping: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in mapping.items() if value not in (None, "", [], {})}


def _outcome_state(status: Any) -> str:
    normalized = str(status or "").lower()
    if normalized in {"success", "succeeded", "passed", "completed", "ok", "allow"}:
        return "succeeded"
    if normalized in {"failed", "failure", "error", "blocked"}:
        return "failed"
    if normalized in {"cancelled", "canceled", "interrupted"}:
        return "interrupted"
    return normalized or "unknown"


def _activity(event: dict[str, Any]) -> dict[str, Any]:
    event_type = event.get("event") or "audit_event"
    tool = event.get("tool")
    if event_type == "tool_call":
        if tool == "mcp__codebase_memory_mcp__index_repository":
            code, subject_type, operation = "code.index", "codebase", "index"
        elif isinstance(tool, str) and tool.startswith(
            ("mcp__semble_mcp__", "mcp__codebase_memory_mcp__")
        ):
            code, subject_type, operation = "code.discovery", "codebase", "inspect"
        else:
            code, subject_type, operation = _TOOL_ACTIVITIES.get(
                tool, ("tool.execute", "tool_operation", "execute")
            )
        return _compact(
            {"code": code, "subject_type": subject_type, "tool": tool, "operation": operation}
        )
    if event_type == "skill_lifecycle":
        return _compact(
            {"code": "skill.lifecycle", "subject_type": "skill", "skill": event.get("skill"),
             "operation": event.get("action")}
        )
    if event_type == "discovery_observation":
        return _compact(
            {"code": "code.discovery", "subject_type": "codebase", "provider": event.get("provider"),
             "operation": event.get("operation")}
        )
    if event_type in {"validation_trigger", "validation_result", "verification_gate"}:
        return _compact(
            {"code": f"validation.{event_type.removeprefix('validation_')}",
             "subject_type": "verification", "validator": event.get("validator")}
        )
    if event_type == "workflow_deviation":
        return _compact(
            {"code": "workflow.deviation", "subject_type": "workflow_rule",
             "operation": event.get("reason")}
        )
    if event_type == "session_end":
        return {"code": "session.end", "subject_type": "session", "operation": "finish"}
    return {"code": "audit.event", "subject_type": "audit_event", "operation": event_type}


def _importance(event: dict[str, Any], state: str) -> str:
    if state == "failed":
        return "error"
    if event.get("event") == "workflow_deviation" or event.get("action") == "continue":
        return "warning"
    return "info"


def _explanation(event: dict[str, Any], state: str) -> dict[str, Any]:
    event_type = event.get("event") or "audit_event"
    tool = event.get("tool")
    if event_type == "tool_call" and tool:
        code = f"tool.{tool}.{state}"
    elif event_type == "workflow_deviation" and event.get("reason"):
        code = f"workflow.{event['reason']}"
    else:
        code = f"event.{event_type}.{state}"
    params: dict[str, Any] = {}
    if event.get("paths"):
        params["path_count"] = len(event["paths"])
    if event.get("rules"):
        params["rule_count"] = len(event["rules"])
    explanation = {"code": code, "params": params}
    if event_type == "workflow_deviation" or event.get("action") == "continue":
        explanation["next_action_code"] = event.get("reason") or "complete_required_validation"
    return _compact(explanation)


def _event_projection(row: sqlite3.Row) -> dict[str, Any]:
    payload = _safe_payload(row["payload_json"])
    event = {
        "timestamp": row["timestamp"], "event": row["event_type"],
        "project_name": row["project_name"], "profile_name": row["profile_name"],
        "session_id": row["session_id"],
        "task_id": row["task_id"], "turn_id": row["turn_id"],
        "status": row["status"], "generation": row["generation"], "rule_id": row["rule_id"],
    }
    event.update(payload)
    legacy = {key: value for key, value in event.items() if value is not None}
    state = _outcome_state(event.get("status") or event.get("validation_status") or event.get("action"))
    model = _compact(
        {"name": event.get("model"), "provider": event.get("model_provider"), "kind": event.get("model_kind")}
    )
    scope = _compact(
        {"project": event.get("project_name"),
         "paths": event.get("paths") or event.get("changed_paths"),
         "rules": event.get("rules") or event.get("required_rules"),
         "provider": event.get("provider"), "validator": event.get("validator")}
    )
    correlation = _compact(
        {"session_id": event.get("session_id"), "task_id": event.get("task_id"),
         "turn_id": event.get("turn_id"), "generation": event.get("generation")}
    )
    metadata = {
        key: value for key, value in payload.items()
        if key not in _PROMOTED_FIELDS and value not in (None, "", [], {})
    }
    evidence = _compact(
        {"event_type": event.get("event") or row["event_type"],
         "storage_schema_version": payload.get("schema_version"),
         "reason_code": event.get("reason"), "rule_id": event.get("rule_id")}
    )
    evidence["metadata"] = metadata
    projection = {
        "schema_version": 2,
        "id": row["id"],
        "kind": event.get("event") or row["event_type"],
        "importance": _importance(event, state),
        "actor": _compact({"profile": event.get("profile_name")}),
        "activity": _activity(event),
        "outcome": _compact({"state": state, "duration_ms": event.get("duration_ms"),
                              "action": event.get("action")}),
        "scope": scope,
        "explanation": _explanation(event, state),
        "correlation": correlation,
        "evidence": evidence,
        "privacy": {"sanitized": True, "raw_content_stored": False},
    }
    if model:
        projection["model"] = model
    # Keep the v1 flat projection during the v2 rollout so stale Desktop copies remain usable.
    return {**legacy, **projection}


def _read_connection(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(path, timeout=1)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 1000")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(audit_events)")}
        if columns and "project_name" not in columns:
            connection.execute("ALTER TABLE audit_events ADD COLUMN project_name TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS audit_events_project_timestamp_idx "
                "ON audit_events(project_name, timestamp DESC)"
            )
            connection.commit()
        return connection
    except sqlite3.Error:
        return None


def read_events(
    database_file: Path | None = None,
    *,
    project_name: str | None = None,
    profile_name: str | None = None,
    event_type: str | None = None,
    status: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    """Return one bounded, filterable SQLite page in timestamp order."""
    connection = _read_connection(database_file or database_path(__file__))
    if connection is None:
        return {"events": [], "total": 0}
    clauses: list[str] = []
    values: list[Any] = []
    for column, value in (("project_name", project_name), ("profile_name", profile_name),
                          ("event_type", event_type), ("status", status)):
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
            SELECT id, timestamp, project_name, profile_name, session_id, task_id, turn_id,
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


def audit_summary(database_file: Path | None = None) -> dict[str, Any]:
    """Return privacy-safe aggregate counts for the agent-audit summary."""
    connection = _read_connection(database_file or database_path(__file__))
    empty = {"total_events": 0, "projects": {}, "profiles": {}, "event_types": {}, "statuses": {}}
    if connection is None:
        return empty
    try:
        rows = connection.execute(
            "SELECT project_name, profile_name, event_type, status FROM audit_events"
        ).fetchall()
        return {
            "total_events": len(rows),
            "projects": dict(sorted(Counter(row["project_name"] or "unknown" for row in rows).items())),
            "profiles": dict(sorted(Counter(row["profile_name"] or "unknown" for row in rows).items())),
            "event_types": dict(sorted(Counter(row["event_type"] for row in rows).items())),
            "statuses": dict(sorted(Counter(row["status"] for row in rows if row["status"]).items())),
        }
    except sqlite3.Error:
        return empty
    finally:
        connection.close()


@router.get("/summary")
def summary() -> dict[str, Any]:
    return audit_summary()


@router.get("/events")
def events(
    project_name: str | None = None,
    profile_name: str | None = None,
    event_type: str | None = None,
    status: str | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=_MAX_LIMIT),
) -> dict[str, Any]:
    return read_events(
        project_name=project_name,
        profile_name=profile_name,
        event_type=event_type,
        status=status,
        offset=offset,
        limit=limit,
    )
