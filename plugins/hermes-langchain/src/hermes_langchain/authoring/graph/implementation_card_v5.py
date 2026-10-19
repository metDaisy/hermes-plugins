"""Legacy implementation-card v5 contract and validation."""

from __future__ import annotations

import json
import re
from typing import Any

from .. import triage_domain
from . import implementation_card_v6
from .contract_common import is_canonical_https_url

CURRENT_CARD_SCHEMA = "backend-implementation-card-v5"
TEST_LEVELS = {"unit", "slice", "repository", "integration", "modulith"}
CONTRACT_DIMENSIONS = {
    "actor_and_authorization",
    "api",
    "state_transition",
    "error_semantics",
    "persistence_and_transaction",
    "events",
}
APPLICABILITY = {"applicable", "not-applicable"}
GENERIC_CONTEXT_TEXT = "저장소 규칙: 적용 계층과 책임 경계를 유지하고 해당 공개 계약을 임의로 넓히지 않는다."
GENERIC_ENTRY_POINT_REASON = "이 조사 시작점에서 해당 책임 경계와 현재 호출 관계를 확인한다."
GENERIC_VERIFICATION_SCENARIO = "성공·실패·경계 조건이 해당 observable의 요구된 동작과 일치하는지 확인한다."
FORBIDDEN_FIELDS = {
    "baseline_sha",
    "planning_sha",
    "assignee",
    "status",
    "dependency",
    "dependencies",
    "depends_on",
    "depends_on_card_keys",
    "workspace",
    "run",
    "runs",
    "result",
    "results",
}
CARD_FIELDS = {
    "schema",
    "card_type",
    "issue",
    "triage_digest",
    "slice",
    "implementation_contract",
    "context",
    "acceptance",
    "verification",
}
HANGUL = re.compile(r"[가-힣]")
ALPHABETIC = re.compile(r"[A-Za-z가-힣]")

def _load_triage_domain() -> Any:
    return triage_domain


def _load_implementation_card_v6() -> Any:
    return implementation_card_v6


def _validate_frozen_triage(body: dict[str, Any]) -> list[str]:
    try:
        return _load_triage_domain().validate(body, "triage")
    except (ImportError, OSError):
        return ["TRIAGE_VALIDATOR_UNAVAILABLE"]


def _validate_implementation_v6(body: Any) -> list[str]:
    try:
        return _load_implementation_card_v6().validate(body)
    except (ImportError, OSError):
        return ["IMPLEMENTATION_V6_VALIDATOR_UNAVAILABLE"]


