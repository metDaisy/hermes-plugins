"""Pure, non-mutating next-action decisions for the KWC role workflow."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_SNAPSHOT_SCHEMA = "kwc-workflow-snapshot-v1"
_DECISION_SCHEMA = "kwc-workflow-decision-v1"
_IMPLEMENTATION_SCHEMAS = {"backend-implementation-card-v5"}
_FINDING_ROUTE_SCHEMAS = _IMPLEMENTATION_SCHEMAS | {"policy-decision-card-v1"}
_READY_CONTRACTS = {
    **{
        schema: ("coder", "run-impl-card", "ready_implementation")
        for schema in _IMPLEMENTATION_SCHEMAS
    },
    "aggregate-review-card-v5": ("reviewer", "run-review", "ready_aggregate_review"),
}


def _result(
    *,
    board: str | None,
    valid: bool,
    phase: str,
    action: dict[str, Any] | None,
    errors: list[str],
) -> dict[str, Any]:
    return {
        "schema": _DECISION_SCHEMA,
        "board": board,
        "valid": valid,
        "phase": phase,
        "action": action,
        "errors": errors,
    }


def decide(snapshot: Any) -> dict[str, Any]:
    """Return one deterministic workflow action without mutating Kanban state."""
    if not isinstance(snapshot, Mapping) or snapshot.get("schema") != _SNAPSHOT_SCHEMA:
        return _result(
            board=None,
            valid=False,
            phase="invalid",
            action=None,
            errors=["SNAPSHOT_SCHEMA"],
        )
    board = snapshot.get("board")
    tasks = snapshot.get("tasks")
    if not isinstance(board, str) or not board.strip() or not isinstance(tasks, list):
        return _result(
            board=board if isinstance(board, str) else None,
            valid=False,
            phase="invalid",
            action=None,
            errors=["SNAPSHOT_FIELDS"],
        )
    running_tasks = [
        task for task in tasks if isinstance(task, Mapping) and task.get("status") == "running"
    ]
    review_queue = [
        task for task in tasks if isinstance(task, Mapping) and task.get("status") == "review"
    ]
    if len(running_tasks) + len(review_queue) > 1:
        return _result(
            board=board,
            valid=False,
            phase="invalid",
            action=None,
            errors=["MULTIPLE_ACTIVE_TASKS"],
        )
    if running_tasks:
        task = running_tasks[0]
        if (
            task.get("body_schema") in _IMPLEMENTATION_SCHEMAS
            and task.get("assignee") == "coder"
            and task.get("active_run_profile") == "coder"
            and task.get("active_run_source_status") == "ready"
        ):
            profile, skill, reason = "coder", "run-impl-card", "active_implementation"
        elif (
            task.get("body_schema") == "aggregate-review-card-v5"
            and task.get("assignee") == "reviewer"
            and task.get("active_run_profile") == "reviewer"
            and task.get("active_run_source_status") == "ready"
        ):
            profile, skill, reason = "reviewer", "run-review", "active_aggregate_review"
        elif (
            task.get("body_schema") in _IMPLEMENTATION_SCHEMAS
            and task.get("assignee") == "project-manager"
            and task.get("active_run_profile") == "project-manager"
            and task.get("active_run_source_status") == "review"
        ):
            profile, skill, reason = (
                "project-manager",
                "run-workflow",
                "active_implementation_checkpoint",
            )
        else:
            return _result(
                board=board,
                valid=False,
                phase="invalid",
                action=None,
                errors=[f"ACTIVE_CONTRACT_MISMATCH:{task.get('task_id')}"],
            )
        return _result(
            board=board,
            valid=True,
            phase="active",
            action={
                "kind": "wait-for-profile",
                "task_id": task.get("task_id"),
                "profile": profile,
                "skill": skill,
                "reason": reason,
            },
            errors=[],
        )
    if review_queue:
        task = review_queue[0]
        valid_checkpoint = (
            task.get("body_schema") in _IMPLEMENTATION_SCHEMAS
            and task.get("assignee") == "project-manager"
            and task.get("latest_run_profile") == "coder"
            and task.get("latest_run_status") == "review_requested"
        )
        if not valid_checkpoint:
            return _result(
                board=board,
                valid=False,
                phase="invalid",
                action=None,
                errors=[f"CHECKPOINT_CONTRACT_MISMATCH:{task.get('task_id')}"],
            )
        return _result(
            board=board,
            valid=True,
            phase="checkpoint",
            action={
                "kind": "dispatch-task",
                "task_id": task.get("task_id"),
                "profile": "project-manager",
                "skill": "run-workflow",
                "reason": "implementation_checkpoint",
            },
            errors=[],
        )
    blocked_tasks = [
        task for task in tasks if isinstance(task, Mapping) and task.get("status") == "blocked"
    ]
    blocked_workers = [
        task
        for task in blocked_tasks
        if (
            task.get("body_schema") in _IMPLEMENTATION_SCHEMAS
            and task.get("latest_run_profile") == "coder"
        )
        or (
            task.get("body_schema") == "aggregate-review-card-v5"
            and task.get("latest_run_profile") == "reviewer"
        )
    ]
    if len(blocked_workers) > 1:
        return _result(
            board=board,
            valid=False,
            phase="invalid",
            action=None,
            errors=["MULTIPLE_BLOCKED_WORKERS"],
        )
    if blocked_workers:
        task = blocked_workers[0]
        if task.get("block_kind") != "needs_input":
            return _result(
                board=board,
                valid=False,
                phase="invalid",
                action=None,
                errors=[f"WORKER_BLOCK_KIND_MISMATCH:{task.get('task_id')}"],
            )
        return _result(
            board=board,
            valid=True,
            phase="blocked",
            action={
                "kind": "wake-profile",
                "task_id": task.get("task_id"),
                "profile": "project-manager",
                "skill": "run-workflow",
                "reason": "worker_needs_input",
            },
            errors=[],
        )
    done_reviews = [
        task
        for task in tasks
        if isinstance(task, Mapping)
        and task.get("status") == "done"
        and task.get("body_schema") == "aggregate-review-card-v5"
    ]
    if done_reviews:
        latest_review = max(
            done_reviews,
            key=lambda task: (
                task.get("latest_run_id")
                if isinstance(task.get("latest_run_id"), int)
                else -1
            ),
        )
        terminal_review_run = (
            latest_review.get("latest_run_status") == "done"
            and latest_review.get("latest_run_outcome") == "completed"
        ) or latest_review.get("latest_run_status") == "completed"
        canonical_result = (
            latest_review.get("latest_run_profile") == "reviewer"
            and terminal_review_run
            and latest_review.get("latest_run_metadata_schema") == "aggregate-review-result-v2"
            and latest_review.get("latest_run_result") in {"approved", "changes-required"}
        )
        if not canonical_result:
            return _result(
                board=board,
                valid=False,
                phase="invalid",
                action=None,
                errors=[f"REVIEW_RESULT_CONTRACT_MISMATCH:{latest_review.get('task_id')}"],
            )
        if latest_review.get("latest_run_result") == "changes-required":
            review_task_id = latest_review.get("task_id")
            findings_routed = any(
                isinstance(task, Mapping)
                and task.get("status") != "archived"
                and task.get("body_schema") in _FINDING_ROUTE_SCHEMAS
                and isinstance(task.get("parents"), list)
                and review_task_id in task["parents"]
                for task in tasks
            )
            if not findings_routed:
                return _result(
                    board=board,
                    valid=True,
                    phase="review-findings",
                    action={
                        "kind": "wake-profile",
                        "task_id": review_task_id,
                        "profile": "project-manager",
                        "skill": "run-workflow",
                        "reason": "route_review_findings",
                    },
                    errors=[],
                )
        task_by_id = {
            task.get("task_id"): task
            for task in tasks
            if isinstance(task, Mapping) and isinstance(task.get("task_id"), str)
        }
        eligible_summaries = [
            task
            for task in tasks
            if latest_review.get("latest_run_result") == "approved"
            and isinstance(task, Mapping)
            and task.get("status") == "todo"
            and task.get("body_schema") == "issue-summary-card-v3"
            and task.get("assignee") == "project-manager"
            and isinstance(task.get("parents"), list)
            and all(task_by_id.get(parent, {}).get("status") == "done" for parent in task["parents"])
        ]
        if len(eligible_summaries) == 1:
            summary = eligible_summaries[0]
            return _result(
                board=board,
                valid=True,
                phase="promotion",
                action={
                    "kind": "promote-task",
                    "task_id": summary.get("task_id"),
                    "profile": "project-manager",
                    "skill": "run-workflow",
                    "reason": "approved_review_summary",
                },
                errors=[],
            )
    has_summary = any(
        isinstance(task, Mapping) and task.get("body_schema") == "issue-summary-card-v3"
        for task in tasks
    )
    if has_summary and all(
        isinstance(task, Mapping) and task.get("status") in {"done", "archived"}
        for task in tasks
    ):
        return _result(
            board=board,
            valid=True,
            phase="complete",
            action=None,
            errors=[],
        )
    task_by_id = {
        task.get("task_id"): task
        for task in tasks
        if isinstance(task, Mapping) and isinstance(task.get("task_id"), str)
    }
    eligible_tasks = [
        task
        for task in tasks
        if isinstance(task, Mapping)
        and task.get("status") == "todo"
        and task.get("body_schema") in _READY_CONTRACTS
        and isinstance(task.get("parents"), list)
        and all(task_by_id.get(parent, {}).get("status") == "done" for parent in task["parents"])
    ]
    if len(eligible_tasks) > 1:
        return _result(
            board=board,
            valid=False,
            phase="invalid",
            action=None,
            errors=["MULTIPLE_ELIGIBLE_TASKS"],
        )
    if len(eligible_tasks) == 1:
        task = eligible_tasks[0]
        profile, skill, _ = _READY_CONTRACTS[task.get("body_schema")]
        forced_skills = task.get("skills", [])
        if not isinstance(forced_skills, list) or any(
            forced_skill != skill for forced_skill in forced_skills
        ):
            return _result(
                board=board,
                valid=False,
                phase="invalid",
                action=None,
                errors=[f"TASK_SKILL_MISMATCH:{task.get('task_id')}"],
            )
        reason = (
            "eligible_implementation"
            if task.get("body_schema") in _IMPLEMENTATION_SCHEMAS
            else "eligible_aggregate_review"
        )
        return _result(
            board=board,
            valid=True,
            phase="promotion",
            action={
                "kind": "promote-task",
                "task_id": task.get("task_id"),
                "profile": profile,
                "skill": skill,
                "reason": reason,
            },
            errors=[],
        )
    ready = [task for task in tasks if isinstance(task, Mapping) and task.get("status") == "ready"]
    if len(ready) != 1:
        error = "MULTIPLE_READY_TASKS" if len(ready) > 1 else "NO_EXECUTABLE_TRANSITION"
        return _result(board=board, valid=False, phase="invalid", action=None, errors=[error])
    task = ready[0]
    contract = _READY_CONTRACTS.get(task.get("body_schema"))
    if contract is None or task.get("assignee") != contract[0]:
        return _result(
            board=board,
            valid=False,
            phase="invalid",
            action=None,
            errors=[f"READY_CONTRACT_MISMATCH:{task.get('task_id')}"],
        )
    profile, skill, reason = contract
    forced_skills = task.get("skills", [])
    if not isinstance(forced_skills, list) or any(
        forced_skill != skill for forced_skill in forced_skills
    ):
        return _result(
            board=board,
            valid=False,
            phase="invalid",
            action=None,
            errors=[f"TASK_SKILL_MISMATCH:{task.get('task_id')}"],
        )
    return _result(
        board=board,
        valid=True,
        phase="ready",
        action={
            "kind": "dispatch-task",
            "task_id": task.get("task_id"),
            "profile": profile,
            "skill": skill,
            "reason": reason,
        },
        errors=[],
    )
