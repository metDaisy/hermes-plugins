"""Minimal backend implementation card v6 contract.

The card defines observable implementation obligations without prescribing the
Coder's internal algorithm or repeating workflow-owned execution rules.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any


SCHEMA = "backend-implementation-card-v6"
CARD_FIELDS = {
    "schema",
    "trace",
    "objective",
    "boundary",
    "obligations",
    "constraints",
    "verification",
}
TRACE_FIELDS = {
    "triage_digest",
    "slice_id",
    "requirement_ids",
    "observable_ids",
    "gap_ids",
}
OBLIGATION_KINDS = {"behavior", "preservation"}
OBSERVABLE_SOURCES = {
    "return-value",
    "http-response",
    "persisted-reload",
    "in-memory-state",
    "interaction",
    "concurrency",
    "structure",
    "thrown-exception",
}
COMPARISONS = {
    "exact",
    "exact-set",
    "exact-map",
    "unchanged",
    "equals-expression",
    "ordered",
    "absent",
    "present",
}
TEST_LEVELS = {"unit", "slice", "repository", "integration", "modulith"}
WORKFLOW_SCOPE_PREFIXES = {
    "기준선 사전 점검",
    "RED",
    "GREEN",
    "검증 CHECK/EXPECT",
    "롤백 경계",
    "완료 증거",
}
SELECTOR = re.compile(
    r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+(?:#[A-Za-z_$][\w$]*)?"
)


def _non_empty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _strings(value: Any, *, nonempty: bool = True) -> bool:
    return (
        isinstance(value, list)
        and (bool(value) or not nonempty)
        and all(_non_empty(item) for item in value)
        and len(value) == len(set(value))
    )


def _error(errors: list[str], condition: bool, code: str) -> None:
    if not condition:
        errors.append(code)


def _exact_fields(value: Any, allowed: set[str], errors: list[str], prefix: str) -> bool:
    if not isinstance(value, dict):
        errors.append(f"{prefix}_NOT_OBJECT")
        return False
    for field in sorted(set(value) - allowed):
        errors.append(f"UNEXPECTED_{prefix}_FIELD:{field}")
    for field in sorted(allowed - set(value)):
        errors.append(f"MISSING_{prefix}_FIELD:{field}")
    return set(value) == allowed


def template() -> dict[str, Any]:
    """Return an intentionally incomplete fact-only v6 scaffold."""
    return {
        "schema": SCHEMA,
        "trace": {
            "triage_digest": "",
            "slice_id": "",
            "requirement_ids": [],
            "observable_ids": [],
            "gap_ids": [],
        },
        "objective": {"outcome": ""},
        "boundary": {"in_scope": [], "out_of_scope": []},
        "obligations": [],
        "constraints": [],
        "verification": [],
    }


def _stable_unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _is_concrete_member_selector(value: Any) -> bool:
    """Reject class-only and constructor-like selectors as executable proof targets."""
    if not isinstance(value, str) or SELECTOR.fullmatch(value) is None or "#" not in value:
        return False
    owner, member = value.rsplit("#", 1)
    return bool(owner and member and member != owner.rsplit(".", 1)[-1])


def _java_locator_symbol(locator: Any, source_set: str) -> str:
    if not isinstance(locator, str):
        return ""
    match = re.fullmatch(
        rf"src/{source_set}/java/(.+)\.java#(.+)",
        locator.replace("\\", "/"),
    )
    if match is None:
        return ""
    qualified_class = match.group(1).replace("/", ".")
    member = match.group(2).split("#")[-1].split("(", 1)[0].split(".")[-1]
    symbol = f"{qualified_class}#{member}"
    return symbol if _is_concrete_member_selector(symbol) else ""


def _is_class_locator(locator: Any, source_set: str) -> bool:
    if not isinstance(locator, str):
        return False
    match = re.fullmatch(
        rf"src/{source_set}/java/(.+)\.java#(.+)",
        locator.replace("\\", "/"),
    )
    if match is None:
        return False
    class_name = match.group(1).rsplit("/", 1)[-1]
    member = match.group(2).split("#")[-1].split("(", 1)[0].split(".")[-1]
    return member == class_name


def _has_structural_source_evidence(
    matrices: dict[str, dict[str, Any]], verification_ids: list[str]
) -> bool:
    return any(
        _is_class_locator(evidence.get("locator"), "main")
        for verification_id in verification_ids
        for evidence in matrices.get(verification_id, {}).get("evidence", [])
        if isinstance(evidence, dict)
    )


def skeletons_from_frozen_triage(triage: Any) -> list[dict[str, Any]]:
    """Derive only source-proven V6 facts; leave proof authoring visibly unresolved."""
    if (
        not isinstance(triage, dict)
        or triage.get("schema") != "triage-v7"
        or triage.get("phase") != "frozen"
    ):
        raise ValueError("FROZEN_TRIAGE_REQUIRED")
    canonical = copy.deepcopy(triage)
    canonical["frozen_digest"] = None
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if triage.get("frozen_digest") != hashlib.sha256(payload.encode("utf-8")).hexdigest():
        raise ValueError("FROZEN_TRIAGE_DIGEST_MISMATCH")
    digest = triage.get("frozen_digest")
    intent = triage.get("intent") if isinstance(triage.get("intent"), dict) else {}
    global_out = intent.get("out_of_scope") if isinstance(intent.get("out_of_scope"), list) else []
    slices = triage.get("implementation_slices")
    gaps = {
        item.get("id"): item
        for item in triage.get("confirmed_gaps", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    matrices = {
        item.get("id"): item
        for item in triage.get("verification_matrix", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if not isinstance(slices, list) or not slices:
        raise ValueError("IMPLEMENTATION_SLICES_REQUIRED")

    cards: list[dict[str, Any]] = []
    for slice_body in slices:
        if not isinstance(slice_body, dict):
            raise ValueError("IMPLEMENTATION_SLICE_NOT_OBJECT")
        acceptances = [
            item for item in slice_body.get("acceptance", []) if isinstance(item, dict)
        ]
        requirement_ids = _stable_unique(
            [
                item
                for acceptance in acceptances
                for item in acceptance.get("requirement_ids", [])
                if isinstance(item, str)
            ]
        )
        observable_ids = _stable_unique(
            [
                item
                for acceptance in acceptances
                for item in acceptance.get("observable_ids", [])
                if isinstance(item, str)
            ]
        )
        gap_ids = [item for item in slice_body.get("gap_ids", []) if isinstance(item, str)]

        constraints: list[dict[str, Any]] = []
        constraint_observables: dict[str, set[str]] = {}
        for gap_id in gap_ids:
            gap = gaps.get(gap_id, {})
            affected = {
                item for item in gap.get("affected_observable_ids", []) if isinstance(item, str)
            }
            for evidence_index, evidence in enumerate(gap.get("evidence", []), start=1):
                if not isinstance(evidence, dict):
                    continue
                constraint_id = f"{gap_id}-FACT-{evidence_index:03d}"
                constraints.append(
                    {
                        "id": constraint_id,
                        "rule": evidence.get("claim", ""),
                        "source": {
                            "locator": evidence.get("locator", ""),
                            "claim": evidence.get("claim", ""),
                        },
                    }
                )
                constraint_observables[constraint_id] = affected
            for rule_index, rule in enumerate(gap.get("rules", []), start=1):
                if not isinstance(rule, dict):
                    continue
                constraint_id = f"{gap_id}-RULE-{rule_index:03d}"
                constraints.append(
                    {
                        "id": constraint_id,
                        "rule": gap.get("required_state", ""),
                        "source": {
                            "locator": rule.get("locator", ""),
                            "claim": rule.get("claim", ""),
                        },
                    }
                )
                constraint_observables[constraint_id] = affected

        obligations: list[dict[str, Any]] = []
        verification_to_targets: dict[str, list[str]] = {}
        for acceptance in acceptances:
            obligation_id = acceptance.get("id", "")
            verification_ids = [
                item for item in acceptance.get("verification_ids", []) if isinstance(item, str)
            ]
            seam_symbols = _stable_unique(
                [
                    _java_locator_symbol(
                        matrices.get(verification_id, {}).get("production_seam", {}).get("locator"),
                        "main",
                    )
                    for verification_id in verification_ids
                ]
            )
            seam_symbols = [symbol for symbol in seam_symbols if "#" in symbol]
            if not seam_symbols or _has_structural_source_evidence(matrices, verification_ids):
                production_seam = next(
                    (
                        matrices.get(verification_id, {}).get("production_seam")
                        for verification_id in verification_ids
                        if isinstance(
                            matrices.get(verification_id, {}).get("production_seam"), dict
                        )
                    ),
                    {},
                )
                constraints.append(
                    {
                        "id": obligation_id,
                        "rule": acceptance.get("criterion", ""),
                        "source": {
                            "locator": production_seam.get("locator", ""),
                            "claim": production_seam.get("claim", ""),
                        },
                    }
                )
            else:
                obligations.append(
                    {
                        "id": obligation_id,
                        "kind": "behavior",
                        "given": [],
                        "operation": {
                            "symbol": seam_symbols[0] if len(seam_symbols) == 1 else "",
                            "invocation_count": 0,
                        },
                        "expected": [acceptance.get("criterion", "")],
                        "observables": [],
                    }
                )
            for verification_id in verification_ids:
                verification_to_targets.setdefault(verification_id, []).append(obligation_id)

        verification: list[dict[str, Any]] = []
        for verification_id, target_ids in verification_to_targets.items():
            matrix = matrices.get(verification_id, {})
            observable_id = matrix.get("observable_id")
            constraint_ids = [
                identifier
                for identifier, affected in constraint_observables.items()
                if observable_id in affected
            ]
            selectors = _stable_unique(
                [
                    _java_locator_symbol(evidence.get("locator"), "test")
                    for evidence in matrix.get("evidence", [])
                    if isinstance(evidence, dict)
                ]
            )
            verification.append(
                {
                    "id": verification_id,
                    "covers": _stable_unique([*target_ids, *constraint_ids]),
                    "level": "",
                    "selectors": [selector for selector in selectors if selector],
                }
            )

        in_scope = [
            item
            for item in slice_body.get("scope", [])
            if isinstance(item, str) and item.split(":", 1)[0].strip() not in WORKFLOW_SCOPE_PREFIXES
        ]
        local_out = [item for item in slice_body.get("out_of_scope", []) if isinstance(item, str)]
        cards.append(
            {
                "schema": SCHEMA,
                "trace": {
                    "triage_digest": digest,
                    "slice_id": slice_body.get("id", ""),
                    "requirement_ids": requirement_ids,
                    "observable_ids": observable_ids,
                    "gap_ids": gap_ids,
                },
                "objective": {"outcome": slice_body.get("outcome", "")},
                "boundary": {
                    "in_scope": _stable_unique(in_scope),
                    "out_of_scope": _stable_unique([*global_out, *local_out]),
                },
                "obligations": obligations,
                "constraints": constraints,
                "verification": verification,
            }
        )
    return cards


def _validate_trace(value: Any, errors: list[str]) -> None:
    if not _exact_fields(value, TRACE_FIELDS, errors, "TRACE"):
        return
    _error(
        errors,
        isinstance(value.get("triage_digest"), str)
        and bool(re.fullmatch(r"[0-9a-f]{64}", value["triage_digest"])),
        "INVALID_TRACE_TRIAGE_DIGEST",
    )
    _error(errors, _non_empty(value.get("slice_id")), "MISSING_TRACE_SLICE_ID")
    for field in ("requirement_ids", "observable_ids", "gap_ids"):
        _error(errors, _strings(value.get(field)), f"MISSING_TRACE_{field.upper()}")


def _validate_objective(value: Any, errors: list[str]) -> None:
    if not _exact_fields(value, {"outcome"}, errors, "OBJECTIVE"):
        return
    _error(errors, _non_empty(value.get("outcome")), "MISSING_OBJECTIVE_OUTCOME")


def _validate_boundary(value: Any, errors: list[str]) -> None:
    if not _exact_fields(value, {"in_scope", "out_of_scope"}, errors, "BOUNDARY"):
        return
    _error(errors, _strings(value.get("in_scope")), "MISSING_BOUNDARY_IN_SCOPE")
    _error(errors, _strings(value.get("out_of_scope")), "MISSING_BOUNDARY_OUT_OF_SCOPE")


def _validate_operation(value: Any, errors: list[str], obligation_id: str) -> None:
    if not _exact_fields(value, {"symbol", "invocation_count"}, errors, f"OBLIGATION_OPERATION:{obligation_id}"):
        return
    _error(
        errors,
        _non_empty(value.get("symbol")),
        f"MISSING_OBLIGATION_OPERATION_SYMBOL:{obligation_id}",
    )
    symbol = value.get("symbol")
    _error(
        errors,
        _is_concrete_member_selector(symbol),
        f"INVALID_OBLIGATION_OPERATION_SYMBOL:{obligation_id}",
    )
    count = value.get("invocation_count")
    _error(
        errors,
        isinstance(count, int) and not isinstance(count, bool) and count > 0,
        f"INVALID_OBLIGATION_OPERATION_INVOCATION_COUNT:{obligation_id}",
    )


def _validate_observable(
    value: Any,
    errors: list[str],
    obligation_id: str,
    index: int,
) -> None:
    prefix = f"OBSERVABLE:{obligation_id}:{index}"
    fields = {"target", "source", "comparison", "identity", "values", "expected"}
    if not _exact_fields(value, fields, errors, prefix):
        return
    target = value.get("target")
    _error(errors, _non_empty(target), f"MISSING_OBSERVABLE_TARGET:{obligation_id}:{index}")
    _error(
        errors,
        not (
            isinstance(target, str)
            and target.strip().casefold() == f"{obligation_id} observable result".casefold()
        ),
        f"PLACEHOLDER_OBSERVABLE_TARGET:{obligation_id}:{index}",
    )
    source = value.get("source")
    comparison = value.get("comparison")
    _error(
        errors,
        source in OBSERVABLE_SOURCES,
        f"INVALID_OBSERVABLE_SOURCE:{obligation_id}:{index}",
    )
    _error(
        errors,
        comparison in COMPARISONS,
        f"INVALID_OBSERVABLE_COMPARISON:{obligation_id}:{index}",
    )
    identity = value.get("identity")
    projected_values = value.get("values")
    _error(
        errors,
        _strings(identity, nonempty=False),
        f"INVALID_OBSERVABLE_IDENTITY:{obligation_id}:{index}",
    )
    _error(
        errors,
        _strings(projected_values, nonempty=False),
        f"INVALID_OBSERVABLE_VALUES:{obligation_id}:{index}",
    )
    if source == "persisted-reload" and comparison in {"unchanged", "exact-map"}:
        _error(
            errors,
            _strings(identity),
            f"MISSING_OBSERVABLE_IDENTITY:{obligation_id}:{index}",
        )
        _error(
            errors,
            _strings(projected_values),
            f"MISSING_OBSERVABLE_VALUES:{obligation_id}:{index}",
        )
    expected = value.get("expected")
    _error(
        errors,
        expected is not None and (not isinstance(expected, str) or bool(expected.strip())),
        f"MISSING_OBSERVABLE_EXPECTED:{obligation_id}:{index}",
    )


def _validate_obligations(value: Any, errors: list[str]) -> set[str]:
    if not isinstance(value, list):
        errors.append("INVALID_OBLIGATIONS")
        return set()
    known: set[str] = set()
    fields = {"id", "kind", "given", "operation", "expected", "observables"}
    for index, obligation in enumerate(value):
        if not isinstance(obligation, dict):
            errors.append(f"OBLIGATION_NOT_OBJECT:{index}")
            continue
        identifier = obligation.get("id")
        label = identifier if _non_empty(identifier) else str(index)
        _exact_fields(obligation, fields, errors, f"OBLIGATION:{label}")
        _error(errors, _non_empty(identifier), f"MISSING_OBLIGATION_ID:{index}")
        if _non_empty(identifier):
            if identifier in known:
                errors.append(f"DUPLICATE_OBLIGATION_ID:{identifier}")
            known.add(identifier)
        _error(
            errors,
            obligation.get("kind") in OBLIGATION_KINDS,
            f"INVALID_OBLIGATION_KIND:{label}",
        )
        _error(errors, _strings(obligation.get("given"), nonempty=False), f"INVALID_OBLIGATION_GIVEN:{label}")
        _validate_operation(obligation.get("operation"), errors, label)
        _error(errors, _strings(obligation.get("expected")), f"MISSING_OBLIGATION_EXPECTED:{label}")
        observables = obligation.get("observables")
        if not isinstance(observables, list) or not observables:
            errors.append(f"MISSING_OBLIGATION_OBSERVABLES:{label}")
            continue
        for observable_index, observable in enumerate(observables):
            _validate_observable(observable, errors, label, observable_index)
    return known


def _validate_constraints(value: Any, errors: list[str]) -> set[str]:
    if not isinstance(value, list):
        errors.append("INVALID_CONSTRAINTS")
        return set()
    known: set[str] = set()
    for index, constraint in enumerate(value):
        if not isinstance(constraint, dict):
            errors.append(f"CONSTRAINT_NOT_OBJECT:{index}")
            continue
        identifier = constraint.get("id")
        label = identifier if _non_empty(identifier) else str(index)
        if not _exact_fields(constraint, {"id", "rule", "source"}, errors, f"CONSTRAINT:{label}"):
            continue
        _error(errors, _non_empty(identifier), f"MISSING_CONSTRAINT_ID:{index}")
        if _non_empty(identifier):
            if identifier in known:
                errors.append(f"DUPLICATE_CONSTRAINT_ID:{identifier}")
            known.add(identifier)
        _error(errors, _non_empty(constraint.get("rule")), f"MISSING_CONSTRAINT_RULE:{label}")
        source = constraint.get("source")
        if not _exact_fields(source, {"locator", "claim"}, errors, f"CONSTRAINT_SOURCE:{label}"):
            continue
        _error(errors, _non_empty(source.get("locator")), f"MISSING_CONSTRAINT_SOURCE_LOCATOR:{label}")
        _error(errors, _non_empty(source.get("claim")), f"MISSING_CONSTRAINT_SOURCE_CLAIM:{label}")
    return known


def _validate_verification(
    value: Any,
    obligation_ids: set[str],
    constraint_ids: set[str],
    errors: list[str],
) -> None:
    if not isinstance(value, list) or not value:
        errors.append("MISSING_VERIFICATION")
        for obligation_id in sorted(obligation_ids):
            errors.append(f"UNCOVERED_OBLIGATION:{obligation_id}")
        for constraint_id in sorted(constraint_ids):
            errors.append(f"UNCOVERED_CONSTRAINT:{constraint_id}")
        return
    known: set[str] = set()
    covered: set[str] = set()
    for index, verification in enumerate(value):
        if not isinstance(verification, dict):
            errors.append(f"VERIFICATION_NOT_OBJECT:{index}")
            continue
        identifier = verification.get("id")
        label = identifier if _non_empty(identifier) else str(index)
        if not _exact_fields(
            verification,
            {"id", "covers", "level", "selectors"},
            errors,
            f"VERIFICATION:{label}",
        ):
            continue
        _error(errors, _non_empty(identifier), f"MISSING_VERIFICATION_ID:{index}")
        if _non_empty(identifier):
            if identifier in known:
                errors.append(f"DUPLICATE_VERIFICATION_ID:{identifier}")
            known.add(identifier)
        covers = verification.get("covers")
        _error(errors, _strings(covers), f"MISSING_VERIFICATION_COVERS:{label}")
        if isinstance(covers, list):
            for obligation_id in covers:
                if obligation_id not in obligation_ids | constraint_ids:
                    errors.append(f"UNKNOWN_VERIFICATION_TARGET:{label}:{obligation_id}")
                else:
                    covered.add(obligation_id)
        _error(
            errors,
            verification.get("level") in TEST_LEVELS,
            f"INVALID_VERIFICATION_LEVEL:{label}",
        )
        selectors = verification.get("selectors")
        _error(errors, _strings(selectors), f"MISSING_VERIFICATION_SELECTORS:{label}")
        if isinstance(selectors, list):
            _error(
                errors,
                all(_is_concrete_member_selector(selector) for selector in selectors),
                f"INVALID_VERIFICATION_SELECTOR:{label}",
            )
    for obligation_id in sorted(obligation_ids - covered):
        errors.append(f"UNCOVERED_OBLIGATION:{obligation_id}")
    for constraint_id in sorted(constraint_ids - covered):
        errors.append(f"UNCOVERED_CONSTRAINT:{constraint_id}")


def validate(card: Any) -> list[str]:
    """Return deterministic v6 contract failures without side effects."""
    if not isinstance(card, dict):
        return ["CARD_NOT_OBJECT"]
    errors: list[str] = []
    for field in sorted(set(card) - CARD_FIELDS):
        errors.append(f"UNEXPECTED_CARD_FIELD:{field}")
    for field in sorted(CARD_FIELDS - set(card)):
        errors.append(f"MISSING_CARD_FIELD:{field}")
    _error(errors, card.get("schema") == SCHEMA, "SCHEMA_VERSION")
    _validate_trace(card.get("trace"), errors)
    _validate_objective(card.get("objective"), errors)
    _validate_boundary(card.get("boundary"), errors)
    obligation_ids = _validate_obligations(card.get("obligations"), errors)
    constraint_ids = _validate_constraints(card.get("constraints"), errors)
    if not obligation_ids and not constraint_ids:
        errors.append("MISSING_IMPLEMENTATION_CONTRACT")
    for duplicate_id in sorted(obligation_ids & constraint_ids):
        errors.append(f"DUPLICATE_CONTRACT_ID:{duplicate_id}")
    _validate_verification(card.get("verification"), obligation_ids, constraint_ids, errors)
    return errors


__all__ = ["SCHEMA", "skeletons_from_frozen_triage", "template", "validate"]
