"""Kanban event and pending-delivery dispatch orchestration."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from typing import Any

def _process_pending_profile_wakes(host: Any, board: str) -> None:
    if host._CONTEXT is None:
        return
    wake_service = host._load_wake_service()
    claimant_id = f'gateway:{board}'
    wake = wake_service.claim_next_profile_wake(state_path=host._state_path(), board=board, claimant_id=claimant_id, ttl_seconds=30, now_fn=host._now)
    if not bool(wake.get('claimed')):
        return
    session_key = host._profile_session_key(str(wake['profile']))
    if not session_key:
        return
    accepted = host._CONTEXT.inject_message(host._profile_wake_message(wake), session_key=session_key)
    if not accepted:
        return
    wake_service.complete_profile_wake(state_path=host._state_path(), action_id=wake['action_id'], claimant_id=claimant_id, claim_generation=wake['claim_generation'], now_fn=host._now)


def _process_pending_comment_deliveries(host: Any, board: str) -> None:
    if host._CONTEXT is None:
        return
    conn = host._connect_state()
    try:
        conn.execute('BEGIN IMMEDIATE')
        deliveries = conn.execute("SELECT * FROM comment_delivery WHERE board = ? AND status IN ('pending', 'retry_pending') ORDER BY id ASC LIMIT 100", (str(board),)).fetchall()
        for row in deliveries:
            delivery = dict(row)
            delivery_id = int(delivery['id'])
            if not bool(delivery['route_requested']):
                conn.execute("UPDATE comment_delivery SET status='suppressed', updated_at=? WHERE id=?", (host._now(), delivery_id))
                continue
            target_count = conn.execute('SELECT COUNT(*) AS value FROM comment_delivery_target WHERE delivery_id=?', (delivery_id,)).fetchone()
            if int(target_count['value'] or 0) == 0:
                context = host._read_comment_context(str(delivery['board']), str(delivery['task_id']), int(delivery['comment_id']))
                if context is None:
                    conn.execute("UPDATE comment_delivery SET status='retry_pending', updated_at=? WHERE id=?", (host._now(), delivery_id))
                    continue
                targets = host._comment_targets(str(context.get('body') or ''), context.get('assignee'))
                if not targets:
                    conn.execute("UPDATE comment_delivery SET status='suppressed', updated_at=? WHERE id=?", (host._now(), delivery_id))
                    continue
                for profile in targets:
                    conn.execute("INSERT INTO comment_delivery_target(delivery_id, profile, status, updated_at) VALUES(?, ?, 'pending', ?)", (delivery_id, profile, host._now()))
            pending_targets = conn.execute("SELECT profile FROM comment_delivery_target WHERE delivery_id=? AND status != 'delivered' ORDER BY rowid ASC", (delivery_id,)).fetchall()
            for target in pending_targets:
                profile = str(target['profile'])
                binding = conn.execute('SELECT session_key FROM profile_session_binding WHERE profile=?', (profile,)).fetchone()
                if binding is None:
                    conn.execute("UPDATE comment_delivery_target SET status='retry_pending', updated_at=? WHERE delivery_id=? AND profile=?", (host._now(), delivery_id, profile))
                    continue
                accepted = host._CONTEXT.inject_message(host._comment_wake_message(delivery, profile), session_key=str(binding['session_key']))
                conn.execute('UPDATE comment_delivery_target SET status=?, updated_at=? WHERE delivery_id=? AND profile=?', ('delivered' if accepted else 'retry_pending', host._now(), delivery_id, profile))
            remaining = conn.execute("SELECT COUNT(*) AS value FROM comment_delivery_target WHERE delivery_id=? AND status != 'delivered'", (delivery_id,)).fetchone()
            status = 'delivered' if int(remaining['value'] or 0) == 0 else 'retry_pending'
            conn.execute('UPDATE comment_delivery SET status=?, updated_at=? WHERE id=?', (status, host._now(), delivery_id))
        conn.commit()
    except (OSError, sqlite3.Error):
        conn.rollback()
    finally:
        conn.close()


def _latest_event_id(host: Any, board: str) -> int:
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect(board=board)
    try:
        row = conn.execute('SELECT COALESCE(MAX(id), 0) AS value FROM task_events').fetchone()
        return int(row['value'] if row else 0)
    finally:
        conn.close()


def _process_event(host: Any, state: dict[str, Any], event: dict[str, Any], actionable: bool, wake_message: str | None) -> bool:
    """Serialize cursor advancement and delivery against concurrent ticks/rebinding."""
    conn = host._connect_state()
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT schema, coordinator_session_key FROM coordinator_binding WHERE singleton = 1').fetchone()
        cursor = conn.execute('SELECT last_event_id FROM board_cursor WHERE board = ?', (state['board'],)).fetchone()
        if row is None or cursor is None or row['schema'] != host._STATE_SCHEMA or (row['coordinator_session_key'] != state['coordinator_session_key']) or (int(cursor['last_event_id']) != int(state['last_event_id'])):
            conn.rollback()
            return False
        if actionable:
            accepted = host._CONTEXT.inject_message(str(wake_message or ''), session_key=row['coordinator_session_key'])
            if not accepted:
                host._insert_activity(conn, board=str(state['board']), action='event_delivery', outcome='retry_pending', reason='session_injection_rejected', task_id=str(event['task_id']), source_event_id=int(event['id']), event_kind=str(event['kind']))
                conn.commit()
                return False
        host._insert_activity(conn, board=str(state['board']), action='event_routed' if actionable else 'event_ignored', outcome='accepted' if actionable else 'filtered', reason='actionable_workflow_event' if actionable else 'not_actionable', task_id=str(event['task_id']), source_event_id=int(event['id']), event_kind=str(event['kind']))
        conn.execute('UPDATE board_cursor SET last_event_id = ? WHERE board = ?', (int(event['id']), state['board']))
        conn.commit()
        state['last_event_id'] = int(event['id'])
        return True
    except (OSError, sqlite3.Error):
        conn.rollback()
        return False
    finally:
        conn.close()


def _read_events(host: Any, board: str, after_id: int) -> list[dict[str, Any]]:
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect(board=board)
    try:
        rows = conn.execute('SELECT e.id, e.task_id, e.kind, e.payload, e.run_id, t.status, t.assignee, t.body, r.profile AS event_profile FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id LEFT JOIN task_runs r ON r.id = e.run_id WHERE e.id > ? ORDER BY e.id ASC LIMIT 200', (int(after_id),)).fetchall()
    finally:
        conn.close()
    events: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row['payload']) if row['payload'] else {}
        except (json.JSONDecodeError, TypeError, ValueError):
            payload = {}
        try:
            task_body = json.loads(row['body']) if row['body'] else {}
        except (json.JSONDecodeError, TypeError, ValueError):
            task_body = {}
        events.append({'id': int(row['id']), 'task_id': str(row['task_id']), 'kind': str(row['kind']), 'run_id': row['run_id'], 'event_profile': row['event_profile'], 'payload': payload if isinstance(payload, dict) else {}, 'status': row['status'], 'assignee': row['assignee'], 'task_body': task_body if isinstance(task_body, dict) else {}})
    return events


def _on_pre_llm_call(host: Any, **kwargs: Any) -> None:
    if host._CONTEXT is None:
        return
    session_key = host._current_session_key()
    session_profile = host._current_execution_profile()
    if not kwargs.get('parent_session_id') and session_key and (session_profile in host._configured_comment_profiles()):
        host._bind_profile_session(session_profile, session_key)
    if not host._is_run_workflow_invocation(kwargs.get('user_message')):
        return
    coordinator_profile = str(host._CONTEXT.get_config('coordinator_profile', 'project-manager') or 'project-manager').strip()
    if not session_key or session_profile != coordinator_profile:
        return
    current = host._read_state()
    if kwargs.get('parent_session_id') and (current is None or current.get('coordinator_session_key') != session_key):
        return
    host._bind_state(session_key)


def _process_pending_events(host: Any, board: str) -> None:
    state = host._read_state(board)
    if state is None:
        return
    coordinator_profile = str(host._CONTEXT.get_config('coordinator_profile', 'project-manager') or 'project-manager').strip()
    decision_comment_author = str(host._CONTEXT.get_config('decision_comment_author', 'dashboard') or 'dashboard').strip()
    for event in host._read_events(board, int(state['last_event_id'])):
        actionable = host._is_actionable(event, coordinator_profile, decision_comment_author)
        wake_message = host._wake_message(board, event) if actionable else None
        if not host._process_event(state, event, actionable, wake_message):
            break


def _find_hint_event_id(host: Any, board: str, task_id: str, event_kind: str, run_id: Any) -> int:
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect(board=board)
    try:
        clauses = ['task_id=?', 'kind=?']
        values: list[Any] = [task_id, event_kind]
        if run_id is not None:
            clauses.append('run_id=?')
            values.append(int(run_id))
        row = conn.execute('SELECT id FROM task_events WHERE ' + ' AND '.join(clauses) + ' ORDER BY id DESC LIMIT 1', tuple(values)).fetchone()
        if row is not None:
            return int(row['id'])
    finally:
        conn.close()
    return host._latest_event_id(board)


def _on_event_hint(host: Any, **kwargs: Any) -> None:
    if host._CONTEXT is None:
        return
    board = str(kwargs.get('board') or '').strip()
    if board:
        if host._read_state() is None:
            host._record_activity(board=board, action='event_hint_skipped', outcome='not_delivered', reason='no_coordinator_binding', task_id=str(kwargs.get('task_id') or '') or None, event_kind=str(kwargs.get('event_kind') or '') or None)
            return
        if host._read_state(board) is None:
            event_id = host._find_hint_event_id(board, str(kwargs.get('task_id') or ''), str(kwargs.get('event_kind') or ''), kwargs.get('run_id'))
            host._ensure_board_cursor(board, max(0, event_id - 1))
        host._process_pending_events(board)


def _on_dispatch_tick(host: Any, **kwargs: Any) -> None:
    if kwargs.get('dry_run') is True or host._CONTEXT is None:
        return
    board = str(kwargs.get('board') or '').strip()
    if not board:
        return
    host._mark_dispatch_heartbeat(board)
    host._process_pending_profile_wakes(board)
    host._process_pending_comment_deliveries(board)
    if host._read_state() is not None and host._read_state(board) is None:
        host._ensure_board_cursor(board, host._latest_event_id(board))
    host._process_pending_events(board)
