"""Aggregate Review card-body validation."""

from __future__ import annotations

from typing import Any

from .card_body_common import (
    SHA,
    SHA256,
    append_error,
    check_exact_fields,
    descriptive_korean,
    non_empty,
    unique_strings,
)


REVIEW_FOCI = {
    "behavior",
    "implementation-convention",
    "cross-card-seam",
    "test-quality",
    "preservation",
}
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
def _validate_acceptance(key: Any, acceptance: Any, errors: list[str]) -> None:
    append_error(errors, isinstance(acceptance, list) and bool(acceptance), f"MISSING_REVIEW_ACCEPTANCE:{key}")
    if not isinstance(acceptance, list):
        return
    acceptance_fields = {
        "id",
        "criterion",
        "behavior_ids",
        "implementation_card_keys",
        "source_acceptance_ids",
        "verification_ids",
    }
    acceptance_ids: set[str] = set()
    acceptance_criteria: set[str] = set()
    for index, item in enumerate(acceptance):
        if not isinstance(item, dict):
            errors.append(f"INVALID_REVIEW_ACCEPTANCE:{key}:{index}")
            continue
        check_exact_fields(item, acceptance_fields, errors, f"UNEXPECTED_REVIEW_ACCEPTANCE_FIELD:{key}:{index}")
        acceptance_id = item.get("id")
        append_error(errors, non_empty(acceptance_id), f"MISSING_REVIEW_ACCEPTANCE_ID:{key}:{index}")
        if isinstance(acceptance_id, str):
            if acceptance_id in acceptance_ids:
                errors.append(f"DUPLICATE_REVIEW_ACCEPTANCE_ID:{key}:{acceptance_id}")
            acceptance_ids.add(acceptance_id)
        criterion = item.get("criterion")
        append_error(errors, descriptive_korean(criterion), f"LOW_QUALITY_REVIEW_ACCEPTANCE_CRITERION:{key}:{index}")
        if isinstance(criterion, str):
            normalized = criterion.strip()
            if normalized in acceptance_criteria:
                errors.append(f"DUPLICATE_REVIEW_ACCEPTANCE_CRITERION:{key}:{index}")
            acceptance_criteria.add(normalized)
        append_error(errors, unique_strings(item.get("behavior_ids")), f"INVALID_REVIEW_ACCEPTANCE_BEHAVIORS:{key}:{index}")
        append_error(errors, unique_strings(item.get("implementation_card_keys"), allow_empty=True), f"INVALID_REVIEW_ACCEPTANCE_IMPLS:{key}:{index}")
        append_error(errors, unique_strings(item.get("source_acceptance_ids"), allow_empty=True), f"INVALID_REVIEW_SOURCE_ACCEPTANCES:{key}:{index}")
        append_error(errors, unique_strings(item.get("verification_ids"), allow_empty=True), f"INVALID_REVIEW_ACCEPTANCE_VERIFICATIONS:{key}:{index}")


def _validate_unique_text(
    key: Any,
    check_id: Any,
    value: Any,
    seen: set[str],
    quality_code: str,
    duplicate_code: str,
    errors: list[str],
    *,
    require_question_mark: bool = False,
) -> None:
    quality = descriptive_korean(value)
    if require_question_mark:
        quality = quality and value.rstrip().endswith("?")
    append_error(errors, quality, f"{quality_code}:{key}:{check_id}")
    if isinstance(value, str):
        normalized = value.strip()
        if normalized in seen:
            errors.append(f"{duplicate_code}:{key}:{check_id}")
        seen.add(normalized)


def _validate_guide_locators(
    key: Any,
    check_id: Any,
    locators: Any,
    guide_locators: set[str],
    errors: list[str],
) -> None:
    append_error(errors, unique_strings(locators), f"MISSING_REVIEW_GUIDE_LOCATORS:{key}:{check_id}")
    if not isinstance(locators, list):
        return
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


