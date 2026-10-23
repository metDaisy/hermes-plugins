"""Plugin-owned fresh-agent orchestration for migrated PM workflows."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..paths import PLUGIN_ROOT
from .agent_contracts import AgentExecutor, StepInvocation
from .artifact_store import (
    ArtifactRecord,
    ArtifactStoreError,
    SQLiteArtifactStore,
    canonical_json,
    canonical_json_sha256,
)

_REQUEST_SCHEMA = "kanban-workflow-orchestration-request-v1"
_WORKFLOW_ID = "kanban-workflow-orchestration-v1"
_RESULT_SCHEMA = "kanban-workflow-orchestration-result-v1"
_RECEIPT_SCHEMA = "kanban-workflow-orchestration-receipt-v1"
_REQUEST_FIELDS = {
    "schema",
    "artifact_id",
    "workflow_id",
    "situation",
    "board",
    "issue_number",
    "generation",
    "workspace_path",
    "task_id",
}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_BOARD = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_STATUSES = {
    "running",
    "checkpoint-done",
    "review-rework",
    "release-done",
    "blocked",
}
_VALIDATION_OUTCOMES = {"passed", "blocked", "not-applicable"}

_BASE_TOOLSETS = ("file", "terminal", "hermes-langchain")
_VERIFICATION_TOOLSETS = (
    *_BASE_TOOLSETS,
    "gradle-mcp",
    "codebase-memory-mcp",
    "github-mcp",
    "semble-mcp",
)
_RESEARCH_TOOLSETS = (
    *_BASE_TOOLSETS,
    "codebase-memory-mcp",
    "github-mcp",
    "semble-mcp",
)

_SITUATIONS: dict[str, dict[str, Any]] = {
    "run-workflow": {
        "skills": (
            "kwc-run-workflow",
            "kwc-triage-authoring-runtime",
            "kwc-impl-authoring-runtime",
            "kwc-review-authoring-runtime",
            "kwc-summary-delivery-runtime",
        ),
        "toolsets": _VERIFICATION_TOOLSETS,
    },
    "implementation-checkpoint": {
        "skills": ("kwc-run-workflow",),
        "toolsets": _VERIFICATION_TOOLSETS,
    },
    "worker-needs-input": {
        "skills": ("kwc-run-workflow",),
        "toolsets": _RESEARCH_TOOLSETS,
    },
    "route-review-findings": {
        "skills": ("kwc-run-workflow",),
        "toolsets": _RESEARCH_TOOLSETS,
    },
    "summary-finalization": {
        "skills": ("kwc-run-workflow",),
        "toolsets": _VERIFICATION_TOOLSETS,
    },
    "service-planning": {
        "skills": ("kwc-service-planning",),
        "toolsets": (*_RESEARCH_TOOLSETS, "web", "browser"),
    },
    "sync-docs": {
        "skills": ("kwc-sync-docs",),
        "toolsets": _RESEARCH_TOOLSETS,
    },
    "update-current-state": {
        "skills": ("kwc-update-current-state",),
        "toolsets": (*_BASE_TOOLSETS, "codebase-memory-mcp", "semble-mcp"),
    },
    "regenerate-summary": {
        "skills": ("kwc-regenerate-summary",),
        "toolsets": _BASE_TOOLSETS,
    },
}

_GOAL = (
    "CONTROLLER_PM_ORCHESTRATION_V1\n"
    "Read the exact staged orchestrator-contract.md, situation skill, board.json, git.json, and manifest.json. "
    "Execute only the requested Plugin-owned workflow situation in the pinned workspace. Treat Project Manager, "
    "Coder, and Reviewer as Kanban workflow roles, not Hermes Profiles. Do not read Profile SOUL, memory, prior "
    "conversation, or legacy wake/response state. Deterministic validators and controller tools own mutations; "
    "read back every changed external target. Return only one JSON object matching the supplied output schema."
)


class WorkflowOrchestrationError(ValueError):
    """Stable fail-closed error for one PM workflow fresh-agent invocation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def workflow_contract(situation: Any) -> dict[str, Any]:
    name = _required_text(situation, "situation", pattern=_ID, max_length=128)
    try:
        template = _SITUATIONS[name]
    except KeyError as exc:
        raise WorkflowOrchestrationError(
            "SITUATION_INVALID", "workflow situation is not Plugin-owned"
        ) from exc
    return {
        "step_id": "workflow_orchestration",
        "goal": _GOAL,
        "skills": (
            "kwc-orchestrator-contract",
            *(str(skill) for skill in template["skills"]),
        ),
        "toolsets": tuple(template["toolsets"]),
        "acceptance_criteria": (
            "Use only the staged Plugin-owned workflow contract and exact board/Git read-back.",
            "Keep workflow role identity separate from the infrastructure host Profile.",
            "Run deterministic preflight before mutation and exact read-back after mutation.",
            "Return a typed result without legacy Profile wake or response tools.",
        ),
        "output_schema": _output_schema(),
    }


