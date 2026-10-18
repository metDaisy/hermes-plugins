#!/usr/bin/env python3
"""그래프 초안과 requirement-rework 비교 산출물을 검증·생성한다."""

from __future__ import annotations

from collections import Counter
import hashlib
import importlib.util
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from contract_common import is_canonical_https_url, serialize_task_body


def _validate_aggregate_review_result(result: Any, graph: Any) -> list[str]:
    workflow_path = Path(__file__).resolve().parents[2] / "kwc-run-workflow" / "scripts" / "workflow.py"
    spec = importlib.util.spec_from_file_location("pm_workflow_contract", workflow_path)
    if spec is None or spec.loader is None:
        return ["WORKFLOW_VALIDATOR_UNAVAILABLE"]
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    expected = result.get("prior_findings") if isinstance(result, dict) else None
    return module.validate_review_result(result, graph, expected)

GRAPH_SCHEMA = "build-task-graph-v6"
CARD_SCHEMAS = {
    "review": "aggregate-review-card-v5",
    "summary": "issue-summary-card-v3",
    "decision": "policy-decision-card-v1",
}
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
TITLE = re.compile(r"^G(?P<generation>[1-9]\d*)-Issue(?P<issue>[1-9]\d*)-(?P<kind>Triage|Impl[1-9]\d*|Review[1-9]\d*|Summary|Decision[1-9]\d*)$")
SHA = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
KOREAN = re.compile(r"[가-힣]")
STATES = {"implemented", "partial", "absent", "unknown"}
DISPOSITIONS = {"inherited", "planned", "blocked"}
REVIEW_FOCI = {
    "behavior",
    "implementation-convention",
    "cross-card-seam",
    "test-quality",
    "preservation",
}
REVIEW_GUIDE_PREFIXES = (
    "docs/backend-development-guide.md#",
    "docs/backend-test-guide.md#",
    ".hermes/docs/plan.md#",
)
REVIEW_GUIDE_LOCATORS = {
    "docs/backend-development-guide.md#Backend Development Guide",
    "docs/backend-development-guide.md#목적과 문서 경계",
    "docs/backend-development-guide.md#기술 기준과 변경 원칙",
    "docs/backend-development-guide.md#Module과 계층",
    "docs/backend-development-guide.md#Domain 구현 규칙",
    "docs/backend-development-guide.md#Application 구현 규칙",
    "docs/backend-development-guide.md#Infra 구현 규칙",
    "docs/backend-development-guide.md#Presentation 구현 규칙",
    "docs/backend-development-guide.md#Dependency injection과 Spring component",
    "docs/backend-development-guide.md#Transaction",
    "docs/backend-development-guide.md#DTO와 MapStruct",
    "docs/backend-development-guide.md#Port, adapter와 module seam",
    "docs/backend-development-guide.md#Persistence와 Flyway",
    "docs/backend-development-guide.md#API, error와 security",
    "docs/backend-development-guide.md#Java style와 자동 검증",
    "docs/backend-development-guide.md#변경별 확인 기준",
    "docs/backend-test-guide.md#Backend Test Guide",
    "docs/backend-test-guide.md#목적과 기준",
    "docs/backend-test-guide.md#1. 공통 규칙",
    "docs/backend-test-guide.md#2. Behavior별 RED–GREEN loop",
    "docs/backend-test-guide.md#3. Test 구조와 Mockito",
    "docs/backend-test-guide.md#4. Parameterized test",
    "docs/backend-test-guide.md#5. MapStruct 제외 범위",
    "docs/backend-test-guide.md#6. Unit test",
    "docs/backend-test-guide.md#7. Slice test",
    "docs/backend-test-guide.md#Web layer",
    "docs/backend-test-guide.md#8. Integration test",
    "docs/backend-test-guide.md#9. Repository test",
    "docs/backend-test-guide.md#10. Spring Modulith와 구조 test",
    "docs/backend-test-guide.md#11. Test level 선택과 검증",
}
CARD_TYPES = {"triage", "implementation", "review", "summary", "decision"}
EXPECTED_ASSIGNEES = {
    "triage": "project-manager",
    "implementation": "coder",
    "review": "reviewer",
    "summary": "project-manager",
    "decision": "project-manager",
}
STATUSES = {"triage", "running", "todo", "ready", "blocked", "done", "archived"}
TITLE_TYPES = {
    "Triage": "triage",
    "Summary": "summary",
}
EPHEMERAL_COORDINATOR_MARKERS = (
    "이번 단계",
    "이번 호출",
    "이번 세션",
    "이번 graph recovery",
    "이번 그래프 복구",
    "this phase",
    "this invocation",
    "this call",
    "this session",
    "graph recovery phase",
)


def _non_empty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _descriptive_korean(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value.strip()) >= 20
        and len(KOREAN.findall(value)) >= 5
    )


def _strings(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(_non_empty(item) for item in value)


def _unique_strings(value: Any, *, allow_empty: bool = False) -> bool:
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and all(_non_empty(item) for item in value)
        and len(value) == len(set(value))
    )


