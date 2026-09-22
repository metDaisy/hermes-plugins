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
                timestamp, profile_name, session_id, task_id, turn_id,
                event_type, status, generation, rule_id, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "2026-09-21T11:16:34+00:00",
                    "main",
                    "session-main",
                    None,
                    None,
                    "tool_call",
                    "success",
                    None,
                    None,
                    '{"tool":"read_file"}',
                ),
                (
                    "2026-09-21T11:18:54+00:00",
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

    assert payload == {
        "events": [
            {
                "timestamp": "2026-09-21T11:18:54+00:00",
                "event": "validation_result",
                "profile_name": "project-manager",
                "session_id": "session-pm",
                "status": "passed",
                "generation": 3,
                "rule_id": "TEST-JAVA-001",
                "rules": ["TEST-JAVA-001"],
            }
        ],
        "total": 1,
    }


def test_summary_groups_sqlite_events_by_profile(tmp_path: Path) -> None:
    database_path = _database_with_events(tmp_path / "audit.db")

    assert _API_MODULE.audit_summary(database_path) == {
        "total_events": 2,
        "profiles": {"main": 1, "project-manager": 1},
        "event_types": {"tool_call": 1, "validation_result": 1},
        "statuses": {"passed": 1, "success": 1},
    }


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
