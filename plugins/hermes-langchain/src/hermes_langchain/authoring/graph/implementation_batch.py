"""Implementation batch templates, completion merging, and validation."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from .graph_contract import TITLE
from .legacy_implementation_card import (
    CONTRACT_DIMENSIONS,
    _error,
    _implementation_schema,
    _implementation_text,
    _unexpected_fields,
    _validate_frozen_triage,
    _validate_implementation_body,
    template,
)
from . import implementation_card
from .implementation_card import SCHEMA as IMPLEMENTATION_CARD_V6

UNRESOLVED_ENTRY_POINT_SYMBOLS = {"", "?", "unknown", "unresolved", "todo", "tbd"}
ENTRY_POINT_OPERATIONS = {
    "create",
    "modify",
    "move-source",
    "move-target",
    "delete",
    "preserve",
    "verify",
}
EXACT_BEHAVIOR_SYMBOL = re.compile(
    r"\b[A-Z][A-Za-z0-9_$]*#[A-Za-z_$][A-Za-z0-9_$]*"
)
VERIFICATION_SELECTOR_MARKERS = {
    "lock": (("pessimistic", "잠금", " lock", "락"), ("lock", "pessimistic")),
    "rollback": (("rollback", "롤백"), ("rollback", "preserve")),
    "expired": (("expired", "만료"), ("expired", "expiration")),
    "idempotent": (
        ("idempot", "반복 실행", "두 번째"),
        ("idempot", "repeat", "second"),
    ),
    "modulith": (
        ("modulith", "모듈 구조", "구조 검증", "module boundary"),
        ("modulith", "modularity", "module", "structure"),
    ),
    "conflict": (("conflict", "충돌"), ("conflict", "preserve")),
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
        return type_name
    return f"{type_name}#{heading}"


def _main_java_artifact(locator: Any, operation: str) -> dict[str, Any] | None:
    if not isinstance(locator, str):
        return None
    normalized = locator.replace("\\", "/")
    match = re.search(
        r"(src/main/java/[A-Za-z0-9_$/.-]+\.java)#([A-Za-z_$][A-Za-z0-9_$]*)(?:\([^)]*\))?",
        normalized,
    )
    if match is None:
        return None
    path, member = match.groups()
    return {
        "path": path,
        "symbol": f"{Path(path).stem}#{member}",
        "operation": operation,
        "acceptance_ids": [],
    }


def _operation_symbols(body: Any) -> set[str]:
    if not isinstance(body, dict):
        return set()
    return {
        operation.get("symbol")
        for obligation in body.get("obligations", [])
        if isinstance(obligation, dict)
        for operation in [obligation.get("operation")]
        if isinstance(operation, dict)
        and isinstance(operation.get("symbol"), str)
        and operation["symbol"]
    }


def _v6_semantic_artifacts(body: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    boundary = body.get("boundary")
    in_scope = boundary.get("in_scope") if isinstance(boundary, dict) else None
    if isinstance(in_scope, list):
        for item in in_scope:
            if not isinstance(item, str):
                continue
            operation_match = re.match(
                r"^\s*(create|modify|move-source|move-target|delete|preserve|verify)\s*:?[ \t]+",
                item,
                flags=re.IGNORECASE,
            )
            if operation_match is None:
                continue
            artifact = _main_java_artifact(item, operation_match.group(1).lower())
            if artifact is not None:
                artifacts.append(artifact)

    verification_only = isinstance(in_scope, list) and any(
        isinstance(item, str) and "production change 없음" in item.casefold()
        for item in in_scope
    )
    if verification_only:
        for constraint in body.get("constraints", []):
            if not isinstance(constraint, dict):
                continue
            source = constraint.get("source")
            locator = source.get("locator") if isinstance(source, dict) else None
            artifact = _main_java_artifact(locator, "verify")
            if artifact is not None:
                artifacts.append(artifact)

    operation_symbols = _operation_symbols(body)
    for artifact in artifacts:
        path = str(artifact.get("path") or "").replace("\\", "/")
        symbol = artifact.get("symbol")
        aliases: list[str] = []
        if (
            path.startswith("src/main/java/")
            and path.endswith(".java")
            and isinstance(symbol, str)
            and "#" in symbol
        ):
            owner, method = symbol.split("#", 1)
            fqcn = path[len("src/main/java/") : -len(".java")].replace("/", ".")
            exact_alias = f"{fqcn}#{method}"
            if fqcn.rsplit(".", 1)[-1] == owner and exact_alias in operation_symbols:
                aliases.append(exact_alias)
        if aliases:
            artifact["aliases"] = aliases

    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for artifact in artifacts:
        key = (artifact["path"], artifact["symbol"], artifact["operation"])
        unique[key] = artifact
    return list(unique.values())


def _artifact_identities(artifact: dict[str, Any]) -> set[str]:
    path = artifact.get("path")
    symbol = artifact.get("symbol")
    identities = {
        value for value in (path, symbol) if isinstance(value, str) and value
    }
    if isinstance(path, str) and isinstance(symbol, str) and "#" in symbol:
        identities.add(f"{path}#{symbol.split('#', 1)[1]}")
    aliases = artifact.get("aliases")
    if isinstance(aliases, list):
        identities.update(alias for alias in aliases if isinstance(alias, str) and alias)
    return identities


def _verification_selectors(body: Any) -> set[str]:
    if not isinstance(body, dict):
        return set()
    selector_field = (
        "selectors"
        if _implementation_schema(body) == IMPLEMENTATION_CARD_V6
        else "tests"
    )
    return {
        selector
        for verification in body.get("verification", [])
        if isinstance(verification, dict)
        for selector in verification.get(selector_field, [])
        if isinstance(selector, str) and selector
    }


def _v6_semantic_references(body: dict[str, Any]) -> list[dict[str, Any]]:
    references = list(_v6_semantic_artifacts(body))
    for constraint in body.get("constraints", []):
        if not isinstance(constraint, dict):
            continue
        source = constraint.get("source")
        locator = source.get("locator") if isinstance(source, dict) else None
        artifact = _main_java_artifact(locator, "reference")
        if artifact is not None:
            references.append(artifact)
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for artifact in references:
        unique[(artifact["path"], artifact["symbol"])] = artifact
    return list(unique.values())


def _dependency_consumer_entry_points(body: Any) -> set[str]:
    if not isinstance(body, dict):
        return set()
    if _implementation_schema(body) == IMPLEMENTATION_CARD_V6:
        selectors = _verification_selectors(body)
        operation_symbols = _operation_symbols(body)
        return selectors | operation_symbols

    context = body.get("context")
    entry_points = context.get("entry_points", []) if isinstance(context, dict) else []
    candidates = {
        str(entry.get("symbol"))
        for entry in entry_points
        if isinstance(entry, dict) and entry.get("symbol")
    }
    for entry in entry_points:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        symbol = entry.get("symbol")
        if isinstance(path, str) and path and isinstance(symbol, str) and symbol:
            normalized_path = path.replace("\\", "/")
            candidates.add(f"{normalized_path}#{symbol.split('#', 1)[-1]}")
    candidates.update(_verification_selectors(body))
    return candidates


def _selector_matches_level(selector: str, test_level: str) -> bool:
    owner = selector.partition("#")[0].lower()
    if test_level == "integration":
        return "integrationtest" in owner
    if test_level == "repository":
        return ".repository." in owner or "repositorytest" in owner
    if test_level == "slice":
        return "controllertest" in owner or "webmvctest" in owner
    if test_level == "modulith":
        return "modularitytest" in owner or "architecturetest" in owner
    if test_level == "unit":
        return not any(
            marker in owner
            for marker in (
                "integrationtest",
                "repositorytest",
                ".repository.",
                "controllertest",
                "webmvctest",
                "modularitytest",
                "architecturetest",
            )
        )
    return False


def _required_selector_markers(scenarios: list[Any]) -> list[str]:
    text = " ".join(str(item).lower() for item in scenarios)
    return [
        marker
        for marker, (scenario_markers, _) in VERIFICATION_SELECTOR_MARKERS.items()
        if any(candidate in text for candidate in scenario_markers)
    ]


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
        required_entry_points: list[dict[str, str]] = []
        required_gap_symbols: list[str] = []
        for gap_id in body["slice"]["gap_ids"]:
            gap = gap_by_id.get(gap_id)
            if not isinstance(gap, dict):
                continue
            required_gap_symbols.extend(
                EXACT_BEHAVIOR_SYMBOL.findall(str(gap.get("required_state") or ""))
            )
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
                if path.startswith("src/"):
                    required_entry_point = {
                        "path": path,
                        "symbol": _entry_point_symbol(path, heading),
                    }
                    if required_entry_point not in required_entry_points:
                        required_entry_points.append(required_entry_point)
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
            "required_verification_scenarios": {
                verification_id: list(
                    verification_by_id.get(verification_id, {}).get(
                        "required_verification", []
                    )
                )
                for verification_id in verification_ids
            },
            "required_entry_points": required_entry_points,
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
                [
                    symbol
                    for change in relevant_contract_changes
                    for symbol in re.findall(
                        r"\b[a-z][A-Za-z0-9_$]*\([^)]*\)",
                        str(change.get("change", "")),
                    )
                ]
                + required_gap_symbols
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


def impl_batch_template_v6(
    triage: dict[str, Any], generation: int = 1, triage_id: str | None = None
) -> dict[str, Any]:
    """Build current-writer v6 wrappers from a frozen Triage."""
    triage_errors = _validate_frozen_triage(triage)
    if triage_errors:
        raise ValueError(f"SOURCE_TRIAGE_INVALID:{triage_errors[0]}")
    issue = triage.get("issue")
    slices = triage.get("implementation_slices")
    if not isinstance(issue, dict) or not isinstance(slices, list) or not slices:
        raise ValueError("IMPLEMENTATION_SLICES_REQUIRED")
    bodies = implementation_card.skeletons_from_frozen_triage(triage)
    if len(bodies) != len(slices):
        raise ValueError("IMPLEMENTATION_V6_SKELETON_COUNT_MISMATCH")
    gaps = {
        item.get("id"): item
        for item in triage.get("confirmed_gaps", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    contract_changes = {
        item.get("id"): item
        for item in triage.get("contract_changes", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    cards: list[dict[str, Any]] = []
    for index, (source_slice, body) in enumerate(zip(slices, bodies), start=1):
        if not isinstance(source_slice, dict):
            raise ValueError(f"INVALID_TRIAGE_SLICE:{index}")
        declared_contract_ids = source_slice.get("contract_change_ids", [])
        if not isinstance(declared_contract_ids, list):
            raise ValueError(f"INVALID_TRIAGE_SLICE_CONTRACT_CHANGES:{index}")
        relevant_contract_changes = [
            contract_changes[contract_id]
            for contract_id in declared_contract_ids
            if contract_id in contract_changes
        ]
        if len(relevant_contract_changes) != len(declared_contract_ids):
            raise ValueError(f"UNKNOWN_TRIAGE_SLICE_CONTRACT_CHANGE:{index}")
        for change in relevant_contract_changes:
            evidence = change.get("evidence")
            source = evidence[0] if isinstance(evidence, list) and evidence else None
            if not isinstance(source, dict):
                raise ValueError(f"CONTRACT_CHANGE_EVIDENCE_REQUIRED:{index}:{change.get('id')}")
            body["constraints"].append(
                {
                    "id": change["id"],
                    "rule": f"{change['id']}: {change.get('change', '')}",
                    "source": {
                        "locator": source.get("locator", ""),
                        "claim": source.get("claim", ""),
                    },
                }
            )
            verification_ids = {
                item
                for item in change.get("verification_ids", [])
                if isinstance(item, str)
            }
            for verification in body.get("verification", []):
                if verification.get("id") in verification_ids:
                    verification["covers"] = list(
                        dict.fromkeys([*verification.get("covers", []), change["id"]])
                    )
        required_symbols: list[str] = []
        for gap_id in source_slice.get("gap_ids", []):
            gap = gaps.get(gap_id, {})
            text = "\n".join(
                [str(gap.get("required_state", ""))]
                + [
                    str(item.get("claim", ""))
                    for field in ("evidence", "rules")
                    for item in gap.get(field, [])
                    if isinstance(item, dict)
                ]
            )
            required_symbols.extend(EXACT_BEHAVIOR_SYMBOL.findall(text))
        wrapper = {
            "ordinal": index,
            "title": f"G{generation}-Issue{issue.get('number')}-Impl{index}",
            "priority": len(slices) - index + 1,
            "body": body,
            "required_verification_ids": [
                item.get("id")
                for item in body.get("verification", [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ],
            "contract_changes": copy.deepcopy(relevant_contract_changes),
            "required_contract_symbols": list(dict.fromkeys(required_symbols)),
            "depends_on_ordinals": [],
        }
        if source_slice.get("depends_on", []):
            raise ValueError(f"TRIAGE_SLICE_DEPENDENCIES_NOT_DEFERRED:{index}")
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
    batch = impl_batch_template_v6(triage, generation=generation, triage_id=triage_id)
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
        body = card["body"]
        if _implementation_schema(body) == IMPLEMENTATION_CARD_V6:
            allowed_fields = {
                "ordinal",
                "obligations",
                "verification",
                "depends_on_ordinals",
            }
            unexpected = sorted(set(authored) - allowed_fields)
            if unexpected:
                raise ValueError(
                    f"UNEXPECTED_IMPL_COMPLETION_FIELD:{ordinal}:{unexpected[0]}"
                )
            authored_obligations = authored.get("obligations")
            expected_obligations = body.get("obligations")
            if not isinstance(authored_obligations, list) or not isinstance(
                expected_obligations, list
            ):
                raise ValueError(f"INVALID_IMPL_COMPLETION_OBLIGATIONS:{ordinal}")
            expected_obligation_ids = [
                item.get("id") for item in expected_obligations if isinstance(item, dict)
            ]
            authored_obligation_ids = [
                item.get("id") for item in authored_obligations if isinstance(item, dict)
            ]
            if authored_obligation_ids != expected_obligation_ids:
                raise ValueError(f"IMPL_COMPLETION_OBLIGATION_IDS_MISMATCH:{ordinal}")
            for expected, completed in zip(expected_obligations, authored_obligations):
                obligation_id = expected.get("id")
                if completed.get("kind") != expected.get("kind"):
                    raise ValueError(
                        f"IMPL_COMPLETION_OBLIGATION_KIND_MISMATCH:{ordinal}:{obligation_id}"
                    )
                expected_operation = expected.get("operation")
                completed_operation = completed.get("operation")
                expected_symbol = (
                    expected_operation.get("symbol")
                    if isinstance(expected_operation, dict)
                    else None
                )
                completed_symbol = (
                    completed_operation.get("symbol")
                    if isinstance(completed_operation, dict)
                    else None
                )
                if expected_symbol and completed_symbol != expected_symbol:
                    raise ValueError(
                        f"IMPL_COMPLETION_OPERATION_SYMBOL_MISMATCH:{ordinal}:{obligation_id}"
                    )
                expected_anchors = expected.get("expected")
                completed_expected = completed.get("expected")
                if not isinstance(completed_expected, list) or any(
                    anchor not in completed_expected
                    for anchor in (
                        expected_anchors if isinstance(expected_anchors, list) else []
                    )
                ):
                    raise ValueError(
                        f"IMPL_COMPLETION_EXPECTED_ANCHOR_MISSING:{ordinal}:{obligation_id}"
                    )
            authored_verification = authored.get("verification")
            expected_verification = body.get("verification")
            if not isinstance(authored_verification, list) or not isinstance(
                expected_verification, list
            ):
                raise ValueError(f"INVALID_IMPL_COMPLETION_VERIFICATION:{ordinal}")
            expected_verification_ids = [
                item.get("id") for item in expected_verification if isinstance(item, dict)
            ]
            authored_verification_ids = [
                item.get("id") for item in authored_verification if isinstance(item, dict)
            ]
            if authored_verification_ids != expected_verification_ids:
                raise ValueError(f"IMPL_COMPLETION_VERIFICATION_IDS_MISMATCH:{ordinal}")
            for expected, completed in zip(expected_verification, authored_verification):
                if completed.get("covers") != expected.get("covers"):
                    raise ValueError(
                        "IMPL_COMPLETION_VERIFICATION_COVERS_MISMATCH:"
                        f"{ordinal}:{expected.get('id')}"
                    )
            body["obligations"] = copy.deepcopy(authored_obligations)
            body["verification"] = copy.deepcopy(authored_verification)
            authored_dependencies = authored.get("depends_on_ordinals", [])
            if not isinstance(authored_dependencies, list):
                raise ValueError(f"INVALID_IMPL_COMPLETION_DEPENDENCIES:{ordinal}")
            card["depends_on_ordinals"] = copy.deepcopy(authored_dependencies)
            continue
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
            verification["test_level"] = authored_item.get("test_level")
            verification["tests"] = authored_item.get("tests")
            authored_scenarios = authored_item.get("scenarios")
            if not isinstance(authored_scenarios, list):
                verification["scenarios"] = authored_scenarios
            else:
                verification["scenarios"] = list(
                    dict.fromkeys(
                        list(verification.get("scenarios", [])) + authored_scenarios
                    )
                )
    if "dependency_audit" in completions:
        merged["dependency_audit"] = copy.deepcopy(completions["dependency_audit"])
    errors = validate_impl_batch(merged)
    if errors:
        raise ValueError("INVALID_IMPL_COMPLETIONS:" + "|".join(errors))
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
    semantic_artifacts: list[list[dict[str, Any]]] = []
    for index, wrapper in enumerate(cards):
        if not isinstance(wrapper, dict):
            errors.append(f"IMPL_BATCH_CARD_NOT_OBJECT:{index}")
            semantic_artifacts.append([])
            continue
        _unexpected_fields(
            wrapper,
            {
                "ordinal",
                "title",
                "priority",
                "body",
                "contract_changes",
                "required_entry_points",
                "required_contract_symbols",
                "required_verification_ids",
                "required_verification_scenarios",
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
        entry_point_keys = {
            (
                str(entry.get("path") or "").replace("\\", "/"),
                str(entry.get("symbol") or ""),
            )
            for entry in (
                body.get("context", {}).get("entry_points", [])
                if isinstance(body, dict) and isinstance(body.get("context"), dict)
                else []
            )
            if isinstance(entry, dict)
        }
        required_entry_points = wrapper.get("required_entry_points")
        if body_schema == IMPLEMENTATION_CARD_V6:
            pass
        elif not isinstance(required_entry_points, list) or not all(
            isinstance(entry, dict)
            and set(entry) == {"path", "symbol"}
            and isinstance(entry.get("path"), str)
            and entry["path"]
            and isinstance(entry.get("symbol"), str)
            and entry["symbol"]
            for entry in required_entry_points or []
        ):
            errors.append(f"INVALID_REQUIRED_ENTRY_POINTS:{index}")
        else:
            for required_entry_point in required_entry_points:
                required_key = (
                    required_entry_point["path"].replace("\\", "/"),
                    required_entry_point["symbol"],
                )
                if required_key not in entry_point_keys:
                    errors.append(
                        f"IMPL_REQUIRED_ENTRY_POINT_MISSING:{index}:"
                        f"{required_key[0]}#{required_key[1]}"
                    )
        for entry_index, entry_symbol in enumerate(entry_symbols):
            if _unresolved_entry_point_symbol(entry_symbol):
                errors.append(f"UNRESOLVED_IMPL_ENTRY_POINT_SYMBOL:{index}:{entry_index}")
        artifact_records: list[dict[str, Any]] = []
        if body_schema == IMPLEMENTATION_CARD_V6 and isinstance(body, dict):
            artifact_records = _v6_semantic_artifacts(body)
        elif isinstance(body, dict):
            acceptance_rows = body.get("acceptance", [])
            acceptance_ids = {
                item.get("id")
                for item in acceptance_rows
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            covered_acceptance_ids: set[str] = set()
            entry_points = (
                body.get("context", {}).get("entry_points", [])
                if isinstance(body.get("context"), dict)
                else []
            )
            for entry_index, entry in enumerate(entry_points):
                if not isinstance(entry, dict):
                    continue
                reason = str(entry.get("reason") or "")
                markers = set(re.findall(r"\[([A-Za-z0-9-]+)\]", reason))
                mapped_acceptance_ids = sorted(markers & acceptance_ids)
                if not mapped_acceptance_ids:
                    errors.append(f"IMPL_ENTRY_POINT_ACCEPTANCE_UNMAPPED:{index}:{entry_index}")
                covered_acceptance_ids.update(mapped_acceptance_ids)
                operations = sorted(markers & ENTRY_POINT_OPERATIONS)
                if not operations:
                    errors.append(f"IMPL_ENTRY_POINT_OPERATION_MISSING:{index}:{entry_index}")
                elif len(operations) > 1:
                    errors.append(f"IMPL_ENTRY_POINT_OPERATION_AMBIGUOUS:{index}:{entry_index}")
                artifact_records.append(
                    {
                        "path": str(entry.get("path") or "").replace("\\", "/"),
                        "symbol": str(entry.get("symbol") or ""),
                        "operation": operations[0] if len(operations) == 1 else None,
                        "acceptance_ids": mapped_acceptance_ids,
                    }
                )
            for acceptance_id in sorted(acceptance_ids - covered_acceptance_ids):
                errors.append(f"IMPL_ACCEPTANCE_ARTIFACTS_UNCOVERED:{index}:{acceptance_id}")
            for acceptance in acceptance_rows:
                if not isinstance(acceptance, dict):
                    continue
                acceptance_id = acceptance.get("id")
                criterion = str(acceptance.get("criterion") or "").lower()
                if not isinstance(acceptance_id, str) or not any(
                    marker in criterion for marker in ("이동", "옮", "move", "relocat")
                ):
                    continue
                associated = [
                    record
                    for record in artifact_records
                    if acceptance_id in record["acceptance_ids"]
                ]
                source_symbols = {
                    record["symbol"]
                    for record in associated
                    if record["operation"] == "move-source"
                }
                target_symbols = {
                    record["symbol"]
                    for record in associated
                    if record["operation"] == "move-target"
                }
                for symbol in sorted(source_symbols - target_symbols):
                    errors.append(f"IMPL_MOVE_TARGET_MISSING:{index}:{acceptance_id}:{symbol}")
                for symbol in sorted(target_symbols - source_symbols):
                    errors.append(f"IMPL_MOVE_SOURCE_MISSING:{index}:{acceptance_id}:{symbol}")
            for verification in body.get("verification", []):
                if not isinstance(verification, dict):
                    continue
                verification_id = verification.get("id")
                tests = verification.get("tests")
                if not isinstance(tests, list):
                    continue
                for test_index, selector in enumerate(tests):
                    if isinstance(selector, str) and "#" not in selector:
                        errors.append(
                            f"IMPL_VERIFICATION_METHOD_SELECTOR_REQUIRED:{index}:"
                            f"{verification_id}:{test_index}"
                        )
                test_level = verification.get("test_level")
                if (
                    isinstance(test_level, str)
                    and tests
                    and not any(
                        isinstance(selector, str)
                        and _selector_matches_level(selector, test_level)
                        for selector in tests
                    )
                ):
                    errors.append(
                        f"IMPL_VERIFICATION_LEVEL_SELECTOR_REQUIRED:{index}:"
                        f"{verification_id}:{test_level}"
                    )
                selector_text = " ".join(
                    selector.lower() for selector in tests if isinstance(selector, str)
                )
                scenarios = verification.get("scenarios")
                for marker in _required_selector_markers(
                    scenarios if isinstance(scenarios, list) else []
                ):
                    selector_markers = VERIFICATION_SELECTOR_MARKERS[marker][1]
                    if not any(candidate in selector_text for candidate in selector_markers):
                        errors.append(
                            f"IMPL_VERIFICATION_SELECTOR_NOT_DECISIVE:{index}:"
                            f"{verification_id}:{marker}"
                        )
            required_verification_scenarios = wrapper.get(
                "required_verification_scenarios"
            )
            if not isinstance(required_verification_scenarios, dict) or not all(
                isinstance(verification_id, str)
                and isinstance(scenarios, list)
                and all(isinstance(scenario, str) and scenario for scenario in scenarios)
                for verification_id, scenarios in (
                    required_verification_scenarios or {}
                ).items()
            ):
                errors.append(f"INVALID_REQUIRED_VERIFICATION_SCENARIOS:{index}")
            else:
                actual_scenarios = {
                    verification.get("id"): verification.get("scenarios", [])
                    for verification in body.get("verification", [])
                    if isinstance(verification, dict)
                }
                for (
                    verification_id,
                    required_scenarios,
                ) in required_verification_scenarios.items():
                    actual = actual_scenarios.get(verification_id, [])
                    for scenario_index, required_scenario in enumerate(
                        required_scenarios
                    ):
                        if required_scenario not in actual:
                            errors.append(
                                f"IMPL_REQUIRED_VERIFICATION_SCENARIO_MISSING:{index}:"
                                f"{verification_id}:{scenario_index}"
                            )
        semantic_artifacts.append(artifact_records)
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
    for parent_index, parent_artifacts in enumerate(semantic_artifacts):
        for child_index in range(parent_index + 1, len(semantic_artifacts)):
            child_artifacts = semantic_artifacts[child_index]
            declared_dependencies = cards[child_index].get("depends_on_ordinals", [])
            if not isinstance(declared_dependencies, list):
                continue
            for parent_artifact in parent_artifacts:
                parent_operation = parent_artifact.get("operation")
                if parent_operation not in {"create", "modify", "move-target", "delete"}:
                    continue
                for child_artifact in child_artifacts:
                    child_operation = child_artifact.get("operation")
                    same_artifact = (
                        parent_artifact.get("path") == child_artifact.get("path")
                        and parent_artifact.get("symbol") == child_artifact.get("symbol")
                    )
                    requires_parent = child_operation == "verify" or (
                        parent_operation in {"create", "move-target"}
                        and child_operation in {"modify", "preserve"}
                    )
                    if (
                        same_artifact
                        and requires_parent
                        and parent_index + 1 not in declared_dependencies
                    ):
                        errors.append(
                            f"IMPL_SEMANTIC_DEPENDENCY_REQUIRED:{parent_index + 1}:"
                            f"{child_index + 1}:{parent_artifact.get('symbol')}"
                        )
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
                parent_artifacts = semantic_artifacts[parent_ordinal - 1]
                child_body = cards[child_ordinal - 1].get("body")
                child_artifacts = semantic_artifacts[child_ordinal - 1]
                if (
                    isinstance(child_body, dict)
                    and _implementation_schema(child_body) == IMPLEMENTATION_CARD_V6
                ):
                    child_artifacts = _v6_semantic_references(child_body)
                artifact = edge["required_artifact"]
                consumer = edge["consumer_entry_point"]
                artifact_proven = any(
                    parent.get("operation")
                    in {"create", "modify", "move-target", "delete"}
                    and parent.get("path") == child.get("path")
                    and parent.get("symbol") == child.get("symbol")
                    and artifact in _artifact_identities(parent)
                    and artifact in _artifact_identities(child)
                    for parent in parent_artifacts
                    for child in child_artifacts
                )
                artifact_proven = artifact_proven or (
                    artifact in _operation_symbols(child_body)
                    and any(
                        parent.get("operation")
                        in {"create", "modify", "move-target", "delete"}
                        and artifact in _artifact_identities(parent)
                        for parent in parent_artifacts
                    )
                )
                artifact_proven = artifact_proven or (
                    artifact
                    in _verification_selectors(cards[parent_ordinal - 1].get("body"))
                    and artifact in _verification_selectors(child_body)
                )
                if not artifact_proven:
                    errors.append(
                        f"IMPL_DEPENDENCY_ARTIFACT_UNPROVEN:{parent_ordinal}:{child_ordinal}"
                    )
                if consumer not in _dependency_consumer_entry_points(child_body):
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
