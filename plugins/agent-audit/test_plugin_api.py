"""Regression tests for the read-only SQLite agent-audit query backend."""

from __future__ import annotations

import importlib.util
import os
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from audit_storage import database_path


_API = Path(__file__).parent / "dashboard" / "plugin_api.py"
_SPEC = importlib.util.spec_from_file_location("agent_audit_plugin_api", _API)
assert _SPEC and _SPEC.loader
_API_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_API_MODULE)


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
                    "2026-09-21T11:16:34+00:00",
                    "hermes-plugins",
                    "main",
                    "session-main",
                    None,
                    None,
                    "tool_call",
                    "success",
                    None,
                    None,
                    '{"schema_version":1,"tool":"read_file","paths":["docs/index.md"],"duration_ms":42,"model":"gpt-5.6-sol","model_provider":"openai-codex","model_kind":"cloud"}',
                ),
                (
                    "2026-09-21T11:18:54+00:00",
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
    assert event["timestamp"] == "2026-09-21T11:18:54+00:00"
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


def test_summary_groups_sqlite_events_by_profile(tmp_path: Path) -> None:
    database_path = _database_with_events(tmp_path / "audit.db")

    assert _API_MODULE.audit_summary(database_path) == {
        "total_events": 2,
        "projects": {"e-commerce-clone-coding": 1, "hermes-plugins": 1},
        "profiles": {"main": 1, "project-manager": 1},
        "event_types": {"tool_call": 1, "validation_result": 1},
        "statuses": {"passed": 1, "success": 1},
    }


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
