"""Shared native-comment write path for KWC tools and dashboard API."""

from __future__ import annotations

import sqlite3
import time
from hashlib import sha256
from pathlib import Path
from typing import Any


def _ensure_delivery_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS comment_delivery ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, board TEXT NOT NULL, task_id TEXT NOT NULL, "
        "comment_id INTEGER NOT NULL, author TEXT NOT NULL, reply_to_comment_id INTEGER, "
        "route_requested INTEGER NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL, "
        "updated_at INTEGER NOT NULL, UNIQUE(board, comment_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS comment_delivery_target ("
        "delivery_id INTEGER NOT NULL, profile TEXT NOT NULL, status TEXT NOT NULL, "
        "updated_at INTEGER NOT NULL, PRIMARY KEY(delivery_id, profile), "
        "FOREIGN KEY(delivery_id) REFERENCES comment_delivery(id) ON DELETE CASCADE)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS comment_request ("
        "board TEXT NOT NULL, request_key TEXT NOT NULL, task_id TEXT NOT NULL, "
        "author TEXT NOT NULL, body_sha256 TEXT NOT NULL, reply_to_comment_id INTEGER, "
        "route_requested INTEGER NOT NULL, status TEXT NOT NULL, comment_id INTEGER, "
        "delivery_id INTEGER, expected_comment_id INTEGER NOT NULL, "
        "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
        "PRIMARY KEY(board, request_key))"
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(comment_request)").fetchall()}
    if "expected_comment_id" not in columns:
        conn.execute("ALTER TABLE comment_request ADD COLUMN expected_comment_id INTEGER")


def _matching_recovery_comment_id(
    comments: list[Any], expected_comment_id: int, author: str, body_digest: str
) -> int | None:
    for comment in comments:
        if int(comment.id) != int(expected_comment_id):
            continue
        if comment.author != author:
            return None
        if sha256(comment.body.encode("utf-8")).hexdigest() != body_digest:
            return None
        return int(comment.id)
    return None


