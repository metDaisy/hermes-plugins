"""Internal module registry used by process entrypoints.

The registry keeps executable workers independent from filesystem adjacency while
preserving one stable filename vocabulary for durable launcher contracts.
"""

from __future__ import annotations

import importlib
from types import ModuleType


_MODULE_BY_FILENAME = {
    "action_executor.py": ".runtime.action_executor",
    "agent_lease_service.py": ".runtime.agent_lease_service",
    "comment_service.py": ".runtime.comment_service",
    "invocation_service.py": ".runtime.invocation_service",
    "profile_launcher.py": ".runtime.profile_launcher",
    "profile_worker.py": ".runtime.profile_worker",
    "runtime_service.py": ".runtime.runtime_service",
    "wake_service.py": ".runtime.wake_service",
    "action_service.py": ".kanban.action_service",
    "dispatch_service.py": ".kanban.dispatch_service",
    "kwc_core.py": ".kanban.kwc_core",
    "mutation_service.py": ".kanban.mutation_service",
    "snapshot_service.py": ".kanban.snapshot_service",
    "workflow_engine.py": ".kanban.workflow_engine",
    "authoring_executor.py": ".authoring.authoring_executor",
    "authoring_launcher.py": ".authoring.authoring_launcher",
    "authoring_service.py": ".authoring.authoring_service",
    "langgraph_router.py": ".authoring.langgraph_router",
    "triage_activity.py": ".authoring.triage_activity",
    "triage_domain.py": ".authoring.triage_domain",
    "triage_workflow.py": ".authoring.triage_workflow",
}


def load(filename: str) -> ModuleType:
    """Load an internal module by its stable historical filename."""

    try:
        module_name = _MODULE_BY_FILENAME[filename]
    except KeyError as exc:
        raise ImportError(f"unknown Hermes LangChain module: {filename}") from exc
    return importlib.import_module(module_name, package=__package__)
