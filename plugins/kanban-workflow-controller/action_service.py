"""Durable outbox and claim ledger for KWC workflow decisions."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


_ACTION_SCHEMA = "kwc-workflow-action-v1"
_SNAPSHOT_SCHEMA = "kwc-workflow-snapshot-v1"
_DECISION_SCHEMA = "kwc-workflow-decision-v1"
_ACTIONABLE_KINDS = {"dispatch-task", "wake-profile", "promote-task"}
_NO_ACTION_KINDS = {"wait-for-profile"}
_RESULT_CODE = re.compile(r"^[A-Z][A-Z0-9_]*(?::[A-Z0-9_]+)*$")
_STABLE_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def _now() -> int:
    return int(time.time())


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _stable_text(value: Any, field: str) -> str:
    text = _required_text(value, field)
    if not _STABLE_TEXT.fullmatch(text):
        raise ValueError(f"{field} must be a compact stable identifier")
    return text


def _positive_ttl(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("ttl_seconds must be a positive integer")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("ttl_seconds must be a positive integer")
    try:
        ttl_seconds = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("ttl_seconds must be a positive integer") from exc
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be a positive integer")
    return ttl_seconds


def _result_code(value: Any) -> str:
    code = _required_text(value, "result_code")
    if not _RESULT_CODE.fullmatch(code):
        raise ValueError("result_code must be a compact stable identifier")
    return code


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
            CREATE TABLE IF NOT EXISTS workflow_action (
                action_id TEXT PRIMARY KEY,
                schema TEXT NOT NULL,
                board TEXT NOT NULL,
                snapshot_fingerprint TEXT NOT NULL,
                decision_schema TEXT NOT NULL,
                action_kind TEXT NOT NULL,
                task_id TEXT NOT NULL,
                profile TEXT NOT NULL,
                skill TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL,
                claim_generation INTEGER NOT NULL DEFAULT 0,
                claimant_id TEXT,
                created_at INTEGER NOT NULL,
                claimed_at INTEGER,
                claim_expires_at INTEGER,
                completed_at INTEGER,
                result_code TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_workflow_action_queue "
            "ON workflow_action(status, created_at, action_id)"
        )
        return conn
    except Exception:
        conn.close()
        raise


def fingerprint_snapshot(snapshot: Any) -> str:
    if not isinstance(snapshot, dict) or snapshot.get("schema") != _SNAPSHOT_SCHEMA:
        raise ValueError("snapshot schema is invalid")
    try:
        encoded = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("snapshot is not canonical JSON") from exc
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _public_payload(
    row: sqlite3.Row,
    *,
    materialized: bool | None = None,
    claimed: bool | None = None,
    takeover: bool = False,
    idempotent_replay: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "schema": str(row["schema"]),
        "action_id": str(row["action_id"]),
        "board": str(row["board"]),
        "snapshot_fingerprint": str(row["snapshot_fingerprint"]),
        "decision_schema": str(row["decision_schema"]),
        "action_kind": str(row["action_kind"]),
        "task_id": str(row["task_id"]),
        "profile": str(row["profile"]),
        "skill": str(row["skill"]),
        "reason": str(row["reason"]),
        "status": str(row["status"]),
        "claim_generation": int(row["claim_generation"]),
        "created_at": int(row["created_at"]),
        "claimed_at": int(row["claimed_at"]) if row["claimed_at"] is not None else None,
        "claim_expires_at": (
            int(row["claim_expires_at"])
            if row["claim_expires_at"] is not None
            else None
        ),
        "completed_at": (
            int(row["completed_at"]) if row["completed_at"] is not None else None
        ),
        "result_code": row["result_code"],
        "takeover": takeover,
        "idempotent_replay": idempotent_replay,
    }
    if materialized is not None:
        payload["materialized"] = materialized
    if claimed is not None:
        payload["claimed"] = claimed
    return payload


def materialize_decision_action(
    *,
    state_path: Path,
    snapshot: Any,
    decision: Any,
    now_fn: Callable[[], int] = _now,
) -> dict[str, Any]:
    """Insert one deterministic outbox row for an actionable workflow decision."""
    if not isinstance(decision, dict) or decision.get("schema") != _DECISION_SCHEMA:
        raise ValueError("decision schema is invalid")
    if not isinstance(decision.get("valid"), bool):
        raise ValueError("decision.valid must be boolean")
    if not decision["valid"]:
        return {
            "ok": True,
            "schema": _ACTION_SCHEMA,
            "materialized": False,
            "reason_code": "INVALID_DECISION",
        }
    action = decision.get("action")
    if action is None:
        return {
            "ok": True,
            "schema": _ACTION_SCHEMA,
            "materialized": False,
            "reason_code": "NO_EXTERNAL_ACTION",
        }
    if not isinstance(action, dict):
        raise ValueError("decision action must be an object")
    action_kind = _required_text(action.get("kind"), "action.kind")
    if action_kind in _NO_ACTION_KINDS:
        return {
            "ok": True,
            "schema": _ACTION_SCHEMA,
            "materialized": False,
            "reason_code": "NO_EXTERNAL_ACTION",
        }
    if action_kind not in _ACTIONABLE_KINDS:
        raise ValueError("decision action kind is unsupported")

    board = _stable_text(decision.get("board"), "decision.board")
    snapshot_board = _stable_text(
        snapshot.get("board") if isinstance(snapshot, dict) else None,
        "snapshot.board",
    )
    if snapshot_board != board:
        raise ValueError("snapshot board does not match decision board")
    snapshot_fingerprint = fingerprint_snapshot(snapshot)
    contract = {
        "board": board,
        "snapshot_fingerprint": snapshot_fingerprint,
        "decision_schema": _DECISION_SCHEMA,
        "action_kind": action_kind,
        "task_id": _stable_text(action.get("task_id"), "action.task_id"),
        "profile": _stable_text(action.get("profile"), "action.profile"),
        "skill": _stable_text(action.get("skill"), "action.skill"),
        "reason": _stable_text(action.get("reason"), "action.reason"),
    }
    identity = json.dumps(
        contract,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    action_id = f"act_{hashlib.sha256(identity).hexdigest()[:32]}"
    now = int(now_fn())

    conn = _connect(state_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM workflow_action WHERE action_id=?", (action_id,)
        ).fetchone()
        if existing is not None:
            existing_contract = {
                key: existing[key]
                for key in (
                    "board",
                    "snapshot_fingerprint",
                    "decision_schema",
                    "action_kind",
                    "task_id",
                    "profile",
                    "skill",
                    "reason",
                )
            }
            if existing_contract != contract:
                raise ValueError("action_id collision with a different action contract")
            conn.commit()
            return _public_payload(
                existing, materialized=True, idempotent_replay=True
            )

        conn.execute(
            """
            INSERT INTO workflow_action(
                action_id, schema, board, snapshot_fingerprint, decision_schema,
                action_kind, task_id, profile, skill, reason, status, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                action_id,
                _ACTION_SCHEMA,
                contract["board"],
                contract["snapshot_fingerprint"],
                contract["decision_schema"],
                contract["action_kind"],
                contract["task_id"],
                contract["profile"],
                contract["skill"],
                contract["reason"],
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM workflow_action WHERE action_id=?", (action_id,)
        ).fetchone()
        conn.commit()
        return _public_payload(row, materialized=True)
    finally:
        conn.close()


