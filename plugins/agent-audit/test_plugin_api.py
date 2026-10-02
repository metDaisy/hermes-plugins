"""Regression tests for the read-only SQLite agent-audit query backend."""

from __future__ import annotations

import importlib.util
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from audit_storage import database_path


_API = Path(__file__).parent / "dashboard" / "plugin_api.py"
_SPEC = importlib.util.spec_from_file_location("agent_audit_plugin_api", _API)
assert _SPEC and _SPEC.loader
_API_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_API_MODULE)
_NOW = datetime.now(timezone.utc).replace(microsecond=0)
_OLDER_EVENT = (_NOW - timedelta(minutes=3)).isoformat()
_NEWER_EVENT = (_NOW - timedelta(minutes=2)).isoformat()
_FAILED_EVENT = (_NOW - timedelta(minutes=1)).isoformat()


def _database_with_events(path: Path) -> Path:
    _API_MODULE.initialize_database(path)
    connection = sqlite3.connect(path)
    try:
        connection.executemany(
            """
            INSERT INTO audit_events (
                timestamp, project_name, profile_name, session_id, task_id, turn_id,
                event_type, status, generation, rule_id, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    _OLDER_EVENT,
                    "hermes-plugins",
                    "main",
                    "session-main",
                    None,
                    None,
                    "tool_call",
                    "success",
                    None,
                    None,
                    '{"schema_version":1,"tool":"read_file","paths":["docs/index.md"],"duration_ms":42,"model":"gpt-5.6-sol","model_provider":"openai-codex","model_kind":"cloud","reasoning_effort":"medium"}',
                ),
                (
                    _NEWER_EVENT,
                    "e-commerce-clone-coding",
                    "project-manager",
                    "session-pm",
                    None,
                    None,
                    "validation_result",
                    "passed",
                    3,
                    "TEST-JAVA-001",
                    '{"rules":["TEST-JAVA-001"]}',
                ),
            ],
        )
        connection.commit()
    finally:
        connection.close()
    return path


def test_events_filter_by_profile_and_preserve_timestamp_order(tmp_path: Path) -> None:
    database_path = _database_with_events(tmp_path / "audit.db")

    payload = _API_MODULE.read_events(database_path, profile_name="project-manager")

    assert payload["total"] == 1
    event = payload["events"][0]
    assert event["timestamp"] == _NEWER_EVENT
    assert event["event"] == "validation_result"
    assert event["profile_name"] == "project-manager"
    assert event["session_id"] == "session-pm"
    assert event["status"] == "passed"
    assert event["generation"] == 3
    assert event["rule_id"] == "TEST-JAVA-001"
    assert event["rules"] == ["TEST-JAVA-001"]
    assert event["project_name"] == "e-commerce-clone-coding"


def test_events_filter_by_project(tmp_path: Path) -> None:
    database_path = _database_with_events(tmp_path / "audit.db")

    payload = _API_MODULE.read_events(database_path, project_name="hermes-plugins")

    assert payload["total"] == 1
    assert payload["events"][0]["scope"]["project"] == "hermes-plugins"


def test_failed_terminal_projection_exposes_only_compact_failure_evidence(tmp_path: Path) -> None:
    database = _database_with_events(tmp_path / "audit.db")
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """
            INSERT INTO audit_events (
                timestamp, project_name, profile_name, session_id,
                event_type, status, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _FAILED_EVENT,
                "hermes-plugins",
                "main",
                "session-main",
                "tool_call",
                "error",
                '{"tool":"terminal","command":"npm test","exit_code":1,'
                '"failure_type":"nonzero_exit","failure_summary":"3 tests failed"}',
            ),
        )
        connection.commit()
    finally:
        connection.close()

    event = _API_MODULE.read_events(database, event_type="tool_call", status="error")["events"][0]

    assert event["activity"]["command"] == "npm test"
    assert event["outcome"] == {
        "state": "failed",
        "exit_code": 1,
        "failure_type": "nonzero_exit",
        "failure_summary": "3 tests failed",
    }
    assert "result" not in event


def test_events_accept_multiple_values_for_every_filter(tmp_path: Path) -> None:
    database = _database_with_events(tmp_path / "audit.db")

    payload = _API_MODULE.read_events(
        database,
        project_name="hermes-plugins,e-commerce-clone-coding",
        profile_name="main,project-manager",
        session_id="session-main,session-pm",
        event_type="tool_call,validation_result",
        status="success,passed",
    )

    assert payload["total"] == 2


