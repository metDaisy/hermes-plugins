"""Summary cross-card lineage and projection validation."""

from __future__ import annotations

from typing import Any


def validate_summary_links(
    graph: dict[str, Any],
    records: dict[str, dict[str, Any]],
    summaries: tuple[str, ...],
    *,
    triage_digest: Any,
    issue_number: Any,
    issue_url: Any,
) -> tuple[str, ...]:
    """Validate the Summary projection against canonical graph facts."""

    errors: list[str] = []
    if not summaries:
        return ()
    summary_body = records[summaries[0]].get("body", {})
    if not isinstance(summary_body, dict):
        return ()

    if summary_body.get("triage_digest") != triage_digest:
        errors.append("SUMMARY_TRIAGE_DIGEST_MISMATCH")
    summary_issue = summary_body.get("issue")
    if not (
        isinstance(summary_issue, dict)
        and summary_issue.get("number") == issue_number
        and summary_issue.get("url") == issue_url
    ):
        errors.append("SUMMARY_ISSUE_MISMATCH")

    expected_outcomes = [
        {"id": row.get("id"), "outcome": row.get("outcome")}
        for row in graph.get("change_requirements", [])
        if isinstance(row, dict)
    ]
    if summary_body.get("target_outcomes") != expected_outcomes:
        errors.append("SUMMARY_TARGET_OUTCOMES_MISMATCH")

    expected_behaviors = [
        {"id": row.get("id"), "behavior": row.get("outcome")}
        for row in graph.get("behaviors", [])
        if isinstance(row, dict)
    ]
    if summary_body.get("preserved_behaviors") != expected_behaviors:
        errors.append("SUMMARY_PRESERVED_BEHAVIORS_MISMATCH")
    return tuple(errors)