def claim_next_action(
    *,
    state_path: Path,
    claimant_id: Any,
    ttl_seconds: Any,
    now_fn: Callable[[], int] = _now,
) -> dict[str, Any]:
    """Claim at most one pending or stale action across all monitored boards."""
    claimant_id = _stable_text(claimant_id, "claimant_id")
    ttl_seconds = _positive_ttl(ttl_seconds)
    now = int(now_fn())

    conn = _connect(state_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        active = conn.execute(
            """
            SELECT * FROM workflow_action
            WHERE status='claimed' AND claim_expires_at > ?
            ORDER BY claimed_at ASC, action_id ASC
            """,
            (now,),
        ).fetchall()
        if len(active) > 1:
            raise ValueError("multiple active action claims violate capacity one")
        if active:
            row = active[0]
            if str(row["claimant_id"]) == claimant_id:
                conn.commit()
                return _public_payload(
                    row, claimed=True, idempotent_replay=True
                )
            conn.commit()
            return {
                "ok": True,
                "schema": _ACTION_SCHEMA,
                "claimed": False,
                "reason_code": "ACTION_HELD",
            }

        candidate = conn.execute(
            """
            SELECT * FROM workflow_action
            WHERE status='pending'
               OR (status='claimed' AND claim_expires_at <= ?)
            ORDER BY created_at ASC, action_id ASC
            LIMIT 1
            """,
            (now,),
        ).fetchone()
        if candidate is None:
            conn.commit()
            return {
                "ok": True,
                "schema": _ACTION_SCHEMA,
                "claimed": False,
                "reason_code": "ACTION_QUEUE_EMPTY",
            }

        takeover = str(candidate["status"]) == "claimed"
        generation = int(candidate["claim_generation"]) + 1
        conn.execute(
            """
            UPDATE workflow_action
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
        claimed_row = conn.execute(
            "SELECT * FROM workflow_action WHERE action_id=?",
            (candidate["action_id"],),
        ).fetchone()
        conn.commit()
        return _public_payload(
            claimed_row, claimed=True, takeover=takeover
        )
    finally:
        conn.close()


def complete_action(
    *,
    state_path: Path,
    action_id: Any,
    claimant_id: Any,
    claim_generation: Any,
    result_code: Any,
    now_fn: Callable[[], int] = _now,
) -> dict[str, Any]:
    """Complete one claimed action with a compact result code."""
    action_id = _required_text(action_id, "action_id")
    claimant_id = _stable_text(claimant_id, "claimant_id")
    if isinstance(claim_generation, bool) or (
        isinstance(claim_generation, float) and not claim_generation.is_integer()
    ):
        raise ValueError("claim_generation must be a positive integer")
    try:
        claim_generation = int(claim_generation)
    except (TypeError, ValueError) as exc:
        raise ValueError("claim_generation must be a positive integer") from exc
    if claim_generation <= 0:
        raise ValueError("claim_generation must be a positive integer")
    result_code = _result_code(result_code)
    now = int(now_fn())

    conn = _connect(state_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM workflow_action WHERE action_id=?", (action_id,)
        ).fetchone()
        if row is None:
            raise ValueError("unknown action_id")
        if (
            str(row["claimant_id"] or "") != claimant_id
            or int(row["claim_generation"]) != claim_generation
        ):
            raise ValueError("caller does not own the action claim")
        if str(row["status"]) == "completed":
            if str(row["result_code"] or "") != result_code:
                raise ValueError("action was already completed with a different result")
            conn.commit()
            return _public_payload(row, idempotent_replay=True)
        if str(row["status"]) != "claimed":
            raise ValueError("action is not claimed")
        if row["claim_expires_at"] is None or now >= int(row["claim_expires_at"]):
            raise ValueError("action claim has expired")

        conn.execute(
            """
            UPDATE workflow_action
            SET status='completed', completed_at=?, result_code=?
            WHERE action_id=? AND status='claimed'
              AND claimant_id=? AND claim_generation=?
            """,
            (now, result_code, action_id, claimant_id, claim_generation),
        )
        completed = conn.execute(
            "SELECT * FROM workflow_action WHERE action_id=?", (action_id,)
        ).fetchone()
        conn.commit()
        return _public_payload(completed)
    finally:
        conn.close()


def read_actions(*, state_path: Path) -> list[dict[str, Any]]:
    """Read privacy-safe action state ordered by creation time."""
    conn = _connect(state_path)
    try:
        rows = conn.execute(
            "SELECT * FROM workflow_action ORDER BY created_at ASC, action_id ASC"
        ).fetchall()
        return [_public_payload(row) for row in rows]
    finally:
        conn.close()
