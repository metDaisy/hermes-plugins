"""Graph wrapper metadata, mode lineage, and source-review validation."""
from __future__ import annotations
from collections import Counter
from dataclasses import dataclass
import importlib.util
import os
from pathlib import Path
import re
from typing import Any
from .contract_common import is_canonical_https_url

@dataclass(frozen=True)
class GraphMetadataValidation:
    errors: tuple[str, ...]
    mode: Any
    issue_number: Any
    issue_url: Any
    generation: Any
    workspace: Any
    planning_baseline_sha: Any
    triage_digest: Any

@dataclass(frozen=True)
class GraphMetadataFacts:
    mode: Any
    issue_number: Any
    issue_url: Any
    generation: Any
    workspace: Any
    planning_baseline_sha: Any
    triage_digest: Any

def _validate_aggregate_review_result(result: Any, graph: Any) -> list[str]:
    homes: list[Path] = []
    configured = os.environ.get("HERMES_HOME")
    if configured:
        homes.append(Path(configured))
    try:
        from hermes_constants import get_hermes_home
        homes.append(Path(get_hermes_home()))
    except (ImportError, OSError):
        pass
    workflow_path = next((home.resolve() / "skills" / "run-workflow" / "scripts" / "workflow.py" for home in homes if (home.resolve() / "skills" / "run-workflow" / "scripts" / "workflow.py").is_file()), None)
    if workflow_path is None:
        return ["WORKFLOW_VALIDATOR_UNAVAILABLE"]
    spec = importlib.util.spec_from_file_location("pm_workflow_contract", workflow_path)
    if spec is None or spec.loader is None:
        return ["WORKFLOW_VALIDATOR_UNAVAILABLE"]
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    expected = result.get("prior_findings") if isinstance(result, dict) else None
    return module.validate_review_result(result, graph, expected)

GRAPH_SCHEMA = "build-task-graph-v6"
SHA = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")

def _non_empty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())

