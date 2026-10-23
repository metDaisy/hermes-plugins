#!/usr/bin/env python3
"""Create and validate backend implementation card JSON drafts."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


_PLUGIN_SRC = Path(__file__).resolve().parents[3] / "src"
if str(_PLUGIN_SRC) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_SRC))
from hermes_langchain.authoring.graph import implementation_batch as _core_impl_batch
from hermes_langchain.authoring.graph.legacy_implementation_card import (
    _validate_implementation_body,
)

from contract_common import is_canonical_https_url, serialize_task_body
from graph_contract import (
    REVIEW_GUIDE_LOCATORS,
    TITLE,
    requirement_diff,
    validate_graph,
    validate_native_readback,
    validate_review_graph,
)
from review_fast_path import (
    create_review_native as _create_review_native,
    create_summary_native as _create_summary_native,
    prepare_review_fast_path as _prepare_review_fast_path,
    prepare_summary_fast_path as _prepare_summary_fast_path,
    regenerate_summary_native as _regenerate_summary_native,
    validate_review_fast_path as _validate_review_fast_path,
)

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
UNRESOLVED_ENTRY_POINT_SYMBOLS = {"", "?", "unknown", "unresolved", "todo", "tbd"}
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



def _validate_frozen_triage(body: dict[str, Any]) -> list[str]:
    triage_script = Path(__file__).resolve().parents[2] / "kwc-create-triage" / "scripts" / "triage.py"
    spec = importlib.util.spec_from_file_location("project_manager_create_triage", triage_script)
    if spec is None or spec.loader is None:
        return ["TRIAGE_VALIDATOR_UNAVAILABLE"]
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate(body, "triage")


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
        r"^(?:[A-Z][A-Z0-9-]+|cc-[a-z0-9-]+):\s+\S", value
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


def _unresolved_entry_point_symbol(value: Any) -> bool:
    return not isinstance(value, str) or value.strip().lower() in UNRESOLVED_ENTRY_POINT_SYMBOLS


def _split_locator(locator: str) -> tuple[str, str]:
    path, separator, heading = locator.partition("#")
    return path, heading if separator else "unknown"


def _entry_point_symbol(path: str, heading: str) -> str:
    if "#" in heading:
        return heading
    type_name = Path(path).stem
    if heading in ("unknown", type_name):
        return type_name if heading != "unknown" else heading
    return f"{type_name}#{heading}"


def impl_batch_template(
    triage: dict[str, Any], generation: int = 1, triage_id: str | None = None
) -> dict[str, Any]:
    """Prefill immutable Frozen Triage facts for Impl-only authoring."""
    issue = triage.get("issue") if isinstance(triage, dict) else None
    slices = triage.get("implementation_slices") if isinstance(triage, dict) else None
    gaps = triage.get("confirmed_gaps") if isinstance(triage, dict) else None
    matrix = triage.get("verification_matrix") if isinstance(triage, dict) else None
    contract_changes = triage.get("contract_changes", []) if isinstance(triage, dict) else []
    if triage.get("schema") != "triage-v7" or triage.get("phase") != "frozen":
        raise ValueError("FROZEN_TRIAGE_REQUIRED")
    if not isinstance(issue, dict) or not isinstance(slices, list) or not slices:
        raise ValueError("INVALID_TRIAGE_INPUT")
    if not isinstance(gaps, list) or not isinstance(matrix, list):
        raise ValueError("INVALID_TRIAGE_EVIDENCE")

    gap_by_id = {gap.get("id"): gap for gap in gaps if isinstance(gap, dict)}
    verification_by_id = {row.get("id"): row for row in matrix if isinstance(row, dict)}
    contract_change_by_id = {
        change.get("id"): change
        for change in contract_changes
        if isinstance(change, dict) and isinstance(change.get("id"), str)
    }
    cards: list[dict[str, Any]] = []
    card_count = len(slices)
    for index, source_slice in enumerate(slices, start=1):
        if not isinstance(source_slice, dict):
            raise ValueError(f"INVALID_TRIAGE_SLICE:{index}")
        body = template(issue.get("number"), issue.get("url"))
        body["triage_digest"] = triage.get("frozen_digest", "")
        acceptance = source_slice.get("acceptance")
        if not isinstance(acceptance, list) or not acceptance:
            raise ValueError(f"MISSING_TRIAGE_SLICE_ACCEPTANCE:{index}")
        body["slice"] = {
            "id": source_slice.get("id", ""),
            "title": source_slice.get("title", ""),
            "outcome": source_slice.get("outcome", ""),
            "behavior_ids": list(
                dict.fromkeys(
                    observable_id
                    for item in acceptance
                    if isinstance(item, dict)
                    for observable_id in item.get("observable_ids", [])
                    if isinstance(observable_id, str)
                )
            ),
            "gap_ids": list(source_slice.get("gap_ids", [])),
            "scope": list(source_slice.get("scope", [])),
            "out_of_scope": list(source_slice.get("out_of_scope", [])),
        }
        body["acceptance"] = [
            {
                key: item.get(key)
                for key in ("id", "criterion", "requirement_ids", "observable_ids")
            }
            for item in acceptance
            if isinstance(item, dict)
        ]

        current_state: list[dict[str, Any]] = []
        constraints: list[dict[str, Any]] = []
        for gap_id in body["slice"]["gap_ids"]:
            gap = gap_by_id.get(gap_id)
            if not isinstance(gap, dict):
                continue
            for evidence in gap.get("evidence", []):
                if not isinstance(evidence, dict) or not isinstance(evidence.get("locator"), str):
                    continue
                path, heading = _split_locator(evidence["locator"])
                if path.startswith("http://") or path.startswith("https://"):
                    continue
                current_state.append(
                    {
                        "fact": evidence.get("claim", ""),
                        "source": {"kind": "repository", "path": path, "heading": heading},
                    }
                )
            for rule in gap.get("rules", []):
                if not isinstance(rule, dict) or not isinstance(rule.get("locator"), str):
                    continue
                path, heading = _split_locator(rule["locator"])
                if path.startswith("http://") or path.startswith("https://"):
                    continue
                constraints.append(
                    {
                        "rule": rule.get("claim", ""),
                        "source": {"kind": "repository", "path": path, "heading": heading},
                    }
                )

        verification_ids = list(
            dict.fromkeys(
                verification_id
                for item in acceptance
                if isinstance(item, dict)
                for verification_id in item.get("verification_ids", [])
                if isinstance(verification_id, str)
            )
        )
        entry_points: list[dict[str, Any]] = []
        verification: list[dict[str, Any]] = []
        for verification_id in verification_ids:
            row = verification_by_id.get(verification_id, {})
            seam = row.get("production_seam") if isinstance(row, dict) else None
            if isinstance(seam, dict) and isinstance(seam.get("locator"), str):
                path, symbol = _split_locator(seam["locator"])
                candidate = {
                    "path": path,
                    "symbol": _entry_point_symbol(path, symbol),
                    "reason": seam.get("claim", ""),
                }
                if candidate not in entry_points:
                    entry_points.append(candidate)
            acceptance_ids = [
                item.get("id")
                for item in acceptance
                if isinstance(item, dict) and verification_id in item.get("verification_ids", [])
            ]
            verification.append(
                {
                    "id": verification_id,
                    "acceptance_ids": acceptance_ids,
                    "test_level": "",
                    "task": "test",
                    "tests": [],
                    "scenarios": list(row.get("required_verification", []))
                    if isinstance(row, dict)
                    else [],
                }
            )
        body["context"] = {
            "current_state": current_state,
            "entry_points": entry_points,
            "constraints": constraints,
        }
        body["verification"] = verification
        wrapper = {
            "ordinal": index,
            "title": f"G{generation}-Issue{issue.get('number')}-Impl{index}",
            "priority": card_count - index + 1,
            "body": body,
            "required_verification_ids": verification_ids,
        }
        if source_slice.get("depends_on", []):
            raise ValueError(f"TRIAGE_SLICE_DEPENDENCIES_NOT_DEFERRED:{index}")
        wrapper["depends_on_ordinals"] = []
        declared_contract_change_ids = source_slice.get("contract_change_ids", [])
        if not isinstance(declared_contract_change_ids, list):
            raise ValueError(f"INVALID_TRIAGE_SLICE_CONTRACT_CHANGES:{index}")
        relevant_contract_changes = [
            contract_change_by_id[contract_id]
            for contract_id in declared_contract_change_ids
            if contract_id in contract_change_by_id
        ]
        if len(relevant_contract_changes) != len(declared_contract_change_ids):
            raise ValueError(f"UNKNOWN_TRIAGE_SLICE_CONTRACT_CHANGE:{index}")
        wrapper["contract_changes"] = copy.deepcopy(relevant_contract_changes)
        wrapper["required_contract_symbols"] = list(
            dict.fromkeys(
                symbol
                for change in relevant_contract_changes
                for symbol in re.findall(
                    r"\b[a-z][A-Za-z0-9_$]*\([^)]*\)", str(change.get("change", ""))
                )
            )
        )
        if triage_id is not None:
            wrapper["parent_ids"] = [triage_id]
        cards.append(wrapper)
    return {
        "issue": issue.get("number"),
        "generation": generation,
        "cards": cards,
        "dependency_audit": {"edges": []},
    }


def prepare_impl_fast_path(
    source_envelope: dict[str, Any],
    active_tasks: list[dict[str, Any]],
    *,
    triage_id: str,
    issue: int,
    generation: int,
    workdir: Path | None = None,
) -> dict[str, Any]:
    task = source_envelope.get("task") if isinstance(source_envelope, dict) else None
    if not isinstance(task, dict) or task.get("id") != triage_id:
        raise ValueError("SOURCE_TRIAGE_ID_MISMATCH")
    if task.get("status") != "triage":
        raise ValueError(f"SOURCE_TRIAGE_STATUS_INVALID:{task.get('status')}")
    if task.get("workspace_kind") != "dir" or not isinstance(task.get("workspace_path"), str):
        raise ValueError("SOURCE_TRIAGE_WORKSPACE_INVALID")
    canonical_workdir = (workdir or Path.cwd()).resolve()
    if Path(task["workspace_path"]).resolve() != canonical_workdir:
        raise ValueError("SOURCE_TRIAGE_WORKSPACE_MISMATCH")
    raw_body = task.get("body")
    if not isinstance(raw_body, str):
        raise ValueError("SOURCE_TRIAGE_BODY_INVALID")
    triage = json.loads(raw_body)
    if triage.get("schema") != "triage-v7" or triage.get("phase") != "frozen":
        raise ValueError("FROZEN_TRIAGE_REQUIRED")
    triage_errors = _validate_frozen_triage(triage)
    if triage_errors:
        raise ValueError(f"SOURCE_TRIAGE_INVALID:{triage_errors[0]}")
    if triage.get("issue", {}).get("number") != issue:
        raise ValueError("SOURCE_TRIAGE_ISSUE_MISMATCH")
    active_impl = [
        item
        for item in active_tasks
        if isinstance(item, dict)
        and re.fullmatch(rf"G{generation}-Issue{issue}-Impl\d+", str(item.get("title", "")))
    ]
    if active_impl:
        raise ValueError(f"ACTIVE_IMPL_EXISTS:{len(active_impl)}")
    batch = impl_batch_template(triage, generation=generation, triage_id=triage_id)
    return {
        "triage_body": triage,
        "batch": batch,
        "summary": {
            "triage_id": triage_id,
            "status": task.get("status"),
            "issue": issue,
            "generation": generation,
            "slice_count": len(triage.get("implementation_slices", [])),
            "active_impl_count": 0,
            "triage_digest": triage.get("frozen_digest"),
        },
    }


def _parse_json_values(text: str) -> list[Any]:
    decoder = json.JSONDecoder()
    values: list[Any] = []
    index = 0
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        try:
            value, index = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            break
        values.extend(value if isinstance(value, list) else [value])
    return values


def apply_impl_completions(batch: dict[str, Any], completions: dict[str, Any]) -> dict[str, Any]:
    """Merge compact authored fields into an immutable-fact Impl batch."""
    cards = batch.get("cards") if isinstance(batch, dict) else None
    authored_cards = completions.get("cards") if isinstance(completions, dict) else None
    if not isinstance(cards, list) or not cards:
        raise ValueError("INVALID_IMPL_BATCH")
    if not isinstance(authored_cards, list) or len(authored_cards) != len(cards):
        raise ValueError("IMPL_COMPLETION_CARD_COUNT_MISMATCH")
    unexpected_completion_fields = sorted(set(completions) - {"cards", "dependency_audit"})
    if unexpected_completion_fields:
        raise ValueError(f"UNEXPECTED_IMPL_COMPLETIONS_FIELD:{unexpected_completion_fields[0]}")

    merged = json.loads(json.dumps(batch, ensure_ascii=False))
    authored_by_ordinal: dict[int, dict[str, Any]] = {}
    for item in authored_cards:
        if not isinstance(item, dict) or not isinstance(item.get("ordinal"), int):
            raise ValueError("INVALID_IMPL_COMPLETION_ORDINAL")
        ordinal = item["ordinal"]
        if ordinal in authored_by_ordinal:
            raise ValueError(f"DUPLICATE_IMPL_COMPLETION_ORDINAL:{ordinal}")
        authored_by_ordinal[ordinal] = item

    for card in merged["cards"]:
        ordinal = card.get("ordinal")
        authored = authored_by_ordinal.get(ordinal)
        if authored is None:
            raise ValueError(f"MISSING_IMPL_COMPLETION:{ordinal}")
        allowed_fields = {
            "ordinal",
            "implementation_contract",
            "entry_points",
            "verification",
            "depends_on_ordinals",
        }
        unexpected = sorted(set(authored) - allowed_fields)
        if unexpected:
            raise ValueError(f"UNEXPECTED_IMPL_COMPLETION_FIELD:{ordinal}:{unexpected[0]}")
        contract = authored.get("implementation_contract")
        if not isinstance(contract, dict):
            raise ValueError(f"MISSING_IMPL_COMPLETION_CONTRACT:{ordinal}")
        body = card["body"]
        body["implementation_contract"] = copy.deepcopy(contract)
        preserved_behaviors = body["implementation_contract"].get("preserved_behaviors")
        if not isinstance(preserved_behaviors, list):
            raise ValueError(f"INVALID_IMPL_COMPLETION_PRESERVED_BEHAVIORS:{ordinal}")
        for contract_change in card.get("contract_changes", []):
            if not isinstance(contract_change, dict):
                continue
            marker = f"{contract_change.get('id')}: {contract_change.get('change')}"
            if marker not in preserved_behaviors:
                preserved_behaviors.append(marker)

        authored_entry_points = authored.get("entry_points")
        if not isinstance(authored_entry_points, list) or not all(
            isinstance(entry_point, dict) for entry_point in authored_entry_points
        ):
            raise ValueError(f"INVALID_IMPL_COMPLETION_ENTRY_POINTS:{ordinal}")
        entry_points: list[dict[str, Any]] = []
        remaining_authored = list(authored_entry_points)
        seen_expected: set[tuple[Any, Any]] = set()
        for expected in body["context"]["entry_points"]:
            expected_symbol = expected.get("symbol")
            expected_key = (expected.get("path"), expected_symbol)
            if expected_key in seen_expected:
                continue
            seen_expected.add(expected_key)
            matching_index = next(
                (
                    index
                    for index, entry_point in enumerate(remaining_authored)
                    if entry_point.get("path") == expected.get("path")
                    and (
                        (
                            _unresolved_entry_point_symbol(expected_symbol)
                            and not _unresolved_entry_point_symbol(entry_point.get("symbol"))
                        )
                        or entry_point.get("symbol") == expected_symbol
                    )
                ),
                None,
            )
            if matching_index is None:
                entry_points.append(expected)
            else:
                entry_points.append(remaining_authored.pop(matching_index))
        entry_points.extend(remaining_authored)
        deduplicated_entry_points: list[dict[str, Any]] = []
        seen_entry_points: set[tuple[Any, Any]] = set()
        for entry_point in entry_points:
            entry_point_key = (entry_point.get("path"), entry_point.get("symbol"))
            if entry_point_key in seen_entry_points:
                continue
            seen_entry_points.add(entry_point_key)
            deduplicated_entry_points.append(entry_point)
        body["context"]["entry_points"] = deduplicated_entry_points
        unresolved = next(
            (
                entry_point
                for entry_point in deduplicated_entry_points
                if _unresolved_entry_point_symbol(entry_point.get("symbol"))
            ),
            None,
        )
        if unresolved is not None:
            raise ValueError(
                f"UNRESOLVED_IMPL_ENTRY_POINT_SYMBOL:{ordinal}:{unresolved.get('path')}"
            )
        authored_dependencies = authored.get(
            "depends_on_ordinals", card.get("depends_on_ordinals", [])
        )
        if not isinstance(authored_dependencies, list):
            raise ValueError(f"INVALID_IMPL_COMPLETION_DEPENDENCIES:{ordinal}")
        card["depends_on_ordinals"] = copy.deepcopy(authored_dependencies)

        authored_verification = authored.get("verification")
        expected_verification = body.get("verification")
        if not isinstance(authored_verification, list) or not isinstance(expected_verification, list):
            raise ValueError(f"INVALID_IMPL_COMPLETION_VERIFICATION:{ordinal}")
        authored_by_id = {
            item.get("id"): item for item in authored_verification if isinstance(item, dict)
        }
        expected_ids = [item.get("id") for item in expected_verification]
        if len(authored_by_id) != len(authored_verification) or set(authored_by_id) != set(expected_ids):
            raise ValueError(f"IMPL_COMPLETION_VERIFICATION_IDS_MISMATCH:{ordinal}")
        for verification in expected_verification:
            authored_item = authored_by_id[verification["id"]]
            unexpected_verification_fields = sorted(
                set(authored_item) - {"id", "test_level", "tests", "scenarios"}
            )
            if unexpected_verification_fields:
                raise ValueError(
                    f"UNEXPECTED_IMPL_COMPLETION_VERIFICATION_FIELD:{ordinal}:"
                    f"{unexpected_verification_fields[0]}"
                )
            for field in ("test_level", "tests", "scenarios"):
                verification[field] = authored_item.get(field)
    if "dependency_audit" in completions:
        merged["dependency_audit"] = copy.deepcopy(completions["dependency_audit"])
    return merged


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


def validate_impl_batch(batch: Any) -> list[str]:
    """Validate Impl-only fast-path wrappers before any native mutation."""
    if not isinstance(batch, dict):
        return ["IMPL_BATCH_NOT_OBJECT"]
    cards = batch.get("cards")
    if not isinstance(cards, list) or not cards:
        return ["MISSING_IMPL_BATCH_CARDS"]

    errors: list[str] = []
    _unexpected_fields(
        batch,
        {"issue", "generation", "cards", "dependency_audit"},
        errors,
        "UNEXPECTED_IMPL_BATCH_FIELD",
    )
    batch_issue = batch.get("issue")
    batch_generation = batch.get("generation")
    _error(errors, isinstance(batch_issue, int) and batch_issue > 0, "INVALID_IMPL_BATCH_ISSUE")
    _error(
        errors,
        isinstance(batch_generation, int) and batch_generation > 0,
        "INVALID_IMPL_BATCH_GENERATION",
    )
    titles: set[str] = set()
    generations: set[int] = set()
    issues: set[int] = set()
    parent_sets: set[tuple[str, ...] | None] = set()
    for index, wrapper in enumerate(cards):
        if not isinstance(wrapper, dict):
            errors.append(f"IMPL_BATCH_CARD_NOT_OBJECT:{index}")
            continue
        _unexpected_fields(
            wrapper,
            {
                "ordinal",
                "title",
                "priority",
                "body",
                "contract_changes",
                "required_contract_symbols",
                "required_verification_ids",
                "parent_ids",
                "depends_on_ordinals",
            },
            errors,
            f"UNEXPECTED_IMPL_BATCH_CARD_FIELD:{index}",
        )
        expected_ordinal = index + 1
        wrapper_ordinal = wrapper.get("ordinal")
        if wrapper_ordinal != expected_ordinal:
            errors.append(
                f"IMPL_WRAPPER_ORDINAL_MISMATCH:{index}:{wrapper_ordinal}:{expected_ordinal}"
            )
        expected_priority = len(cards) - index
        if wrapper.get("priority") != expected_priority:
            errors.append(
                f"IMPL_PRIORITY_MISMATCH:{index}:{wrapper.get('priority')}:{expected_priority}"
            )
        parent_ids = wrapper.get("parent_ids")
        if parent_ids is not None and (
            not isinstance(parent_ids, list)
            or len(parent_ids) != 1
            or not isinstance(parent_ids[0], str)
            or not parent_ids[0]
        ):
            errors.append(f"INVALID_IMPL_PARENT_IDS:{index}")
        elif parent_ids is None:
            parent_sets.add(None)
        else:
            parent_sets.add(tuple(parent_ids))
        title = wrapper.get("title")
        match = TITLE.fullmatch(title) if isinstance(title, str) else None
        if match is None or not match.group("kind").startswith("Impl"):
            errors.append(f"INVALID_IMPL_TITLE:{index}")
        else:
            generation = int(match.group("generation"))
            issue_number = int(match.group("issue"))
            ordinal = int(match.group("kind")[len("Impl") :])
            generations.add(generation)
            issues.add(issue_number)
            if ordinal != expected_ordinal:
                errors.append(
                    f"IMPL_TITLE_ORDINAL_MISMATCH:{index}:{ordinal}:{expected_ordinal}"
                )
            if title in titles:
                errors.append(f"DUPLICATE_IMPL_TITLE:{title}")
            titles.add(title)
            if isinstance(batch_generation, int) and generation != batch_generation:
                errors.append(f"IMPL_BATCH_GENERATION_MISMATCH:{index}")
            if isinstance(batch_issue, int) and issue_number != batch_issue:
                errors.append(f"IMPL_BATCH_ISSUE_MISMATCH:{index}")

        body = wrapper.get("body")
        for error in validate(body):
            errors.append(f"IMPL_BODY_INVALID:{index}:{error}")
        required_verification_ids = wrapper.get("required_verification_ids")
        if required_verification_ids is not None:
            if not isinstance(required_verification_ids, list) or not all(
                isinstance(verification_id, str) and verification_id
                for verification_id in required_verification_ids
            ):
                errors.append(f"INVALID_REQUIRED_VERIFICATION_IDS:{index}")
            else:
                actual_verification_ids = [
                    item.get("id")
                    for item in (
                        body.get("verification", []) if isinstance(body, dict) else []
                    )
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                ]
                missing = sorted(set(required_verification_ids) - set(actual_verification_ids))
                unexpected = sorted(set(actual_verification_ids) - set(required_verification_ids))
                duplicates = sorted(
                    verification_id
                    for verification_id in set(actual_verification_ids)
                    if actual_verification_ids.count(verification_id) > 1
                )
                if (
                    missing
                    or unexpected
                    or duplicates
                    or len(actual_verification_ids) != len(required_verification_ids)
                ):
                    errors.append(
                        f"IMPL_REQUIRED_VERIFICATION_IDS_MISMATCH:{index}:"
                        f"missing={','.join(missing) or '-'}:"
                        f"unexpected={','.join(unexpected) or '-'}:"
                        f"duplicates={','.join(duplicates) or '-'}"
                    )
        implementation_contract = (
            body.get("implementation_contract") if isinstance(body, dict) else None
        )
        preserved_behaviors = (
            implementation_contract.get("preserved_behaviors")
            if isinstance(implementation_contract, dict)
            else None
        )
        contract_rules = [
            rule
            for dimension in CONTRACT_DIMENSIONS
            for rule in (
                implementation_contract.get(dimension, {}).get("rules", [])
                if isinstance(implementation_contract, dict)
                and isinstance(implementation_contract.get(dimension), dict)
                else []
            )
            if isinstance(rule, str)
        ]
        entry_symbols = [
            entry.get("symbol")
            for entry in (
                body.get("context", {}).get("entry_points", [])
                if isinstance(body, dict) and isinstance(body.get("context"), dict)
                else []
            )
            if isinstance(entry, dict) and isinstance(entry.get("symbol"), str)
        ]
        for entry_index, entry_symbol in enumerate(entry_symbols):
            if _unresolved_entry_point_symbol(entry_symbol):
                errors.append(f"UNRESOLVED_IMPL_ENTRY_POINT_SYMBOL:{index}:{entry_index}")
        contract_changes = wrapper.get("contract_changes")
        if not isinstance(contract_changes, list) or not all(
            isinstance(change, dict) for change in contract_changes
        ):
            errors.append(f"INVALID_CONTRACT_CHANGES:{index}")
        else:
            for change in contract_changes:
                marker = f"{change.get('id')}: {change.get('change')}"
                if not isinstance(preserved_behaviors, list) or marker not in preserved_behaviors:
                    errors.append(f"CONTRACT_CHANGE_MISSING:{index}:{change.get('id')}")
        required_contract_symbols = wrapper.get("required_contract_symbols", [])
        if not isinstance(required_contract_symbols, list) or not all(
            isinstance(symbol, str) and symbol for symbol in required_contract_symbols
        ):
            errors.append(f"INVALID_REQUIRED_CONTRACT_SYMBOLS:{index}")
        else:
            for symbol in required_contract_symbols:
                in_contract = any(symbol in rule for rule in contract_rules)
                in_entry_point = any(
                    entry_symbol == symbol or entry_symbol.endswith(f"#{symbol}")
                    for entry_symbol in entry_symbols
                )
                if not in_contract and not in_entry_point:
                    errors.append(f"REQUIRED_CONTRACT_SYMBOL_MISSING:{index}:{symbol}")
        depends_on_ordinals = wrapper.get("depends_on_ordinals", [])
        invalid_dependency = None
        if not isinstance(depends_on_ordinals, list):
            invalid_dependency = "shape"
        elif len(depends_on_ordinals) != len(set(depends_on_ordinals)):
            invalid_dependency = "duplicate"
        else:
            invalid_dependency = next(
                (
                    dependency
                    for dependency in depends_on_ordinals
                    if not isinstance(dependency, int)
                    or dependency < 1
                    or dependency >= expected_ordinal
                ),
                None,
            )
        if invalid_dependency is not None:
            errors.append(f"INVALID_IMPL_DEPENDENCY:{index}:{invalid_dependency}")
        if match is not None and isinstance(body, dict):
            body_issue = body.get("issue")
            body_issue_number = body_issue.get("number") if isinstance(body_issue, dict) else None
            if body_issue_number != int(match.group("issue")):
                errors.append(f"IMPL_TITLE_ISSUE_MISMATCH:{index}")

    if len(generations) > 1:
        errors.append("IMPL_TITLE_GENERATION_MISMATCH")
    if len(issues) > 1:
        errors.append("IMPL_TITLE_ISSUE_SET_MISMATCH")
    if len(parent_sets) > 1:
        errors.append("IMPL_PARENT_SET_MISMATCH")
    dependency_audit = batch.get("dependency_audit")
    if not isinstance(dependency_audit, dict):
        errors.append("MISSING_IMPL_DEPENDENCY_AUDIT")
    else:
        expected_pairs = [
            (dependency, wrapper.get("ordinal"))
            for wrapper in cards
            if isinstance(wrapper, dict)
            for dependency in wrapper.get("depends_on_ordinals", [])
            if isinstance(dependency, int)
        ]
        audit_edges = dependency_audit.get("edges")
        if not isinstance(audit_edges, list):
            errors.append("IMPL_DEPENDENCY_AUDIT_MISMATCH")
        else:
            audit_pairs: list[tuple[Any, Any]] = []
            proof_fields = {
                "from_ordinal",
                "to_ordinal",
                "required_artifact",
                "consumer_entry_point",
                "why_baseline_cannot_satisfy",
            }
            for edge in audit_edges:
                if not isinstance(edge, dict):
                    errors.append("IMPL_DEPENDENCY_AUDIT_EDGE_NOT_OBJECT")
                    continue
                pair = (edge.get("from_ordinal"), edge.get("to_ordinal"))
                audit_pairs.append(pair)
                if set(edge) != proof_fields or not all(
                    isinstance(edge.get(field), str) and edge[field].strip()
                    for field in (
                        "required_artifact",
                        "consumer_entry_point",
                        "why_baseline_cannot_satisfy",
                    )
                ):
                    errors.append(f"IMPL_DEPENDENCY_PROOF_INCOMPLETE:{pair[0]}:{pair[1]}")
                    continue
                parent_ordinal, child_ordinal = pair
                if not (
                    isinstance(parent_ordinal, int)
                    and isinstance(child_ordinal, int)
                    and 1 <= parent_ordinal < child_ordinal <= len(cards)
                ):
                    errors.append(f"INVALID_IMPL_DEPENDENCY_AUDIT_EDGE:{parent_ordinal}:{child_ordinal}")
                    continue
                parent_text = json.dumps(cards[parent_ordinal - 1].get("body"), ensure_ascii=False)
                child_text = json.dumps(cards[child_ordinal - 1].get("body"), ensure_ascii=False)
                artifact = edge["required_artifact"]
                consumer = edge["consumer_entry_point"]
                if artifact not in parent_text or artifact not in child_text:
                    errors.append(
                        f"IMPL_DEPENDENCY_ARTIFACT_UNPROVEN:{parent_ordinal}:{child_ordinal}"
                    )
                if consumer not in child_text:
                    errors.append(
                        f"IMPL_DEPENDENCY_CONSUMER_UNPROVEN:{parent_ordinal}:{child_ordinal}"
                    )
            if audit_pairs != expected_pairs:
                errors.append("IMPL_DEPENDENCY_AUDIT_MISMATCH")
    return errors


def render_impl_batch(batch: dict[str, Any]) -> dict[str, dict[str, Any]]:
    errors = validate_impl_batch(batch)
    if errors:
        raise ValueError("INVALID_IMPL_BATCH:" + "|".join(errors))
    return {
        f"impl-{wrapper['ordinal']}.json": copy.deepcopy(wrapper["body"])
        for wrapper in batch["cards"]
    }


def _created_task_id(payload: Any, expected_title: str) -> str:
    candidates: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("title") == expected_title:
                for field in ("id", "task_id"):
                    candidate = value.get(field)
                    if isinstance(candidate, str) and candidate.startswith("t_"):
                        candidates.append(candidate)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    if not candidates and isinstance(payload, dict):
        for field in ("id", "task_id"):
            candidate = payload.get(field)
            if isinstance(candidate, str) and candidate.startswith("t_"):
                candidates.append(candidate)
    unique = list(dict.fromkeys(candidates))
    if len(unique) != 1:
        raise ValueError(f"CREATE_ID_NOT_UNIQUE:{len(unique)}")
    return unique[0]


def _parent_ids(parents: Any) -> list[str]:
    if not isinstance(parents, list):
        return []
    result: list[str] = []
    for parent in parents:
        if isinstance(parent, str):
            result.append(parent)
        elif isinstance(parent, dict):
            candidate = parent.get("id") or parent.get("task_id")
            if isinstance(candidate, str):
                result.append(candidate)
    return result


def _implementation_slice_id(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    if body.get("schema") == "backend-implementation-card-v6":
        trace = body.get("trace")
        return trace.get("slice_id") if isinstance(trace, dict) else None
    body_slice = body.get("slice")
    return body_slice.get("id") if isinstance(body_slice, dict) else None


def _implementation_acceptance_ids(
    body: Any, expected_ids: list[str]
) -> list[str]:
    if not isinstance(body, dict):
        return []
    if body.get("schema") == "backend-implementation-card-v6":
        expected = set(expected_ids)
        return [
            item.get("id")
            for field in ("obligations", "constraints")
            for item in body.get(field, [])
            if isinstance(item, dict) and item.get("id") in expected
        ]
    return [
        item.get("id")
        for item in body.get("acceptance", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]


def prepare_review_fast_path(
    active_tasks: list[dict[str, Any]],
    *,
    triage_id: str,
    implementation_ids: list[str],
    issue: int,
    generation: int,
) -> dict[str, Any]:
    return _prepare_review_fast_path(
        active_tasks,
        triage_id=triage_id,
        implementation_ids=implementation_ids,
        issue=issue,
        generation=generation,
        validate_triage=_validate_frozen_triage,
        validate_implementation=_validate_implementation_body,
        guide_locators=REVIEW_GUIDE_LOCATORS,
    )


def validate_review_fast_path(bundle: dict[str, Any], review_body: dict[str, Any]) -> list[str]:
    return _validate_review_fast_path(
        bundle,
        review_body,
        validate_review=validate_review_graph,
        validate_implementation=_validate_implementation_body,
    )


def create_review_native(
    bundle: dict[str, Any],
    review_body: dict[str, Any],
    *,
    profile: str,
    board: str,
    body_file: Path,
    workspace_path: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    return _create_review_native(
        bundle,
        review_body,
        profile=profile,
        board=board,
        body_file=body_file,
        workspace_path=workspace_path,
        dry_run=dry_run,
        validate_triage=_validate_frozen_triage,
        validate_implementation=_validate_implementation_body,
        validate_review=validate_review_graph,
        guide_locators=REVIEW_GUIDE_LOCATORS,
    )


def prepare_summary_fast_path(
    active_tasks: list[dict[str, Any]],
    *,
    triage_id: str,
    issue: int,
    generation: int,
) -> dict[str, Any]:
    return _prepare_summary_fast_path(
        active_tasks,
        triage_id=triage_id,
        issue=issue,
        generation=generation,
        validate_triage=_validate_frozen_triage,
    )


def create_summary_native(
    bundle: dict[str, Any],
    *,
    profile: str,
    board: str,
    body_file: Path,
    workspace_path: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    return _create_summary_native(
        bundle,
        profile=profile,
        board=board,
        body_file=body_file,
        workspace_path=workspace_path,
        dry_run=dry_run,
        validate_triage=_validate_frozen_triage,
    )


def regenerate_summary_native(
    *,
    profile: str,
    board: str,
    triage_id: str,
    issue: int,
    generation: int,
    workspace_path: Path,
    bundle_file: Path,
    body_file: Path,
) -> dict[str, Any]:
    return _regenerate_summary_native(
        profile=profile,
        board=board,
        triage_id=triage_id,
        issue=issue,
        generation=generation,
        workspace_path=workspace_path,
        bundle_file=bundle_file,
        body_file=body_file,
        validate_triage=_validate_frozen_triage,
    )


def create_impl_batch_native(
    batch: dict[str, Any],
    *,
    profile: str,
    board: str,
    triage_id: str,
    body_dir: Path,
    workspace_path: Path,
) -> dict[str, Any]:
    errors = validate_impl_batch(batch)
    if errors:
        raise ValueError("INVALID_IMPL_BATCH:" + "|".join(errors))
    workspace = workspace_path.resolve()
    if workspace != Path.cwd().resolve():
        raise ValueError("NATIVE_CREATE_WORKSPACE_MISMATCH")
    body_root = body_dir.resolve()
    temp_root = (workspace / ".temp").resolve()
    if temp_root not in body_root.parents:
        raise ValueError("NATIVE_BODY_DIR_OUTSIDE_TEMP")
    for index, wrapper in enumerate(batch["cards"]):
        if wrapper.get("parent_ids") != [triage_id]:
            raise ValueError(f"IMPL_PARENT_TRIAGE_MISMATCH:{index}")
        body_file = body_root / f"impl-{wrapper['ordinal']}.json"
        expected_text = json.dumps(wrapper["body"], ensure_ascii=False, indent=2) + "\n"
        if _read_json(body_file) != wrapper["body"] or body_file.read_text(encoding="utf-8") != expected_text:
            raise ValueError(f"NATIVE_BODY_FILE_MISMATCH:{wrapper['ordinal']}")

    base = ["hermes", "--profile", profile, "kanban", "--board", board]
    created_ids: list[str] = []
    created: list[dict[str, Any]] = []

    def run_values(command: list[str]) -> list[Any]:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=workspace,
        )
        return _parse_json_values(completed.stdout)

    def run_json(command: list[str]) -> Any:
        values = run_values(command)
        if len(values) != 1:
            raise ValueError(f"NATIVE_JSON_VALUE_COUNT:{len(values)}")
        return values[0]

    def list_tasks() -> list[dict[str, Any]]:
        return [
            value
            for value in run_values([*base, "list", "--json"])
            if isinstance(value, dict)
        ]

    def validated_triage(envelope: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        task = envelope.get("task") if isinstance(envelope, dict) else None
        if not isinstance(task, dict) or task.get("id") != triage_id:
            raise ValueError("SOURCE_TRIAGE_ID_MISMATCH")
        if task.get("status") != "triage":
            raise ValueError("TRIAGE_STATUS_CHANGED")
        if task.get("workspace_kind") != "dir" or task.get("workspace_path") != workspace.as_posix():
            raise ValueError("SOURCE_TRIAGE_WORKSPACE_MISMATCH")
        raw_body = task.get("body")
        if not isinstance(raw_body, str):
            raise ValueError("SOURCE_TRIAGE_BODY_INVALID")
        body = json.loads(raw_body)
        triage_errors = _validate_frozen_triage(body)
        if triage_errors:
            raise ValueError(f"SOURCE_TRIAGE_INVALID:{triage_errors[0]}")
        if body.get("issue", {}).get("number") != batch.get("issue"):
            raise ValueError("SOURCE_TRIAGE_ISSUE_MISMATCH")
        digests = set()
        for wrapper in batch["cards"]:
            impl_body = wrapper.get("body")
            if not isinstance(impl_body, dict):
                continue
            if impl_body.get("schema") == "backend-implementation-card-v6":
                trace = impl_body.get("trace")
                digests.add(
                    trace.get("triage_digest") if isinstance(trace, dict) else None
                )
            else:
                digests.add(impl_body.get("triage_digest"))
        if digests != {body.get("frozen_digest")}:
            raise ValueError("SOURCE_TRIAGE_DIGEST_MISMATCH")
        source_slice_ids = [
            item.get("id")
            for item in body.get("implementation_slices", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        batch_slice_ids = [
            _implementation_slice_id(wrapper.get("body"))
            for wrapper in batch["cards"]
            if isinstance(wrapper.get("body"), dict)
        ]
        source_slice_set = set(source_slice_ids)
        batch_slice_set = set(batch_slice_ids)
        missing = sorted(source_slice_set - batch_slice_set)
        unexpected = sorted(batch_slice_set - source_slice_set)
        duplicates = sorted(
            slice_id for slice_id in batch_slice_set if batch_slice_ids.count(slice_id) > 1
        )
        if (
            missing
            or unexpected
            or duplicates
            or len(batch_slice_ids) != len(source_slice_ids)
        ):
            raise ValueError(
                "IMPL_SLICE_CLOSURE_MISMATCH:"
                f"missing={','.join(missing) or '-'}:"
                f"unexpected={','.join(unexpected) or '-'}:"
                f"duplicates={','.join(duplicates) or '-'}"
            )
        source_slices = {
            item["id"]: item
            for item in body.get("implementation_slices", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        for wrapper in batch["cards"]:
            impl_body = wrapper.get("body", {})
            slice_id = _implementation_slice_id(impl_body)
            source_slice = source_slices.get(slice_id, {})
            source_acceptance = source_slice.get("acceptance", [])
            expected_acceptance_ids = [
                item.get("id")
                for item in source_acceptance
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ]
            actual_acceptance_ids = _implementation_acceptance_ids(
                impl_body, expected_acceptance_ids
            )
            acceptance_missing = sorted(set(expected_acceptance_ids) - set(actual_acceptance_ids))
            acceptance_unexpected = sorted(set(actual_acceptance_ids) - set(expected_acceptance_ids))
            acceptance_duplicates = sorted(
                item_id
                for item_id in set(actual_acceptance_ids)
                if actual_acceptance_ids.count(item_id) > 1
            )
            if (
                acceptance_missing
                or acceptance_unexpected
                or acceptance_duplicates
                or len(actual_acceptance_ids) != len(expected_acceptance_ids)
            ):
                raise ValueError(
                    f"IMPL_ACCEPTANCE_CLOSURE_MISMATCH:{slice_id}:"
                    f"missing={','.join(acceptance_missing) or '-'}:"
                    f"unexpected={','.join(acceptance_unexpected) or '-'}:"
                    f"duplicates={','.join(acceptance_duplicates) or '-'}"
                )
            expected_verification_ids = sorted(
                {
                    verification_id
                    for acceptance in source_acceptance
                    if isinstance(acceptance, dict)
                    for verification_id in acceptance.get("verification_ids", [])
                    if isinstance(verification_id, str)
                }
            )
            actual_verification_ids = [
                item.get("id")
                for item in impl_body.get("verification", [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ]
            verification_missing = sorted(
                set(expected_verification_ids) - set(actual_verification_ids)
            )
            verification_unexpected = sorted(
                set(actual_verification_ids) - set(expected_verification_ids)
            )
            verification_duplicates = sorted(
                item_id
                for item_id in set(actual_verification_ids)
                if actual_verification_ids.count(item_id) > 1
            )
            if (
                verification_missing
                or verification_unexpected
                or verification_duplicates
                or len(actual_verification_ids) != len(expected_verification_ids)
            ):
                raise ValueError(
                    f"IMPL_VERIFICATION_CLOSURE_MISMATCH:{slice_id}:"
                    f"missing={','.join(verification_missing) or '-'}:"
                    f"unexpected={','.join(verification_unexpected) or '-'}:"
                    f"duplicates={','.join(verification_duplicates) or '-'}"
                )
        return task, body

    triage_envelope = run_json([*base, "show", triage_id, "--json"])
    triage_task, triage_body = validated_triage(triage_envelope)
    baseline_tasks = list_tasks()
    baseline_ids = {
        task.get("id")
        for task in baseline_tasks
        if isinstance(task.get("id"), str)
    }
    expected_titles = {wrapper["title"] for wrapper in batch["cards"]}
    try:
        for wrapper in batch["cards"]:
            ordinal = wrapper["ordinal"]
            body_file = body_root / f"impl-{ordinal}.json"
            expected_body = body_file.read_text(encoding="utf-8")
            try:
                payload = run_json(
                    [
                        *base,
                        "create",
                        wrapper["title"],
                        "--body-file",
                        str(body_file),
                        "--assignee",
                        "coder",
                        "--priority",
                        str(wrapper["priority"]),
                        "--workspace",
                        f"dir:{workspace.as_posix()}",
                        "--parent",
                        triage_id,
                        "--json",
                    ]
                )
                task_id = _created_task_id(payload, wrapper["title"])
            except (TypeError, ValueError):
                recovered = [
                    task["id"]
                    for task in list_tasks()
                    if isinstance(task.get("id"), str)
                    and task["id"] not in baseline_ids
                    and task.get("title") in expected_titles
                ]
                created_ids.extend(task_id for task_id in recovered if task_id not in created_ids)
                raise
            created_ids.append(task_id)
            envelope = run_json([*base, "show", task_id, "--json"])
            task = envelope.get("task") if isinstance(envelope, dict) else None
            parents = envelope.get("parents") if isinstance(envelope, dict) else None
            checks = {
                "title": isinstance(task, dict) and task.get("title") == wrapper["title"],
                "body": isinstance(task, dict) and task.get("body") == expected_body,
                "assignee": isinstance(task, dict) and task.get("assignee") == "coder",
                "skills": isinstance(task, dict) and task.get("skills") == [],
                "status": isinstance(task, dict) and task.get("status") == "todo",
                "priority": isinstance(task, dict) and task.get("priority") == wrapper["priority"],
                "workspace_kind": isinstance(task, dict) and task.get("workspace_kind") == "dir",
                "workspace_path": isinstance(task, dict)
                and task.get("workspace_path") == workspace.as_posix(),
                "parent": triage_id in _parent_ids(parents),
            }
            if not all(checks.values()):
                raise ValueError(f"NATIVE_READBACK_MISMATCH:{task_id}")
            created.append({"id": task_id, "title": wrapper["title"], "checks": checks})

        final_triage_envelope = run_json([*base, "show", triage_id, "--json"])
        final_triage_task, final_triage_body = validated_triage(final_triage_envelope)
        if final_triage_task.get("body") != triage_task.get("body") or final_triage_body != triage_body:
            raise ValueError("SOURCE_TRIAGE_CHANGED")
    except (OSError, subprocess.CalledProcessError, TypeError, ValueError) as original:
        cleanup_errors: list[str] = []
        for task_id in created_ids:
            try:
                subprocess.run(
                    [*base, "archive", task_id],
                    check=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    cwd=workspace,
                )
            except (OSError, subprocess.CalledProcessError) as cleanup_error:
                cleanup_errors.append(f"ARCHIVE_FAILED:{task_id}:{cleanup_error}")
        try:
            active_ids = {
                task.get("id")
                for task in list_tasks()
                if isinstance(task.get("id"), str)
            }
            for task_id in created_ids:
                if task_id in active_ids:
                    cleanup_errors.append(f"ARCHIVE_READBACK_FAILED:{task_id}")
        except (OSError, subprocess.CalledProcessError, TypeError, ValueError) as cleanup_error:
            cleanup_errors.append(f"ROLLBACK_READBACK_FAILED:{cleanup_error}")
        if cleanup_errors:
            raise ValueError("ROLLBACK_FAILED:" + "|".join(cleanup_errors)) from original
        raise

    return {
        "valid": True,
        "created": created,
        "triage": {"id": triage_id, "status": final_triage_task["status"]},
    }


def _temp_path(path: Path) -> Path:
    if path.is_absolute():
        raise ValueError("output path must be relative to this project's .temp directory")
    temp_root = (Path.cwd() / ".temp").resolve()
    destination = (Path.cwd() / path).resolve()
    if destination == temp_root or temp_root not in destination.parents:
        raise ValueError("output path must be relative to this project's .temp directory")
    return destination


def write_json(path: Path, payload: Any) -> None:
    destination = _temp_path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> Any:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"DUPLICATE_JSON_KEY:{key}")
            value[key] = item
        return value

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys)


impl_batch_template = _core_impl_batch.impl_batch_template_v6
apply_impl_completions = _core_impl_batch.apply_impl_completions
validate_impl_batch = _core_impl_batch.validate_impl_batch
render_impl_batch = _core_impl_batch.render_impl_batch


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    template_parser = subparsers.add_parser("template", help="write a backend implementation card draft")
    template_parser.add_argument("--issue", required=True, type=int)
    template_parser.add_argument("--issue-url", required=True)
    template_parser.add_argument("--output", required=True, type=Path)
    batch_template_parser = subparsers.add_parser(
        "impl-batch-template", help="prefill an Impl-only batch from a Frozen Triage body"
    )
    batch_template_parser.add_argument("--triage", required=True, type=Path)
    batch_template_parser.add_argument("--triage-id")
    batch_template_parser.add_argument("--generation", type=int, default=1)
    batch_template_parser.add_argument("--output", required=True, type=Path)
    prepare_parser = subparsers.add_parser(
        "prepare-impl-fast-path", help="read and materialize the Frozen Triage fast-path inputs once"
    )
    prepare_parser.add_argument("--profile", required=True)
    prepare_parser.add_argument("--board", required=True)
    prepare_parser.add_argument("--triage-id", required=True)
    prepare_parser.add_argument("--issue", required=True, type=int)
    prepare_parser.add_argument("--generation", type=int, default=1)
    prepare_parser.add_argument("--output-dir", required=True, type=Path)
    prepare_parser.add_argument("--batch-output", required=True, type=Path)
    completion_parser = subparsers.add_parser(
        "apply-impl-completions", help="merge compact authored fields into a prefilled Impl batch"
    )
    completion_parser.add_argument("--batch", required=True, type=Path)
    completion_parser.add_argument("--completions", required=True, type=Path)
    completion_parser.add_argument("--output", required=True, type=Path)
    render_batch_parser = subparsers.add_parser(
        "render-impl-batch", help="validate an Impl batch and write all native body files"
    )
    render_batch_parser.add_argument("--batch", required=True, type=Path)
    render_batch_parser.add_argument("--output-dir", required=True, type=Path)
    create_batch_parser = subparsers.add_parser(
        "create-impl-batch-native",
        help="create every validated Impl card and verify native read-back atomically",
    )
    create_batch_parser.add_argument("--batch", required=True, type=Path)
    create_batch_parser.add_argument("--body-dir", required=True, type=Path)
    create_batch_parser.add_argument("--profile", required=True)
    create_batch_parser.add_argument("--board", required=True)
    create_batch_parser.add_argument("--triage-id", required=True)
    create_batch_parser.add_argument("--workspace", required=True, type=Path)
    create_batch_parser.add_argument("--output", required=True, type=Path)
    validate_parser = subparsers.add_parser("validate", help="validate a backend implementation card")
    validate_parser.add_argument("card", type=Path)
    batch_parser = subparsers.add_parser(
        "validate-impl-batch", help="validate Impl-only fast-path wrappers and bodies"
    )
    batch_parser.add_argument("batch", type=Path)
    graph_parser = subparsers.add_parser("validate-graph", help="validate a task graph wrapper")
    graph_parser.add_argument("graph", type=Path)
    graph_parser.add_argument("--phase", choices=("draft", "native"), default="draft")
    graph_parser.add_argument("--source-review-result", type=Path)
    review_parser = subparsers.add_parser(
        "validate-review-graph",
        help="validate an aggregate Review body against frozen source cards without graph topology checks",
    )
    review_parser.add_argument("graph", type=Path)
    prepare_review_parser = subparsers.add_parser(
        "prepare-review-fast-path",
        help="read Review source cards once and prefill immutable authoring facts",
    )
    prepare_review_parser.add_argument("--profile", required=True)
    prepare_review_parser.add_argument("--board", required=True)
    prepare_review_parser.add_argument("--triage-id", required=True)
    prepare_review_parser.add_argument("--implementation-id", action="append", required=True, dest="implementation_ids")
    prepare_review_parser.add_argument("--issue", required=True, type=int)
    prepare_review_parser.add_argument("--generation", type=int, default=1)
    prepare_review_parser.add_argument("--bundle-output", required=True, type=Path)
    prepare_review_parser.add_argument("--body-output", required=True, type=Path)
    create_review_parser = subparsers.add_parser(
        "create-review-native",
        help="validate, create, and read back one Review card from a frozen source bundle",
    )
    create_review_parser.add_argument("--bundle", required=True, type=Path)
    create_review_parser.add_argument("--body", required=True, type=Path)
    create_review_parser.add_argument("--profile", required=True)
    create_review_parser.add_argument("--board", required=True)
    create_review_parser.add_argument("--workspace", required=True, type=Path)
    create_review_parser.add_argument("--output", required=True, type=Path)
    create_review_parser.add_argument("--dry-run", action="store_true")
    prepare_summary_parser = subparsers.add_parser(
        "prepare-summary-fast-path",
        help="derive a deterministic Summary body and complete parent closure from frozen Triage",
    )
    prepare_summary_parser.add_argument("--profile", required=True)
    prepare_summary_parser.add_argument("--board", required=True)
    prepare_summary_parser.add_argument("--triage-id", required=True)
    prepare_summary_parser.add_argument("--issue", required=True, type=int)
    prepare_summary_parser.add_argument("--generation", type=int, default=1)
    prepare_summary_parser.add_argument("--bundle-output", required=True, type=Path)
    prepare_summary_parser.add_argument("--body-output", required=True, type=Path)
    create_summary_parser = subparsers.add_parser(
        "create-summary-native",
        help="revalidate, create, and read back one deterministic Summary card",
    )
    create_summary_parser.add_argument("--bundle", required=True, type=Path)
    create_summary_parser.add_argument("--body", required=True, type=Path)
    create_summary_parser.add_argument("--profile", required=True)
    create_summary_parser.add_argument("--board", required=True)
    create_summary_parser.add_argument("--workspace", required=True, type=Path)
    create_summary_parser.add_argument("--output", required=True, type=Path)
    create_summary_parser.add_argument("--dry-run", action="store_true")
    regenerate_summary_parser = subparsers.add_parser(
        "regenerate-summary-native",
        help="archive, recreate, and measure one deterministic Summary in a single operation",
    )
    regenerate_summary_parser.add_argument("--profile", required=True)
    regenerate_summary_parser.add_argument("--board", required=True)
    regenerate_summary_parser.add_argument("--triage-id", required=True)
    regenerate_summary_parser.add_argument("--issue", required=True, type=int)
    regenerate_summary_parser.add_argument("--generation", type=int, default=1)
    regenerate_summary_parser.add_argument("--workspace", required=True, type=Path)
    regenerate_summary_parser.add_argument("--bundle-output", required=True, type=Path)
    regenerate_summary_parser.add_argument("--body-output", required=True, type=Path)
    regenerate_summary_parser.add_argument("--output", required=True, type=Path)
    native_parser = subparsers.add_parser("validate-native-readback", help="compare graph wrapper with full Kanban show read-back")
    native_parser.add_argument("graph", type=Path)
    native_parser.add_argument("readback", type=Path)
    native_parser.add_argument("--phase", choices=("draft", "native"), default="native")
    native_parser.add_argument("--source-review-result", type=Path)
    render_parser = subparsers.add_parser("render-body", help="write one card body as readable native JSON")
    render_parser.add_argument("graph", type=Path)
    render_parser.add_argument("--card-key", required=True)
    render_parser.add_argument("--output", required=True, type=Path)
    diff_parser = subparsers.add_parser("requirement-diff", help="write a read-only requirement comparison")
    diff_parser.add_argument("--base", required=True)
    diff_parser.add_argument("--revised", required=True)
    diff_parser.add_argument("--path", action="append", required=True, dest="paths")
    diff_parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    if args.command == "template":
        write_json(args.output, template(args.issue, args.issue_url))
        return 0
    if args.command == "impl-batch-template":
        try:
            write_json(
                args.output,
                impl_batch_template(
                    _read_json(args.triage),
                    generation=args.generation,
                    triage_id=args.triage_id,
                ),
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            print(json.dumps({"valid": False, "errors": [f"IMPL_BATCH_TEMPLATE_FAILED:{exc}"]}, ensure_ascii=False))
            return 2
        return 0
    if args.command == "prepare-impl-fast-path":
        try:
            base = ["hermes", "--profile", args.profile, "kanban", "--board", args.board]
            show = subprocess.run(
                [*base, "show", args.triage_id, "--json"],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            listing = subprocess.run(
                [*base, "list", "--json"],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            show_values = _parse_json_values(show.stdout)
            if len(show_values) != 1 or not isinstance(show_values[0], dict):
                raise ValueError(f"SOURCE_TRIAGE_READBACK_COUNT:{len(show_values)}")
            active_tasks = [item for item in _parse_json_values(listing.stdout) if isinstance(item, dict)]
            prepared = prepare_impl_fast_path(
                show_values[0],
                active_tasks,
                triage_id=args.triage_id,
                issue=args.issue,
                generation=args.generation,
            )
            write_json(args.output_dir / "source-triage.json", show_values[0])
            write_json(args.output_dir / "active-board.json", active_tasks)
            write_json(args.output_dir / "triage-body.json", prepared["triage_body"])
            write_json(args.batch_output, prepared["batch"])
            print(json.dumps(prepared["summary"], ensure_ascii=False))
        except (
            OSError,
            json.JSONDecodeError,
            subprocess.CalledProcessError,
            TypeError,
            ValueError,
        ) as exc:
            print(
                json.dumps(
                    {"valid": False, "errors": [f"PREPARE_IMPL_FAST_PATH_FAILED:{exc}"]},
                    ensure_ascii=False,
                )
            )
            return 2
        return 0
    if args.command == "prepare-review-fast-path":
        try:
            base = ["hermes", "--profile", args.profile, "kanban", "--board", args.board]
            listing = subprocess.run(
                [*base, "list", "--json"], check=True, capture_output=True, text=True, encoding="utf-8"
            )
            active_tasks = [item for item in _parse_json_values(listing.stdout) if isinstance(item, dict)]
            bundle = prepare_review_fast_path(
                active_tasks,
                triage_id=args.triage_id,
                implementation_ids=args.implementation_ids,
                issue=args.issue,
                generation=args.generation,
            )
            write_json(args.bundle_output, bundle)
            write_json(args.body_output, bundle["review_body_template"])
            print(json.dumps({
                "valid": True,
                "title": bundle["title"],
                "source_count": 1 + len(bundle["implementation_cards"]),
                "native_calls": 1,
                "bundle": str(args.bundle_output),
                "body": str(args.body_output),
            }, ensure_ascii=False))
        except (OSError, json.JSONDecodeError, subprocess.CalledProcessError, TypeError, ValueError) as exc:
            print(json.dumps({"valid": False, "errors": [f"PREPARE_REVIEW_FAST_PATH_FAILED:{exc}"]}, ensure_ascii=False))
            return 2
        return 0
    if args.command == "create-review-native":
        try:
            report = create_review_native(
                _read_json(args.bundle),
                _read_json(args.body),
                profile=args.profile,
                board=args.board,
                body_file=args.body,
                workspace_path=args.workspace,
                dry_run=args.dry_run,
            )
            write_json(args.output, report)
            print(json.dumps(report, ensure_ascii=False))
        except (OSError, json.JSONDecodeError, subprocess.CalledProcessError, TypeError, ValueError) as exc:
            print(json.dumps({"valid": False, "errors": [f"CREATE_REVIEW_NATIVE_FAILED:{exc}"]}, ensure_ascii=False))
            return 2
        return 0
    if args.command == "prepare-summary-fast-path":
        try:
            base = ["hermes", "--profile", args.profile, "kanban", "--board", args.board]
            listing = subprocess.run(
                [*base, "list", "--json"], check=True, capture_output=True, text=True, encoding="utf-8"
            )
            active_tasks = [item for item in _parse_json_values(listing.stdout) if isinstance(item, dict)]
            bundle = prepare_summary_fast_path(
                active_tasks,
                triage_id=args.triage_id,
                issue=args.issue,
                generation=args.generation,
            )
            write_json(args.bundle_output, bundle)
            write_json(args.body_output, bundle["body"])
            print(json.dumps({
                "valid": True,
                "title": bundle["title"],
                "parent_ids": bundle["parent_ids"],
                "native_calls": 1,
                "bundle": str(args.bundle_output),
                "body": str(args.body_output),
            }, ensure_ascii=False))
        except (OSError, json.JSONDecodeError, subprocess.CalledProcessError, TypeError, ValueError) as exc:
            print(json.dumps({"valid": False, "errors": [f"PREPARE_SUMMARY_FAST_PATH_FAILED:{exc}"]}, ensure_ascii=False))
            return 2
        return 0
    if args.command == "create-summary-native":
        try:
            report = create_summary_native(
                _read_json(args.bundle),
                profile=args.profile,
                board=args.board,
                body_file=args.body,
                workspace_path=args.workspace,
                dry_run=args.dry_run,
            )
            write_json(args.output, report)
            print(json.dumps(report, ensure_ascii=False))
        except (OSError, json.JSONDecodeError, subprocess.CalledProcessError, TypeError, ValueError) as exc:
            print(json.dumps({"valid": False, "errors": [f"CREATE_SUMMARY_NATIVE_FAILED:{exc}"]}, ensure_ascii=False))
            return 2
        return 0
    if args.command == "regenerate-summary-native":
        try:
            report = regenerate_summary_native(
                profile=args.profile,
                board=args.board,
                triage_id=args.triage_id,
                issue=args.issue,
                generation=args.generation,
                workspace_path=args.workspace,
                bundle_file=args.bundle_output,
                body_file=args.body_output,
            )
            write_json(args.output, report)
            print(json.dumps(report, ensure_ascii=False))
        except (OSError, json.JSONDecodeError, subprocess.CalledProcessError, TypeError, ValueError) as exc:
            print(
                json.dumps(
                    {"valid": False, "errors": [f"REGENERATE_SUMMARY_NATIVE_FAILED:{exc}"]},
                    ensure_ascii=False,
                )
            )
            return 2
        return 0
    if args.command == "apply-impl-completions":
        try:
            write_json(
                args.output,
                apply_impl_completions(
                    _read_json(args.batch),
                    _read_json(args.completions),
                ),
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            print(
                json.dumps(
                    {"valid": False, "errors": [f"APPLY_IMPL_COMPLETIONS_FAILED:{exc}"]},
                    ensure_ascii=False,
                )
            )
            return 2
        return 0
    if args.command == "render-impl-batch":
        try:
            rendered = render_impl_batch(_read_json(args.batch))
            for filename, body in rendered.items():
                write_json(args.output_dir / filename, body)
            print(
                json.dumps(
                    {"valid": True, "count": len(rendered), "files": list(rendered)},
                    ensure_ascii=False,
                )
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            print(
                json.dumps(
                    {"valid": False, "errors": [f"RENDER_IMPL_BATCH_FAILED:{exc}"]},
                    ensure_ascii=False,
                )
            )
            return 2
        return 0
    if args.command == "create-impl-batch-native":
        try:
            report = create_impl_batch_native(
                _read_json(args.batch),
                profile=args.profile,
                board=args.board,
                triage_id=args.triage_id,
                body_dir=args.body_dir,
                workspace_path=args.workspace,
            )
            write_json(args.output, report)
            print(
                json.dumps(
                    {
                        "valid": True,
                        "created": [
                            {"id": item["id"], "title": item["title"]}
                            for item in report["created"]
                        ],
                        "triage_status": report["triage"]["status"],
                    },
                    ensure_ascii=False,
                )
            )
        except (
            OSError,
            json.JSONDecodeError,
            subprocess.CalledProcessError,
            TypeError,
            ValueError,
        ) as exc:
            print(
                json.dumps(
                    {"valid": False, "errors": [f"CREATE_IMPL_BATCH_NATIVE_FAILED:{exc}"]},
                    ensure_ascii=False,
                )
            )
            return 2
        return 0
    if args.command == "requirement-diff":
        try:
            write_json(args.output, requirement_diff(Path.cwd(), args.base, args.revised, args.paths))
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            print(json.dumps({"valid": False, "errors": [f"REQUIREMENT_DIFF_FAILED:{exc}"]}, ensure_ascii=False))
            return 2
        return 0
    if args.command == "render-body":
        try:
            graph = _read_json(args.graph)
            cards = graph.get("cards") if isinstance(graph, dict) else None
            matches = [
                card
                for card in cards or []
                if isinstance(card, dict) and card.get("key") == args.card_key
            ]
            if len(matches) != 1:
                raise ValueError(f"CARD_KEY_MATCH_COUNT:{args.card_key}:{len(matches)}")
            destination = _temp_path(args.output)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(serialize_task_body(matches[0].get("body")).encode("utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            print(json.dumps({"valid": False, "errors": [f"RENDER_BODY_FAILED:{exc}"]}, ensure_ascii=False))
            return 2
        return 0
    if args.command in {"validate-graph", "validate-review-graph", "validate-native-readback"}:
        source = args.graph
    elif args.command == "validate-impl-batch":
        source = args.batch
    else:
        source = args.card
    try:
        payload = _read_json(source)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(json.dumps({"valid": False, "errors": [f"READ_INPUT_FAILED:{exc}"]}, ensure_ascii=False))
        return 2
    source_review_result = None
    if args.command in {"validate-graph", "validate-native-readback"} and args.source_review_result is not None:
        try:
            source_review_result = _read_json(args.source_review_result)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            print(json.dumps({"valid": False, "errors": [f"READ_INPUT_FAILED:{exc}"]}, ensure_ascii=False))
            return 2
    if args.command == "validate-native-readback":
        try:
            readback = _read_json(args.readback)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            print(json.dumps({"valid": False, "errors": [f"READ_INPUT_FAILED:{exc}"]}, ensure_ascii=False))
            return 2
        errors = validate_graph(
            payload,
            phase=args.phase,
            validate_implementation=_validate_implementation_body,
            source_review_result=source_review_result,
        )
        errors.extend(validate_native_readback(payload, readback))
    elif args.command == "validate-graph":
        errors = validate_graph(
            payload,
            phase=args.phase,
            validate_implementation=_validate_implementation_body,
            source_review_result=source_review_result,
        )
    elif args.command == "validate-review-graph":
        errors = validate_review_graph(
            payload,
            validate_implementation=_validate_implementation_body,
        )
    elif args.command == "validate-impl-batch":
        errors = validate_impl_batch(payload)
    else:
        errors = validate(payload)
    print(json.dumps({"valid": not errors, "errors": errors}, ensure_ascii=False))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