class WorkflowOrchestrator:
    """Invoke and persist one Profile-independent PM workflow situation."""

    def __init__(
        self,
        *,
        artifact_store: SQLiteArtifactStore,
        staging_root: Path,
        executor: AgentExecutor,
        snapshot_reader: Callable[[str], dict[str, Any]],
        git_state_reader: Callable[[Path], dict[str, Any]],
    ) -> None:
        self._artifact_store = artifact_store
        self._staging_root = Path(staging_root).resolve()
        self._executor = executor
        self._snapshot_reader = snapshot_reader
        self._git_state_reader = git_state_reader

    def invoke(self, request: Any) -> dict[str, Any]:
        contract = _request(request)
        role_contract = workflow_contract(contract["situation"])
        workspace = Path(contract["workspace_path"]).resolve()
        board = _board_snapshot(self._snapshot_reader(contract["board"]), contract["board"])
        git = _git_state(self._git_state_reader(workspace), workspace)
        input_value = {
            "request": contract,
            "board": board,
            "git": git,
            "execution_contract": {
                "step_id": role_contract["step_id"],
                "skills": list(role_contract["skills"]),
                "toolsets": list(role_contract["toolsets"]),
            },
        }
        input_sha256 = canonical_json_sha256(input_value)
        try:
            existing = self._artifact_store.get(contract["artifact_id"])
        except ArtifactStoreError as exc:
            if exc.code != "ARTIFACT_NOT_FOUND":
                raise
        else:
            return self._replay(existing, input_value, contract)

        invocation_key = canonical_json_sha256(input_value)
        artifact_directory = self._stage(
            invocation_key=invocation_key,
            request=contract,
            board=board,
            git=git,
            role_contract=role_contract,
        )
        invocation = StepInvocation(
            run_id=f"workflow:{contract['board']}:g{contract['generation']}",
            invocation_id=invocation_key,
            step_id=role_contract["step_id"],
            goal=role_contract["goal"],
            allowed_toolsets=role_contract["toolsets"],
            acceptance_criteria=role_contract["acceptance_criteria"],
            output_schema=role_contract["output_schema"],
            input={
                "schema": "kanban-workflow-orchestration-input-v1",
                "artifact_directory": artifact_directory.as_posix(),
                "request": contract,
                "board_sha256": canonical_json_sha256(board),
                "git_sha256": canonical_json_sha256(git),
            },
            working_directory=contract["workspace_path"],
            skills=role_contract["skills"],
        )
        try:
            produced = self._executor.invoke(invocation)
        except Exception as exc:
            raise WorkflowOrchestrationError(
                "AGENT_INVOCATION_FAILED", "fresh workflow Agent invocation failed"
            ) from exc
        agent_id = _required_text(
            produced.agent_id, "agent_id", pattern=_ID, max_length=128
        )
        result = _result(produced.output, contract)
        payload = {
            "schema": _RECEIPT_SCHEMA,
            "workflow_id": contract["workflow_id"],
            "artifact_id": contract["artifact_id"],
            "invocation_key": invocation_key,
            "agent_id": agent_id,
            "board_sha256": canonical_json_sha256(board),
            "git_sha256": canonical_json_sha256(git),
            "execution_contract": {
                "step_id": invocation.step_id,
                "skills": list(invocation.skills),
                "toolsets": list(invocation.allowed_toolsets),
                "workspace_path": invocation.working_directory,
            },
            "result": result,
        }
        record = ArtifactRecord(
            artifact_id=contract["artifact_id"],
            run_id=invocation.run_id,
            step_id=invocation.step_id,
            attempt=1,
            executor_kind="agent",
            input_sha256=input_sha256,
            output_sha256=canonical_json_sha256(payload),
            timeout_seconds=invocation.timeout_seconds,
            validation_result="accepted",
            executor_identity=agent_id,
            payload=payload,
        )
        stored = self._artifact_store.put(record, input_value=input_value)
        acknowledge = getattr(self._executor, "acknowledge_persisted_result", None)
        if callable(acknowledge):
            acknowledge(invocation.invocation_id)
        return _public_receipt(stored, payload, idempotent_replay=False)

    def _replay(
        self,
        record: ArtifactRecord,
        input_value: Mapping[str, Any],
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload = _json_copy(record.payload)
        if (
            record.step_id != "workflow_orchestration"
            or record.executor_kind != "agent"
            or record.validation_result != "accepted"
            or record.input_sha256 != canonical_json_sha256(input_value)
            or record.output_sha256 != canonical_json_sha256(payload)
            or payload.get("schema") != _RECEIPT_SCHEMA
            or payload.get("workflow_id") != request["workflow_id"]
            or payload.get("artifact_id") != request["artifact_id"]
            or payload.get("agent_id") != record.executor_identity
        ):
            raise WorkflowOrchestrationError(
                "CONFLICTING_REPLAY", "stored workflow orchestration receipt conflicts"
            )
        _result(payload.get("result"), request)
        return _public_receipt(record, payload, idempotent_replay=True)

    def _stage(
        self,
        *,
        invocation_key: str,
        request: Mapping[str, Any],
        board: Mapping[str, Any],
        git: Mapping[str, Any],
        role_contract: Mapping[str, Any],
    ) -> Path:
        directory = self._staging_root / invocation_key
        directory.mkdir(parents=True, exist_ok=True)
        situation_skill = str(role_contract["skills"][1])
        files = {
            "orchestrator-contract.md": _skill_path("kwc-orchestrator-contract").read_text(
                encoding="utf-8"
            ),
            "workflow.md": _skill_path(situation_skill).read_text(encoding="utf-8"),
            "board.json": _pretty_json(board),
            "git.json": _pretty_json(git),
        }
        for skill in role_contract["skills"][2:]:
            files[f"skill-{skill}.md"] = _skill_path(str(skill)).read_text(encoding="utf-8")
        manifest = {
            "schema": "kanban-workflow-orchestration-manifest-v1",
            "invocation_key": invocation_key,
            "request": dict(request),
            "files": {
                name: hashlib.sha256(content.encode("utf-8")).hexdigest()
                for name, content in files.items()
            },
            "execution_contract": {
                "step_id": role_contract["step_id"],
                "skills": list(role_contract["skills"]),
                "toolsets": list(role_contract["toolsets"]),
            },
        }
        files["manifest.json"] = _pretty_json(manifest)
        for name, content in files.items():
            path = directory / name
            if path.exists() and path.read_text(encoding="utf-8") != content:
                raise WorkflowOrchestrationError(
                    "STAGED_ARTIFACT_CONFLICT", "staged workflow artifact conflicts"
                )
            path.write_text(content, encoding="utf-8")
        return directory


def read_git_state(workspace: Path) -> dict[str, Any]:
    """Read the exact Git identity used by workflow orchestration admission."""

    root = Path(workspace).resolve()
    if not root.is_dir():
        raise WorkflowOrchestrationError("WORKSPACE_INVALID", "workspace does not exist")
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout.strip()
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout.splitlines()
    dirty_paths: list[str] = []
    for line in status:
        path = line[3:].strip() if len(line) >= 4 else ""
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            dirty_paths.append(path)
    return {
        "schema": "kwc-git-state-v1",
        "workspace_path": str(root),
        "head_sha": head,
        "dirty_paths": dirty_paths,
    }


def _request(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _REQUEST_FIELDS:
        raise WorkflowOrchestrationError("REQUEST_INVALID", "workflow request fields are invalid")
    request = dict(value)
    if request.get("schema") != _REQUEST_SCHEMA or request.get("workflow_id") != _WORKFLOW_ID:
        raise WorkflowOrchestrationError("REQUEST_INVALID", "workflow request schema is invalid")
    request["artifact_id"] = _required_text(
        request.get("artifact_id"), "artifact_id", pattern=_ID, max_length=128
    )
    request["situation"] = _required_text(
        request.get("situation"), "situation", pattern=_ID, max_length=128
    )
    workflow_contract(request["situation"])
    request["board"] = _required_text(
        request.get("board"), "board", pattern=_BOARD, max_length=64
    )
    for field in ("issue_number", "generation"):
        if type(request.get(field)) is not int or request[field] < 1:
            raise WorkflowOrchestrationError("REQUEST_INVALID", f"request.{field} is invalid")
    workspace = Path(str(request.get("workspace_path") or "")).resolve()
    if not workspace.is_dir():
        raise WorkflowOrchestrationError("WORKSPACE_INVALID", "workspace does not exist")
    request["workspace_path"] = str(workspace)
    task_id = request.get("task_id")
    request["task_id"] = (
        None
        if task_id is None
        else _required_text(task_id, "task_id", pattern=_ID, max_length=128)
    )
    return request


def _board_snapshot(value: Any, expected_board: str) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or value.get("schema") != "kwc-workflow-snapshot-v1"
        or value.get("board") != expected_board
        or not isinstance(value.get("tasks"), Sequence)
        or isinstance(value.get("tasks"), (str, bytes))
    ):
        raise WorkflowOrchestrationError("BOARD_SNAPSHOT_INVALID", "board snapshot is invalid")
    return _json_copy(value)


def _git_state(value: Any, workspace: Path) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"schema", "workspace_path", "head_sha", "dirty_paths"}
        or value.get("schema") != "kwc-git-state-v1"
        or Path(str(value.get("workspace_path") or "")).resolve() != workspace
        or not _SHA40.fullmatch(str(value.get("head_sha") or ""))
        or not isinstance(value.get("dirty_paths"), Sequence)
        or isinstance(value.get("dirty_paths"), (str, bytes))
        or any(not isinstance(path, str) or not path for path in value["dirty_paths"])
    ):
        raise WorkflowOrchestrationError("GIT_STATE_INVALID", "Git state is invalid")
    return _json_copy(value)