def _strings(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(_non_empty(item) for item in value)

def _error(errors: list[str], condition: bool, code: str) -> None:
    if not condition:
        errors.append(code)

def _exact_fields(value: dict[str, Any], allowed: set[str], errors: list[str], prefix: str) -> None:
    for field in sorted(set(value) - allowed):
        errors.append(f"{prefix}:{field}")

def _validate_locator(locator: Any, errors: list[str], prefix: str) -> None:
    if not isinstance(locator, dict):
        errors.append(f"{prefix}_NOT_OBJECT")
        return
    _exact_fields(locator, {"path", "heading", "revision"}, errors, f"UNEXPECTED_{prefix}_FIELD")
    _error(errors, _non_empty(locator.get("path")), f"MISSING_{prefix}_PATH")
    _error(errors, _non_empty(locator.get("heading")), f"MISSING_{prefix}_HEADING")
    _error(errors, isinstance(locator.get("revision"), str) and bool(SHA.fullmatch(locator["revision"])), f"INVALID_{prefix}_REVISION")

def _validate_base_metadata(graph: dict[str, Any], errors: list[str]) -> GraphMetadataFacts:
    allowed = {"schema", "mode", "issue", "generation", "workspace", "planning_baseline_sha", "triage_digest", "requirement_basis", "revised_requirement", "lineage", "source_review", "change_requirements", "behaviors", "cards", "execution_order", "ready_candidate", "archived_card_keys"}
    _exact_fields(graph, allowed, errors, "UNEXPECTED_GRAPH_FIELD")
    _error(errors, graph.get("schema") == GRAPH_SCHEMA, "GRAPH_SCHEMA")
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
    return GraphMetadataFacts(mode, issue_number, issue_url, generation, workspace, planning_baseline_sha, triage_digest)

def _validate_lineage(lineage: Any, generation: Any, errors: list[str], *, review_rework: bool) -> None:
    missing = "MISSING_REVIEW_REWORK_LINEAGE" if review_rework else "MISSING_REWORK_LINEAGE"
    if not isinstance(lineage, dict):
        errors.append(missing)
        return
    _exact_fields(lineage, {"prior_generation", "prior_summary", "archived_unfinished_keys"}, errors, "UNEXPECTED_LINEAGE_FIELD")
    if review_rework:
        _error(errors, lineage.get("prior_generation") == generation, "REVIEW_REWORK_GENERATION_MISMATCH")
    else:
        _error(errors, isinstance(generation, int) and lineage.get("prior_generation") == generation - 1, "REWORK_GENERATION_DISCONTINUITY")
    _error(errors, _non_empty(lineage.get("prior_summary")), "MISSING_PRIOR_SUMMARY")
    if not review_rework:
        _error(errors, isinstance(lineage.get("archived_unfinished_keys"), list), "MISSING_ARCHIVED_LINEAGE")

def _validate_source_dispositions(dispositions: Any, errors: list[str]) -> None:
    _error(errors, isinstance(dispositions, list) and bool(dispositions), "MISSING_SOURCE_DISPOSITIONS")
    finding_ids: set[str] = set()
    if not isinstance(dispositions, list):
        return
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

def _validate_source_review(source_review: Any, errors: list[str]) -> None:
    if not isinstance(source_review, dict):
        errors.append("MISSING_SOURCE_REVIEW")
        return
    _exact_fields(source_review, {"review_key", "dispositions", "idempotency"}, errors, "UNEXPECTED_SOURCE_REVIEW_FIELD")
    _error(errors, _non_empty(source_review.get("review_key")), "MISSING_SOURCE_REVIEW_KEY")
    _validate_source_dispositions(source_review.get("dispositions"), errors)
    idempotency = source_review.get("idempotency")
    _error(errors, isinstance(idempotency, dict) and bool(idempotency), "MISSING_IDEMPOTENCY_MAP")
    if isinstance(idempotency, dict):
        _error(errors, all(_non_empty(key) and _non_empty(value) for key, value in idempotency.items()), "INVALID_IDEMPOTENCY_MAP")
        _error(errors, len(set(idempotency.values())) == len(idempotency), "DUPLICATE_IDEMPOTENCY_KEY")

def _validate_canonical_source_review(source_review: Any, source_review_result: Any, graph: dict[str, Any], errors: list[str]) -> None:
    if not isinstance(source_review_result, dict):
        errors.append("MISSING_CANONICAL_SOURCE_REVIEW_RESULT")
        return
    canonical_errors = _validate_aggregate_review_result(source_review_result, graph)
    errors.extend(f"INVALID_CANONICAL_SOURCE_REVIEW_RESULT:{error}" for error in canonical_errors)
    _error(errors, source_review_result.get("result") == "changes-required", "SOURCE_REVIEW_RESULT_NOT_CHANGES_REQUIRED")
    _error(errors, isinstance(source_review, dict) and source_review.get("review_key") == source_review_result.get("review_card_key"), "SOURCE_REVIEW_KEY_MISMATCH")
    canonical_dispositions = [(finding.get("finding_id"), finding.get("verdict")) for finding in source_review_result.get("findings", []) if isinstance(finding, dict) and finding.get("verdict") in {"correction-required", "context-required", "decision-required"}]
    graph_dispositions = [(disposition.get("finding_id"), disposition.get("verdict")) for disposition in (source_review.get("dispositions", []) if isinstance(source_review, dict) else []) if isinstance(disposition, dict)]
    _error(errors, Counter(graph_dispositions) == Counter(canonical_dispositions), "SOURCE_REVIEW_DISPOSITION_MISMATCH")

def _validate_mode_metadata(graph: dict[str, Any], facts: GraphMetadataFacts, source_review_result: Any, errors: list[str]) -> None:
    if facts.mode == "new":
        _error(errors, facts.generation == 1, "NEW_REQUIRES_GENERATION_1")
        _error(errors, graph.get("revised_requirement") is None, "NEW_FORBIDS_REVISED_REQUIREMENT")
        _error(errors, graph.get("lineage") is None, "NEW_FORBIDS_LINEAGE")
        _error(errors, graph.get("source_review") is None, "NEW_FORBIDS_SOURCE_REVIEW")
    elif facts.mode == "requirement-rework":
        _error(errors, isinstance(facts.generation, int) and facts.generation > 1, "REWORK_REQUIRES_NEXT_GENERATION")
        _validate_locator(graph.get("revised_requirement"), errors, "REVISED_REQUIREMENT")
        _validate_lineage(graph.get("lineage"), facts.generation, errors, review_rework=False)
        _error(errors, graph.get("source_review") is None, "REQUIREMENT_REWORK_FORBIDS_SOURCE_REVIEW")
    else:
        _error(errors, graph.get("revised_requirement") is None, "REVIEW_REWORK_FORBIDS_REQUIREMENT_REVISION")
        _validate_lineage(graph.get("lineage"), facts.generation, errors, review_rework=True)
        source_review = graph.get("source_review")
        _validate_source_review(source_review, errors)
        _validate_canonical_source_review(source_review, source_review_result, graph, errors)

def validate_graph_metadata(graph: dict[str, Any], *, source_review_result: Any = None) -> GraphMetadataValidation:
    errors: list[str] = []
    facts = _validate_base_metadata(graph, errors)
    _validate_mode_metadata(graph, facts, source_review_result, errors)
    _validate_locator(graph.get("requirement_basis"), errors, "REQUIREMENT_BASIS")
    return GraphMetadataValidation(errors=tuple(errors), mode=facts.mode, issue_number=facts.issue_number, issue_url=facts.issue_url, generation=facts.generation, workspace=facts.workspace, planning_baseline_sha=facts.planning_baseline_sha, triage_digest=facts.triage_digest)
