"""Internal module registry for Plugin-owned authoring helpers."""

from __future__ import annotations

import importlib
from types import ModuleType


_MODULE_BY_FILENAME = {
    "snapshot_service.py": ".kanban.snapshot_service",
    "authoring_executor.py": ".authoring.authoring_executor",
    "authoring_service.py": ".authoring.authoring_service",
    "langgraph_router.py": ".authoring.langgraph_router",
    "triage_activity.py": ".authoring.triage_activity",
    "triage_domain.py": ".authoring.triage_domain",
    "triage_workflow.py": ".authoring.triage_workflow",
}


def load(filename: str) -> ModuleType:
    """Load one supported Plugin-owned helper by filename."""

    try:
        module_name = _MODULE_BY_FILENAME[filename]
    except KeyError as exc:
        raise ImportError(f"unknown Hermes LangChain module: {filename}") from exc
    return importlib.import_module(module_name, package=__package__)