def _validate_checks(key: Any, checks: Any, errors: list[str]) -> set[str]:
    append_error(errors, isinstance(checks, list) and bool(checks), f"MISSING_REVIEW_CHECKS:{key}")
    check_ids: set[str] = set()
    check_questions: set[str] = set()
    check_outcomes: set[str] = set()
    guide_locators: set[str] = set()
    if not isinstance(checks, list):
        return guide_locators
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
        check_exact_fields(item, check_fields, errors, f"UNEXPECTED_REVIEW_CHECK_FIELD:{key}:{index}")
        check_id = item.get("id")
        append_error(errors, non_empty(check_id), f"MISSING_REVIEW_CHECK_ID:{key}:{index}")
        if isinstance(check_id, str):
            if check_id in check_ids:
                errors.append(f"DUPLICATE_REVIEW_CHECK_ID:{key}:{check_id}")
            check_ids.add(check_id)
        append_error(errors, item.get("focus") in REVIEW_FOCI, f"INVALID_REVIEW_CHECK_FOCUS:{key}:{check_id}")
        _validate_unique_text(
            key,
            check_id,
            item.get("question"),
            check_questions,
            "LOW_QUALITY_REVIEW_CHECK_QUESTION",
            "DUPLICATE_REVIEW_CHECK_QUESTION",
            errors,
            require_question_mark=True,
        )
        _validate_unique_text(
            key,
            check_id,
            item.get("expected_outcome"),
            check_outcomes,
            "LOW_QUALITY_REVIEW_CHECK_EXPECTED_OUTCOME",
            "DUPLICATE_REVIEW_CHECK_EXPECTED_OUTCOME",
            errors,
        )
        for field in ("behavior_ids", "acceptance_ids"):
            append_error(errors, unique_strings(item.get(field)), f"INVALID_REVIEW_CHECK_{field.upper()}:{key}:{check_id}")
        for field in ("implementation_card_keys", "source_acceptance_ids", "verification_ids"):
            append_error(errors, unique_strings(item.get(field), allow_empty=True), f"INVALID_REVIEW_CHECK_{field.upper()}:{key}:{check_id}")
        _validate_guide_locators(key, check_id, item.get("guide_locators"), guide_locators, errors)
    return guide_locators


def _validate_guide_coverage(key: Any, guide_locators: set[str], errors: list[str]) -> None:
    append_error(
        errors,
        any(locator.startswith("docs/backend-development-guide.md#") for locator in guide_locators),
        f"REVIEW_DEVELOPMENT_GUIDE_COVERAGE:{key}",
    )
    append_error(
        errors,
        any(locator.startswith("docs/backend-test-guide.md#") for locator in guide_locators),
        f"REVIEW_TEST_GUIDE_COVERAGE:{key}",
    )
    append_error(
        errors,
        any(locator.startswith(".hermes/docs/plan.md#") for locator in guide_locators),
        f"REVIEW_ACTIVE_PLAN_COVERAGE:{key}",
    )


def validate_review_body(key: Any, body: dict[str, Any]) -> tuple[str, ...]:
    errors: list[str] = []
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
    check_exact_fields(body, allowed, errors, f"UNEXPECTED_REVIEW_BODY_FIELD:{key}")
    append_error(errors, isinstance(body.get("triage_digest"), str) and bool(SHA256.fullmatch(body["triage_digest"])), f"INVALID_REVIEW_TRIAGE_DIGEST:{key}")
    append_error(errors, isinstance(body.get("baseline_sha"), str) and bool(SHA.fullmatch(body["baseline_sha"])), f"INVALID_REVIEW_BASELINE:{key}")
    append_error(errors, unique_strings(body.get("behavior_ids")), f"MISSING_REVIEW_BEHAVIORS:{key}")
    append_error(errors, unique_strings(body.get("implementation_card_keys"), allow_empty=True), f"MISSING_REVIEW_IMPLS:{key}")
    _validate_acceptance(key, body.get("acceptance"), errors)
    guide_locators = _validate_checks(key, body.get("review_checks"), errors)
    _validate_guide_coverage(key, guide_locators, errors)
    exclusions = body.get("scope_exclusions")
    append_error(
        errors,
        isinstance(exclusions, list)
        and all(non_empty(item) for item in exclusions)
        and len(exclusions) == len(set(exclusions)),
        f"INVALID_REVIEW_SCOPE_EXCLUSIONS:{key}",
    )
    return tuple(errors)
