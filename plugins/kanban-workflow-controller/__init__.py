"""Passive Kanban event router for an existing PM workflow session."""

from __future__ import annotations

import json
import importlib.util
import re
import sqlite3
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

try:
    from .comment_service import create_comment_request as _create_comment_request
except ImportError:
    _service_path = Path(__file__).resolve().parent / "comment_service.py"
    _service_name = "_hermes_kanban_workflow_controller_comment_service"
    _service_module = sys.modules.get(_service_name)
    if _service_module is None:
        _service_spec = importlib.util.spec_from_file_location(_service_name, _service_path)
        if _service_spec is None or _service_spec.loader is None:
            raise ImportError(f"cannot load KWC comment service from {_service_path}")
        _service_module = importlib.util.module_from_spec(_service_spec)
        sys.modules[_service_name] = _service_module
        _service_spec.loader.exec_module(_service_module)
    elif Path(_service_module.__file__).resolve() != _service_path:
        raise ImportError("KWC comment service module name is already bound to another path")
    _create_comment_request = _service_module.create_comment_request

try:
    from .invocation_service import submit_pm_response as _submit_pm_response
except ImportError:
    _invocation_path = Path(__file__).resolve().parent / "invocation_service.py"
    _invocation_name = "_hermes_kanban_workflow_controller_invocation_service"
    _invocation_module = sys.modules.get(_invocation_name)
    if _invocation_module is None:
        _invocation_spec = importlib.util.spec_from_file_location(
            _invocation_name, _invocation_path
        )
        if _invocation_spec is None or _invocation_spec.loader is None:
            raise ImportError(f"cannot load KWC invocation service from {_invocation_path}")
        _invocation_module = importlib.util.module_from_spec(_invocation_spec)
        sys.modules[_invocation_name] = _invocation_module
        _invocation_spec.loader.exec_module(_invocation_module)
    elif Path(_invocation_module.__file__).resolve() != _invocation_path:
        raise ImportError(
            "KWC invocation service module name is already bound to another path"
        )
    _submit_pm_response = _invocation_module.submit_pm_response


_STATE_SCHEMA = "kanban-workflow-controller-state-v1"
_WAKE_SCHEMA = "kanban-workflow-wakeup-v1"
_COMMENT_WAKE_SCHEMA = "kanban-comment-wakeup-v1"
_DECISION_SCHEMA = "policy-decision-card-v1"
_IMPLEMENTATION_SCHEMAS = {
    "backend-implementation-card-v2",
    "backend-implementation-card-v3",
}
_REVIEW_SCHEMA = "aggregate-review-card-v3"
_IMPLEMENTATION_PROFILE = "coder"
_REVIEW_PROFILE = "reviewer"
_PROFILE_MENTION = re.compile(r"@\{([A-Za-z0-9][A-Za-z0-9._-]*)\}")
_RUN_WORKFLOW_MARKER = 'The user has invoked the "run-workflow" skill'
_CONTEXT: Any = None
_ACTIVITY_RETENTION_SECONDS = 30 * 24 * 60 * 60


