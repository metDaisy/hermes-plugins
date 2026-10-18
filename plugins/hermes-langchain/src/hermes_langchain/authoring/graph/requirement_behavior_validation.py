"""Requirement and behavior validation for build-task graph contracts."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any

STATES = {"implemented", "partial", "absent", "unknown"}
DISPOSITIONS = {"inherited", "planned", "blocked"}

@dataclass(frozen=True)
class RequirementBehaviorValidation:
    behaviors: Any
    requirement_ids: frozenset[str]
    behavior_ids: frozenset[str]
    errors: tuple[str, ...]

def _non_empty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())

def _strings(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(_non_empty(item) for item in value)

def _unique_strings(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(_non_empty(item) for item in value) and len(value) == len(set(value))

def _error(errors: list[str], condition: bool, code: str) -> None:
    if not condition:
        errors.append(code)

def _exact_fields(value: dict[str, Any], allowed: set[str], errors: list[str], prefix: str) -> None:
    for field in sorted(set(value) - allowed):
        errors.append(f"{prefix}:{field}")

def _validate_requirement(index: int, requirement: Any, requirement_ids: set[str], errors: list[str]) -> None:
    if not isinstance(requirement, dict):
        errors.append(f"CHANGE_REQUIREMENT_NOT_OBJECT:{index}")
        return
    _exact_fields(requirement, {"id", "outcome", "source"}, errors, f"UNEXPECTED_CHANGE_REQUIREMENT_FIELD:{index}")
    identifier = requirement.get("id")
    _error(errors, _non_empty(identifier), f"MISSING_CHANGE_REQUIREMENT_ID:{index}")
    _error(errors, _non_empty(requirement.get("outcome")), f"MISSING_CHANGE_REQUIREMENT_OUTCOME:{index}")
    if isinstance(identifier, str) and identifier:
        if identifier in requirement_ids:
            errors.append(f"DUPLICATE_CHANGE_REQUIREMENT_ID:{identifier}")
        requirement_ids.add(identifier)
    source = requirement.get("source")
    if not isinstance(source, dict):
        errors.append(f"CHANGE_REQUIREMENT_SOURCE_NOT_OBJECT:{index}")
        return
    _exact_fields(source, {"locator", "claim"}, errors, f"UNEXPECTED_CHANGE_REQUIREMENT_SOURCE_FIELD:{index}")
    _error(errors, _non_empty(source.get("locator")), f"MISSING_CHANGE_REQUIREMENT_LOCATOR:{index}")
    _error(errors, _non_empty(source.get("claim")), f"MISSING_CHANGE_REQUIREMENT_CLAIM:{index}")

def _validate_requirements(requirements: Any, errors: list[str]) -> set[str]:
    requirement_ids: set[str] = set()
    if not isinstance(requirements, list) or not requirements:
        errors.append("MISSING_CHANGE_REQUIREMENTS")
    if isinstance(requirements, list):
        for index, requirement in enumerate(requirements):
            _validate_requirement(index, requirement, requirement_ids, errors)
    return requirement_ids

def _validate_behavior_requirements(identifier: Any, references: Any, requirement_ids: set[str], covered_requirement_ids: set[str], errors: list[str]) -> None:
    _error(errors, _strings(references), f"MISSING_BEHAVIOR_REQUIREMENT_IDS:{identifier}")
    if not isinstance(references, list):
        return
    if len(references) != len(set(references)):
        errors.append(f"DUPLICATE_BEHAVIOR_REQUIREMENT_ID:{identifier}")
    for requirement_id in references:
        if not isinstance(requirement_id, str):
            continue
        if requirement_id not in requirement_ids:
            errors.append(f"UNKNOWN_CHANGE_REQUIREMENT:{identifier}:{requirement_id}")
        else:
            covered_requirement_ids.add(requirement_id)

def _validate_behavior_work_state(identifier: Any, behavior: dict[str, Any], state: Any, disposition: Any, errors: list[str]) -> None:
    if state == "implemented":
        _error(errors, disposition == "inherited", f"IMPLEMENTED_NOT_INHERITED:{identifier}")
        _error(errors, _strings(behavior.get("evidence")), f"MISSING_INHERITED_EVIDENCE:{identifier}")
    elif state in {"partial", "absent"}:
        _error(errors, disposition == "planned", f"WORK_NOT_PLANNED:{identifier}")
        singular = behavior.get("implementation_card_key")
        plural = behavior.get("implementation_card_keys")
        _error(errors, not (singular is not None and plural is not None), f"AMBIGUOUS_BEHAVIOR_IMPL:{identifier}")
        _error(errors, _non_empty(singular) or _unique_strings(plural), f"MISSING_BEHAVIOR_IMPL:{identifier}")
    elif state == "unknown":
        _error(errors, disposition == "blocked", f"UNKNOWN_NOT_BLOCKED:{identifier}")
    if disposition != "planned":
        _error(errors, "implementation_card_key" not in behavior and "implementation_card_keys" not in behavior, f"UNEXPECTED_BEHAVIOR_IMPL:{identifier}")

def _validate_behavior(index: int, behavior: Any, requirement_ids: set[str], behavior_ids: set[str], covered_requirement_ids: set[str], errors: list[str]) -> None:
    if not isinstance(behavior, dict):
        errors.append(f"BEHAVIOR_NOT_OBJECT:{index}")
        return
    _exact_fields(behavior, {"id", "outcome", "state", "disposition", "evidence", "implementation_card_key", "implementation_card_keys", "requirement_ids"}, errors, f"UNEXPECTED_BEHAVIOR_FIELD:{index}")
    identifier = behavior.get("id")
    _error(errors, _non_empty(identifier), f"MISSING_BEHAVIOR_ID:{index}")
    if identifier in behavior_ids:
        errors.append(f"DUPLICATE_BEHAVIOR_ID:{identifier}")
    if isinstance(identifier, str):
        behavior_ids.add(identifier)
    state = behavior.get("state")
    disposition = behavior.get("disposition")
    _error(errors, state in STATES, f"INVALID_BEHAVIOR_STATE:{identifier}")
    _error(errors, disposition in DISPOSITIONS, f"INVALID_BEHAVIOR_DISPOSITION:{identifier}")
    _error(errors, _non_empty(behavior.get("outcome")), f"MISSING_BEHAVIOR_OUTCOME:{identifier}")
    _validate_behavior_requirements(identifier, behavior.get("requirement_ids"), requirement_ids, covered_requirement_ids, errors)
    _validate_behavior_work_state(identifier, behavior, state, disposition, errors)

def _validate_behaviors(behaviors: Any, requirement_ids: set[str], errors: list[str]) -> tuple[set[str], set[str]]:
    behavior_ids: set[str] = set()
    covered_requirement_ids: set[str] = set()
    if not isinstance(behaviors, list) or not behaviors:
        errors.append("MISSING_BEHAVIORS")
    else:
        for index, behavior in enumerate(behaviors):
            _validate_behavior(index, behavior, requirement_ids, behavior_ids, covered_requirement_ids, errors)
    return behavior_ids, covered_requirement_ids

def validate_requirement_behaviors(graph: dict[str, Any]) -> RequirementBehaviorValidation:
    errors: list[str] = []
    requirement_ids = _validate_requirements(graph.get("change_requirements"), errors)
    behaviors = graph.get("behaviors")
    behavior_ids, covered_requirement_ids = _validate_behaviors(behaviors, requirement_ids, errors)
    for requirement_id in sorted(requirement_ids - covered_requirement_ids):
        errors.append(f"UNCOVERED_CHANGE_REQUIREMENT:{requirement_id}")
    return RequirementBehaviorValidation(behaviors, frozenset(requirement_ids), frozenset(behavior_ids), tuple(errors))
