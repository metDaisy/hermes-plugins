"""Pure triage-v7 domain contract owned by KWC."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = "triage-v7"
PHASES = {"intake", "analysis", "frozen"}
STATUSES = {"triage", "todo", "ready", "running", "review", "blocked", "done", "archived"}
COVERAGE = {"covered", "partial", "missing"}
TARGET_DISPOSITIONS = {"gap", "preserve", "compliant", "out-of-scope"}
DOCUMENT_DISPOSITIONS = {"update", "no-change", "not-applicable", "blocked"}
DECISION_STATUSES = {"pending", "approved", "rejected", "deferred"}
INTENT_AMENDMENT_FIELDS = {
    "change_requirements": {"source"},
    "preserved_observables": {"source"},
    "quality_gates": {"source"},
    "finalization_requirements": {"outcome", "source"},
}
SHA = re.compile(r"^[0-9a-f]{40}$")
TASK_ID = re.compile(r"^t_[0-9a-f]+$")
HANGUL = re.compile(r"[가-힣]")
EPHEMERAL_COORDINATOR_MARKERS = (
    "이 작업에서는",
    "downstream card lifecycle",
    "이번 단계",
    "이번 호출",
    "이번 세션",
    "이번 triage",
    "this phase",
    "this invocation",
    "this call",
    "this session",
)
UNRESOLVED_ALTERNATIVE_MARKERS = (
    "사용하거나",
    "선택하거나",
    "and/or",
    "use either",
    "choose either",
)
CROSS_MODULE_CAPABILITY_MARKERS = ("capability", "snapshot", "projection", " fact")
CROSS_MODULE_FACT_GROUPS = {
    "price": ("가격", "price"),
    "status": ("판매 가능", "판매 상태", "availability", "available", "status"),
    "stock": ("재고", "stock"),
    "purchase-limit": ("구매 한도", "구매 제한", "purchase limit", "max purchase"),
}
ERROR_OWNERSHIP_MARKERS = ("오류 소유", "error ownership", "error origin", "오류의 producer")

TOP_LEVEL_FIELDS = {
    "schema",
    "phase",
    "frozen_digest",
    "issue",
    "baseline",
    "intent",
    "investigation_targets",
    "confirmed_gaps",
    "target_dispositions",
    "implementation_slices",
    "verification_matrix",
    "contract_changes",
    "document_impacts",
    "open_decisions",
    "handoff",
}


class JsonInputError(ValueError):
    """Raised when an input is not strict JSON."""


def _error(errors: list[str], condition: bool, code: str) -> None:
    if not condition:
        errors.append(code)


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _korean_description(value: Any) -> bool:
    return isinstance(value, str) and len(HANGUL.findall(value)) >= 2


def _repository_source(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    locator = str(value.get("locator") or "").strip()
    return bool(locator) and not locator.startswith(("http://", "https://"))


def _string_list(value: Any, *, nonempty: bool = False) -> bool:
    return (
        isinstance(value, list)
        and (bool(value) or not nonempty)
        and all(_non_empty_string(item) for item in value)
    )


def _contains_non_finite_number(value: Any) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_contains_non_finite_number(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_non_finite_number(item) for item in value)
    return False


def _contains_ephemeral_coordinator_context(value: Any) -> bool:
    if isinstance(value, str):
        normalized = value.casefold()
        return any(
            re.search(
                rf"(?<![0-9a-z_]){re.escape(marker.casefold())}(?![0-9a-z_])",
                normalized,
            )
            is not None
            for marker in EPHEMERAL_COORDINATOR_MARKERS
        )
    if isinstance(value, dict):
        return any(_contains_ephemeral_coordinator_context(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_ephemeral_coordinator_context(item) for item in value)
    return False


def _unresolved_implementation_alternatives(body: dict[str, Any]) -> list[str]:
    candidates: list[tuple[str, Any]] = []
    intent = body.get("intent")
    if isinstance(intent, dict):
        requirements = intent.get("change_requirements")
        if isinstance(requirements, list):
            candidates.extend(
                (
                    f"intent.change_requirements[{item.get('id', index)}].outcome",
                    item.get("outcome"),
                )
                for index, item in enumerate(requirements)
                if isinstance(item, dict)
            )
    gaps = body.get("confirmed_gaps")
    if isinstance(gaps, list):
        candidates.extend(
            (f"confirmed_gaps[{item.get('id', index)}].required_state", item.get("required_state"))
            for index, item in enumerate(gaps)
            if isinstance(item, dict)
        )
    slices = body.get("implementation_slices")
    if isinstance(slices, list):
        for slice_index, item in enumerate(slices):
            if not isinstance(item, dict):
                continue
            slice_id = item.get("id", slice_index)
            candidates.append((f"implementation_slices[{slice_id}].outcome", item.get("outcome")))
            scope = item.get("scope")
            if isinstance(scope, list):
                candidates.extend(
                    (f"implementation_slices[{slice_id}].scope[{index}]", value)
                    for index, value in enumerate(scope)
                )
            acceptance = item.get("acceptance")
            if isinstance(acceptance, list):
                candidates.extend(
                    (
                        f"implementation_slices[{slice_id}].acceptance["
                        f"{criterion.get('id', index)}].criterion",
                        criterion.get("criterion"),
                    )
                    for index, criterion in enumerate(acceptance)
                    if isinstance(criterion, dict)
                )
    return [
        f"{path}:{marker}"
        for path, value in candidates
        if isinstance(value, str)
        for marker in UNRESOLVED_ALTERNATIVE_MARKERS
        if marker.casefold() in f" {value.casefold()} "
    ]


def _contains_unresolved_implementation_alternative(body: dict[str, Any]) -> bool:
    return bool(_unresolved_implementation_alternatives(body))


def _exact_fields(value: Any, fields: set[str], errors: list[str], prefix: str) -> bool:
    if not isinstance(value, dict):
        errors.append(f"INVALID_{prefix}")
        return False
    actual = set(value)
    if actual != fields:
        errors.append(f"INVALID_{prefix}_FIELDS")
        for field in sorted(fields - actual):
            errors.append(f"MISSING_{prefix}_FIELD:{field}")
        for field in sorted(actual - fields):
            errors.append(f"UNEXPECTED_{prefix}_FIELD:{field}")
    return True


def _unique_id(value: Any, known: set[str], errors: list[str], prefix: str) -> str | None:
    identifier = value.get("id") if isinstance(value, dict) else None
    _error(errors, _non_empty_string(identifier), f"MISSING_{prefix}_ID")
    if not _non_empty_string(identifier):
        return None
    if identifier in known:
        errors.append(f"DUPLICATE_{prefix}:{identifier}")
    known.add(identifier)
    return identifier


def _validate_source(value: Any, errors: list[str], prefix: str) -> None:
    if not _exact_fields(value, {"locator", "claim"}, errors, prefix):
        return
    _error(errors, _non_empty_string(value.get("locator")), f"MISSING_{prefix}_LOCATOR")
    _error(errors, _non_empty_string(value.get("claim")), f"MISSING_{prefix}_CLAIM")


def _validate_sources(
    value: Any,
    errors: list[str],
    prefix: str,
    *,
    nonempty: bool = True,
    unique_locators: bool = False,
) -> None:
    _error(errors, isinstance(value, list), f"INVALID_{prefix}")
    if not isinstance(value, list):
        return
    if nonempty:
        _error(errors, bool(value), f"MISSING_{prefix}")
    locators: set[str] = set()
    for index, item in enumerate(value):
        _validate_source(item, errors, f"{prefix}:{index}")
        if unique_locators and isinstance(item, dict):
            locator = item.get("locator")
            if _non_empty_string(locator):
                if locator in locators:
                    errors.append(f"DUPLICATE_{prefix}_LOCATOR:{locator}")
                locators.add(locator)


def _validate_korean_source_claims(value: Any, errors: list[str], prefix: str) -> None:
    if not isinstance(value, list):
        return
    for index, source in enumerate(value):
        if not isinstance(source, dict):
            continue
        _error(
            errors,
            _korean_description(source.get("claim")),
            f"DESCRIPTION_NOT_KOREAN:{prefix}:{index}:claim",
        )


def _is_test_evidence(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    locator = value.get("locator")
    if not _non_empty_string(locator):
        return False
    normalized = locator.replace("\\", "/").lower()
    path = normalized.split("#", 1)[0]
    return (
        path.startswith("tests/")
        or "/tests/" in path
        or path.startswith("src/test/")
        or "/src/test/" in path
        or path.endswith("test.java")
        or path.endswith("tests.java")
        or path.endswith("_test.py")
        or path.endswith(".spec.ts")
        or path.endswith(".test.ts")
    )


def _is_process_only_outcome(value: Any) -> bool:
    if not _non_empty_string(value):
        return False
    normalized = value.strip().casefold().rstrip(".!?")
    words = normalized.split()
    english_first_word = words[0]
    if english_first_word in {"prepare", "review", "organize", "investigate", "analyze", "check"}:
        return True
    if not normalized.endswith(("준비한다", "검토한다", "정리한다", "조사한다", "분석한다", "확인한다")):
        return False
    # A substantive outcome may naturally end with a process verb (e.g. "... 책임을 분리해 정리한다").
    # Treat only short, marker-free sentences as process-only so the validator does not reject real behavior.
    substantive_markers = (
        "api", "http", "dto", "response", "status", "binding", "mapping", "error", "code", "contract",
        "invariant", "transaction", "persistence", "lock", "ownership", "cookie", "item", "quantity",
        "offer", "module", "port", "adapter", "aggregate", "service", "uri", "예외", "오류", "응답",
        "상태", "경계", "불변", "트랜잭션", "저장", "쿠키", "수량", "재고", "가격", "구매",
    )
    return len(words) <= 5 and not any(marker in normalized for marker in substantive_markers)


def _validate_requirement_entries(
    value: Any,
    errors: list[str],
    prefix: str,
    content_field: str,
    *,
    nonempty: bool,
) -> set[str]:
    _error(errors, isinstance(value, list), f"INVALID_{prefix}")
    known: set[str] = set()
    if not isinstance(value, list):
        return known
    if nonempty:
        _error(errors, bool(value), f"MISSING_{prefix}")
    for index, item in enumerate(value):
        item_prefix = f"{prefix}:{index}"
        if not _exact_fields(item, {"id", content_field, "source"}, errors, item_prefix):
            continue
        _unique_id(item, known, errors, prefix)
        _error(errors, _non_empty_string(item.get(content_field)), f"MISSING_{item_prefix}_{content_field.upper()}")
        _validate_source(item.get("source"), errors, f"{item_prefix}_SOURCE")
    return known


def template(
    issue_number: int,
    title: str,
    issue_url: str,
    planning_sha: str,
    current_state_sha: str,
    requirement_locators: list[str],
    current_state_locators: list[str],
) -> dict[str, Any]:
    """Return an intake template containing only caller-supplied identity."""
    return {
        "schema": SCHEMA,
        "phase": "intake",
        "frozen_digest": None,
        "issue": {"number": issue_number, "title": title, "url": issue_url},
        "baseline": {
            "planning_sha": planning_sha,
            "requirement_locators": requirement_locators,
            "current_state": {
                "snapshot_sha": current_state_sha,
                "freshness": "fresh",
                "usage": "read-only",
                "locators": current_state_locators,
            },
        },
        "intent": {
            "goal": None,
            "change_requirements": [],
            "preserved_observables": [],
            "quality_gates": [],
            "finalization_requirements": [],
            "out_of_scope": [],
        },
        "investigation_targets": [],
        "confirmed_gaps": [],
        "target_dispositions": [],
        "implementation_slices": [],
        "verification_matrix": [],
        "contract_changes": [],
        "document_impacts": [],
        "open_decisions": [],
        "handoff": {"graph_allowed": False, "blocker": "analysis-incomplete"},
    }


def compose_intake(base: dict[str, Any], authoring: dict[str, Any]) -> dict[str, Any]:
    """Place PM-authored intake fields into the canonical Triage schema."""
    if not isinstance(authoring, dict):
        raise ValueError("INVALID_INTAKE_AUTHORING")

    expected = {"intent", "investigation_targets"}
    authoring_errors = [
        *(f"MISSING_INTAKE_AUTHORING_FIELD:{field}" for field in sorted(expected - set(authoring))),
        *(f"UNEXPECTED_INTAKE_AUTHORING_FIELD:{field}" for field in sorted(set(authoring) - expected)),
    ]

    composed = copy.deepcopy(base)
    if "intent" in authoring:
        composed["intent"] = copy.deepcopy(authoring["intent"])
    if "investigation_targets" in authoring:
        composed["investigation_targets"] = copy.deepcopy(authoring["investigation_targets"])
    intent = authoring.get("intent")
    invalid_out_of_scope = (
        isinstance(intent, dict)
        and not _string_list(intent.get("out_of_scope"), nonempty=True)
    )
    if invalid_out_of_scope:
        authoring_errors.append("INTAKE_OUT_OF_SCOPE_MUST_BE_NONEMPTY_STRING_LIST")

    validation_errors = validate(composed, "triage")
    if invalid_out_of_scope:
        validation_errors = [error for error in validation_errors if error != "MISSING_OUT_OF_SCOPE"]
    authoring_errors.extend(validation_errors)
    if authoring_errors:
        raise ValueError("INVALID_INTAKE_AUTHORING:" + ",".join(authoring_errors))
    return composed


def compose_analysis(intake: dict[str, Any], authoring: dict[str, Any]) -> dict[str, Any]:
    """Merge analysis-only authoring fields into a validated intake artifact."""
    if not isinstance(authoring, dict):
        raise ValueError("INVALID_ANALYSIS_AUTHORING")

    analysis_fields = {
        "confirmed_gaps",
        "target_dispositions",
        "implementation_slices",
        "verification_matrix",
        "contract_changes",
        "document_impacts",
        "open_decisions",
    }
    expected = {"intake_amendments", *analysis_fields}
    authoring_errors = [
        *(f"MISSING_ANALYSIS_AUTHORING_FIELD:{field}" for field in sorted(expected - set(authoring))),
        *(f"UNEXPECTED_ANALYSIS_AUTHORING_FIELD:{field}" for field in sorted(set(authoring) - expected)),
    ]
    if not isinstance(intake, dict) or intake.get("phase") != "intake" or validate(intake, "triage"):
        authoring_errors.append("ANALYSIS_BASE_MUST_BE_VALID_INTAKE")

    composed = copy.deepcopy(intake) if isinstance(intake, dict) else {}
    amendments = authoring.get("intake_amendments")
    amendment_fields = {
        "requirement_locators",
        "current_state_locators",
        "intent_entries",
        "investigation_targets",
    }
    if _exact_fields(amendments, amendment_fields, authoring_errors, "ANALYSIS_INTAKE_AMENDMENTS"):
        baseline = composed.get("baseline") if isinstance(composed.get("baseline"), dict) else {}
        current_state = (
            baseline.get("current_state")
            if isinstance(baseline.get("current_state"), dict)
            else {}
        )
        for field, target in (
            ("requirement_locators", baseline),
            ("current_state_locators", current_state),
        ):
            locators = amendments.get(field)
            if not _string_list(locators):
                authoring_errors.append(f"INVALID_ANALYSIS_AMENDMENT:{field}")
            elif locators:
                target["locators" if field == "current_state_locators" else field] = copy.deepcopy(locators)

        intent_updates = amendments.get("intent_entries")
        if not isinstance(intent_updates, list):
            authoring_errors.append("INVALID_ANALYSIS_AMENDMENT:intent_entries")
        else:
            seen_intent_updates: set[tuple[str, str]] = set()
            intent = composed.get("intent") if isinstance(composed.get("intent"), dict) else {}
            for index, update in enumerate(intent_updates):
                prefix = f"ANALYSIS_INTENT_AMENDMENT:{index}"
                if not _exact_fields(update, {"section", "id", "changes"}, authoring_errors, prefix):
                    continue
                section = update.get("section")
                identifier = update.get("id")
                changes = update.get("changes")
                key = (section, identifier)
                if key in seen_intent_updates:
                    authoring_errors.append(f"DUPLICATE_{prefix}:{section}:{identifier}")
                seen_intent_updates.add(key)
                allowed = INTENT_AMENDMENT_FIELDS.get(section)
                if allowed is None:
                    authoring_errors.append(f"UNKNOWN_{prefix}_SECTION:{section}")
                    continue
                if not _non_empty_string(identifier) or not isinstance(changes, dict) or not changes:
                    authoring_errors.append(f"INVALID_{prefix}")
                    continue
                for field in sorted(set(changes) - allowed):
                    authoring_errors.append(f"UNEXPECTED_{prefix}_CHANGE:{field}")
                entries = intent.get(section)
                matches = (
                    [item for item in entries if isinstance(item, dict) and item.get("id") == identifier]
                    if isinstance(entries, list)
                    else []
                )
                if len(matches) != 1:
                    authoring_errors.append(f"UNKNOWN_{prefix}_ID:{identifier}")
                    continue
                for field, value in changes.items():
                    if field in allowed:
                        matches[0][field] = copy.deepcopy(value)

        target_updates = amendments.get("investigation_targets")
        if not isinstance(target_updates, list):
            authoring_errors.append("INVALID_ANALYSIS_AMENDMENT:investigation_targets")
        else:
            seen_target_updates: set[str] = set()
            targets = composed.get("investigation_targets")
            for index, update in enumerate(target_updates):
                prefix = f"ANALYSIS_TARGET_AMENDMENT:{index}"
                if not _exact_fields(update, {"id", "changes"}, authoring_errors, prefix):
                    continue
                identifier = update.get("id")
                changes = update.get("changes")
                if identifier in seen_target_updates:
                    authoring_errors.append(f"DUPLICATE_{prefix}:{identifier}")
                if _non_empty_string(identifier):
                    seen_target_updates.add(identifier)
                allowed = {"locator", "question", "source"}
                if not _non_empty_string(identifier) or not isinstance(changes, dict) or not changes:
                    authoring_errors.append(f"INVALID_{prefix}")
                    continue
                for field in sorted(set(changes) - allowed):
                    authoring_errors.append(f"UNEXPECTED_{prefix}_CHANGE:{field}")
                matches = (
                    [item for item in targets if isinstance(item, dict) and item.get("id") == identifier]
                    if isinstance(targets, list)
                    else []
                )
                if len(matches) != 1:
                    authoring_errors.append(f"UNKNOWN_{prefix}_ID:{identifier}")
                    continue
                for field, value in changes.items():
                    if field in allowed:
                        matches[0][field] = copy.deepcopy(value)

    composed["phase"] = "analysis"
    composed["frozen_digest"] = None
    for field in analysis_fields:
        if field in authoring:
            composed[field] = copy.deepcopy(authoring[field])
    decisions = authoring.get("open_decisions")
    unresolved = (
        isinstance(decisions, list)
        and any(
            isinstance(item, dict) and item.get("status") in {"pending", "deferred"}
            for item in decisions
        )
    )
    composed["handoff"] = {
        "graph_allowed": False,
        "blocker": "decision-required" if unresolved else "freeze-required",
    }

    authoring_errors.extend(validate(composed, "triage"))
    if authoring_errors:
        raise ValueError("INVALID_ANALYSIS_AUTHORING:" + ",".join(authoring_errors))
    return composed


def _body_digest(body: dict[str, Any]) -> str:
    canonical = copy.deepcopy(body)
    canonical["frozen_digest"] = None
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_identity(body: dict[str, Any], errors: list[str]) -> None:
    issue = body.get("issue")
    if _exact_fields(issue, {"number", "title", "url"}, errors, "ISSUE"):
        _error(errors, isinstance(issue.get("number"), int) and issue["number"] > 0, "INVALID_ISSUE_NUMBER")
        _error(errors, _non_empty_string(issue.get("title")), "MISSING_ISSUE_TITLE")
        _error(errors, _non_empty_string(issue.get("url")), "MISSING_ISSUE_URL")

    baseline = body.get("baseline")
    if not _exact_fields(
        baseline,
        {"planning_sha", "requirement_locators", "current_state"},
        errors,
        "BASELINE",
    ):
        return
    _error(errors, isinstance(baseline.get("planning_sha"), str) and SHA.fullmatch(baseline["planning_sha"]) is not None, "INVALID_PLANNING_SHA")
    _error(errors, _string_list(baseline.get("requirement_locators"), nonempty=True), "MISSING_REQUIREMENT_LOCATORS")
    current = baseline.get("current_state")
    if _exact_fields(current, {"snapshot_sha", "freshness", "usage", "locators"}, errors, "CURRENT_STATE"):
        _error(errors, isinstance(current.get("snapshot_sha"), str) and SHA.fullmatch(current["snapshot_sha"]) is not None, "INVALID_CURRENT_STATE_SHA")
        _error(
            errors,
            current.get("freshness") in {"fresh", "committed-baseline-safe"},
            "CURRENT_STATE_NOT_FRESH",
        )
        _error(errors, current.get("usage") == "read-only", "CURRENT_STATE_NOT_READ_ONLY")
        _error(errors, _string_list(current.get("locators"), nonempty=True), "MISSING_CURRENT_STATE_LOCATORS")


def _fact_groups(text: str) -> set[str]:
    lowered = text.lower()
    return {
        group
        for group, markers in CROSS_MODULE_FACT_GROUPS.items()
        if any(marker in lowered for marker in markers)
    }


def _validate_cross_module_fact_observables(intent: dict[str, Any], errors: list[str]) -> None:
    requirements = intent.get("change_requirements")
    observables = intent.get("preserved_observables")
    if not isinstance(requirements, list) or not isinstance(observables, list):
        return
    observable_texts = []
    for observable in observables:
        if not isinstance(observable, dict):
            continue
        source = observable.get("source") if isinstance(observable.get("source"), dict) else {}
        text = f"{observable.get('behavior') or ''} {source.get('claim') or ''}".lower()
        if any(marker in text for marker in ERROR_OWNERSHIP_MARKERS):
            continue
        observable_texts.append(text)
    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        source = requirement.get("source") if isinstance(requirement.get("source"), dict) else {}
        text = f"{requirement.get('outcome') or ''} {source.get('claim') or ''}".lower()
        groups = _fact_groups(text)
        if len(groups) < 2 or not any(marker in text for marker in CROSS_MODULE_CAPABILITY_MARKERS):
            continue
        if not any(groups <= _fact_groups(observable_text) for observable_text in observable_texts):
            errors.append(
                f"MISSING_CROSS_MODULE_FACT_OBSERVABLE:{requirement.get('id') or 'unknown'}"
            )


def _validate_intent(body: dict[str, Any], errors: list[str]) -> tuple[set[str], set[str], set[str]]:
    intent = body.get("intent")
    fields = {
        "goal",
        "change_requirements",
        "preserved_observables",
        "quality_gates",
        "finalization_requirements",
        "out_of_scope",
    }
    if not _exact_fields(intent, fields, errors, "INTENT"):
        return set(), set(), set()
    _error(errors, _non_empty_string(intent.get("goal")), "MISSING_INTENT_GOAL")
    change_ids = _validate_requirement_entries(
        intent.get("change_requirements"), errors, "CHANGE_REQUIREMENT", "outcome", nonempty=True
    )
    observable_ids = _validate_requirement_entries(
        intent.get("preserved_observables"), errors, "PRESERVED_OBSERVABLE", "behavior", nonempty=False
    )
    quality_ids = _validate_requirement_entries(
        intent.get("quality_gates"), errors, "QUALITY_GATE", "outcome", nonempty=False
    )
    _validate_requirement_entries(
        intent.get("finalization_requirements"), errors, "FINALIZATION_REQUIREMENT", "outcome", nonempty=False
    )
    _validate_cross_module_fact_observables(intent, errors)
    _error(errors, _string_list(intent.get("out_of_scope"), nonempty=True), "MISSING_OUT_OF_SCOPE")
    return change_ids, observable_ids, quality_ids


def _validate_targets(body: dict[str, Any], errors: list[str]) -> set[str]:
    targets = body.get("investigation_targets")
    _error(errors, isinstance(targets, list), "INVALID_INVESTIGATION_TARGETS")
    known: set[str] = set()
    if not isinstance(targets, list):
        return known
    for index, target in enumerate(targets):
        prefix = f"INVESTIGATION_TARGET:{index}"
        if not _exact_fields(target, {"id", "locator", "question", "source"}, errors, prefix):
            continue
        _unique_id(target, known, errors, "INVESTIGATION_TARGET")
        _error(errors, _non_empty_string(target.get("locator")), f"MISSING_{prefix}_LOCATOR")
        _error(errors, _non_empty_string(target.get("question")), f"MISSING_{prefix}_QUESTION")
        _validate_source(target.get("source"), errors, f"{prefix}_SOURCE")
    return known


def _validate_gaps(
    body: dict[str, Any],
    errors: list[str],
    requirement_ids: set[str],
    observable_ids: set[str],
) -> tuple[set[str], set[str]]:
    gaps = body.get("confirmed_gaps")
    _error(errors, isinstance(gaps, list), "INVALID_CONFIRMED_GAPS")
    known: set[str] = set()
    mapped_requirements: set[str] = set()
    if not isinstance(gaps, list):
        return known, mapped_requirements
    fields = {
        "id",
        "subject",
        "current_state",
        "required_state",
        "evidence",
        "rules",
        "requirement_ids",
        "affected_observable_ids",
    }
    for index, gap in enumerate(gaps):
        prefix = f"CONFIRMED_GAP:{index}"
        if not _exact_fields(gap, fields, errors, prefix):
            continue
        gap_identifier = _unique_id(gap, known, errors, "CONFIRMED_GAP")
        for field in ("subject", "current_state", "required_state"):
            _error(errors, _non_empty_string(gap.get(field)), f"MISSING_{prefix}_{field.upper()}")
        _validate_sources(gap.get("evidence"), errors, f"{prefix}_EVIDENCE", unique_locators=True)
        _validate_sources(gap.get("rules"), errors, f"{prefix}_RULES", unique_locators=True)
        _validate_korean_source_claims(gap.get("evidence"), errors, f"{prefix}_EVIDENCE")
        _validate_korean_source_claims(gap.get("rules"), errors, f"{prefix}_RULES")
        rules = gap.get("rules")
        _error(
            errors,
            isinstance(rules, list) and any(_repository_source(rule) for rule in rules),
            f"MISSING_REPOSITORY_CONSTRAINT:CONFIRMED_GAP:{gap_identifier or index}",
        )
        ids = gap.get("requirement_ids")
        _error(errors, _string_list(ids, nonempty=True), f"MISSING_{prefix}_REQUIREMENT_IDS")
        if isinstance(ids, list):
            for identifier in ids:
                _error(errors, identifier in requirement_ids, f"UNKNOWN_{prefix}_REQUIREMENT:{identifier}")
                if identifier in requirement_ids:
                    mapped_requirements.add(identifier)
        affected = gap.get("affected_observable_ids")
        _error(errors, _string_list(affected), f"INVALID_{prefix}_AFFECTED_OBSERVABLE_IDS")
        if isinstance(affected, list):
            for identifier in affected:
                _error(errors, identifier in observable_ids, f"UNKNOWN_{prefix}_OBSERVABLE:{identifier}")
    return known, mapped_requirements


def _validate_target_dispositions(
    body: dict[str, Any], errors: list[str], target_ids: set[str], gap_ids: set[str]
) -> None:
    entries = body.get("target_dispositions")
    _error(errors, isinstance(entries, list), "INVALID_TARGET_DISPOSITIONS")
    observed: set[str] = set()
    if not isinstance(entries, list):
        return
    fields = {"target_id", "disposition", "finding_ids", "rationale"}
    for index, entry in enumerate(entries):
        prefix = f"TARGET_DISPOSITION:{index}"
        if not _exact_fields(entry, fields, errors, prefix):
            continue
        target_id = entry.get("target_id")
        _error(errors, _non_empty_string(target_id), f"MISSING_{prefix}_TARGET_ID")
        _error(errors, target_id in target_ids, f"UNKNOWN_{prefix}_TARGET:{target_id}")
        if _non_empty_string(target_id):
            if target_id in observed:
                errors.append(f"DUPLICATE_TARGET_DISPOSITION:{target_id}")
            observed.add(target_id)
        disposition = entry.get("disposition")
        _error(errors, disposition in TARGET_DISPOSITIONS, f"INVALID_{prefix}_DISPOSITION")
        finding_ids = entry.get("finding_ids")
        _error(errors, _string_list(finding_ids), f"INVALID_{prefix}_FINDING_IDS")
        if disposition == "gap":
            _error(errors, _string_list(finding_ids, nonempty=True), f"MISSING_{prefix}_FINDING_IDS")
        elif isinstance(finding_ids, list) and finding_ids:
            errors.append(f"NON_GAP_DISPOSITION_WITH_FINDINGS:{target_id}")
        if isinstance(finding_ids, list):
            for identifier in finding_ids:
                _error(errors, identifier in gap_ids, f"UNKNOWN_{prefix}_FINDING:{identifier}")
        _error(errors, _non_empty_string(entry.get("rationale")), f"MISSING_{prefix}_RATIONALE")
    for identifier in sorted(target_ids - observed):
        errors.append(f"UNRESOLVED_INVESTIGATION_TARGET:{identifier}")


def _validate_verification(
    body: dict[str, Any], errors: list[str], observable_ids: set[str]
) -> set[str]:
    entries = body.get("verification_matrix")
    _error(errors, isinstance(entries, list), "INVALID_VERIFICATION_MATRIX")
    known: set[str] = set()
    observed_observables: set[str] = set()
    if not isinstance(entries, list):
        return known
    fields = {
        "id",
        "observable_id",
        "current_coverage",
        "evidence",
        "production_seam",
        "coverage_gap",
        "required_verification",
    }
    for index, entry in enumerate(entries):
        prefix = f"VERIFICATION:{index}"
        if not _exact_fields(entry, fields, errors, prefix):
            continue
        identifier = _unique_id(entry, known, errors, "VERIFICATION")
        observable_id = entry.get("observable_id")
        _error(errors, observable_id in observable_ids, f"UNKNOWN_{prefix}_OBSERVABLE:{observable_id}")
        if observable_id in observable_ids:
            if observable_id in observed_observables:
                errors.append(f"DUPLICATE_VERIFICATION_OBSERVABLE:{observable_id}")
            observed_observables.add(observable_id)
        coverage = entry.get("current_coverage")
        _error(errors, coverage in COVERAGE, f"INVALID_{prefix}_COVERAGE")
        evidence = entry.get("evidence")
        _validate_sources(evidence, errors, f"{prefix}_EVIDENCE", nonempty=coverage in {"covered", "partial"})
        evidence_locators: set[str] = set()
        if isinstance(evidence, list):
            for source in evidence:
                locator = source.get("locator") if isinstance(source, dict) else None
                if not _non_empty_string(locator):
                    continue
                if locator in evidence_locators:
                    errors.append(
                        f"DUPLICATE_VERIFICATION_EVIDENCE_LOCATOR:{identifier}:{locator}"
                    )
                evidence_locators.add(locator)
        production_seam = entry.get("production_seam")
        _validate_source(production_seam, errors, f"{prefix}_PRODUCTION_SEAM")
        if isinstance(production_seam, dict):
            locator = production_seam.get("locator")
            _error(
                errors,
                _non_empty_string(locator) and locator.replace("\\", "/").startswith("src/main/"),
                f"INVALID_{prefix}_PRODUCTION_SEAM_LOCATOR",
            )
        if coverage == "covered":
            if not isinstance(evidence, list) or not any(_is_test_evidence(item) for item in evidence):
                errors.append(f"COVERED_VERIFICATION_WITHOUT_TEST_EVIDENCE:{identifier}")
            _error(errors, entry.get("coverage_gap") is None, f"COVERED_VERIFICATION_WITH_GAP:{identifier}")
        elif coverage == "partial":
            if not isinstance(evidence, list) or not any(_is_test_evidence(item) for item in evidence):
                errors.append(f"PARTIAL_VERIFICATION_WITHOUT_TEST_EVIDENCE:{identifier}")
            if not _non_empty_string(entry.get("coverage_gap")):
                errors.append(f"PARTIAL_VERIFICATION_WITHOUT_GAP:{identifier}")
        elif coverage == "missing":
            _error(errors, evidence == [], f"MISSING_VERIFICATION_WITH_EVIDENCE:{identifier}")
            _error(errors, _non_empty_string(entry.get("coverage_gap")), f"MISSING_VERIFICATION_WITHOUT_GAP:{identifier}")
        _error(errors, _string_list(entry.get("required_verification"), nonempty=True), f"MISSING_{prefix}_REQUIRED_VERIFICATION")
    for identifier in sorted(observable_ids - observed_observables):
        errors.append(f"UNMAPPED_PRESERVED_OBSERVABLE:{identifier}")
    return known


def _validate_slices(
    body: dict[str, Any],
    errors: list[str],
    gap_ids: set[str],
    requirement_ids: set[str],
    observable_ids: set[str],
    verification_ids: set[str],
    contract_change_ids: set[str],
) -> set[str]:
    slices = body.get("implementation_slices")
    _error(errors, isinstance(slices, list), "INVALID_IMPLEMENTATION_SLICES")
    known: set[str] = set()
    sliced_gaps: set[str] = set()
    mapped_contract_changes: set[str] = set()
    dependencies: list[tuple[str, list[Any], str]] = []
    if not isinstance(slices, list):
        return mapped_contract_changes
    fields = {
        "id", "title", "outcome", "gap_ids", "scope", "out_of_scope", "acceptance",
        "contract_change_ids", "depends_on",
    }
    for index, item in enumerate(slices):
        prefix = f"IMPLEMENTATION_SLICE:{index}"
        if not _exact_fields(item, fields, errors, prefix):
            continue
        identifier = _unique_id(item, known, errors, "IMPLEMENTATION_SLICE")
        for field in ("title", "outcome"):
            _error(errors, _non_empty_string(item.get(field)), f"MISSING_{prefix}_{field.upper()}")
            _error(
                errors,
                _korean_description(item.get(field)),
                f"DESCRIPTION_NOT_KOREAN:{prefix}:{field}",
            )
        if _is_process_only_outcome(item.get("outcome")):
            errors.append(f"NON_OBSERVABLE_IMPLEMENTATION_SLICE_OUTCOME:{identifier}")
        ids = item.get("gap_ids")
        _error(errors, _string_list(ids, nonempty=True), f"MISSING_{prefix}_GAP_IDS")
        if isinstance(ids, list):
            for gap_id in ids:
                _error(errors, gap_id in gap_ids, f"UNKNOWN_{prefix}_GAP:{gap_id}")
                if gap_id in gap_ids:
                    sliced_gaps.add(gap_id)
        _error(errors, _string_list(item.get("scope"), nonempty=True), f"MISSING_{prefix}_SCOPE")
        _error(errors, _string_list(item.get("out_of_scope"), nonempty=True), f"MISSING_{prefix}_OUT_OF_SCOPE")
        for field in ("scope", "out_of_scope"):
            values = item.get(field)
            if isinstance(values, list):
                for value_index, value in enumerate(values):
                    _error(
                        errors,
                        _korean_description(value),
                        f"DESCRIPTION_NOT_KOREAN:{prefix}:{field}:{value_index}",
                    )
        acceptance = item.get("acceptance")
        _error(errors, isinstance(acceptance, list) and bool(acceptance), f"MISSING_{prefix}_ACCEPTANCE")
        acceptance_ids: set[str] = set()
        if isinstance(acceptance, list):
            for acceptance_index, criterion in enumerate(acceptance):
                criterion_prefix = f"{prefix}_ACCEPTANCE:{acceptance_index}"
                criterion_fields = {"id", "criterion", "requirement_ids", "observable_ids", "verification_ids"}
                if not _exact_fields(criterion, criterion_fields, errors, criterion_prefix):
                    continue
                _unique_id(criterion, acceptance_ids, errors, f"{prefix}_ACCEPTANCE")
                _error(errors, _non_empty_string(criterion.get("criterion")), f"MISSING_{criterion_prefix}_CRITERION")
                _error(
                    errors,
                    _korean_description(criterion.get("criterion")),
                    f"DESCRIPTION_NOT_KOREAN:{criterion_prefix}:criterion",
                )
                for field, known_ids in (
                    ("requirement_ids", requirement_ids),
                    ("observable_ids", observable_ids),
                    ("verification_ids", verification_ids),
                ):
                    references = criterion.get(field)
                    _error(errors, _string_list(references, nonempty=field != "observable_ids"), f"MISSING_{criterion_prefix}_{field.upper()}")
                    if isinstance(references, list):
                        for reference in references:
                            _error(errors, reference in known_ids, f"UNKNOWN_{criterion_prefix}_{field.upper()}:{reference}")
        depends_on = item.get("depends_on")
        _error(errors, _string_list(depends_on), f"INVALID_{prefix}_DEPENDS_ON")
        if identifier is not None and isinstance(depends_on, list):
            if depends_on:
                errors.append(f"TRIAGE_DEPENDENCIES_DEFERRED_TO_GRAPH:{identifier}")
            dependencies.append((identifier, depends_on, prefix))
        contract_references = item.get("contract_change_ids")
        _error(errors, _string_list(contract_references), f"INVALID_{prefix}_CONTRACT_CHANGE_IDS")
        if isinstance(contract_references, list):
            for contract_id in contract_references:
                _error(
                    errors,
                    contract_id in contract_change_ids,
                    f"UNKNOWN_{prefix}_CONTRACT_CHANGE:{contract_id}",
                )
                if contract_id in contract_change_ids:
                    mapped_contract_changes.add(contract_id)
    for identifier, depends_on, prefix in dependencies:
        for dependency in depends_on:
            _error(errors, dependency in known and dependency != identifier, f"UNKNOWN_{prefix}_DEPENDENCY:{dependency}")
    for gap_id in sorted(gap_ids - sliced_gaps):
        errors.append(f"UNSLICED_CONFIRMED_GAP:{gap_id}")
    return mapped_contract_changes


def _validate_contract_changes(body: dict[str, Any], errors: list[str], verification_ids: set[str]) -> None:
    entries = body.get("contract_changes")
    _error(errors, isinstance(entries, list), "INVALID_CONTRACT_CHANGES")
    known: set[str] = set()
    if not isinstance(entries, list):
        return
    fields = {"id", "consumer", "producer", "change", "evidence", "verification_ids"}
    for index, entry in enumerate(entries):
        prefix = f"CONTRACT_CHANGE:{index}"
        if not _exact_fields(entry, fields, errors, prefix):
            continue
        _unique_id(entry, known, errors, "CONTRACT_CHANGE")
        for field in ("consumer", "producer", "change"):
            _error(errors, _non_empty_string(entry.get(field)), f"MISSING_{prefix}_{field.upper()}")
        _validate_sources(entry.get("evidence"), errors, f"{prefix}_EVIDENCE", unique_locators=True)
        references = entry.get("verification_ids")
        _error(errors, _string_list(references, nonempty=True), f"MISSING_{prefix}_VERIFICATION_IDS")
        if isinstance(references, list):
            for reference in references:
                _error(errors, reference in verification_ids, f"UNKNOWN_{prefix}_VERIFICATION:{reference}")


def _validate_document_impacts(body: dict[str, Any], errors: list[str]) -> None:
    entries = body.get("document_impacts")
    _error(errors, isinstance(entries, list), "INVALID_DOCUMENT_IMPACTS")
    seen: set[str] = set()
    if not isinstance(entries, list):
        return
    fields = {"document", "disposition", "evidence", "action"}
    for index, entry in enumerate(entries):
        prefix = f"DOCUMENT_IMPACT:{index}"
        if not _exact_fields(entry, fields, errors, prefix):
            continue
        document = entry.get("document")
        _error(errors, _non_empty_string(document), f"MISSING_{prefix}_DOCUMENT")
        if _non_empty_string(document):
            if document in seen:
                errors.append(f"DUPLICATE_DOCUMENT_IMPACT:{document}")
            seen.add(document)
        disposition = entry.get("disposition")
        _error(errors, disposition in DOCUMENT_DISPOSITIONS, f"INVALID_{prefix}_DISPOSITION")
        _validate_sources(
            entry.get("evidence"),
            errors,
            f"{prefix}_EVIDENCE",
            nonempty=disposition != "not-applicable",
            unique_locators=True,
        )
        if disposition == "update":
            _error(errors, _non_empty_string(entry.get("action")), f"MISSING_{prefix}_ACTION")
        else:
            _error(errors, entry.get("action") is None, f"UNEXPECTED_{prefix}_ACTION")


def _validate_decisions(body: dict[str, Any], errors: list[str], observable_ids: set[str]) -> None:
    decisions = body.get("open_decisions")
    _error(errors, isinstance(decisions, list), "INVALID_OPEN_DECISIONS")
    known: set[str] = set()
    if not isinstance(decisions, list):
        return
    authority_locators = {
        str(locator).split("#", 1)[0]
        for locator in body.get("baseline", {}).get("requirement_locators", [])
        if _non_empty_string(locator)
    }
    intent = body.get("intent")
    if isinstance(intent, dict):
        for section in (
            "change_requirements",
            "preserved_observables",
            "quality_gates",
            "finalization_requirements",
        ):
            entries = intent.get(section)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                source = entry.get("source") if isinstance(entry, dict) else None
                locator = source.get("locator") if isinstance(source, dict) else None
                if _non_empty_string(locator):
                    authority_locators.add(locator.split("#", 1)[0])
    fields = {
        "id",
        "question",
        "why_unresolved",
        "evidence",
        "affected_observable_ids",
        "options",
        "owner",
        "status",
        "approved_option_id",
    }
    for index, decision in enumerate(decisions):
        prefix = f"DECISION:{index}"
        if not _exact_fields(decision, fields, errors, prefix):
            continue
        decision_id = _unique_id(decision, known, errors, "DECISION")
        for field in ("question", "why_unresolved"):
            _error(errors, _non_empty_string(decision.get(field)), f"MISSING_{prefix}_{field.upper()}")
        _error(errors, decision.get("owner") == "user", f"DECISION_OWNER_MUST_BE_USER:{decision_id}")
        evidence = decision.get("evidence")
        _validate_sources(evidence, errors, f"{prefix}_EVIDENCE", unique_locators=True)
        evidence_locators = set()
        if isinstance(evidence, list):
            evidence_locators = {
                str(item.get("locator")).split("#", 1)[0]
                for item in evidence
                if isinstance(item, dict) and _non_empty_string(item.get("locator"))
            }
        _error(
            errors,
            bool(authority_locators & evidence_locators),
            f"DECISION_AUTHORITY_EVIDENCE_REQUIRED:{decision_id}",
        )
        affected = decision.get("affected_observable_ids")
        _error(errors, _string_list(affected), f"INVALID_{prefix}_AFFECTED_OBSERVABLE_IDS")
        if isinstance(affected, list):
            for observable_id in affected:
                _error(errors, observable_id in observable_ids, f"UNKNOWN_{prefix}_OBSERVABLE:{observable_id}")
        options = decision.get("options")
        _error(errors, isinstance(options, list) and len(options) >= 2, f"MISSING_{prefix}_OPTIONS")
        option_ids: set[str] = set()
        option_choices: set[str] = set()
        if isinstance(options, list):
            for option_index, option in enumerate(options):
                option_prefix = f"{prefix}_OPTION:{option_index}"
                if not _exact_fields(option, {"id", "choice", "impact", "preserves_observable_ids"}, errors, option_prefix):
                    continue
                option_id = _unique_id(option, option_ids, errors, f"{prefix}_OPTION")
                for field in ("choice", "impact"):
                    _error(errors, _non_empty_string(option.get(field)), f"MISSING_{option_prefix}_{field.upper()}")
                choice = option.get("choice")
                if _non_empty_string(choice):
                    normalized_choice = choice.strip().casefold()
                    if normalized_choice in option_choices:
                        errors.append(f"DUPLICATE_DECISION_CHOICE:{decision_id}:{option_id}")
                    option_choices.add(normalized_choice)
                preserved = option.get("preserves_observable_ids")
                _error(errors, _string_list(preserved), f"INVALID_{option_prefix}_PRESERVES_OBSERVABLE_IDS")
                if isinstance(affected, list) and isinstance(preserved, list):
                    for observable_id in affected:
                        if observable_id not in preserved:
                            errors.append(f"FIXED_OBSERVABLE_REOPENED:{decision_id}:{option_id}:{observable_id}")
        status = decision.get("status")
        _error(errors, status in DECISION_STATUSES, f"INVALID_{prefix}_STATUS")
        approved_option = decision.get("approved_option_id")
        if status == "approved":
            _error(errors, approved_option in option_ids, f"INVALID_{prefix}_APPROVED_OPTION")
        else:
            _error(errors, approved_option is None, f"UNEXPECTED_{prefix}_APPROVED_OPTION")


def validate(body: Any, status: str | None = None) -> list[str]:
    """Return deterministic triage-v7 errors without mutating Kanban."""
    if not isinstance(body, dict):
        return ["BODY_NOT_OBJECT"]
    errors: list[str] = []
    _error(errors, not _contains_non_finite_number(body), "NON_FINITE_NUMBER")
    _error(errors, not _contains_ephemeral_coordinator_context(body), "EPHEMERAL_COORDINATOR_CONTEXT")
    _error(errors, body.get("schema") == SCHEMA, "SCHEMA_VERSION")
    _exact_fields(body, TOP_LEVEL_FIELDS, errors, "V7_TOP_LEVEL")
    phase = body.get("phase")
    _error(errors, phase in PHASES, "INVALID_PHASE")
    if status is not None:
        _error(errors, status in STATUSES, "INVALID_NATIVE_STATUS")
        if phase in {"intake", "analysis"}:
            _error(errors, status == "triage", "ACTIVE_TRIAGE_MUST_REMAIN_NATIVE_TRIAGE")
        elif phase == "frozen":
            _error(errors, status in {"triage", "done"}, "FROZEN_TRIAGE_STATUS")
    _validate_identity(body, errors)
    requirement_ids, observable_ids, _ = _validate_intent(body, errors)
    target_ids = _validate_targets(body, errors)
    handoff = body.get("handoff")
    if _exact_fields(handoff, {"graph_allowed", "blocker"}, errors, "HANDOFF"):
        _error(errors, isinstance(handoff.get("graph_allowed"), bool), "INVALID_HANDOFF_GRAPH_ALLOWED")
        _error(errors, handoff.get("blocker") is None or _non_empty_string(handoff.get("blocker")), "INVALID_HANDOFF_BLOCKER")

    if phase == "intake":
        for field in (
            "confirmed_gaps",
            "target_dispositions",
            "implementation_slices",
            "verification_matrix",
            "contract_changes",
            "document_impacts",
            "open_decisions",
        ):
            _error(errors, body.get(field) == [], f"INTAKE_HAS_{field.upper()}")
        _error(errors, body.get("frozen_digest") is None, "INTAKE_HAS_FROZEN_DIGEST")
        if isinstance(handoff, dict):
            _error(errors, handoff.get("graph_allowed") is False, "INTAKE_GRAPH_ALLOWED")
            _error(errors, handoff.get("blocker") == "analysis-incomplete", "INVALID_INTAKE_BLOCKER")
        alternative_locations = _unresolved_implementation_alternatives(body)
        if alternative_locations:
            errors.append("UNRESOLVED_IMPLEMENTATION_ALTERNATIVE")
            errors.extend(
                f"UNRESOLVED_IMPLEMENTATION_ALTERNATIVE:{location}"
                for location in alternative_locations
            )
        return errors

    gap_ids, mapped_requirements = _validate_gaps(body, errors, requirement_ids, observable_ids)
    for requirement_id in sorted(requirement_ids - mapped_requirements):
        errors.append(f"UNMAPPED_CHANGE_REQUIREMENT:{requirement_id}")
    _validate_target_dispositions(body, errors, target_ids, gap_ids)
    verification_ids = _validate_verification(body, errors, observable_ids)
    raw_contract_changes = body.get("contract_changes")
    contract_change_ids = {
        item.get("id")
        for item in (raw_contract_changes if isinstance(raw_contract_changes, list) else [])
        if isinstance(item, dict) and _non_empty_string(item.get("id"))
    }
    mapped_contract_changes = _validate_slices(
        body,
        errors,
        gap_ids,
        requirement_ids,
        observable_ids,
        verification_ids,
        contract_change_ids,
    )
    _validate_contract_changes(body, errors, verification_ids)
    for contract_id in sorted(contract_change_ids - mapped_contract_changes):
        errors.append(f"UNMAPPED_CONTRACT_CHANGE:{contract_id}")
    _validate_document_impacts(body, errors)
    _validate_decisions(body, errors, observable_ids)

    decisions = body.get("open_decisions") if isinstance(body.get("open_decisions"), list) else []
    unresolved = [item for item in decisions if isinstance(item, dict) and item.get("status") in {"pending", "deferred"}]
    alternative_locations = _unresolved_implementation_alternatives(body)
    if alternative_locations and not unresolved:
        errors.append("UNRESOLVED_IMPLEMENTATION_ALTERNATIVE")
        errors.extend(
            f"UNRESOLVED_IMPLEMENTATION_ALTERNATIVE:{location}"
            for location in alternative_locations
        )
    if phase == "analysis":
        _error(errors, body.get("frozen_digest") is None, "ANALYSIS_HAS_FROZEN_DIGEST")
        if isinstance(handoff, dict):
            _error(errors, handoff.get("graph_allowed") is False, "ANALYSIS_GRAPH_ALLOWED")
            expected = "decision-required" if unresolved else "freeze-required"
            _error(errors, handoff.get("blocker") == expected, "INVALID_ANALYSIS_BLOCKER")
    if phase == "frozen":
        _error(errors, not unresolved, "FROZEN_WITH_OPEN_DECISIONS")
        if isinstance(handoff, dict):
            _error(errors, handoff.get("graph_allowed") is True, "FROZEN_GRAPH_NOT_ALLOWED")
            _error(errors, handoff.get("blocker") is None, "FROZEN_WITH_BLOCKER")
        digest = body.get("frozen_digest")
        _error(errors, isinstance(digest, str) and digest == _body_digest(body), "FROZEN_BODY_DIGEST_MISMATCH")
    return errors


def validate_repository_locators(body: Any, repo: Path) -> list[str]:
    """Verify repository-relative evidence locators resolve inside *repo*."""
    if not isinstance(body, dict):
        return ["BODY_NOT_OBJECT"]

    locator_values: set[str] = set()

    def collect(value: Any, key: str | None = None) -> None:
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                collect(child_value, child_key)
        elif isinstance(value, list):
            for child in value:
                collect(child, key)
        elif isinstance(value, str) and key in {"locator", "locators", "requirement_locators"}:
            locator_values.add(value.strip())

    collect(body)
    root = repo.resolve()
    local_prefixes = ("src/", "docs/", ".hermes/", "gradle/")
    local_files = {"AGENTS.md", "build.gradle", "settings.gradle"}
    errors: list[str] = []
    for locator in sorted(locator_values):
        normalized = locator.replace("\\", "/")
        path_text = normalized.split("#", 1)[0]
        path_text = re.sub(r":L?\d+(?:-L?\d+)?$", "", path_text)
        if not (path_text.startswith(local_prefixes) or path_text in local_files):
            continue
        candidate = (root / path_text).resolve()
        if not candidate.is_relative_to(root) or not candidate.exists():
            errors.append(f"MISSING_REPOSITORY_LOCATOR:{path_text}")
    return errors


def current_state_admission(inspection: Any) -> str | None:
    if not isinstance(inspection, dict):
        return None
    dirty_paths = inspection.get("dirty_paths")
    committed_baseline_safe = (
        inspection.get("freshness") == "insufficient"
        and inspection.get("worktree_clean") is False
        and isinstance(dirty_paths, list)
        and bool(dirty_paths)
        and all(
            isinstance(path, str)
            and path.replace("\\", "/").startswith((".hermes/", "hermes/"))
            for path in dirty_paths
        )
        and inspection.get("covered_changed_paths") == []
        and inspection.get("errors") == ["WORKTREE_DIRTY"]
    )
    if committed_baseline_safe:
        return "committed-baseline-safe"
    if inspection.get("freshness") == "fresh" and inspection.get("worktree_clean") is True:
        return "fresh"
    return None


def validate_current_state_inspection(body: Any, inspection: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(body, dict):
        return ["BODY_NOT_OBJECT"]
    if not isinstance(inspection, dict):
        return ["CURRENT_STATE_INSPECTION_NOT_OBJECT"]
    _error(errors, inspection.get("schema") == "current-state-inspection-v1", "CURRENT_STATE_INSPECTION_SCHEMA")
    admission = current_state_admission(inspection)
    _error(
        errors,
        admission is not None,
        "CURRENT_STATE_INSPECTION_NOT_FRESH",
    )
    _error(
        errors,
        admission is not None,
        "CURRENT_STATE_INSPECTION_DIRTY",
    )
    if "snapshot_is_ancestor" in inspection:
        _error(errors, inspection.get("snapshot_is_ancestor") is True, "CURRENT_STATE_SNAPSHOT_NOT_ANCESTOR")
    baseline = body.get("baseline") if isinstance(body.get("baseline"), dict) else {}
    current = baseline.get("current_state") if isinstance(baseline.get("current_state"), dict) else {}
    _error(
        errors,
        admission is None or current.get("freshness") == admission,
        "CURRENT_STATE_ADMISSION_MISMATCH",
    )
    _error(errors, inspection.get("inspection_sha") == baseline.get("planning_sha"), "CURRENT_STATE_INSPECTION_SHA_MISMATCH")
    _error(errors, inspection.get("snapshot_sha") == current.get("snapshot_sha"), "CURRENT_STATE_SNAPSHOT_SHA_MISMATCH")
    return errors


def freeze(body: dict[str, Any], inspection: Any | None = None) -> dict[str, Any]:
    """Freeze a resolved analysis body and open the downstream graph handoff."""
    if not isinstance(body, dict):
        raise ValueError("BODY_NOT_OBJECT")
    if body.get("phase") != "analysis":
        raise ValueError("FREEZE_REQUIRES_ANALYSIS_PHASE")
    if any(
        isinstance(item, dict) and item.get("status") in {"pending", "deferred"}
        for item in body.get("open_decisions", [])
    ):
        raise ValueError("OPEN_DECISIONS_PREVENT_FREEZE")
    candidate = copy.deepcopy(body)
    if inspection is not None:
        admission = current_state_admission(inspection)
        if admission is not None:
            candidate["baseline"]["current_state"]["freshness"] = admission
    errors = validate(candidate, "triage")
    if inspection is not None:
        errors.extend(validate_current_state_inspection(candidate, inspection))
    if errors:
        raise ValueError(errors[0])
    frozen = candidate
    frozen["phase"] = "frozen"
    frozen["handoff"] = {"graph_allowed": True, "blocker": None}
    frozen["frozen_digest"] = _body_digest(frozen)
    return frozen


def validate_card(
    task: dict[str, Any],
    requested_task_id: str,
    expected_status: str | None,
    expected_assignee: str | None,
    expected_issue_number: int,
    expected_issue_title: str,
    expected_issue_url: str,
) -> list[str]:
    errors: list[str] = []
    actual_id = task.get("id")
    _error(errors, isinstance(actual_id, str) and TASK_ID.fullmatch(actual_id) is not None, "INVALID_NATIVE_TASK_ID")
    _error(errors, actual_id == requested_task_id, "TASK_ID_MISMATCH")
    if expected_status is not None:
        _error(errors, task.get("status") == expected_status, "STATUS_MISMATCH")
    if expected_assignee is not None:
        _error(errors, task.get("assignee") == expected_assignee, "ASSIGNEE_MISMATCH")
    raw_body = task.get("body")
    if not isinstance(raw_body, str):
        return errors + ["MISSING_NATIVE_BODY"]
    try:
        body = _loads_json(raw_body)
    except JsonInputError:
        return errors + ["BODY_NOT_JSON"]
    errors.extend(validate(body, task.get("status")))
    issue = body.get("issue") if isinstance(body, dict) else None
    if isinstance(issue, dict):
        _error(errors, issue.get("number") == expected_issue_number, "ISSUE_NUMBER_MISMATCH")
        _error(errors, issue.get("title") == expected_issue_title, "ISSUE_TITLE_MISMATCH")
        _error(errors, issue.get("url") == expected_issue_url, "ISSUE_URL_MISMATCH")
    return errors


def decision_request_ids(decision_body: Any) -> set[str]:
    """Return request IDs from a singular or aggregate Decision card."""
    if not isinstance(decision_body, dict):
        return set()
    entries = decision_body.get("decision_requests")
    if isinstance(entries, list):
        return {
            item.get("id")
            for item in entries
            if isinstance(item, dict) and _non_empty_string(item.get("id"))
        }
    identifier = decision_body.get("decision_request_id")
    return {identifier} if _non_empty_string(identifier) else set()


def validate_decision_alignment(triage_body: Any, decision_body: Any) -> list[str]:
    """Compare a decision-card request list with triage-v7 open decisions."""
    if not isinstance(triage_body, dict):
        return ["TRIAGE_BODY_NOT_OBJECT"]
    if not isinstance(decision_body, dict):
        return ["DECISION_BODY_NOT_OBJECT"]
    expected = [
        item.get("id")
        for item in triage_body.get("open_decisions", [])
        if isinstance(item, dict) and _non_empty_string(item.get("id"))
    ]
    entries = decision_body.get("decision_requests")
    if not isinstance(entries, list):
        return ["DECISION_REQUESTS_INVALID"]
    actual = [
        item.get("id")
        for item in entries
        if isinstance(item, dict) and _non_empty_string(item.get("id"))
    ]
    aligned = (
        len(expected) == len(set(expected))
        and len(actual) == len(entries)
        and len(actual) == len(set(actual))
        and sorted(actual) == sorted(expected)
    )
    return [] if aligned else ["DECISION_REQUEST_COVERAGE_MISMATCH"]


def _reject_constant(value: str) -> None:
    raise JsonInputError("NON_FINITE_NUMBER")


def _loads_json(text: str) -> Any:
    def object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise JsonInputError(f"DUPLICATE_JSON_KEY:{key}")
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=object_pairs_hook, parse_constant=_reject_constant)
    except JsonInputError:
        raise
    except (json.JSONDecodeError, ValueError) as error:
        raise JsonInputError("INVALID_JSON") from error
    if _contains_non_finite_number(value):
        raise JsonInputError("NON_FINITE_NUMBER")
    return value



@dataclass(frozen=True)
class TriageIdentity:
    issue_number: int
    title: str
    issue_url: str
    planning_sha: str
    current_state_sha: str
    requirement_locators: tuple[str, ...]
    current_state_locators: tuple[str, ...]


class TriageContract:
    def template(self, identity: TriageIdentity) -> dict[str, Any]:
        return template(
            identity.issue_number,
            identity.title,
            identity.issue_url,
            identity.planning_sha,
            identity.current_state_sha,
            list(identity.requirement_locators),
            list(identity.current_state_locators),
        )


class ComposeIntake:
    def __init__(self, contract: TriageContract) -> None:
        self._contract = contract

    def execute(self, identity: TriageIdentity, authoring: dict[str, Any]) -> dict[str, Any]:
        return compose_intake(self._contract.template(identity), authoring)


class ComposeAnalysis:
    def execute(self, intake: dict[str, Any], authoring: dict[str, Any]) -> dict[str, Any]:
        return compose_analysis(intake, authoring)


class TriageValidator:
    def validate(self, body: Any, status: str | None = None) -> list[str]:
        return validate(body, status)

    def validate_repository_locators(self, body: Any, repo: Path) -> list[str]:
        return validate_repository_locators(body, repo)

    def validate_current_state_inspection(self, body: Any, inspection: Any) -> list[str]:
        return validate_current_state_inspection(body, inspection)


class FreezeTriage:
    def __init__(self, validator: TriageValidator) -> None:
        self._validator = validator

    def execute(self, body: dict[str, Any], inspection: Any | None = None) -> dict[str, Any]:
        if inspection is not None:
            errors = self._validator.validate_current_state_inspection(body, inspection)
            if errors:
                raise ValueError(errors[0])
        return freeze(body)


@dataclass(frozen=True)
class TriageModule:
    contract: TriageContract
    compose_intake: ComposeIntake
    compose_analysis: ComposeAnalysis
    validator: TriageValidator
    freezer: FreezeTriage


def build_triage_module() -> TriageModule:
    contract = TriageContract()
    validator = TriageValidator()
    return TriageModule(
        contract=contract,
        compose_intake=ComposeIntake(contract),
        compose_analysis=ComposeAnalysis(),
        validator=validator,
        freezer=FreezeTriage(validator),
    )


__all__ = [
    "SCHEMA",
    "PHASES",
    "STATUSES",
    "TASK_ID",
    "JsonInputError",
    "template",
    "compose_intake",
    "compose_analysis",
    "validate",
    "validate_repository_locators",
    "validate_current_state_inspection",
    "freeze",
    "validate_card",
    "decision_request_ids",
    "validate_decision_alignment",
    "_loads_json",
    "TriageIdentity",
    "TriageContract",
    "ComposeIntake",
    "ComposeAnalysis",
    "TriageValidator",
    "FreezeTriage",
    "TriageModule",
    "build_triage_module",
]