def _now() -> int:
    return int(time.time())


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
    conn.execute(
        "CREATE TABLE IF NOT EXISTS runtime_heartbeat ("
        "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
        "board TEXT NOT NULL, last_dispatch_tick_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS activity_log ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL, "
        "board TEXT NOT NULL, action TEXT NOT NULL, outcome TEXT NOT NULL, "
        "reason TEXT NOT NULL, task_id TEXT, source_event_id INTEGER, event_kind TEXT)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kwc_activity_created_at "
        "ON activity_log(created_at DESC)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS coordinator_binding ("
        "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
        "schema TEXT NOT NULL, coordinator_session_key TEXT NOT NULL, bound_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS board_cursor ("
        "board TEXT PRIMARY KEY, last_event_id INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS board_heartbeat ("
        "board TEXT PRIMARY KEY, last_dispatch_tick_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS profile_session_binding ("
        "profile TEXT PRIMARY KEY, session_key TEXT NOT NULL, bound_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS comment_delivery ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, board TEXT NOT NULL, task_id TEXT NOT NULL, "
        "comment_id INTEGER NOT NULL, author TEXT NOT NULL, reply_to_comment_id INTEGER, "
        "route_requested INTEGER NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL, "
        "updated_at INTEGER NOT NULL, UNIQUE(board, comment_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS comment_delivery_target ("
        "delivery_id INTEGER NOT NULL, profile TEXT NOT NULL, status TEXT NOT NULL, "
        "updated_at INTEGER NOT NULL, PRIMARY KEY(delivery_id, profile), "
        "FOREIGN KEY(delivery_id) REFERENCES comment_delivery(id) ON DELETE CASCADE)"
    )
    legacy_binding = conn.execute(
        "SELECT board, coordinator_session_key, last_event_id, bound_at FROM binding WHERE singleton = 1"
    ).fetchone()
    if legacy_binding is not None:
        conn.execute(
            "INSERT OR IGNORE INTO coordinator_binding(singleton, schema, coordinator_session_key, bound_at) "
            "VALUES(1, ?, ?, ?)",
            (_STATE_SCHEMA, legacy_binding["coordinator_session_key"], legacy_binding["bound_at"]),
        )
        conn.execute(
            "INSERT OR IGNORE INTO board_cursor(board, last_event_id) VALUES(?, ?)",
            (legacy_binding["board"], legacy_binding["last_event_id"]),
        )
    legacy_heartbeat = conn.execute(
        "SELECT board, last_dispatch_tick_at FROM runtime_heartbeat WHERE singleton = 1"
    ).fetchone()
    if legacy_heartbeat is not None:
        conn.execute(
            "INSERT OR IGNORE INTO board_heartbeat(board, last_dispatch_tick_at) VALUES(?, ?)",
            (legacy_heartbeat["board"], legacy_heartbeat["last_dispatch_tick_at"]),
        )
    conn.commit()
    return conn


def _insert_activity(
    conn: sqlite3.Connection,
    *,
    board: str,
    action: str,
    outcome: str,
    reason: str,
    task_id: str | None = None,
    source_event_id: int | None = None,
    event_kind: str | None = None,
) -> None:
    now = _now()
    conn.execute(
        "INSERT INTO activity_log(created_at, board, action, outcome, reason, task_id, "
        "source_event_id, event_kind) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (now, board, action, outcome, reason, task_id, source_event_id, event_kind),
    )
    conn.execute("DELETE FROM activity_log WHERE created_at < ?", (now - _ACTIVITY_RETENTION_SECONDS,))


def _record_activity(**fields: Any) -> None:
    try:
        with closing(_connect_state()) as conn:
            _insert_activity(conn, **fields)
            conn.commit()
    except (OSError, sqlite3.Error):
        return


def _read_activity(limit: int = 100) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(int(limit), 200))
    try:
        with closing(_connect_state()) as conn:
            rows = conn.execute(
                "SELECT id, created_at, board, action, outcome, reason, task_id, "
                "source_event_id, event_kind FROM activity_log "
                "ORDER BY id DESC LIMIT ?",
                (bounded_limit,),
            ).fetchall()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return []
    return [dict(row) for row in rows]


def _bind_profile_session(profile: str, session_key: str) -> None:
    profile = str(profile or "").strip()
    session_key = str(session_key or "").strip()
    if not profile or not session_key:
        return
    with closing(_connect_state()) as conn:
        conn.execute(
            "INSERT INTO profile_session_binding(profile, session_key, bound_at) VALUES(?, ?, ?) "
            "ON CONFLICT(profile) DO UPDATE SET session_key=excluded.session_key, "
            "bound_at=excluded.bound_at",
            (profile, session_key, _now()),
        )
        conn.commit()


def _enqueue_comment_delivery(
    *,
    board: str,
    task_id: str,
    comment_id: int,
    author: str,
    reply_to_comment_id: int | None,
    route_requested: bool,
) -> int:
    now = _now()
    with closing(_connect_state()) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO comment_delivery("
            "board, task_id, comment_id, author, reply_to_comment_id, route_requested, "
            "status, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                str(board),
                str(task_id),
                int(comment_id),
                str(author),
                int(reply_to_comment_id) if reply_to_comment_id is not None else None,
                1 if route_requested else 0,
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT id FROM comment_delivery WHERE board = ? AND comment_id = ?",
            (str(board), int(comment_id)),
        ).fetchone()
        conn.commit()
        return int(row["id"])


