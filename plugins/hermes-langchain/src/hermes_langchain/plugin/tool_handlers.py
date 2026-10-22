"""Hermes tool handlers and authoring adapters."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import threading
from pathlib import Path
from typing import Any

from ..authoring import artifact_paths
from ..controller.contracts import ControllerPublicError
from ..controller.kanban_activation import KanbanActivationError
from ..controller.kanban_agent_delivery import KanbanAgentDeliveryError
from ..controller.kanban_completion import KanbanCompletionError
from ..controller.kanban_dispatch import KanbanDispatchError
from ..controller.kanban_graph_materialization import KanbanGraphMaterializationError
from ..controller.kanban_materialization import KanbanMaterializationError


def _hermes_langchain_begin_authoring_tool(
    host: Any,
    arguments: dict[str, Any],
) -> str:
    try:
        host_profile = host._current_session_profile()
        if host._current_execution_profile() != host_profile:
            raise ValueError("authoring start requires the host session")
        state_path = host._state_path()
        board = str(arguments.get("board") or "").strip()
        generation = arguments.get("generation")
        assignment = host._load_authoring_service().begin_authoring(
            state_path=state_path,
            board=board,
            issue_number=arguments.get("issue_number"),
            generation=generation,
            workspace_path=Path(str(arguments.get("workspace_path") or "")),
            pm_session_key=f"kwc-authoring:{board}:g{generation}",
            replace_incomplete=arguments.get("replace_incomplete", False),
        )
        launch = host._load_authoring_launcher().launch_assignment(
            assignment=assignment,
            workspace=Path(str(assignment["workspace_path"])),
            state_path=state_path,
            profile="project-manager",
            host_profile=host_profile,
            hermes_executable=shutil.which("hermes") or "hermes",
        )
        return json.dumps(
            {"ok": True, **assignment, "launch": launch},
            ensure_ascii=False,
            sort_keys=True,
        )
    except (OSError, sqlite3.Error, subprocess.SubprocessError, TypeError, ValueError) as exc:
        return json.dumps(
            {
                "ok": False,
                "reason_code": "AUTHORING_START_REJECTED",
                "error": str(exc),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    except Exception:
        return json.dumps(
            {"ok": False, "reason_code": "INTERNAL_FAILURE"},
            sort_keys=True,
        )

def _create_kwc_comment(host: Any, *, board: Any, task_id: Any, body: Any, author: Any, idempotency_key: Any, reply_to_comment_id: Any=None, route_requested: bool) -> dict[str, Any]:
    resolved_board = host._resolved_board(board)
    return host._create_comment_request(host._state_path(), board=resolved_board, task_id=task_id, body=body, author=author, idempotency_key=idempotency_key, reply_to_comment_id=reply_to_comment_id, route_requested=route_requested)


def _schedule_comment_delivery(host: Any, board: str) -> None:
    threading.Thread(target=host._process_pending_comment_deliveries, args=(str(board),), daemon=True, name='kwc-comment-delivery').start()


def _kwc_comment_tool(host: Any, arguments: dict[str, Any]) -> str:
    try:
        result = host._create_kwc_comment(board=arguments.get('board'), task_id=arguments.get('task_id'), body=arguments.get('body'), author=host._current_execution_profile(), idempotency_key=arguments.get('idempotency_key'), route_requested=True)
        host._schedule_comment_delivery(str(result['board']))
        return json.dumps(result, ensure_ascii=False, sort_keys=True)
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        return json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False, sort_keys=True)


def _kwc_reply_tool(host: Any, arguments: dict[str, Any]) -> str:
    try:
        author = host._current_execution_profile()
        board = host._resolved_board(arguments.get('board'))
        task_id = str(arguments.get('task_id') or '').strip()
        reply_id = int(arguments.get('reply_to_comment_id'))
        result = host._create_kwc_comment(board=board, task_id=task_id, body=arguments.get('body'), author=author, idempotency_key=arguments.get('idempotency_key'), reply_to_comment_id=reply_id, route_requested=False)
        host._schedule_comment_delivery(str(result['board']))
        return json.dumps(result, ensure_ascii=False, sort_keys=True)
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        return json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False, sort_keys=True)


def _kwc_pm_response_tool(host: Any, arguments: dict[str, Any]) -> str:
    try:
        result = host._submit_pm_response(state_path=host._state_path(), invocation_id=arguments.get('invocation_id'), caller_profile=host._current_execution_profile(), caller_session_key=host._current_session_key(), outcome=arguments.get('outcome'), task_id=arguments.get('task_id'), reason_code=arguments.get('reason_code'))
        return json.dumps(result, ensure_ascii=False, sort_keys=True)
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        return json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False, sort_keys=True)


def _kwc_profile_response_tool(host: Any, arguments: dict[str, Any]) -> str:
    try:
        result = host._submit_profile_response(state_path=host._state_path(), invocation_id=arguments.get('invocation_id'), caller_profile=host._current_execution_profile(), caller_session_key=host._current_session_key(), outcome=arguments.get('outcome'), task_id=arguments.get('task_id'), reason_code=arguments.get('reason_code'))
        return json.dumps(result, ensure_ascii=False, sort_keys=True)
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        return json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False, sort_keys=True)


def _kwc_authoring_tool(host: Any, phase: str, arguments: dict[str, Any]) -> str:
    try:
        service = host._load_authoring_service()
        session_key, assignment = host._assignment_context(service, arguments.get('assignment_id'))
        if assignment.get('phase') != phase:
            raise ValueError('assignment phase does not match tool')
        execution_arguments = dict(arguments)
        if phase == 'triage':
            if assignment.get('triage_stage') != 'materialize':
                raise ValueError('triage stages are incomplete')
            execution_arguments.update(host._load_triage_workflow().materialization_arguments(assignment))
        execution = host._load_authoring_executor().execute_authoring_phase(assignment=assignment, arguments=execution_arguments, profile_home=host._profile_home())
        next_assignment = service.complete_phase(state_path=host._state_path(), assignment_id=assignment['assignment_id'], caller_profile='project-manager', caller_session_key=session_key, phase=phase, created_task_ids=execution['created_task_ids'])
        return json.dumps({**execution, 'next_assignment': next_assignment}, ensure_ascii=False, sort_keys=True)
    except (OSError, sqlite3.Error, subprocess.SubprocessError, TypeError, ValueError) as exc:
        return json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False, sort_keys=True)


def _kwc_advance_triage_tool(host: Any, arguments: dict[str, Any]) -> str:
    try:
        service = host._load_authoring_service()
        session_key, assignment = host._assignment_context(service, arguments.get('assignment_id'))
        if assignment.get('phase') != 'triage':
            raise ValueError('assignment phase does not match tool')
        execution = host._load_triage_workflow().advance_stage(assignment=assignment, artifact_file=arguments.get('artifact_file'), profile_home=host._profile_home())
        next_assignment = service.complete_triage_stage(state_path=host._state_path(), assignment_id=assignment['assignment_id'], caller_profile='project-manager', caller_session_key=session_key, stage=assignment['triage_stage'], artifact=execution['artifact'])
        return json.dumps({**execution, 'next_assignment': next_assignment}, ensure_ascii=False, sort_keys=True)
    except (OSError, sqlite3.Error, subprocess.SubprocessError, TypeError, ValueError) as exc:
        return json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False, sort_keys=True)


def _kwc_recover_frozen_triage_tool(host: Any, arguments: dict[str, Any]) -> str:
    try:
        service = host._load_authoring_service()
        session_key, assignment = host._assignment_context(service, arguments.get('assignment_id'))
        if assignment.get('phase') != 'impl':
            raise ValueError('assignment phase does not match tool')
        workspace = Path(str(assignment['workspace_path'])).resolve()
        output_file = artifact_paths.triage_output_dir(
            workspace,
            board=assignment['board'],
            issue_number=int(assignment['issue_number']),
            generation=int(assignment['generation']),
        ) / 'triage-recovery-result.json'
        execution = host._load_triage_activity().recover_frozen_triage(assignment=assignment, frozen_body_file=arguments.get('frozen_body_file'), output_file=output_file)
        next_assignment = service.replace_impl_triage_source(state_path=host._state_path(), assignment_id=assignment['assignment_id'], caller_profile='project-manager', caller_session_key=session_key, expected_triage_id=execution['previous_task_id'], replacement_triage_id=execution['task_id'])
        return json.dumps({**execution, 'next_assignment': next_assignment}, ensure_ascii=False, sort_keys=True)
    except (OSError, sqlite3.Error, subprocess.SubprocessError, TypeError, ValueError) as exc:
        return json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False, sort_keys=True)


def _authoring_handler(host: Any, phase: str):

    def handler(arguments: dict[str, Any]) -> str:
        return host._kwc_authoring_tool(phase, arguments)
    return handler


def _event_hint_callback(host: Any, event_kind: str):

    def callback(**kwargs: Any) -> None:
        host._on_event_hint(event_kind=event_kind, **kwargs)
    return callback


def _hermes_langchain_route_tool(host: Any, arguments: dict[str, Any]) -> str:
    try:
        router = host._load_sibling_module('_hermes_langchain_router', 'langgraph_router.py')
        result = router.route_authoring(phase=arguments.get('phase'), triage_stage=arguments.get('triage_stage'))
        return json.dumps({'ok': not result.get('errors'), 'next_action': result.get('next_action'), 'errors': result.get('errors', [])}, ensure_ascii=False, sort_keys=True)
    except Exception as exc:
        return json.dumps({'ok': False, 'next_action': None, 'errors': [str(exc)]}, ensure_ascii=False, sort_keys=True)


def _hermes_langchain_probe_guard_tool(
    _host: Any,
    _arguments: dict[str, Any],
) -> str:
    return json.dumps({"ok": True}, sort_keys=True)


def _hermes_langchain_controller_tool(host: Any, arguments: dict[str, Any]) -> str:
    try:
        if host._CONTEXT is None:
            raise RuntimeError('Hermes plugin context is unavailable')
        from ..controller.service import execute_controller_action

        result = execute_controller_action(
            ctx=host._CONTEXT,
            profile_home=host._profile_home(),
            arguments=arguments,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)
    except ControllerPublicError as exc:
        return json.dumps(
            {
                'ok': False,
                'workflow_id': arguments.get('workflow_id'),
                'run_id': arguments.get('run_id'),
                'error': str(exc),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    except Exception as exc:
        return json.dumps(
            {
                'ok': False,
                'workflow_id': arguments.get('workflow_id'),
                'run_id': arguments.get('run_id'),
                'error': f'controller operation failed: {type(exc).__name__}',
            },
            ensure_ascii=False,
            sort_keys=True,
        )


def _hermes_langchain_materialize_tool(host: Any, arguments: dict[str, Any]) -> str:
    try:
        from ..controller.service import execute_materialization_action

        result = execute_materialization_action(
            profile_home=host._profile_home(),
            arguments=arguments,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)
    except (ControllerPublicError, KanbanMaterializationError) as exc:
        return json.dumps(
            {
                'ok': False,
                'workflow_id': arguments.get('workflow_id'),
                'error': str(exc),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    except Exception as exc:
        return json.dumps(
            {
                'ok': False,
                'workflow_id': arguments.get('workflow_id'),
                'error': f'materialization failed: {type(exc).__name__}',
            },
            ensure_ascii=False,
            sort_keys=True,
        )


def _hermes_langchain_activate_tool(host: Any, arguments: dict[str, Any]) -> str:
    try:
        from ..controller.service import execute_activation_action

        result = execute_activation_action(
            profile_home=host._profile_home(),
            arguments=arguments,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)
    except KanbanActivationError as exc:
        return json.dumps(
            {
                'ok': False,
                'workflow_id': arguments.get('workflow_id'),
                'reason_code': exc.code,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    except ControllerPublicError:
        return json.dumps(
            {
                'ok': False,
                'workflow_id': arguments.get('workflow_id'),
                'reason_code': 'REQUEST_INVALID',
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    except Exception:
        return json.dumps(
            {
                'ok': False,
                'workflow_id': arguments.get('workflow_id'),
                'reason_code': 'INTERNAL_FAILURE',
            },
            ensure_ascii=False,
            sort_keys=True,
        )


def _hermes_langchain_project_graph_tool(host: Any, arguments: dict[str, Any]) -> str:
    try:
        from ..controller.service import execute_graph_projection_action

        if host._CONTEXT is None:
            raise RuntimeError("Hermes plugin context is unavailable")
        result = execute_graph_projection_action(
            ctx=host._CONTEXT,
            profile_home=host._profile_home(),
            arguments=arguments,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)
    except KanbanGraphMaterializationError as exc:
        return json.dumps(
            {
                "ok": False,
                "workflow_id": arguments.get("workflow_id"),
                "reason_code": exc.code,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    except ControllerPublicError:
        return json.dumps(
            {
                "ok": False,
                "workflow_id": arguments.get("workflow_id"),
                "reason_code": "REQUEST_INVALID",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    except Exception:
        return json.dumps(
            {
                "ok": False,
                "workflow_id": arguments.get("workflow_id"),
                "reason_code": "INTERNAL_FAILURE",
            },
            ensure_ascii=False,
            sort_keys=True,
        )


def _hermes_langchain_materialize_graph_tool(host: Any, arguments: dict[str, Any]) -> str:
    try:
        from ..controller.service import execute_graph_materialization_action

        result = execute_graph_materialization_action(
            profile_home=host._profile_home(),
            arguments=arguments,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)
    except KanbanGraphMaterializationError as exc:
        return json.dumps(
            {
                "ok": False,
                "workflow_id": arguments.get("workflow_id"),
                "reason_code": exc.code,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    except ControllerPublicError:
        return json.dumps(
            {
                "ok": False,
                "workflow_id": arguments.get("workflow_id"),
                "reason_code": "REQUEST_INVALID",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    except Exception:
        return json.dumps(
            {
                "ok": False,
                "workflow_id": arguments.get("workflow_id"),
                "reason_code": "INTERNAL_FAILURE",
            },
            ensure_ascii=False,
            sort_keys=True,
        )


def _hermes_langchain_dispatch_tool(host: Any, arguments: dict[str, Any]) -> str:
    try:
        from ..controller.service import execute_dispatch_action

        result = execute_dispatch_action(
            profile_home=host._profile_home(),
            arguments=arguments,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)
    except KanbanDispatchError as exc:
        return json.dumps(
            {
                "ok": False,
                "workflow_id": arguments.get("workflow_id"),
                "reason_code": exc.code,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    except ControllerPublicError:
        return json.dumps(
            {
                "ok": False,
                "workflow_id": arguments.get("workflow_id"),
                "reason_code": "REQUEST_INVALID",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    except Exception:
        return json.dumps(
            {
                "ok": False,
                "workflow_id": arguments.get("workflow_id"),
                "reason_code": "INTERNAL_FAILURE",
            },
            ensure_ascii=False,
            sort_keys=True,
        )


def _hermes_langchain_invoke_agent_tool(host: Any, arguments: dict[str, Any]) -> str:
    try:
        from ..controller.service import execute_agent_delivery_action

        result = execute_agent_delivery_action(
            ctx=host._CONTEXT,
            profile_home=host._profile_home(),
            arguments=arguments,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)
    except KanbanAgentDeliveryError as exc:
        return json.dumps(
            {"ok": False, "workflow_id": arguments.get("workflow_id"), "reason_code": exc.code},
            ensure_ascii=False,
            sort_keys=True,
        )
    except ControllerPublicError:
        return json.dumps(
            {"ok": False, "workflow_id": arguments.get("workflow_id"), "reason_code": "REQUEST_INVALID"},
            ensure_ascii=False,
            sort_keys=True,
        )
    except Exception:
        return json.dumps(
            {"ok": False, "workflow_id": arguments.get("workflow_id"), "reason_code": "INTERNAL_FAILURE"},
            ensure_ascii=False,
            sort_keys=True,
        )


def _hermes_langchain_complete_tool(host: Any, arguments: dict[str, Any]) -> str:
    try:
        from ..controller.service import execute_completion_action

        result = execute_completion_action(
            profile_home=host._profile_home(),
            arguments=arguments,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)
    except KanbanCompletionError as exc:
        return json.dumps(
            {"ok": False, "workflow_id": arguments.get("workflow_id"), "reason_code": exc.code},
            ensure_ascii=False,
            sort_keys=True,
        )
    except ControllerPublicError:
        return json.dumps(
            {"ok": False, "workflow_id": arguments.get("workflow_id"), "reason_code": "REQUEST_INVALID"},
            ensure_ascii=False,
            sort_keys=True,
        )
    except Exception:
        return json.dumps(
            {"ok": False, "workflow_id": arguments.get("workflow_id"), "reason_code": "INTERNAL_FAILURE"},
            ensure_ascii=False,
            sort_keys=True,
        )
