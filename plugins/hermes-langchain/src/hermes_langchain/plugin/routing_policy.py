"""Pure and context-facing routing policy helpers."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

def _read_comment_context(host: Any, board: str, task_id: str, comment_id: int) -> dict[str, Any] | None:
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect(board=board)
    try:
        row = conn.execute('SELECT c.body, t.assignee FROM task_comments c JOIN tasks t ON t.id = c.task_id WHERE c.id = ? AND c.task_id = ?', (int(comment_id), str(task_id))).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def _comment_targets(host: Any, body: str, assignee: Any) -> list[str]:
    mentions = list(dict.fromkeys((match.group(1) for match in host._PROFILE_MENTION.finditer(body or ''))))
    if mentions:
        return mentions
    assignee_name = str(assignee or '').strip()
    return [assignee_name] if assignee_name else []


def _comment_wake_message(host: Any, delivery: dict[str, Any], target_profile: str) -> str:
    return json.dumps({'schema': host._COMMENT_WAKE_SCHEMA, 'board': delivery['board'], 'source_task_id': delivery['task_id'], 'source_comment_id': int(delivery['comment_id']), 'reply_to_comment_id': delivery['reply_to_comment_id'], 'target_profile': target_profile, 'instruction': 'Read the source task and canonical comment thread, answer the comment, then use kwc_reply with source_comment_id as reply_to_comment_id.'}, ensure_ascii=False, sort_keys=True)


def _profile_wake_message(host: Any, wake: dict[str, Any]) -> str:
    response_tool = 'kwc_pm_response' if wake['profile'] == str(host._CONTEXT.get_config('coordinator_profile', 'project-manager') or 'project-manager') else 'kwc_profile_response'
    return json.dumps({'schema': host._PROFILE_WAKE_SCHEMA, 'action_id': wake['action_id'], 'board': wake['board'], 'source_task_id': wake['task_id'], 'target_profile': wake['profile'], 'skill': wake['skill'], 'invocation_id': wake['invocation_id'], 'instruction': f'Freshly read the canonical board, task, runs, events, comments, and Git state; perform the requested skill and submit exactly one typed result with {response_tool}.'}, ensure_ascii=False, sort_keys=True)


def _is_run_workflow_invocation(host: Any, user_message: Any) -> bool:
    text = str(user_message or '').lstrip()
    return bool(re.match('^/run-workflow(?:\\s|$)', text)) or text.startswith(f'[IMPORTANT: {host._RUN_WORKFLOW_MARKER}.')


def _current_session_key(host: Any) -> str:
    try:
        from gateway.session_context import get_session_env
        session_key = str(get_session_env('HERMES_SESSION_KEY', '') or '').strip()
        if session_key:
            return session_key
        return str(get_session_env('HERMES_SESSION_ID', '') or '').strip()
    except ImportError:
        return ''


def _current_session_identifiers(host: Any) -> list[str]:
    values = [host._current_session_key()]
    try:
        from gateway.session_context import get_session_env
        values.append(str(get_session_env('HERMES_SESSION_ID', '') or '').strip())
    except ImportError:
        pass
    return list(dict.fromkeys((value for value in values if value)))


def _assignment_context(host: Any, service: Any, assignment_id: Any) -> tuple[str, dict[str, Any]]:
    if host._current_execution_profile() != 'project-manager':
        raise ValueError('caller profile does not own assignment')
    session_ids = host._current_session_identifiers()
    if not session_ids:
        raise ValueError('authoring assignment requires a bound session')
    assignment = service.read_assignment_by_id(state_path=host._state_path(), assignment_id=assignment_id)
    if assignment is None:
        raise ValueError('assignment is stale or unknown')
    if assignment.get('profile') != 'project-manager':
        raise ValueError('assignment profile is invalid')
    return (session_ids[0], assignment)


def _current_session_profile(host: Any) -> str:
    try:
        from gateway.session_context import get_session_env
        profile = str(get_session_env('HERMES_SESSION_PROFILE', '') or '').strip()
    except ImportError:
        profile = ''
    if profile:
        return profile
    configured = str(os.getenv('HERMES_PROFILE_NAME') or os.getenv('HERMES_PROFILE') or '').strip()
    if configured:
        return configured
    try:
        from hermes_constants import get_hermes_home
        profile_name = get_hermes_home().name.strip()
        if profile_name:
            return profile_name
    except ImportError:
        pass
    return host._profile_home().name


def _current_execution_profile(host: Any) -> str:
    execution_role = str(os.environ.get('HERMES_KWC_EXECUTION_ROLE', '') or '').strip()
    return execution_role or host._current_session_profile()


def _pm_response_available(host: Any) -> bool:
    if host._CONTEXT is None:
        return False
    coordinator_profile = str(host._CONTEXT.get_config('coordinator_profile', 'project-manager') or 'project-manager').strip()
    return host._current_execution_profile() == coordinator_profile


def _profile_response_available(host: Any) -> bool:
    if host._CONTEXT is None:
        return False
    raw = host._CONTEXT.get_config('execution_profiles', 'project-manager,coder,reviewer')
    if isinstance(raw, (list, tuple, set)):
        values = raw
    else:
        values = str(raw or '').split(',')
    profiles = {str(value).strip() for value in values if str(value).strip()}
    current_profile = host._current_execution_profile()
    coordinator_profile = str(host._CONTEXT.get_config('coordinator_profile', 'project-manager') or 'project-manager').strip()
    return current_profile in profiles and current_profile != coordinator_profile


def _configured_comment_profiles(host: Any) -> set[str]:
    raw = host._CONTEXT.get_config('comment_profiles', 'project-manager,coder,reviewer') if host._CONTEXT is not None else ''
    if isinstance(raw, (list, tuple, set)):
        values = raw
    else:
        values = str(raw or '').split(',')
    return {str(value).strip() for value in values if str(value).strip()}


def _is_actionable(host: Any, event: dict[str, Any], coordinator_profile: str, decision_comment_author: str) -> bool:
    kind = event.get('kind')
    event_profile = str(event.get('event_profile') or '').strip()
    task_body = event.get('task_body') if isinstance(event.get('task_body'), dict) else {}
    task_schema = task_body.get('schema')
    if kind in {'blocked', 'block_loop_detected'}:
        if event_profile == coordinator_profile:
            return False
        return event_profile == host._IMPLEMENTATION_PROFILE and task_schema in host._IMPLEMENTATION_SCHEMAS or (event_profile == host._REVIEW_PROFILE and task_schema == host._REVIEW_SCHEMA)
    if kind == 'completed':
        return event_profile != coordinator_profile and event_profile == host._REVIEW_PROFILE and (task_schema == host._REVIEW_SCHEMA)
    if kind != 'commented':
        return False
    payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
    author = str(payload.get('author') or '').strip()
    return event.get('status') == 'blocked' and event.get('assignee') == coordinator_profile and (task_body.get('schema') == host._DECISION_SCHEMA) and (author == decision_comment_author)


def _wake_message(host: Any, board: str, event: dict[str, Any]) -> str:
    return json.dumps({'schema': host._WAKE_SCHEMA, 'board': board, 'source_task_id': event['task_id'], 'source_event_id': event['id'], 'event_kind': event['kind'], 'instruction': 'Load run-workflow, then read back the source task, runs, comments, events, and current board state before choosing the next transition.'}, ensure_ascii=False, sort_keys=True)


def _resolved_board(host: Any, board: Any) -> str:
    requested = str(board or '').strip()
    if requested:
        return requested
    from hermes_cli import kanban_db
    return str(kanban_db.get_current_board())
