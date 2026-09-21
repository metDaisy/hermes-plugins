"""Regression tests for the Desktop shell-hook bridge."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


_PLUGIN = Path(__file__).with_name("__init__.py")
_SHELL_HOOK = Path(__file__).with_name("shell_hook.py")


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_post_tool_payload_reaches_existing_writer() -> None:
    adapter = _load(_SHELL_HOOK, "agent_audit_shell_hook")
    audit = adapter._AUDIT
    events: list[dict] = []
    audit._write = lambda event, **fields: events.append({"event": event, **fields})

    payload = {
        "hook_event_name": "post_tool_call",
        "tool_name": "read_file",
        "tool_input": {"path": "docs/index.md"},
        "session_id": "session-desktop",
        "profile": "main",
        "extra": {
            "status": "success",
            "duration_ms": 12,
            "task_id": "task-1",
            "turn_id": "turn-1",
            "result": "safe result",
        },
    }

    adapter._dispatch(payload)

    assert events == [
        {
            "event": "tool_call",
            "tool": "read_file",
            "status": "success",
            "duration_ms": 12,
            "session_id": "session-desktop",
            "task_id": "task-1",
            "turn_id": "turn-1",
            "paths": ["docs/index.md"],
            "profile_name": "main",
        }
    ]


def test_adapter_writes_sqlite_event_from_shell_payload() -> None:
    adapter = _load(_SHELL_HOOK, "agent_audit_shell_hook_sqlite")
    with TemporaryDirectory() as directory:
        database = Path(directory) / "audit.db"
        adapter._AUDIT._db_path = lambda: database
        payload = {
            "hook_event_name": "post_tool_call",
            "tool_name": "read_file",
            "tool_input": {"path": "docs/index.md"},
            "session_id": "session-desktop",
            "profile": "main",
            "extra": {"status": "success", "duration_ms": 7},
        }
        with patch("sys.stdin", StringIO(json.dumps(payload))):
            assert adapter.main() == 0

        connection = sqlite3.connect(database)
        try:
            row = connection.execute(
                "SELECT profile_name, session_id, event_type, status FROM audit_events"
            ).fetchone()
        finally:
            connection.close()

    assert row == ("main", "session-desktop", "tool_call", "success")


def test_cli_payload_is_not_double_recorded_by_shell_bridge() -> None:
    adapter = _load(_SHELL_HOOK, "agent_audit_shell_hook_dedup")
    events: list[dict] = []
    adapter._AUDIT._write = lambda event, **fields: events.append({"event": event, **fields})

    adapter._dispatch(
        {
            "hook_event_name": "post_tool_call",
            "tool_name": "read_file",
            "tool_input": {"path": "docs/index.md"},
            "session_id": "session-cli",
            "profile": "main",
            "extra": {"platform": "cli", "status": "success"},
        }
    )

    assert events == []


if __name__ == "__main__":
    test_post_tool_payload_reaches_existing_writer()
    test_adapter_writes_sqlite_event_from_shell_payload()
    print("agent-audit shell-hook tests: passed")
