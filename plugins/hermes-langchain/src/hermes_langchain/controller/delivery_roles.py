"""Profile-independent Coder and Reviewer workflow roles."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Protocol

from hermes_langchain.authoring.graph import implementation_card

from .contracts import StepDefinition, Transition, WorkflowDefinition, _freeze
from .process_executor import validate_process_invocation_budget
from .runtime import AgentResult, StepInvocation
from .triage_parity import normalize_evidence_bundle


_SHA40 = re.compile(r"[0-9a-f]{40}")
_SHA64 = re.compile(r"[0-9a-f]{64}")
_AXES = (
    "spec",
    "maintainability",
    "persistence",
    "architecture",
    "evolution-compatibility",
    "security",
)
_DISPOSITIONS = (
    "correction-required",
    "context-required",
    "decision-required",
)
_REVIEWER_AGENT_GOAL = (
    "CONTROLLER_REVIEWER_ROLE_V1\n"
    "Independently review only the accepted plan, accepted diff, verification "
    "evidence and closed rubric supplied here. This role is read-only: do not edit "
    "source, tests, configuration, documentation or workflow state. Do not access "
    "Kanban, Profile, SOUL, memory, prior conversation or Coder reasoning. Return "
    "only one JSON object matching the supplied output schema. Do not invoke tools."
)


class RoleProducer(Protocol):
    @property
    def workspace(self) -> Path: ...

    def invoke(self, invocation: StepInvocation) -> AgentResult: ...


class DeliveryRoleExecutor:
    """Validate role admission and typed handoffs around isolated producers."""

    def __init__(
        self,
        *,
        coder_producer: RoleProducer,
        reviewer_producer: RoleProducer,
        verification_readback_fn: Callable[[Path, dict[str, Any]], dict[str, Any]],
        git_head_fn: Callable[[Path], str] | None = None,
        diff_snapshot_fn: Callable[[Path, str], dict[str, Any]] | None = None,
        repository_root_fn: Callable[[Path], Path | None] | None = None,
    ) -> None:
        self._coder_producer = coder_producer
        self._reviewer_producer = reviewer_producer
        self._coder_workspace = _producer_workspace(coder_producer, "CODER")
        self._reviewer_workspace = _producer_workspace(reviewer_producer, "REVIEWER")
        if self._coder_workspace == self._reviewer_workspace:
            raise ValueError("DELIVERY_ROLE_WORKSPACES_MUST_DIFFER")
        repository_root = (repository_root_fn or _repository_root)(self._reviewer_workspace)
        if repository_root is not None:
            raise ValueError("REVIEWER_WORKSPACE_MUST_BE_ISOLATED")
        if not callable(verification_readback_fn):
            raise ValueError("DELIVERY_VERIFICATION_PORT_REQUIRED")
        self._verification_readback_fn = verification_readback_fn
        self._git_head_fn = git_head_fn or _git_head
        self._diff_snapshot_fn = diff_snapshot_fn or _git_diff_snapshot

    def invoke(self, invocation: StepInvocation) -> AgentResult:
        if invocation.step_id == "coder_execute":
            return self._invoke_coder(invocation)
        if invocation.step_id == "reviewer_review":
            return self._invoke_reviewer(invocation)
        raise ValueError("DELIVERY_ROLE_STEP_UNKNOWN")

    def _invoke_coder(self, invocation: StepInvocation) -> AgentResult:
        admitted, workspace, baseline = _admit_coder(
            invocation.input,
            git_head_fn=self._git_head_fn,
        )
        if workspace != self._coder_workspace:
            raise ValueError("CODER_EXECUTOR_WORKSPACE_MISMATCH")
        canonical_admitted = _json_copy(admitted)
        produced = self._coder_producer.invoke(
            replace(
                invocation,
                goal=(
                    "CONTROLLER_CODER_ROLE_V1\n"
                    "Implement only the immutable implementation card in the pinned workspace. "
                    "Use only the bounded evidence and verification contract supplied here. "
                    "Do not access Kanban, Profile, SOUL, memory, prior conversation or raw Triage. "
                    "Do not commit, reset, stash or clean. Return only one JSON object matching the "
                    "supplied output schema after the declared Gradle MCP verification passes."
                ),
                input=_freeze(_json_copy(canonical_admitted)),
                output_schema=_coder_output_schema(),
            )
        )
        verification_readback = _json_copy(
            self._verification_readback_fn(
                workspace,
                _freeze(_json_copy(canonical_admitted["verification_contract"])),
            )
        )
        snapshot = self._diff_snapshot_fn(workspace, baseline)
        if not _coder_result_valid(
            produced.output,
            admitted=canonical_admitted,
            snapshot=snapshot,
            verification_readback=verification_readback,
        ):
            raise ValueError("CODER_ROLE_RESULT_INVALID")
        _validate_reviewer_invocation_budget(
            run_id=invocation.run_id,
            accepted_plan=canonical_admitted["implementation_card"],
            coder_result=produced.output,
        )
        return produced

    def _invoke_reviewer(self, invocation: StepInvocation) -> AgentResult:
        admitted = _admit_reviewer(invocation.input)
        canonical_admitted = _json_copy(admitted)
        produced = self._reviewer_producer.invoke(
            replace(
                invocation,
                goal=_REVIEWER_AGENT_GOAL,
                input=_freeze(_json_copy(canonical_admitted)),
                output_schema=_review_output_schema(),
            )
        )
        if not _review_result_valid(produced.output, admitted=canonical_admitted):
            raise ValueError("REVIEWER_ROLE_RESULT_INVALID")
        return produced


def delivery_roles_workflow() -> WorkflowDefinition:
    """Build the canonical Coder → Reviewer role workflow."""

    coder = StepDefinition(
        step_id="coder_execute",
        goal="Execute one admitted implementation card as the profile-independent Coder role.",
        allowed_toolsets=("file", "gradle-mcp", "codebase-memory-mcp", "semble-mcp"),
        acceptance_criteria=(
            "The immutable card and pinned workspace admission are valid.",
            "Changed paths, diff digest and all declared verification evidence are exact read-back.",
        ),
        output_schema=_coder_output_schema(),
        projector=_coder_projector,
        validator=lambda value: _coder_result_shape(value),
    )
    reviewer = StepDefinition(
        step_id="reviewer_review",
        goal="Review accepted implementation evidence as the profile-independent Reviewer role.",
        allowed_toolsets=(),
        tool_free=True,
        acceptance_criteria=(
            "The accepted plan, diff, verification evidence and rubric are the only inputs.",
            "The typed read-only review result is coherent with the accepted evidence.",
        ),
        output_schema=_review_output_schema(),
        projector=_reviewer_projector,
        validator=lambda value: _review_result_shape(value),
    )
    return WorkflowDefinition(
        steps=(coder, reviewer),
        transitions=(Transition("coder_execute", "reviewer_review"),),
        initial_step_id="coder_execute",
    )


def _coder_projector(initial: dict[str, Any], _artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "implementation_card": dict(initial["implementation_card"]),
        "evidence": dict(initial["evidence"]),
        "workspace": dict(initial["workspace"]),
        "verification_contract": dict(initial["verification_contract"]),
    }


def _reviewer_projector(
    initial: dict[str, Any],
    artifacts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    coder = artifacts["coder_execute"]["payload"]
    return {
        "accepted_plan": dict(initial["implementation_card"]),
        "diff": dict(coder["diff"]),
        "verification_evidence": dict(coder["verification_evidence"]),
        "rubric": dict(initial["rubric"]),
    }


def _validate_reviewer_invocation_budget(
    *,
    run_id: str,
    accepted_plan: dict[str, Any],
    coder_result: dict[str, Any],
) -> None:
    reviewer_step = delivery_roles_workflow().steps[1]
    validate_process_invocation_budget(
        StepInvocation(
            run_id=run_id,
            invocation_id=f"{run_id}:reviewer_review:1",
            step_id="reviewer_review",
            goal=_REVIEWER_AGENT_GOAL,
            allowed_toolsets=reviewer_step.allowed_toolsets,
            acceptance_criteria=reviewer_step.acceptance_criteria,
            output_schema=dict(reviewer_step.output_schema),
            input=_freeze(
                {
                    "accepted_plan": _json_copy(accepted_plan),
                    "diff": _json_copy(coder_result["diff"]),
                    "verification_evidence": _json_copy(
                        coder_result["verification_evidence"]
                    ),
                    "rubric": _review_rubric(),
                }
            ),
        )
    )


def _admit_coder(
    value: Any,
    *,
    git_head_fn: Callable[[Path], str],
) -> tuple[dict[str, Any], Path, str]:
    expected = {"implementation_card", "evidence", "workspace", "verification_contract"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("CODER_ROLE_INPUT_FIELDS")
    card = _json_copy(value["implementation_card"])
    errors = implementation_card.validate(card)
    if errors:
        raise ValueError(f"CODER_CARD_INVALID:{errors[0]}")
    workspace_value = value["workspace"]
    if not isinstance(workspace_value, dict) or set(workspace_value) != {
        "schema",
        "path",
        "baseline_head_sha",
    }:
        raise ValueError("CODER_WORKSPACE_FIELDS")
    if workspace_value.get("schema") != "controller-workspace-v1":
        raise ValueError("CODER_WORKSPACE_SCHEMA")
    raw_path = workspace_value.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("CODER_WORKSPACE_PATH_INVALID")
    workspace = Path(raw_path.strip())
    if not workspace.is_absolute() or not workspace.resolve().is_dir():
        raise ValueError("CODER_WORKSPACE_PATH_INVALID")
    workspace = workspace.resolve()
    repository_root = _repository_root(workspace)
    if repository_root is not None and repository_root != workspace:
        raise ValueError("CODER_WORKSPACE_MUST_BE_REPOSITORY_ROOT")
    baseline = str(workspace_value.get("baseline_head_sha") or "").lower()
    if not _SHA40.fullmatch(baseline):
        raise ValueError("CODER_BASELINE_INVALID")
    actual_head = str(git_head_fn(workspace) or "").strip().lower()
    if actual_head != baseline:
        raise ValueError("CODER_BASELINE_MISMATCH")
    evidence = normalize_evidence_bundle(
        _json_copy(value["evidence"]),
        workspace=workspace,
        git_head_fn=git_head_fn,
    )
    verification = _validate_verification_contract(value["verification_contract"], card)
    return (
        {
            "implementation_card": card,
            "evidence": evidence,
            "workspace": {
                "schema": "controller-workspace-v1",
                "path": workspace.as_posix(),
                "baseline_head_sha": baseline,
            },
            "verification_contract": verification,
        },
        workspace,
        baseline,
    )


def _validate_verification_contract(value: Any, card: dict[str, Any]) -> dict[str, Any]:
    value = _json_copy(value)
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "runner",
        "focused",
        "full_backend",
        "quality_tasks",
    }:
        raise ValueError("CODER_VERIFICATION_FIELDS")
    if value.get("schema") != "controller-verification-contract-v1" or value.get("runner") != "gradle-mcp":
        raise ValueError("CODER_VERIFICATION_SCHEMA")
    focused = value.get("focused")
    expected = {
        item["id"]: (item["level"], item["selectors"])
        for item in card["verification"]
    }
    if not isinstance(focused, list) or len(focused) != len(expected):
        raise ValueError("CODER_VERIFICATION_COVERAGE")
    actual: dict[str, tuple[Any, Any]] = {}
    for row in focused:
        if not isinstance(row, dict) or set(row) != {"verification_id", "selectors", "level"}:
            raise ValueError("CODER_VERIFICATION_ROW")
        verification_id = row.get("verification_id")
        if verification_id in actual:
            raise ValueError("CODER_VERIFICATION_DUPLICATE")
        actual[verification_id] = (row.get("level"), row.get("selectors"))
    if actual != expected:
        raise ValueError("CODER_VERIFICATION_COVERAGE")
    if value.get("full_backend") != {"task": "test"}:
        raise ValueError("CODER_FULL_BACKEND_CONTRACT")
    quality_tasks = value.get("quality_tasks")
    if quality_tasks != ["checkstyleMain", "checkstyleTest"]:
        raise ValueError("CODER_QUALITY_CONTRACT")
    return _json_copy(value)


def _admit_reviewer(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "accepted_plan",
        "diff",
        "verification_evidence",
        "rubric",
    }:
        raise ValueError("REVIEWER_ROLE_INPUT_FIELDS")
    plan = _json_copy(value["accepted_plan"])
    if implementation_card.validate(plan):
        raise ValueError("REVIEWER_PLAN_INVALID")
    diff = _json_copy(value["diff"])
    verification = _json_copy(value["verification_evidence"])
    if not _diff_shape(diff) or not _verification_evidence_shape(verification):
        raise ValueError("REVIEWER_ACCEPTED_EVIDENCE_INVALID")
    rubric = _json_copy(value["rubric"])
    if rubric != _review_rubric():
        raise ValueError("REVIEWER_RUBRIC_INVALID")
    return {
        "accepted_plan": plan,
        "diff": diff,
        "verification_evidence": verification,
        "rubric": rubric,
    }


def _review_rubric() -> dict[str, Any]:
    return {
        "schema": "controller-review-rubric-v1",
        "axes": list(_AXES),
        "finding_dispositions": list(_DISPOSITIONS),
    }


def _coder_result_valid(
    value: Any,
    *,
    admitted: dict[str, Any],
    snapshot: dict[str, Any],
    verification_readback: dict[str, Any],
) -> bool:
    if not _coder_result_shape(value):
        return False
    diff = value["diff"]
    if diff.get("baseline_head_sha") != admitted["workspace"]["baseline_head_sha"]:
        return False
    if diff.get("result_head_sha") != admitted["workspace"]["baseline_head_sha"]:
        return False
    if diff != snapshot:
        return False
    if (
        not _verification_evidence_shape(verification_readback)
        or value["verification_evidence"] != verification_readback
    ):
        return False
    contract = admitted["verification_contract"]
    checks = verification_readback["checks"]
    expected = {
        row["verification_id"]: (row["selectors"], row["level"])
        for row in contract["focused"]
    }
    verification_ids = [row["verification_id"] for row in checks]
    if len(checks) != len(expected) or len(verification_ids) != len(set(verification_ids)):
        return False
    actual = {
        row["verification_id"]: (row["selectors"], row["level"])
        for row in checks
        if isinstance(row, dict)
    }
    if actual != expected or any(row.get("runner") != "gradle-mcp" or row.get("result") != "passed" for row in checks):
        return False
    if verification_readback["full_backend"] != {
        "runner": "gradle-mcp",
        "task": "test",
        "result": "passed",
    }:
        return False
    quality = verification_readback["quality"]
    return quality == [
        {"runner": "gradle-mcp", "task": task, "result": "passed"}
        for task in contract["quality_tasks"]
    ]


def _review_result_valid(value: Any, *, admitted: dict[str, Any]) -> bool:
    if not _review_result_shape(value):
        return False
    diff = admitted["diff"]
    return (
        value["baseline_head_sha"] == diff["baseline_head_sha"]
        and value["reviewed_head_sha"] == diff["result_head_sha"]
        and value["accepted_plan_sha256"] == _json_digest(admitted["accepted_plan"])
        and value["diff_sha256"] == diff["sha256"]
        and value["verification_evidence_sha256"]
        == _json_digest(admitted["verification_evidence"])
        and value["rubric_axes"] == admitted["rubric"]["axes"]
        and _review_findings_valid(value["findings"], diff)
        and (
            (value["result"] == "approved" and not value["findings"] and not value["unverified"])
            or (value["result"] == "changes-required" and bool(value["findings"]))
        )
    )


def _coder_result_shape(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value)
        == {
            "schema",
            "status",
            "diff",
            "verification_evidence",
            "documentation_impact",
            "residual_risks",
        }
        and value.get("schema") == "controller-coder-result-v1"
        and value.get("status") == "completed"
        and _diff_shape(value.get("diff"))
        and _verification_evidence_shape(value.get("verification_evidence"))
        and _string_list(value.get("documentation_impact"))
        and _string_list(value.get("residual_risks"))
    )


def _diff_shape(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "baseline_head_sha",
        "result_head_sha",
        "changed_paths",
        "unified_diff",
        "sha256",
    }:
        return False
    paths = value.get("changed_paths")
    return (
        isinstance(value.get("baseline_head_sha"), str)
        and bool(_SHA40.fullmatch(value["baseline_head_sha"]))
        and isinstance(value.get("result_head_sha"), str)
        and bool(_SHA40.fullmatch(value["result_head_sha"]))
        and isinstance(paths, list)
        and bool(paths)
        and len(paths) <= 64
        and len(paths) == len(set(paths))
        and all(_safe_relative_path(path) for path in paths)
        and isinstance(value.get("unified_diff"), str)
        and bool(value["unified_diff"])
        and len(value["unified_diff"].encode("utf-8")) <= 262_144
        and isinstance(value.get("sha256"), str)
        and bool(_SHA64.fullmatch(value["sha256"]))
        and hashlib.sha256(value["unified_diff"].encode("utf-8")).hexdigest()
        == value["sha256"]
    )


def _verification_evidence_shape(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"checks", "full_backend", "quality"}:
        return False
    checks = value.get("checks")
    quality = value.get("quality")
    if not isinstance(checks, list) or not checks or not isinstance(quality, list):
        return False
    return all(
        isinstance(row, dict)
        and set(row) == {"verification_id", "runner", "selectors", "level", "result"}
        and isinstance(row.get("verification_id"), str)
        and bool(row["verification_id"])
        and row.get("runner") == "gradle-mcp"
        and isinstance(row.get("selectors"), list)
        and bool(row["selectors"])
        and all(isinstance(selector, str) and bool(selector) for selector in row["selectors"])
        and isinstance(row.get("level"), str)
        and row.get("result") == "passed"
        for row in checks
    ) and all(
        isinstance(row, dict)
        and set(row) == {"runner", "task", "result"}
        and row.get("runner") == "gradle-mcp"
        and isinstance(row.get("task"), str)
        and bool(row["task"])
        and row.get("result") == "passed"
        for row in quality
    ) and isinstance(value.get("full_backend"), dict)


def _review_result_shape(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "result",
        "baseline_head_sha",
        "reviewed_head_sha",
        "accepted_plan_sha256",
        "diff_sha256",
        "verification_evidence_sha256",
        "rubric_axes",
        "findings",
        "unverified",
    }:
        return False
    findings = value.get("findings")
    finding_ids = [finding.get("finding_id") for finding in findings if isinstance(finding, dict)] if isinstance(findings, list) else []
    return (
        value.get("schema") == "controller-review-result-v1"
        and value.get("result") in {"approved", "changes-required"}
        and isinstance(value.get("baseline_head_sha"), str)
        and bool(_SHA40.fullmatch(value["baseline_head_sha"]))
        and isinstance(value.get("reviewed_head_sha"), str)
        and bool(_SHA40.fullmatch(value["reviewed_head_sha"]))
        and isinstance(value.get("accepted_plan_sha256"), str)
        and bool(_SHA64.fullmatch(value["accepted_plan_sha256"]))
        and isinstance(value.get("diff_sha256"), str)
        and bool(_SHA64.fullmatch(value["diff_sha256"]))
        and isinstance(value.get("verification_evidence_sha256"), str)
        and bool(_SHA64.fullmatch(value["verification_evidence_sha256"]))
        and value.get("rubric_axes") == list(_AXES)
        and isinstance(findings, list)
        and all(_finding_shape(finding) for finding in findings)
        and len(finding_ids) == len(set(finding_ids))
        and _string_list(value.get("unverified"))
    )


def _review_findings_valid(findings: list[dict[str, Any]], diff: dict[str, Any]) -> bool:
    changed_paths = set(diff["changed_paths"])
    line_ranges = _diff_new_line_ranges(diff["unified_diff"])
    return all(
        finding["path"] in changed_paths
        and any(start <= finding["line"] <= end for start, end in line_ranges.get(finding["path"], ()))
        for finding in findings
    )


def _diff_new_line_ranges(unified_diff: str) -> dict[str, tuple[tuple[int, int], ...]]:
    ranges: dict[str, list[tuple[int, int]]] = {}
    old_path: str | None = None
    new_path: str | None = None
    old_remaining = 0
    new_remaining = 0
    in_hunk = False
    for line in unified_diff.splitlines():
        if in_hunk:
            if line.startswith("\\ No newline at end of file"):
                continue
            prefix = line[:1]
            if prefix == " ":
                old_remaining -= 1
                new_remaining -= 1
            elif prefix == "-":
                old_remaining -= 1
            elif prefix == "+":
                new_remaining -= 1
            if old_remaining <= 0 and new_remaining <= 0:
                in_hunk = False
            continue
        if line.startswith("--- "):
            old_path = _diff_marker_path(line[4:], prefix="a/")
            continue
        if line.startswith("+++ "):
            new_path = _diff_marker_path(line[4:], prefix="b/")
            continue
        if not line.startswith("@@"):
            continue
        match = re.match(
            r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@",
            line,
        )
        if match is None:
            continue
        old_start = int(match.group(1))
        old_remaining = int(match.group(2) or "1")
        new_start = int(match.group(3))
        new_remaining = int(match.group(4) or "1")
        finding_path = new_path if new_path is not None else old_path
        finding_start = new_start if new_path is not None else old_start
        finding_count = new_remaining if new_path is not None else old_remaining
        if finding_path is not None and finding_count > 0:
            ranges.setdefault(finding_path, []).append(
                (finding_start, finding_start + finding_count - 1)
            )
        in_hunk = old_remaining > 0 or new_remaining > 0
    return {path: tuple(values) for path, values in ranges.items()}


def _diff_marker_path(marker: str, *, prefix: str) -> str | None:
    path = marker.split("\t", 1)[0]
    if path == "/dev/null":
        return None
    return path[len(prefix) :] if path.startswith(prefix) else path


def _finding_shape(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"finding_id", "axis", "disposition", "path", "line", "observed", "impact"}
        and isinstance(value.get("finding_id"), str)
        and bool(value["finding_id"])
        and value.get("axis") in _AXES
        and value.get("disposition") in _DISPOSITIONS
        and _safe_relative_path(value.get("path"))
        and type(value.get("line")) is int
        and value["line"] > 0
        and isinstance(value.get("observed"), str)
        and bool(value["observed"].strip())
        and isinstance(value.get("impact"), str)
        and bool(value["impact"].strip())
    )


def _coder_output_schema() -> dict[str, Any]:
    check_schema = _closed_object_schema(
        {
            "verification_id": {"type": "string", "minLength": 1},
            "runner": {"type": "string", "const": "gradle-mcp"},
            "selectors": _string_array_schema(min_items=1),
            "level": {"type": "string", "minLength": 1},
            "result": {"type": "string", "const": "passed"},
        }
    )
    task_result_schema = _closed_object_schema(
        {
            "runner": {"type": "string", "const": "gradle-mcp"},
            "task": {"type": "string", "minLength": 1},
            "result": {"type": "string", "const": "passed"},
        }
    )
    return _closed_object_schema(
        {
            "schema": {"type": "string", "const": "controller-coder-result-v1"},
            "status": {"type": "string", "const": "completed"},
            "diff": _closed_object_schema(
                {
                    "baseline_head_sha": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
                    "result_head_sha": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
                    "changed_paths": _string_array_schema(min_items=1, max_items=64),
                    "unified_diff": {"type": "string", "minLength": 1, "maxLength": 262144},
                    "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                }
            ),
            "verification_evidence": _closed_object_schema(
                {
                    "checks": {
                        "type": "array",
                        "minItems": 1,
                        "items": check_schema,
                    },
                    "full_backend": task_result_schema,
                    "quality": {"type": "array", "items": task_result_schema},
                }
            ),
            "documentation_impact": _string_array_schema(),
            "residual_risks": _string_array_schema(),
        }
    )


def _review_output_schema() -> dict[str, Any]:
    finding_schema = _closed_object_schema(
        {
            "finding_id": {"type": "string", "minLength": 1},
            "axis": {"type": "string", "enum": list(_AXES)},
            "disposition": {"type": "string", "enum": list(_DISPOSITIONS)},
            "path": {"type": "string", "minLength": 1},
            "line": {"type": "integer", "minimum": 1},
            "observed": {"type": "string", "minLength": 1},
            "impact": {"type": "string", "minLength": 1},
        }
    )
    return _closed_object_schema(
        {
            "schema": {"type": "string", "const": "controller-review-result-v1"},
            "result": {"type": "string", "enum": ["approved", "changes-required"]},
            "baseline_head_sha": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
            "reviewed_head_sha": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
            "accepted_plan_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "diff_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "verification_evidence_sha256": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
            },
            "rubric_axes": {
                "type": "array",
                "items": {"type": "string", "enum": list(_AXES)},
                "minItems": len(_AXES),
                "maxItems": len(_AXES),
                "uniqueItems": True,
            },
            "findings": {"type": "array", "items": finding_schema},
            "unverified": _string_array_schema(),
        }
    )


def _closed_object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _string_array_schema(*, min_items: int = 0, max_items: int = 32) -> dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "minItems": min_items,
        "maxItems": max_items,
        "uniqueItems": True,
    }


def _producer_workspace(producer: RoleProducer, role: str) -> Path:
    raw_workspace = getattr(producer, "workspace", None)
    if raw_workspace is None:
        raise ValueError(f"{role}_PRODUCER_WORKSPACE_REQUIRED")
    workspace = Path(raw_workspace)
    if not workspace.is_absolute() or not workspace.resolve().is_dir():
        raise ValueError(f"{role}_PRODUCER_WORKSPACE_INVALID")
    return workspace.resolve()


def _repository_root(workspace: Path) -> Path | None:
    completed = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return None
    root = completed.stdout.strip()
    return Path(root).resolve() if root else None


def _git_head(workspace: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _git_diff_snapshot(workspace: Path, baseline: str) -> dict[str, Any]:
    head = _git_head(workspace).lower()
    paths_result = subprocess.run(
        ["git", "-C", str(workspace), "diff", "--name-only", baseline],
        check=True,
        capture_output=True,
        text=True,
    )
    diff_result = subprocess.run(
        ["git", "-C", str(workspace), "diff", "--binary", baseline],
        check=True,
        capture_output=True,
    )
    untracked_result = subprocess.run(
        ["git", "-C", str(workspace), "ls-files", "--others", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
    )
    tracked_paths = [line.replace("\\", "/") for line in paths_result.stdout.splitlines() if line]
    untracked_paths = [
        raw.decode("utf-8").replace("\\", "/")
        for raw in untracked_result.stdout.split(b"\0")
        if raw
    ]
    diff_bytes = diff_result.stdout
    for relative_path in sorted(untracked_paths):
        untracked_diff = subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "diff",
                "--no-index",
                "--binary",
                "--",
                os.devnull,
                relative_path,
            ],
            check=False,
            capture_output=True,
        )
        if untracked_diff.returncode not in {0, 1}:
            raise subprocess.CalledProcessError(
                untracked_diff.returncode,
                untracked_diff.args,
                output=untracked_diff.stdout,
                stderr=untracked_diff.stderr,
            )
        diff_bytes += b"\n" + untracked_diff.stdout
    paths = sorted(dict.fromkeys([*tracked_paths, *untracked_paths]))
    unified_diff = diff_bytes.decode("utf-8")
    return {
        "baseline_head_sha": baseline,
        "result_head_sha": head,
        "changed_paths": paths,
        "unified_diff": unified_diff,
        "sha256": hashlib.sha256(diff_bytes).hexdigest(),
    }


def _json_digest(value: Any) -> str:
    data = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _string_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= 32
        and len(value) == len(set(value))
        and all(isinstance(item, str) and bool(item.strip()) for item in value)
    )


def _safe_relative_path(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and path.as_posix() == value