def test_events_support_offset_and_limit_pagination(tmp_path: Path) -> None:
    database_path = _database_with_events(tmp_path / "audit.db")

    first_page = _API_MODULE.read_events(database_path, offset=0, limit=1)
    second_page = _API_MODULE.read_events(database_path, offset=1, limit=1)

    assert first_page["total"] == 2
    assert len(first_page["events"]) == 1
    assert first_page["events"][0]["event"] == "validation_result"
    assert second_page["total"] == 2
    assert len(second_page["events"]) == 1
    assert second_page["events"][0]["event"] == "tool_call"


def test_events_project_user_facing_activity_and_model_schema(tmp_path: Path) -> None:
    database_path = _database_with_events(tmp_path / "audit.db")

    payload = _API_MODULE.read_events(database_path, profile_name="main")

    event = payload["events"][0]
    assert event["schema_version"] == 2
    assert event["id"] == 1
    assert event["kind"] == "tool_call"
    assert event["importance"] == "info"
    assert event["actor"] == {"profile": "main"}
    assert event["model"] == {
        "name": "gpt-5.6-sol",
        "provider": "openai-codex",
        "kind": "cloud",
        "reasoning_effort": "medium",
    }
    assert event["activity"] == {
        "code": "filesystem.read",
        "subject_type": "project_file",
        "tool": "read_file",
        "operation": "read",
    }
    assert event["outcome"] == {"state": "succeeded", "duration_ms": 42}
    assert event["scope"] == {"project": "hermes-plugins", "paths": ["docs/index.md"]}
    assert event["explanation"] == {
        "code": "tool.read_file.succeeded",
        "params": {"path_count": 1},
    }
    assert event["correlation"] == {"session_id": "session-main"}
    assert event["evidence"] == {
        "event_type": "tool_call",
        "storage_schema_version": 1,
        "metadata": {},
    }
    assert event["privacy"] == {"sanitized": True, "raw_content_stored": False}
    # Legacy flat fields remain for one compatibility release.
    assert event["tool"] == "read_file"
    assert event["paths"] == ["docs/index.md"]


def test_event_projection_resolves_session_title_from_profile_state(tmp_path: Path) -> None:
    database = _database_with_events(tmp_path / "audit.db")
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE audit_events SET project_name = 'IdeaProjects' WHERE session_id = 'session-main'"
        )
        connection.commit()
    finally:
        connection.close()
    profile_dir = tmp_path / "main"
    profile_dir.mkdir()
    state = sqlite3.connect(profile_dir / "state.db")
    try:
        state.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, model_config TEXT, "
            "cwd TEXT, git_repo_root TEXT)"
        )
        state.execute(
            "INSERT INTO sessions (id, title, model_config, cwd, git_repo_root) VALUES (?, ?, ?, ?, ?)",
            (
                "session-main",
                "Agent Audit UI 개선",
                '{"reasoning_config":{"effort":"high"}}',
                "C:/Users/lee/IdeaProjects/hermes-plugins",
                "C:/Users/lee/IdeaProjects/hermes-plugins",
            ),
        )
        state.commit()
    finally:
        state.close()

    event = _API_MODULE.read_events(database, profile_name="main", session_id="session-main")["events"][0]

    assert event["session"] == {"id": "session-main", "title": "Agent Audit UI 개선"}
    assert event["model"]["reasoning_effort"] == "medium"
    assert event["project_name"] == "hermes-plugins"
    assert event["scope"]["project"] == "hermes-plugins"

    sessions = _API_MODULE.audit_sessions(database, project_name="hermes-plugins")
    assert sessions == {
        "sessions": [
            {
                "id": "session-main",
                "title": "Agent Audit UI 개선",
                "project": "hermes-plugins",
                "profile": "main",
                "count": 1,
                "last_activity": _OLDER_EVENT,
            }
        ]
    }


def test_common_agent_tools_are_grouped_into_user_facing_activities(tmp_path: Path) -> None:
    cases = {
        "terminal": "system.command",
        "computer_use": "desktop.interact",
        "browser_exec": "web.interact",
        "mcp__semble_mcp__search": "code.discovery",
        "mcp__codebase_memory_mcp__search_graph": "code.discovery",
        "mcp__codebase_memory_mcp__index_repository": "code.index",
        "skill_view": "skill.inspect",
        "todo_list": "workflow.plan",
    }

    for tool, code in cases.items():
        assert _API_MODULE._activity({"event": "tool_call", "tool": tool})["code"] == code

    assert _API_MODULE._activity(
        {"event": "tool_call", "tool": "skill_view", "skill": "frontend-design"}
    )["skill"] == "frontend-design"