def enqueue_delivery(
    state_path: Path,
    *,
    board: str,
    task_id: str,
    comment_id: int,
    author: str,
    reply_to_comment_id: int | None,
    route_requested: bool,
) -> int:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(state_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_delivery_schema(conn)
        now = int(time.time())
        conn.execute(
            "INSERT OR IGNORE INTO comment_delivery("
            "board, task_id, comment_id, author, reply_to_comment_id, route_requested, "
            "status, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                board,
                task_id,
                int(comment_id),
                author,
                reply_to_comment_id,
                1 if route_requested else 0,
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT id FROM comment_delivery WHERE board=? AND comment_id=?",
            (board, int(comment_id)),
        ).fetchone()
        conn.commit()
        return int(row["id"])
    finally:
        conn.close()


def create_comment_request(
    state_path: Path,
    *,
    board: Any,
    task_id: Any,
    body: Any,
    author: Any,
    idempotency_key: Any,
    reply_to_comment_id: Any = None,
    route_requested: bool,
) -> dict[str, Any]:
    from hermes_cli import kanban_db
    from hermes_cli import kanban_db_connect as kbc

    resolved_board = str(board or "").strip()
    task_id = str(task_id or "").strip()
    body = str(body or "").strip()
    author = str(author or "").strip()
    request_key = str(idempotency_key or "").strip()
    if not resolved_board:
        raise ValueError("board is required")
    if not task_id:
        raise ValueError("task_id is required")
    if not body:
        raise ValueError("body is required")
    if not author:
        raise ValueError("author is required")
    if not request_key:
        raise ValueError("idempotency_key is required")

    reply_id = int(reply_to_comment_id) if reply_to_comment_id is not None else None
    body_digest = sha256(body.encode("utf-8")).hexdigest()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    conn = kbc.connect(board=resolved_board)
    recovered = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone() is None:
            raise ValueError(f"unknown task {task_id}")
        if reply_id is not None and conn.execute(
            "SELECT 1 FROM task_comments WHERE id=? AND task_id=?", (reply_id, task_id)
        ).fetchone() is None:
            raise ValueError("reply_to_comment_id must belong to task_id")
        next_comment_id = int(
            conn.execute(
                "SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name='task_comments'), 0) + 1"
            ).fetchone()[0]
        )

        state = sqlite3.connect(state_path, timeout=5.0)
        state.row_factory = sqlite3.Row
        try:
            _ensure_delivery_schema(state)
            state.execute("BEGIN IMMEDIATE")
            request = state.execute(
                "SELECT * FROM comment_request WHERE board=? AND request_key=?",
                (resolved_board, request_key),
            ).fetchone()
            now = int(time.time())
            if request is not None:
                if (
                    request["task_id"] != task_id
                    or request["author"] != author
                    or request["body_sha256"] != body_digest
                    or request["reply_to_comment_id"] != reply_id
                    or bool(request["route_requested"]) != bool(route_requested)
                ):
                    raise ValueError(
                        "idempotency_key was already used for a different comment request"
                    )
                if request["status"] == "completed":
                    state.commit()
                    conn.rollback()
                    return {
                        "ok": True,
                        "board": resolved_board,
                        "task_id": task_id,
                        "comment_id": int(request["comment_id"]),
                        "delivery_id": int(request["delivery_id"]),
                        "reply_to_comment_id": reply_id,
                        "idempotent_replay": True,
                    }
                expected_comment_id = int(request["expected_comment_id"] or 0)
                recovered_comment_id = _matching_recovery_comment_id(
                    kanban_db.list_comments(conn, task_id),
                    expected_comment_id,
                    author,
                    body_digest,
                )
                if recovered_comment_id is not None:
                    comment_id = recovered_comment_id
                    recovered = True
                else:
                    if (
                        request["status"] == "in_progress"
                        and now - int(request["updated_at"]) < 30
                    ):
                        raise ValueError("comment request is already in progress; retry shortly")
                    expected_comment_id = next_comment_id
                    state.execute(
                        "UPDATE comment_request SET status='in_progress', "
                        "expected_comment_id=?, updated_at=? WHERE board=? AND request_key=?",
                        (expected_comment_id, now, resolved_board, request_key),
                    )
            else:
                expected_comment_id = next_comment_id
                state.execute(
                    "INSERT INTO comment_request("
                    "board, request_key, task_id, author, body_sha256, reply_to_comment_id, "
                    "route_requested, status, expected_comment_id, created_at, updated_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, 'in_progress', ?, ?, ?)",
                    (
                        resolved_board,
                        request_key,
                        task_id,
                        author,
                        body_digest,
                        reply_id,
                        1 if route_requested else 0,
                        expected_comment_id,
                        now,
                        now,
                    ),
                )
            state.commit()
        finally:
            state.close()

        if recovered:
            conn.rollback()
        else:
            comment_id = kanban_db.add_comment(conn, task_id, author=author, body=body)
            if int(comment_id) != int(expected_comment_id):
                raise RuntimeError("native comment id did not match the reserved request id")
            conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()

    delivery_id = enqueue_delivery(
        state_path,
        board=resolved_board,
        task_id=task_id,
        comment_id=comment_id,
        author=author,
        reply_to_comment_id=reply_id,
        route_requested=route_requested,
    )
    state = sqlite3.connect(state_path, timeout=5.0)
    try:
        state.execute(
            "UPDATE comment_request SET status='completed', comment_id=?, delivery_id=?, updated_at=? "
            "WHERE board=? AND request_key=?",
            (comment_id, delivery_id, int(time.time()), resolved_board, request_key),
        )
        state.commit()
    finally:
        state.close()
    return {
        "ok": True,
        "board": resolved_board,
        "task_id": task_id,
        "comment_id": comment_id,
        "delivery_id": delivery_id,
        "reply_to_comment_id": reply_id,
        "idempotent_replay": recovered,
    }


def nudge_dispatch(board: str) -> None:
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    conn = kbc.connect(board=board)
    try:
        kbd.dispatch_once(conn, dry_run=False, max_spawn=0, board=board)
    finally:
        conn.close()
