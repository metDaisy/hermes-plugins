"""Hermes tool registration for the Profile-independent Plugin boundary."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from typing import Any

Handler = Callable[[dict[str, Any]], str]
Callback = Callable[..., Any]

_TOOLSET = "hermes-langchain"
_PROBE_TOOLSET = "hermes-langchain-controller-probe"


def _schema(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
    *,
    additional_properties: bool | None = False,
) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": copy.deepcopy(properties),
        "required": list(required),
    }
    if additional_properties is not None:
        parameters["additionalProperties"] = additional_properties
    return {
        "name": name,
        "description": description,
        "parameters": parameters,
    }


def _register_tool(
    ctx: Any,
    *,
    name: str,
    schema: dict[str, Any],
    handler: Handler,
    description: str,
    emoji: str,
    check_fn: Callback | None = None,
    toolset: str = _TOOLSET,
) -> None:
    metadata: dict[str, Any] = {"description": description, "emoji": emoji}
    if check_fn is not None:
        metadata["check_fn"] = check_fn
    ctx.register_tool(name, toolset, schema, handler, **metadata)


def register_plugin(
    ctx: Any,
    *,
    handlers: Mapping[str, Handler],
) -> None:
    """Register the fixed Hermes contracts against supplied runtime callbacks."""

    _register_tool(
        ctx,
        name="hermes_langchain_run_workflow",
        schema=_schema(
            "hermes_langchain_run_workflow",
            "Run one Plugin-owned PM workflow situation through a fresh Agent.",
            {
                "workflow_id": {
                    "type": "string",
                    "enum": ["kanban-workflow-orchestration-v1"],
                },
                "artifact_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                },
                "situation": {
                    "type": "string",
                    "enum": [
                        "run-workflow",
                        "implementation-checkpoint",
                        "worker-needs-input",
                        "route-review-findings",
                        "summary-finalization",
                        "service-planning",
                        "sync-docs",
                        "update-current-state",
                        "regenerate-summary",
                    ],
                },
                "board": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "pattern": "^[a-z0-9][a-z0-9_-]{0,63}$",
                },
                "issue_number": {"type": "integer", "minimum": 1},
                "generation": {"type": "integer", "minimum": 1},
                "workspace_path": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 4096,
                    "pattern": "^(?:[A-Za-z]:[\\\\/]|/)",
                },
                "task_id": {
                    "anyOf": [
                        {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 128,
                            "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                        },
                        {"type": "null"},
                    ]
                },
            },
            [
                "workflow_id",
                "artifact_id",
                "situation",
                "board",
                "issue_number",
                "generation",
                "workspace_path",
                "task_id",
            ],
        ),
        handler=handlers["hermes_langchain_run_workflow"],
        description="Execute and persist one Profile-independent KWC workflow situation.",
        emoji="🧭",
    )
    _register_tool(
        ctx,
        name="hermes_langchain_begin_authoring",
        schema=_schema(
            "hermes_langchain_begin_authoring",
            "Start one canonical fresh-agent authoring workflow from the host session.",
            {
                "board": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "pattern": "^[a-z0-9][a-z0-9_-]{0,63}$",
                },
                "issue_number": {"type": "integer", "minimum": 1},
                "generation": {"type": "integer", "minimum": 1},
                "workspace_path": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 4096,
                    "pattern": "^(?:[A-Za-z]:[\\\\/]|/)",
                },
                "replace_incomplete": {"type": "boolean"},
            },
            ["board", "issue_number", "generation", "workspace_path"],
        ),
        handler=handlers["hermes_langchain_begin_authoring"],
        description="Create and launch one controller-owned fresh authoring assignment.",
        emoji="🚦",
    )

    _register_tool(
        ctx,
        name="kwc_advance_triage",
        schema=_schema(
            "kwc_advance_triage",
            "Validate the current semantic Triage stage and return only the next stage contract.",
            {
                "assignment_id": {
                    "type": "string",
                    "description": "Opaque assignment id supplied by KWC.",
                },
                "artifact_file": {
                    "type": "string",
                    "description": "Repo-relative JSON artifact for the current Triage stage.",
                },
            },
            ["assignment_id", "artifact_file"],
        ),
        handler=handlers["kwc_advance_triage"],
        description="Validate and advance one KWC-owned Triage stage.",
        emoji="🧭",
    )
    _register_tool(
        ctx,
        name="kwc_recover_frozen_triage",
        schema=_schema(
            "kwc_recover_frozen_triage",
            "Replace a bad Impl source with an exact validated frozen Triage body.",
            {
                "assignment_id": {
                    "type": "string",
                    "description": "Opaque Impl assignment id supplied by KWC.",
                },
                "frozen_body_file": {
                    "type": "string",
                    "description": "Repo-relative JSON file containing the exact frozen triage-v7 body.",
                },
            },
            ["assignment_id", "frozen_body_file"],
        ),
        handler=handlers["kwc_recover_frozen_triage"],
        description="Recover an exact frozen Triage before Impl materialization.",
        emoji="🧭",
    )

    authoring_contracts = {
        "kwc_create_triage": ("triage", {}, []),
        "kwc_create_impl_graph": (
            "impl",
            {"batch_file": {"type": "string"}},
            ["batch_file"],
        ),
        "kwc_create_review": (
            "review",
            {"body_file": {"type": "string"}},
            ["body_file"],
        ),
        "kwc_create_summary": ("summary", {}, []),
    }
    for tool_name, (phase, properties, required) in authoring_contracts.items():
        _register_tool(
            ctx,
            name=tool_name,
            schema=_schema(
                tool_name,
                f"Materialize and verify the current KWC {phase} authoring assignment.",
                {
                    "assignment_id": {
                        "type": "string",
                        "description": "Opaque assignment id supplied by KWC.",
                    },
                    **properties,
                },
                ["assignment_id", *required],
            ),
            handler=handlers[tool_name],
            description=f"Create and read back the KWC-owned {phase} card phase.",
            emoji="🧭",
        )

    _register_tool(
        ctx,
        name="hermes_langchain_route",
        schema=_schema(
            "hermes_langchain_route",
            "Resolve the next KWC-compatible authoring action without mutating external state.",
            {
                "phase": {
                    "type": "string",
                    "enum": ["triage", "impl", "review", "summary", "complete"],
                },
                "triage_stage": {
                    "type": "string",
                    "enum": [
                        "intake",
                        "discovery",
                        "source_confirmation",
                        "analysis",
                        "materialize",
                    ],
                },
            },
            ["phase"],
        ),
        handler=handlers["hermes_langchain_route"],
        description="Resolve one deterministic LangGraph authoring route.",
        emoji="🕸️",
    )
    _register_tool(
        ctx,
        name="hermes_langchain_probe_guard",
        schema=_schema(
            "hermes_langchain_probe_guard",
            "A side-effect-free inert tool reserved for the controller probe children.",
            {},
            [],
        ),
        handler=handlers["hermes_langchain_probe_guard"],
        description="Return a fixed inert receipt without reading or changing state.",
        emoji="🛡️",
        toolset=_PROBE_TOOLSET,
    )
    _register_tool(
        ctx,
        name="hermes_langchain_controller",
        schema=_schema(
            "hermes_langchain_controller",
            "Run or inspect one plugin-owned deterministic controller workflow.",
            {
                "action": {
                    "type": "string",
                    "enum": ["start", "resume", "status"],
                },
                "workflow_id": {
                    "type": "string",
                    "enum": ["controller-probe-v1"],
                },
                "run_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                },
                "input": {
                    "type": "object",
                    "properties": {
                        "request": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 8000,
                        },
                    },
                    "required": ["request"],
                    "additionalProperties": False,
                },
            },
            ["action", "workflow_id", "run_id"],
        ),
        handler=handlers["hermes_langchain_controller"],
        description="Execute a fixed LangGraph workflow through Hermes fresh agents.",
        emoji="🕸️",
    )
    _register_tool(
        ctx,
        name="hermes_langchain_materialize",
        schema=_schema(
            "hermes_langchain_materialize",
            "Materialize one accepted Triage artifact on native Kanban.",
            {
                "workflow_id": {
                    "type": "string",
                    "enum": ["triage-parity-v1"],
                },
                "artifact_id": {"type": "string", "minLength": 1},
                "artifact_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
                "board": {"type": "string", "minLength": 1},
                "issue_number": {"type": "integer", "minimum": 1},
                "generation": {"type": "integer", "minimum": 1},
                "workspace_path": {"type": "string", "minLength": 1},
                "expected_snapshot_fingerprint": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
            },
            [
                "workflow_id",
                "artifact_id",
                "artifact_sha256",
                "board",
                "issue_number",
                "generation",
                "workspace_path",
                "expected_snapshot_fingerprint",
            ],
        ),
        handler=handlers["hermes_langchain_materialize"],
        description="Fence, create, read back, and verify one accepted Triage milestone.",
        emoji="📌",
    )
    _register_tool(
        ctx,
        name="hermes_langchain_activate",
        schema=_schema(
            "hermes_langchain_activate",
            "Promote one accepted artifact-bound task on native Kanban.",
            {
                "workflow_id": {
                    "type": "string",
                    "enum": ["canonical-task-activation-v1"],
                },
                "artifact_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                },
                "artifact_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
                "board": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "pattern": "^[a-z0-9][a-z0-9_-]{0,63}$",
                },
                "issue_number": {"type": "integer", "minimum": 1},
                "generation": {"type": "integer", "minimum": 1},
                "workspace_path": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 4096,
                    "pattern": "^(?:[A-Za-z]:[\\\\/]|/)",
                },
                "task_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                },
                "expected_snapshot_fingerprint": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
            },
            [
                "workflow_id",
                "artifact_id",
                "artifact_sha256",
                "board",
                "issue_number",
                "generation",
                "workspace_path",
                "task_id",
                "expected_snapshot_fingerprint",
            ],
        ),
        handler=handlers["hermes_langchain_activate"],
        description="Fence, promote, read back, and verify one accepted task.",
        emoji="▶️",
    )
    _register_tool(
        ctx,
        name="hermes_langchain_project_graph",
        schema=_schema(
            "hermes_langchain_project_graph",
            "Validate and persist one accepted-Triage-bound task graph.",
            {
                "workflow_id": {
                    "type": "string",
                    "enum": ["canonical-task-graph-projection-v1"],
                },
                "artifact_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                },
                "authoring_artifact_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                },
                "run_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                },
                "accepted_triage_artifact_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                },
                "accepted_triage_artifact_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
                "source_task_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                },
                "board": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "pattern": "^[a-z0-9][a-z0-9_-]{0,63}$",
                },
                "issue_number": {"type": "integer", "minimum": 1},
                "generation": {"type": "integer", "minimum": 1},
                "workspace_path": {"type": "string", "minLength": 1},
            },
            [
                "workflow_id",
                "artifact_id",
                "authoring_artifact_id",
                "run_id",
                "accepted_triage_artifact_id",
                "accepted_triage_artifact_sha256",
                "source_task_id",
                "board",
                "issue_number",
                "generation",
                "workspace_path",
            ],
        ),
        handler=handlers["hermes_langchain_project_graph"],
        description="Validate accepted Triage provenance and persist one task graph.",
        emoji="📐",
    )
    _register_tool(
        ctx,
        name="hermes_langchain_materialize_graph",
        schema=_schema(
            "hermes_langchain_materialize_graph",
            "Materialize one accepted Impl/Review/Summary task graph on native Kanban.",
            {
                "workflow_id": {
                    "type": "string",
                    "enum": ["canonical-task-graph-materialization-v1"],
                },
                "artifact_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                },
                "artifact_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
                "board": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "pattern": "^[a-z0-9][a-z0-9_-]{0,63}$",
                },
                "issue_number": {"type": "integer", "minimum": 1},
                "generation": {"type": "integer", "minimum": 1},
                "workspace_path": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 4096,
                    "pattern": "^(?:[A-Za-z]:[\\\\/]|/)",
                },
                "expected_snapshot_fingerprint": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
            },
            [
                "workflow_id",
                "artifact_id",
                "artifact_sha256",
                "board",
                "issue_number",
                "generation",
                "workspace_path",
                "expected_snapshot_fingerprint",
            ],
        ),
        handler=handlers["hermes_langchain_materialize_graph"],
        description="Fence, create, read back, and verify an accepted task graph.",
        emoji="🧩",
    )
    _register_tool(
        ctx,
        name="hermes_langchain_dispatch",
        schema=_schema(
            "hermes_langchain_dispatch",
            "Claim one accepted activated task through the canonical controller.",
            {
                "workflow_id": {
                    "type": "string",
                    "enum": ["canonical-task-dispatch-v1"],
                },
                "artifact_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                },
                "graph_artifact_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                },
                "graph_artifact_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "activation_artifact_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                },
                "activation_artifact_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "activation_key": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "board": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "pattern": "^[a-z0-9][a-z0-9_-]{0,63}$",
                },
                "issue_number": {"type": "integer", "minimum": 1},
                "generation": {"type": "integer", "minimum": 1},
                "workspace_path": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 4096,
                    "pattern": "^(?:[A-Za-z]:[\\\\/]|/)",
                },
                "task_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                },
                "expected_snapshot_fingerprint": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            },
            [
                "workflow_id",
                "artifact_id",
                "graph_artifact_id",
                "graph_artifact_sha256",
                "activation_artifact_id",
                "activation_artifact_sha256",
                "activation_key",
                "board",
                "issue_number",
                "generation",
                "workspace_path",
                "task_id",
                "expected_snapshot_fingerprint",
            ],
        ),
        handler=handlers["hermes_langchain_dispatch"],
        description="Fence and atomically claim one accepted activated task.",
        emoji="🚚",
    )
    _register_tool(
        ctx,
        name="hermes_langchain_invoke_agent",
        schema=_schema(
            "hermes_langchain_invoke_agent",
            "Invoke one graph-bound fresh Agent and persist typed delivery provenance.",
            {
                "workflow_id": {
                    "type": "string",
                    "enum": ["canonical-task-agent-delivery-v1"],
                },
                "artifact_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                },
                "dispatch_artifact_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                },
                "dispatch_artifact_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
            },
            [
                "workflow_id",
                "artifact_id",
                "dispatch_artifact_id",
                "dispatch_artifact_sha256",
            ],
        ),
        handler=handlers["hermes_langchain_invoke_agent"],
        description="Run the controller-selected Agent contract and bind its typed result.",
        emoji="🤖",
    )
    _register_tool(
        ctx,
        name="hermes_langchain_complete",
        schema=_schema(
            "hermes_langchain_complete",
            "Complete the exact claimed native run from one accepted typed delivery.",
            {
                "workflow_id": {
                    "type": "string",
                    "enum": ["canonical-task-completion-v1"],
                },
                "artifact_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                },
                "dispatch_artifact_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                },
                "dispatch_artifact_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
                "delivery_artifact_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                },
                "delivery_artifact_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
                "expected_snapshot_fingerprint": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
            },
            [
                "workflow_id",
                "artifact_id",
                "dispatch_artifact_id",
                "dispatch_artifact_sha256",
                "delivery_artifact_id",
                "delivery_artifact_sha256",
                "expected_snapshot_fingerprint",
            ],
        ),
        handler=handlers["hermes_langchain_complete"],
        description="Fence, complete, and exactly read back one claimed native run.",
        emoji="✅",
    )
