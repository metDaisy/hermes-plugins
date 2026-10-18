"""Hermes Plugin composition root with compatibility wrappers."""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from ..dependencies import load as _load_dependency
from ..paths import PLUGIN_ROOT
from . import dispatch_service as _dispatch_service
from . import routing_policy as _routing_policy
from . import runtime_state as _runtime_state
from . import tool_handlers as _tool_handlers
from .registration import register_plugin

def _load_sibling_module(unique_name: str, filename: str) -> Any:
    del unique_name
    return _load_dependency(filename)


def _create_comment_request(*args: Any, **kwargs: Any) -> Any:
    module = _load_sibling_module(
        "_hermes_langchain_comment_service", "comment_service.py"
    )
    return module.create_comment_request(*args, **kwargs)


def _submit_pm_response(*args: Any, **kwargs: Any) -> Any:
    module = _load_sibling_module(
        "_hermes_langchain_invocation_service",
        "invocation_service.py",
    )
    return module.submit_pm_response(*args, **kwargs)


def _submit_profile_response(*args: Any, **kwargs: Any) -> Any:
    module = _load_sibling_module(
        "_hermes_langchain_invocation_service",
        "invocation_service.py",
    )
    return module.submit_profile_response(*args, **kwargs)


def _load_authoring_service() -> Any:
    return _load_sibling_module(
        "_hermes_langchain_authoring_service", "authoring_service.py"
    )


def _load_authoring_executor() -> Any:
    return _load_sibling_module(
        "_hermes_langchain_authoring_executor", "authoring_executor.py"
    )


def _load_triage_workflow() -> Any:
    return _load_sibling_module(
        "_hermes_langchain_triage_workflow", "triage_workflow.py"
    )


def _load_triage_activity() -> Any:
    return _load_sibling_module(
        "_hermes_langchain_triage_activity", "triage_activity.py"
    )


def _now() -> int:
    return int(time.time())


def _state_path() -> Path:
    override = str(os.getenv("HERMES_LANGCHAIN_STATE_PATH") or "").strip()
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise RuntimeError("HERMES_LANGCHAIN_STATE_PATH must be absolute")
        return path
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / "plugin-data" / "hermes-langchain" / "state.db"


def _profile_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        return PLUGIN_ROOT

    return Path(get_hermes_home()).resolve()


def _load_wake_service() -> Any:
    return _load_dependency("wake_service.py")

_STATE_SCHEMA = "hermes-langchain-state-v1"
_WAKE_SCHEMA = "kanban-workflow-wakeup-v1"
_COMMENT_WAKE_SCHEMA = "kanban-comment-wakeup-v1"
_PROFILE_WAKE_SCHEMA = "kwc-profile-wakeup-v1"
_DECISION_SCHEMA = "policy-decision-card-v1"
_IMPLEMENTATION_SCHEMAS = {"backend-implementation-card-v5"}
_REVIEW_SCHEMA = "aggregate-review-card-v5"
_IMPLEMENTATION_PROFILE = "coder"
_REVIEW_PROFILE = "reviewer"
_PROFILE_MENTION = re.compile(r"@\{([A-Za-z0-9][A-Za-z0-9._-]*)\}")
_RUN_WORKFLOW_MARKER = 'The user has invoked the "run-workflow" skill'
_CONTEXT: Any = None
_ACTIVITY_RETENTION_SECONDS = 30 * 24 * 60 * 60


def _host() -> Any:
    return sys.modules[__name__]


def _connect_state(*args: Any, **kwargs: Any) -> Any:
    return _runtime_state._connect_state(_host(), *args, **kwargs)


def _insert_activity(*args: Any, **kwargs: Any) -> Any:
    return _runtime_state._insert_activity(_host(), *args, **kwargs)


def _record_activity(*args: Any, **kwargs: Any) -> Any:
    return _runtime_state._record_activity(_host(), *args, **kwargs)


def _read_activity(*args: Any, **kwargs: Any) -> Any:
    return _runtime_state._read_activity(_host(), *args, **kwargs)


def _bind_profile_session(*args: Any, **kwargs: Any) -> Any:
    return _runtime_state._bind_profile_session(_host(), *args, **kwargs)


def _enqueue_comment_delivery(*args: Any, **kwargs: Any) -> Any:
    return _runtime_state._enqueue_comment_delivery(_host(), *args, **kwargs)


def _read_comment_delivery(*args: Any, **kwargs: Any) -> Any:
    return _runtime_state._read_comment_delivery(_host(), *args, **kwargs)


