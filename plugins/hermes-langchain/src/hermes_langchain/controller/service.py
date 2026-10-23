"""Plugin-owned registry and execution service for deterministic controller workflows."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .artifact_store import ArtifactRecord, SQLiteArtifactStore, canonical_json_sha256
from .contracts import (
    ControllerPublicError,
    StepDefinition,
    Transition,
    WorkflowDefinition,
)
from .executors import ExecutorRegistry
from .hermes_executor import HermesSubagentExecutor
from .kanban_activation import (
    AcceptedTaskActivator,
    HermesNativeKanbanActivationPort,
)
from .kanban_agent_delivery import AcceptedTaskAgentInvoker, HermesNativeClaimLeasePort
from .kanban_completion import AcceptedTaskCompleter, HermesNativeKanbanCompletionPort
from .kanban_dispatch import (
    AcceptedTaskDispatcher,
    HermesNativeKanbanDispatchPort,
)
from .kanban_graph_materialization import (
    AcceptedTaskGraphProjector,
    AcceptedTaskGraphMaterializer,
    HermesNativeKanbanGraphMaterializationPort,
)
from .kanban_materialization import (
    AcceptedMilestoneMaterializer,
    HermesNativeKanbanMaterializationPort,
)
from .runtime import ControllerRuntime
from .workflow_orchestration import (
    WorkflowOrchestrator,
    read_git_state,
)

_CONTROLLER_DIR = Path("plugin-data") / "hermes-langchain" / "controller" / "v2"
_WORKFLOW_ID = "controller-probe-v1"
_TRIAGE_WORKFLOW_ID = "triage-parity-v1"
_ACTIVATION_WORKFLOW_ID = "canonical-task-activation-v1"
_GRAPH_PROJECTION_WORKFLOW_ID = "canonical-task-graph-projection-v1"
_GRAPH_MATERIALIZATION_WORKFLOW_ID = "canonical-task-graph-materialization-v1"
_DISPATCH_WORKFLOW_ID = "canonical-task-dispatch-v1"
_AGENT_DELIVERY_WORKFLOW_ID = "canonical-task-agent-delivery-v1"
_AGENT_RESULT_STORE_ID = "canonical-task-agent-result-v1"
_COMPLETION_WORKFLOW_ID = "canonical-task-completion-v1"
_ORCHESTRATION_WORKFLOW_ID = "kanban-workflow-orchestration-v1"
_REGISTERED_WORKFLOW_IDS = (
    _WORKFLOW_ID,
    _TRIAGE_WORKFLOW_ID,
    _ACTIVATION_WORKFLOW_ID,
    _GRAPH_PROJECTION_WORKFLOW_ID,
    _GRAPH_MATERIALIZATION_WORKFLOW_ID,
    _DISPATCH_WORKFLOW_ID,
    _AGENT_DELIVERY_WORKFLOW_ID,
    _COMPLETION_WORKFLOW_ID,
    _ORCHESTRATION_WORKFLOW_ID,
)
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MAX_PROBE_TEXT_CHARS = 8_000
_PROBE_TOOLSET = "hermes-langchain-controller-probe"


def registered_workflow_ids() -> tuple[str, ...]:
    """Return the closed set of controller workflows exposed by the plugin."""

    return _REGISTERED_WORKFLOW_IDS


def controller_checkpoint_path(profile_home: Path, workflow_id: str) -> Path:
    """Resolve the profile-local checkpoint database for one registered workflow."""

    _require_registered_workflow_id(workflow_id)
    return Path(profile_home).resolve() / _CONTROLLER_DIR / f"{workflow_id}.sqlite3"


def controller_artifact_store_path(profile_home: Path, workflow_id: str) -> Path:
    """Resolve the profile-local Artifact Store for one registered workflow."""

    _require_registered_workflow_id(workflow_id)
    return (
        Path(profile_home).resolve()
        / _CONTROLLER_DIR
        / f"{workflow_id}.artifacts.sqlite3"
    )


def controller_lifecycle_root(profile_home: Path) -> Path:
    """Resolve host-private lifecycle state through the Hermes Core storage API."""

    from plugins.plugin_storage import plugin_private_data_dir

    root = plugin_private_data_dir("hermes-langchain") / "controller" / "v2" / "lifecycle"
    expected_home = Path(profile_home).resolve()
    if expected_home not in root.resolve().parents:
        raise RuntimeError("Hermes private plugin storage does not match the active profile")
    return root


def execute_controller_action(
    *,
    ctx: Any,
    profile_home: Path,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Execute one closed controller action through the current Hermes context."""

    action = str(arguments.get("action") or "").strip()
    if action not in {"start", "resume", "status"}:
        raise ControllerPublicError("controller action must be start, resume, or status")
    workflow_id = _require_probe_workflow_id(arguments.get("workflow_id"))
    run_id = _require_run_id(arguments.get("run_id"))
    initial_input = _initial_input(arguments.get("input")) if action == "start" else None
    if action != "start" and arguments.get("input") not in (None, {}):
        raise ControllerPublicError("controller input is accepted only for start")

    executor = HermesSubagentExecutor.from_plugin_context(
        ctx,
        durable_root=controller_lifecycle_root(profile_home),
    )
    runtime = ControllerRuntime.open(
        workflow=_controller_probe_workflow(),
        executor_registry=ExecutorRegistry({"agent": executor}),
        checkpoint_path=controller_checkpoint_path(profile_home, workflow_id),
        artifact_store_path=controller_artifact_store_path(profile_home, workflow_id),
    )
    try:
        if action == "start":
            state = runtime.start(run_id=run_id, initial_input=initial_input or {})
        elif action == "resume":
            state = runtime.resume(run_id=run_id)
        else:
            state = runtime.snapshot(run_id=run_id)
    finally:
        runtime.close()
    response = {
        "ok": state.get("status") != "blocked",
        "workflow_id": workflow_id,
        **state,
    }
    if response.get("error") is None:
        response.pop("error", None)
    return response


