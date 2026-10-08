"""Read-only normalization of canonical Hermes Kanban state for WorkflowEngine."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any


_SNAPSHOT_SCHEMA = "kwc-workflow-snapshot-v1"


def _ensure_hermes_source_root(hermes_cli_module: Any) -> None:
    """Make repository-root compatibility modules importable in a child Runtime."""
    package_file = getattr(hermes_cli_module, "__file__", None)
    if not isinstance(package_file, str) or not package_file:
        return
    source_root = str(Path(package_file).resolve().parent.parent)
    if source_root not in sys.path:
        sys.path.insert(0, source_root)


def _body_schema(body: Any) -> str | None:
    if not isinstance(body, str) or not body.strip():
        return None
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, Mapping):
        return None
    schema = parsed.get("schema")
    return schema if isinstance(schema, str) and schema else None


def _linked_ids(items: Any) -> list[str]:
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return []
    result: list[str] = []
    for item in items:
        value = item.get("id") if isinstance(item, Mapping) else item
        if isinstance(value, str) and value:
            result.append(value)
    return result


def _claimed_source_status(events: Any, run_id: Any) -> str | None:
    if not isinstance(run_id, int) or not isinstance(events, Sequence):
        return None
    for event in reversed(events):
        if (
            getattr(event, "run_id", None) == run_id
            and getattr(event, "kind", None) == "claimed"
        ):
            payload = getattr(event, "payload", None)
            if isinstance(payload, Mapping):
                source_status = payload.get("source_status")
                return source_status if isinstance(source_status, str) else None
    return None


def normalize_snapshot(
    *,
    board: str,
    tasks: Sequence[Any],
    graph_contexts: Mapping[str, Any],
    runs_by_task: Mapping[str, Sequence[Any]],
    events_by_task: Mapping[str, Sequence[Any]],
) -> dict[str, Any]:
    """Convert canonical task/run/event objects into the closed engine snapshot."""
    normalized: list[dict[str, Any]] = []
    for task in tasks:
        task_id = str(getattr(task, "id"))
        runs = list(runs_by_task.get(task_id, ()))
        latest_run = runs[-1] if runs else None
        current_run_id = getattr(task, "current_run_id", None)
        active_run = next(
            (run for run in reversed(runs) if getattr(run, "id", None) == current_run_id),
            None,
        )
        metadata = getattr(latest_run, "metadata", None)
        if not isinstance(metadata, Mapping):
            metadata = {}
        graph = graph_contexts.get(task_id, {})
        if not isinstance(graph, Mapping):
            graph = {}
        skills = getattr(task, "skills", None)
        normalized.append(
            {
                "task_id": task_id,
                "status": getattr(task, "status", None),
                "assignee": getattr(task, "assignee", None),
                "body_schema": _body_schema(getattr(task, "body", None)),
                "parents": _linked_ids(graph.get("parents")),
                "children": _linked_ids(graph.get("children")),
                "skills": [str(skill) for skill in skills] if isinstance(skills, list) else [],
                "active_run_profile": getattr(active_run, "profile", None),
                "active_run_source_status": _claimed_source_status(
                    events_by_task.get(task_id, ()), current_run_id
                ),
                "latest_run_profile": getattr(latest_run, "profile", None),
                "latest_run_id": getattr(latest_run, "id", None),
                "latest_run_status": getattr(latest_run, "status", None),
                "latest_run_metadata_schema": metadata.get("schema"),
                "latest_run_result": metadata.get("result"),
                "block_kind": getattr(task, "block_kind", None),
            }
        )
    return {"schema": _SNAPSHOT_SCHEMA, "board": board, "tasks": normalized}


def read_board_snapshot(
    board: str,
    *,
    kanban_db_module: Any = None,
    connect_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Read one board through canonical Hermes functions without mutating it."""
    if kanban_db_module is None or connect_fn is None:
        import hermes_cli

        _ensure_hermes_source_root(hermes_cli)
        from hermes_cli import kanban_db
        from hermes_cli import kanban_db_connect

        kanban_db_module = kanban_db_module or kanban_db
        connect_fn = connect_fn or kanban_db_connect.connect
    conn = connect_fn(board=board)
    try:
        tasks = kanban_db_module.list_tasks(conn)
        task_ids = [task.id for task in tasks]
        graph_contexts = kanban_db_module.task_graph_contexts(conn, task_ids)
        runs_by_task = {
            task_id: kanban_db_module.list_runs(conn, task_id) for task_id in task_ids
        }
        events_by_task = {
            task_id: kanban_db_module.list_events(conn, task_id) for task_id in task_ids
        }
        return normalize_snapshot(
            board=board,
            tasks=tasks,
            graph_contexts=graph_contexts,
            runs_by_task=runs_by_task,
            events_by_task=events_by_task,
        )
    finally:
        conn.close()
