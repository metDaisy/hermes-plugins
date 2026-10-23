#!/usr/bin/env python3
"""Deterministic source, validation, and native-create path for aggregate Review cards."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .contract_common import serialize_task_body
from .graph_contract import SUMMARY_RELEASE_EXCLUDED_ACTIONS, SUMMARY_REQUIRED_CONDITIONS, TITLE

Validate = Callable[[Any], list[str]]


def _snapshot(task: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "id", "title", "body", "assignee", "skills", "status", "priority",
        "workspace_kind", "workspace_path",
    )
    snapshot = {field: task.get(field) for field in fields}
    if snapshot.get("workspace_kind") == "dir" and isinstance(snapshot.get("workspace_path"), str):
        snapshot["workspace_path"] = Path(snapshot["workspace_path"]).resolve().as_posix()
    return snapshot


def _fingerprint(tasks: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        [_snapshot(task) for task in tasks],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _body(task: dict[str, Any], label: str) -> dict[str, Any]:
    raw = task.get("body")
    if not isinstance(raw, str):
        raise ValueError(f"{label}_BODY_INVALID")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{label}_BODY_NOT_OBJECT")
    return value


def _implementation_triage_digest(body: dict[str, Any]) -> Any:
    if body.get("schema") == "backend-implementation-card-v6":
        trace = body.get("trace")
        return trace.get("triage_digest") if isinstance(trace, dict) else None
    return body.get("triage_digest")


def _implementation_behavior_ids(body: dict[str, Any]) -> list[str]:
    if body.get("schema") == "backend-implementation-card-v6":
        trace = body.get("trace")
        values = trace.get("observable_ids") if isinstance(trace, dict) else None
        return list(values) if isinstance(values, list) else []
    implementation_slice = body.get("slice")
    values = (
        implementation_slice.get("behavior_ids")
        if isinstance(implementation_slice, dict)
        else None
    )
    return list(values) if isinstance(values, list) else []


def _implementation_contract_ids(body: dict[str, Any]) -> list[str]:
    if body.get("schema") == "backend-implementation-card-v6":
        return list(
            dict.fromkeys(
                item.get("id")
                for field in ("obligations", "constraints")
                for item in body.get(field, [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            )
        )
    return [
        row.get("id")
        for row in body.get("acceptance", [])
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    ]


def _json_values(text: str) -> list[Any]:
    decoder = json.JSONDecoder()
    values: list[Any] = []
    offset = 0
    while offset < len(text):
        while offset < len(text) and text[offset].isspace():
            offset += 1
        if offset >= len(text):
            break
        value, offset = decoder.raw_decode(text, offset)
        values.extend(value if isinstance(value, list) else [value])
    return values


def _parent_ids(parents: Any) -> list[str]:
    if not isinstance(parents, list):
        return []
    return [
        parent if isinstance(parent, str) else parent.get("id") or parent.get("task_id")
        for parent in parents
        if isinstance(parent, (str, dict))
    ]


def _same_task_ids(left: list[str], right: list[str]) -> bool:
    return sorted(left) == sorted(right)


def _created_id(payload: Any, title: str) -> str:
    candidates: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("title") == title:
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


def summary_body_from_triage(triage_body: dict[str, Any]) -> dict[str, Any]:
    """Project a human-readable, immutable Summary contract from frozen Triage."""
    issue = triage_body.get("issue")
    intent = triage_body.get("intent")
    if not isinstance(issue, dict) or not isinstance(intent, dict):
        raise ValueError("SUMMARY_SOURCE_TRIAGE_INCOMPLETE")
    triage_digest = triage_body.get("frozen_digest")
    if not isinstance(triage_digest, str) or re.fullmatch(r"[0-9a-f]{64}", triage_digest) is None:
        raise ValueError("SUMMARY_SOURCE_TRIAGE_DIGEST_INVALID")
    target_outcomes = [
        {"id": row.get("id"), "outcome": row.get("outcome")}
        for row in intent.get("change_requirements", [])
        if isinstance(row, dict)
    ]
    preserved_behaviors = [
        {"id": row.get("id"), "behavior": row.get("behavior")}
        for row in intent.get("preserved_observables", [])
        if isinstance(row, dict)
    ]
    return {
        "schema": "issue-summary-card-v3",
        "issue": {
            "number": issue.get("number"),
            "title": issue.get("title"),
            "url": issue.get("url"),
        },
        "triage_digest": triage_digest,
        "objective": intent.get("goal"),
        "target_outcomes": target_outcomes,
        "preserved_behaviors": preserved_behaviors,
        "completion_contract": {
            "admission_schema": "summary-admission-v1",
            "required_conditions": list(SUMMARY_REQUIRED_CONDITIONS),
        },
        "release_boundary": {
            "mode": "deferred",
            "next_card_type": "issue-release",
            "excluded_actions": list(SUMMARY_RELEASE_EXCLUDED_ACTIONS),
        },
    }


def prepare_summary_fast_path(
    active_tasks: list[dict[str, Any]],
    *,
    triage_id: str,
    issue: int,
    generation: int,
    validate_triage: Validate,
) -> dict[str, Any]:
    """Derive one Summary body and complete native parent closure from frozen Triage."""
    by_id = {
        task.get("id"): task
        for task in active_tasks
        if isinstance(task, dict) and isinstance(task.get("id"), str)
    }
    triage_task = by_id.get(triage_id)
    if triage_task is None:
        raise ValueError(f"SUMMARY_SOURCE_TRIAGE_NOT_ACTIVE:{triage_id}")
    triage_match = TITLE.fullmatch(str(triage_task.get("title", "")))
    if (
        triage_match is None
        or triage_match.group("kind") != "Triage"
        or int(triage_match.group("issue")) != issue
        or int(triage_match.group("generation")) != generation
    ):
        raise ValueError("SUMMARY_SOURCE_TRIAGE_TITLE_INVALID")
    triage_body = _body(triage_task, "SUMMARY_SOURCE_TRIAGE")
    triage_errors = validate_triage(triage_body)
    if triage_errors:
        raise ValueError(f"SUMMARY_SOURCE_TRIAGE_INVALID:{triage_errors[0]}")
    if triage_body.get("issue", {}).get("number") != issue:
        raise ValueError("SUMMARY_SOURCE_TRIAGE_ISSUE_MISMATCH")

    title = f"G{generation}-Issue{issue}-Summary"
    if any(task.get("title") == title for task in active_tasks if isinstance(task, dict)):
        raise ValueError("SUMMARY_TITLE_ALREADY_EXISTS")

    parent_rows: list[tuple[tuple[int, int], dict[str, Any]]] = []
    for task in active_tasks:
        if not isinstance(task, dict):
            continue
        match = TITLE.fullmatch(str(task.get("title", "")))
        if (
            match is None
            or int(match.group("issue")) != issue
            or int(match.group("generation")) != generation
        ):
            continue
        kind = match.group("kind")
        if kind.startswith("Impl"):
            order = (0, int(kind.removeprefix("Impl")))
            expected_schemas = {
                "backend-implementation-card-v5",
                "backend-implementation-card-v6",
            }
        elif kind.startswith("Review"):
            order = (1, int(kind.removeprefix("Review")))
            expected_schemas = {"aggregate-review-card-v5"}
        elif kind.startswith("Decision"):
            order = (2, int(kind.removeprefix("Decision")))
            expected_schemas = {"policy-decision-card-v1"}
        else:
            continue
        body = _body(task, f"SUMMARY_PARENT:{task.get('id')}")
        if body.get("schema") not in expected_schemas:
            raise ValueError(f"SUMMARY_PARENT_SCHEMA_INVALID:{task.get('id')}")
        body_digest = (
            _implementation_triage_digest(body)
            if kind.startswith("Impl")
            else body.get("triage_digest")
        )
        if kind.startswith(("Impl", "Review")) and body_digest != triage_body.get("frozen_digest"):
            raise ValueError(f"SUMMARY_PARENT_TRIAGE_DIGEST_MISMATCH:{task.get('id')}")
        parent_rows.append((order, task))
    parent_rows.sort(key=lambda item: item[0])
    if not parent_rows or not any(order[0] == 1 for order, _ in parent_rows):
        raise ValueError("SUMMARY_REVIEW_PARENT_MISSING")
    parents = [task for _, task in parent_rows]
    workspaces = {
        (task.get("workspace_kind"), Path(str(task.get("workspace_path", ""))).resolve().as_posix())
        for task in parents
    }
    if len(workspaces) != 1:
        raise ValueError("SUMMARY_PARENT_WORKSPACE_MISMATCH")
    workspace_kind, workspace = next(iter(workspaces))
    if workspace_kind != "dir":
        raise ValueError("SUMMARY_PARENT_WORKSPACE_KIND_INVALID")
    source_tasks = [_snapshot(triage_task), *(_snapshot(task) for task in parents)]
    return {
        "schema": "summary-fast-path-v1",
        "issue": issue,
        "generation": generation,
        "title": title,
        "workspace": workspace,
        "triage_id": triage_id,
        "parent_ids": [task["id"] for task in parents],
        "source_fingerprint": _fingerprint(source_tasks),
        "body": summary_body_from_triage(triage_body),
    }


def prepare_review_fast_path(
    active_tasks: list[dict[str, Any]],
    *,
    triage_id: str,
    implementation_ids: list[str],
    issue: int,
    generation: int,
    validate_triage: Validate,
    validate_implementation: Validate,
    guide_locators: set[str],
) -> dict[str, Any]:
    """Prefill immutable facts only; semantic acceptance and checks remain PM-authored."""
    if not implementation_ids or len(implementation_ids) != len(set(implementation_ids)):
        raise ValueError("INVALID_REVIEW_IMPLEMENTATION_IDS")
    by_id = {
        task.get("id"): task
        for task in active_tasks
        if isinstance(task, dict) and isinstance(task.get("id"), str)
    }
    for task_id in [triage_id, *implementation_ids]:
        if task_id not in by_id:
            raise ValueError(f"REVIEW_SOURCE_NOT_ACTIVE:{task_id}")

    expected_impl_ids = {
        task["id"]
        for task in active_tasks
        if isinstance(task, dict)
        and isinstance(task.get("id"), str)
        and (match := TITLE.fullmatch(str(task.get("title", "")))) is not None
        and match.group("kind").startswith("Impl")
        and int(match.group("issue")) == issue
        and int(match.group("generation")) == generation
    }
    supplied_impl_ids = set(implementation_ids)
    if supplied_impl_ids != expected_impl_ids:
        missing = sorted(expected_impl_ids - supplied_impl_ids)
        extra = sorted(supplied_impl_ids - expected_impl_ids)
        raise ValueError(f"REVIEW_IMPLEMENTATION_CLOSURE_MISMATCH:missing={missing}:extra={extra}")

    triage_task = by_id[triage_id]
    triage_body = _body(triage_task, "SOURCE_TRIAGE")
    triage_errors = validate_triage(triage_body)
    if triage_errors:
        raise ValueError(f"SOURCE_TRIAGE_INVALID:{triage_errors[0]}")
    if triage_body.get("issue", {}).get("number") != issue:
        raise ValueError("SOURCE_TRIAGE_ISSUE_MISMATCH")
    planning_sha = triage_body.get("baseline", {}).get("planning_sha")
    if not isinstance(planning_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", planning_sha):
        raise ValueError("SOURCE_TRIAGE_BASELINE_INVALID")

    implementations: list[dict[str, Any]] = []
    for task_id in implementation_ids:
        task = by_id[task_id]
        match = TITLE.fullmatch(str(task.get("title", "")))
        if (
            match is None
            or not match.group("kind").startswith("Impl")
            or int(match.group("issue")) != issue
            or int(match.group("generation")) != generation
        ):
            raise ValueError(f"REVIEW_SOURCE_IMPL_TITLE_INVALID:{task_id}")
        body = _body(task, f"SOURCE_IMPL:{task_id}")
        errors = validate_implementation(body)
        if errors:
            raise ValueError(f"SOURCE_IMPL_INVALID:{task_id}:{errors[0]}")
        if _implementation_triage_digest(body) != triage_body.get("frozen_digest"):
            raise ValueError(f"SOURCE_IMPL_TRIAGE_DIGEST_MISMATCH:{task_id}")
        ordinal = int(match.group("kind")[len("Impl") :])
        implementations.append(
            {
                "key": f"impl-{ordinal}",
                "id": task_id,
                "title": task["title"],
                "task": _snapshot(task),
                "body": body,
                "behavior_ids": _implementation_behavior_ids(body),
                "acceptance_ids": _implementation_contract_ids(body),
                "verification_ids": [row.get("id") for row in body.get("verification", [])],
            }
        )
    implementations.sort(key=lambda item: int(item["key"].split("-")[1]))
    if len({item["key"] for item in implementations}) != len(implementations):
        raise ValueError("DUPLICATE_REVIEW_IMPLEMENTATION_KEY")

    triage_snapshot = _snapshot(triage_task)
    source_tasks = [triage_snapshot, *(item["task"] for item in implementations)]
    workspace_paths = {task.get("workspace_path") for task in source_tasks}
    workspace_kinds = {task.get("workspace_kind") for task in source_tasks}
    if len(workspace_paths) != 1 or workspace_kinds != {"dir"}:
        raise ValueError("REVIEW_SOURCE_WORKSPACE_MISMATCH")
    behavior_ids = [
        item.get("id")
        for item in triage_body.get("intent", {}).get("preserved_observables", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    if not behavior_ids:
        raise ValueError("REVIEW_SOURCE_BEHAVIORS_MISSING")
    keys = [item["key"] for item in implementations]
    return {
        "schema": "review-fast-path-v1",
        "issue": issue,
        "generation": generation,
        "title": f"G{generation}-Issue{issue}-Review1",
        "workspace": next(iter(workspace_paths)),
        "triage": {"id": triage_id, "task": triage_snapshot, "body": triage_body},
        "implementation_cards": implementations,
        "source_fingerprint": _fingerprint(source_tasks),
        "guide_locators": sorted(guide_locators),
        "review_body_template": {
            "schema": "aggregate-review-card-v5",
            "triage_digest": triage_body["frozen_digest"],
            "baseline_sha": planning_sha,
            "behavior_ids": behavior_ids,
            "implementation_card_keys": keys,
            "acceptance": [],
            "review_checks": [],
            "scope_exclusions": list(triage_body.get("intent", {}).get("out_of_scope", [])),
        },
    }


def validation_graph(bundle: dict[str, Any], review_body: dict[str, Any]) -> dict[str, Any]:
    triage = bundle["triage"]
    triage_body = triage["body"]
    implementations = bundle["implementation_cards"]
    keys = [item["key"] for item in implementations]
    behaviors = [
        {
            "id": item["id"],
            "outcome": item.get("behavior", item["id"]),
            "state": "implemented",
            "disposition": "inherited",
            "evidence": ["Frozen Triage source closure"],
            "requirement_ids": ["REVIEW-SOURCE"],
        }
        for item in triage_body.get("intent", {}).get("preserved_observables", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    cards = [
        {
            "key": "triage", "title": triage["task"]["title"], "card_type": "triage",
            "assignee": triage["task"].get("assignee"), "skills": triage["task"].get("skills", []),
            "workspace": bundle["workspace"], "status": triage["task"].get("status"),
            "priority": triage["task"].get("priority"), "parents": [], "body": None,
        }
    ]
    for item in implementations:
        task = item["task"]
        cards.append(
            {
                "key": item["key"], "title": item["title"], "card_type": "implementation",
                "assignee": task.get("assignee"), "skills": task.get("skills", []),
                "workspace": bundle["workspace"], "status": task.get("status"),
                "priority": task.get("priority"), "parents": ["triage"], "body": item["body"],
            }
        )
    cards.extend(
        [
            {
                "key": "review-1", "title": bundle["title"], "card_type": "review",
                "assignee": "reviewer", "skills": [], "workspace": bundle["workspace"],
                "status": "todo", "priority": 0, "parents": keys, "body": review_body,
            },
            {
                "key": "summary", "title": f"G{bundle['generation']}-Issue{bundle['issue']}-Summary",
                "card_type": "summary", "assignee": "project-manager", "skills": [],
                "workspace": bundle["workspace"], "status": "todo", "priority": 0,
                "parents": [*keys, "review-1"],
                "body": summary_body_from_triage(triage_body),
            },
        ]
    )
    return {
        "schema": "build-task-graph-v6", "mode": "new", "issue": triage_body["issue"],
        "generation": bundle["generation"], "workspace": bundle["workspace"],
        "planning_baseline_sha": triage_body["baseline"]["planning_sha"],
        "triage_digest": triage_body["frozen_digest"],
        "requirement_basis": {"path": "review-fast-path", "heading": "frozen source closure", "revision": triage_body["baseline"]["planning_sha"]},
        "revised_requirement": None, "lineage": None, "source_review": None,
        "change_requirements": triage_body.get("intent", {}).get("change_requirements", []),
        "behaviors": behaviors, "cards": cards,
        "execution_order": [*keys, "review-1"], "ready_candidate": keys[0], "archived_card_keys": [],
    }


def validate_review_fast_path(
    bundle: dict[str, Any],
    review_body: dict[str, Any],
    *,
    validate_review: Callable[..., list[str]],
    validate_implementation: Validate,
) -> list[str]:
    if bundle.get("schema") != "review-fast-path-v1":
        return ["REVIEW_FAST_PATH_SCHEMA"]
    template = bundle.get("review_body_template")
    if not isinstance(template, dict):
        return ["REVIEW_FAST_PATH_TEMPLATE_MISSING"]
    immutable = ("schema", "triage_digest", "baseline_sha", "behavior_ids", "implementation_card_keys")
    for field in immutable:
        if review_body.get(field) != template.get(field):
            return [f"REVIEW_FAST_PATH_IMMUTABLE_FIELD_MISMATCH:{field}"]
    return validate_review(validation_graph(bundle, review_body), validate_implementation=validate_implementation)


def create_summary_native(
    bundle: dict[str, Any],
    *,
    profile: str,
    board: str,
    body_file: Path,
    workspace_path: Path,
    dry_run: bool,
    validate_triage: Validate,
    active_tasks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Revalidate sources, create one Summary, and verify body plus both dependency directions."""
    started = time.monotonic()
    workspace = workspace_path.resolve()
    execution_root = Path.cwd().resolve()
    if Path(str(bundle.get("workspace", ""))).resolve() != workspace:
        raise ValueError("SUMMARY_CREATE_WORKSPACE_MISMATCH")
    resolved_body = body_file.resolve()
    if (execution_root / ".temp").resolve() not in resolved_body.parents:
        raise ValueError("SUMMARY_BODY_FILE_OUTSIDE_TEMP")
    expected_body = serialize_task_body(bundle.get("body"))
    if resolved_body.read_text(encoding="utf-8") != expected_body:
        raise ValueError("SUMMARY_BODY_FILE_MISMATCH")

    base = ["hermes", "--profile", profile, "kanban", "--board", board]
    native_calls = 0
    native_calls_lock = threading.Lock()

    def run_values(command: list[str]) -> list[Any]:
        nonlocal native_calls
        with native_calls_lock:
            native_calls += 1
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=execution_root,
        )
        return _json_values(completed.stdout)

    def list_tasks() -> list[dict[str, Any]]:
        return [item for item in run_values([*base, "list", "--json"]) if isinstance(item, dict)]

    current = prepare_summary_fast_path(
        active_tasks if active_tasks is not None else list_tasks(),
        triage_id=bundle.get("triage_id"),
        issue=bundle.get("issue"),
        generation=bundle.get("generation"),
        validate_triage=validate_triage,
    )
    identity_fields = ("schema", "issue", "generation", "title", "workspace", "triage_id", "parent_ids", "body")
    if any(current.get(field) != bundle.get(field) for field in identity_fields):
        raise ValueError("SUMMARY_BUNDLE_IDENTITY_STALE")
    if current.get("source_fingerprint") != bundle.get("source_fingerprint"):
        raise ValueError("SUMMARY_SOURCE_STALE")
    if dry_run:
        return {
            "valid": True,
            "dry_run": True,
            "title": bundle["title"],
            "parent_ids": bundle["parent_ids"],
            "native_calls": native_calls,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }

    created_id: str | None = None
    try:
        command = [
            *base,
            "create",
            bundle["title"],
            "--body-file",
            str(resolved_body),
            "--assignee",
            "project-manager",
            "--priority",
            "0",
            "--workspace",
            f"dir:{workspace.as_posix()}",
        ]
        for task_id in bundle["parent_ids"]:
            command.extend(["--parent", task_id])
        command.append("--json")
        values = run_values(command)
        if len(values) != 1:
            raise ValueError(f"SUMMARY_NATIVE_JSON_VALUE_COUNT:{len(values)}")
        created_id = _created_id(values[0], bundle["title"])
        shown = run_values([*base, "show", created_id, "--json"])
        if len(shown) != 1 or not isinstance(shown[0], dict):
            raise ValueError("SUMMARY_READBACK_COUNT")
        envelope = shown[0]
        task = envelope.get("task")
        checks = {
            "title": isinstance(task, dict) and task.get("title") == bundle["title"],
            "body": isinstance(task, dict) and task.get("body") == expected_body,
            "assignee": isinstance(task, dict) and task.get("assignee") == "project-manager",
            "status": isinstance(task, dict) and task.get("status") == "todo",
            "priority": isinstance(task, dict) and task.get("priority") == 0,
            "workspace": isinstance(task, dict) and task.get("workspace_path") == workspace.as_posix(),
            "parents": _same_task_ids(_parent_ids(envelope.get("parents")), bundle["parent_ids"]),
        }
        if not all(checks.values()):
            raise ValueError(f"NATIVE_SUMMARY_READBACK_MISMATCH:{created_id}")
        def verify_reverse_link(parent_id: str) -> str | None:
            parent_values = run_values([*base, "show", parent_id, "--json"])
            if len(parent_values) != 1 or created_id not in _parent_ids(parent_values[0].get("children")):
                return parent_id
            return None

        with ThreadPoolExecutor(max_workers=min(8, len(bundle["parent_ids"]))) as executor:
            reverse_link_failures = [
                parent_id
                for parent_id in executor.map(verify_reverse_link, bundle["parent_ids"])
                if parent_id is not None
            ]
        if reverse_link_failures:
            raise ValueError(f"NATIVE_SUMMARY_REVERSE_LINK_MISMATCH:{reverse_link_failures[0]}")
    except (OSError, subprocess.CalledProcessError, TypeError, ValueError):
        if created_id is not None:
            subprocess.run(
                [*base, "archive", created_id],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=execution_root,
            )
        raise
    return {
        "valid": True,
        "dry_run": False,
        "task_id": created_id,
        "title": bundle["title"],
        "parent_ids": bundle["parent_ids"],
        "native_calls": native_calls,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }


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
    validate_triage: Validate,
) -> dict[str, Any]:
    """Archive the active Summary and recreate it through one measured operation."""
    started = time.monotonic()
    execution_root = Path.cwd().resolve()
    temp_root = (execution_root / ".temp").resolve()
    outputs = [bundle_file.resolve(), body_file.resolve()]
    if any(temp_root not in path.parents for path in outputs):
        raise ValueError("SUMMARY_OUTPUT_OUTSIDE_TEMP")
    base = ["hermes", "--profile", profile, "kanban", "--board", board]
    native_calls = 0
    native_calls_lock = threading.Lock()
    stages: dict[str, int] = {}

    def run(command: list[str], *, json_output: bool) -> list[Any]:
        nonlocal native_calls
        with native_calls_lock:
            native_calls += 1
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=execution_root,
        )
        return _json_values(completed.stdout) if json_output else []

    listed_at = time.monotonic()
    active_tasks = [item for item in run([*base, "list", "--json"], json_output=True) if isinstance(item, dict)]
    stages["list_ms"] = round((time.monotonic() - listed_at) * 1000)
    title = f"G{generation}-Issue{issue}-Summary"
    summaries = [task for task in active_tasks if task.get("title") == title]
    if len(summaries) > 1:
        raise ValueError("MULTIPLE_ACTIVE_SUMMARIES")

    archived_id: str | None = None
    if summaries:
        archived_id = summaries[0].get("id")
        if not isinstance(archived_id, str):
            raise ValueError("ACTIVE_SUMMARY_ID_INVALID")
        archive_at = time.monotonic()
        run([*base, "archive", archived_id], json_output=False)
        with ThreadPoolExecutor(max_workers=2) as executor:
            archived_future = executor.submit(
                run,
                [*base, "show", archived_id, "--json"],
                json_output=True,
            )
            active_future = executor.submit(run, [*base, "list", "--json"], json_output=True)
            archived_values = archived_future.result()
            active_tasks = [item for item in active_future.result() if isinstance(item, dict)]
        archived_task = archived_values[0].get("task") if len(archived_values) == 1 else None
        if not isinstance(archived_task, dict) or archived_task.get("status") != "archived":
            raise ValueError(f"SUMMARY_ARCHIVE_READBACK_MISMATCH:{archived_id}")
        stages["archive_readback_ms"] = round((time.monotonic() - archive_at) * 1000)
    else:
        stages["archive_readback_ms"] = 0

    prepare_at = time.monotonic()
    bundle = prepare_summary_fast_path(
        active_tasks,
        triage_id=triage_id,
        issue=issue,
        generation=generation,
        validate_triage=validate_triage,
    )
    bundle_file.parent.mkdir(parents=True, exist_ok=True)
    bundle_file.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    body_file.parent.mkdir(parents=True, exist_ok=True)
    body_file.write_text(serialize_task_body(bundle["body"]), encoding="utf-8")
    stages["prepare_write_ms"] = round((time.monotonic() - prepare_at) * 1000)

    create_at = time.monotonic()
    created = create_summary_native(
        bundle,
        profile=profile,
        board=board,
        body_file=body_file,
        workspace_path=workspace_path,
        dry_run=False,
        validate_triage=validate_triage,
        active_tasks=active_tasks,
    )
    stages["create_readback_ms"] = round((time.monotonic() - create_at) * 1000)
    native_calls += created["native_calls"]
    return created | {
        "archived_task_id": archived_id,
        "native_calls": native_calls,
        "stages": stages,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }


def create_review_native(
    bundle: dict[str, Any],
    review_body: dict[str, Any],
    *,
    profile: str,
    board: str,
    body_file: Path,
    workspace_path: Path,
    dry_run: bool,
    validate_triage: Validate,
    validate_implementation: Validate,
    validate_review: Callable[..., list[str]],
    guide_locators: set[str],
) -> dict[str, Any]:
    started = time.monotonic()
    workspace = workspace_path.resolve()
    execution_root = Path.cwd().resolve()
    if Path(str(bundle.get("workspace", ""))).resolve() != workspace:
        raise ValueError("NATIVE_CREATE_WORKSPACE_MISMATCH")
    resolved_body = body_file.resolve()
    if (execution_root / ".temp").resolve() not in resolved_body.parents:
        raise ValueError("NATIVE_BODY_FILE_OUTSIDE_TEMP")
    expected_body = serialize_task_body(review_body)
    if resolved_body.read_text(encoding="utf-8") != expected_body:
        raise ValueError("NATIVE_REVIEW_BODY_FILE_MISMATCH")

    base = ["hermes", "--profile", profile, "kanban", "--board", board]
    native_calls = 0

    def run_values(command: list[str]) -> list[Any]:
        nonlocal native_calls
        native_calls += 1
        completed = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8", cwd=execution_root)
        return _json_values(completed.stdout)

    def list_tasks() -> list[dict[str, Any]]:
        return [item for item in run_values([*base, "list", "--json"]) if isinstance(item, dict)]

    implementation_ids = [item["id"] for item in bundle["implementation_cards"]]
    baseline = list_tasks()
    current = prepare_review_fast_path(
        baseline, triage_id=bundle["triage"]["id"], implementation_ids=implementation_ids,
        issue=bundle["issue"], generation=bundle["generation"],
        validate_triage=validate_triage, validate_implementation=validate_implementation,
        guide_locators=guide_locators,
    )
    expected_identity = {
        "schema": bundle.get("schema"),
        "issue": bundle.get("issue"),
        "generation": bundle.get("generation"),
        "title": bundle.get("title"),
        "workspace": bundle.get("workspace"),
        "triage_id": bundle.get("triage", {}).get("id"),
        "implementations": [
            (item.get("key"), item.get("id")) for item in bundle.get("implementation_cards", [])
        ],
    }
    current_identity = {
        "schema": current.get("schema"),
        "issue": current.get("issue"),
        "generation": current.get("generation"),
        "title": current.get("title"),
        "workspace": current.get("workspace"),
        "triage_id": current.get("triage", {}).get("id"),
        "implementations": [
            (item.get("key"), item.get("id")) for item in current.get("implementation_cards", [])
        ],
    }
    if current_identity != expected_identity:
        raise ValueError("REVIEW_BUNDLE_IDENTITY_STALE")
    if current["source_fingerprint"] != bundle.get("source_fingerprint"):
        raise ValueError("REVIEW_SOURCE_STALE")
    errors = validate_review_fast_path(
        current, review_body, validate_review=validate_review,
        validate_implementation=validate_implementation,
    )
    if errors:
        raise ValueError("INVALID_REVIEW_BODY:" + "|".join(errors))
    existing = [task for task in baseline if task.get("title") == bundle["title"]]
    if existing and not dry_run:
        raise ValueError("REVIEW_TITLE_ALREADY_EXISTS")
    if dry_run:
        return {
            "valid": True, "dry_run": True, "title": bundle["title"],
            "existing_ids": [task.get("id") for task in existing],
            "native_calls": native_calls,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }

    created_id: str | None = None
    try:
        command = [
            *base, "create", bundle["title"], "--body-file", str(resolved_body),
            "--assignee", "reviewer", "--priority", "0",
            "--workspace", f"dir:{workspace.as_posix()}",
        ]
        for task_id in implementation_ids:
            command.extend(["--parent", task_id])
        command.append("--json")
        values = run_values(command)
        if len(values) != 1:
            raise ValueError(f"NATIVE_JSON_VALUE_COUNT:{len(values)}")
        created_id = _created_id(values[0], bundle["title"])
        shown = run_values([*base, "show", created_id, "--json"])
        if len(shown) != 1 or not isinstance(shown[0], dict):
            raise ValueError("REVIEW_READBACK_COUNT")
        envelope = shown[0]
        task = envelope.get("task")
        checks = {
            "title": isinstance(task, dict) and task.get("title") == bundle["title"],
            "body": isinstance(task, dict) and task.get("body") == expected_body,
            "assignee": isinstance(task, dict) and task.get("assignee") == "reviewer",
            "status": isinstance(task, dict) and task.get("status") == "todo",
            "priority": isinstance(task, dict) and task.get("priority") == 0,
            "workspace": isinstance(task, dict) and task.get("workspace_path") == workspace.as_posix(),
            "parents": _same_task_ids(_parent_ids(envelope.get("parents")), implementation_ids),
        }
        if not all(checks.values()):
            raise ValueError(f"NATIVE_REVIEW_READBACK_MISMATCH:{created_id}")
        final = prepare_review_fast_path(
            list_tasks(), triage_id=bundle["triage"]["id"], implementation_ids=implementation_ids,
            issue=bundle["issue"], generation=bundle["generation"],
            validate_triage=validate_triage, validate_implementation=validate_implementation,
            guide_locators=guide_locators,
        )
        if final["source_fingerprint"] != bundle["source_fingerprint"]:
            raise ValueError("REVIEW_SOURCE_CHANGED")
    except (OSError, subprocess.CalledProcessError, TypeError, ValueError):
        if created_id is not None:
            subprocess.run([*base, "archive", created_id], check=True, capture_output=True, text=True, encoding="utf-8", cwd=execution_root)
        raise
    return {
        "valid": True, "dry_run": False,
        "created": {"id": created_id, "title": bundle["title"], "checks": checks},
        "native_calls": native_calls,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }
