"""Durable, single-use response ledger for KWC Profile invocations."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any


_INVOCATION_SCHEMA = "kwc-profile-invocation-v1"
_REASON_CODE = re.compile(r"^[A-Z][A-Z0-9_]*(?::[A-Z0-9_]+)*$")


def _now() -> int:
    return int(time.time())


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _reason_code(value: Any) -> str | None:
    code = _optional_text(value)
    if code is not None and not _REASON_CODE.fullmatch(code):
        raise ValueError("reason_code must be a compact stable identifier")
    return code


def _normalized_outcomes(values: Iterable[Any]) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise ValueError("allowed_outcomes must be a collection of outcome strings")
    outcomes: list[str] = []
    seen: set[str] = set()
    for value in values:
        outcome = str(value or "").strip()
        if outcome and outcome not in seen:
            outcomes.append(outcome)
            seen.add(outcome)
    if not outcomes:
        raise ValueError("allowed_outcomes must not be empty")
    return outcomes


def _connect(state_path: Path) -> sqlite3.Connection:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(state_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS workflow_invocation (
            invocation_id TEXT PRIMARY KEY,
            schema TEXT NOT NULL,
            workflow_instance_id TEXT NOT NULL,
            board TEXT NOT NULL,
            profile TEXT NOT NULL,
            session_key TEXT NOT NULL,
            expected_response TEXT NOT NULL,
            allowed_outcomes_json TEXT NOT NULL,
            snapshot_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL,
            outcome TEXT,
            task_id TEXT,
            reason_code TEXT,
            created_at INTEGER NOT NULL,
            responded_at INTEGER
        )
        """
    )
    return conn


def _public_payload(row: sqlite3.Row, *, idempotent_replay: bool | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "schema": str(row["schema"]),
        "invocation_id": str(row["invocation_id"]),
        "workflow_instance_id": str(row["workflow_instance_id"]),
        "board": str(row["board"]),
        "profile": str(row["profile"]),
        "expected_response": str(row["expected_response"]),
        "snapshot_fingerprint": str(row["snapshot_fingerprint"]),
        "status": str(row["status"]),
        "outcome": row["outcome"],
        "task_id": row["task_id"],
        "reason_code": row["reason_code"],
        "created_at": int(row["created_at"]),
        "responded_at": int(row["responded_at"]) if row["responded_at"] is not None else None,
    }
    if idempotent_replay is not None:
        payload["idempotent_replay"] = idempotent_replay
    return payload


def create_invocation(
    *,
    state_path: Path,
    invocation_id: Any,
    workflow_instance_id: Any,
    board: Any,
    profile: Any,
    session_key: Any,
    expected_response: Any,
    allowed_outcomes: Iterable[Any],
    snapshot_fingerprint: Any,
    now_fn: Callable[[], int] = _now,
) -> dict[str, Any]:
    """Create one pending invocation or replay the identical creation request."""
    contract = {
        "invocation_id": _required_text(invocation_id, "invocation_id"),
        "workflow_instance_id": _required_text(workflow_instance_id, "workflow_instance_id"),
        "board": _required_text(board, "board"),
        "profile": _required_text(profile, "profile"),
        "session_key": _required_text(session_key, "session_key"),
        "expected_response": _required_text(expected_response, "expected_response"),
        "allowed_outcomes_json": json.dumps(
            _normalized_outcomes(allowed_outcomes), ensure_ascii=False, separators=(",", ":")
        ),
        "snapshot_fingerprint": _required_text(snapshot_fingerprint, "snapshot_fingerprint"),
    }
    conn = _connect(state_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM workflow_invocation WHERE invocation_id=?",
            (contract["invocation_id"],),
        ).fetchone()
        if existing is not None:
            existing_contract = {
                key: existing[key]
                for key in (
                    "invocation_id",
                    "workflow_instance_id",
                    "board",
                    "profile",
                    "session_key",
                    "expected_response",
                    "allowed_outcomes_json",
                    "snapshot_fingerprint",
                )
            }
            if existing_contract != contract:
                raise ValueError("invocation_id was already used for a different contract")
            conn.commit()
            return _public_payload(existing)

        conn.execute(
            """
            INSERT INTO workflow_invocation(
                invocation_id, schema, workflow_instance_id, board, profile, session_key,
                expected_response, allowed_outcomes_json, snapshot_fingerprint, status, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                contract["invocation_id"],
                _INVOCATION_SCHEMA,
                contract["workflow_instance_id"],
                contract["board"],
                contract["profile"],
                contract["session_key"],
                contract["expected_response"],
                contract["allowed_outcomes_json"],
                contract["snapshot_fingerprint"],
                int(now_fn()),
            ),
        )
        row = conn.execute(
            "SELECT * FROM workflow_invocation WHERE invocation_id=?",
            (contract["invocation_id"],),
        ).fetchone()
        conn.commit()
        return _public_payload(row)
    finally:
        conn.close()


def submit_profile_response(
    *,
    state_path: Path,
    invocation_id: Any,
    caller_profile: Any,
    caller_session_key: Any,
    outcome: Any,
    task_id: Any = None,
    reason_code: Any = None,
    now_fn: Callable[[], int] = _now,
) -> dict[str, Any]:
    """Accept exactly one Profile/session-bound typed response without mutating Kanban."""
    invocation_id = _required_text(invocation_id, "invocation_id")
    caller_profile = _required_text(caller_profile, "caller_profile")
    caller_session_key = _required_text(caller_session_key, "caller_session_key")
    outcome = _required_text(outcome, "outcome")
    response = {
        "outcome": outcome,
        "task_id": _optional_text(task_id),
        "reason_code": _reason_code(reason_code),
    }

    conn = _connect(state_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM workflow_invocation WHERE invocation_id=?", (invocation_id,)
        ).fetchone()
        if row is None:
            raise ValueError("unknown invocation_id")
        if str(row["profile"]) != caller_profile:
            raise ValueError("invocation profile does not match the caller profile")
        if str(row["session_key"]) != caller_session_key:
            raise ValueError("invocation session does not match the caller session")
        allowed_outcomes = json.loads(str(row["allowed_outcomes_json"]))
        if outcome not in allowed_outcomes:
            raise ValueError("outcome is not allowed for this invocation")

        if str(row["status"]) == "accepted":
            stored = {
                "outcome": row["outcome"],
                "task_id": row["task_id"],
                "reason_code": row["reason_code"],
            }
            if stored != response:
                raise ValueError("invocation was already accepted with a different response")
            conn.commit()
            return _public_payload(row, idempotent_replay=True)
        if str(row["status"]) != "pending":
            raise ValueError("invocation is not pending")

        conn.execute(
            """
            UPDATE workflow_invocation
            SET status='accepted', outcome=?, task_id=?, reason_code=?, responded_at=?
            WHERE invocation_id=? AND status='pending'
            """,
            (
                response["outcome"],
                response["task_id"],
                response["reason_code"],
                int(now_fn()),
                invocation_id,
            ),
        )
        accepted = conn.execute(
            "SELECT * FROM workflow_invocation WHERE invocation_id=?", (invocation_id,)
        ).fetchone()
        conn.commit()
        return _public_payload(accepted, idempotent_replay=False)
    finally:
        conn.close()


# Backward-compatible name for the coordinator-specific tool.
submit_pm_response = submit_profile_response


def read_invocation(*, state_path: Path, invocation_id: Any) -> dict[str, Any] | None:
    invocation_id = _required_text(invocation_id, "invocation_id")
    conn = _connect(state_path)
    try:
        row = conn.execute(
            "SELECT * FROM workflow_invocation WHERE invocation_id=?", (invocation_id,)
        ).fetchone()
        return _public_payload(row) if row is not None else None
    finally:
        conn.close()
