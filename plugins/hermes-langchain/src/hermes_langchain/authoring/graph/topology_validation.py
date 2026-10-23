"""Card topology validation for build-task graph contracts."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class TopologyValidation:
    triages: tuple[str, ...]
    summaries: tuple[str, ...]
    reviews: tuple[str, ...]
    errors: tuple[str, ...]

def _non_empty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())

def _error(errors: list[str], condition: bool, code: str) -> None:
    if not condition:
        errors.append(code)

def _append_dependency_cycle_errors(records: dict[str, dict[str, Any]], errors: list[str]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(key: str) -> None:
        if key in visiting:
            errors.append(f"DEPENDENCY_CYCLE:{key}")
            return
        if key in visited:
            return
        visiting.add(key)
        for parent in records[key].get("parents", []):
            if parent in records:
                visit(parent)
        visiting.remove(key)
        visited.add(key)
    for key in records:
        visit(key)

def _role_keys(records: dict[str, dict[str, Any]], card_type: str) -> tuple[str, ...]:
    return tuple(key for key, card in records.items() if card.get("card_type") == card_type)

def _validate_parent_existence(records: dict[str, dict[str, Any]], errors: list[str]) -> None:
    for key, card in records.items():
        parents = card.get("parents", [])
        for parent in parents if isinstance(parents, list) else []:
            if parent not in records:
                errors.append(f"UNKNOWN_PARENT:{key}:{parent}")
            if parent == key:
                errors.append(f"SELF_PARENT:{key}")
    _append_dependency_cycle_errors(records, errors)

def _expected_priority(records: dict[str, dict[str, Any]], execution_order: list[Any], index: int, key: Any) -> int:
    del records, key
    return len(execution_order) - index

def _validate_execution_order(graph: dict[str, Any], records: dict[str, dict[str, Any]], errors: list[str]) -> None:
    executable = [key for key, card in records.items() if card.get("card_type") in {"implementation", "review"} and card.get("status") not in {"done", "archived"}]
    execution_order = graph.get("execution_order")
    _error(errors, isinstance(execution_order, list), "INVALID_EXECUTION_ORDER")
    if isinstance(execution_order, list):
        _error(errors, all(_non_empty(key) for key in execution_order), "INVALID_EXECUTION_ORDER_KEY")
        _error(errors, len(execution_order) == len(set(execution_order)), "DUPLICATE_EXECUTION_ORDER_KEY")
        _error(errors, set(execution_order) == set(executable), "EXECUTION_ORDER_COVERAGE")
        for index, key in enumerate(execution_order):
            if key not in records:
                errors.append(f"EXECUTION_ORDER_CARD_NOT_FOUND:{key}")
                continue
            _error(errors, records[key].get("priority") == _expected_priority(records, execution_order, index, key), f"EXECUTION_PRIORITY_MISMATCH:{key}")
        if execution_order:
            _error(errors, graph.get("ready_candidate") == execution_order[0], "READY_CANDIDATE_ORDER_MISMATCH")
    for key, card in records.items():
        if card.get("card_type") not in {"implementation", "review"}:
            _error(errors, card.get("priority") == 0, f"NON_EXECUTION_PRIORITY_MUST_BE_ZERO:{key}")

def _validate_role_membership(records: dict[str, dict[str, Any]], triages: tuple[str, ...], summaries: tuple[str, ...], mode: str, errors: list[str]) -> None:
    if triages:
        _error(errors, records[triages[0]].get("parents") == [], "TRIAGE_HAS_PARENT")
    if summaries:
        summary_parents = set(records[summaries[0]].get("parents", []))
        required_parents = {key for key, card in records.items() if card.get("card_type") in {"implementation", "review", "decision"}}
        _error(errors, required_parents == summary_parents, "SUMMARY_MEMBERSHIP_MISMATCH")
    if mode in {"new", "requirement-rework"}:
        _error(errors, not any(card.get("card_type") == "decision" for card in records.values()), "PLANNING_DECISION_MUST_NOT_BE_GRAPH_CARD")

def _validate_parent_types(records: dict[str, dict[str, Any]], reviews: tuple[str, ...], mode: str, errors: list[str]) -> None:
    for key in reviews:
        _error(errors, bool(records[key].get("parents")), f"REVIEW_WITHOUT_PARENT:{key}")
    for key, card in records.items():
        parent_types = {records[parent].get("card_type") for parent in card.get("parents", []) if parent in records}
        card_type = card.get("card_type")
        if card_type == "implementation":
            _error(errors, bool(parent_types) and parent_types <= {"triage", "implementation", "review", "decision"}, f"INVALID_IMPL_PARENTS:{key}")
        elif card_type == "review":
            allowed = {"implementation", "review", "decision"}
            if mode in {"new", "requirement-rework"}:
                allowed.add("triage")
            _error(errors, bool(parent_types) and parent_types <= allowed, f"INVALID_REVIEW_PARENTS:{key}")
        elif card_type == "decision":
            _error(errors, "review" in parent_types, f"DECISION_WITHOUT_REVIEW_PARENT:{key}")

def _validate_impl_review_children(records: dict[str, dict[str, Any]], reviews: tuple[str, ...], errors: list[str]) -> None:
    for impl_key, impl in records.items():
        if impl.get("card_type") != "implementation":
            continue
        child_reviews = [key for key in reviews if impl_key in records[key].get("parents", [])]
        _error(errors, bool(child_reviews), f"IMPLEMENTATION_WITHOUT_REVIEW_CHILD:{impl_key}")

def validate_card_topology(graph: dict[str, Any], records: dict[str, dict[str, Any]], *, mode: str) -> TopologyValidation:
    errors: list[str] = []
    triages = _role_keys(records, "triage")
    summaries = _role_keys(records, "summary")
    reviews = _role_keys(records, "review")
    _error(errors, len(triages) == 1, "TRIAGE_COUNT")
    _error(errors, len(summaries) == 1, "SUMMARY_COUNT")
    _error(errors, bool(reviews), "REVIEW_COUNT")
    _validate_parent_existence(records, errors)
    _validate_execution_order(graph, records, errors)
    _validate_role_membership(records, triages, summaries, mode, errors)
    _validate_parent_types(records, reviews, mode, errors)
    _validate_impl_review_children(records, reviews, errors)
    return TopologyValidation(triages, summaries, reviews, tuple(errors))
