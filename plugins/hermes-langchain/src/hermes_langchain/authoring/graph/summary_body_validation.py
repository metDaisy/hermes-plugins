"""Issue Summary card-body validation."""

from __future__ import annotations

from typing import Any

from .card_body_common import (
    KOREAN,
    SHA256,
    append_error,
    check_exact_fields,
    descriptive_korean,
    non_empty,
)
from .contract_common import is_canonical_https_url


SUMMARY_REQUIRED_CONDITIONS = [
    "all-direct-parents-done",
    "latest-review-approved",
    "all-blocking-findings-resolved",
    "final-implementation-sha-frozen",
    "documentation-lineage-verified",
    "clean-worktree",
    "user-local-approval",
]
SUMMARY_RELEASE_EXCLUDED_ACTIONS = [
    "push",
    "pull-request",
    "ci",
    "external-review",
    "merge",
    "issue-close",
]
def validate_summary_body(key: Any, body: dict[str, Any]) -> tuple[str, ...]:
    errors: list[str] = []
    allowed = {
        "schema",
        "issue",
        "triage_digest",
        "objective",
        "target_outcomes",
        "preserved_behaviors",
        "completion_contract",
        "release_boundary",
    }
    check_exact_fields(body, allowed, errors, f"UNEXPECTED_SUMMARY_BODY_FIELD:{key}")
    issue = body.get("issue")
    if isinstance(issue, dict):
        check_exact_fields(issue, {"number", "title", "url"}, errors, f"UNEXPECTED_SUMMARY_ISSUE_FIELD:{key}")
    append_error(errors, isinstance(issue, dict), f"INVALID_SUMMARY_ISSUE:{key}")
    if isinstance(issue, dict):
        append_error(errors, isinstance(issue.get("number"), int) and issue["number"] > 0, f"INVALID_SUMMARY_ISSUE_NUMBER:{key}")
        append_error(
            errors,
            non_empty(issue.get("title")) and bool(KOREAN.search(issue["title"])),
            f"INVALID_SUMMARY_ISSUE_TITLE:{key}",
        )
        append_error(errors, is_canonical_https_url(issue.get("url")), f"INVALID_SUMMARY_ISSUE_URL:{key}")
    append_error(errors, isinstance(body.get("triage_digest"), str) and bool(SHA256.fullmatch(body["triage_digest"])), f"INVALID_SUMMARY_TRIAGE_DIGEST:{key}")
    append_error(errors, descriptive_korean(body.get("objective")), f"INVALID_SUMMARY_OBJECTIVE:{key}")
    for field, text_field, error_code in (
        ("target_outcomes", "outcome", "INVALID_SUMMARY_TARGET_OUTCOMES"),
        ("preserved_behaviors", "behavior", "INVALID_SUMMARY_PRESERVED_BEHAVIORS"),
    ):
        rows = body.get(field)
        valid_rows = isinstance(rows, list) and bool(rows)
        seen_ids: set[str] = set()
        if isinstance(rows, list):
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    valid_rows = False
                    continue
                check_exact_fields(row, {"id", text_field}, errors, f"UNEXPECTED_{error_code}_FIELD:{key}:{index}")
                row_id = row.get("id")
                text = row.get(text_field)
                if (
                    not non_empty(row_id)
                    or row_id in seen_ids
                    or not non_empty(text)
                    or not bool(KOREAN.search(text))
                ):
                    valid_rows = False
                if isinstance(row_id, str):
                    seen_ids.add(row_id)
        append_error(errors, valid_rows, f"{error_code}:{key}")
    completion = body.get("completion_contract")
    if isinstance(completion, dict):
        check_exact_fields(completion, {"admission_schema", "required_conditions"}, errors, f"UNEXPECTED_SUMMARY_COMPLETION_FIELD:{key}")
    append_error(
        errors,
        isinstance(completion, dict)
        and completion.get("admission_schema") == "summary-admission-v1"
        and completion.get("required_conditions") == SUMMARY_REQUIRED_CONDITIONS,
        f"INVALID_SUMMARY_COMPLETION_CONTRACT:{key}",
    )
    release = body.get("release_boundary")
    if isinstance(release, dict):
        check_exact_fields(release, {"mode", "next_card_type", "excluded_actions"}, errors, f"UNEXPECTED_SUMMARY_RELEASE_FIELD:{key}")
    append_error(
        errors,
        isinstance(release, dict)
        and release.get("mode") == "deferred"
        and release.get("next_card_type") == "issue-release"
        and release.get("excluded_actions") == SUMMARY_RELEASE_EXCLUDED_ACTIONS,
        f"INVALID_SUMMARY_RELEASE_BOUNDARY:{key}",
    )
    return tuple(errors)
