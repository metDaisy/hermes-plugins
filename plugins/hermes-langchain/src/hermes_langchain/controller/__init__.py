"""Deterministic LangGraph controller contracts."""

from importlib import import_module

from .artifact_store import (
    ArtifactRecord,
    ArtifactStoreError,
    SQLiteArtifactStore,
    canonical_json,
    canonical_json_sha256,
)
from .executors import (
    EXECUTOR_KINDS,
    ExecutorRegistry,
    ExecutorRegistryError,
    StepExecutor,
)
from .contracts import (
    ArtifactRef,
    ControllerPublicError,
    StepDefinition,
    StepRun,
    Transition,
    WorkflowContractError,
    WorkflowDefinition,
    WorkflowRun,
    select_next_step,
)

_LAZY_EXPORTS = {
    "AgentResult": (".runtime", "AgentResult"),
    "ControllerRuntime": (".runtime", "ControllerRuntime"),
    "HermesSubagentExecutor": (".hermes_executor", "HermesSubagentExecutor"),
    "HermesProcessExecutor": (".process_executor", "HermesProcessExecutor"),
    "StepInvocation": (".runtime", "StepInvocation"),
    "controller_artifact_store_path": (".service", "controller_artifact_store_path"),
    "controller_checkpoint_path": (".service", "controller_checkpoint_path"),
    "execute_controller_action": (".service", "execute_controller_action"),
    "execute_materialization_action": (".service", "execute_materialization_action"),
    "registered_workflow_ids": (".service", "registered_workflow_ids"),
    "profile_independent_e2e_workflow": (
        ".profile_independent_e2e",
        "profile_independent_e2e_workflow",
    ),
    "normalize_evidence_bundle": (".triage_parity", "normalize_evidence_bundle"),
    "TriageParityExecutor": (".triage_parity", "TriageParityExecutor"),
    "triage_parity_workflow": (".triage_parity", "triage_parity_workflow"),
    "DeliveryRoleExecutor": (".delivery_roles", "DeliveryRoleExecutor"),
    "delivery_roles_workflow": (".delivery_roles", "delivery_roles_workflow"),
    "AcceptedMilestoneMaterializer": (
        ".kanban_materialization",
        "AcceptedMilestoneMaterializer",
    ),
    "HermesNativeKanbanMaterializationPort": (
        ".kanban_materialization",
        "HermesNativeKanbanMaterializationPort",
    ),
    "KanbanMaterializationError": (
        ".kanban_materialization",
        "KanbanMaterializationError",
    ),
    "fingerprint_native_snapshot": (
        ".kanban_materialization",
        "fingerprint_native_snapshot",
    ),
}


def __getattr__(name: str) -> object:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value

__all__ = [
    "AgentResult",
    "AcceptedMilestoneMaterializer",
    "ArtifactRecord",
    "ArtifactRef",
    "ArtifactStoreError",
    "ControllerPublicError",
    "ControllerRuntime",
    "DeliveryRoleExecutor",
    "EXECUTOR_KINDS",
    "ExecutorRegistry",
    "ExecutorRegistryError",
    "HermesSubagentExecutor",
    "HermesProcessExecutor",
    "HermesNativeKanbanMaterializationPort",
    "KanbanMaterializationError",
    "StepDefinition",
    "StepExecutor",
    "StepInvocation",
    "StepRun",
    "SQLiteArtifactStore",
    "Transition",
    "TriageParityExecutor",
    "WorkflowContractError",
    "WorkflowDefinition",
    "WorkflowRun",
    "canonical_json",
    "canonical_json_sha256",
    "controller_artifact_store_path",
    "controller_checkpoint_path",
    "delivery_roles_workflow",
    "execute_controller_action",
    "execute_materialization_action",
    "fingerprint_native_snapshot",
    "registered_workflow_ids",
    "profile_independent_e2e_workflow",
    "normalize_evidence_bundle",
    "select_next_step",
    "triage_parity_workflow",
]
