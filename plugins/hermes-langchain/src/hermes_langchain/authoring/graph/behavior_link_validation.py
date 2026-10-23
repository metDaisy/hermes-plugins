"""Behavior-to-Implementation linkage validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BehaviorLinkValidation:
    errors: tuple[str, ...]
    implementation_keys: frozenset[str]
    planned_impl_keys: frozenset[str]


def _behavior_impl_keys(behavior: dict[str, Any]) -> set[str]:
    plural = behavior.get("implementation_card_keys")
    if isinstance(plural, list) and all(isinstance(item, str) for item in plural):
        return set(plural)
    singular = behavior.get("implementation_card_key")
    return {singular} if isinstance(singular, str) else set()


def _implementation_behavior_ids(body: Any) -> set[str]:
    if not isinstance(body, dict):
        return set()
    if body.get("schema") == "backend-implementation-card-v6":
        trace = body.get("trace")
        return set(trace.get("observable_ids", [])) if isinstance(trace, dict) else set()
    body_slice = body.get("slice")
    return (
        set(body_slice.get("behavior_ids", []))
        if isinstance(body_slice, dict)
        else set()
    )


def validate_behavior_implementation_links(
    behaviors: Any,
    records: dict[str, dict[str, Any]],
    *,
    mode: Any,
) -> BehaviorLinkValidation:
    """Validate Behavior ownership and exact Implementation-card coverage."""

    errors: list[str] = []
    for behavior in behaviors or []:
        if not isinstance(behavior, dict) or behavior.get("state") not in {"partial", "absent"}:
            continue
        behavior_id = behavior.get("id")
        for impl_key in _behavior_impl_keys(behavior):
            impl = records.get(impl_key)
            if impl is None or impl.get("card_type") != "implementation":
                errors.append(f"BEHAVIOR_IMPL_NOT_FOUND:{behavior_id}:{impl_key}")
            if impl and isinstance(impl.get("body"), dict):
                linked_ids = _implementation_behavior_ids(impl["body"])
                if behavior_id not in linked_ids:
                    errors.append(f"BEHAVIOR_IMPL_MISMATCH:{behavior_id}:{impl_key}")

    implementation_keys = {
        key
        for key, card in records.items()
        if card.get("card_type") == "implementation"
    }
    planned_impl_keys = {
        impl_key
        for behavior in behaviors or []
        if isinstance(behavior, dict) and behavior.get("disposition") == "planned"
        for impl_key in _behavior_impl_keys(behavior)
    }
    if mode in {"new", "requirement-rework"}:
        if implementation_keys != planned_impl_keys:
            errors.append("IMPLEMENTATION_BEHAVIOR_COVERAGE")
        for impl_key in implementation_keys:
            actual_ids = _implementation_behavior_ids(records[impl_key].get("body"))
            assigned_ids = {
                behavior.get("id")
                for behavior in behaviors or []
                if isinstance(behavior, dict)
                and behavior.get("disposition") == "planned"
                and impl_key in _behavior_impl_keys(behavior)
            }
            if actual_ids != assigned_ids:
                errors.append(f"IMPLEMENTATION_BEHAVIOR_COVERAGE:{impl_key}")

    return BehaviorLinkValidation(
        errors=tuple(errors),
        implementation_keys=frozenset(implementation_keys),
        planned_impl_keys=frozenset(planned_impl_keys),
    )
