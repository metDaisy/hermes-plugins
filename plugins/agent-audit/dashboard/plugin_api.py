"""Read-only API for privacy-safe SQLite agent-audit events."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query

try:
    from ..audit_storage import apply_retention
    from ..audit_storage import database_path
except ImportError:
    import sys

    plugin_root = str(Path(__file__).resolve().parents[1])
    if plugin_root not in sys.path:
        sys.path.insert(0, plugin_root)
    from audit_storage import apply_retention, database_path

router = APIRouter()

_MAX_LIMIT = 200
_SAFE_FIELDS = {
    "schema_version", "timestamp", "event", "project_name", "profile_name", "session_id", "task_id",
    "turn_id", "action", "skill", "command", "exit_code", "failure_type", "failure_summary",
    "provenance", "use_count", "reused", "tool",
    "status", "duration_ms", "paths", "provider", "operation", "category", "reason",
    "rule_id", "expected_providers", "observed_providers", "trigger", "required_rules",
    "changed_paths", "generation", "validator", "rules", "attempt", "validation_status",
    "rule_statuses", "missing_rules", "failed_rules", "unresolved_mutation", "completed",
    "failed", "interrupted", "turn_exit_reason",
    "model", "model_provider", "model_kind", "reasoning_effort",
}

_PROMOTED_FIELDS = {
    "schema_version", "timestamp", "event", "project_name", "profile_name", "session_id", "task_id",
    "turn_id", "status", "duration_ms", "paths", "provider", "operation", "tool",
    "skill", "command", "exit_code", "failure_type", "failure_summary", "validator", "rules",
    "required_rules", "rule_id", "generation", "action",
    "category", "reason", "validation_status", "model", "model_provider", "model_kind",
    "reasoning_effort",
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
            CREATE TABLE IF NOT EXISTS audit_daily_rollups (
                date TEXT NOT NULL,
                project_name TEXT NOT NULL,
                profile_name TEXT NOT NULL,
                event_count INTEGER NOT NULL,
                succeeded_count INTEGER NOT NULL,
                failed_count INTEGER NOT NULL,
                session_count INTEGER NOT NULL,
                PRIMARY KEY (date, project_name, profile_name)
            );
            CREATE TABLE IF NOT EXISTS audit_retention_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
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
            {"code": code, "subject_type": subject_type, "tool": tool,
             "skill": event.get("skill"), "command": event.get("command"),
             "operation": operation}
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
    exit_code = event.get("exit_code")
    has_failure_evidence = bool(event.get("failure_type")) or (
        isinstance(exit_code, int) and exit_code != 0
    )
    state = "failed" if has_failure_evidence else _outcome_state(
        event.get("status") or event.get("validation_status") or event.get("action")
    )
    model = _compact(
        {"name": event.get("model"), "provider": event.get("model_provider"),
         "kind": event.get("model_kind"), "reasoning_effort": event.get("reasoning_effort")}
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
                              "action": event.get("action"), "exit_code": event.get("exit_code"),
                              "failure_type": event.get("failure_type"),
                              "failure_summary": event.get("failure_summary")}),
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
        apply_retention(connection)
        return connection
    except sqlite3.Error:
        return None


def _filter_values(value: str | list[str] | tuple[str, ...] | None) -> list[str]:
    values = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def _profile_root(database_file: Path) -> Path:
    if database_file.parent.name == ".hermes":
        return database_file.parent.parent
    return database_file.parent


def _path_name(value: Any) -> str | None:
    normalized = str(value or "").replace("\\", "/").rstrip("/")
    return normalized.rsplit("/", 1)[-1] if normalized else None


def _session_details(
    database_file: Path, grouped: dict[str, set[str]]
) -> dict[tuple[str, str], dict[str, Any]]:
    details: dict[tuple[str, str], dict[str, Any]] = {}
    root = _profile_root(database_file)
    for profile, session_ids in grouped.items():
        state_file = root / profile / "state.db"
        if not state_file.is_file():
            continue
        state: sqlite3.Connection | None = None
        try:
            state = sqlite3.connect(state_file, timeout=1)
            state.row_factory = sqlite3.Row
            columns = {row[1] for row in state.execute("PRAGMA table_info(sessions)")}
            title_expression = (
                "COALESCE(NULLIF(title, ''), NULLIF(display_name, ''))"
                if "title" in columns and "display_name" in columns
                else ("title" if "title" in columns else ("display_name" if "display_name" in columns else "NULL"))
            )
            model_expression = "model_config" if "model_config" in columns else "NULL"
            project_expression = (
                "COALESCE(NULLIF(git_repo_root, ''), NULLIF(cwd, ''))"
                if "git_repo_root" in columns and "cwd" in columns
                else ("git_repo_root" if "git_repo_root" in columns else ("cwd" if "cwd" in columns else "NULL"))
            )
            placeholders = ",".join("?" for _ in session_ids)
            rows = state.execute(
                f"SELECT id, {title_expression} AS session_title, {model_expression} AS model_config, "
                f"{project_expression} AS project_path "
                f"FROM sessions WHERE id IN ({placeholders})",
                list(session_ids),
            ).fetchall()
            for row in rows:
                effort = None
                try:
                    config = json.loads(row["model_config"] or "{}")
                    reasoning = config.get("reasoning_config") if isinstance(config, dict) else None
                    if isinstance(reasoning, dict):
                        effort = reasoning.get("effort") or ("없음" if reasoning.get("enabled") is False else None)
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
                session_id = str(row["id"])
                details[(profile, session_id)] = _compact(
                    {"title": row["session_title"], "effort": effort,
                     "project": _path_name(row["project_path"])}
                )
        except sqlite3.Error:
            continue
        finally:
            if state is not None:
                state.close()
    return details


def _synchronize_project_names(connection: sqlite3.Connection, database_file: Path) -> None:
    rows = connection.execute(
        "SELECT DISTINCT profile_name, session_id FROM audit_events "
        "WHERE profile_name IS NOT NULL AND session_id IS NOT NULL AND session_id <> ''"
    ).fetchall()
    grouped: dict[str, set[str]] = {}
    for row in rows:
        grouped.setdefault(str(row["profile_name"]), set()).add(str(row["session_id"]))
    details = _session_details(database_file, grouped)
    changed = False
    for (profile, session_id), detail in details.items():
        project = detail.get("project")
        if not project:
            continue
        cursor = connection.execute(
            "UPDATE audit_events SET project_name = ? "
            "WHERE profile_name = ? AND session_id = ? AND COALESCE(project_name, '') <> ?",
            (project, profile, session_id, project),
        )
        changed = changed or cursor.rowcount > 0
    if changed:
        connection.commit()


def _enrich_sessions(database_file: Path, events: list[dict[str, Any]]) -> None:
    grouped: dict[str, set[str]] = {}
    for event in events:
        profile = str(event.get("actor", {}).get("profile") or "").strip()
        session_id = str(event.get("correlation", {}).get("session_id") or "").strip()
        if profile and session_id:
            grouped.setdefault(profile, set()).add(session_id)
    details = _session_details(database_file, grouped)
    for event in events:
        profile = str(event.get("actor", {}).get("profile") or "")
        session_id = str(event.get("correlation", {}).get("session_id") or "")
        detail = details.get((profile, session_id))
        if not detail:
            continue
        event["session"] = _compact({"id": session_id, "title": detail.get("title")})
        if detail.get("project"):
            event["project_name"] = detail["project"]
            event.setdefault("scope", {})["project"] = detail["project"]
        if detail.get("effort") and event.get("model"):
            event["model"].setdefault("reasoning_effort", detail["effort"])


def read_events(
    database_file: Path | None = None,
    *,
    project_name: str | list[str] | None = None,
    profile_name: str | list[str] | None = None,
    session_id: str | list[str] | None = None,
    event_type: str | list[str] | None = None,
    status: str | list[str] | None = None,
    offset: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    """Return one bounded, filterable SQLite page in timestamp order."""
    database_file = database_file or database_path(__file__)
    connection = _read_connection(database_file)
    if connection is None:
        return {"events": [], "total": 0}
    clauses: list[str] = []
    values: list[Any] = []
    for column, value in (("project_name", project_name), ("profile_name", profile_name),
                          ("session_id", session_id),
                          ("event_type", event_type), ("status", status)):
        selected = _filter_values(value)
        if selected:
            clauses.append(f"{column} IN ({','.join('?' for _ in selected)})")
            values.extend(selected)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    bounded_offset = max(0, offset)
    bounded_limit = min(_MAX_LIMIT, max(1, limit))
    try:
        _synchronize_project_names(connection, database_file)
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
        events = [_event_projection(row) for row in rows]
        _enrich_sessions(database_file, events)
        return {"events": events, "total": total}
    except sqlite3.Error:
        return {"events": [], "total": 0}
    finally:
        connection.close()


def audit_summary(database_file: Path | None = None) -> dict[str, Any]:
    """Return privacy-safe aggregate counts for the agent-audit summary."""
    database_file = database_file or database_path(__file__)
    connection = _read_connection(database_file)
    empty = {"total_events": 0, "projects": {}, "profiles": {}, "event_types": {}, "statuses": {}}
    if connection is None:
        return empty
    try:
        _synchronize_project_names(connection, database_file)
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


def audit_sessions(
    database_file: Path | None = None,
    *,
    project_name: str | list[str] | None = None,
    profile_name: str | list[str] | None = None,
) -> dict[str, Any]:
    """Return session labels and counts for project/session navigation."""
    database_file = database_file or database_path(__file__)
    connection = _read_connection(database_file)
    if connection is None:
        return {"sessions": []}
    try:
        _synchronize_project_names(connection, database_file)
        clauses = ["session_id IS NOT NULL", "session_id <> ''"]
        values: list[Any] = []
        for column, value in (("project_name", project_name), ("profile_name", profile_name)):
            selected = _filter_values(value)
            if selected:
                clauses.append(f"{column} IN ({','.join('?' for _ in selected)})")
                values.extend(selected)
        rows = connection.execute(
            "SELECT project_name, profile_name, session_id, COUNT(*) AS event_count, "
            "MAX(timestamp) AS last_activity FROM audit_events WHERE "
            + " AND ".join(clauses)
            + " GROUP BY project_name, profile_name, session_id ORDER BY last_activity DESC",
            values,
        ).fetchall()
        grouped: dict[str, set[str]] = {}
        for row in rows:
            grouped.setdefault(str(row["profile_name"] or ""), set()).add(str(row["session_id"]))
        details = _session_details(database_file, grouped)
        return {
            "sessions": [
                {
                    "id": str(row["session_id"]),
                    "title": details.get(
                        (str(row["profile_name"] or ""), str(row["session_id"])), {}
                    ).get("title"),
                    "project": row["project_name"],
                    "profile": row["profile_name"],
                    "count": row["event_count"],
                    "last_activity": row["last_activity"],
                }
                for row in rows
            ]
        }
    except sqlite3.Error:
        return {"sessions": []}
    finally:
        connection.close()


def _ranked_counts(values: list[str], key: str, limit: int = 5) -> list[dict[str, Any]]:
    return [
        {key: name, "count": count}
        for name, count in sorted(Counter(values).items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def audit_insights(
    database_file: Path | None = None,
    *,
    project_name: str | list[str] | None = None,
    profile_name: str | list[str] | None = None,
    session_id: str | list[str] | None = None,
) -> dict[str, Any]:
    """Return privacy-safe facts that explain work patterns in the selected scope."""
    database_file = database_file or database_path(__file__)
    connection = _read_connection(database_file)
    empty = {
        "scope": {"projects": [], "sessions": []},
        "totals": {"events": 0, "sessions": 0, "succeeded": 0, "failed": 0,
                   "interrupted": 0, "success_rate": 0},
        "activities": [], "tools": [], "skills": [], "validators": [],
        "files": {"searched": 0, "read": 0, "modified": 0, "unique": 0},
        "recent_activity": None,
    }
    if connection is None:
        return empty
    try:
        _synchronize_project_names(connection, database_file)
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in (("project_name", project_name), ("profile_name", profile_name),
                              ("session_id", session_id)):
            selected = _filter_values(value)
            if selected:
                clauses.append(f"{column} IN ({','.join('?' for _ in selected)})")
                values.extend(selected)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = connection.execute(
            "SELECT id, timestamp, project_name, profile_name, session_id, task_id, turn_id, "
            "event_type, status, generation, rule_id, payload_json FROM audit_events"
            + where + " ORDER BY timestamp DESC, id DESC",
            values,
        ).fetchall()
        if not rows:
            return empty
        events = [_event_projection(row) for row in rows]
        states = [event["outcome"]["state"] for event in events]
        succeeded = sum(state == "succeeded" for state in states)
        failed = sum(state == "failed" for state in states)
        interrupted = sum(state == "interrupted" for state in states)
        decided = succeeded + failed
        file_sets = {"searched": set(), "read": set(), "modified": set()}
        activities: list[str] = []
        tools: list[str] = []
        skills: list[str] = []
        validators: list[str] = []
        for event in events:
            activity = event.get("activity", {})
            code = activity.get("code")
            if code:
                activities.append(str(code))
            if activity.get("tool"):
                tools.append(str(activity["tool"]))
            if activity.get("skill"):
                skills.append(str(activity["skill"]))
            if activity.get("validator"):
                validators.append(str(activity["validator"]))
            paths = event.get("scope", {}).get("paths") or []
            bucket = {"filesystem.search": "searched", "filesystem.read": "read",
                      "filesystem.modify": "modified"}.get(code)
            if bucket:
                file_sets[bucket].update(str(path) for path in paths)
        unique_files = set().union(*file_sets.values())
        projects = sorted({str(row["project_name"]) for row in rows if row["project_name"]})
        sessions = sorted({str(row["session_id"]) for row in rows if row["session_id"]})
        return {
            "scope": {"projects": projects, "sessions": sessions},
            "totals": {"events": len(events), "sessions": len(sessions), "succeeded": succeeded,
                       "failed": failed, "interrupted": interrupted,
                       "success_rate": round(succeeded * 100 / decided) if decided else 0},
            "activities": _ranked_counts(activities, "code"),
            "tools": _ranked_counts(tools, "name"),
            "skills": _ranked_counts(skills, "name"),
            "validators": _ranked_counts(validators, "name"),
            "files": {"searched": len(file_sets["searched"]), "read": len(file_sets["read"]),
                      "modified": len(file_sets["modified"]), "unique": len(unique_files)},
            "recent_activity": rows[0]["timestamp"],
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
    session_id: str | None = None,
    event_type: str | None = None,
    status: str | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=_MAX_LIMIT),
) -> dict[str, Any]:
    return read_events(
        project_name=project_name,
        profile_name=profile_name,
        session_id=session_id,
        event_type=event_type,
        status=status,
        offset=offset,
        limit=limit,
    )


@router.get("/sessions")
def sessions(
    project_name: str | None = None,
    profile_name: str | None = None,
) -> dict[str, Any]:
    return audit_sessions(project_name=project_name, profile_name=profile_name)


@router.get("/insights")
def insights(
    project_name: str | None = None,
    profile_name: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    return audit_insights(
        project_name=project_name,
        profile_name=profile_name,
        session_id=session_id,
    )
