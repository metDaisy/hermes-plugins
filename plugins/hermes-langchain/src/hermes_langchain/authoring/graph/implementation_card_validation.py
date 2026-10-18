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
    if validate_external:
        errors.extend(
            f"IMPLEMENTATION:{key}:{failure}"
            for failure in validate_external(body)
        )

    body_issue = body.get("issue")
    if isinstance(body_issue, dict):
        if body_issue.get("number") != context.issue_number:
            errors.append(f"IMPLEMENTATION_ISSUE_NUMBER_MISMATCH:{key}")
        if body_issue.get("url") != context.issue_url:
            errors.append(f"IMPLEMENTATION_ISSUE_URL_MISMATCH:{key}")
    if body.get("triage_digest") != context.triage_digest:
        errors.append(f"IMPLEMENTATION_TRIAGE_DIGEST_MISMATCH:{key}")

    body_slice = body.get("slice")
    body_behavior_ids = (
        set(body_slice.get("behavior_ids", []))
        if isinstance(body_slice, dict)
        else set()
    )
    if not body_behavior_ids <= context.behavior_ids:
        errors.append(f"IMPLEMENTATION_UNKNOWN_BEHAVIOR:{key}")

    body_requirement_ids: set[str] = set()
    for acceptance in body.get("acceptance", []):
        if not isinstance(acceptance, dict):
            continue
        for requirement_id in acceptance.get("requirement_ids", []):
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
