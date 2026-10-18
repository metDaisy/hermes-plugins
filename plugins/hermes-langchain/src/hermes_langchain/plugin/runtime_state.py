"""SQLite-backed durable runtime state operations."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any

def _connect_state(host: Any) -> sqlite3.Connection:
    path = host._state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
    conn.execute('CREATE TABLE IF NOT EXISTS binding (singleton INTEGER PRIMARY KEY CHECK (singleton = 1), schema TEXT NOT NULL, board TEXT NOT NULL, coordinator_session_key TEXT NOT NULL, last_event_id INTEGER NOT NULL, bound_at INTEGER NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS runtime_heartbeat (singleton INTEGER PRIMARY KEY CHECK (singleton = 1), board TEXT NOT NULL, last_dispatch_tick_at INTEGER NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS activity_log (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL, board TEXT NOT NULL, action TEXT NOT NULL, outcome TEXT NOT NULL, reason TEXT NOT NULL, task_id TEXT, source_event_id INTEGER, event_kind TEXT)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_kwc_activity_created_at ON activity_log(created_at DESC)')
    conn.execute('CREATE TABLE IF NOT EXISTS coordinator_binding (singleton INTEGER PRIMARY KEY CHECK (singleton = 1), schema TEXT NOT NULL, coordinator_session_key TEXT NOT NULL, bound_at INTEGER NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS board_cursor (board TEXT PRIMARY KEY, last_event_id INTEGER NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS board_heartbeat (board TEXT PRIMARY KEY, last_dispatch_tick_at INTEGER NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS profile_session_binding (profile TEXT PRIMARY KEY, session_key TEXT NOT NULL, bound_at INTEGER NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS comment_delivery (id INTEGER PRIMARY KEY AUTOINCREMENT, board TEXT NOT NULL, task_id TEXT NOT NULL, comment_id INTEGER NOT NULL, author TEXT NOT NULL, reply_to_comment_id INTEGER, route_requested INTEGER NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, UNIQUE(board, comment_id))')
    conn.execute('CREATE TABLE IF NOT EXISTS comment_delivery_target (delivery_id INTEGER NOT NULL, profile TEXT NOT NULL, status TEXT NOT NULL, updated_at INTEGER NOT NULL, PRIMARY KEY(delivery_id, profile), FOREIGN KEY(delivery_id) REFERENCES comment_delivery(id) ON DELETE CASCADE)')
    legacy_binding = conn.execute('SELECT board, coordinator_session_key, last_event_id, bound_at FROM binding WHERE singleton = 1').fetchone()
    if legacy_binding is not None:
        conn.execute('INSERT OR IGNORE INTO coordinator_binding(singleton, schema, coordinator_session_key, bound_at) VALUES(1, ?, ?, ?)', (host._STATE_SCHEMA, legacy_binding['coordinator_session_key'], legacy_binding['bound_at']))
        conn.execute('INSERT OR IGNORE INTO board_cursor(board, last_event_id) VALUES(?, ?)', (legacy_binding['board'], legacy_binding['last_event_id']))
    legacy_heartbeat = conn.execute('SELECT board, last_dispatch_tick_at FROM runtime_heartbeat WHERE singleton = 1').fetchone()
    if legacy_heartbeat is not None:
        conn.execute('INSERT OR IGNORE INTO board_heartbeat(board, last_dispatch_tick_at) VALUES(?, ?)', (legacy_heartbeat['board'], legacy_heartbeat['last_dispatch_tick_at']))
    conn.commit()
    return conn


def _insert_activity(host: Any, conn: sqlite3.Connection, *, board: str, action: str, outcome: str, reason: str, task_id: str | None=None, source_event_id: int | None=None, event_kind: str | None=None) -> None:
    now = host._now()
    conn.execute('INSERT INTO activity_log(created_at, board, action, outcome, reason, task_id, source_event_id, event_kind) VALUES(?, ?, ?, ?, ?, ?, ?, ?)', (now, board, action, outcome, reason, task_id, source_event_id, event_kind))
    conn.execute('DELETE FROM activity_log WHERE created_at < ?', (now - host._ACTIVITY_RETENTION_SECONDS,))


def _record_activity(host: Any, **fields: Any) -> None:
    try:
        with closing(host._connect_state()) as conn:
            host._insert_activity(conn, **fields)
            conn.commit()
    except (OSError, sqlite3.Error):
        return


def _read_activity(host: Any, limit: int=100) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(int(limit), 200))
    try:
        with closing(host._connect_state()) as conn:
            rows = conn.execute('SELECT id, created_at, board, action, outcome, reason, task_id, source_event_id, event_kind FROM activity_log ORDER BY id DESC LIMIT ?', (bounded_limit,)).fetchall()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return []
    return [dict(row) for row in rows]


def _bind_profile_session(host: Any, profile: str, session_key: str) -> None:
    profile = str(profile or '').strip()
    session_key = str(session_key or '').strip()
    if not profile or not session_key:
        return
    with closing(host._connect_state()) as conn:
        conn.execute('INSERT INTO profile_session_binding(profile, session_key, bound_at) VALUES(?, ?, ?) ON CONFLICT(profile) DO UPDATE SET session_key=excluded.session_key, bound_at=excluded.bound_at', (profile, session_key, host._now()))
        conn.commit()


def _enqueue_comment_delivery(host: Any, *, board: str, task_id: str, comment_id: int, author: str, reply_to_comment_id: int | None, route_requested: bool) -> int:
    now = host._now()
    with closing(host._connect_state()) as conn:
        conn.execute("INSERT OR IGNORE INTO comment_delivery(board, task_id, comment_id, author, reply_to_comment_id, route_requested, status, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, 'pending', ?, ?)", (str(board), str(task_id), int(comment_id), str(author), int(reply_to_comment_id) if reply_to_comment_id is not None else None, 1 if route_requested else 0, now, now))
        row = conn.execute('SELECT id FROM comment_delivery WHERE board = ? AND comment_id = ?', (str(board), int(comment_id))).fetchone()
        conn.commit()
        return int(row['id'])


def _read_comment_delivery(host: Any, delivery_id: int) -> dict[str, Any] | None:
    with closing(host._connect_state()) as conn:
        row = conn.execute('SELECT * FROM comment_delivery WHERE id = ?', (int(delivery_id),)).fetchone()
    return dict(row) if row is not None else None


def _profile_session_key(host: Any, profile: str) -> str:
    coordinator_profile = str(host._CONTEXT.get_config('coordinator_profile', 'project-manager') or 'project-manager')
    with closing(host._connect_state()) as conn:
        if profile == coordinator_profile:
            row = conn.execute('SELECT coordinator_session_key AS session_key FROM coordinator_binding WHERE singleton=1').fetchone()
        else:
            row = conn.execute('SELECT session_key FROM profile_session_binding WHERE profile=?', (profile,)).fetchone()
    return str(row['session_key'] or '').strip() if row is not None else ''


def _mark_dispatch_heartbeat(host: Any, board: str) -> None:
    with closing(host._connect_state()) as conn:
        previous = conn.execute('SELECT board FROM board_heartbeat WHERE board = ?', (board,)).fetchone()
        conn.execute('INSERT INTO board_heartbeat(board, last_dispatch_tick_at) VALUES(?, ?) ON CONFLICT(board) DO UPDATE SET last_dispatch_tick_at=excluded.last_dispatch_tick_at', (board, host._now()))
        if previous is None:
            host._insert_activity(conn, board=board, action='board_monitoring', outcome='active', reason='configured_board_dispatch_tick')
        conn.commit()


def _read_dispatch_heartbeat(host: Any) -> dict[str, Any] | None:
    try:
        with closing(host._connect_state()) as conn:
            row = conn.execute('SELECT board, last_dispatch_tick_at FROM board_heartbeat ORDER BY last_dispatch_tick_at DESC LIMIT 1').fetchone()
    except (OSError, sqlite3.Error):
        return None
    return dict(row) if row is not None else None


def _bind_state(host: Any, board_or_session_key: str, coordinator_session_key: str | None=None, baseline_event_id: int | None=None) -> None:
    board = board_or_session_key if coordinator_session_key is not None else ''
    session_key = coordinator_session_key or board_or_session_key
    conn = host._connect_state()
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('INSERT INTO coordinator_binding(singleton, schema, coordinator_session_key, bound_at) VALUES(1, ?, ?, ?) ON CONFLICT(singleton) DO UPDATE SET schema=excluded.schema, coordinator_session_key=excluded.coordinator_session_key, bound_at=excluded.bound_at', (host._STATE_SCHEMA, session_key, host._now()))
        if board:
            existing = conn.execute('SELECT last_event_id FROM board_cursor WHERE board = ?', (board,)).fetchone()
            cursor = max(int(baseline_event_id or 0), int(existing['last_event_id']) if existing else 0)
            conn.execute('INSERT INTO board_cursor(board, last_event_id) VALUES(?, ?) ON CONFLICT(board) DO UPDATE SET last_event_id=excluded.last_event_id', (board, cursor))
        host._insert_activity(conn, board=board or '*', action='session_bound', outcome='accepted', reason='coordinator_run_workflow_invocation')
        conn.commit()
    except (OSError, sqlite3.Error):
        conn.rollback()
        raise
    finally:
        conn.close()


def _read_state(host: Any, board: str | None=None) -> dict[str, Any] | None:
    try:
        with closing(host._connect_state()) as conn:
            row = conn.execute('SELECT schema, coordinator_session_key, bound_at FROM coordinator_binding WHERE singleton = 1').fetchone()
            cursor = conn.execute('SELECT last_event_id FROM board_cursor WHERE board = ?', (board,)).fetchone() if board else None
    except (OSError, sqlite3.Error):
        return None
    if row is None or row['schema'] != host._STATE_SCHEMA:
        return None
    if not str(row['coordinator_session_key'] or '').strip():
        return None
    result = dict(row)
    if board:
        if cursor is None:
            return None
        result['board'] = board
        result['last_event_id'] = int(cursor['last_event_id'])
    return result


def _ensure_board_cursor(host: Any, board: str, baseline_event_id: int) -> None:
    with closing(host._connect_state()) as conn:
        conn.execute('INSERT OR IGNORE INTO board_cursor(board, last_event_id) VALUES(?, ?)', (board, max(0, int(baseline_event_id))))
        conn.commit()
