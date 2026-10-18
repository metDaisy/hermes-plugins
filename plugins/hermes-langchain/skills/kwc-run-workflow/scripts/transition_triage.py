#!/usr/bin/env python3
"""Preserve a frozen Triage body while using Hermes's canonical transition.

The public ``hermes kanban specify`` command always calls the auxiliary LLM and
may rewrite title/body. PM activation instead needs the same native
``triage -> todo`` transition without field mutation. This adapter must run
with the Python interpreter that owns the Hermes CLI installation.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def _snapshot(task: Any) -> dict[str, Any]:
    return {
        "title": task.title,
        "body": task.body,
        "assignee": task.assignee,
        "status": task.status,
    }


def transition(board: str, task_id: str) -> dict[str, Any]:
    with kbc.connect_closing(board=board) as conn:
        before_task = kb.get_task(conn, task_id)
        if before_task is None:
            return {"valid": False, "error": "TRIAGE_TASK_NOT_FOUND", "task_id": task_id}
        before = _snapshot(before_task)
        if before["status"] != "triage":
            return {
                "valid": False,
                "error": f"TRIAGE_INVALID_STATUS:{before['status']}",
                "task_id": task_id,
                "before": before,
            }
        changed = kb.specify_triage_task(conn, task_id)
        after_task = kb.get_task(conn, task_id)
        if after_task is None:
            return {"valid": False, "error": "TRIAGE_READBACK_MISSING", "task_id": task_id}
        after = _snapshot(after_task)

    preserved = all(after[field] == before[field] for field in ("title", "body", "assignee"))
    valid = bool(changed) and preserved and after["status"] in {"todo", "ready"}
    return {
        "valid": valid,
        "error": None if valid else "TRIAGE_NATIVE_TRANSITION_READBACK_FAILED",
        "task_id": task_id,
        "before": before,
        "after": after,
        "preserved": preserved,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", required=True)
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()
    try:
        report = transition(args.board, args.task_id)
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        report = {"valid": False, "error": f"TRIAGE_NATIVE_TRANSITION_FAILED:{exc}"}
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get("valid") else 1


if __name__ == "__main__":
    raise SystemExit(main())
