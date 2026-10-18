#!/usr/bin/env python3
"""Validate PM/Coder checkpoint handoff contracts."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

BUILD_TASK_GRAPH_SCRIPTS = Path(__file__).resolve().parents[2] / "kwc-build-task-graph" / "scripts"
sys.path.insert(0, str(BUILD_TASK_GRAPH_SCRIPTS))
from build_task_graph import validate as validate_card  # noqa: E402

SHA = re.compile(r"^[0-9a-f]{40}$")
TEST_LEVELS = {"unit", "slice", "repository", "integration", "modulith"}
CURRENT_HANDOFF_SCHEMA = "backend-implementation-handoff-v2"
HANDOFF_FIELDS = {
    "schema",
    "handoff_id",
    "source_run_id",
    "implemented_behavior_ids",
    "changed_paths",
    "acceptance_results",
    "focused_verification_results",
    "full_backend_verification_result",
    "documentation_impact",
    "residual_risks",
    "implementation_convention_readback",
}
LAYERS = {"presentation", "application", "domain", "infra"}
CHECKPOINT_FIELDS = {
    "schema",
    "source_handoff_id",
    "result",
    "committed_paths",
    "commit_sha",
    "focused_verification_readback",
    "full_backend_verification_readback",
    "verification_run_id",
    "documentation_impact_resolution",
    "clean_worktree",
}


def _non_empty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _strings(value: Any, *, allow_empty: bool = False) -> bool:
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and all(_non_empty(item) for item in value)
    )


def _exact_ids(value: Any, expected: set[str]) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(item, str) for item in value)
        and len(value) == len(set(value))
        and set(value) == expected
    )


def _unexpected(value: dict[str, Any], allowed: set[str], errors: list[str], prefix: str) -> None:
    for field in sorted(set(value) - allowed):
        errors.append(f"{prefix}:{field}")


def _is_document_path(path: str) -> bool:
    normalized = path.casefold()
    name = normalized.rsplit("/", 1)[-1].lower()
    return (
        normalized.startswith("docs/")
        or normalized.startswith(".hermes/profiles/")
        or name in {"readme.md", "agents.md", "claude.md"}
        or name.endswith((".md", ".mdx", ".adoc", ".rst"))
    )


def _is_literal_repository_path(path: str) -> bool:
    if not _non_empty(path) or "\\" in path or path.startswith(("/", ":", "-")):
        return False
    if re.match(r"^[A-Za-z]:/", path):
        return False
    if "..." in path or any(marker in path for marker in ("*", "?", "[", "]", "{" , "}")):
        return False
    return all(part not in {"", ".", ".."} for part in path.split("/"))


def _validate_implementation_convention_readback(value: Any, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append("INVALID_IMPLEMENTATION_CONVENTION_READBACK")
        return
    _unexpected(
        value,
        {"backend_development_guide_sections", "backend_test_guide_sections", "architecture_sections", "package_info_paths", "layer_ownership", "checked_boundaries"},
        errors,
        "UNEXPECTED_IMPLEMENTATION_CONVENTION_READBACK_FIELD",
    )
    for field in ("backend_development_guide_sections", "backend_test_guide_sections", "checked_boundaries"):
        items = value.get(field)
        if not _strings(items) or len(items) != len(set(items)):
            errors.append(f"INVALID_IMPLEMENTATION_CONVENTION_{field.upper()}")
    for field in ("architecture_sections", "package_info_paths"):
        items = value.get(field)
        if not _strings(items, allow_empty=True) or len(items) != len(set(items)):
            errors.append(f"INVALID_IMPLEMENTATION_CONVENTION_{field.upper()}")
    package_paths = value.get("package_info_paths")
    if isinstance(package_paths, list):
        for path in package_paths:
            if isinstance(path, str) and path.endswith("package-info.java") and _is_literal_repository_path(path):
                continue
            errors.append(f"INVALID_IMPLEMENTATION_CONVENTION_PACKAGE_INFO_PATH:{path}")
    ownership = value.get("layer_ownership")
    if not isinstance(ownership, list) or not ownership:
        errors.append("INVALID_IMPLEMENTATION_CONVENTION_LAYER_OWNERSHIP")
        return
    concerns: list[str] = []
    for index, item in enumerate(ownership):
        if not isinstance(item, dict):
            errors.append(f"IMPLEMENTATION_CONVENTION_LAYER_OWNERSHIP_NOT_OBJECT:{index}")
            continue
        _unexpected(item, {"concern", "owner"}, errors, f"UNEXPECTED_IMPLEMENTATION_CONVENTION_LAYER_OWNERSHIP_FIELD:{index}")
        concern = item.get("concern")
        if _non_empty(concern):
            concerns.append(concern)
        else:
            errors.append(f"MISSING_IMPLEMENTATION_CONVENTION_CONCERN:{index}")
        if item.get("owner") not in LAYERS:
            errors.append(f"INVALID_IMPLEMENTATION_CONVENTION_OWNER:{index}")
    if len(concerns) != len(set(concerns)):
        errors.append("DUPLICATE_IMPLEMENTATION_CONVENTION_CONCERN")


def validate_handoff(card: Any, handoff: Any) -> list[str]:
    """Validate a Coder review handoff against its immutable Impl card."""
    errors: list[str] = []
    card_errors = validate_card(card)
    if card_errors:
        return [f"INVALID_CARD:{error}" for error in card_errors]
    if not isinstance(handoff, dict):
        return ["HANDOFF_NOT_OBJECT"]
    schema = handoff.get("schema")
    _unexpected(handoff, HANDOFF_FIELDS, errors, "UNEXPECTED_HANDOFF_FIELD")
    if schema != CURRENT_HANDOFF_SCHEMA:
        errors.append("HANDOFF_SCHEMA")
    if not _non_empty(handoff.get("handoff_id")):
        errors.append("MISSING_HANDOFF_ID")
    if not _positive_int(handoff.get("source_run_id")):
        errors.append("MISSING_SOURCE_RUN_ID")
    _validate_implementation_convention_readback(handoff.get("implementation_convention_readback"), errors)

    card_slice = card.get("slice")
    behavior_ids = set(card_slice.get("behavior_ids", [])) if isinstance(card_slice, dict) else set()
    if not _exact_ids(handoff.get("implemented_behavior_ids"), behavior_ids):
        errors.append("BEHAVIOR_COVERAGE_MISMATCH")

    changed_paths = handoff.get("changed_paths")
    valid_changed_paths = _strings(changed_paths)
    if not valid_changed_paths or len(changed_paths) != len(set(changed_paths)):
        errors.append("INVALID_CHANGED_PATHS")
    if isinstance(changed_paths, list):
        for path in changed_paths:
            if not isinstance(path, str):
                continue
            if not _is_literal_repository_path(path):
                errors.append(f"INVALID_CHANGED_PATH:{path}")
            elif _is_document_path(path):
                errors.append(f"DOCUMENT_PATH_IN_CHANGED_PATHS:{path}")

    acceptance_ids = {
        entry.get("id")
        for entry in card.get("acceptance", [])
        if isinstance(entry, dict) and _non_empty(entry.get("id"))
    }
    expected_acceptance_verifications: dict[str, set[str]] = {
        acceptance_id: set() for acceptance_id in acceptance_ids
    }
    verification_contracts: dict[str, dict[str, Any]] = {}
    for verification in card.get("verification", []):
        if not isinstance(verification, dict) or not _non_empty(verification.get("id")):
            continue
        verification_id = verification["id"]
        verification_contracts[verification_id] = verification
        for acceptance_id in verification.get("acceptance_ids", []):
            if acceptance_id in expected_acceptance_verifications:
                expected_acceptance_verifications[acceptance_id].add(verification_id)

    acceptance_results = handoff.get("acceptance_results")
    result_acceptance_ids: list[str] = []
    if not isinstance(acceptance_results, list):
        errors.append("INVALID_ACCEPTANCE_RESULTS")
    else:
        for index, result in enumerate(acceptance_results):
            if not isinstance(result, dict):
                errors.append(f"ACCEPTANCE_RESULT_NOT_OBJECT:{index}")
                continue
            _unexpected(
                result,
                {"acceptance_id", "result", "focused_verification_ids"},
                errors,
                f"UNEXPECTED_ACCEPTANCE_RESULT_FIELD:{index}",
            )
            acceptance_id = result.get("acceptance_id")
            if isinstance(acceptance_id, str):
                result_acceptance_ids.append(acceptance_id)
            if result.get("result") != "pass":
                errors.append(f"ACCEPTANCE_NOT_PASS:{acceptance_id or index}")
            expected = expected_acceptance_verifications.get(acceptance_id, set())
            if not _exact_ids(result.get("focused_verification_ids"), expected):
                errors.append(f"ACCEPTANCE_VERIFICATION_MISMATCH:{acceptance_id or index}")
    if not _exact_ids(result_acceptance_ids, acceptance_ids):
        errors.append("ACCEPTANCE_COVERAGE_MISMATCH")

    focused_results = handoff.get("focused_verification_results")
    result_verification_ids: list[str] = []
    if not isinstance(focused_results, list):
        errors.append("INVALID_FOCUSED_RESULTS")
    else:
        for index, result in enumerate(focused_results):
            if not isinstance(result, dict):
                errors.append(f"FOCUSED_RESULT_NOT_OBJECT:{index}")
                continue
            _unexpected(
                result,
                {"verification_id", "runner", "CHECK", "CWD", "EXPECT", "test_levels", "scenario_results", "result"},
                errors,
                f"UNEXPECTED_FOCUSED_RESULT_FIELD:{index}",
            )
            verification_id = result.get("verification_id")
            if isinstance(verification_id, str):
                result_verification_ids.append(verification_id)
            label = verification_id or str(index)
            contract = verification_contracts.get(verification_id, {})
            if result.get("runner") != "gradle-mcp":
                errors.append(f"FOCUSED_RUNNER_MISMATCH:{label}")
            expected_check = {"task": contract.get("task"), "tests": contract.get("tests")}
            if result.get("CHECK") != expected_check:
                errors.append(f"FOCUSED_CHECK_MISMATCH:{label}")
            if result.get("CWD") != ".":
                errors.append(f"FOCUSED_CWD_MISMATCH:{label}")
            if result.get("EXPECT") != "BUILD SUCCESSFUL":
                errors.append(f"FOCUSED_EXPECT_MISMATCH:{label}")
            test_levels = result.get("test_levels")
            if (
                not _strings(test_levels)
                or len(test_levels) != len(set(test_levels))
                or any(level not in TEST_LEVELS for level in test_levels)
                or contract.get("test_level") not in test_levels
            ):
                errors.append(f"TEST_LEVEL_NOT_SATISFIED:{label}")
            if result.get("result") != "pass":
                errors.append(f"FOCUSED_RESULT_NOT_PASS:{label}")
            scenario_results = result.get("scenario_results")
            actual_scenarios: list[str] = []
            if not isinstance(scenario_results, list):
                errors.append(f"INVALID_SCENARIO_RESULTS:{label}")
            else:
                for scenario_index, scenario_result in enumerate(scenario_results):
                    if not isinstance(scenario_result, dict):
                        errors.append(f"SCENARIO_RESULT_NOT_OBJECT:{label}:{scenario_index}")
                        continue
                    _unexpected(
                        scenario_result,
                        {"scenario", "result"},
                        errors,
                        f"UNEXPECTED_SCENARIO_RESULT_FIELD:{label}:{scenario_index}",
                    )
                    scenario = scenario_result.get("scenario")
                    if isinstance(scenario, str):
                        actual_scenarios.append(scenario)
                    if scenario_result.get("result") != "pass":
                        errors.append(f"SCENARIO_NOT_PASS:{label}:{scenario_index}")
            expected_scenarios = set(
                verification_contracts.get(verification_id, {}).get("scenarios", [])
            )
            if not _exact_ids(actual_scenarios, expected_scenarios):
                errors.append(f"SCENARIO_COVERAGE_MISMATCH:{label}")
    if not _exact_ids(result_verification_ids, set(verification_contracts)):
        errors.append("FOCUSED_VERIFICATION_COVERAGE_MISMATCH")

    full = handoff.get("full_backend_verification_result")
    if not isinstance(full, dict):
        errors.append("INVALID_FULL_BACKEND_RESULT")
    else:
        _unexpected(full, {"runner", "CHECK", "CWD", "EXPECT", "result"}, errors, "UNEXPECTED_FULL_RESULT_FIELD")
        if full.get("runner") != "gradle-mcp":
            errors.append("FULL_BACKEND_RUNNER_MISMATCH")
        if full.get("CHECK") != "test":
            errors.append("FULL_BACKEND_CHECK_MISMATCH")
        if full.get("CWD") != ".":
            errors.append("FULL_BACKEND_CWD_MISMATCH")
        if full.get("EXPECT") != "BUILD SUCCESSFUL":
            errors.append("FULL_BACKEND_EXPECT_MISMATCH")
        if full.get("result") != "pass":
            errors.append("FULL_BACKEND_NOT_PASS")


    impact = handoff.get("documentation_impact")
    if not isinstance(impact, dict):
        errors.append("INVALID_DOCUMENTATION_IMPACT")
    else:
        _unexpected(impact, {"detected", "details"}, errors, "UNEXPECTED_DOCUMENTATION_IMPACT_FIELD")
        detected = impact.get("detected")
        details = impact.get("details")
        if not isinstance(detected, bool) or not _strings(details, allow_empty=True):
            errors.append("INVALID_DOCUMENTATION_IMPACT")
        elif detected != bool(details):
            errors.append("DOCUMENTATION_IMPACT_MISMATCH")
    if not _strings(handoff.get("residual_risks"), allow_empty=True):
        errors.append("INVALID_RESIDUAL_RISKS")
    return errors


def _validate_review_run(review_run: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(review_run, dict):
        return ["REVIEW_RUN_NOT_OBJECT"]
    _unexpected(
        review_run,
        {"run_id", "status", "assignee", "source_status"},
        errors,
        "UNEXPECTED_REVIEW_RUN_FIELD",
    )
    if not _positive_int(review_run.get("run_id")):
        errors.append("INVALID_REVIEW_RUN_ID")
    if review_run.get("status") != "running":
        errors.append("REVIEW_RUN_NOT_ACTIVE")
    if review_run.get("assignee") != "project-manager":
        errors.append("REVIEW_RUN_ASSIGNEE_MISMATCH")
    if review_run.get("source_status") != "review":
        errors.append("REVIEW_RUN_NOT_CLAIMED_FROM_REVIEW")
    return errors


def validate_change_request(card: Any, handoff: Any, review_run: Any, request: Any) -> list[str]:
    """Validate PM findings before writing them to a durable Kanban comment."""
    card_errors = validate_card(card)
    if card_errors:
        return [f"INVALID_CARD:{error}" for error in card_errors]
    handoff_errors = validate_handoff(card, handoff)
    if handoff_errors:
        return [f"INVALID_HANDOFF:{error}" for error in handoff_errors]
    errors = _validate_review_run(review_run)
    if not isinstance(request, dict):
        return ["CHANGE_REQUEST_NOT_OBJECT"]
    _unexpected(
        request,
        {"schema", "source_handoff_id", "source_review_run_id", "findings"},
        errors,
        "UNEXPECTED_CHANGE_REQUEST_FIELD",
    )
    if request.get("schema") != "backend-implementation-change-request-v1":
        errors.append("CHANGE_REQUEST_SCHEMA")
    if request.get("source_handoff_id") != handoff.get("handoff_id"):
        errors.append("SOURCE_HANDOFF_MISMATCH")
    if request.get("source_review_run_id") != review_run.get("run_id"):
        errors.append("SOURCE_REVIEW_RUN_MISMATCH")
    findings = request.get("findings")
    if not isinstance(findings, list) or not findings:
        errors.append("MISSING_FINDINGS")
        return errors
    finding_ids: list[str] = []
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            errors.append(f"FINDING_NOT_OBJECT:{index}")
            continue
        _unexpected(
            finding,
            {"finding_id", "path", "symbol", "observed_problem", "expected_result", "allowed_scope", "verification"},
            errors,
            f"UNEXPECTED_FINDING_FIELD:{index}",
        )
        finding_id = finding.get("finding_id")
        if _non_empty(finding_id):
            finding_ids.append(finding_id)
        else:
            errors.append(f"MISSING_FINDING_ID:{index}")
        for field in ("path", "symbol", "observed_problem", "expected_result"):
            if not _non_empty(finding.get(field)):
                errors.append(f"MISSING_FINDING_{field.upper()}:{index}")
        finding_path = finding.get("path")
        if _non_empty(finding_path) and (
            not _is_literal_repository_path(finding_path) or _is_document_path(finding_path)
        ):
            errors.append(f"INVALID_FINDING_PATH:{index}:{finding_path}")
        for field in ("allowed_scope", "verification"):
            if not _strings(finding.get(field)):
                errors.append(f"MISSING_FINDING_{field.upper()}:{index}")
        expected_verification = {
            verification.get("id")
            for verification in card.get("verification", [])
            if isinstance(verification, dict) and _non_empty(verification.get("id"))
        } | {"full-backend"}
        if not _exact_ids(finding.get("verification"), expected_verification):
            errors.append(f"FINDING_VERIFICATION_MISMATCH:{index}")
    if len(finding_ids) != len(set(finding_ids)):
        errors.append("DUPLICATE_FINDING_ID")
    return errors


def validate_checkpoint(card: Any, handoff: Any, review_run: Any, checkpoint: Any) -> list[str]:
    """Validate checkpoint evidence after commit and Git read-back."""
    handoff_errors = validate_handoff(card, handoff)
    if handoff_errors:
        return [f"INVALID_HANDOFF:{error}" for error in handoff_errors]
    errors: list[str] = []
    review_run_errors = _validate_review_run(review_run)
    errors.extend(f"INVALID_REVIEW_RUN:{error}" for error in review_run_errors)
    if not isinstance(checkpoint, dict):
        return ["CHECKPOINT_NOT_OBJECT"]
    _unexpected(checkpoint, CHECKPOINT_FIELDS, errors, "UNEXPECTED_CHECKPOINT_FIELD")
    if checkpoint.get("schema") != "backend-implementation-checkpoint-v1":
        errors.append("CHECKPOINT_SCHEMA")
    if checkpoint.get("source_handoff_id") != handoff.get("handoff_id"):
        errors.append("SOURCE_HANDOFF_MISMATCH")
    if checkpoint.get("result") != "pass":
        errors.append("CHECKPOINT_NOT_PASS")
    committed_paths = checkpoint.get("committed_paths")
    changed_paths = handoff.get("changed_paths")
    if not _strings(changed_paths) or not _strings(committed_paths):
        errors.append("COMMITTED_PATHS_MISMATCH")
    elif not _exact_ids(committed_paths, set(changed_paths)):
        errors.append("COMMITTED_PATHS_MISMATCH")
    commit_sha = checkpoint.get("commit_sha")
    if not isinstance(commit_sha, str) or SHA.fullmatch(commit_sha) is None:
        errors.append("INVALID_COMMIT_SHA")
    if checkpoint.get("full_backend_verification_readback") != "pass":
        errors.append("FULL_BACKEND_READBACK_NOT_PASS")
    pm_handoff = dict(handoff)
    pm_handoff["focused_verification_results"] = checkpoint.get("focused_verification_readback")
    for error in validate_handoff(card, pm_handoff):
        errors.append(f"PM_FOCUSED_READBACK:{error}")
    if not _positive_int(checkpoint.get("verification_run_id")):
        errors.append("MISSING_PM_VERIFICATION_RUN_ID")
    elif isinstance(review_run, dict) and checkpoint.get("verification_run_id") != review_run.get("run_id"):
        errors.append("PM_VERIFICATION_RUN_MISMATCH")
    resolution = checkpoint.get("documentation_impact_resolution")
    impact = handoff.get("documentation_impact", {})
    if not isinstance(resolution, dict):
        errors.append("INVALID_DOCUMENTATION_IMPACT_RESOLUTION")
    else:
        _unexpected(
            resolution,
            {"status", "changed_paths", "commit_sha"},
            errors,
            "UNEXPECTED_DOCUMENTATION_RESOLUTION_FIELD",
        )
        expected_status = "resolved" if impact.get("detected") is True else "not-applicable"
        if resolution.get("status") != expected_status:
            errors.append("DOCUMENTATION_IMPACT_UNRESOLVED")
        docs_paths = resolution.get("changed_paths")
        if not _strings(docs_paths, allow_empty=expected_status == "not-applicable"):
            errors.append("INVALID_DOCUMENTATION_RESOLUTION_PATHS")
        elif any(not _is_literal_repository_path(path) or not _is_document_path(path) for path in docs_paths):
            errors.append("INVALID_DOCUMENTATION_RESOLUTION_PATHS")
        docs_sha = resolution.get("commit_sha")
        if expected_status == "resolved":
            if not isinstance(docs_sha, str) or SHA.fullmatch(docs_sha) is None:
                errors.append("INVALID_DOCUMENTATION_RESOLUTION_SHA")
            elif docs_sha == commit_sha:
                errors.append("DOCUMENTATION_COMMIT_NOT_DISTINCT")
        elif docs_sha is not None:
            errors.append("UNEXPECTED_DOCUMENTATION_RESOLUTION_SHA")
    if checkpoint.get("clean_worktree") is not True:
        errors.append("WORKTREE_NOT_CLEAN")
    return errors


def _read_json(path: Path) -> Any:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"DUPLICATE_JSON_KEY:{key}")
            value[key] = item
        return value

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    handoff_parser = subparsers.add_parser("handoff")
    handoff_parser.add_argument("card", type=Path)
    handoff_parser.add_argument("handoff", type=Path)
    change_parser = subparsers.add_parser("change-request")
    change_parser.add_argument("card", type=Path)
    change_parser.add_argument("handoff", type=Path)
    change_parser.add_argument("review_run", type=Path)
    change_parser.add_argument("request", type=Path)
    checkpoint_parser = subparsers.add_parser("checkpoint")
    checkpoint_parser.add_argument("card", type=Path)
    checkpoint_parser.add_argument("handoff", type=Path)
    checkpoint_parser.add_argument("review_run", type=Path)
    checkpoint_parser.add_argument("checkpoint", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "handoff":
            errors = validate_handoff(_read_json(args.card), _read_json(args.handoff))
        elif args.command == "change-request":
            errors = validate_change_request(
                _read_json(args.card),
                _read_json(args.handoff),
                _read_json(args.review_run),
                _read_json(args.request),
            )
        else:
            errors = validate_checkpoint(
                _read_json(args.card),
                _read_json(args.handoff),
                _read_json(args.review_run),
                _read_json(args.checkpoint),
            )
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(json.dumps({"valid": False, "errors": [f"READ_INPUT_FAILED:{exc}"]}, ensure_ascii=False))
        return 2
    print(json.dumps({"valid": not errors, "errors": errors}, ensure_ascii=False))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
