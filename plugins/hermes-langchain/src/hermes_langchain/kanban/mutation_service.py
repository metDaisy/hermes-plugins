"""Canonical Hermes Kanban mutation adapters for claimed KWC actions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import action_service


_ACTION_SCHEMA = "kwc-workflow-action-v1"
_PROGRESS_STATUSES = {"ready", "running", "review", "done"}


def _load_action_service() -> Any:
    return action_service


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _default_canonical_modules() -> tuple[Any, Callable[..., Any]]:
    from hermes_cli import kanban_db, kanban_db_connect

    return kanban_db, kanban_db_connect.connect


def _snapshot_task_status(snapshot: Any, task_id: str) -> str:
    if not isinstance(snapshot, dict):
        return ""
    tasks = snapshot.get("tasks")
    if not isinstance(tasks, list):
        return ""
    for task in tasks:
        if isinstance(task, dict) and task.get("task_id") == task_id:
            return str(task.get("status") or "")
    return ""


def execute_promote_action(
    *,
    action: Any,
    snapshot_reader: Callable[[str], dict[str, Any]],
    connect_fn: Callable[..., Any] | None = None,
    kanban_db_module: Any = None,
) -> dict[str, Any]:
    """Promote one claimed action through canonical Hermes functions and read it back."""
    if not isinstance(action, dict) or action.get("schema") != _ACTION_SCHEMA:
        raise ValueError("action schema is invalid")
    if action.get("action_kind") != "promote-task":
        raise ValueError("action_kind must be promote-task")
    if action.get("status") != "claimed":
        raise ValueError("action must be claimed before mutation")

    action_id = _required_text(action.get("action_id"), "action_id")
    board = _required_text(action.get("board"), "board")
    task_id = _required_text(action.get("task_id"), "task_id")
    reason = _required_text(action.get("reason"), "reason")
    expected_fingerprint = _required_text(
        action.get("snapshot_fingerprint"), "snapshot_fingerprint"
    )

    current_snapshot = snapshot_reader(board)
    actual_fingerprint = _load_action_service().fingerprint_snapshot(current_snapshot)
    if actual_fingerprint != expected_fingerprint:
        current_status = _snapshot_task_status(current_snapshot, task_id)
        if current_status in _PROGRESS_STATUSES:
            return {
                "ok": True,
                "action_id": action_id,
                "board": board,
                "task_id": task_id,
                "result_code": "PROMOTION_ALREADY_APPLIED",
                "previous_status": current_status,
                "current_status": current_status,
                "idempotent_replay": True,
            }
        raise ValueError("canonical snapshot changed before mutation")

    if kanban_db_module is None or connect_fn is None:
        default_module, default_connect = _default_canonical_modules()
        kanban_db_module = kanban_db_module or default_module
        connect_fn = connect_fn or default_connect

    conn = connect_fn(board=board)
    try:
        before = kanban_db_module.get_task(conn, task_id)
        if before is None:
            raise ValueError("canonical task was not found")
        previous_status = str(getattr(before, "status", "") or "")
        if previous_status in _PROGRESS_STATUSES:
            return {
                "ok": True,
                "action_id": action_id,
                "board": board,
                "task_id": task_id,
                "result_code": "PROMOTION_ALREADY_APPLIED",
                "previous_status": previous_status,
                "current_status": previous_status,
                "idempotent_replay": True,
            }

        ok, _error = kanban_db_module.promote_task(
            conn,
            task_id,
            actor="hermes-langchain",
            reason=reason,
            dry_run=False,
        )
        if not ok:
            raise ValueError("canonical promotion failed")

        after = kanban_db_module.get_task(conn, task_id)
        current_status = str(getattr(after, "status", "") or "") if after else ""
        if current_status not in _PROGRESS_STATUSES:
            raise ValueError("canonical promotion read-back did not confirm progress")
        return {
            "ok": True,
            "action_id": action_id,
            "board": board,
            "task_id": task_id,
            "result_code": "PROMOTED",
            "previous_status": previous_status,
            "current_status": current_status,
            "idempotent_replay": False,
        }
    finally:
        conn.close()
