"""Native Kanban read-back validation for authored task graphs."""

from __future__ import annotations

import json
from typing import Any

from .contract_common import serialize_task_body


def _non_empty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _error(errors: list[str], condition: bool, code: str) -> None:
    if not condition:
        errors.append(code)


def _index_native_tasks(
    readback: list[Any],
    errors: list[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    actual_by_title: dict[str, dict[str, Any]] = {}
    actual_by_id: dict[str, dict[str, Any]] = {}
    for index, envelope in enumerate(readback):
        task = envelope.get("task") if isinstance(envelope, dict) else None
        if not isinstance(task, dict):
            errors.append(f"NATIVE_TASK_NOT_OBJECT:{index}")
            continue
        task_id, title = task.get("id"), task.get("title")
        if not _non_empty(task_id):
            errors.append(f"NATIVE_TASK_ID_MISSING:{index}")
            continue
        if not _non_empty(title):
            errors.append(f"NATIVE_TASK_TITLE_MISSING:{index}")
            continue
        if task_id in actual_by_id:
            errors.append(f"DUPLICATE_NATIVE_TASK_ID:{task_id}")
        if title in actual_by_title:
            errors.append(f"DUPLICATE_NATIVE_TASK_TITLE:{title}")
        actual_by_id[task_id] = envelope
        actual_by_title[title] = envelope
    return actual_by_title, actual_by_id


def _validate_native_body(
    key: Any,
    card: dict[str, Any],
    body: Any,
    errors: list[str],
) -> None:
    if card.get("body") is None and card.get("card_type") == "triage":
        _error(errors, isinstance(body, str) and bool(body.strip()), f"NATIVE_TRIAGE_BODY_MISSING:{key}")
        return
    if card.get("body") is None:
        _error(errors, body in {None, ""}, f"NATIVE_BODY_MISMATCH:{key}")
        return
    try:
        native_body = json.loads(body) if isinstance(body, str) else body
    except json.JSONDecodeError:
        native_body = None
        errors.append(f"NATIVE_BODY_NOT_JSON:{key}")
    expected_body = card.get("body")
    if native_body != expected_body:
        errors.append(f"NATIVE_BODY_MISMATCH:{key}")
        return
    try:
        expected_text = serialize_task_body(expected_body)
    except (TypeError, ValueError):
        errors.append(f"NATIVE_BODY_SERIALIZATION_FAILED:{key}")
    else:
        _error(
            errors,
            isinstance(body, str) and body == expected_text,
            f"NATIVE_BODY_FORMAT_MISMATCH:{key}",
        )


def _expected_parent_ids(
    card: dict[str, Any],
    planned_by_key: dict[Any, dict[str, Any]],
    actual_by_title: dict[str, dict[str, Any]],
) -> list[Any]:
    expected_parent_ids: list[Any] = []
    for parent_key in card.get("parents", []):
        parent = planned_by_key.get(parent_key)
        parent_envelope = actual_by_title.get(parent.get("title")) if isinstance(parent, dict) else None
        if isinstance(parent_envelope, dict):
            expected_parent_ids.append(parent_envelope["task"].get("id"))
    return expected_parent_ids


def _validate_native_card(
    key: Any,
    card: dict[str, Any],
    envelope: dict[str, Any],
    planned_by_key: dict[Any, dict[str, Any]],
    actual_by_title: dict[str, dict[str, Any]],
    errors: list[str],
) -> None:
    task = envelope["task"]
    for field in ("assignee", "status", "priority"):
        _error(errors, task.get(field) == card.get(field), f"NATIVE_{field.upper()}_MISMATCH:{key}")
    _error(errors, task.get("skills") == card.get("skills"), f"NATIVE_SKILLS_MISMATCH:{key}")
    _validate_native_body(key, card, task.get("body"), errors)
    parent_ids = envelope.get("parents")
    _error(
        errors,
        isinstance(parent_ids, list) and all(_non_empty(item) for item in parent_ids),
        f"NATIVE_PARENTS_INVALID:{key}",
    )
    if isinstance(parent_ids, list):
        expected_parent_ids = _expected_parent_ids(card, planned_by_key, actual_by_title)
        _error(errors, set(parent_ids) == set(expected_parent_ids), f"NATIVE_PARENT_MISMATCH:{key}")


def validate_native_readback(graph: Any, readback: Any) -> list[str]:
    """Compare every persisted graph card with full public Kanban show read-back."""

    errors: list[str] = []
    if not isinstance(graph, dict) or not isinstance(graph.get("cards"), list):
        return ["GRAPH_SCHEMA"]
    if not isinstance(readback, list):
        return ["NATIVE_READBACK_NOT_LIST"]
    planned_by_key = {
        card.get("key"): card
        for card in graph["cards"]
        if isinstance(card, dict) and _non_empty(card.get("key"))
    }
    actual_by_title, _ = _index_native_tasks(readback, errors)
    planned_titles = {card.get("title") for card in planned_by_key.values()}
    _error(errors, planned_titles == set(actual_by_title), "NATIVE_READBACK_MEMBERSHIP_MISMATCH")
    for key, card in planned_by_key.items():
        envelope = actual_by_title.get(card.get("title"))
        if isinstance(envelope, dict):
            _validate_native_card(
                key,
                card,
                envelope,
                planned_by_key,
                actual_by_title,
                errors,
            )
    return errors
