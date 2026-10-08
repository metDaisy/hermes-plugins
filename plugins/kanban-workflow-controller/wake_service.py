"""Durable privacy-safe delivery queue for KWC Profile wakeups."""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


_WAKE_SCHEMA = "kwc-profile-wake-v1"
_STABLE_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def _now() -> int:
    return int(time.time())


def _stable_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text or not _STABLE_TEXT.fullmatch(text):
        raise ValueError(f"{field} must be a compact stable identifier")
    return text


def _positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or (
        isinstance(value, float) and not value.is_integer()
    ):
        raise ValueError(f"{field} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return parsed


def _connect(state_path: Path) -> sqlite3.Connection:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(state_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        for attempt in range(5):
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS profile_wake (
                action_id TEXT PRIMARY KEY,
                schema TEXT NOT NULL,
                board TEXT NOT NULL,
                task_id TEXT NOT NULL,
                profile TEXT NOT NULL,
                skill TEXT NOT NULL,
                invocation_id TEXT NOT NULL,
                status TEXT NOT NULL,
                claim_generation INTEGER NOT NULL DEFAULT 0,
                claimant_id TEXT,
                created_at INTEGER NOT NULL,
                claimed_at INTEGER,
                claim_expires_at INTEGER,
                delivered_at INTEGER
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_profile_wake_queue "
            "ON profile_wake(board, status, created_at, action_id)"
        )
        return conn
    except Exception:
        conn.close()
        raise


def _payload(
    row: sqlite3.Row,
    *,
    claimed: bool | None = None,
    takeover: bool = False,
    idempotent_replay: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "schema": str(row["schema"]),
        "action_id": str(row["action_id"]),
        "board": str(row["board"]),
        "task_id": str(row["task_id"]),
        "profile": str(row["profile"]),
        "skill": str(row["skill"]),
        "invocation_id": str(row["invocation_id"]),
        "status": str(row["status"]),
        "claim_generation": int(row["claim_generation"]),
        "created_at": int(row["created_at"]),
        "claimed_at": int(row["claimed_at"]) if row["claimed_at"] is not None else None,
        "claim_expires_at": (
            int(row["claim_expires_at"])
            if row["claim_expires_at"] is not None
            else None
        ),
        "delivered_at": (
            int(row["delivered_at"]) if row["delivered_at"] is not None else None
        ),
        "takeover": takeover,
        "idempotent_replay": idempotent_replay,
    }
    if claimed is not None:
        payload["claimed"] = claimed
    return payload


def enqueue_profile_wake(
    *,
    state_path: Path,
    action_id: Any,
    board: Any,
    task_id: Any,
    profile: Any,
    skill: Any,
    invocation_id: Any,
    now_fn: Callable[[], int] = _now,
) -> dict[str, Any]:
    contract = {
        "action_id": _stable_text(action_id, "action_id"),
        "board": _stable_text(board, "board"),
        "task_id": _stable_text(task_id, "task_id"),
        "profile": _stable_text(profile, "profile"),
        "skill": _stable_text(skill, "skill"),
        "invocation_id": _stable_text(invocation_id, "invocation_id"),
    }
    now = int(now_fn())
    conn = _connect(state_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM profile_wake WHERE action_id=?", (contract["action_id"],)
        ).fetchone()
        if existing is not None:
            existing_contract = {
                key: str(existing[key])
                for key in ("action_id", "board", "task_id", "profile", "skill", "invocation_id")
            }
            if existing_contract != contract:
                raise ValueError("action_id is already bound to a different wake contract")
            conn.commit()
            return _payload(existing, idempotent_replay=True)
        conn.execute(
            """
            INSERT INTO profile_wake(
                action_id, schema, board, task_id, profile, skill,
                invocation_id, status, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                contract["action_id"],
                _WAKE_SCHEMA,
                contract["board"],
                contract["task_id"],
                contract["profile"],
                contract["skill"],
                contract["invocation_id"],
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM profile_wake WHERE action_id=?", (contract["action_id"],)
        ).fetchone()
        conn.commit()
        return _payload(row)
    finally:
        conn.close()


def claim_next_profile_wake(
    *,
    state_path: Path,
    board: Any,
    claimant_id: Any,
    ttl_seconds: Any,
    now_fn: Callable[[], int] = _now,
) -> dict[str, Any]:
    board = _stable_text(board, "board")
    claimant_id = _stable_text(claimant_id, "claimant_id")
    ttl_seconds = _positive_integer(ttl_seconds, "ttl_seconds")
    now = int(now_fn())
    conn = _connect(state_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        active = conn.execute(
            """
            SELECT * FROM profile_wake
            WHERE board=? AND status='claimed' AND claim_expires_at > ?
            ORDER BY claimed_at ASC, action_id ASC
            """,
            (board, now),
        ).fetchall()
        if len(active) > 1:
            raise ValueError("multiple active wake claims violate capacity one")
        if active:
            row = active[0]
            if str(row["claimant_id"]) == claimant_id:
                conn.commit()
                return _payload(row, claimed=True, idempotent_replay=True)
            conn.commit()
            return {
                "ok": True,
                "schema": _WAKE_SCHEMA,
                "claimed": False,
                "reason_code": "WAKE_HELD",
            }
        candidate = conn.execute(
            """
            SELECT * FROM profile_wake
            WHERE board=? AND (
                status='pending' OR (status='claimed' AND claim_expires_at <= ?)
            )
            ORDER BY created_at ASC, action_id ASC
            LIMIT 1
            """,
            (board, now),
        ).fetchone()
        if candidate is None:
            conn.commit()
            return {
                "ok": True,
                "schema": _WAKE_SCHEMA,
                "claimed": False,
                "reason_code": "WAKE_QUEUE_EMPTY",
            }
        takeover = str(candidate["status"]) == "claimed"
        generation = int(candidate["claim_generation"]) + 1
        conn.execute(
            """
            UPDATE profile_wake
            SET status='claimed', claim_generation=?, claimant_id=?,
                claimed_at=?, claim_expires_at=?
            WHERE action_id=?
            """,
            (
                generation,
                claimant_id,
                now,
                now + ttl_seconds,
                candidate["action_id"],
            ),
        )
        row = conn.execute(
            "SELECT * FROM profile_wake WHERE action_id=?", (candidate["action_id"],)
        ).fetchone()
        conn.commit()
        return _payload(row, claimed=True, takeover=takeover)
    finally:
        conn.close()


def complete_profile_wake(
    *,
    state_path: Path,
    action_id: Any,
    claimant_id: Any,
    claim_generation: Any,
    now_fn: Callable[[], int] = _now,
) -> dict[str, Any]:
    action_id = _stable_text(action_id, "action_id")
    claimant_id = _stable_text(claimant_id, "claimant_id")
    claim_generation = _positive_integer(claim_generation, "claim_generation")
    now = int(now_fn())
    conn = _connect(state_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM profile_wake WHERE action_id=?", (action_id,)
        ).fetchone()
        if row is None:
            raise ValueError("unknown action_id")
        matches = (
            str(row["claimant_id"] or "") == claimant_id
            and int(row["claim_generation"]) == claim_generation
        )
        if str(row["status"]) == "delivered":
            if not matches:
                raise ValueError("wake was delivered by a different claimant or generation")
            conn.commit()
            return _payload(row, claimed=True, idempotent_replay=True)
        if str(row["status"]) != "claimed" or not matches:
            raise ValueError("wake claim does not match claimant or generation")
        if row["claim_expires_at"] is None or now >= int(row["claim_expires_at"]):
            raise ValueError("wake claim has expired")
        conn.execute(
            "UPDATE profile_wake SET status='delivered', delivered_at=? WHERE action_id=?",
            (now, action_id),
        )
        delivered = conn.execute(
            "SELECT * FROM profile_wake WHERE action_id=?", (action_id,)
        ).fetchone()
        conn.commit()
        return _payload(delivered, claimed=True)
    finally:
        conn.close()


def read_profile_wakes(*, state_path: Path, board: Any | None = None) -> list[dict[str, Any]]:
    conn = _connect(state_path)
    try:
        if board is None:
            rows = conn.execute(
                "SELECT * FROM profile_wake ORDER BY created_at ASC, action_id ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM profile_wake WHERE board=? ORDER BY created_at ASC, action_id ASC",
                (_stable_text(board, "board"),),
            ).fetchall()
        return [_payload(row) for row in rows]
    finally:
        conn.close()


def _table_columns(state_path: Path) -> set[str]:
    conn = _connect(state_path)
    try:
        return {str(row["name"]) for row in conn.execute("PRAGMA table_info(profile_wake)")}
    finally:
        conn.close()
