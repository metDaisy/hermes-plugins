"""Hermes Plugin composition root for Profile-independent workflows."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

from ..dependencies import load as _load_dependency
from ..paths import PLUGIN_ROOT
from . import tool_handlers as _tool_handlers
from .registration import register_plugin

_CONTEXT: Any = None

_KWC_SKILL_NAMES = (
    "kwc-orchestrator-contract",
    "kwc-create-triage",
    "kwc-triage-authoring-runtime",
    "kwc-impl-authoring-runtime",
    "kwc-review-authoring-runtime",
    "kwc-implementation-delivery-runtime",
    "kwc-review-delivery-runtime",
    "kwc-summary-delivery-runtime",
    "kwc-build-task-graph",
    "kwc-run-workflow",
    "kwc-service-planning",
    "kwc-sync-docs",
    "kwc-update-current-state",
    "kwc-regenerate-summary",
)


def _host() -> Any:
    return sys.modules[__name__]


def _load_sibling_module(unique_name: str, filename: str) -> Any:
    del unique_name
    return _load_dependency(filename)


def _load_authoring_service() -> Any:
    return _load_dependency("authoring_service.py")


def _load_authoring_executor() -> Any:
    return _load_dependency("authoring_executor.py")


def _load_triage_workflow() -> Any:
    return _load_dependency("triage_workflow.py")


def _load_triage_activity() -> Any:
    return _load_dependency("triage_activity.py")


def _state_path() -> Path:
    override = str(os.getenv("HERMES_LANGCHAIN_STATE_PATH") or "").strip()
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise RuntimeError("HERMES_LANGCHAIN_STATE_PATH must be absolute")
        return path
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir("hermes-langchain") / "state.db"


def _profile_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        override = str(os.getenv("HERMES_HOME") or "").strip()
        return Path(override).resolve() if override else PLUGIN_ROOT
    return Path(get_hermes_home()).resolve()


def _host_profile() -> str:
    value = str(os.getenv("HERMES_KWC_FRESH_HOST_PROFILE") or "").strip()
    if value:
        return value
    value = str(os.getenv("HERMES_PROFILE") or "").strip()
    if value:
        return value
    context_profile = str(getattr(_CONTEXT, "profile_name", "") or "").strip()
    return context_profile or "main"


def _current_session_key() -> str:
    try:
        from gateway.session_context import get_session_env

        for key in ("HERMES_SESSION_KEY", "HERMES_SESSION_ID"):
            value = str(get_session_env(key, "") or "").strip()
            if value:
                return value
    except ImportError:
        pass
    for key in ("HERMES_SESSION_KEY", "HERMES_SESSION_ID"):
        value = str(os.getenv(key) or "").strip()
        if value:
            return value
    return ""


def _current_session_identifiers() -> list[str]:
    values = [_current_session_key()]
    try:
        from gateway.session_context import get_session_env

        values.append(str(get_session_env("HERMES_SESSION_ID", "") or "").strip())
    except ImportError:
        values.append(str(os.getenv("HERMES_SESSION_ID") or "").strip())
    return list(dict.fromkeys(value for value in values if value))


def _assignment_context(service: Any, assignment_id: Any) -> tuple[str, dict[str, Any]]:
    assignment = service.read_assignment_by_id(
        state_path=_state_path(),
        assignment_id=assignment_id,
    )
    if assignment is None:
        raise ValueError("assignment is stale or unknown")
    session_ids = _current_session_identifiers()
    if not session_ids:
        session_ids = [
            f"kwc-authoring:{assignment['board']}:g{assignment['generation']}"
        ]
    for session_key in session_ids:
        if service.session_owns_assignment(
            state_path=_state_path(),
            assignment_id=assignment["assignment_id"],
            caller_session_key=session_key,
        ):
            return session_key, assignment
    raise ValueError("current controller session does not own assignment")


def _kwc_authoring_tool(phase: str, arguments: dict[str, Any]) -> Any:
    return _tool_handlers._kwc_authoring_tool(_host(), phase, arguments)


def _kwc_advance_triage_tool(
    arguments: dict[str, Any],
    task_id: str | None = None,
) -> Any:
    del task_id
    return _tool_handlers._kwc_advance_triage_tool(_host(), arguments)


def _kwc_recover_frozen_triage_tool(arguments: dict[str, Any]) -> Any:
    return _tool_handlers._kwc_recover_frozen_triage_tool(_host(), arguments)


def _hermes_langchain_begin_authoring_tool(arguments: dict[str, Any]) -> Any:
    return _tool_handlers._hermes_langchain_begin_authoring_tool(_host(), arguments)


def _authoring_handler(phase: str) -> Any:
    return _tool_handlers._authoring_handler(_host(), phase)


def _hermes_langchain_run_workflow_tool(arguments: dict[str, Any]) -> Any:
    return _tool_handlers._hermes_langchain_run_workflow_tool(_host(), arguments)


def _hermes_langchain_route_tool(arguments: dict[str, Any]) -> Any:
    return _tool_handlers._hermes_langchain_route_tool(_host(), arguments)


def _hermes_langchain_probe_guard_tool(arguments: dict[str, Any]) -> Any:
    return _tool_handlers._hermes_langchain_probe_guard_tool(_host(), arguments)


def _hermes_langchain_controller_tool(arguments: dict[str, Any]) -> Any:
    return _tool_handlers._hermes_langchain_controller_tool(_host(), arguments)


def _hermes_langchain_materialize_tool(arguments: dict[str, Any]) -> Any:
    return _tool_handlers._hermes_langchain_materialize_tool(_host(), arguments)


def _hermes_langchain_activate_tool(arguments: dict[str, Any]) -> Any:
    return _tool_handlers._hermes_langchain_activate_tool(_host(), arguments)


def _hermes_langchain_project_graph_tool(arguments: dict[str, Any]) -> Any:
    return _tool_handlers._hermes_langchain_project_graph_tool(_host(), arguments)


def _hermes_langchain_materialize_graph_tool(arguments: dict[str, Any]) -> Any:
    return _tool_handlers._hermes_langchain_materialize_graph_tool(_host(), arguments)


def _hermes_langchain_dispatch_tool(arguments: dict[str, Any]) -> Any:
    return _tool_handlers._hermes_langchain_dispatch_tool(_host(), arguments)


def _hermes_langchain_invoke_agent_tool(arguments: dict[str, Any]) -> Any:
    return _tool_handlers._hermes_langchain_invoke_agent_tool(_host(), arguments)


def _hermes_langchain_complete_tool(arguments: dict[str, Any]) -> Any:
    return _tool_handlers._hermes_langchain_complete_tool(_host(), arguments)


def _plugin_handlers() -> dict[str, Any]:
    return {
        "hermes_langchain_run_workflow": _hermes_langchain_run_workflow_tool,
        "hermes_langchain_begin_authoring": _hermes_langchain_begin_authoring_tool,
        "kwc_advance_triage": _kwc_advance_triage_tool,
        "kwc_recover_frozen_triage": _kwc_recover_frozen_triage_tool,
        "kwc_create_triage": _authoring_handler("triage"),
        "kwc_create_impl_graph": _authoring_handler("impl"),
        "kwc_create_review": _authoring_handler("review"),
        "kwc_create_summary": _authoring_handler("summary"),
        "hermes_langchain_route": _hermes_langchain_route_tool,
        "hermes_langchain_probe_guard": _hermes_langchain_probe_guard_tool,
        "hermes_langchain_controller": _hermes_langchain_controller_tool,
        "hermes_langchain_materialize": _hermes_langchain_materialize_tool,
        "hermes_langchain_activate": _hermes_langchain_activate_tool,
        "hermes_langchain_project_graph": _hermes_langchain_project_graph_tool,
        "hermes_langchain_materialize_graph": _hermes_langchain_materialize_graph_tool,
        "hermes_langchain_dispatch": _hermes_langchain_dispatch_tool,
        "hermes_langchain_invoke_agent": _hermes_langchain_invoke_agent_tool,
        "hermes_langchain_complete": _hermes_langchain_complete_tool,
    }


def _register_kwc_skills(ctx: Any) -> None:
    for name in _KWC_SKILL_NAMES:
        path = PLUGIN_ROOT / "skills" / name / "SKILL.md"
        content = path.read_text(encoding="utf-8")
        match = re.search(r"(?m)^description:\s*(.+?)\s*$", content)
        description = match.group(1).strip().strip('"\'') if match else name
        ctx.register_skill(
            name,
            path,
            description=description,
            frontmatter={"name": name, "description": description},
        )


def register(ctx: Any) -> None:
    """Register only Plugin-owned Skills and closed controller tools."""

    global _CONTEXT
    _CONTEXT = ctx
    _register_kwc_skills(ctx)
    register_plugin(ctx, handlers=_plugin_handlers())