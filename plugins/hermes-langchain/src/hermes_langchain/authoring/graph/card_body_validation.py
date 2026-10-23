"""Thin dispatcher for build-task card-body validation."""

from __future__ import annotations

from typing import Any

from .card_body_common import append_error, contains_ephemeral_coordinator_context
from .decision_body_validation import validate_decision_body
from .review_body_validation import REVIEW_GUIDE_LOCATORS, validate_review_body
from .summary_body_validation import (
    SUMMARY_RELEASE_EXCLUDED_ACTIONS,
    SUMMARY_REQUIRED_CONDITIONS,
    validate_summary_body,
)


CARD_SCHEMAS = {
    "review": "aggregate-review-card-v5",
    "summary": "issue-summary-card-v3",
    "decision": "policy-decision-card-v1",
}
IMPLEMENTATION_SCHEMAS = {
    "backend-implementation-card-v5",
    "backend-implementation-card-v6",
}


def validate_card_body(
    card: dict[str, Any],
    behavior_ids: set[str],
) -> tuple[str, ...]:
    """Validate one card body while preserving legacy error ordering."""

    errors: list[str] = []
    key = card.get("key")
    card_type = card.get("card_type")
    body = card.get("body")

    if card_type == "triage":
        append_error(errors, body is None, f"TRIAGE_BODY_MUST_BE_EXTERNAL:{key}")
        return tuple(errors)
    if card_type == "implementation":
        append_error(errors, isinstance(body, dict), f"MISSING_IMPLEMENTATION_BODY:{key}")
        if isinstance(body, dict):
            append_error(
                errors,
                body.get("schema") in IMPLEMENTATION_SCHEMAS,
                f"IMPLEMENTATION_SCHEMA:{key}",
            )
        return tuple(errors)
    if not isinstance(body, dict):
        return (f"MISSING_CARD_BODY:{key}",)

    append_error(
        errors,
        body.get("schema") == CARD_SCHEMAS.get(card_type),
        f"CARD_BODY_SCHEMA:{key}",
    )
    if card_type in {"review", "summary"}:
        append_error(
            errors,
            not contains_ephemeral_coordinator_context(body),
            f"EPHEMERAL_COORDINATOR_CONTEXT:{key}",
        )

    if card_type == "review":
        errors.extend(validate_review_body(key, body))
    elif card_type == "summary":
        errors.extend(validate_summary_body(key, body))
    elif card_type == "decision":
        errors.extend(validate_decision_body(key, body))

    referenced = body.get("behavior_ids", body.get("effective_behavior_ids", []))
    if isinstance(referenced, list):
        for behavior_id in referenced:
            if behavior_id not in behavior_ids:
                errors.append(f"UNKNOWN_BEHAVIOR:{key}:{behavior_id}")
    return tuple(errors)