def _read_comment_delivery(delivery_id: int) -> dict[str, Any] | None:
    with closing(_connect_state()) as conn:
        row = conn.execute("SELECT * FROM comment_delivery WHERE id = ?", (int(delivery_id),)).fetchone()
    return dict(row) if row is not None else None


def _read_comment_context(board: str, task_id: str, comment_id: int) -> dict[str, Any] | None:
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect(board=board)
    try:
        row = conn.execute(
            "SELECT c.body, t.assignee FROM task_comments c "
            "JOIN tasks t ON t.id = c.task_id WHERE c.id = ? AND c.task_id = ?",
            (int(comment_id), str(task_id)),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def _comment_targets(body: str, assignee: Any) -> list[str]:
    mentions = list(dict.fromkeys(match.group(1) for match in _PROFILE_MENTION.finditer(body or "")))
    if mentions:
        return mentions
    assignee_name = str(assignee or "").strip()
    return [assignee_name] if assignee_name else []


def _comment_wake_message(delivery: dict[str, Any], target_profile: str) -> str:
    return json.dumps(
        {
            "schema": _COMMENT_WAKE_SCHEMA,
            "board": delivery["board"],
            "source_task_id": delivery["task_id"],
            "source_comment_id": int(delivery["comment_id"]),
            "reply_to_comment_id": delivery["reply_to_comment_id"],
            "target_profile": target_profile,
            "instruction": (
                "Read the source task and canonical comment thread, answer the comment, then use "
                "kwc_reply with source_comment_id as reply_to_comment_id."
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _process_pending_comment_deliveries(board: str) -> None:
    if _CONTEXT is None:
        return
    conn = _connect_state()
    try:
        conn.execute("BEGIN IMMEDIATE")
        deliveries = conn.execute(
            "SELECT * FROM comment_delivery WHERE board = ? "
            "AND status IN ('pending', 'retry_pending') ORDER BY id ASC LIMIT 100",
            (str(board),),
        ).fetchall()
        for row in deliveries:
            delivery = dict(row)
            delivery_id = int(delivery["id"])
            if not bool(delivery["route_requested"]):
                conn.execute(
                    "UPDATE comment_delivery SET status='suppressed', updated_at=? WHERE id=?",
                    (_now(), delivery_id),
                )
                continue
            target_count = conn.execute(
                "SELECT COUNT(*) AS value FROM comment_delivery_target WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
            if int(target_count["value"] or 0) == 0:
                context = _read_comment_context(
                    str(delivery["board"]), str(delivery["task_id"]), int(delivery["comment_id"])
                )
                if context is None:
                    conn.execute(
                        "UPDATE comment_delivery SET status='retry_pending', updated_at=? WHERE id=?",
                        (_now(), delivery_id),
                    )
                    continue
                targets = _comment_targets(str(context.get("body") or ""), context.get("assignee"))
                if not targets:
                    conn.execute(
                        "UPDATE comment_delivery SET status='suppressed', updated_at=? WHERE id=?",
                        (_now(), delivery_id),
                    )
                    continue
                for profile in targets:
                    conn.execute(
                        "INSERT INTO comment_delivery_target(delivery_id, profile, status, updated_at) "
                        "VALUES(?, ?, 'pending', ?)",
                        (delivery_id, profile, _now()),
                    )
            pending_targets = conn.execute(
                "SELECT profile FROM comment_delivery_target WHERE delivery_id=? "
                "AND status != 'delivered' ORDER BY rowid ASC",
                (delivery_id,),
            ).fetchall()
            for target in pending_targets:
                profile = str(target["profile"])
                binding = conn.execute(
                    "SELECT session_key FROM profile_session_binding WHERE profile=?", (profile,)
                ).fetchone()
                if binding is None:
                    conn.execute(
                        "UPDATE comment_delivery_target SET status='retry_pending', updated_at=? "
                        "WHERE delivery_id=? AND profile=?",
                        (_now(), delivery_id, profile),
                    )
                    continue
                accepted = _CONTEXT.inject_message(
                    _comment_wake_message(delivery, profile), session_key=str(binding["session_key"])
                )
                conn.execute(
                    "UPDATE comment_delivery_target SET status=?, updated_at=? "
                    "WHERE delivery_id=? AND profile=?",
                    ("delivered" if accepted else "retry_pending", _now(), delivery_id, profile),
                )
            remaining = conn.execute(
                "SELECT COUNT(*) AS value FROM comment_delivery_target "
                "WHERE delivery_id=? AND status != 'delivered'",
                (delivery_id,),
            ).fetchone()
            status = "delivered" if int(remaining["value"] or 0) == 0 else "retry_pending"
            conn.execute(
                "UPDATE comment_delivery SET status=?, updated_at=? WHERE id=?",
                (status, _now(), delivery_id),
            )
        conn.commit()
    except (OSError, sqlite3.Error):
        conn.rollback()
    finally:
        conn.close()


def _mark_dispatch_heartbeat(board: str) -> None:
    with closing(_connect_state()) as conn:
        previous = conn.execute("SELECT board FROM board_heartbeat WHERE board = ?", (board,)).fetchone()
        conn.execute(
            "INSERT INTO board_heartbeat(board, last_dispatch_tick_at) VALUES(?, ?) "
            "ON CONFLICT(board) DO UPDATE SET last_dispatch_tick_at=excluded.last_dispatch_tick_at",
            (board, _now()),
        )
        if previous is None:
            _insert_activity(
                conn,
                board=board,
                action="board_monitoring",
                outcome="active",
                reason="configured_board_dispatch_tick",
            )
        conn.commit()


def _read_dispatch_heartbeat() -> dict[str, Any] | None:
    try:
        with closing(_connect_state()) as conn:
            row = conn.execute(
                "SELECT board, last_dispatch_tick_at FROM board_heartbeat "
                "ORDER BY last_dispatch_tick_at DESC LIMIT 1"
            ).fetchone()
    except (OSError, sqlite3.Error):
        return None
    return dict(row) if row is not None else None


def _latest_event_id(board: str) -> int:
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect(board=board)
    try:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) AS value FROM task_events").fetchone()
        return int(row["value"] if row else 0)
    finally:
        conn.close()


def _bind_state(
    board_or_session_key: str,
    coordinator_session_key: str | None = None,
    baseline_event_id: int | None = None,
) -> None:
    board = board_or_session_key if coordinator_session_key is not None else ""
    session_key = coordinator_session_key or board_or_session_key
    conn = _connect_state()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO coordinator_binding(singleton, schema, coordinator_session_key, bound_at) "
            "VALUES(1, ?, ?, ?) ON CONFLICT(singleton) DO UPDATE SET "
            "schema=excluded.schema, coordinator_session_key=excluded.coordinator_session_key, "
            "bound_at=excluded.bound_at",
            (_STATE_SCHEMA, session_key, _now()),
        )
        if board:
            existing = conn.execute(
                "SELECT last_event_id FROM board_cursor WHERE board = ?", (board,)
            ).fetchone()
            cursor = max(int(baseline_event_id or 0), int(existing["last_event_id"]) if existing else 0)
            conn.execute(
                "INSERT INTO board_cursor(board, last_event_id) VALUES(?, ?) "
                "ON CONFLICT(board) DO UPDATE SET last_event_id=excluded.last_event_id",
                (board, cursor),
            )
        _insert_activity(
            conn,
            board=board or "*",
            action="session_bound",
            outcome="accepted",
            reason="coordinator_run_workflow_invocation",
        )
        conn.commit()
    except (OSError, sqlite3.Error):
        conn.rollback()
        raise
    finally:
        conn.close()


def _read_state(board: str | None = None) -> dict[str, Any] | None:
    try:
        with closing(_connect_state()) as conn:
            row = conn.execute(
                "SELECT schema, coordinator_session_key, bound_at "
                "FROM coordinator_binding WHERE singleton = 1"
            ).fetchone()
            cursor = (
                conn.execute("SELECT last_event_id FROM board_cursor WHERE board = ?", (board,)).fetchone()
                if board
                else None
            )
    except (OSError, sqlite3.Error):
        return None
    if row is None or row["schema"] != _STATE_SCHEMA:
        return None
    if not str(row["coordinator_session_key"] or "").strip():
        return None
    result = dict(row)
    if board:
        if cursor is None:
            return None
        result["board"] = board
        result["last_event_id"] = int(cursor["last_event_id"])
    return result


def _ensure_board_cursor(board: str, baseline_event_id: int) -> None:
    with closing(_connect_state()) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO board_cursor(board, last_event_id) VALUES(?, ?)",
            (board, max(0, int(baseline_event_id))),
        )
        conn.commit()


def _process_event(
    state: dict[str, Any], event: dict[str, Any], actionable: bool, wake_message: str | None
) -> bool:
    """Serialize cursor advancement and delivery against concurrent ticks/rebinding."""
    conn = _connect_state()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT schema, coordinator_session_key FROM coordinator_binding WHERE singleton = 1"
        ).fetchone()
        cursor = conn.execute(
            "SELECT last_event_id FROM board_cursor WHERE board = ?", (state["board"],)
        ).fetchone()
        if (
            row is None
            or cursor is None
            or row["schema"] != _STATE_SCHEMA
            or row["coordinator_session_key"] != state["coordinator_session_key"]
            or int(cursor["last_event_id"]) != int(state["last_event_id"])
        ):
            conn.rollback()
            return False
        if actionable:
            accepted = _CONTEXT.inject_message(
                str(wake_message or ""), session_key=row["coordinator_session_key"]
            )
            if not accepted:
                _insert_activity(
                    conn,
                    board=str(state["board"]),
                    action="event_delivery",
                    outcome="retry_pending",
                    reason="session_injection_rejected",
                    task_id=str(event["task_id"]),
                    source_event_id=int(event["id"]),
                    event_kind=str(event["kind"]),
                )
                conn.commit()
                return False
        _insert_activity(
            conn,
            board=str(state["board"]),
            action="event_routed" if actionable else "event_ignored",
            outcome="accepted" if actionable else "filtered",
            reason="actionable_workflow_event" if actionable else "not_actionable",
            task_id=str(event["task_id"]),
            source_event_id=int(event["id"]),
            event_kind=str(event["kind"]),
        )
        conn.execute(
            "UPDATE board_cursor SET last_event_id = ? WHERE board = ?",
            (int(event["id"]), state["board"]),
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


def _pm_response_available() -> bool:
    if _CONTEXT is None:
        return False
    coordinator_profile = str(
        _CONTEXT.get_config("coordinator_profile", "project-manager") or "project-manager"
    ).strip()
    return _current_session_profile() == coordinator_profile


def _configured_comment_profiles() -> set[str]:
    raw = _CONTEXT.get_config(
        "comment_profiles", "project-manager,coder,reviewer"
    ) if _CONTEXT is not None else ""
    if isinstance(raw, (list, tuple, set)):
        values = raw
    else:
        values = str(raw or "").split(",")
    return {str(value).strip() for value in values if str(value).strip()}


def _on_pre_llm_call(**kwargs: Any) -> None:
    if _CONTEXT is None:
        return
    session_key = _current_session_key()
    session_profile = _current_session_profile()
    if (
        not kwargs.get("parent_session_id")
        and session_key
        and session_profile in _configured_comment_profiles()
    ):
        _bind_profile_session(session_profile, session_key)
    if not _is_run_workflow_invocation(kwargs.get("user_message")):
        return
    coordinator_profile = str(
        _CONTEXT.get_config("coordinator_profile", "project-manager") or "project-manager"
    ).strip()
    if not session_key or session_profile != coordinator_profile:
        return
    current = _read_state()
    if kwargs.get("parent_session_id") and (
        current is None or current.get("coordinator_session_key") != session_key
    ):
        return
    _bind_state(session_key)


def _is_actionable(
    event: dict[str, Any],
    coordinator_profile: str,
    decision_comment_author: str,
) -> bool:
    kind = event.get("kind")
    event_profile = str(event.get("event_profile") or "").strip()
    task_body = event.get("task_body") if isinstance(event.get("task_body"), dict) else {}
    task_schema = task_body.get("schema")
    if kind == "blocked":
        if event_profile == coordinator_profile:
            return False
        return (
            event_profile == _IMPLEMENTATION_PROFILE and task_schema in _IMPLEMENTATION_SCHEMAS
        ) or (
            event_profile == _REVIEW_PROFILE and task_schema == _REVIEW_SCHEMA
        )
    if kind == "completed":
        return (
            event_profile != coordinator_profile
            and event_profile == _REVIEW_PROFILE
            and task_schema == _REVIEW_SCHEMA
        )
    if kind != "commented":
        return False
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
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


def _process_pending_events(board: str) -> None:
    state = _read_state(board)
    if state is None:
        return
    coordinator_profile = str(
        _CONTEXT.get_config("coordinator_profile", "project-manager") or "project-manager"
    ).strip()
    decision_comment_author = str(
        _CONTEXT.get_config("decision_comment_author", "dashboard") or "dashboard"
    ).strip()
    for event in _read_events(board, int(state["last_event_id"])):
        actionable = _is_actionable(
            event,
            coordinator_profile,
            decision_comment_author,
        )
        wake_message = _wake_message(board, event) if actionable else None
        if not _process_event(state, event, actionable, wake_message):
            break


def _find_hint_event_id(board: str, task_id: str, event_kind: str, run_id: Any) -> int:
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect(board=board)
    try:
        clauses = ["task_id=?", "kind=?"]
        values: list[Any] = [task_id, event_kind]
        if run_id is not None:
            clauses.append("run_id=?")
            values.append(int(run_id))
        row = conn.execute(
            "SELECT id FROM task_events WHERE " + " AND ".join(clauses) + " ORDER BY id DESC LIMIT 1",
            tuple(values),
        ).fetchone()
        if row is not None:
            return int(row["id"])
    finally:
        conn.close()
    return _latest_event_id(board)


def _on_event_hint(**kwargs: Any) -> None:
    if _CONTEXT is None:
        return
    board = str(kwargs.get("board") or "").strip()
    if board:
        if _read_state() is None:
            _record_activity(
                board=board,
                action="event_hint_skipped",
                outcome="not_delivered",
                reason="no_coordinator_binding",
                task_id=str(kwargs.get("task_id") or "") or None,
                event_kind=str(kwargs.get("event_kind") or "") or None,
            )
            return
        if _read_state(board) is None:
            event_id = _find_hint_event_id(
                board,
                str(kwargs.get("task_id") or ""),
                str(kwargs.get("event_kind") or ""),
                kwargs.get("run_id"),
            )
            _ensure_board_cursor(board, max(0, event_id - 1))
        _process_pending_events(board)


def _on_dispatch_tick(**kwargs: Any) -> None:
    if kwargs.get("dry_run") is True or _CONTEXT is None:
        return
    board = str(kwargs.get("board") or "").strip()
    if not board:
        return
    _mark_dispatch_heartbeat(board)
    _process_pending_comment_deliveries(board)
    if _read_state() is not None and _read_state(board) is None:
        _ensure_board_cursor(board, _latest_event_id(board))
    _process_pending_events(board)


def _resolved_board(board: Any) -> str:
    requested = str(board or "").strip()
    if requested:
        return requested
    from hermes_cli import kanban_db

    return str(kanban_db.get_current_board())


def _create_kwc_comment(
    *,
    board: Any,
    task_id: Any,
    body: Any,
    author: Any,
    idempotency_key: Any,
    reply_to_comment_id: Any = None,
    route_requested: bool,
) -> dict[str, Any]:
    resolved_board = _resolved_board(board)
    return _create_comment_request(
        _state_path(),
        board=resolved_board,
        task_id=task_id,
        body=body,
        author=author,
        idempotency_key=idempotency_key,
        reply_to_comment_id=reply_to_comment_id,
        route_requested=route_requested,
    )


def _schedule_comment_delivery(board: str) -> None:
    threading.Thread(
        target=_process_pending_comment_deliveries,
        args=(str(board),),
        daemon=True,
        name="kwc-comment-delivery",
    ).start()


def _kwc_comment_tool(arguments: dict[str, Any]) -> str:
    try:
        result = _create_kwc_comment(
            board=arguments.get("board"),
            task_id=arguments.get("task_id"),
            body=arguments.get("body"),
            author=_current_session_profile(),
            idempotency_key=arguments.get("idempotency_key"),
            route_requested=True,
        )
        _schedule_comment_delivery(str(result["board"]))
        return json.dumps(result, ensure_ascii=False, sort_keys=True)
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True)


def _kwc_reply_tool(arguments: dict[str, Any]) -> str:
    try:
        author = _current_session_profile()
        board = _resolved_board(arguments.get("board"))
        task_id = str(arguments.get("task_id") or "").strip()
        reply_id = int(arguments.get("reply_to_comment_id"))
        result = _create_kwc_comment(
            board=board,
            task_id=task_id,
            body=arguments.get("body"),
            author=author,
            idempotency_key=arguments.get("idempotency_key"),
            reply_to_comment_id=reply_id,
            route_requested=False,
        )
        _schedule_comment_delivery(str(result["board"]))
        return json.dumps(result, ensure_ascii=False, sort_keys=True)
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True)


