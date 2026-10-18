"""Fenced canonical Hermes Kanban adapter for KWC dispatch actions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import action_service


_ACTION_SCHEMA = "kwc-workflow-action-v1"


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


def _snapshot_task(snapshot: Any, task_id: str) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("tasks"), list):
        return None
    for task in snapshot["tasks"]:
        if isinstance(task, dict) and task.get("task_id") == task_id:
            return task
    return None


def _already_claimed_result(
    *, action_id: str, board: str, task_id: str, profile: str, task: dict[str, Any] | None
) -> dict[str, Any] | None:
    if task is None or str(task.get("status") or "") != "running":
        return None
    if str(task.get("active_run_profile") or "") != profile:
        return None
    run_id = task.get("latest_run_id")
    if isinstance(run_id, bool):
        return None
    try:
        run_id = int(run_id)
    except (TypeError, ValueError):
        return None
    if run_id <= 0:
        return None
    return {
        "ok": True,
        "action_id": action_id,
        "board": board,
        "task_id": task_id,
        "profile": profile,
        "run_id": run_id,
        "workspace_path": str(task.get("workspace_path") or ""),
        "result_code": "DISPATCH_ALREADY_CLAIMED",
        "idempotent_replay": True,
    }


def execute_dispatch_action(
    *,
    action: Any,
    snapshot_reader: Callable[[str], dict[str, Any]],
    connect_fn: Callable[..., Any] | None = None,
    kanban_db_module: Any = None,
    claim_ttl_seconds: int = 1800,
) -> dict[str, Any]:
    """Claim one ready/review task through native Core after snapshot fencing."""
    if not isinstance(action, dict) or action.get("schema") != _ACTION_SCHEMA:
        raise ValueError("action schema is invalid")
    if action.get("action_kind") != "dispatch-task":
        raise ValueError("action_kind must be dispatch-task")
    if action.get("status") != "claimed":
        raise ValueError("action must be claimed before dispatch")

    action_id = _required_text(action.get("action_id"), "action_id")
    board = _required_text(action.get("board"), "board")
    task_id = _required_text(action.get("task_id"), "task_id")
    profile = _required_text(action.get("profile"), "profile")
    expected_fingerprint = _required_text(
        action.get("snapshot_fingerprint"), "snapshot_fingerprint"
    )

    current_snapshot = snapshot_reader(board)
    current_task = _snapshot_task(current_snapshot, task_id)
    actual_fingerprint = _load_action_service().fingerprint_snapshot(current_snapshot)
    if actual_fingerprint != expected_fingerprint:
        recovered = _already_claimed_result(
            action_id=action_id,
            board=board,
            task_id=task_id,
            profile=profile,
            task=current_task,
        )
        if recovered is not None:
            return recovered
        raise ValueError("canonical snapshot changed before dispatch")
    if current_task is None:
        raise ValueError("canonical task was not found")
    source_status = str(current_task.get("status") or "")
    if source_status not in {"ready", "review"}:
        raise ValueError("dispatch source status must be ready or review")

    if kanban_db_module is None or connect_fn is None:
        default_module, default_connect = _default_canonical_modules()
        kanban_db_module = kanban_db_module or default_module
        connect_fn = connect_fn or default_connect

    conn = connect_fn(board=board)
    try:
        claim_fn = (
            kanban_db_module.claim_review_task
            if source_status == "review"
            else kanban_db_module.claim_task
        )
        claimed = claim_fn(
            conn,
            task_id,
            ttl_seconds=claim_ttl_seconds,
            claimer=f"kwc:{action_id}",
        )
        if claimed is None:
            raise ValueError("canonical dispatch claim failed")
        after = kanban_db_module.get_task(conn, task_id)
        current_status = str(getattr(after, "status", "") or "") if after else ""
        run_id = getattr(after, "current_run_id", None) if after else None
        if current_status != "running" or isinstance(run_id, bool):
            raise ValueError("canonical dispatch read-back did not confirm a running task")
        try:
            run_id = int(run_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("canonical dispatch read-back did not confirm a run") from exc
        if run_id <= 0:
            raise ValueError("canonical dispatch read-back did not confirm a run")
        return {
            "ok": True,
            "action_id": action_id,
            "board": board,
            "task_id": task_id,
            "profile": profile,
            "run_id": run_id,
            "workspace_path": str(getattr(after, "workspace_path", "") or ""),
            "result_code": "DISPATCH_CLAIMED",
            "idempotent_replay": False,
        }
    finally:
        conn.close()