def _non_empty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _strings(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(_non_empty(item) for item in value)


def _error(errors: list[str], condition: bool, code: str) -> None:
    if not condition:
        errors.append(code)


def _unexpected_fields(value: dict[str, Any], allowed: set[str], errors: list[str], prefix: str) -> None:
    for field in sorted(set(value) - allowed):
        errors.append(f"{prefix}:{field}")


def _validate_korean(value: Any, errors: list[str], field: str) -> None:
    if not _non_empty(value):
        return
    hangul_count = len(HANGUL.findall(value))
    if hangul_count < 2:
        errors.append(f"DESCRIPTION_NOT_KOREAN:{field}")


def _validate_korean_strings(value: Any, errors: list[str], field: str) -> None:
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_korean(item, errors, f"{field}:{index}")


def _contract_change_marker(value: Any) -> bool:
    return isinstance(value, str) and re.match(
        r"^(?:[A-Z][A-Z0-9 -]+|cc-[a-z0-9-]+):\s+\S", value
    ) is not None


def _append_forbidden_fields(value: Any, errors: list[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in FORBIDDEN_FIELDS:
                code = f"FORBIDDEN_FIELD:{key}"
                if code not in errors:
                    errors.append(code)
            _append_forbidden_fields(child, errors)
    elif isinstance(value, list):
        for child in value:
            _append_forbidden_fields(child, errors)


def template(issue: int, issue_url: str) -> dict[str, Any]:
    """Create a fact-only card scaffold; PM authors all behavior and evidence."""
    return {
        "schema": CURRENT_CARD_SCHEMA,
        "card_type": "implementation",
        "issue": {"number": issue, "url": issue_url},
        "triage_digest": "",
        "slice": {
            "id": "",
            "title": "",
            "outcome": "",
            "behavior_ids": [],
            "gap_ids": [],
            "scope": [],
            "out_of_scope": [],
        },
        "implementation_contract": {
            dimension: {"applicability": "", "rules": []}
            for dimension in sorted(CONTRACT_DIMENSIONS)
        }
        | {"preserved_behaviors": []},
        "context": {
            "current_state": [],
            "entry_points": [],
            "constraints": [],
        },
        "acceptance": [],
        "verification": [],
    }


def _validate_source(value: Any, errors: list[str], label: str) -> None:
    if not isinstance(value, dict):
        errors.append(f"SOURCE_NOT_OBJECT:{label}")
        return
    _unexpected_fields(value, {"kind", "path", "heading"}, errors, f"UNEXPECTED_SOURCE_FIELD:{label}")
    if value.get("kind") != "repository":
        errors.append(f"INVALID_SOURCE_KIND:{label}")
    _error(errors, _non_empty(value.get("path")), f"MISSING_SOURCE_PATH:{label}")
    _error(errors, _non_empty(value.get("heading")), f"MISSING_SOURCE_HEADING:{label}")


def _validate_slice(value: Any, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append("MISSING_SLICE")
        return
    _unexpected_fields(
        value,
        {"id", "title", "outcome", "behavior_ids", "gap_ids", "scope", "out_of_scope"},
        errors,
        "UNEXPECTED_SLICE_FIELD",
    )
    _error(errors, _non_empty(value.get("id")), "MISSING_SLICE_ID")
    _error(errors, _non_empty(value.get("title")), "MISSING_SLICE_TITLE")
    _error(errors, _non_empty(value.get("outcome")), "MISSING_SLICE_OUTCOME")
    _validate_korean(value.get("title"), errors, "slice:title")
    _validate_korean(value.get("outcome"), errors, "slice:outcome")
    for field, code in (
        ("behavior_ids", "MISSING_SLICE_BEHAVIOR_IDS"),
        ("gap_ids", "MISSING_SLICE_GAP_IDS"),
        ("scope", "MISSING_SLICE_SCOPE"),
        ("out_of_scope", "MISSING_SLICE_OUT_OF_SCOPE"),
    ):
        field_value = value.get(field)
        _error(errors, _strings(field_value), code)
        if isinstance(field_value, list) and all(isinstance(item, str) for item in field_value):
            _error(errors, len(field_value) == len(set(field_value)), f"DUPLICATE_SLICE_VALUE:{field}")
    _validate_korean_strings(value.get("scope"), errors, "slice:scope")
    _validate_korean_strings(value.get("out_of_scope"), errors, "slice:out_of_scope")


def _validate_implementation_contract(value: Any, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append("MISSING_IMPLEMENTATION_CONTRACT")
        return
    allowed = CONTRACT_DIMENSIONS | {"preserved_behaviors"}
    _unexpected_fields(value, allowed, errors, "UNEXPECTED_IMPLEMENTATION_CONTRACT_FIELD")
    for dimension in sorted(CONTRACT_DIMENSIONS):
        contract = value.get(dimension)
        if not isinstance(contract, dict):
            errors.append(f"MISSING_IMPLEMENTATION_CONTRACT_DIMENSION:{dimension}")
            continue
        _unexpected_fields(
            contract,
            {"applicability", "rules"},
            errors,
            f"UNEXPECTED_IMPLEMENTATION_CONTRACT_DIMENSION_FIELD:{dimension}",
        )
        _error(
            errors,
            contract.get("applicability") in APPLICABILITY,
            f"INVALID_IMPLEMENTATION_CONTRACT_APPLICABILITY:{dimension}",
        )
        rules = contract.get("rules")
        _error(errors, _strings(rules), f"MISSING_IMPLEMENTATION_CONTRACT_RULES:{dimension}")
        _validate_korean_strings(rules, errors, f"implementation_contract:{dimension}")
    preserved = value.get("preserved_behaviors")
    _error(errors, _strings(preserved), "MISSING_PRESERVED_BEHAVIORS")
    if isinstance(preserved, list):
        for index, behavior in enumerate(preserved):
            if not _contract_change_marker(behavior):
                _validate_korean(
                    behavior,
                    errors,
                    f"implementation_contract:preserved_behaviors:{index}",
                )


def _validate_context(value: Any, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append("MISSING_CONTEXT")
        return
    _unexpected_fields(value, {"current_state", "entry_points", "constraints"}, errors, "UNEXPECTED_CONTEXT_FIELD")
    for field, text_field in (("current_state", "fact"), ("constraints", "rule")):
        entries = value.get(field)
        if not isinstance(entries, list) or not entries:
            errors.append(f"MISSING_CONTEXT_{field.upper()}")
            continue
        for index, entry in enumerate(entries):
            label = f"{field}:{index}"
            if not isinstance(entry, dict):
                errors.append(f"CONTEXT_ENTRY_NOT_OBJECT:{label}")
                continue
            _unexpected_fields(entry, {text_field, "source"}, errors, f"UNEXPECTED_CONTEXT_ENTRY_FIELD:{label}")
            _error(errors, _non_empty(entry.get(text_field)), f"MISSING_CONTEXT_TEXT:{label}")
            _validate_korean(entry.get(text_field), errors, f"context:{label}")
            if entry.get(text_field) == GENERIC_CONTEXT_TEXT:
                errors.append(f"GENERIC_CONTEXT_TEXT:{label}")
            _validate_source(entry.get("source"), errors, label)
    entry_points = value.get("entry_points")
    if not isinstance(entry_points, list) or not entry_points:
        errors.append("MISSING_CONTEXT_ENTRY_POINTS")
    else:
        for index, entry in enumerate(entry_points):
            if not isinstance(entry, dict):
                errors.append(f"ENTRY_POINT_NOT_OBJECT:{index}")
                continue
            _unexpected_fields(entry, {"path", "symbol", "reason"}, errors, f"UNEXPECTED_ENTRY_POINT_FIELD:{index}")
            for field in ("path", "symbol", "reason"):
                _error(errors, _non_empty(entry.get(field)), f"MISSING_ENTRY_POINT_{field.upper()}:{index}")
            if isinstance(entry.get("symbol"), str) and entry["symbol"].endswith(".java"):
                errors.append(f"ENTRY_POINT_SYMBOL_IS_FILE:{index}")
            if entry.get("reason") == GENERIC_ENTRY_POINT_REASON:
                errors.append(f"GENERIC_ENTRY_POINT_REASON:{index}")
            _validate_korean(entry.get("reason"), errors, f"entry_point:{index}:reason")


def _validate_acceptance(value: Any, errors: list[str]) -> set[str]:
    if not isinstance(value, list) or not value:
        errors.append("MISSING_ACCEPTANCE")
        return set()
    identifiers: set[str] = set()
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            errors.append(f"ACCEPTANCE_NOT_OBJECT:{index}")
            continue
        _unexpected_fields(
            entry,
            {"id", "criterion", "requirement_ids", "observable_ids"},
            errors,
            f"UNEXPECTED_ACCEPTANCE_FIELD:{index}",
        )
        identifier = entry.get("id")
        label = identifier if _non_empty(identifier) else str(index)
        _error(errors, _non_empty(identifier), f"MISSING_ACCEPTANCE_ID:{index}")
        if isinstance(identifier, str) and identifier:
            if identifier in identifiers:
                errors.append(f"DUPLICATE_ACCEPTANCE_ID:{identifier}")
            identifiers.add(identifier)
        _error(errors, _non_empty(entry.get("criterion")), f"MISSING_ACCEPTANCE_OUTCOME:{label}")
        _validate_korean(entry.get("criterion"), errors, f"acceptance:{label}")
        for field in ("requirement_ids", "observable_ids"):
            _error(errors, _strings(entry.get(field)), f"MISSING_ACCEPTANCE_{field.upper()}:{label}")
    return identifiers


def _validate_artifact_closure(card: dict[str, Any], errors: list[str]) -> None:
    acceptance = card.get("acceptance")
    context = card.get("context")
    if not isinstance(acceptance, list) or not isinstance(context, dict):
        return
    entry_points = context.get("entry_points")
    if not isinstance(entry_points, list):
        return

    artifacts: list[tuple[str, str]] = []
    for entry in entry_points:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        symbol = entry.get("symbol")
        if isinstance(path, str) and isinstance(symbol, str):
            artifacts.append((path.replace("\\", "/").lower(), symbol.lower()))

    for entry in acceptance:
        if not isinstance(entry, dict):
            continue
        identifier = entry.get("id")
        criterion = entry.get("criterion")
        if not isinstance(identifier, str) or not isinstance(criterion, str):
            continue
        normalized = criterion.lower()
        if not (
            "presentation dto" in normalized
            and "application command/result" in normalized
            and "분리" in normalized
        ):
            continue
        required = {
            "application-request": lambda path, symbol: "/application/dto/" in path
            and ("request" in path or "request" in symbol),
            "presentation-request": lambda path, symbol: "/presentation/dto/" in path
            and ("request" in path or "request" in symbol),
            "application-command": lambda path, symbol: "/application/command/" in path
            and ("command" in path or "command" in symbol),
            "application-response": lambda path, symbol: "/application/dto/" in path
            and ("response" in path or "response" in symbol),
            "presentation-response": lambda path, symbol: "/presentation/dto/" in path
            and ("response" in path or "response" in symbol),
            "application-result": lambda path, symbol: "/application/result/" in path
            and ("result" in path or "result" in symbol),
        }
        missing = [
            label
            for label, predicate in required.items()
            if not any(predicate(path, symbol) for path, symbol in artifacts)
        ]
        if missing:
            errors.append(
                f"INCOMPLETE_HTTP_DTO_RESULT_BOUNDARY:{identifier}:{','.join(missing)}"
            )
        if any("/application/response/" in path for path, _symbol in artifacts):
            errors.append(f"AMBIGUOUS_APPLICATION_OUTPUT_BOUNDARY:{identifier}")


def _validate_verification(value: Any, acceptance_ids: set[str], errors: list[str]) -> None:
    if not isinstance(value, list) or not value:
        errors.append("MISSING_VERIFICATION")
        for acceptance_id in sorted(acceptance_ids):
            errors.append(f"UNCOVERED_ACCEPTANCE:{acceptance_id}")
        return
    verification_ids: set[str] = set()
    covered: set[str] = set()
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            errors.append(f"VERIFICATION_NOT_OBJECT:{index}")
            continue
        _unexpected_fields(
            entry,
            {"id", "acceptance_ids", "test_level", "task", "tests", "scenarios"},
            errors,
            f"UNEXPECTED_VERIFICATION_FIELD:{index}",
        )
        identifier = entry.get("id")
        label = identifier if _non_empty(identifier) else str(index)
        _error(errors, _non_empty(identifier), f"MISSING_VERIFICATION_ID:{index}")
        if isinstance(identifier, str) and identifier:
            if identifier in verification_ids:
                errors.append(f"DUPLICATE_VERIFICATION_ID:{identifier}")
            verification_ids.add(identifier)
        references = entry.get("acceptance_ids")
        _error(errors, _strings(references), f"MISSING_ACCEPTANCE_IDS:{label}")
        if isinstance(references, list):
            for acceptance_id in references:
                if not isinstance(acceptance_id, str):
                    continue
                if acceptance_id not in acceptance_ids:
                    errors.append(f"UNKNOWN_ACCEPTANCE_ID:{label}:{acceptance_id}")
                else:
                    covered.add(acceptance_id)
        test_level = entry.get("test_level")
        _error(errors, isinstance(test_level, str) and test_level in TEST_LEVELS, f"INVALID_TEST_LEVEL:{label}")
        _error(errors, _non_empty(entry.get("task")), f"MISSING_VERIFICATION_TASK:{label}")
        tests = entry.get("tests")
        _error(errors, _strings(tests), f"MISSING_VERIFICATION_TESTS:{label}")
        if isinstance(tests, list):
            _error(
                errors,
                all(
                    isinstance(test, str)
                    and re.fullmatch(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+(?:#[A-Za-z_$][\w$]*)?", test)
                    for test in tests
                ),
                f"INVALID_VERIFICATION_TEST:{label}",
            )
        scenarios = entry.get("scenarios")
        _error(errors, _strings(scenarios), f"MISSING_REQUIRED_SCENARIOS:{label}")
        _validate_korean_strings(scenarios, errors, f"verification:{label}:scenarios")
        if isinstance(scenarios, list):
            for scenario_index, scenario in enumerate(scenarios):
                if scenario == GENERIC_VERIFICATION_SCENARIO:
                    errors.append(f"GENERIC_VERIFICATION_SCENARIO:{label}:{scenario_index}")
    for acceptance_id in sorted(acceptance_ids - covered):
        errors.append(f"UNCOVERED_ACCEPTANCE:{acceptance_id}")


def validate(card: Any) -> list[str]:
    """Return deterministic card contract failures without mutating Git or Kanban."""
    errors: list[str] = []
    if not isinstance(card, dict):
        return ["CARD_NOT_OBJECT"]
    _append_forbidden_fields(card, errors)
    for field in sorted(set(card) - CARD_FIELDS):
        errors.append(f"UNEXPECTED_CARD_FIELD:{field}")
    schema = card.get("schema")
    _error(errors, schema == CURRENT_CARD_SCHEMA, "SCHEMA_VERSION")
    _error(errors, card.get("card_type") == "implementation", "INVALID_CARD_TYPE")
    issue = card.get("issue")
    if not isinstance(issue, dict):
        errors.append("INVALID_ISSUE")
    else:
        _unexpected_fields(issue, {"number", "url"}, errors, "UNEXPECTED_ISSUE_FIELD")
        issue_number = issue.get("number")
        _error(
            errors,
            isinstance(issue_number, int) and not isinstance(issue_number, bool) and issue_number > 0,
            "INVALID_ISSUE_NUMBER",
        )
        issue_url = issue.get("url")
        _error(
            errors,
            is_canonical_https_url(issue_url),
            "INVALID_ISSUE_URL",
        )
    _error(errors, bool(re.fullmatch(r"[0-9a-f]{64}", card.get("triage_digest", ""))), "MISSING_TRIAGE_DIGEST")
    _validate_slice(card.get("slice"), errors)
    _validate_implementation_contract(card.get("implementation_contract"), errors)
    _validate_context(card.get("context"), errors)
    acceptance_ids = _validate_acceptance(card.get("acceptance"), errors)
    _validate_artifact_closure(card, errors)
    _validate_verification(card.get("verification"), acceptance_ids, errors)
    return errors


def _implementation_schema(body: Any) -> str:
    return body.get("schema", "") if isinstance(body, dict) else ""


def _validate_implementation_body(body: Any) -> list[str]:
    if _implementation_schema(body) == "backend-implementation-card-v6":
        return _validate_implementation_v6(body)
    return validate(body)


def _implementation_triage_digest(body: Any) -> Any:
    if _implementation_schema(body) == "backend-implementation-card-v6":
        trace = body.get("trace") if isinstance(body, dict) else None
        return trace.get("triage_digest") if isinstance(trace, dict) else None
    return body.get("triage_digest") if isinstance(body, dict) else None


def _implementation_text(body: Any) -> str:
    return json.dumps(body, ensure_ascii=False, sort_keys=True) if isinstance(body, dict) else ""