def _result(value: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "schema",
        "status",
        "issue_board",
        "phase_task_run",
        "head_final_implementation_sha",
        "validation",
        "native_readback",
        "external_readback",
        "blocker",
        "next_transition",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise WorkflowOrchestrationError("RESULT_INVALID", "workflow result fields are invalid")
    result = _json_copy(value)
    if result.get("schema") != _RESULT_SCHEMA or result.get("status") not in _STATUSES:
        raise WorkflowOrchestrationError("RESULT_INVALID", "workflow result identity is invalid")
    issue_board = result.get("issue_board")
    if not isinstance(issue_board, Mapping) or dict(issue_board) != {
        "board": request["board"],
        "issue_number": request["issue_number"],
        "generation": request["generation"],
    }:
        raise WorkflowOrchestrationError("RESULT_INVALID", "workflow result Issue identity changed")
    phase = result.get("phase_task_run")
    if not isinstance(phase, Mapping) or set(phase) != {"phase", "task_id", "run_id"}:
        raise WorkflowOrchestrationError("RESULT_INVALID", "workflow phase result is invalid")
    _required_text(phase.get("phase"), "phase", max_length=128)
    if phase.get("task_id") is not None:
        _required_text(phase.get("task_id"), "task_id", pattern=_ID, max_length=128)
    if phase.get("run_id") is not None and (
        type(phase.get("run_id")) is not int or phase["run_id"] < 1
    ):
        raise WorkflowOrchestrationError("RESULT_INVALID", "workflow run identity is invalid")
    heads = result.get("head_final_implementation_sha")
    if not isinstance(heads, Mapping) or set(heads) != {
        "head_sha",
        "final_implementation_sha",
    }:
        raise WorkflowOrchestrationError("RESULT_INVALID", "workflow Git result is invalid")
    if not _SHA40.fullmatch(str(heads.get("head_sha") or "")):
        raise WorkflowOrchestrationError("RESULT_INVALID", "workflow HEAD is invalid")
    final_sha = heads.get("final_implementation_sha")
    if final_sha is not None and not _SHA40.fullmatch(str(final_sha)):
        raise WorkflowOrchestrationError("RESULT_INVALID", "final implementation SHA is invalid")
    validations = result.get("validation")
    if not isinstance(validations, list) or not validations:
        raise WorkflowOrchestrationError("RESULT_INVALID", "workflow validation evidence is required")
    for row in validations:
        if (
            not isinstance(row, Mapping)
            or set(row) != {"name", "outcome", "evidence"}
            or row.get("outcome") not in _VALIDATION_OUTCOMES
        ):
            raise WorkflowOrchestrationError("RESULT_INVALID", "workflow validation row is invalid")
        _required_text(row.get("name"), "validation.name", max_length=128)
        _required_text(row.get("evidence"), "validation.evidence", max_length=4000)
    for field in ("native_readback", "external_readback"):
        values = result.get(field)
        if not isinstance(values, list) or any(
            not isinstance(item, str) or not item.strip() for item in values
        ):
            raise WorkflowOrchestrationError("RESULT_INVALID", f"workflow {field} is invalid")
    for field in ("blocker", "next_transition"):
        if result.get(field) is not None:
            _required_text(result.get(field), field, max_length=4000)
    if result["status"] == "blocked" and result.get("blocker") is None:
        raise WorkflowOrchestrationError("RESULT_INVALID", "blocked workflow requires blocker")
    return result


def _output_schema() -> dict[str, Any]:
    nullable_string = {"anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]}
    return {
        "type": "object",
        "properties": {
            "schema": {"const": _RESULT_SCHEMA},
            "status": {"type": "string", "enum": sorted(_STATUSES)},
            "issue_board": {
                "type": "object",
                "properties": {
                    "board": {"type": "string"},
                    "issue_number": {"type": "integer", "minimum": 1},
                    "generation": {"type": "integer", "minimum": 1},
                },
                "required": ["board", "issue_number", "generation"],
                "additionalProperties": False,
            },
            "phase_task_run": {
                "type": "object",
                "properties": {
                    "phase": {"type": "string", "minLength": 1},
                    "task_id": nullable_string,
                    "run_id": {
                        "anyOf": [
                            {"type": "integer", "minimum": 1},
                            {"type": "null"},
                        ]
                    },
                },
                "required": ["phase", "task_id", "run_id"],
                "additionalProperties": False,
            },
            "head_final_implementation_sha": {
                "type": "object",
                "properties": {
                    "head_sha": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
                    "final_implementation_sha": {
                        "anyOf": [
                            {"type": "string", "pattern": "^[0-9a-f]{40}$"},
                            {"type": "null"},
                        ]
                    },
                },
                "required": ["head_sha", "final_implementation_sha"],
                "additionalProperties": False,
            },
            "validation": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "minLength": 1},
                        "outcome": {
                            "type": "string",
                            "enum": sorted(_VALIDATION_OUTCOMES),
                        },
                        "evidence": {"type": "string", "minLength": 1},
                    },
                    "required": ["name", "outcome", "evidence"],
                    "additionalProperties": False,
                },
            },
            "native_readback": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
            },
            "external_readback": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
            },
            "blocker": nullable_string,
            "next_transition": nullable_string,
        },
        "required": [
            "schema",
            "status",
            "issue_board",
            "phase_task_run",
            "head_final_implementation_sha",
            "validation",
            "native_readback",
            "external_readback",
            "blocker",
            "next_transition",
        ],
        "additionalProperties": False,
    }


