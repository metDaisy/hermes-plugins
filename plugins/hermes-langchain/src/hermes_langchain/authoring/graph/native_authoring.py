"""Native Kanban materialization use cases for task-graph authoring."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .graph_contract import REVIEW_GUIDE_LOCATORS, validate_review_graph
from .implementation_batch import _parse_json_values, validate_impl_batch
from .implementation_card_v5 import (
    _implementation_triage_digest,
    _validate_frozen_triage,
    validate,
)
from .review_fast_path import (
    create_review_native as _create_review_native,
    create_summary_native as _create_summary_native,
    prepare_review_fast_path as _prepare_review_fast_path,
    prepare_summary_fast_path as _prepare_summary_fast_path,
    regenerate_summary_native as _regenerate_summary_native,
    validate_review_fast_path as _validate_review_fast_path,
)
from .task_graph_io import _read_json

def _created_task_id(payload: Any, expected_title: str) -> str:
    candidates: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("title") == expected_title:
                for field in ("id", "task_id"):
                    candidate = value.get(field)
                    if isinstance(candidate, str) and candidate.startswith("t_"):
                        candidates.append(candidate)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    if not candidates and isinstance(payload, dict):
        for field in ("id", "task_id"):
            candidate = payload.get(field)
            if isinstance(candidate, str) and candidate.startswith("t_"):
                candidates.append(candidate)
    unique = list(dict.fromkeys(candidates))
    if len(unique) != 1:
        raise ValueError(f"CREATE_ID_NOT_UNIQUE:{len(unique)}")
    return unique[0]


def _parent_ids(parents: Any) -> list[str]:
    if not isinstance(parents, list):
        return []
    result: list[str] = []
    for parent in parents:
        if isinstance(parent, str):
            result.append(parent)
        elif isinstance(parent, dict):
            candidate = parent.get("id") or parent.get("task_id")
            if isinstance(candidate, str):
                result.append(candidate)
    return result


def prepare_review_fast_path(
    active_tasks: list[dict[str, Any]],
    *,
    triage_id: str,
    implementation_ids: list[str],
    issue: int,
    generation: int,
) -> dict[str, Any]:
    return _prepare_review_fast_path(
        active_tasks,
        triage_id=triage_id,
        implementation_ids=implementation_ids,
        issue=issue,
        generation=generation,
        validate_triage=_validate_frozen_triage,
        validate_implementation=validate,
        guide_locators=REVIEW_GUIDE_LOCATORS,
    )


def validate_review_fast_path(bundle: dict[str, Any], review_body: dict[str, Any]) -> list[str]:
    return _validate_review_fast_path(
        bundle,
        review_body,
        validate_review=validate_review_graph,
        validate_implementation=validate,
    )


def create_review_native(
    bundle: dict[str, Any],
    review_body: dict[str, Any],
    *,
    profile: str,
    board: str,
    body_file: Path,
    workspace_path: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    return _create_review_native(
        bundle,
        review_body,
        profile=profile,
        board=board,
        body_file=body_file,
        workspace_path=workspace_path,
        dry_run=dry_run,
        validate_triage=_validate_frozen_triage,
        validate_implementation=validate,
        validate_review=validate_review_graph,
        guide_locators=REVIEW_GUIDE_LOCATORS,
    )


def prepare_summary_fast_path(
    active_tasks: list[dict[str, Any]],
    *,
    triage_id: str,
    issue: int,
    generation: int,
) -> dict[str, Any]:
    return _prepare_summary_fast_path(
        active_tasks,
        triage_id=triage_id,
        issue=issue,
        generation=generation,
        validate_triage=_validate_frozen_triage,
    )


def create_summary_native(
    bundle: dict[str, Any],
    *,
    profile: str,
    board: str,
    body_file: Path,
    workspace_path: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    return _create_summary_native(
        bundle,
        profile=profile,
        board=board,
        body_file=body_file,
        workspace_path=workspace_path,
        dry_run=dry_run,
        validate_triage=_validate_frozen_triage,
    )


def regenerate_summary_native(
    *,
    profile: str,
    board: str,
    triage_id: str,
    issue: int,
    generation: int,
    workspace_path: Path,
    bundle_file: Path,
    body_file: Path,
) -> dict[str, Any]:
    return _regenerate_summary_native(
        profile=profile,
        board=board,
        triage_id=triage_id,
        issue=issue,
        generation=generation,
        workspace_path=workspace_path,
        bundle_file=bundle_file,
        body_file=body_file,
        validate_triage=_validate_frozen_triage,
    )


def create_impl_batch_native(
    batch: dict[str, Any],
    *,
    profile: str,
    board: str,
    triage_id: str,
    body_dir: Path,
    workspace_path: Path,
) -> dict[str, Any]:
    errors = validate_impl_batch(batch)
    if errors:
        raise ValueError("INVALID_IMPL_BATCH:" + "|".join(errors))
    workspace = workspace_path.resolve()
    if workspace != Path.cwd().resolve():
        raise ValueError("NATIVE_CREATE_WORKSPACE_MISMATCH")
    body_root = body_dir.resolve()
    temp_root = (workspace / ".temp").resolve()
    if temp_root not in body_root.parents:
        raise ValueError("NATIVE_BODY_DIR_OUTSIDE_TEMP")
    for index, wrapper in enumerate(batch["cards"]):
        if wrapper.get("parent_ids") != [triage_id]:
            raise ValueError(f"IMPL_PARENT_TRIAGE_MISMATCH:{index}")
        body_file = body_root / f"impl-{wrapper['ordinal']}.json"
        expected_text = json.dumps(wrapper["body"], ensure_ascii=False, indent=2) + "\n"
        if _read_json(body_file) != wrapper["body"] or body_file.read_text(encoding="utf-8") != expected_text:
            raise ValueError(f"NATIVE_BODY_FILE_MISMATCH:{wrapper['ordinal']}")

    base = ["hermes", "--profile", profile, "kanban", "--board", board]
    created_ids: list[str] = []
    created_by_ordinal: dict[int, str] = {}
    created: list[dict[str, Any]] = []

    def run_values(command: list[str]) -> list[Any]:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=workspace,
        )
        return _parse_json_values(completed.stdout)

    def run_json(command: list[str]) -> Any:
        values = run_values(command)
        if len(values) != 1:
            raise ValueError(f"NATIVE_JSON_VALUE_COUNT:{len(values)}")
        return values[0]

    def list_tasks() -> list[dict[str, Any]]:
        return [
            value
            for value in run_values([*base, "list", "--json"])
            if isinstance(value, dict)
        ]

    def validated_triage(envelope: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        task = envelope.get("task") if isinstance(envelope, dict) else None
        if not isinstance(task, dict) or task.get("id") != triage_id:
            raise ValueError("SOURCE_TRIAGE_ID_MISMATCH")
        if task.get("status") != "triage":
            raise ValueError("TRIAGE_STATUS_CHANGED")
        if task.get("workspace_kind") != "dir" or task.get("workspace_path") != workspace.as_posix():
            raise ValueError("SOURCE_TRIAGE_WORKSPACE_MISMATCH")
        raw_body = task.get("body")
        if not isinstance(raw_body, str):
            raise ValueError("SOURCE_TRIAGE_BODY_INVALID")
        body = json.loads(raw_body)
        triage_errors = _validate_frozen_triage(body)
        if triage_errors:
            raise ValueError(f"SOURCE_TRIAGE_INVALID:{triage_errors[0]}")
        if body.get("issue", {}).get("number") != batch.get("issue"):
            raise ValueError("SOURCE_TRIAGE_ISSUE_MISMATCH")
        digests = {
            _implementation_triage_digest(wrapper.get("body"))
            for wrapper in batch["cards"]
            if isinstance(wrapper.get("body"), dict)
        }
        if digests != {body.get("frozen_digest")}:
            raise ValueError("SOURCE_TRIAGE_DIGEST_MISMATCH")
        source_slice_ids = [
            item.get("id")
            for item in body.get("implementation_slices", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        batch_slice_ids = [
            wrapper.get("body", {}).get("slice", {}).get("id")
            for wrapper in batch["cards"]
            if isinstance(wrapper.get("body"), dict)
        ]
        source_slice_set = set(source_slice_ids)
        batch_slice_set = set(batch_slice_ids)
        missing = sorted(source_slice_set - batch_slice_set)
        unexpected = sorted(batch_slice_set - source_slice_set)
        duplicates = sorted(
            slice_id for slice_id in batch_slice_set if batch_slice_ids.count(slice_id) > 1
        )
        if (
            missing
            or unexpected
            or duplicates
            or len(batch_slice_ids) != len(source_slice_ids)
        ):
            raise ValueError(
                "IMPL_SLICE_CLOSURE_MISMATCH:"
                f"missing={','.join(missing) or '-'}:"
                f"unexpected={','.join(unexpected) or '-'}:"
                f"duplicates={','.join(duplicates) or '-'}"
            )
        source_slices = {
            item["id"]: item
            for item in body.get("implementation_slices", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        for wrapper in batch["cards"]:
            impl_body = wrapper.get("body", {})
            slice_id = impl_body.get("slice", {}).get("id")
            source_slice = source_slices.get(slice_id, {})
            source_acceptance = source_slice.get("acceptance", [])
            expected_acceptance_ids = [
                item.get("id")
                for item in source_acceptance
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ]
            actual_acceptance_ids = [
                item.get("id")
                for item in impl_body.get("acceptance", [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ]
            acceptance_missing = sorted(set(expected_acceptance_ids) - set(actual_acceptance_ids))
            acceptance_unexpected = sorted(set(actual_acceptance_ids) - set(expected_acceptance_ids))
            acceptance_duplicates = sorted(
                item_id
                for item_id in set(actual_acceptance_ids)
                if actual_acceptance_ids.count(item_id) > 1
            )
            if (
                acceptance_missing
                or acceptance_unexpected
                or acceptance_duplicates
                or len(actual_acceptance_ids) != len(expected_acceptance_ids)
            ):
                raise ValueError(
                    f"IMPL_ACCEPTANCE_CLOSURE_MISMATCH:{slice_id}:"
                    f"missing={','.join(acceptance_missing) or '-'}:"
                    f"unexpected={','.join(acceptance_unexpected) or '-'}:"
                    f"duplicates={','.join(acceptance_duplicates) or '-'}"
                )
            expected_verification_ids = sorted(
                {
                    verification_id
                    for acceptance in source_acceptance
                    if isinstance(acceptance, dict)
                    for verification_id in acceptance.get("verification_ids", [])
                    if isinstance(verification_id, str)
                }
            )
            actual_verification_ids = [
                item.get("id")
                for item in impl_body.get("verification", [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ]
            verification_missing = sorted(
                set(expected_verification_ids) - set(actual_verification_ids)
            )
            verification_unexpected = sorted(
                set(actual_verification_ids) - set(expected_verification_ids)
            )
            verification_duplicates = sorted(
                item_id
                for item_id in set(actual_verification_ids)
                if actual_verification_ids.count(item_id) > 1
            )
            if (
                verification_missing
                or verification_unexpected
                or verification_duplicates
                or len(actual_verification_ids) != len(expected_verification_ids)
            ):
                raise ValueError(
                    f"IMPL_VERIFICATION_CLOSURE_MISMATCH:{slice_id}:"
                    f"missing={','.join(verification_missing) or '-'}:"
                    f"unexpected={','.join(verification_unexpected) or '-'}:"
                    f"duplicates={','.join(verification_duplicates) or '-'}"
                )
        return task, body

    triage_envelope = run_json([*base, "show", triage_id, "--json"])
    triage_task, triage_body = validated_triage(triage_envelope)
    baseline_tasks = list_tasks()
    baseline_ids = {
        task.get("id")
        for task in baseline_tasks
        if isinstance(task.get("id"), str)
    }
    expected_titles = {wrapper["title"] for wrapper in batch["cards"]}
    try:
        for wrapper in batch["cards"]:
            ordinal = wrapper["ordinal"]
            body_file = body_root / f"impl-{ordinal}.json"
            expected_body = body_file.read_text(encoding="utf-8")
            expected_parent_ids = [
                triage_id,
                *[
                    created_by_ordinal[dependency]
                    for dependency in wrapper.get("depends_on_ordinals", [])
                ],
            ]
            parent_args = [
                argument
                for parent_id in expected_parent_ids
                for argument in ("--parent", parent_id)
            ]
            try:
                payload = run_json(
                    [
                        *base,
                        "create",
                        wrapper["title"],
                        "--body-file",
                        str(body_file),
                        "--assignee",
                        "coder",
                        "--priority",
                        str(wrapper["priority"]),
                        "--workspace",
                        f"dir:{workspace.as_posix()}",
                        *parent_args,
                        "--json",
                    ]
                )
                task_id = _created_task_id(payload, wrapper["title"])
            except (TypeError, ValueError):
                recovered = [
                    task["id"]
                    for task in list_tasks()
                    if isinstance(task.get("id"), str)
                    and task["id"] not in baseline_ids
                    and task.get("title") in expected_titles
                ]
                created_ids.extend(task_id for task_id in recovered if task_id not in created_ids)
                raise
            created_ids.append(task_id)
            created_by_ordinal[ordinal] = task_id
            envelope = run_json([*base, "show", task_id, "--json"])
            task = envelope.get("task") if isinstance(envelope, dict) else None
            parents = envelope.get("parents") if isinstance(envelope, dict) else None
            checks = {
                "title": isinstance(task, dict) and task.get("title") == wrapper["title"],
                "body": isinstance(task, dict) and task.get("body") == expected_body,
                "assignee": isinstance(task, dict) and task.get("assignee") == "coder",
                "skills": isinstance(task, dict) and task.get("skills") == [],
                "status": isinstance(task, dict) and task.get("status") == "todo",
                "priority": isinstance(task, dict) and task.get("priority") == wrapper["priority"],
                "workspace_kind": isinstance(task, dict) and task.get("workspace_kind") == "dir",
                "workspace_path": isinstance(task, dict)
                and task.get("workspace_path") == workspace.as_posix(),
                "parents": len(_parent_ids(parents)) == len(expected_parent_ids)
                and set(_parent_ids(parents)) == set(expected_parent_ids),
            }
            if not all(checks.values()):
                raise ValueError(f"NATIVE_READBACK_MISMATCH:{task_id}")
            created.append({"id": task_id, "title": wrapper["title"], "checks": checks})

        final_triage_envelope = run_json([*base, "show", triage_id, "--json"])
        final_triage_task, final_triage_body = validated_triage(final_triage_envelope)
        if final_triage_task.get("body") != triage_task.get("body") or final_triage_body != triage_body:
            raise ValueError("SOURCE_TRIAGE_CHANGED")
    except (OSError, subprocess.CalledProcessError, TypeError, ValueError) as original:
        cleanup_errors: list[str] = []
        for task_id in created_ids:
            try:
                subprocess.run(
                    [*base, "archive", task_id],
                    check=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    cwd=workspace,
                )
            except (OSError, subprocess.CalledProcessError) as cleanup_error:
                cleanup_errors.append(f"ARCHIVE_FAILED:{task_id}:{cleanup_error}")
        try:
            active_ids = {
                task.get("id")
                for task in list_tasks()
                if isinstance(task.get("id"), str)
            }
            for task_id in created_ids:
                if task_id in active_ids:
                    cleanup_errors.append(f"ARCHIVE_READBACK_FAILED:{task_id}")
        except (OSError, subprocess.CalledProcessError, TypeError, ValueError) as cleanup_error:
            cleanup_errors.append(f"ROLLBACK_READBACK_FAILED:{cleanup_error}")
        if cleanup_errors:
            raise ValueError("ROLLBACK_FAILED:" + "|".join(cleanup_errors)) from original
        raise

    return {
        "valid": True,
        "created": created,
        "triage": {"id": triage_id, "status": final_triage_task["status"]},
    }
