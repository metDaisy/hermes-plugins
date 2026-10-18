"""Implementation batch templates, completion merging, and validation."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from .graph_contract import TITLE
from .implementation_card_v5 import (
    CONTRACT_DIMENSIONS,
    _error,
    _implementation_schema,
    _implementation_text,
    _unexpected_fields,
    _validate_frozen_triage,
    _validate_implementation_body,
    template,
)

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
    slice_ordinal_by_id = {
        source_slice.get("id"): ordinal
        for ordinal, source_slice in enumerate(slices, start=1)
        if isinstance(source_slice, dict) and isinstance(source_slice.get("id"), str)
    }
    cards: list[dict[str, Any]] = []
    dependency_edges: list[dict[str, int]] = []
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
        }
        depends_on_ordinals: list[int] = []
        for dependency_id in source_slice.get("depends_on", []):
            dependency_ordinal = slice_ordinal_by_id.get(dependency_id)
            if not isinstance(dependency_ordinal, int) or dependency_ordinal >= index:
                raise ValueError(f"INVALID_TRIAGE_SLICE_DEPENDENCY:{index}:{dependency_id}")
            depends_on_ordinals.append(dependency_ordinal)
            dependency_edges.append({"from_ordinal": dependency_ordinal, "to_ordinal": index})
        wrapper["depends_on_ordinals"] = depends_on_ordinals
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
        "dependency_audit": {"edges": dependency_edges},
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
            matching_index = next((
                index
                for index, entry_point in enumerate(remaining_authored)
                if entry_point.get("path") == expected.get("path")
                and entry_point.get("symbol") == expected_symbol
            ), None)
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
    return merged


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
        body_schema = _implementation_schema(body)
        for error in _validate_implementation_body(body):
            errors.append(f"IMPL_BODY_INVALID:{index}:{error}")
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
        contract_changes = wrapper.get("contract_changes")
        if not isinstance(contract_changes, list) or not all(
            isinstance(change, dict) for change in contract_changes
        ):
            errors.append(f"INVALID_CONTRACT_CHANGES:{index}")
        elif body_schema == "backend-implementation-card-v6":
            body_text = _implementation_text(body)
            for change in contract_changes:
                change_id = change.get("id")
                change_text = change.get("change")
                if not isinstance(change_id, str) or not isinstance(change_text, str):
                    errors.append(f"INVALID_CONTRACT_CHANGE:{index}")
                elif change_id not in body_text or change_text not in body_text:
                    errors.append(f"CONTRACT_CHANGE_MISSING:{index}:{change_id}")
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
        elif body_schema == "backend-implementation-card-v6":
            body_text = _implementation_text(body)
            for symbol in required_contract_symbols:
                if symbol not in body_text:
                    errors.append(f"REQUIRED_CONTRACT_SYMBOL_MISSING:{index}:{symbol}")
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
        if (
            match is not None
            and isinstance(body, dict)
            and body_schema != "backend-implementation-card-v6"
        ):
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
        expected_edges = [
            {"from_ordinal": dependency, "to_ordinal": wrapper.get("ordinal")}
            for wrapper in cards
            if isinstance(wrapper, dict)
            for dependency in wrapper.get("depends_on_ordinals", [])
            if isinstance(dependency, int)
        ]
        if dependency_audit.get("edges") != expected_edges:
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