def _skill_path(name: str) -> Path:
    path = PLUGIN_ROOT / "skills" / name / "SKILL.md"
    if not path.is_file():
        raise WorkflowOrchestrationError("SKILL_MISSING", "Plugin-owned workflow Skill is missing")
    return path


def _required_text(
    value: Any,
    field: str,
    *,
    pattern: re.Pattern[str] | None = None,
    max_length: int,
) -> str:
    text = str(value or "").strip()
    if not text or len(text) > max_length or (pattern is not None and not pattern.fullmatch(text)):
        raise WorkflowOrchestrationError("REQUEST_INVALID", f"{field} is invalid")
    return text


def _json_copy(value: Any) -> dict[str, Any]:
    copied = json.loads(canonical_json(value))
    if not isinstance(copied, dict):
        raise WorkflowOrchestrationError("CONTRACT_INVALID", "canonical value is not an object")
    return copied


def _pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _public_receipt(
    record: ArtifactRecord,
    payload: Mapping[str, Any],
    *,
    idempotent_replay: bool,
) -> dict[str, Any]:
    return {
        "ok": True,
        "artifact_id": record.artifact_id,
        "artifact_sha256": record.output_sha256,
        "agent_id": payload["agent_id"],
        "result": _json_copy(payload["result"]),
        "idempotent_replay": idempotent_replay,
    }
