"""Passive Kanban event router for an existing PM workflow session."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any


_STATE_SCHEMA = "kanban-workflow-controller-state-v1"
_WAKE_SCHEMA = "kanban-workflow-wakeup-v1"
_DECISION_SCHEMA = "policy-decision-card-v1"
_RUN_WORKFLOW_MARKER = 'The user has invoked the "run-workflow" skill'
_CONTEXT: Any = None


def _state_path() -> Path:
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / "plugin-data" / "kanban-workflow-controller" / "state.db"


def _connect_state() -> sqlite3.Connection:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS binding ("
        "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
        "schema TEXT NOT NULL, board TEXT NOT NULL, coordinator_session_key TEXT NOT NULL, "
        "last_event_id INTEGER NOT NULL, bound_at INTEGER NOT NULL)"
    )
    return conn


def _latest_event_id(board: str) -> int:
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect(board=board)
    try:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) AS value FROM task_events").fetchone()
        return int(row["value"] if row else 0)
    finally:
        conn.close()


def _bind_state(board: str, coordinator_session_key: str, baseline_event_id: int) -> None:
    conn = _connect_state()
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT board, coordinator_session_key, last_event_id FROM binding WHERE singleton = 1"
        ).fetchone()
        cursor = int(baseline_event_id)
        if current is not None and current["board"] == board:
            if current["coordinator_session_key"] == coordinator_session_key:
                cursor = int(current["last_event_id"])
            else:
                cursor = max(cursor, int(current["last_event_id"]))
        conn.execute(
            "INSERT INTO binding(singleton, schema, board, coordinator_session_key, last_event_id, bound_at) "
            "VALUES(1, ?, ?, ?, ?, ?) "
            "ON CONFLICT(singleton) DO UPDATE SET schema=excluded.schema, board=excluded.board, "
            "coordinator_session_key=excluded.coordinator_session_key, "
            "last_event_id=excluded.last_event_id, bound_at=excluded.bound_at",
            (_STATE_SCHEMA, board, coordinator_session_key, cursor, int(time.time())),
        )
        conn.commit()
    except (OSError, sqlite3.Error):
        conn.rollback()
        raise
    finally:
        conn.close()


def _read_state() -> dict[str, Any] | None:
    try:
        with closing(_connect_state()) as conn:
            row = conn.execute(
                "SELECT schema, board, coordinator_session_key, last_event_id, bound_at "
                "FROM binding WHERE singleton = 1"
            ).fetchone()
    except (OSError, sqlite3.Error):
        return None
    if row is None or row["schema"] != _STATE_SCHEMA:
        return None
    if not str(row["board"] or "").strip():
        return None
    if not str(row["coordinator_session_key"] or "").strip():
        return None
    return dict(row)


def _process_event(
    state: dict[str, Any], event: dict[str, Any], actionable: bool, wake_message: str | None
) -> bool:
    """Serialize cursor advancement and delivery against concurrent ticks/rebinding."""
    conn = _connect_state()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT schema, board, coordinator_session_key, last_event_id "
            "FROM binding WHERE singleton = 1"
        ).fetchone()
        if (
            row is None
            or row["schema"] != _STATE_SCHEMA
            or row["board"] != state["board"]
            or row["coordinator_session_key"] != state["coordinator_session_key"]
            or int(row["last_event_id"]) != int(state["last_event_id"])
        ):
            conn.rollback()
            return False
        if actionable:
            accepted = _CONTEXT.inject_message(
                str(wake_message or ""), session_key=row["coordinator_session_key"]
            )
            if not accepted:
                conn.rollback()
                return False
        conn.execute(
            "UPDATE binding SET last_event_id = ? WHERE singleton = 1",
            (int(event["id"]),),
        )
        conn.commit()
        state["last_event_id"] = int(event["id"])
        return True
    except (OSError, sqlite3.Error):
        conn.rollback()
        return False
    finally:
        conn.close()


def _read_events(board: str, after_id: int) -> list[dict[str, Any]]:
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect(board=board)
    try:
        rows = conn.execute(
            "SELECT e.id, e.task_id, e.kind, e.payload, e.run_id, "
            "t.status, t.assignee, t.body, r.profile AS event_profile "
            "FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id "
            "LEFT JOIN task_runs r ON r.id = e.run_id "
            "WHERE e.id > ? ORDER BY e.id ASC LIMIT 200",
            (int(after_id),),
        ).fetchall()
    finally:
        conn.close()
    events: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except (json.JSONDecodeError, TypeError, ValueError):
            payload = {}
        try:
            task_body = json.loads(row["body"]) if row["body"] else {}
        except (json.JSONDecodeError, TypeError, ValueError):
            task_body = {}
        events.append(
            {
                "id": int(row["id"]),
                "task_id": str(row["task_id"]),
                "kind": str(row["kind"]),
                "run_id": row["run_id"],
                "event_profile": row["event_profile"],
                "payload": payload if isinstance(payload, dict) else {},
                "status": row["status"],
                "assignee": row["assignee"],
                "task_body": task_body if isinstance(task_body, dict) else {},
            }
        )
    return events


def _is_run_workflow_invocation(user_message: Any) -> bool:
    text = str(user_message or "").lstrip()
    return bool(re.match(r"^/run-workflow(?:\s|$)", text)) or text.startswith(
        f"[IMPORTANT: {_RUN_WORKFLOW_MARKER}."
    )


def _current_session_key() -> str:
    try:
        from gateway.session_context import get_session_env

        return str(get_session_env("HERMES_SESSION_KEY", "") or "").strip()
    except ImportError:
        return ""


def _current_session_profile() -> str:
    try:
        from gateway.session_context import get_session_env

        return str(get_session_env("HERMES_SESSION_PROFILE", "") or "").strip()
    except ImportError:
        return ""


def _on_pre_llm_call(**kwargs: Any) -> None:
    if _CONTEXT is None or not _is_run_workflow_invocation(kwargs.get("user_message")):
        return
    board = str(_CONTEXT.get_config("board", "") or "").strip()
    coordinator_profile = str(
        _CONTEXT.get_config("coordinator_profile", "project-manager") or "project-manager"
    ).strip()
    session_key = _current_session_key()
    if not board or not session_key or _current_session_profile() != coordinator_profile:
        return
    current = _read_state()
    if kwargs.get("parent_session_id") and (
        current is None or current.get("coordinator_session_key") != session_key
    ):
        return
    _bind_state(board, session_key, _latest_event_id(board))


def _is_actionable(
    event: dict[str, Any], coordinator_profile: str, decision_comment_author: str
) -> bool:
    kind = event.get("kind")
    if kind in {"blocked", "completed"}:
        event_profile = str(event.get("event_profile") or "").strip()
        return bool(event_profile) and event_profile != coordinator_profile
    if kind != "commented":
        return False
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    task_body = event.get("task_body") if isinstance(event.get("task_body"), dict) else {}
    author = str(payload.get("author") or "").strip()
    return (
        event.get("status") == "blocked"
        and event.get("assignee") == coordinator_profile
        and task_body.get("schema") == _DECISION_SCHEMA
        and author == decision_comment_author
    )


def _wake_message(board: str, event: dict[str, Any]) -> str:
    return json.dumps(
        {
            "schema": _WAKE_SCHEMA,
            "board": board,
            "source_task_id": event["task_id"],
            "source_event_id": event["id"],
            "event_kind": event["kind"],
            "instruction": (
                "Load run-workflow, then read back the source task, runs, comments, events, "
                "and current board state before choosing the next transition."
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _on_dispatch_tick(**kwargs: Any) -> None:
    if kwargs.get("dry_run") is True or _CONTEXT is None:
        return
    state = _read_state()
    if state is None:
        return
    board = str(kwargs.get("board") or "").strip()
    if not board or board != state["board"]:
        return
    coordinator_profile = str(
        _CONTEXT.get_config("coordinator_profile", "project-manager") or "project-manager"
    ).strip()
    decision_comment_author = str(
        _CONTEXT.get_config("decision_comment_author", "dashboard") or "dashboard"
    ).strip()
    for event in _read_events(board, int(state["last_event_id"])):
        actionable = _is_actionable(event, coordinator_profile, decision_comment_author)
        wake_message = _wake_message(board, event) if actionable else None
        if not _process_event(state, event, actionable, wake_message):
            break


def register(ctx: Any) -> None:
    global _CONTEXT
    _CONTEXT = ctx
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("on_kanban_dispatch_tick", _on_dispatch_tick)