def _profile_session_key(*args: Any, **kwargs: Any) -> Any:
    return _runtime_state._profile_session_key(_host(), *args, **kwargs)


def _mark_dispatch_heartbeat(*args: Any, **kwargs: Any) -> Any:
    return _runtime_state._mark_dispatch_heartbeat(_host(), *args, **kwargs)


def _read_dispatch_heartbeat(*args: Any, **kwargs: Any) -> Any:
    return _runtime_state._read_dispatch_heartbeat(_host(), *args, **kwargs)


def _bind_state(*args: Any, **kwargs: Any) -> Any:
    return _runtime_state._bind_state(_host(), *args, **kwargs)


def _read_state(*args: Any, **kwargs: Any) -> Any:
    return _runtime_state._read_state(_host(), *args, **kwargs)


def _ensure_board_cursor(*args: Any, **kwargs: Any) -> Any:
    return _runtime_state._ensure_board_cursor(_host(), *args, **kwargs)


def _read_comment_context(*args: Any, **kwargs: Any) -> Any:
    return _routing_policy._read_comment_context(_host(), *args, **kwargs)


def _comment_targets(*args: Any, **kwargs: Any) -> Any:
    return _routing_policy._comment_targets(_host(), *args, **kwargs)


def _comment_wake_message(*args: Any, **kwargs: Any) -> Any:
    return _routing_policy._comment_wake_message(_host(), *args, **kwargs)


def _profile_wake_message(*args: Any, **kwargs: Any) -> Any:
    return _routing_policy._profile_wake_message(_host(), *args, **kwargs)


def _is_run_workflow_invocation(*args: Any, **kwargs: Any) -> Any:
    return _routing_policy._is_run_workflow_invocation(_host(), *args, **kwargs)


def _current_session_key(*args: Any, **kwargs: Any) -> Any:
    return _routing_policy._current_session_key(_host(), *args, **kwargs)


def _current_session_identifiers(*args: Any, **kwargs: Any) -> Any:
    return _routing_policy._current_session_identifiers(_host(), *args, **kwargs)


def _assignment_context(*args: Any, **kwargs: Any) -> Any:
    return _routing_policy._assignment_context(_host(), *args, **kwargs)


def _current_session_profile(*args: Any, **kwargs: Any) -> Any:
    return _routing_policy._current_session_profile(_host(), *args, **kwargs)


def _current_execution_profile(*args: Any, **kwargs: Any) -> Any:
    return _routing_policy._current_execution_profile(_host(), *args, **kwargs)


def _pm_response_available(*args: Any, **kwargs: Any) -> Any:
    return _routing_policy._pm_response_available(_host(), *args, **kwargs)


def _profile_response_available(*args: Any, **kwargs: Any) -> Any:
    return _routing_policy._profile_response_available(_host(), *args, **kwargs)


def _configured_comment_profiles(*args: Any, **kwargs: Any) -> Any:
    return _routing_policy._configured_comment_profiles(_host(), *args, **kwargs)


def _is_actionable(*args: Any, **kwargs: Any) -> Any:
    return _routing_policy._is_actionable(_host(), *args, **kwargs)


def _wake_message(*args: Any, **kwargs: Any) -> Any:
    return _routing_policy._wake_message(_host(), *args, **kwargs)


def _resolved_board(*args: Any, **kwargs: Any) -> Any:
    return _routing_policy._resolved_board(_host(), *args, **kwargs)


def _process_pending_profile_wakes(*args: Any, **kwargs: Any) -> Any:
    return _dispatch_service._process_pending_profile_wakes(_host(), *args, **kwargs)


def _process_pending_comment_deliveries(*args: Any, **kwargs: Any) -> Any:
    return _dispatch_service._process_pending_comment_deliveries(_host(), *args, **kwargs)


def _latest_event_id(*args: Any, **kwargs: Any) -> Any:
    return _dispatch_service._latest_event_id(_host(), *args, **kwargs)


def _process_event(*args: Any, **kwargs: Any) -> Any:
    return _dispatch_service._process_event(_host(), *args, **kwargs)


def _read_events(*args: Any, **kwargs: Any) -> Any:
    return _dispatch_service._read_events(_host(), *args, **kwargs)


def _on_pre_llm_call(*args: Any, **kwargs: Any) -> Any:
    return _dispatch_service._on_pre_llm_call(_host(), *args, **kwargs)


def _process_pending_events(*args: Any, **kwargs: Any) -> Any:
    return _dispatch_service._process_pending_events(_host(), *args, **kwargs)


def _find_hint_event_id(*args: Any, **kwargs: Any) -> Any:
    return _dispatch_service._find_hint_event_id(_host(), *args, **kwargs)


def _on_event_hint(*args: Any, **kwargs: Any) -> Any:
    return _dispatch_service._on_event_hint(_host(), *args, **kwargs)


def _on_dispatch_tick(*args: Any, **kwargs: Any) -> Any:
    return _dispatch_service._on_dispatch_tick(_host(), *args, **kwargs)


def _create_kwc_comment(*args: Any, **kwargs: Any) -> Any:
    return _tool_handlers._create_kwc_comment(_host(), *args, **kwargs)


def _schedule_comment_delivery(*args: Any, **kwargs: Any) -> Any:
    return _tool_handlers._schedule_comment_delivery(_host(), *args, **kwargs)


def _kwc_comment_tool(*args: Any, **kwargs: Any) -> Any:
    return _tool_handlers._kwc_comment_tool(_host(), *args, **kwargs)


def _kwc_reply_tool(*args: Any, **kwargs: Any) -> Any:
    return _tool_handlers._kwc_reply_tool(_host(), *args, **kwargs)


def _kwc_pm_response_tool(*args: Any, **kwargs: Any) -> Any:
    return _tool_handlers._kwc_pm_response_tool(_host(), *args, **kwargs)


def _kwc_profile_response_tool(*args: Any, **kwargs: Any) -> Any:
    return _tool_handlers._kwc_profile_response_tool(_host(), *args, **kwargs)


def _kwc_authoring_tool(*args: Any, **kwargs: Any) -> Any:
    return _tool_handlers._kwc_authoring_tool(_host(), *args, **kwargs)


def _kwc_advance_triage_tool(*args: Any, **kwargs: Any) -> Any:
    return _tool_handlers._kwc_advance_triage_tool(_host(), *args, **kwargs)


def _kwc_recover_frozen_triage_tool(*args: Any, **kwargs: Any) -> Any:
    return _tool_handlers._kwc_recover_frozen_triage_tool(_host(), *args, **kwargs)


def _authoring_handler(*args: Any, **kwargs: Any) -> Any:
    return _tool_handlers._authoring_handler(_host(), *args, **kwargs)


def _event_hint_callback(*args: Any, **kwargs: Any) -> Any:
    return _tool_handlers._event_hint_callback(_host(), *args, **kwargs)


def _hermes_langchain_route_tool(*args: Any, **kwargs: Any) -> Any:
    return _tool_handlers._hermes_langchain_route_tool(_host(), *args, **kwargs)


_KWC_SKILL_NAMES = (
    "kwc-orchestrator-contract",
    "kwc-create-triage",
    "kwc-build-task-graph",
    "kwc-run-workflow",
    "kwc-service-planning",
    "kwc-sync-docs",
    "kwc-update-current-state",
    "kwc-regenerate-summary",
)


def _plugin_hooks() -> dict[str, Any]:
    return {
        "pre_llm_call": _on_pre_llm_call,
        "kanban_task_completed": _event_hint_callback("completed"),
        "kanban_task_blocked": _event_hint_callback("blocked"),
        "on_kanban_worker_exited": _event_hint_callback("worker_exited"),
        "on_kanban_worker_stale_claim": _event_hint_callback("worker_stale_claim"),
        "on_kanban_dispatch_tick": _on_dispatch_tick,
    }


def _plugin_handlers() -> dict[str, Any]:
    return {
        "kwc_advance_triage": _kwc_advance_triage_tool,
        "kwc_recover_frozen_triage": _kwc_recover_frozen_triage_tool,
        "kwc_create_triage": _authoring_handler("triage"),
        "kwc_create_impl_graph": _authoring_handler("impl"),
        "kwc_create_review": _authoring_handler("review"),
        "kwc_create_summary": _authoring_handler("summary"),
        "kwc_comment": _kwc_comment_tool,
        "kwc_reply": _kwc_reply_tool,
        "kwc_pm_response": _kwc_pm_response_tool,
        "kwc_profile_response": _kwc_profile_response_tool,
        "hermes_langchain_route": _hermes_langchain_route_tool,
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
    """Wire Hermes callbacks to the plugin's fixed registration contracts."""

    global _CONTEXT
    _CONTEXT = ctx
    _register_kwc_skills(ctx)
    register_plugin(
        ctx,
        hooks=_plugin_hooks(),
        handlers=_plugin_handlers(),
        checks={
            "kwc_pm_response": _pm_response_available,
            "kwc_profile_response": _profile_response_available,
        },
    )