def test_terminal_command_is_exposed_only_from_the_sanitized_allowlist(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"
    _API_MODULE.initialize_database(database)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """
            INSERT INTO audit_events (
                timestamp, project_name, profile_name, session_id, event_type, status, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-10-02T08:49:37+00:00",
                "e-commerce-clone-coding",
                "main",
                "session-terminal",
                "tool_call",
                "success",
                '{"tool":"terminal","command":"npm test && git status --short"}',
            ),
        )
        connection.commit()
    finally:
        connection.close()

    event = _API_MODULE.read_events(database)["events"][0]

    assert event["activity"]["command"] == "npm test && git status --short"
    assert event["command"] == "npm test && git status --short"


def test_summary_groups_sqlite_events_by_profile(tmp_path: Path) -> None:
    database_path = _database_with_events(tmp_path / "audit.db")

    assert _API_MODULE.audit_summary(database_path) == {
        "total_events": 2,
        "projects": {"e-commerce-clone-coding": 1, "hermes-plugins": 1},
        "profiles": {"main": 1, "project-manager": 1},
        "event_types": {"tool_call": 1, "validation_result": 1},
        "statuses": {"passed": 1, "success": 1},
    }


def test_project_insights_explain_activity_outcomes_and_tools(tmp_path: Path) -> None:
    database = _database_with_events(tmp_path / "audit.db")

    insights = _API_MODULE.audit_insights(database, project_name="hermes-plugins")

    assert insights["scope"] == {"projects": ["hermes-plugins"], "sessions": ["session-main"]}
    assert insights["totals"] == {
        "events": 1,
        "sessions": 1,
        "succeeded": 1,
        "failed": 0,
        "interrupted": 0,
        "success_rate": 100,
    }
    assert insights["activities"] == [{"code": "filesystem.read", "count": 1}]
    assert insights["tools"] == [{"name": "read_file", "count": 1}]
    assert insights["skills"] == []
    assert insights["validators"] == []
    assert insights["files"] == {"searched": 0, "read": 1, "modified": 0, "unique": 1}
    assert insights["recent_activity"] == _OLDER_EVENT


def test_project_insights_accept_session_and_profile_filters(tmp_path: Path) -> None:
    database = _database_with_events(tmp_path / "audit.db")

    insights = _API_MODULE.audit_insights(
        database,
        project_name="e-commerce-clone-coding",
        session_id="session-pm",
        profile_name="project-manager",
    )

    assert insights["totals"]["events"] == 1
    assert insights["totals"]["sessions"] == 1
    assert insights["activities"] == [{"code": "validation.result", "count": 1}]
    assert insights["validators"] == []


def test_default_database_path_is_resolved_at_request_time(tmp_path: Path) -> None:
    first = _database_with_events(tmp_path / "first.db")
    second = _database_with_events(tmp_path / "second.db")
    original = _API_MODULE.database_path
    try:
        _API_MODULE.database_path = lambda _source: first
        assert _API_MODULE.read_events()["total"] == 2

        _API_MODULE.database_path = lambda _source: second
        assert _API_MODULE.audit_summary()["total_events"] == 2
    finally:
        _API_MODULE.database_path = original


def test_read_connection_migrates_existing_database_for_project_filter(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE audit_events (id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, "
            "profile_name TEXT, session_id TEXT, task_id TEXT, turn_id TEXT, event_type TEXT NOT NULL, "
            "status TEXT, generation INTEGER, rule_id TEXT, payload_json TEXT NOT NULL)"
        )
        connection.commit()
    finally:
        connection.close()

    assert _API_MODULE.read_events(database, project_name="hermes-plugins") == {"events": [], "total": 0}
    connection = sqlite3.connect(database)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(audit_events)")}
    finally:
        connection.close()
    assert "project_name" in columns


def test_installed_surfaces_share_profile_database_path(tmp_path: Path) -> None:
    profile_home = tmp_path / "profiles" / "main"
    module_file = profile_home / "plugins" / "agent-audit" / "dashboard" / "plugin_api.py"
    previous = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(profile_home)
    try:
        assert database_path(module_file) == tmp_path / "profiles" / ".hermes" / "audit.db"
    finally:
        if previous is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = previous


if __name__ == "__main__":
    for name, test in sorted(globals().items()):
        if name.startswith("test_"):
            with TemporaryDirectory() as directory:
                test(Path(directory))
    print("agent-audit SQLite dashboard tests: passed")
