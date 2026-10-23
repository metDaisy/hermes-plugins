"""Review cross-card linkage and coverage validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .card_record_validation import TITLE


@dataclass(frozen=True)
class ReviewLinkValidation:
    errors: tuple[str, ...]
    latest_review: str | None


@dataclass(frozen=True)
class ReviewSources:
    acceptance_ids: set[str]
    verification_ids: set[str]
    acceptance_owners: dict[str, set[str]]
    verification_owners: dict[str, set[str]]
    implementation_behavior_ids: set[str]


@dataclass(frozen=True)
class ReviewAcceptanceCoverage:
    review_ids: set[str]
    behavior_ids: set[str]
    implementation_keys: set[str]
    source_ids: set[str]
    verification_ids: set[str]


@dataclass(frozen=True)
class ReviewCheckCoverage:
    rows: list[dict[str, Any]]
    behavior_ids: set[str]
    implementation_keys: set[str]
    acceptance_ids: set[str]
    source_ids: set[str]
    verification_ids: set[str]


def _non_empty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


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


def _latest_review_key(
    records: dict[str, dict[str, Any]],
    reviews: tuple[str, ...],
) -> str | None:
    numbered_reviews = [
        (int(match.group("kind")[6:]), key)
        for key in reviews
        if (match := TITLE.fullmatch(str(records[key].get("title", ""))))
        and match.group("kind").startswith("Review")
    ]
    return max(numbered_reviews, default=(0, None))[1]


def _collect_review_sources(
    key: str,
    records: dict[str, dict[str, Any]],
    declared_impl_keys: set[str],
    errors: list[str],
) -> ReviewSources:
    acceptance_ids: set[str] = set()
    verification_ids: set[str] = set()
    acceptance_owners: dict[str, set[str]] = {}
    verification_owners: dict[str, set[str]] = {}
    implementation_behavior_ids: set[str] = set()

    for impl_key in declared_impl_keys:
        _error(
            errors,
            impl_key in records and records[impl_key].get("card_type") == "implementation",
            f"REVIEW_IMPL_NOT_FOUND:{key}:{impl_key}",
        )
        impl_body = records.get(impl_key, {}).get("body")
        if not isinstance(impl_body, dict):
            continue
        if impl_body.get("schema") == "backend-implementation-card-v6":
            trace = impl_body.get("trace")
            if isinstance(trace, dict):
                implementation_behavior_ids.update(_string_set(trace.get("observable_ids")))
            source_contracts = [
                *_dict_items(impl_body.get("obligations")),
                *_dict_items(impl_body.get("constraints")),
            ]
        else:
            implementation_slice = impl_body.get("slice")
            if isinstance(implementation_slice, dict):
                implementation_behavior_ids.update(
                    _string_set(implementation_slice.get("behavior_ids"))
                )
            source_contracts = _dict_items(impl_body.get("acceptance"))
        for acceptance in source_contracts:
            source_id = acceptance.get("id")
            if _non_empty(source_id):
                owners = acceptance_owners.setdefault(source_id, set())
                owners.add(impl_key)
                if len(owners) > 1:
                    errors.append(f"DUPLICATE_REVIEW_SOURCE_ACCEPTANCE_ID:{key}:{source_id}")
                acceptance_ids.add(source_id)
        for verification in _dict_items(impl_body.get("verification")):
            source_id = verification.get("id")
            if _non_empty(source_id):
                verification_owners.setdefault(source_id, set()).add(impl_key)
                verification_ids.add(source_id)

    return ReviewSources(
        acceptance_ids=acceptance_ids,
        verification_ids=verification_ids,
        acceptance_owners=acceptance_owners,
        verification_owners=verification_owners,
        implementation_behavior_ids=implementation_behavior_ids,
    )


def _validate_review_acceptance(
    key: str,
    body: dict[str, Any],
    declared_behavior_ids: set[str],
    declared_impl_keys: set[str],
    sources: ReviewSources,
    errors: list[str],
) -> ReviewAcceptanceCoverage:
    review_ids: set[str] = set()
    behavior_ids: set[str] = set()
    implementation_keys: set[str] = set()
    source_ids: set[str] = set()
    verification_ids: set[str] = set()

    for acceptance in _dict_items(body.get("acceptance")):
        acceptance_id = acceptance.get("id")
        if isinstance(acceptance_id, str):
            review_ids.add(acceptance_id)
        row_behavior_ids = _string_set(acceptance.get("behavior_ids"))
        row_impl_keys = _string_set(acceptance.get("implementation_card_keys"))
        row_source_ids = _string_set(acceptance.get("source_acceptance_ids"))
        row_verification_ids = _string_set(acceptance.get("verification_ids"))
        behavior_ids.update(row_behavior_ids)
        implementation_keys.update(row_impl_keys)
        source_ids.update(row_source_ids)
        verification_ids.update(row_verification_ids)
        for behavior_id in row_behavior_ids:
            _error(errors, behavior_id in declared_behavior_ids, f"UNKNOWN_REVIEW_ACCEPTANCE_BEHAVIOR:{key}:{acceptance_id}:{behavior_id}")
        for impl_key in row_impl_keys:
            _error(errors, impl_key in declared_impl_keys, f"UNKNOWN_REVIEW_ACCEPTANCE_IMPL:{key}:{acceptance_id}:{impl_key}")
        for source_id in row_source_ids:
            _error(errors, source_id in sources.acceptance_ids, f"UNKNOWN_REVIEW_SOURCE_ACCEPTANCE:{key}:{acceptance_id}:{source_id}")
            _error(
                errors,
                bool(sources.acceptance_owners.get(source_id))
                and sources.acceptance_owners[source_id] <= row_impl_keys,
                f"REVIEW_ACCEPTANCE_SOURCE_NOT_OWNED:{key}:{acceptance_id}:{source_id}",
            )
        for verification_id in row_verification_ids:
            _error(errors, verification_id in sources.verification_ids, f"UNKNOWN_REVIEW_ACCEPTANCE_VERIFICATION:{key}:{acceptance_id}:{verification_id}")
            _error(
                errors,
                bool(sources.verification_owners.get(verification_id))
                and bool(sources.verification_owners[verification_id] & row_impl_keys),
                f"REVIEW_ACCEPTANCE_VERIFICATION_NOT_OWNED:{key}:{acceptance_id}:{verification_id}",
            )

    return ReviewAcceptanceCoverage(
        review_ids=review_ids,
        behavior_ids=behavior_ids,
        implementation_keys=implementation_keys,
        source_ids=source_ids,
        verification_ids=verification_ids,
    )


def _validate_acceptance_coverage(
    key: str,
    coverage: ReviewAcceptanceCoverage,
    declared_behavior_ids: set[str],
    declared_impl_keys: set[str],
    sources: ReviewSources,
    errors: list[str],
) -> None:
    _error(errors, coverage.behavior_ids == declared_behavior_ids, f"REVIEW_ACCEPTANCE_BEHAVIOR_COVERAGE:{key}")
    _error(errors, coverage.implementation_keys == declared_impl_keys, f"REVIEW_ACCEPTANCE_IMPL_COVERAGE:{key}")
    _error(errors, coverage.source_ids == sources.acceptance_ids, f"REVIEW_ACCEPTANCE_SOURCE_COVERAGE:{key}")
    _error(errors, coverage.verification_ids == sources.verification_ids, f"REVIEW_ACCEPTANCE_VERIFICATION_COVERAGE:{key}")


def _validate_review_checks(
    key: str,
    body: dict[str, Any],
    declared_behavior_ids: set[str],
    declared_impl_keys: set[str],
    review_acceptance_ids: set[str],
    sources: ReviewSources,
    errors: list[str],
) -> ReviewCheckCoverage:
    behavior_ids: set[str] = set()
    implementation_keys: set[str] = set()
    acceptance_ids: set[str] = set()
    source_ids: set[str] = set()
    verification_ids: set[str] = set()
    rows = _dict_items(body.get("review_checks"))

    for check in rows:
        check_id = check.get("id")
        row_behavior_ids = _string_set(check.get("behavior_ids"))
        row_impl_keys = _string_set(check.get("implementation_card_keys"))
        row_acceptance_ids = _string_set(check.get("acceptance_ids"))
        row_source_ids = _string_set(check.get("source_acceptance_ids"))
        row_verification_ids = _string_set(check.get("verification_ids"))
        behavior_ids.update(row_behavior_ids)
        implementation_keys.update(row_impl_keys)
        acceptance_ids.update(row_acceptance_ids)
        source_ids.update(row_source_ids)
        verification_ids.update(row_verification_ids)
        for behavior_id in row_behavior_ids:
            _error(errors, behavior_id in declared_behavior_ids, f"UNKNOWN_REVIEW_CHECK_BEHAVIOR:{key}:{check_id}:{behavior_id}")
        for impl_key in row_impl_keys:
            _error(errors, impl_key in declared_impl_keys, f"UNKNOWN_REVIEW_CHECK_IMPL:{key}:{check_id}:{impl_key}")
        for acceptance_id in row_acceptance_ids:
            _error(errors, acceptance_id in review_acceptance_ids, f"UNKNOWN_REVIEW_CHECK_ACCEPTANCE:{key}:{check_id}:{acceptance_id}")
        for source_id in row_source_ids:
            _error(errors, source_id in sources.acceptance_ids, f"UNKNOWN_REVIEW_CHECK_SOURCE_ACCEPTANCE:{key}:{check_id}:{source_id}")
            _error(
                errors,
                bool(sources.acceptance_owners.get(source_id))
                and sources.acceptance_owners[source_id] <= row_impl_keys,
                f"REVIEW_CHECK_SOURCE_NOT_OWNED:{key}:{check_id}:{source_id}",
            )
        for verification_id in row_verification_ids:
            _error(errors, verification_id in sources.verification_ids, f"UNKNOWN_REVIEW_CHECK_VERIFICATION:{key}:{check_id}:{verification_id}")
            _error(
                errors,
                bool(sources.verification_owners.get(verification_id))
                and bool(sources.verification_owners[verification_id] & row_impl_keys),
                f"REVIEW_CHECK_VERIFICATION_NOT_OWNED:{key}:{check_id}:{verification_id}",
            )

    return ReviewCheckCoverage(
        rows=rows,
        behavior_ids=behavior_ids,
        implementation_keys=implementation_keys,
        acceptance_ids=acceptance_ids,
        source_ids=source_ids,
        verification_ids=verification_ids,
    )


def _validate_check_coverage(
    key: str,
    coverage: ReviewCheckCoverage,
    acceptance: ReviewAcceptanceCoverage,
    declared_behavior_ids: set[str],
    declared_impl_keys: set[str],
    sources: ReviewSources,
    errors: list[str],
) -> None:
    _error(errors, coverage.behavior_ids == declared_behavior_ids, f"REVIEW_CHECK_BEHAVIOR_COVERAGE:{key}")
    _error(errors, coverage.implementation_keys == declared_impl_keys, f"REVIEW_CHECK_IMPL_COVERAGE:{key}")
    _error(errors, coverage.acceptance_ids == acceptance.review_ids, f"REVIEW_CHECK_ACCEPTANCE_COVERAGE:{key}")
    _error(errors, coverage.source_ids == sources.acceptance_ids, f"REVIEW_CHECK_SOURCE_ACCEPTANCE_COVERAGE:{key}")
    _error(errors, coverage.verification_ids == sources.verification_ids, f"REVIEW_CHECK_VERIFICATION_COVERAGE:{key}")


def _validate_required_check_focuses(
    key: str,
    coverage: ReviewCheckCoverage,
    declared_behavior_ids: set[str],
    declared_impl_keys: set[str],
    implementation_behavior_ids: set[str],
    errors: list[str],
) -> None:
    if declared_impl_keys:
        focuses = {check.get("focus") for check in coverage.rows}
        _error(errors, "implementation-convention" in focuses, f"REVIEW_IMPLEMENTATION_CONVENTION_CHECK_MISSING:{key}")
        _error(errors, "test-quality" in focuses, f"REVIEW_TEST_QUALITY_CHECK_MISSING:{key}")
    if len(declared_impl_keys) > 1:
        _error(
            errors,
            any(
                check.get("focus") == "cross-card-seam"
                and len(_string_set(check.get("implementation_card_keys"))) > 1
                for check in coverage.rows
            ),
            f"REVIEW_CROSS_CARD_SEAM_CHECK_MISSING:{key}",
        )
    inherited_behavior_ids = declared_behavior_ids - implementation_behavior_ids
    if inherited_behavior_ids:
        preservation_behavior_ids = {
            behavior_id
            for check in coverage.rows
            if check.get("focus") == "preservation"
            for behavior_id in _string_set(check.get("behavior_ids"))
        }
        _error(errors, inherited_behavior_ids <= preservation_behavior_ids, f"REVIEW_PRESERVATION_CHECK_COVERAGE:{key}")


def _validate_review_card(
    key: str,
    body: dict[str, Any],
    records: dict[str, dict[str, Any]],
    behavior_ids: set[str],
    triage_digest: Any,
    planning_baseline_sha: Any,
    errors: list[str],
) -> None:
    _error(errors, body.get("triage_digest") == triage_digest, f"REVIEW_TRIAGE_DIGEST_MISMATCH:{key}")
    _error(errors, body.get("baseline_sha") == planning_baseline_sha, f"REVIEW_BASELINE_MISMATCH:{key}")
    declared_behavior_ids = _string_set(body.get("behavior_ids"))
    declared_impl_keys = _string_set(body.get("implementation_card_keys"))
    sources = _collect_review_sources(key, records, declared_impl_keys, errors)
    _error(errors, declared_behavior_ids == behavior_ids, f"REVIEW_BEHAVIOR_COVERAGE:{key}")
    acceptance = _validate_review_acceptance(
        key,
        body,
        declared_behavior_ids,
        declared_impl_keys,
        sources,
        errors,
    )
    _validate_acceptance_coverage(
        key,
        acceptance,
        declared_behavior_ids,
        declared_impl_keys,
        sources,
        errors,
    )
    checks = _validate_review_checks(
        key,
        body,
        declared_behavior_ids,
        declared_impl_keys,
        acceptance.review_ids,
        sources,
        errors,
    )
    _validate_check_coverage(
        key,
        checks,
        acceptance,
        declared_behavior_ids,
        declared_impl_keys,
        sources,
        errors,
    )
    _validate_required_check_focuses(
        key,
        checks,
        declared_behavior_ids,
        declared_impl_keys,
        sources.implementation_behavior_ids,
        errors,
    )


def validate_review_links(
    records: dict[str, dict[str, Any]],
    reviews: tuple[str, ...],
    *,
    behavior_ids: set[str],
    implementation_keys: set[str],
    triage_digest: Any,
    planning_baseline_sha: Any,
) -> ReviewLinkValidation:
    """Validate Review references, source ownership, and aggregate coverage."""

    errors: list[str] = []
    latest_review = _latest_review_key(records, reviews)
    for key in reviews:
        body = records[key].get("body")
        if isinstance(body, dict):
            _validate_review_card(
                key,
                body,
                records,
                behavior_ids,
                triage_digest,
                planning_baseline_sha,
                errors,
            )
    if latest_review:
        latest_body = records[latest_review].get("body", {})
        _error(
            errors,
            _string_set(latest_body.get("implementation_card_keys")) == implementation_keys,
            "LATEST_REVIEW_IMPL_COVERAGE",
        )
    return ReviewLinkValidation(errors=tuple(errors), latest_review=latest_review)