def _string_set(value: Any) -> set[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return set()
    return set(value)


def _dict_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _error(errors: list[str], condition: bool, code: str) -> None:
    if not condition:
        errors.append(code)


def _exact_fields(value: dict[str, Any], allowed: set[str], errors: list[str], prefix: str) -> None:
    for field in sorted(set(value) - allowed):
        errors.append(f"{prefix}:{field}")


def _contains_ephemeral_coordinator_context(value: Any) -> bool:
    if isinstance(value, str):
        normalized = value.casefold()
        return any(marker.casefold() in normalized for marker in EPHEMERAL_COORDINATOR_MARKERS)
    if isinstance(value, dict):
        return any(_contains_ephemeral_coordinator_context(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_ephemeral_coordinator_context(child) for child in value)
    return False


def _validate_locator(locator: Any, errors: list[str], prefix: str) -> None:
    if not isinstance(locator, dict):
        errors.append(f"{prefix}_NOT_OBJECT")
        return
    _exact_fields(locator, {"path", "heading", "revision"}, errors, f"UNEXPECTED_{prefix}_FIELD")
    _error(errors, _non_empty(locator.get("path")), f"MISSING_{prefix}_PATH")
    _error(errors, _non_empty(locator.get("heading")), f"MISSING_{prefix}_HEADING")
    _error(errors, isinstance(locator.get("revision"), str) and bool(SHA.fullmatch(locator["revision"])), f"INVALID_{prefix}_REVISION")


def _validate_body(card: dict[str, Any], behavior_ids: set[str], errors: list[str]) -> None:
    key, card_type, body = card.get("key"), card.get("card_type"), card.get("body")
    if card_type == "triage":
        _error(errors, body is None, f"TRIAGE_BODY_MUST_BE_EXTERNAL:{key}")
        return
    if card_type == "implementation":
        _error(errors, isinstance(body, dict), f"MISSING_IMPLEMENTATION_BODY:{key}")
        if isinstance(body, dict):
            _error(errors, body.get("schema") == "backend-implementation-card-v5", f"IMPLEMENTATION_SCHEMA:{key}")
        return
    if not isinstance(body, dict):
        errors.append(f"MISSING_CARD_BODY:{key}")
        return
    _error(errors, body.get("schema") == CARD_SCHEMAS.get(card_type), f"CARD_BODY_SCHEMA:{key}")
    if card_type in {"review", "summary"}:
        _error(
            errors,
            not _contains_ephemeral_coordinator_context(body),
            f"EPHEMERAL_COORDINATOR_CONTEXT:{key}",
        )
    if card_type == "review":
        allowed = {
            "schema",
            "triage_digest",
            "baseline_sha",
            "behavior_ids",
            "implementation_card_keys",
            "acceptance",
            "review_checks",
            "scope_exclusions",
        }
        _exact_fields(body, allowed, errors, f"UNEXPECTED_REVIEW_BODY_FIELD:{key}")
        _error(errors, isinstance(body.get("triage_digest"), str) and bool(SHA256.fullmatch(body["triage_digest"])), f"INVALID_REVIEW_TRIAGE_DIGEST:{key}")
        _error(errors, isinstance(body.get("baseline_sha"), str) and bool(SHA.fullmatch(body["baseline_sha"])), f"INVALID_REVIEW_BASELINE:{key}")
        _error(errors, _unique_strings(body.get("behavior_ids")), f"MISSING_REVIEW_BEHAVIORS:{key}")
        _error(errors, _unique_strings(body.get("implementation_card_keys"), allow_empty=True), f"MISSING_REVIEW_IMPLS:{key}")

        acceptance = body.get("acceptance")
        _error(errors, isinstance(acceptance, list) and bool(acceptance), f"MISSING_REVIEW_ACCEPTANCE:{key}")
        acceptance_ids: set[str] = set()
        acceptance_criteria: set[str] = set()
        if isinstance(acceptance, list):
            acceptance_fields = {
                "id",
                "criterion",
                "behavior_ids",
                "implementation_card_keys",
                "source_acceptance_ids",
                "verification_ids",
            }
            for index, item in enumerate(acceptance):
                if not isinstance(item, dict):
                    errors.append(f"INVALID_REVIEW_ACCEPTANCE:{key}:{index}")
                    continue
                _exact_fields(item, acceptance_fields, errors, f"UNEXPECTED_REVIEW_ACCEPTANCE_FIELD:{key}:{index}")
                acceptance_id = item.get("id")
                _error(errors, _non_empty(acceptance_id), f"MISSING_REVIEW_ACCEPTANCE_ID:{key}:{index}")
                if isinstance(acceptance_id, str):
                    if acceptance_id in acceptance_ids:
                        errors.append(f"DUPLICATE_REVIEW_ACCEPTANCE_ID:{key}:{acceptance_id}")
                    acceptance_ids.add(acceptance_id)
                criterion = item.get("criterion")
                _error(errors, _descriptive_korean(criterion), f"LOW_QUALITY_REVIEW_ACCEPTANCE_CRITERION:{key}:{index}")
                if isinstance(criterion, str):
                    normalized = criterion.strip()
                    if normalized in acceptance_criteria:
                        errors.append(f"DUPLICATE_REVIEW_ACCEPTANCE_CRITERION:{key}:{index}")
                    acceptance_criteria.add(normalized)
                _error(errors, _unique_strings(item.get("behavior_ids")), f"INVALID_REVIEW_ACCEPTANCE_BEHAVIORS:{key}:{index}")
                _error(errors, _unique_strings(item.get("implementation_card_keys"), allow_empty=True), f"INVALID_REVIEW_ACCEPTANCE_IMPLS:{key}:{index}")
                _error(errors, _unique_strings(item.get("source_acceptance_ids"), allow_empty=True), f"INVALID_REVIEW_SOURCE_ACCEPTANCES:{key}:{index}")
                _error(errors, _unique_strings(item.get("verification_ids"), allow_empty=True), f"INVALID_REVIEW_ACCEPTANCE_VERIFICATIONS:{key}:{index}")

        checks = body.get("review_checks")
        _error(errors, isinstance(checks, list) and bool(checks), f"MISSING_REVIEW_CHECKS:{key}")
        check_ids: set[str] = set()
        check_questions: set[str] = set()
        check_outcomes: set[str] = set()
        guide_locators: set[str] = set()
        if isinstance(checks, list):
            check_fields = {
                "id",
                "focus",
                "question",
                "expected_outcome",
                "behavior_ids",
                "implementation_card_keys",
                "acceptance_ids",
                "source_acceptance_ids",
                "verification_ids",
                "guide_locators",
            }
            for index, item in enumerate(checks):
                if not isinstance(item, dict):
                    errors.append(f"INVALID_REVIEW_CHECK:{key}:{index}")
                    continue
                _exact_fields(item, check_fields, errors, f"UNEXPECTED_REVIEW_CHECK_FIELD:{key}:{index}")
                check_id = item.get("id")
                _error(errors, _non_empty(check_id), f"MISSING_REVIEW_CHECK_ID:{key}:{index}")
                if isinstance(check_id, str):
                    if check_id in check_ids:
                        errors.append(f"DUPLICATE_REVIEW_CHECK_ID:{key}:{check_id}")
                    check_ids.add(check_id)
                _error(errors, item.get("focus") in REVIEW_FOCI, f"INVALID_REVIEW_CHECK_FOCUS:{key}:{check_id}")
                question = item.get("question")
                expected_outcome = item.get("expected_outcome")
                _error(
                    errors,
                    _descriptive_korean(question) and question.rstrip().endswith("?"),
                    f"LOW_QUALITY_REVIEW_CHECK_QUESTION:{key}:{check_id}",
                )
                _error(errors, _descriptive_korean(expected_outcome), f"LOW_QUALITY_REVIEW_CHECK_EXPECTED_OUTCOME:{key}:{check_id}")
                if isinstance(question, str):
                    normalized = question.strip()
                    if normalized in check_questions:
                        errors.append(f"DUPLICATE_REVIEW_CHECK_QUESTION:{key}:{check_id}")
                    check_questions.add(normalized)
                if isinstance(expected_outcome, str):
                    normalized = expected_outcome.strip()
                    if normalized in check_outcomes:
                        errors.append(f"DUPLICATE_REVIEW_CHECK_EXPECTED_OUTCOME:{key}:{check_id}")
                    check_outcomes.add(normalized)
                for field in ("behavior_ids", "acceptance_ids"):
                    _error(errors, _unique_strings(item.get(field)), f"INVALID_REVIEW_CHECK_{field.upper()}:{key}:{check_id}")
                for field in ("implementation_card_keys", "source_acceptance_ids", "verification_ids"):
                    _error(errors, _unique_strings(item.get(field), allow_empty=True), f"INVALID_REVIEW_CHECK_{field.upper()}:{key}:{check_id}")
                locators = item.get("guide_locators")
                _error(errors, _unique_strings(locators), f"MISSING_REVIEW_GUIDE_LOCATORS:{key}:{check_id}")
                if isinstance(locators, list):
                    for locator in locators:
                        valid_active_plan_locator = (
                            isinstance(locator, str)
                            and locator.startswith(".hermes/docs/plan.md#")
                            and bool(locator.removeprefix(".hermes/docs/plan.md#").strip())
                        )
                        if not isinstance(locator, str) or (
                            locator not in REVIEW_GUIDE_LOCATORS and not valid_active_plan_locator
                        ):
                            errors.append(f"INVALID_REVIEW_GUIDE_LOCATOR:{key}:{check_id}:{locator}")
                        else:
                            guide_locators.add(locator)
        _error(
            errors,
            any(locator.startswith("docs/backend-development-guide.md#") for locator in guide_locators),
            f"REVIEW_DEVELOPMENT_GUIDE_COVERAGE:{key}",
        )
        _error(
            errors,
            any(locator.startswith("docs/backend-test-guide.md#") for locator in guide_locators),
            f"REVIEW_TEST_GUIDE_COVERAGE:{key}",
        )
        _error(
            errors,
            any(locator.startswith(".hermes/docs/plan.md#") for locator in guide_locators),
            f"REVIEW_ACTIVE_PLAN_COVERAGE:{key}",
        )
        exclusions = body.get("scope_exclusions")
        _error(
            errors,
            isinstance(exclusions, list)
            and all(_non_empty(item) for item in exclusions)
            and len(exclusions) == len(set(exclusions)),
            f"INVALID_REVIEW_SCOPE_EXCLUSIONS:{key}",
        )
    elif card_type == "summary":
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
        _exact_fields(body, allowed, errors, f"UNEXPECTED_SUMMARY_BODY_FIELD:{key}")
        issue = body.get("issue")
        if isinstance(issue, dict):
            _exact_fields(issue, {"number", "title", "url"}, errors, f"UNEXPECTED_SUMMARY_ISSUE_FIELD:{key}")
        _error(errors, isinstance(issue, dict), f"INVALID_SUMMARY_ISSUE:{key}")
        if isinstance(issue, dict):
            _error(errors, isinstance(issue.get("number"), int) and issue["number"] > 0, f"INVALID_SUMMARY_ISSUE_NUMBER:{key}")
            _error(
                errors,
                _non_empty(issue.get("title")) and bool(KOREAN.search(issue["title"])),
                f"INVALID_SUMMARY_ISSUE_TITLE:{key}",
            )
            _error(errors, is_canonical_https_url(issue.get("url")), f"INVALID_SUMMARY_ISSUE_URL:{key}")
        _error(errors, isinstance(body.get("triage_digest"), str) and bool(SHA256.fullmatch(body["triage_digest"])), f"INVALID_SUMMARY_TRIAGE_DIGEST:{key}")
        _error(errors, _descriptive_korean(body.get("objective")), f"INVALID_SUMMARY_OBJECTIVE:{key}")
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
                    _exact_fields(row, {"id", text_field}, errors, f"UNEXPECTED_{error_code}_FIELD:{key}:{index}")
                    row_id = row.get("id")
                    text = row.get(text_field)
                    if (
                        not _non_empty(row_id)
                        or row_id in seen_ids
                        or not _non_empty(text)
                        or not bool(KOREAN.search(text))
                    ):
                        valid_rows = False
                    if isinstance(row_id, str):
                        seen_ids.add(row_id)
            _error(errors, valid_rows, f"{error_code}:{key}")
        completion = body.get("completion_contract")
        if isinstance(completion, dict):
            _exact_fields(completion, {"admission_schema", "required_conditions"}, errors, f"UNEXPECTED_SUMMARY_COMPLETION_FIELD:{key}")
        _error(
            errors,
            isinstance(completion, dict)
            and completion.get("admission_schema") == "summary-admission-v1"
            and completion.get("required_conditions") == SUMMARY_REQUIRED_CONDITIONS,
            f"INVALID_SUMMARY_COMPLETION_CONTRACT:{key}",
        )
        release = body.get("release_boundary")
        if isinstance(release, dict):
            _exact_fields(release, {"mode", "next_card_type", "excluded_actions"}, errors, f"UNEXPECTED_SUMMARY_RELEASE_FIELD:{key}")
        _error(
            errors,
            isinstance(release, dict)
            and release.get("mode") == "deferred"
            and release.get("next_card_type") == "issue-release"
            and release.get("excluded_actions") == SUMMARY_RELEASE_EXCLUDED_ACTIONS,
            f"INVALID_SUMMARY_RELEASE_BOUNDARY:{key}",
        )
    elif card_type == "decision":
        allowed = {"schema", "source_review_key", "finding_ids", "question", "decision_owner"}
        _exact_fields(body, allowed, errors, f"UNEXPECTED_DECISION_BODY_FIELD:{key}")
        _error(errors, _non_empty(body.get("source_review_key")), f"MISSING_DECISION_REVIEW:{key}")
        _error(errors, _strings(body.get("finding_ids")), f"MISSING_DECISION_FINDINGS:{key}")
        _error(errors, _non_empty(body.get("question")), f"MISSING_DECISION_QUESTION:{key}")
        _error(errors, body.get("decision_owner") == "user", f"INVALID_DECISION_OWNER:{key}")
    referenced = body.get("behavior_ids", body.get("effective_behavior_ids", []))
    if isinstance(referenced, list):
        for behavior_id in referenced:
            if behavior_id not in behavior_ids:
                errors.append(f"UNKNOWN_BEHAVIOR:{key}:{behavior_id}")


def validate_graph(
    graph: Any,
    *,
    phase: str = "draft",
    validate_implementation: Callable[[Any], list[str]] | None = None,
    source_review_result: Any = None,
) -> list[str]:
    """결정론적으로 graph wrapper와 persisted card body를 검증한다."""
    if phase not in {"draft", "native"}:
        return ["INVALID_PHASE"]
    errors: list[str] = []
    if not isinstance(graph, dict):
        return ["GRAPH_NOT_OBJECT"]
    allowed = {"schema", "mode", "issue", "generation", "workspace", "planning_baseline_sha", "triage_digest", "requirement_basis", "revised_requirement", "lineage", "source_review", "change_requirements", "behaviors", "cards", "execution_order", "ready_candidate", "archived_card_keys"}
    _exact_fields(graph, allowed, errors, "UNEXPECTED_GRAPH_FIELD")
    schema = graph.get("schema")
    _error(errors, schema == GRAPH_SCHEMA, "GRAPH_SCHEMA")
    mode = graph.get("mode")
    _error(errors, mode in {"new", "requirement-rework", "review-rework"}, "INVALID_MODE")
    issue = graph.get("issue")
    issue_number = issue.get("number") if isinstance(issue, dict) else None
    if isinstance(issue, dict):
        _exact_fields(issue, {"number", "url"}, errors, "UNEXPECTED_ISSUE_FIELD")
    _error(errors, isinstance(issue_number, int) and not isinstance(issue_number, bool) and issue_number > 0, "INVALID_ISSUE")
    issue_url = issue.get("url") if isinstance(issue, dict) else None
    _error(errors, is_canonical_https_url(issue_url), "INVALID_ISSUE_URL")
    generation = graph.get("generation")
    _error(errors, isinstance(generation, int) and not isinstance(generation, bool) and generation > 0, "INVALID_GENERATION")
    workspace = graph.get("workspace")
    _error(errors, _non_empty(workspace), "MISSING_WORKSPACE")
    planning_baseline_sha = graph.get("planning_baseline_sha")
    _error(errors, isinstance(planning_baseline_sha, str) and bool(SHA.fullmatch(planning_baseline_sha)), "INVALID_PLANNING_BASELINE_SHA")
    triage_digest = graph.get("triage_digest")
    _error(errors, isinstance(triage_digest, str) and bool(SHA256.fullmatch(triage_digest)), "INVALID_TRIAGE_DIGEST")
    if mode == "new":
        _error(errors, generation == 1, "NEW_REQUIRES_GENERATION_1")
        _error(errors, graph.get("revised_requirement") is None, "NEW_FORBIDS_REVISED_REQUIREMENT")
        _error(errors, graph.get("lineage") is None, "NEW_FORBIDS_LINEAGE")
        _error(errors, graph.get("source_review") is None, "NEW_FORBIDS_SOURCE_REVIEW")
    elif mode == "requirement-rework":
        _error(errors, isinstance(generation, int) and generation > 1, "REWORK_REQUIRES_NEXT_GENERATION")
        _validate_locator(graph.get("revised_requirement"), errors, "REVISED_REQUIREMENT")
        lineage = graph.get("lineage")
        if not isinstance(lineage, dict):
            errors.append("MISSING_REWORK_LINEAGE")
        else:
            _exact_fields(lineage, {"prior_generation", "prior_summary", "archived_unfinished_keys"}, errors, "UNEXPECTED_LINEAGE_FIELD")
            _error(errors, isinstance(generation, int) and lineage.get("prior_generation") == generation - 1, "REWORK_GENERATION_DISCONTINUITY")
            _error(errors, _non_empty(lineage.get("prior_summary")), "MISSING_PRIOR_SUMMARY")
            _error(errors, isinstance(lineage.get("archived_unfinished_keys"), list), "MISSING_ARCHIVED_LINEAGE")
        _error(errors, graph.get("source_review") is None, "REQUIREMENT_REWORK_FORBIDS_SOURCE_REVIEW")
    else:
        _error(errors, graph.get("revised_requirement") is None, "REVIEW_REWORK_FORBIDS_REQUIREMENT_REVISION")
        lineage = graph.get("lineage")
        if not isinstance(lineage, dict):
            errors.append("MISSING_REVIEW_REWORK_LINEAGE")
        else:
            _exact_fields(lineage, {"prior_generation", "prior_summary", "archived_unfinished_keys"}, errors, "UNEXPECTED_LINEAGE_FIELD")
            _error(errors, lineage.get("prior_generation") == generation, "REVIEW_REWORK_GENERATION_MISMATCH")
            _error(errors, _non_empty(lineage.get("prior_summary")), "MISSING_PRIOR_SUMMARY")
        source_review = graph.get("source_review")
        if not isinstance(source_review, dict):
            errors.append("MISSING_SOURCE_REVIEW")
        else:
            _exact_fields(source_review, {"review_key", "dispositions", "idempotency"}, errors, "UNEXPECTED_SOURCE_REVIEW_FIELD")
            _error(errors, _non_empty(source_review.get("review_key")), "MISSING_SOURCE_REVIEW_KEY")
            dispositions = source_review.get("dispositions")
            _error(errors, isinstance(dispositions, list) and bool(dispositions), "MISSING_SOURCE_DISPOSITIONS")
            finding_ids: set[str] = set()
            if isinstance(dispositions, list):
                for index, disposition in enumerate(dispositions):
                    if not isinstance(disposition, dict):
                        errors.append(f"SOURCE_DISPOSITION_NOT_OBJECT:{index}")
                        continue
                    _exact_fields(disposition, {"finding_id", "verdict", "appended_card_keys"}, errors, f"UNEXPECTED_SOURCE_DISPOSITION_FIELD:{index}")
                    finding_id = disposition.get("finding_id")
                    _error(errors, _non_empty(finding_id), f"MISSING_SOURCE_FINDING_ID:{index}")
                    if finding_id in finding_ids:
                        errors.append(f"DUPLICATE_SOURCE_FINDING_ID:{finding_id}")
                    if isinstance(finding_id, str):
                        finding_ids.add(finding_id)
                    _error(errors, disposition.get("verdict") in {"correction-required", "context-required", "decision-required"}, f"INVALID_SOURCE_VERDICT:{finding_id}")
                    _error(errors, _strings(disposition.get("appended_card_keys")), f"MISSING_DISPOSITION_CARDS:{finding_id}")
            idempotency = source_review.get("idempotency")
            _error(errors, isinstance(idempotency, dict) and bool(idempotency), "MISSING_IDEMPOTENCY_MAP")
            if isinstance(idempotency, dict):
                _error(errors, all(_non_empty(key) and _non_empty(value) for key, value in idempotency.items()), "INVALID_IDEMPOTENCY_MAP")
                _error(errors, len(set(idempotency.values())) == len(idempotency), "DUPLICATE_IDEMPOTENCY_KEY")
        if not isinstance(source_review_result, dict):
            errors.append("MISSING_CANONICAL_SOURCE_REVIEW_RESULT")
        else:
            canonical_errors = _validate_aggregate_review_result(source_review_result, graph)
            errors.extend(f"INVALID_CANONICAL_SOURCE_REVIEW_RESULT:{error}" for error in canonical_errors)
            _error(errors, source_review_result.get("result") == "changes-required", "SOURCE_REVIEW_RESULT_NOT_CHANGES_REQUIRED")
            _error(
                errors,
                isinstance(source_review, dict)
                and source_review.get("review_key") == source_review_result.get("review_card_key"),
                "SOURCE_REVIEW_KEY_MISMATCH",
            )
            canonical_dispositions = [
                (finding.get("finding_id"), finding.get("verdict"))
                for finding in source_review_result.get("findings", [])
                if isinstance(finding, dict)
                and finding.get("verdict") in {"correction-required", "context-required", "decision-required"}
            ]
            graph_dispositions = [
                (disposition.get("finding_id"), disposition.get("verdict"))
                for disposition in (
                    source_review.get("dispositions", [])
                    if isinstance(source_review, dict)
                    else []
                )
                if isinstance(disposition, dict)
            ]
            _error(errors, Counter(graph_dispositions) == Counter(canonical_dispositions), "SOURCE_REVIEW_DISPOSITION_MISMATCH")
    _validate_locator(graph.get("requirement_basis"), errors, "REQUIREMENT_BASIS")

    requirements = graph.get("change_requirements")
    requirement_ids: set[str] = set()
    if not isinstance(requirements, list) or not requirements:
        errors.append("MISSING_CHANGE_REQUIREMENTS")
    if isinstance(requirements, list):
        for index, requirement in enumerate(requirements):
            if not isinstance(requirement, dict):
                errors.append(f"CHANGE_REQUIREMENT_NOT_OBJECT:{index}")
                continue
            _exact_fields(
                requirement,
                {"id", "outcome", "source"},
                errors,
                f"UNEXPECTED_CHANGE_REQUIREMENT_FIELD:{index}",
            )
            identifier = requirement.get("id")
            _error(errors, _non_empty(identifier), f"MISSING_CHANGE_REQUIREMENT_ID:{index}")
            _error(
                errors,
                _non_empty(requirement.get("outcome")),
                f"MISSING_CHANGE_REQUIREMENT_OUTCOME:{index}",
            )
            if isinstance(identifier, str) and identifier:
                if identifier in requirement_ids:
                    errors.append(f"DUPLICATE_CHANGE_REQUIREMENT_ID:{identifier}")
                requirement_ids.add(identifier)
            source = requirement.get("source")
            if not isinstance(source, dict):
                errors.append(f"CHANGE_REQUIREMENT_SOURCE_NOT_OBJECT:{index}")
                continue
            _exact_fields(
                source,
                {"locator", "claim"},
                errors,
                f"UNEXPECTED_CHANGE_REQUIREMENT_SOURCE_FIELD:{index}",
            )
            _error(errors, _non_empty(source.get("locator")), f"MISSING_CHANGE_REQUIREMENT_LOCATOR:{index}")
            _error(errors, _non_empty(source.get("claim")), f"MISSING_CHANGE_REQUIREMENT_CLAIM:{index}")

    behaviors = graph.get("behaviors")
    behavior_ids: set[str] = set()
    covered_requirement_ids: set[str] = set()
    if not isinstance(behaviors, list) or not behaviors:
        errors.append("MISSING_BEHAVIORS")
    else:
        for index, behavior in enumerate(behaviors):
            if not isinstance(behavior, dict):
                errors.append(f"BEHAVIOR_NOT_OBJECT:{index}")
                continue
            _exact_fields(behavior, {"id", "outcome", "state", "disposition", "evidence", "implementation_card_key", "requirement_ids"}, errors, f"UNEXPECTED_BEHAVIOR_FIELD:{index}")
            identifier = behavior.get("id")
            _error(errors, _non_empty(identifier), f"MISSING_BEHAVIOR_ID:{index}")
            if identifier in behavior_ids:
                errors.append(f"DUPLICATE_BEHAVIOR_ID:{identifier}")
            if isinstance(identifier, str):
                behavior_ids.add(identifier)
            state, disposition = behavior.get("state"), behavior.get("disposition")
            _error(errors, state in STATES, f"INVALID_BEHAVIOR_STATE:{identifier}")
            _error(errors, disposition in DISPOSITIONS, f"INVALID_BEHAVIOR_DISPOSITION:{identifier}")
            _error(errors, _non_empty(behavior.get("outcome")), f"MISSING_BEHAVIOR_OUTCOME:{identifier}")
            references = behavior.get("requirement_ids")
            _error(errors, _strings(references), f"MISSING_BEHAVIOR_REQUIREMENT_IDS:{identifier}")
            if isinstance(references, list):
                if len(references) != len(set(references)):
                    errors.append(f"DUPLICATE_BEHAVIOR_REQUIREMENT_ID:{identifier}")
                for requirement_id in references:
                    if not isinstance(requirement_id, str):
                        continue
                    if requirement_id not in requirement_ids:
                        errors.append(f"UNKNOWN_CHANGE_REQUIREMENT:{identifier}:{requirement_id}")
                    else:
                        covered_requirement_ids.add(requirement_id)
            evidence = behavior.get("evidence")
            if state == "implemented":
                _error(errors, disposition == "inherited", f"IMPLEMENTED_NOT_INHERITED:{identifier}")
                _error(errors, _strings(evidence), f"MISSING_INHERITED_EVIDENCE:{identifier}")
            elif state in {"partial", "absent"}:
                _error(errors, disposition == "planned", f"WORK_NOT_PLANNED:{identifier}")
                _error(errors, _non_empty(behavior.get("implementation_card_key")), f"MISSING_BEHAVIOR_IMPL:{identifier}")
            elif state == "unknown":
                _error(errors, disposition == "blocked", f"UNKNOWN_NOT_BLOCKED:{identifier}")
            if disposition != "planned":
                _error(errors, "implementation_card_key" not in behavior, f"UNEXPECTED_BEHAVIOR_IMPL:{identifier}")
    for requirement_id in sorted(requirement_ids - covered_requirement_ids):
        errors.append(f"UNCOVERED_CHANGE_REQUIREMENT:{requirement_id}")

    cards = graph.get("cards")
    if not isinstance(cards, list) or not cards:
        return errors + ["MISSING_CARDS"]
    records: dict[str, dict[str, Any]] = {}
    titles: set[str] = set()
    for index, card in enumerate(cards):
        if not isinstance(card, dict):
            errors.append(f"CARD_NOT_OBJECT:{index}")
            continue
        _exact_fields(card, {"key", "title", "card_type", "assignee", "skills", "workspace", "status", "priority", "parents", "body"}, errors, f"UNEXPECTED_CARD_FIELD:{index}")
        key = card.get("key")
        if not _non_empty(key):
            errors.append(f"MISSING_CARD_KEY:{index}")
            continue
        if key in records:
            errors.append(f"DUPLICATE_CARD_KEY:{key}")
        records[key] = card
        match = TITLE.fullmatch(str(card.get("title", "")))
        title = card.get("title")
        if title in titles:
            errors.append(f"DUPLICATE_CARD_TITLE:{title}")
        if isinstance(title, str):
            titles.add(title)
        if not match:
            errors.append(f"INVALID_TITLE:{key}")
        elif int(match.group("generation")) != generation or int(match.group("issue")) != issue_number:
            errors.append(f"TITLE_LINEAGE_MISMATCH:{key}")
        elif TITLE_TYPES.get(match.group("kind"), "implementation" if match.group("kind").startswith("Impl") else "review" if match.group("kind").startswith("Review") else "decision") != card.get("card_type"):
            errors.append(f"TITLE_CARD_TYPE_MISMATCH:{key}")
        card_type = card.get("card_type")
        _error(errors, card_type in CARD_TYPES, f"INVALID_CARD_TYPE:{key}")
        _error(errors, card.get("assignee") == EXPECTED_ASSIGNEES.get(card_type), f"ASSIGNEE_MISMATCH:{key}")
        skills = card.get("skills")
        _error(errors, skills == [], f"TASK_SKILLS_MUST_BE_EMPTY:{key}")
        _error(errors, card.get("workspace") == workspace, f"WORKSPACE_MISMATCH:{key}")
        _error(errors, card.get("status") in STATUSES, f"INVALID_CARD_STATUS:{key}")
        _error(errors, isinstance(card.get("priority"), int) and not isinstance(card.get("priority"), bool), f"INVALID_CARD_PRIORITY:{key}")
        _error(errors, isinstance(card.get("parents"), list), f"INVALID_PARENTS:{key}")
        _validate_body(card, behavior_ids, errors)
        if card.get("card_type") == "implementation" and isinstance(card.get("body"), dict):
            if validate_implementation:
                errors.extend(f"IMPLEMENTATION:{key}:{failure}" for failure in validate_implementation(card["body"]))
            body_issue = card["body"].get("issue")
            if isinstance(body_issue, dict):
                _error(errors, body_issue.get("number") == issue_number, f"IMPLEMENTATION_ISSUE_NUMBER_MISMATCH:{key}")
                _error(errors, body_issue.get("url") == issue_url, f"IMPLEMENTATION_ISSUE_URL_MISMATCH:{key}")
            _error(errors, card["body"].get("triage_digest") == graph.get("triage_digest"), f"IMPLEMENTATION_TRIAGE_DIGEST_MISMATCH:{key}")
            body_slice = card["body"].get("slice")
            body_behavior_ids = set(body_slice.get("behavior_ids", [])) if isinstance(body_slice, dict) else set()
            _error(errors, body_behavior_ids <= behavior_ids, f"IMPLEMENTATION_UNKNOWN_BEHAVIOR:{key}")
            body_requirement_ids: set[str] = set()
            for acceptance in card["body"].get("acceptance", []):
                if not isinstance(acceptance, dict):
                    continue
                for requirement_id in acceptance.get("requirement_ids", []):
                    if isinstance(requirement_id, str):
                        body_requirement_ids.add(requirement_id)
                        if requirement_id not in requirement_ids:
                            errors.append(f"IMPLEMENTATION_UNKNOWN_REQUIREMENT:{key}:{requirement_id}")
            assigned_requirement_ids = {
                requirement_id
                for behavior in behaviors or []
                if isinstance(behavior, dict) and behavior.get("id") in body_behavior_ids
                for requirement_id in behavior.get("requirement_ids", [])
                if isinstance(requirement_id, str)
            }
            _error(errors, body_requirement_ids == assigned_requirement_ids, f"IMPLEMENTATION_REQUIREMENT_COVERAGE:{key}")

    triages = [key for key, card in records.items() if card.get("card_type") == "triage"]
    summaries = [key for key, card in records.items() if card.get("card_type") == "summary"]
    reviews = [key for key, card in records.items() if card.get("card_type") == "review"]
    _error(errors, len(triages) == 1, "TRIAGE_COUNT")
    _error(errors, len(summaries) == 1, "SUMMARY_COUNT")
    _error(errors, bool(reviews), "REVIEW_COUNT")
    for key, card in records.items():
        for parent in card.get("parents", []) if isinstance(card.get("parents"), list) else []:
            if parent not in records:
                errors.append(f"UNKNOWN_PARENT:{key}:{parent}")
            if parent == key:
                errors.append(f"SELF_PARENT:{key}")
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

    def depends_on(key: str, ancestor: str, seen: set[str] | None = None) -> bool:
        if seen is None:
            seen = set()
        if key in seen:
            return False
        seen.add(key)
        parents = records.get(key, {}).get("parents", [])
        return ancestor in parents or any(
            parent in records and depends_on(parent, ancestor, seen)
            for parent in parents
        )

    executable = [
        key
        for key, card in records.items()
        if card.get("card_type") in {"implementation", "review"}
        and card.get("status") not in {"done", "archived"}
    ]
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
            expected_priority = len(execution_order) - index
            _error(errors, records[key].get("priority") == expected_priority, f"EXECUTION_PRIORITY_MISMATCH:{key}")
        if execution_order:
            _error(errors, graph.get("ready_candidate") == execution_order[0], "READY_CANDIDATE_ORDER_MISMATCH")
    for key, card in records.items():
        if card.get("card_type") not in {"implementation", "review"}:
            _error(errors, card.get("priority") == 0, f"NON_EXECUTION_PRIORITY_MUST_BE_ZERO:{key}")
    if triages:
        _error(errors, records[triages[0]].get("parents") == [], "TRIAGE_HAS_PARENT")
    if summaries:
        summary_parents = set(records[summaries[0]].get("parents", []))
        required_parents = {key for key, card in records.items() if card.get("card_type") in {"implementation", "review", "decision"}}
        _error(errors, required_parents == summary_parents, "SUMMARY_MEMBERSHIP_MISMATCH")
    if mode in {"new", "requirement-rework"}:
        _error(
            errors,
            not any(card.get("card_type") == "decision" for card in records.values()),
            "PLANNING_DECISION_MUST_NOT_BE_GRAPH_CARD",
        )
    for key in reviews:
        _error(errors, bool(records[key].get("parents")), f"REVIEW_WITHOUT_PARENT:{key}")
    for key, card in records.items():
        parent_types = {records[parent].get("card_type") for parent in card.get("parents", []) if parent in records}
        card_type = card.get("card_type")
        if card_type == "implementation":
            _error(errors, bool(parent_types) and parent_types <= {"triage", "implementation", "review", "decision"}, f"INVALID_IMPL_PARENTS:{key}")
        elif card_type == "review":
            allowed_review_parents = {"implementation", "review", "decision"}
            if mode in {"new", "requirement-rework"}:
                allowed_review_parents.add("triage")
            _error(errors, bool(parent_types) and parent_types <= allowed_review_parents, f"INVALID_REVIEW_PARENTS:{key}")
        elif card_type == "decision":
            _error(errors, "review" in parent_types, f"DECISION_WITHOUT_REVIEW_PARENT:{key}")
    for impl_key, impl in records.items():
        if impl.get("card_type") != "implementation":
            continue
        child_reviews = [key for key in reviews if impl_key in records[key].get("parents", [])]
        _error(errors, bool(child_reviews), f"IMPLEMENTATION_WITHOUT_REVIEW_CHILD:{impl_key}")

    for behavior in behaviors or []:
        if not isinstance(behavior, dict) or behavior.get("state") not in {"partial", "absent"}:
            continue
        behavior_id = behavior.get("id")
        impl_key = behavior.get("implementation_card_key")
        impl = records.get(impl_key)
        _error(errors, impl is not None and impl.get("card_type") == "implementation", f"BEHAVIOR_IMPL_NOT_FOUND:{behavior_id}")
        if impl and isinstance(impl.get("body"), dict):
            body_slice = impl["body"].get("slice")
            linked_ids = set(body_slice.get("behavior_ids", [])) if isinstance(body_slice, dict) else set()
            _error(errors, behavior_id in linked_ids, f"BEHAVIOR_IMPL_MISMATCH:{behavior_id}")
    implementation_keys = {key for key, card in records.items() if card.get("card_type") == "implementation"}
    planned_impl_keys = {
        behavior.get("implementation_card_key")
        for behavior in behaviors or []
        if isinstance(behavior, dict) and behavior.get("disposition") == "planned"
    }
    if mode in {"new", "requirement-rework"}:
        _error(errors, implementation_keys == planned_impl_keys, "IMPLEMENTATION_BEHAVIOR_COVERAGE")
        for impl_key in implementation_keys:
            body = records[impl_key].get("body", {})
            body_slice = body.get("slice") if isinstance(body, dict) else None
            actual_ids = set(body_slice.get("behavior_ids", [])) if isinstance(body_slice, dict) else set()
            assigned_ids = {
                behavior.get("id")
                for behavior in behaviors or []
                if isinstance(behavior, dict)
                and behavior.get("disposition") == "planned"
                and behavior.get("implementation_card_key") == impl_key
            }
            _error(errors, actual_ids == assigned_ids, f"IMPLEMENTATION_BEHAVIOR_COVERAGE:{impl_key}")
    numbered_reviews = [
        (int(match.group("kind")[6:]), key)
        for key in reviews
        if (match := TITLE.fullmatch(str(records[key].get("title", "")))) and match.group("kind").startswith("Review")
    ]
    latest_review = max(numbered_reviews, default=(0, None))[1]
    for key in reviews:
        body = records[key].get("body")
        if not isinstance(body, dict):
            continue
        _error(errors, body.get("triage_digest") == triage_digest, f"REVIEW_TRIAGE_DIGEST_MISMATCH:{key}")
        _error(errors, body.get("baseline_sha") == planning_baseline_sha, f"REVIEW_BASELINE_MISMATCH:{key}")
        declared_behavior_ids = _string_set(body.get("behavior_ids"))
        declared_impl_keys = _string_set(body.get("implementation_card_keys"))
        source_acceptance_ids: set[str] = set()
        source_verification_ids: set[str] = set()
        source_acceptance_owners: dict[str, set[str]] = {}
        source_verification_owners: dict[str, set[str]] = {}
        implementation_behavior_ids: set[str] = set()
        for impl_key in declared_impl_keys:
            _error(errors, impl_key in records and records[impl_key].get("card_type") == "implementation", f"REVIEW_IMPL_NOT_FOUND:{key}:{impl_key}")
            impl_body = records.get(impl_key, {}).get("body")
            if not isinstance(impl_body, dict):
                continue
            implementation_slice = impl_body.get("slice")
            if isinstance(implementation_slice, dict):
                implementation_behavior_ids.update(_string_set(implementation_slice.get("behavior_ids")))
            for acceptance in _dict_items(impl_body.get("acceptance")):
                source_id = acceptance.get("id")
                if _non_empty(source_id):
                    owners = source_acceptance_owners.setdefault(source_id, set())
                    owners.add(impl_key)
                    if len(owners) > 1:
                        errors.append(f"DUPLICATE_REVIEW_SOURCE_ACCEPTANCE_ID:{key}:{source_id}")
                    source_acceptance_ids.add(source_id)
            for verification in _dict_items(impl_body.get("verification")):
                source_id = verification.get("id")
                if _non_empty(source_id):
                    owners = source_verification_owners.setdefault(source_id, set())
                    owners.add(impl_key)
                    source_verification_ids.add(source_id)

        _error(errors, declared_behavior_ids == behavior_ids, f"REVIEW_BEHAVIOR_COVERAGE:{key}")

        review_acceptance_ids: set[str] = set()
        acceptance_behavior_ids: set[str] = set()
        acceptance_impl_keys: set[str] = set()
        acceptance_source_ids: set[str] = set()
        acceptance_verification_ids: set[str] = set()
        for acceptance in _dict_items(body.get("acceptance")):
            acceptance_id = acceptance.get("id")
            if isinstance(acceptance_id, str):
                review_acceptance_ids.add(acceptance_id)
            row_behavior_ids = _string_set(acceptance.get("behavior_ids"))
            row_impl_keys = _string_set(acceptance.get("implementation_card_keys"))
            row_source_ids = _string_set(acceptance.get("source_acceptance_ids"))
            row_verification_ids = _string_set(acceptance.get("verification_ids"))
            acceptance_behavior_ids.update(row_behavior_ids)
            acceptance_impl_keys.update(row_impl_keys)
            acceptance_source_ids.update(row_source_ids)
            acceptance_verification_ids.update(row_verification_ids)
            for behavior_id in row_behavior_ids:
                _error(errors, behavior_id in declared_behavior_ids, f"UNKNOWN_REVIEW_ACCEPTANCE_BEHAVIOR:{key}:{acceptance_id}:{behavior_id}")
            for impl_key in row_impl_keys:
                _error(errors, impl_key in declared_impl_keys, f"UNKNOWN_REVIEW_ACCEPTANCE_IMPL:{key}:{acceptance_id}:{impl_key}")
            for source_id in row_source_ids:
                _error(errors, source_id in source_acceptance_ids, f"UNKNOWN_REVIEW_SOURCE_ACCEPTANCE:{key}:{acceptance_id}:{source_id}")
                _error(
                    errors,
                    bool(source_acceptance_owners.get(source_id))
                    and source_acceptance_owners[source_id] <= row_impl_keys,
                    f"REVIEW_ACCEPTANCE_SOURCE_NOT_OWNED:{key}:{acceptance_id}:{source_id}",
                )
            for verification_id in row_verification_ids:
                _error(errors, verification_id in source_verification_ids, f"UNKNOWN_REVIEW_ACCEPTANCE_VERIFICATION:{key}:{acceptance_id}:{verification_id}")
                _error(
                    errors,
                    bool(source_verification_owners.get(verification_id))
                    and bool(source_verification_owners[verification_id] & row_impl_keys),
                    f"REVIEW_ACCEPTANCE_VERIFICATION_NOT_OWNED:{key}:{acceptance_id}:{verification_id}",
                )

        _error(errors, acceptance_behavior_ids == declared_behavior_ids, f"REVIEW_ACCEPTANCE_BEHAVIOR_COVERAGE:{key}")
        _error(errors, acceptance_impl_keys == declared_impl_keys, f"REVIEW_ACCEPTANCE_IMPL_COVERAGE:{key}")
        _error(errors, acceptance_source_ids == source_acceptance_ids, f"REVIEW_ACCEPTANCE_SOURCE_COVERAGE:{key}")
        _error(errors, acceptance_verification_ids == source_verification_ids, f"REVIEW_ACCEPTANCE_VERIFICATION_COVERAGE:{key}")

        check_behavior_ids: set[str] = set()
        check_impl_keys: set[str] = set()
        check_acceptance_ids: set[str] = set()
        check_source_ids: set[str] = set()
        check_verification_ids: set[str] = set()
        valid_checks = _dict_items(body.get("review_checks"))
        for check in valid_checks:
            check_id = check.get("id")
            row_behavior_ids = _string_set(check.get("behavior_ids"))
            row_impl_keys = _string_set(check.get("implementation_card_keys"))
            row_acceptance_ids = _string_set(check.get("acceptance_ids"))
            row_source_ids = _string_set(check.get("source_acceptance_ids"))
            row_verification_ids = _string_set(check.get("verification_ids"))
            check_behavior_ids.update(row_behavior_ids)
            check_impl_keys.update(row_impl_keys)
            check_acceptance_ids.update(row_acceptance_ids)
            check_source_ids.update(row_source_ids)
            check_verification_ids.update(row_verification_ids)
            for behavior_id in row_behavior_ids:
                _error(errors, behavior_id in declared_behavior_ids, f"UNKNOWN_REVIEW_CHECK_BEHAVIOR:{key}:{check_id}:{behavior_id}")
            for impl_key in row_impl_keys:
                _error(errors, impl_key in declared_impl_keys, f"UNKNOWN_REVIEW_CHECK_IMPL:{key}:{check_id}:{impl_key}")
            for acceptance_id in row_acceptance_ids:
                _error(errors, acceptance_id in review_acceptance_ids, f"UNKNOWN_REVIEW_CHECK_ACCEPTANCE:{key}:{check_id}:{acceptance_id}")
            for source_id in row_source_ids:
                _error(errors, source_id in source_acceptance_ids, f"UNKNOWN_REVIEW_CHECK_SOURCE_ACCEPTANCE:{key}:{check_id}:{source_id}")
                _error(
                    errors,
                    bool(source_acceptance_owners.get(source_id))
                    and source_acceptance_owners[source_id] <= row_impl_keys,
                    f"REVIEW_CHECK_SOURCE_NOT_OWNED:{key}:{check_id}:{source_id}",
                )
            for verification_id in row_verification_ids:
                _error(errors, verification_id in source_verification_ids, f"UNKNOWN_REVIEW_CHECK_VERIFICATION:{key}:{check_id}:{verification_id}")
                _error(
                    errors,
                    bool(source_verification_owners.get(verification_id))
                    and bool(source_verification_owners[verification_id] & row_impl_keys),
                    f"REVIEW_CHECK_VERIFICATION_NOT_OWNED:{key}:{check_id}:{verification_id}",
                )

        _error(errors, check_behavior_ids == declared_behavior_ids, f"REVIEW_CHECK_BEHAVIOR_COVERAGE:{key}")
        _error(errors, check_impl_keys == declared_impl_keys, f"REVIEW_CHECK_IMPL_COVERAGE:{key}")
        _error(errors, check_acceptance_ids == review_acceptance_ids, f"REVIEW_CHECK_ACCEPTANCE_COVERAGE:{key}")
        _error(errors, check_source_ids == source_acceptance_ids, f"REVIEW_CHECK_SOURCE_ACCEPTANCE_COVERAGE:{key}")
        _error(errors, check_verification_ids == source_verification_ids, f"REVIEW_CHECK_VERIFICATION_COVERAGE:{key}")

        if declared_impl_keys:
            focuses = {check.get("focus") for check in valid_checks}
            _error(errors, "implementation-convention" in focuses, f"REVIEW_IMPLEMENTATION_CONVENTION_CHECK_MISSING:{key}")
            _error(errors, "test-quality" in focuses, f"REVIEW_TEST_QUALITY_CHECK_MISSING:{key}")
        if len(declared_impl_keys) > 1:
            _error(
                errors,
                any(
                    check.get("focus") == "cross-card-seam"
                    and len(_string_set(check.get("implementation_card_keys"))) > 1
                    for check in valid_checks
                ),
                f"REVIEW_CROSS_CARD_SEAM_CHECK_MISSING:{key}",
            )
        inherited_behavior_ids = declared_behavior_ids - implementation_behavior_ids
        if inherited_behavior_ids:
            preservation_behavior_ids = {
                behavior_id
                for check in valid_checks
                if check.get("focus") == "preservation"
                for behavior_id in _string_set(check.get("behavior_ids"))
            }
            _error(errors, inherited_behavior_ids <= preservation_behavior_ids, f"REVIEW_PRESERVATION_CHECK_COVERAGE:{key}")
    if latest_review:
        latest_body = records[latest_review].get("body", {})
        _error(errors, _string_set(latest_body.get("implementation_card_keys")) == implementation_keys, "LATEST_REVIEW_IMPL_COVERAGE")
    if summaries:
        summary_body = records[summaries[0]].get("body", {})
        if isinstance(summary_body, dict):
            _error(errors, summary_body.get("triage_digest") == triage_digest, "SUMMARY_TRIAGE_DIGEST_MISMATCH")
            summary_issue = summary_body.get("issue")
            _error(
                errors,
                isinstance(summary_issue, dict)
                and summary_issue.get("number") == issue_number
                and summary_issue.get("url") == issue_url,
                "SUMMARY_ISSUE_MISMATCH",
            )
            target_outcomes = summary_body.get("target_outcomes")
            expected_outcomes = [
                {"id": row.get("id"), "outcome": row.get("outcome")}
                for row in graph.get("change_requirements", [])
                if isinstance(row, dict)
            ]
            _error(errors, target_outcomes == expected_outcomes, "SUMMARY_TARGET_OUTCOMES_MISMATCH")
            preserved_behaviors = summary_body.get("preserved_behaviors")
            expected_behaviors = [
                {"id": row.get("id"), "behavior": row.get("outcome")}
                for row in graph.get("behaviors", [])
                if isinstance(row, dict)
            ]
            _error(errors, preserved_behaviors == expected_behaviors, "SUMMARY_PRESERVED_BEHAVIORS_MISMATCH")

    ready_candidate = graph.get("ready_candidate")
    source_review = graph.get("source_review") if isinstance(graph.get("source_review"), dict) else {}
    dispositions = source_review.get("dispositions", []) if isinstance(source_review, dict) else []
    appended = {
        key
        for disposition in dispositions
        if isinstance(disposition, dict)
        for key in disposition.get("appended_card_keys", [])
    }
    appended_types = {records[key].get("card_type") for key in appended if key in records}
    decision_only = mode == "review-rework" and bool(appended) and appended_types == {"decision"}
    if mode == "review-rework":
        _error(errors, (ready_candidate is None) == decision_only, "REVIEW_REWORK_ACTIVATION_MISMATCH")
        _error(errors, set(source_review.get("idempotency", {})) == appended, "IDEMPOTENCY_CARD_COVERAGE")
        for disposition in dispositions:
            if not isinstance(disposition, dict):
                continue
            keys = disposition.get("appended_card_keys", [])
            types = {records[key].get("card_type") for key in keys if key in records}
            verdict = disposition.get("verdict")
            expected = {"implementation"} if verdict == "correction-required" else {"review"} if verdict == "context-required" else {"decision"}
            _error(errors, bool(types & expected), f"SOURCE_VERDICT_CARD_TYPE:{disposition.get('finding_id')}")
            if verdict == "decision-required":
                for key in keys:
                    card = records.get(key, {})
                    if card.get("card_type") != "decision" or not isinstance(card.get("body"), dict):
                        continue
                    _error(errors, card["body"].get("source_review_key") == source_review.get("review_key"), f"DECISION_SOURCE_REVIEW_MISMATCH:{key}")
                    _error(errors, disposition.get("finding_id") in card["body"].get("finding_ids", []), f"DECISION_SOURCE_FINDING_MISMATCH:{key}")
    _error(errors, decision_only or ready_candidate in records, "READY_CANDIDATE_NOT_FOUND")
    if ready_candidate in records and triages:
        candidate = records[ready_candidate]
        _error(errors, candidate.get("card_type") in {"implementation", "review"}, "READY_CANDIDATE_TYPE")
        if mode == "review-rework":
            source_key = source_review.get("review_key")
            _error(errors, source_key in candidate.get("parents", []), "READY_CANDIDATE_NOT_SOURCE_REVIEW_CHILD")
            _error(errors, ready_candidate in appended, "READY_CANDIDATE_NOT_APPENDED")
        else:
            _error(errors, candidate.get("parents") == [triages[0]], "READY_CANDIDATE_PARENT_SET")
            if planned_impl_keys:
                _error(errors, candidate.get("card_type") == "implementation", "READY_CANDIDATE_NOT_IMPL")
            else:
                _error(errors, candidate.get("card_type") == "review", "NO_IMPL_REQUIRES_REVIEW_CANDIDATE")
    ready = [key for key, card in records.items() if card.get("status") == "ready"]
    if phase == "draft":
        _error(errors, not ready, "DRAFT_HAS_READY_CARD")
        if triages:
            expected_triage = "done" if mode == "review-rework" else "triage"
            _error(errors, records[triages[0]].get("status") == expected_triage, "DRAFT_TRIAGE_STATUS")
        source_key = source_review.get("review_key")
        for key, card in records.items():
            if key in triages or card.get("status") == "archived":
                continue
            historical = mode == "review-rework" and key not in appended and key != summaries[0]
            if historical:
                _error(errors, card.get("status") == "done", f"HISTORICAL_CARD_NOT_DONE:{key}")
                continue
            expected = {"todo", "blocked"} if card.get("card_type") == "decision" else {"todo"}
            _error(errors, card.get("status") in expected, f"DRAFT_ACTIVE_STATUS:{key}")
        if mode == "review-rework" and source_key in records:
            _error(errors, records[source_key].get("status") == "done", "SOURCE_REVIEW_NOT_DONE")
    else:
        expected_ready = [] if decision_only else [ready_candidate]
        _error(errors, ready == expected_ready, "NATIVE_READY_COUNT_OR_ID")
        if triages:
            _error(errors, records[triages[0]].get("status") == "done", "NATIVE_TRIAGE_NOT_DONE")
        source_key = source_review.get("review_key")
        for key, card in records.items():
            historical = mode == "review-rework" and key not in appended and key != summaries[0]
            if key in triages or key == ready_candidate or card.get("status") == "archived" or historical:
                if historical:
                    _error(errors, card.get("status") == "done", f"HISTORICAL_CARD_NOT_DONE:{key}")
                continue
            expected = {"todo", "blocked"} if card.get("card_type") == "decision" else {"todo"}
            _error(errors, card.get("status") in expected, f"NATIVE_ACTIVE_STATUS:{key}")
        if decision_only:
            _error(errors, not ready, "DECISION_ONLY_HAS_READY_CARD")
    if mode == "review-rework" and isinstance(graph.get("source_review"), dict):
        source_key = source_review.get("review_key")
        _error(errors, source_key in records and records[source_key].get("card_type") == "review", "SOURCE_REVIEW_NOT_FOUND")
        for key in appended:
            _error(errors, key in records, f"APPENDED_CARD_NOT_FOUND:{key}")
    archived = graph.get("archived_card_keys")
    _error(errors, isinstance(archived, list), "INVALID_ARCHIVED_CARD_KEYS")
    if isinstance(archived, list):
        for key in archived:
            card = records.get(key)
            _error(errors, card is not None, f"ARCHIVED_CARD_NOT_FOUND:{key}")
            if card:
                _error(errors, card.get("status") == "archived", f"ARCHIVE_STATUS_MISMATCH:{key}")
                _error(errors, card.get("card_type") in {"implementation", "review", "summary"}, f"ARCHIVE_TYPE_FORBIDDEN:{key}")
        for key, card in records.items():
            if card.get("status") == "archived" and key not in archived:
                errors.append(f"ARCHIVED_CARD_UNDECLARED:{key}")
    return errors


def validate_review_graph(
    graph: Any,
    *,
    validate_implementation: Callable[[Any], list[str]] | None = None,
) -> list[str]:
    """Validate a Review body against frozen source cards without revalidating graph topology."""
    errors = validate_graph(graph, validate_implementation=validate_implementation)
    review_prefixes = (
        "CARD_BODY_SCHEMA:review",
        "EPHEMERAL_COORDINATOR_CONTEXT:review",
        "UNEXPECTED_REVIEW_",
        "INVALID_REVIEW_",
        "MISSING_REVIEW_",
        "DUPLICATE_REVIEW_",
        "UNKNOWN_REVIEW_",
        "REVIEW_TRIAGE_",
        "REVIEW_BASELINE_",
        "REVIEW_IMPL_",
        "REVIEW_BEHAVIOR_",
        "REVIEW_ACCEPTANCE_",
        "REVIEW_CHECK_",
        "REVIEW_IMPLEMENTATION_",
        "REVIEW_TEST_",
        "REVIEW_ACTIVE_PLAN_",
        "REVIEW_CROSS_",
        "REVIEW_PRESERVATION_",
        "LOW_QUALITY_REVIEW_",
        "LATEST_REVIEW_",
        "MISSING_IMPLEMENTATION_BODY:",
        "IMPLEMENTATION_SCHEMA:",
        "IMPLEMENTATION:",
        "IMPLEMENTATION_ISSUE_",
        "IMPLEMENTATION_TRIAGE_DIGEST_MISMATCH:",
    )
    return [error for error in errors if error.startswith(review_prefixes)]


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
    planned_titles = {card.get("title") for card in planned_by_key.values()}
    _error(errors, planned_titles == set(actual_by_title), "NATIVE_READBACK_MEMBERSHIP_MISMATCH")
    for key, card in planned_by_key.items():
        envelope = actual_by_title.get(card.get("title"))
        if not isinstance(envelope, dict):
            continue
        task = envelope["task"]
        for field in ("assignee", "status", "priority"):
            _error(errors, task.get(field) == card.get(field), f"NATIVE_{field.upper()}_MISMATCH:{key}")
        _error(errors, task.get("skills") == card.get("skills"), f"NATIVE_SKILLS_MISMATCH:{key}")
        body = task.get("body")
        if card.get("body") is None and card.get("card_type") == "triage":
            _error(errors, isinstance(body, str) and bool(body.strip()), f"NATIVE_TRIAGE_BODY_MISSING:{key}")
        elif card.get("body") is None:
            _error(errors, body in {None, ""}, f"NATIVE_BODY_MISMATCH:{key}")
        else:
            try:
                native_body = json.loads(body) if isinstance(body, str) else body
            except json.JSONDecodeError:
                native_body = None
                errors.append(f"NATIVE_BODY_NOT_JSON:{key}")
            expected_body = card.get("body")
            if native_body != expected_body:
                errors.append(f"NATIVE_BODY_MISMATCH:{key}")
            else:
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
        parent_ids = envelope.get("parents")
        _error(errors, isinstance(parent_ids, list) and all(_non_empty(item) for item in parent_ids), f"NATIVE_PARENTS_INVALID:{key}")
        if isinstance(parent_ids, list):
            expected_parent_ids = []
            for parent_key in card.get("parents", []):
                parent = planned_by_key.get(parent_key)
                parent_envelope = actual_by_title.get(parent.get("title")) if isinstance(parent, dict) else None
                if isinstance(parent_envelope, dict):
                    expected_parent_ids.append(parent_envelope["task"].get("id"))
            _error(errors, set(parent_ids) == set(expected_parent_ids), f"NATIVE_PARENT_MISMATCH:{key}")
    return errors


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repository, check=True, text=True, encoding="utf-8", capture_output=True).stdout


def requirement_diff(repository: Path, base: str, revised: str, paths: list[str]) -> dict[str, Any]:
    """Git을 변경하지 않고 stable hunk ID를 가진 비교 산출물을 만든다."""
    for revision in (base, revised):
        _git(repository, "rev-parse", "--verify", f"{revision}^{{commit}}")
    command = ["diff", "--find-renames", "--find-copies", "--unified=0", "--no-ext-diff", base, revised, "--", *paths]
    raw = _git(repository, *command)
    hunks: list[dict[str, Any]] = []
    old_path = new_path = ""
    current: dict[str, Any] | None = None
    for line in raw.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" ", 3)
            old_path, new_path = parts[2][2:], parts[3][2:]
            current = None
        elif line.startswith("rename from ") or line.startswith("copy from "):
            old_path = line.split(" ", 2)[2]
        elif line.startswith("rename to ") or line.startswith("copy to "):
            new_path = line.split(" ", 2)[2]
        elif line.startswith("@@ "):
            match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if not match:
                continue
            current = {
                "old_path": old_path,
                "new_path": new_path,
                "old_start": int(match.group(1)),
                "old_count": int(match.group(2) or "1"),
                "new_start": int(match.group(3)),
                "new_count": int(match.group(4) or "1"),
                "lines": [],
            }
            hunks.append(current)
        elif current is not None and line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            current["lines"].append(line)
    for hunk in hunks:
        canonical = json.dumps(hunk, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        hunk["id"] = "h_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return {
        "schema": "requirement-rework-comparison-v1",
        "provenance": {"base_revision": base, "revised_revision": revised, "paths": paths, "git_args": command},
        "hunks": hunks,
        "behavior_deltas": [],
        "behavior_delta_status": "human-review-required",
    }
