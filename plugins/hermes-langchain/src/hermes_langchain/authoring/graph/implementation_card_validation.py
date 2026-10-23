"""Implementation card lineage and requirement-coverage validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ImplementationCardContext:
    """Immutable graph facts shared by every Implementation card."""

    issue_number: int
    issue_url: str
    triage_digest: str
    behavior_ids: frozenset[str]
    requirement_ids: frozenset[str]
    behaviors: tuple[Any, ...]


def validate_implementation_card(
    card: dict[str, Any],
    context: ImplementationCardContext,
    *,
    validate_external: Callable[[Any], list[str]] | None = None,
) -> tuple[str, ...]:
    """Validate one Implementation card without mutating graph state."""

    body = card.get("body")
    if card.get("card_type") != "implementation" or not isinstance(body, dict):
        return ()

    key = card.get("key")
    errors: list[str] = []
    is_v6 = body.get("schema") == "backend-implementation-card-v6"
    trace = body.get("trace") if is_v6 else None
    if validate_external:
        errors.extend(
            f"IMPLEMENTATION:{key}:{failure}"
            for failure in validate_external(body)
        )

    body_issue = body.get("issue")
    if not is_v6 and isinstance(body_issue, dict):
        if body_issue.get("number") != context.issue_number:
            errors.append(f"IMPLEMENTATION_ISSUE_NUMBER_MISMATCH:{key}")
        if body_issue.get("url") != context.issue_url:
            errors.append(f"IMPLEMENTATION_ISSUE_URL_MISMATCH:{key}")
    body_triage_digest = (
        trace.get("triage_digest")
        if isinstance(trace, dict)
        else body.get("triage_digest")
    )
    if body_triage_digest != context.triage_digest:
        errors.append(f"IMPLEMENTATION_TRIAGE_DIGEST_MISMATCH:{key}")

    if is_v6:
        body_behavior_ids = (
            set(trace.get("observable_ids", [])) if isinstance(trace, dict) else set()
        )
    else:
        body_slice = body.get("slice")
        body_behavior_ids = (
            set(body_slice.get("behavior_ids", []))
            if isinstance(body_slice, dict)
            else set()
        )
    if not body_behavior_ids <= context.behavior_ids:
        errors.append(f"IMPLEMENTATION_UNKNOWN_BEHAVIOR:{key}")

    body_requirement_ids: set[str] = set()
    if is_v6:
        requirement_groups = (
            [trace.get("requirement_ids", [])]
            if isinstance(trace, dict)
            else []
        )
    else:
        requirement_groups = [
            acceptance.get("requirement_ids", [])
            for acceptance in body.get("acceptance", [])
            if isinstance(acceptance, dict)
        ]
    for requirement_group in requirement_groups:
        for requirement_id in requirement_group:
            if isinstance(requirement_id, str):
                body_requirement_ids.add(requirement_id)
                if requirement_id not in context.requirement_ids:
                    errors.append(
                        f"IMPLEMENTATION_UNKNOWN_REQUIREMENT:{key}:{requirement_id}"
                    )

    assigned_requirement_ids = {
        requirement_id
        for behavior in context.behaviors
        if isinstance(behavior, dict) and behavior.get("id") in body_behavior_ids
        for requirement_id in behavior.get("requirement_ids", [])
        if isinstance(requirement_id, str)
    }
    if not body_requirement_ids <= assigned_requirement_ids:
        errors.append(f"IMPLEMENTATION_REQUIREMENT_COVERAGE:{key}")
    return tuple(errors)
