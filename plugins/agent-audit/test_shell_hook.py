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
        "cwd": "C:/Users/leee/IdeaProjects/hermes-plugins",
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
            "project_name": "hermes-plugins",
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
            "cwd": "C:/Users/leee/IdeaProjects/hermes-plugins",
            "extra": {"status": "success", "duration_ms": 7},
        }
        with patch("sys.stdin", StringIO(json.dumps(payload))):
            assert adapter.main() == 0

        connection = sqlite3.connect(database)
        try:
            row = connection.execute(
                "SELECT project_name, profile_name, session_id, event_type, status FROM audit_events"
            ).fetchone()
        finally:
            connection.close()

    assert row == ("hermes-plugins", "main", "session-desktop", "tool_call", "success")


def test_pre_api_request_payload_records_model_context_for_following_tool() -> None:
    adapter = _load(_SHELL_HOOK, "agent_audit_shell_hook_model")
    with TemporaryDirectory() as directory:
        database = Path(directory) / "audit.db"
        adapter._AUDIT._db_path = lambda: database
        adapter._dispatch(
            {
                "hook_event_name": "pre_api_request",
                "session_id": "session-model",
                "profile": "main",
                "extra": {
                    "model": "gpt-5.6-sol",
                    "provider": "openai-codex",
                    "base_url": "https://api.openai.com/v1",
                    "turn_id": "turn-model",
                },
            }
        )
        adapter._dispatch(
            {
                "hook_event_name": "post_tool_call",
                "tool_name": "search_files",
                "tool_input": {"path": "desktop"},
                "session_id": "session-model",
                "profile": "main",
                "extra": {"status": "success", "turn_id": "turn-model"},
            }
        )

        connection = sqlite3.connect(database)
        try:
            payload = connection.execute(
                "SELECT payload_json FROM audit_events WHERE event_type = 'tool_call'"
            ).fetchone()[0]
        finally:
            connection.close()

    assert '"model":"gpt-5.6-sol"' in payload
    assert '"model_provider":"openai-codex"' in payload
    assert '"model_kind":"cloud"' in payload


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
    test_pre_api_request_payload_records_model_context_for_following_tool()
    test_cli_payload_is_not_double_recorded_by_shell_bridge()
    print("agent-audit shell-hook tests: passed")