def execute_workflow_orchestration_action(
    *,
    ctx: Any,
    profile_home: Path,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Execute one migrated PM workflow situation through a fresh Agent."""

    workflow_id = str(arguments.get("workflow_id") or "").strip()
    if workflow_id != _ORCHESTRATION_WORKFLOW_ID:
        raise ControllerPublicError(
            f"unknown workflow orchestration contract: {workflow_id or '<empty>'}"
        )
    request = dict(arguments)
    request["schema"] = "kanban-workflow-orchestration-request-v1"
    from ..kanban.snapshot_service import read_board_snapshot

    store = SQLiteArtifactStore.open(
        controller_artifact_store_path(profile_home, workflow_id)
    )
    try:
        receipt = WorkflowOrchestrator(
            artifact_store=store,
            staging_root=(
                Path(profile_home).resolve()
                / _CONTROLLER_DIR
                / "workflow-orchestration-inputs"
            ),
            executor=HermesSubagentExecutor.from_plugin_context(
                ctx,
                durable_root=controller_lifecycle_root(profile_home),
                defer_release=True,
            ),
            snapshot_reader=read_board_snapshot,
            git_state_reader=read_git_state,
        ).invoke(request)
    finally:
        store.close()
    return {"ok": True, "workflow_id": workflow_id, **receipt}


def execute_materialization_action(
    *,
    profile_home: Path,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Materialize one accepted Triage artifact through the native Kanban port."""

    workflow_id = str(arguments.get("workflow_id") or "").strip()
    if workflow_id != _TRIAGE_WORKFLOW_ID:
        raise ControllerPublicError(
            f"unknown materialization workflow: {workflow_id or '<empty>'}"
        )
    request = dict(arguments)
    request.pop("workflow_id", None)
    request["schema"] = "controller-kanban-materialization-v1"
    artifact_store = SQLiteArtifactStore.open(
        controller_artifact_store_path(profile_home, workflow_id)
    )
    try:
        receipt = AcceptedMilestoneMaterializer(
            artifact_store=artifact_store,
            kanban_port=HermesNativeKanbanMaterializationPort(),
        ).materialize(request)
    finally:
        artifact_store.close()
    return {"ok": True, "workflow_id": workflow_id, **receipt}


def execute_activation_action(
    *,
    profile_home: Path,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Activate one accepted task through the canonical native Kanban port."""

    workflow_id = str(arguments.get("workflow_id") or "").strip()
    if workflow_id != _ACTIVATION_WORKFLOW_ID:
        raise ControllerPublicError(
            f"unknown activation workflow: {workflow_id or '<empty>'}"
        )
    request = dict(arguments)
    request.pop("workflow_id", None)
    request["schema"] = "controller-kanban-activation-v1"
    artifact_store = SQLiteArtifactStore.open(
        controller_artifact_store_path(profile_home, workflow_id)
    )
    try:
        receipt = AcceptedTaskActivator(
            artifact_store=artifact_store,
            kanban_port=HermesNativeKanbanActivationPort(),
        ).activate(request)
    finally:
        artifact_store.close()
    return {"ok": True, "workflow_id": workflow_id, **receipt}


def execute_graph_projection_action(
    *,
    ctx: Any,
    profile_home: Path,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Project one graph from accepted Triage provenance into the canonical store."""

    workflow_id = str(arguments.get("workflow_id") or "").strip()
    if workflow_id != _GRAPH_PROJECTION_WORKFLOW_ID:
        raise ControllerPublicError(
            f"unknown graph projection workflow: {workflow_id or '<empty>'}"
        )
    request = dict(arguments)
    request.pop("workflow_id", None)
    request["schema"] = "controller-task-graph-projection-request-v1"
    triage_store = SQLiteArtifactStore.open(
        controller_artifact_store_path(profile_home, _TRIAGE_WORKFLOW_ID)
    )
    authoring_store = SQLiteArtifactStore.open(
        controller_artifact_store_path(profile_home, _GRAPH_PROJECTION_WORKFLOW_ID)
    )
    graph_store = SQLiteArtifactStore.open(
        controller_artifact_store_path(
            profile_home, _GRAPH_MATERIALIZATION_WORKFLOW_ID
        )
    )
    try:
        receipt = AcceptedTaskGraphProjector(
            triage_artifact_store=triage_store,
            authoring_artifact_store=authoring_store,
            graph_artifact_store=graph_store,
            agent_executor=HermesSubagentExecutor.from_plugin_context(
                ctx,
                durable_root=controller_lifecycle_root(profile_home),
            ),
        ).project(request)
    finally:
        graph_store.close()
        authoring_store.close()
        triage_store.close()
    return {"ok": True, "workflow_id": workflow_id, **receipt}


def execute_graph_materialization_action(
    *,
    profile_home: Path,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Materialize one accepted Impl/Review/Summary graph through native Kanban."""

    workflow_id = str(arguments.get("workflow_id") or "").strip()
    if workflow_id != _GRAPH_MATERIALIZATION_WORKFLOW_ID:
        raise ControllerPublicError(
            f"unknown graph materialization workflow: {workflow_id or '<empty>'}"
        )
    request = dict(arguments)
    request.pop("workflow_id", None)
    request["schema"] = "controller-kanban-graph-materialization-request-v1"
    artifact_store = SQLiteArtifactStore.open(
        controller_artifact_store_path(profile_home, workflow_id)
    )
    activation_store = SQLiteArtifactStore.open(
        controller_artifact_store_path(profile_home, _ACTIVATION_WORKFLOW_ID)
    )
    kanban_port = HermesNativeKanbanGraphMaterializationPort()
    try:
        receipt = AcceptedTaskGraphMaterializer(
            artifact_store=artifact_store,
            kanban_port=kanban_port,
        ).materialize(request)
        graph_record = artifact_store.get(receipt["artifact_id"])
        graph = AcceptedTaskGraphMaterializer._graph(graph_record, request)
        native = kanban_port.read_graph(
            board=receipt["board"],
            source_task_id=receipt["source_task_id"],
            task_ids=receipt["task_ids"],
        )
        receipt["activation_artifacts"] = _project_activation_artifacts(
            store=activation_store,
            graph_record=graph_record,
            graph=graph,
            task_ids=receipt["task_ids"],
            native=native,
        )
    finally:
        activation_store.close()
        artifact_store.close()
    return {"ok": True, "workflow_id": workflow_id, **receipt}


def _project_activation_artifacts(
    *,
    store: SQLiteArtifactStore,
    graph_record: ArtifactRecord,
    graph: dict[str, Any],
    task_ids: dict[str, str],
    native: dict[str, Any],
) -> dict[str, dict[str, str]]:
    """Persist deterministic per-task activation projections from exact native read-back."""

    native_by_id = {
        task.get("id"): task
        for task in native.get("tasks", [])
        if isinstance(task, dict)
    }
    cards = {card["key"]: card for card in graph["cards"]}
    projected: dict[str, dict[str, str]] = {}
    for key, task_id in task_ids.items():
        card = cards[key]
        task = native_by_id[task_id]
        payload = {
            "schema": "controller-task-activation-v1",
            "board": graph["board"],
            "issue_number": graph["issue_number"],
            "generation": graph["generation"],
            "workspace_path": graph["workspace_path"],
            "reason_code": (
                "accepted_graph_candidate"
                if key == graph["activation_candidate_key"]
                else "accepted_graph_dependency"
            ),
            "task": {
                "task_id": task_id,
                "title": card["title"],
                "body_sha256": card["body_sha256"],
                "assignee": card["assignee"],
                "status": task["status"],
                "workspace_kind": task["workspace_kind"],
                "workspace_path": task["workspace_path"],
                "skills": list(task.get("skills") or []),
                "idempotency_key": card["idempotency_key"],
            },
        }
        input_value = {
            "graph_artifact_id": graph_record.artifact_id,
            "graph_artifact_sha256": graph_record.output_sha256,
            "card_key": key,
            "task_id": task_id,
            "native_task_sha256": canonical_json_sha256(task),
        }
        artifact_id = (
            f"activation:{graph_record.output_sha256[:16]}:"
            f"{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}"
        )
        record = store.put(
            ArtifactRecord(
                artifact_id=artifact_id,
                run_id=graph_record.run_id,
                step_id="task_activation",
                attempt=1,
                executor_kind="deterministic",
                input_sha256=canonical_json_sha256(input_value),
                output_sha256=canonical_json_sha256(payload),
                timeout_seconds=30,
                validation_result="accepted",
                executor_identity="controller-task-activation",
                payload=payload,
            ),
            input_value=input_value,
        )
        projected[key] = {
            "artifact_id": record.artifact_id,
            "artifact_sha256": record.output_sha256,
            "task_id": task_id,
        }
    return projected


def execute_dispatch_action(
    *,
    profile_home: Path,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Claim one accepted activated task through the canonical native port."""

    workflow_id = str(arguments.get("workflow_id") or "").strip()
    if workflow_id != _DISPATCH_WORKFLOW_ID:
        raise ControllerPublicError(
            f"unknown dispatch workflow: {workflow_id or '<empty>'}"
        )
    request = dict(arguments)
    request.pop("workflow_id", None)
    request["schema"] = "controller-kanban-dispatch-v1"
    graph_store = SQLiteArtifactStore.open(
        controller_artifact_store_path(profile_home, _GRAPH_MATERIALIZATION_WORKFLOW_ID)
    )
    activation_store = SQLiteArtifactStore.open(
        controller_artifact_store_path(profile_home, _ACTIVATION_WORKFLOW_ID)
    )
    dispatch_store = SQLiteArtifactStore.open(
        controller_artifact_store_path(profile_home, _DISPATCH_WORKFLOW_ID)
    )
    try:
        receipt = AcceptedTaskDispatcher(
            graph_artifact_store=graph_store,
            activation_artifact_store=activation_store,
            dispatch_artifact_store=dispatch_store,
            kanban_port=HermesNativeKanbanDispatchPort(),
        ).dispatch(request)
    finally:
        dispatch_store.close()
        activation_store.close()
        graph_store.close()
    return {"ok": True, "workflow_id": workflow_id, **receipt}


def execute_agent_delivery_action(
    *,
    ctx: Any,
    profile_home: Path,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Invoke the graph-bound fresh Agent and persist typed delivery provenance."""

    workflow_id = str(arguments.get("workflow_id") or "").strip()
    if workflow_id != _AGENT_DELIVERY_WORKFLOW_ID:
        raise ControllerPublicError(
            f"unknown Agent delivery workflow: {workflow_id or '<empty>'}"
        )
    request = dict(arguments)
    request.pop("workflow_id", None)
    request["schema"] = "controller-task-agent-invocation-v1"
    graph_store = SQLiteArtifactStore.open(
        controller_artifact_store_path(profile_home, _GRAPH_MATERIALIZATION_WORKFLOW_ID)
    )
    activation_store = SQLiteArtifactStore.open(
        controller_artifact_store_path(profile_home, _ACTIVATION_WORKFLOW_ID)
    )
    dispatch_store = SQLiteArtifactStore.open(
        controller_artifact_store_path(profile_home, _DISPATCH_WORKFLOW_ID)
    )
    delivery_store = SQLiteArtifactStore.open(
        controller_artifact_store_path(profile_home, _AGENT_DELIVERY_WORKFLOW_ID)
    )
    result_store = SQLiteArtifactStore.open(
        Path(profile_home).resolve()
        / _CONTROLLER_DIR
        / f"{_AGENT_RESULT_STORE_ID}.artifacts.sqlite3"
    )
    hydration_root = (
        Path(profile_home).resolve() / _CONTROLLER_DIR / "agent-inputs"
    )
    try:
        receipt = AcceptedTaskAgentInvoker(
            graph_artifact_store=graph_store,
            activation_artifact_store=activation_store,
            dispatch_artifact_store=dispatch_store,
            delivery_artifact_store=delivery_store,
            result_artifact_store=result_store,
            hydration_root=hydration_root,
            claim_lease_port=HermesNativeClaimLeasePort(),
            executor=HermesSubagentExecutor.from_plugin_context(
                ctx,
                durable_root=controller_lifecycle_root(profile_home),
                defer_release=True,
            ),
        ).invoke(request)
    finally:
        result_store.close()
        delivery_store.close()
        dispatch_store.close()
        activation_store.close()
        graph_store.close()
    return {"ok": True, "workflow_id": workflow_id, **receipt}


def execute_completion_action(
    *,
    profile_home: Path,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Complete the exact claimed native run from one accepted typed delivery."""

    workflow_id = str(arguments.get("workflow_id") or "").strip()
    if workflow_id != _COMPLETION_WORKFLOW_ID:
        raise ControllerPublicError(
            f"unknown completion workflow: {workflow_id or '<empty>'}"
        )
    request = dict(arguments)
    request.pop("workflow_id", None)
    request["schema"] = "controller-kanban-completion-v1"
    graph_store = SQLiteArtifactStore.open(
        controller_artifact_store_path(
            profile_home, _GRAPH_MATERIALIZATION_WORKFLOW_ID
        )
    )
    activation_store = SQLiteArtifactStore.open(
        controller_artifact_store_path(profile_home, _ACTIVATION_WORKFLOW_ID)
    )
    dispatch_store = SQLiteArtifactStore.open(
        controller_artifact_store_path(profile_home, _DISPATCH_WORKFLOW_ID)
    )
    delivery_store = SQLiteArtifactStore.open(
        controller_artifact_store_path(profile_home, _AGENT_DELIVERY_WORKFLOW_ID)
    )
    result_store = SQLiteArtifactStore.open(
        Path(profile_home).resolve()
        / _CONTROLLER_DIR
        / f"{_AGENT_RESULT_STORE_ID}.artifacts.sqlite3"
    )
    completion_store = SQLiteArtifactStore.open(
        controller_artifact_store_path(profile_home, _COMPLETION_WORKFLOW_ID)
    )
    try:
        receipt = AcceptedTaskCompleter(
            graph_artifact_store=graph_store,
            activation_artifact_store=activation_store,
            dispatch_artifact_store=dispatch_store,
            delivery_artifact_store=delivery_store,
            result_artifact_store=result_store,
            completion_artifact_store=completion_store,
            kanban_port=HermesNativeKanbanCompletionPort(),
        ).complete(request)
    finally:
        completion_store.close()
        result_store.close()
        delivery_store.close()
        dispatch_store.close()
        activation_store.close()
        graph_store.close()
    return {"ok": True, "workflow_id": workflow_id, **receipt}


def _require_registered_workflow_id(value: Any) -> str:
    workflow_id = str(value or "").strip()
    if workflow_id not in _REGISTERED_WORKFLOW_IDS:
        raise ControllerPublicError(
            f"unknown controller workflow: {workflow_id or '<empty>'}"
        )
    return workflow_id


def _require_probe_workflow_id(value: Any) -> str:
    workflow_id = _require_registered_workflow_id(value)
    if workflow_id != _WORKFLOW_ID:
        raise ControllerPublicError(f"workflow does not support controller actions: {workflow_id}")
    return workflow_id


def _require_run_id(value: Any) -> str:
    run_id = str(value or "").strip()
    if not _RUN_ID.fullmatch(run_id):
        raise ControllerPublicError(
            "run_id must be 1-128 characters using letters, digits, '.', '_', ':', or '-'"
        )
    return run_id


def _initial_input(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ControllerPublicError("controller start requires an input object")
    request = value.get("request")
    if not isinstance(request, str) or not request.strip():
        raise ControllerPublicError("controller probe input requires a non-empty request")
    if len(request) > _MAX_PROBE_TEXT_CHARS:
        raise ControllerPublicError(
            f"controller probe request must be at most {_MAX_PROBE_TEXT_CHARS} characters"
        )
    return dict(value)


def _controller_probe_workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        steps=(
            StepDefinition(
                step_id="extract_requirement",
                goal=(
                    "Extract exactly one concise requirement from the supplied request. "
                    "Do not call tools. Return only the required JSON object."
                ),
                allowed_toolsets=(_PROBE_TOOLSET,),
                acceptance_criteria=(
                    "The requirement is a non-empty string grounded only in input.request.",
                    "The output contains no keys other than requirement.",
                ),
                output_schema={
                    "type": "object",
                    "properties": {
                        "requirement": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": _MAX_PROBE_TEXT_CHARS,
                        }
                    },
                    "required": ["requirement"],
                    "additionalProperties": False,
                },
                projector=_project_initial_request,
                validator=_valid_requirement,
            ),
            StepDefinition(
                step_id="confirm_requirement",
                goal=(
                    "Confirm the accepted requirement in one concise sentence. "
                    "Do not call tools. Return only the required JSON object."
                ),
                allowed_toolsets=(_PROBE_TOOLSET,),
                acceptance_criteria=(
                    "The confirmation is a non-empty string grounded only in input.requirement.",
                    "The output contains no keys other than confirmation.",
                ),
                output_schema={
                    "type": "object",
                    "properties": {
                        "confirmation": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": _MAX_PROBE_TEXT_CHARS,
                        }
                    },
                    "required": ["confirmation"],
                    "additionalProperties": False,
                },
                projector=_project_accepted_requirement,
                validator=_valid_confirmation,
            ),
        ),
        transitions=(Transition("extract_requirement", "confirm_requirement"),),
        initial_step_id="extract_requirement",
    )


def _project_initial_request(
    initial_input: dict[str, Any],
    _artifacts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {"request": initial_input["request"].strip()}


def _project_accepted_requirement(
    _initial_input: dict[str, Any],
    artifacts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    payload = artifacts["extract_requirement"]["payload"]
    return {"requirement": payload["requirement"]}


def _valid_requirement(output: dict[str, Any]) -> bool:
    return _exact_non_empty_string(output, "requirement")


def _valid_confirmation(output: dict[str, Any]) -> bool:
    return _exact_non_empty_string(output, "confirmation")


def _exact_non_empty_string(output: dict[str, Any], key: str) -> bool:
    return (
        set(output) == {key}
        and isinstance(output.get(key), str)
        and bool(str(output[key]).strip())
        and len(str(output[key])) <= _MAX_PROBE_TEXT_CHARS
    )