def _kwc_pm_response_tool(arguments: dict[str, Any]) -> str:
    try:
        result = _submit_pm_response(
            state_path=_state_path(),
            invocation_id=arguments.get("invocation_id"),
            caller_profile=_current_session_profile(),
            caller_session_key=_current_session_key(),
            outcome=arguments.get("outcome"),
            task_id=arguments.get("task_id"),
            reason_code=arguments.get("reason_code"),
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        return json.dumps(
            {"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True
        )


def _event_hint_callback(event_kind: str):
    def callback(**kwargs: Any) -> None:
        _on_event_hint(event_kind=event_kind, **kwargs)

    return callback


def register(ctx: Any) -> None:
    global _CONTEXT
    _CONTEXT = ctx
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("kanban_task_completed", _event_hint_callback("completed"))
    ctx.register_hook("kanban_task_blocked", _event_hint_callback("blocked"))
    ctx.register_hook("on_kanban_worker_exited", _event_hint_callback("worker_exited"))
    ctx.register_hook("on_kanban_worker_stale_claim", _event_hint_callback("worker_stale_claim"))
    ctx.register_hook("on_kanban_dispatch_tick", _on_dispatch_tick)
    shared_properties = {
        "board": {"type": "string", "description": "Kanban board slug; omit for current board."},
        "task_id": {"type": "string", "description": "Native Kanban task id."},
        "body": {"type": "string", "description": "Comment body."},
        "idempotency_key": {
            "type": "string",
            "description": "Stable unique key reused when retrying the same comment request.",
        },
    }
    ctx.register_tool(
        "kwc_comment",
        "kanban-workflow-controller",
        {
            "name": "kwc_comment",
            "description": "Add a KWC-routed comment. Mentions override the task assignee.",
            "parameters": {
                "type": "object",
                "properties": shared_properties,
                "required": ["task_id", "body", "idempotency_key"],
            },
        },
        _kwc_comment_tool,
        description="Add a native Kanban comment and wake the routed Profile.",
        emoji="💬",
    )
    ctx.register_tool(
        "kwc_reply",
        "kanban-workflow-controller",
        {
            "name": "kwc_reply",
            "description": "Reply to a KWC comment without recursively waking another agent.",
            "parameters": {
                "type": "object",
                "properties": {
                    **shared_properties,
                    "reply_to_comment_id": {
                        "type": "integer",
                        "description": "Source comment id being answered.",
                    },
                },
                "required": ["task_id", "reply_to_comment_id", "body", "idempotency_key"],
            },
        },
        _kwc_reply_tool,
        description="Reply in the native Kanban thread without a routing loop.",
        emoji="↩️",
    )
    ctx.register_tool(
        "kwc_pm_response",
        "kanban-workflow-controller",
        {
            "name": "kwc_pm_response",
            "description": (
                "Submit the structured result for the current KWC PM invocation. "
                "The invocation, Profile, session, and allowed outcome are validated."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "invocation_id": {
                        "type": "string",
                        "description": "Opaque invocation id supplied by KWC.",
                    },
                    "outcome": {
                        "type": "string",
                        "description": "One of the outcomes allowed by this invocation.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Related native Kanban task id, when applicable.",
                    },
                    "reason_code": {
                        "type": "string",
                        "description": "Compact stable reason code, not free-form reasoning.",
                    },
                },
                "required": ["invocation_id", "outcome"],
            },
        },
        _kwc_pm_response_tool,
        check_fn=_pm_response_available,
        description="Submit a Profile/session-bound PM result without mutating Kanban.",
        emoji="📨",
    )
