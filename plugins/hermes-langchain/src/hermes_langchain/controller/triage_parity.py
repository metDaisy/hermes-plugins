"""Canonical five-Step Triage parity workflow and bounded evidence contract."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Protocol

from ..authoring import triage_activity, triage_workflow
from ..paths import PLUGIN_ROOT
from .contracts import StepDefinition, Transition, WorkflowDefinition
from .runtime import AgentResult, StepInvocation


_STAGE_IDS = (
    "triage_intake",
    "triage_discovery",
    "triage_source_confirmation",
    "triage_analysis",
    "triage_materialize",
)
_STAGE_NAMES = {
    "triage_intake": "intake",
    "triage_discovery": "discovery",
    "triage_source_confirmation": "source_confirmation",
    "triage_analysis": "analysis",
    "triage_materialize": "materialize",
}
_GIT_HEAD = re.compile(r"^[0-9a-fA-F]{40,64}$")
_EVIDENCE_LIMITS = {
    "codebase_memory": 8,
    "semble": 8,
    "direct_source": 16,
}
_BUNDLE_FIELDS = {
    "schema",
    "workspace_path",
    "git_head",
    "codebase_memory",
    "semble",
    "direct_source",
}
_ASSIGNMENT_FIELDS = (
    "schema",
    "assignment_id",
    "board",
    "issue_number",
    "generation",
    "workspace_path",
    "phase",
    "profile",
)
_AUTHORING_STAGES = frozenset(_STAGE_IDS[:-1])
_RAW_SCHEMAS = {
    "triage_intake": ("kwc-triage-intake-authoring-v1", ("schema", "identity", "intake")),
    "triage_discovery": (
        "kwc-triage-discovery-v2",
        ("schema", "targets", "tool_usage", "fallbacks", "codebase_memory"),
    ),
    "triage_source_confirmation": (
        "kwc-triage-source-confirmation-v2",
        ("schema", "targets", "open_questions"),
    ),
    "triage_analysis": ("kwc-triage-analysis-authoring-v1", ("schema", "analysis")),
}
_PROMPTS = {
    "triage_intake": "triage-intake.md",
    "triage_discovery": "triage-discovery.md",
    "triage_source_confirmation": "triage-source-confirmation.md",
    "triage_analysis": "triage-analysis.md",
}


class AgentProducer(Protocol):
    def invoke(self, invocation: StepInvocation) -> AgentResult: ...


class TriageParityExecutor:
    """Adapt controller invocations to the existing Triage validators and freezer."""

    def __init__(
        self,
        *,
        producer: AgentProducer,
        inspection_fn: Callable[..., dict[str, Any]] | None = None,
        git_head_fn: Callable[[Path], str] | None = None,
        kanban_port: Any | None = None,
    ) -> None:
        if kanban_port is None:
            raise ValueError("TRIAGE_KANBAN_PORT_REQUIRED")
        self._producer = producer
        self._inspection_fn = inspection_fn
        self._git_head_fn = git_head_fn
        self._kanban_port = kanban_port

    def invoke(self, invocation: StepInvocation) -> AgentResult:
        self._validated_evidence(invocation)
        if invocation.step_id in _AUTHORING_STAGES:
            return self._author_stage(invocation)
        if invocation.step_id == "triage_materialize":
            return self._materialize(invocation)
        raise ValueError("TRIAGE_STEP_UNKNOWN")

    def _validated_evidence(self, invocation: StepInvocation) -> dict[str, Any]:
        workspace = self._workspace(invocation)
        evidence = invocation.input.get("evidence")
        try:
            json_value = json.loads(json.dumps(evidence, ensure_ascii=False))
        except (TypeError, ValueError) as exc:
            raise ValueError("EVIDENCE_BUNDLE_INVALID") from exc
        return normalize_evidence_bundle(
            json_value,
            workspace=workspace,
            git_head_fn=self._git_head_fn or triage_workflow._git_head,
        )

    def _author_stage(self, invocation: StepInvocation) -> AgentResult:
        stage = _STAGE_NAMES[invocation.step_id]
        workspace = self._workspace(invocation)
        raw_path = self._run_dir(workspace, invocation.run_id) / f"{stage}-authoring.json"
        prompt = (PLUGIN_ROOT / "prompts" / _PROMPTS[invocation.step_id]).read_text(
            encoding="utf-8"
        )
        controller_prompt = (
            "CONTROLLER_TRIAGE_AUTHORING_V1\n"
            "You are a bounded Triage stage producer. The deterministic controller owns "
            "artifact persistence, stage advancement, validation and materialization.\n"
            "Do not write an artifact file and do not call kwc_advance_triage or any Kanban "
            "mutation tool. Return only one JSON object matching the supplied output schema.\n"
            "Use only the projected assignment, bounded evidence and accepted predecessor "
            "payloads in this invocation. The legacy prompt below is semantic guidance only; "
            "ignore its artifact-file, advance-tool and stage-routing instructions.\n\n"
            "--- LEGACY SEMANTIC CONTRACT ---\n"
            f"{prompt}"
        )
        assignment = self._stage_assignment(invocation, stage, raw_path)
        producer_input = {
            "assignment": dict(invocation.input["assignment"]),
            "evidence": dict(invocation.input["evidence"]),
            "predecessors": {
                step_id: {
                    key: value
                    for key, value in payload.items()
                    if key != "body_text"
                }
                for step_id, payload in invocation.input["predecessors"].items()
            },
        }
        produced = self._producer.invoke(
            replace(
                invocation,
                goal=controller_prompt,
                output_schema=_raw_output_schema(invocation.step_id),
                input=producer_input,
            )
        )
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(
            json.dumps(produced.output, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        arguments: dict[str, Any] = {
            "assignment": assignment,
            "artifact_file": raw_path,
        }
        if stage == "intake" and self._inspection_fn is not None:
            arguments["inspection_fn"] = self._inspection_fn
        if stage == "discovery" and self._git_head_fn is not None:
            arguments["git_head_fn"] = self._git_head_fn
        result = triage_workflow.advance_stage(**arguments)
        metadata = dict(result["artifact"])
        body = _read_verified_body(workspace, metadata)
        body_text = (workspace / metadata["path"]).read_text(encoding="utf-8")
        return AgentResult(
            agent_id=produced.agent_id,
            output={
                "stage": stage,
                "artifact": metadata,
                "body": body,
                "body_text": body_text,
            },
        )

    def _materialize(self, invocation: StepInvocation) -> AgentResult:
        workspace = self._workspace(invocation)
        assignment = self._stage_assignment(invocation, "materialize", None)
        arguments = triage_workflow.materialization_arguments(assignment)
        report_path = self._run_dir(workspace, invocation.run_id) / "materialize-report.json"
        report = triage_activity.execute_triage_authoring(
            assignment=assignment,
            authoring_file=workspace / arguments["authoring_file"],
            current_state_inspection_file=workspace / arguments["current_state_inspection_file"],
            output_file=report_path,
            kanban_port=self._kanban_port,
        )
        frozen_path = workspace / str(report["frozen_body_file"])
        frozen_bytes = frozen_path.read_bytes()
        body_text = frozen_bytes.decode("utf-8")
        body = json.loads(body_text)
        milestone = {
            "schema": "controller-triage-milestone-v1",
            "board": assignment["board"],
            "issue_number": assignment["issue_number"],
            "generation": assignment["generation"],
            "workspace_path": assignment["workspace_path"],
            "title": str(report["title"]),
            "assignee": assignment["profile"],
            "status": "triage",
            "body_text": body_text,
            "frozen_digest": str(report["frozen_digest"]),
        }
        return AgentResult(
            agent_id=f"materialize:{invocation.run_id}",
            output={
                "stage": "materialize",
                "artifact": {
                    "path": frozen_path.relative_to(workspace).as_posix(),
                    "sha256": hashlib.sha256(frozen_bytes).hexdigest(),
                },
                "body": body,
                "body_text": body_text,
                "frozen_digest": str(report["frozen_digest"]),
                "read_back": dict(report),
                "milestone": milestone,
            },
        )

    @staticmethod
    def _workspace(invocation: StepInvocation) -> Path:
        assignment = invocation.input.get("assignment")
        if not isinstance(assignment, dict):
            raise ValueError("TRIAGE_ASSIGNMENT_REQUIRED")
        workspace = Path(str(assignment.get("workspace_path") or "")).resolve()
        if not workspace.is_dir():
            raise ValueError("TRIAGE_WORKSPACE_UNAVAILABLE")
        return workspace

    @staticmethod
    def _run_dir(workspace: Path, run_id: str) -> Path:
        safe_run_id = re.sub(r"[^A-Za-z0-9._-]+", "_", run_id)
        run_digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]
        return workspace / ".temp" / "controller-triage" / f"{safe_run_id}-{run_digest}"

    def _stage_assignment(
        self,
        invocation: StepInvocation,
        stage: str,
        raw_path: Path | None,
    ) -> dict[str, Any]:
        base = invocation.input.get("assignment")
        predecessors = invocation.input.get("predecessors")
        if not isinstance(base, dict) or not isinstance(predecessors, dict):
            raise ValueError("TRIAGE_PROJECTION_INVALID")
        assignment = dict(base)
        assignment.update(
            {
                "phase": "triage",
                "triage_stage": stage,
                "stage_artifacts": self._restore_predecessor_artifacts(
                    self._workspace(invocation),
                    predecessors,
                ),
            }
        )
        if raw_path is not None:
            assignment["artifact_file"] = raw_path.relative_to(
                self._workspace(invocation)
            ).as_posix()
        return assignment

    @staticmethod
    def _restore_predecessor_artifacts(
        workspace: Path,
        predecessors: dict[str, Any],
    ) -> dict[str, dict[str, str]]:
        restored: dict[str, dict[str, str]] = {}
        for step_id, payload in predecessors.items():
            stage = _STAGE_NAMES.get(step_id)
            if stage is None or payload.get("stage") != stage:
                raise ValueError("TRIAGE_PREDECESSOR_STAGE_INVALID")
            artifact = payload.get("artifact")
            body = payload.get("body")
            body_text = payload.get("body_text")
            if (
                not isinstance(artifact, dict)
                or not isinstance(body, dict)
                or not isinstance(body_text, str)
            ):
                raise ValueError("TRIAGE_PREDECESSOR_ARTIFACT_INVALID")
            relative_path = Path(str(artifact.get("path") or ""))
            if not str(relative_path) or relative_path.is_absolute():
                raise ValueError("TRIAGE_PREDECESSOR_PATH_INVALID")
            target = (workspace / relative_path).resolve()
            try:
                target.relative_to(workspace)
            except ValueError as exc:
                raise ValueError("TRIAGE_PREDECESSOR_PATH_INVALID") from exc
            data = body_text.encode("utf-8")
            digest = hashlib.sha256(data).hexdigest()
            if artifact.get("sha256") != digest:
                raise ValueError("TRIAGE_PREDECESSOR_DIGEST_INVALID")
            if json.loads(body_text) != json.loads(json.dumps(body, ensure_ascii=False)):
                raise ValueError("TRIAGE_PREDECESSOR_BODY_INVALID")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            restored[stage] = {
                "path": relative_path.as_posix(),
                "sha256": digest,
            }
        return restored


def triage_parity_workflow() -> WorkflowDefinition:
    """Build the canonical intake → materialize Triage workflow."""

    steps = tuple(
        StepDefinition(
            step_id=step_id,
            goal=_goal(step_id),
            allowed_toolsets=_toolsets(step_id),
            acceptance_criteria=_acceptance(step_id),
            output_schema=_accepted_output_schema(step_id),
            projector=_projector(index),
            validator=_accepted_output_validator(step_id),
            executor_kind="materialize" if step_id == "triage_materialize" else "agent",
        )
        for index, step_id in enumerate(_STAGE_IDS)
    )
    return WorkflowDefinition(
        steps=steps,
        transitions=tuple(
            Transition(source, target)
            for source, target in zip(_STAGE_IDS, _STAGE_IDS[1:])
        ),
        initial_step_id=_STAGE_IDS[0],
    )


def normalize_evidence_bundle(
    value: Any,
    *,
    workspace: Path,
    git_head_fn: Callable[[Path], str],
) -> dict[str, Any]:
    """Normalize bounded discovery evidence against one workspace and Git HEAD."""

    normalized = _canonical_evidence_bundle(value)
    workspace = Path(workspace).resolve()
    if not workspace.is_dir():
        raise ValueError("EVIDENCE_WORKSPACE_UNAVAILABLE")
    declared_workspace = Path(normalized["workspace_path"])
    try:
        same_workspace = declared_workspace.samefile(workspace)
    except OSError:
        same_workspace = False
    if not same_workspace:
        raise ValueError("EVIDENCE_WORKSPACE_MISMATCH")
    actual_head = str(git_head_fn(workspace) or "").strip().lower()
    if not _GIT_HEAD.fullmatch(actual_head):
        raise ValueError("EVIDENCE_GIT_HEAD_INVALID")
    if normalized["git_head"] != actual_head:
        raise ValueError("EVIDENCE_GIT_HEAD_MISMATCH")
    normalized["workspace_path"] = workspace.as_posix()
    normalized["git_head"] = actual_head
    return normalized


def _canonical_evidence_bundle(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _BUNDLE_FIELDS:
        raise ValueError("EVIDENCE_BUNDLE_FIELDS")
    if value.get("schema") != "controller-evidence-bundle-v1":
        raise ValueError("EVIDENCE_BUNDLE_SCHEMA")
    raw_workspace = value.get("workspace_path")
    if not isinstance(raw_workspace, str) or not raw_workspace.strip():
        raise ValueError("EVIDENCE_WORKSPACE_INVALID")
    declared_workspace = Path(raw_workspace.strip())
    if not declared_workspace.is_absolute():
        raise ValueError("EVIDENCE_WORKSPACE_INVALID")
    declared_workspace = declared_workspace.resolve()
    raw_head = value.get("git_head")
    if not isinstance(raw_head, str):
        raise ValueError("EVIDENCE_GIT_HEAD_INVALID")
    declared_head = raw_head.strip().lower()
    if not _GIT_HEAD.fullmatch(declared_head):
        raise ValueError("EVIDENCE_GIT_HEAD_INVALID")

    seen: set[str] = set()
    normalized: dict[str, Any] = {
        "schema": "controller-evidence-bundle-v1",
        "workspace_path": declared_workspace.as_posix(),
        "git_head": declared_head,
    }
    for source, limit in _EVIDENCE_LIMITS.items():
        records = value.get(source)
        if not isinstance(records, list) or len(records) > limit:
            raise ValueError(f"EVIDENCE_LIMIT:{source}")
        normalized_records: list[dict[str, str]] = []
        for record in records:
            if not isinstance(record, dict) or set(record) != {"locator", "claim"}:
                raise ValueError(f"EVIDENCE_RECORD_FIELDS:{source}")
            raw_locator = record.get("locator")
            raw_claim = record.get("claim")
            if (
                not isinstance(raw_locator, str)
                or len(raw_locator) > 1024
                or not isinstance(raw_claim, str)
                or len(raw_claim) > 4096
            ):
                raise ValueError(f"EVIDENCE_RECORD_INVALID:{source}")
            locator = raw_locator.strip().replace("\\", "/")
            claim = raw_claim.strip()
            if not locator or len(locator) > 1024 or not claim or len(claim) > 4096:
                raise ValueError(f"EVIDENCE_RECORD_INVALID:{source}")
            if locator in seen:
                raise ValueError(f"EVIDENCE_LOCATOR_DUPLICATE:{locator}")
            seen.add(locator)
            normalized_records.append({"locator": locator, "claim": claim})
        normalized[source] = normalized_records
    return normalized


def _raw_output_schema(step_id: str) -> dict[str, Any]:
    schema_name, required = _RAW_SCHEMAS[step_id]
    properties = {field: {"type": "object"} for field in required if field != "schema"}
    properties["schema"] = {"type": "string", "const": schema_name}
    if "targets" in properties:
        properties["targets"] = {"type": "array"}
    if "fallbacks" in properties:
        properties["fallbacks"] = {"type": "array"}
    if "open_questions" in properties:
        properties["open_questions"] = {"type": "array"}
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _read_verified_body(workspace: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    if set(metadata) != {"path", "sha256"}:
        raise ValueError("TRIAGE_ARTIFACT_METADATA_INVALID")
    path = (workspace / str(metadata["path"])).resolve()
    try:
        path.relative_to(workspace)
    except ValueError as exc:
        raise ValueError("TRIAGE_ARTIFACT_OUTSIDE_WORKSPACE") from exc
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != metadata["sha256"]:
        raise ValueError("TRIAGE_ARTIFACT_DIGEST_MISMATCH")
    value = json.loads(data.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("TRIAGE_ARTIFACT_BODY_INVALID")
    return value


def _projector(index: int) -> Callable[[dict[str, Any], dict[str, dict[str, Any]]], dict[str, Any]]:
    predecessor_ids = _STAGE_IDS[:index]

    def project(
        initial_input: dict[str, Any],
        artifacts: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "assignment": _project_assignment(initial_input["assignment"]),
            "evidence": _canonical_evidence_bundle(initial_input["evidence"]),
            "predecessors": {
                step_id: dict(artifacts[step_id]["payload"])
                for step_id in predecessor_ids
            },
        }

    return project


def _project_assignment(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("TRIAGE_ASSIGNMENT_REQUIRED")
    projected = {field: value[field] for field in _ASSIGNMENT_FIELDS if field in value}
    if set(projected) != set(_ASSIGNMENT_FIELDS):
        raise ValueError("TRIAGE_ASSIGNMENT_IDENTITY_INCOMPLETE")
    if projected["schema"] != "kwc-assignment-v1" or projected["phase"] != "triage":
        raise ValueError("TRIAGE_ASSIGNMENT_IDENTITY_INVALID")
    return projected


def _goal(step_id: str) -> str:
    stage = _STAGE_NAMES[step_id]
    if stage == "materialize":
        return "Freeze the accepted Triage authoring and verify the injected exact read-back port."
    return (
        f"Author the existing {stage} Triage contract from only the projected assignment, "
        "accepted predecessor artifacts, and bounded evidence."
    )


def _toolsets(step_id: str) -> tuple[str, ...]:
    if step_id == "triage_intake":
        return ("github-mcp", "file")
    if step_id == "triage_discovery":
        return ("codebase-memory-mcp", "semble-mcp", "file")
    return ("file",)


def _acceptance(step_id: str) -> tuple[str, ...]:
    stage = _STAGE_NAMES[step_id]
    if stage == "materialize":
        return (
            "Existing freeze and semantic validation accept the final body.",
            "Injected native read-back exactly matches the expected body and metadata.",
        )
    return (
        f"Existing {stage} schema and semantic validator accept the stage output.",
        "The accepted canonical body and SHA-256 are returned without additional fields.",
    )


def _accepted_output_schema(step_id: str) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "stage": {"type": "string", "const": _STAGE_NAMES[step_id]},
        "artifact": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            },
            "required": ["path", "sha256"],
            "additionalProperties": False,
        },
        "body": {"type": "object"},
        "body_text": {"type": "string", "minLength": 1},
    }
    required = ["stage", "artifact", "body", "body_text"]
    if step_id == "triage_materialize":
        properties["frozen_digest"] = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
        properties["read_back"] = {"type": "object"}
        properties["milestone"] = {
            "type": "object",
            "properties": {
                "schema": {"type": "string", "const": "controller-triage-milestone-v1"},
                "board": {"type": "string", "minLength": 1},
                "issue_number": {"type": "integer", "minimum": 1},
                "generation": {"type": "integer", "minimum": 1},
                "workspace_path": {"type": "string", "minLength": 1},
                "title": {"type": "string", "minLength": 1},
                "assignee": {"type": "string", "minLength": 1},
                "status": {"type": "string", "const": "triage"},
                "body_text": {"type": "string", "minLength": 1},
                "frozen_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            },
            "required": [
                "schema",
                "board",
                "issue_number",
                "generation",
                "workspace_path",
                "title",
                "assignee",
                "status",
                "body_text",
                "frozen_digest",
            ],
            "additionalProperties": False,
        }
        required.extend(("frozen_digest", "read_back", "milestone"))

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _accepted_output_validator(step_id: str) -> Callable[[dict[str, Any]], bool]:
    stage = _STAGE_NAMES[step_id]
    expected = {"stage", "artifact", "body", "body_text"}
    if step_id == "triage_materialize":
        expected.update(("frozen_digest", "read_back", "milestone"))
    def validate(value: dict[str, Any]) -> bool:
        artifact = value.get("artifact")
        body = value.get("body")
        body_text = value.get("body_text")
        if not (
            set(value) == expected
            and value.get("stage") == stage
            and isinstance(artifact, dict)
            and set(artifact) == {"path", "sha256"}
            and isinstance(artifact.get("path"), str)
            and bool(artifact["path"])
            and isinstance(artifact.get("sha256"), str)
            and bool(re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]))
            and isinstance(body, dict)
            and isinstance(body_text, str)
            and bool(body_text)
            and hashlib.sha256(body_text.encode("utf-8")).hexdigest() == artifact["sha256"]
        ):
            return False
        try:
            if json.loads(body_text) != json.loads(json.dumps(body, ensure_ascii=False)):
                return False
        except (TypeError, ValueError):
            return False
        if step_id != "triage_materialize":
            return True
        frozen_digest = value.get("frozen_digest")
        read_back = value.get("read_back")
        milestone = value.get("milestone")
        return (
            isinstance(frozen_digest, str)
            and bool(re.fullmatch(r"[0-9a-f]{64}", frozen_digest))
            and body.get("frozen_digest") == frozen_digest
            and isinstance(read_back, dict)
            and set(read_back)
            == {
                "valid",
                "verified",
                "task_id",
                "title",
                "status",
                "frozen_digest",
                "frozen_body_file",
            }
            and read_back.get("valid") is True
            and read_back.get("verified") is True
            and read_back.get("frozen_digest") == frozen_digest
            and read_back.get("frozen_body_file") == artifact["path"]
            and read_back.get("status") == "triage"
            and isinstance(read_back.get("task_id"), str)
            and bool(read_back["task_id"])
            and isinstance(read_back.get("title"), str)
            and bool(read_back["title"])
            and isinstance(milestone, dict)
            and set(milestone)
            == {
                "schema",
                "board",
                "issue_number",
                "generation",
                "workspace_path",
                "title",
                "assignee",
                "status",
                "body_text",
                "frozen_digest",
            }
            and milestone.get("schema") == "controller-triage-milestone-v1"
            and isinstance(milestone.get("board"), str)
            and bool(milestone["board"].strip())
            and type(milestone.get("issue_number")) is int
            and milestone["issue_number"] > 0
            and type(milestone.get("generation")) is int
            and milestone["generation"] > 0
            and isinstance(milestone.get("workspace_path"), str)
            and bool(milestone["workspace_path"].strip())
            and milestone.get("title") == read_back.get("title")
            and isinstance(milestone.get("assignee"), str)
            and bool(milestone["assignee"].strip())
            and milestone.get("status") == "triage"
            and milestone.get("body_text") == body_text
            and milestone.get("frozen_digest") == frozen_digest
        )

    return validate
