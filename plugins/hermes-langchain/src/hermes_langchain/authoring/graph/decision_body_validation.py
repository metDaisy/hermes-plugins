"""Policy Decision card-body validation."""

from __future__ import annotations

from typing import Any

from .card_body_common import append_error, check_exact_fields, non_empty, strings


def validate_decision_body(key: Any, body: dict[str, Any]) -> tuple[str, ...]:
    errors: list[str] = []
    allowed = {"schema", "source_review_key", "finding_ids", "question", "decision_owner"}
    check_exact_fields(body, allowed, errors, f"UNEXPECTED_DECISION_BODY_FIELD:{key}")
    append_error(errors, non_empty(body.get("source_review_key")), f"MISSING_DECISION_REVIEW:{key}")
    append_error(errors, strings(body.get("finding_ids")), f"MISSING_DECISION_FINDINGS:{key}")
    append_error(errors, non_empty(body.get("question")), f"MISSING_DECISION_QUESTION:{key}")
    append_error(errors, body.get("decision_owner") == "user", f"INVALID_DECISION_OWNER:{key}")
    return tuple(errors)
