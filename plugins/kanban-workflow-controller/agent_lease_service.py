"""Durable capacity-one lease for the VRAM-constrained local agent."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


_LEASE_SCHEMA = "kwc-agent-lease-v1"
_RESOURCE = "local-agent"


def _now() -> int:
    return int(time.time())


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
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
            CREATE TABLE IF NOT EXISTS agent_lease (
                resource TEXT PRIMARY KEY,
                schema TEXT NOT NULL,
                lease_id TEXT NOT NULL,
                holder_id TEXT NOT NULL,
                status TEXT NOT NULL,
                generation INTEGER NOT NULL,
                acquired_at INTEGER NOT NULL,
                heartbeat_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                released_at INTEGER
            )
            """
        )
        return conn
    except Exception:
        conn.close()
        raise


def _owner_payload(
    row: sqlite3.Row,
    *,
    acquired: bool,
    takeover: bool = False,
    idempotent_replay: bool = False,
) -> dict[str, Any]:
    return {
        "ok": True,
        "schema": str(row["schema"]),
        "resource": str(row["resource"]),
        "lease_id": str(row["lease_id"]),
        "status": str(row["status"]),
        "generation": int(row["generation"]),
        "acquired_at": int(row["acquired_at"]),
        "heartbeat_at": int(row["heartbeat_at"]),
        "expires_at": int(row["expires_at"]),
        "released_at": (
            int(row["released_at"]) if row["released_at"] is not None else None
        ),
        "acquired": acquired,
        "takeover": takeover,
        "idempotent_replay": idempotent_replay,
    }


def _held_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "ok": True,
        "schema": str(row["schema"]),
        "resource": str(row["resource"]),
        "status": "active",
        "generation": int(row["generation"]),
        "heartbeat_at": int(row["heartbeat_at"]),
        "expires_at": int(row["expires_at"]),
        "acquired": False,
        "reason_code": "LEASE_HELD",
    }


def acquire_local_agent_lease(
    *,
    state_path: Path,
    lease_id: Any,
    holder_id: Any,
    ttl_seconds: Any,
    now_fn: Callable[[], int] = _now,
) -> dict[str, Any]:
    """Acquire the single local-agent slot, or report its active holder."""
    lease_id = _required_text(lease_id, "lease_id")
    holder_id = _required_text(holder_id, "holder_id")
    ttl_seconds = _positive_ttl(ttl_seconds)
    now = int(now_fn())

    conn = _connect(state_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM agent_lease WHERE resource=?", (_RESOURCE,)
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO agent_lease(
                    resource, schema, lease_id, holder_id, status, generation,
                    acquired_at, heartbeat_at, expires_at, released_at
                ) VALUES(?, ?, ?, ?, 'active', 1, ?, ?, ?, NULL)
                """,
                (
                    _RESOURCE,
                    _LEASE_SCHEMA,
                    lease_id,
                    holder_id,
                    now,
                    now,
                    now + ttl_seconds,
                ),
            )
            acquired = conn.execute(
                "SELECT * FROM agent_lease WHERE resource=?", (_RESOURCE,)
            ).fetchone()
            conn.commit()
            return _owner_payload(acquired, acquired=True)

        same_lease = str(row["lease_id"]) == lease_id
        same_holder = str(row["holder_id"]) == holder_id
        status = str(row["status"])
        expired = status == "active" and now >= int(row["expires_at"])

        if same_lease and not same_holder:
            raise ValueError("lease_id is already bound to a different holder")
        if same_lease and same_holder and status == "active" and not expired:
            conn.commit()
            return _owner_payload(
                row, acquired=True, idempotent_replay=True
            )
        if same_lease and same_holder and expired:
            raise ValueError("expired lease_id cannot be reused")
        if same_lease and same_holder and status == "released":
            raise ValueError("released lease_id cannot be reused")
        if status == "active" and not expired:
            conn.commit()
            return _held_payload(row)

        generation = int(row["generation"]) + 1
        takeover = status == "active" and expired
        conn.execute(
            """
            UPDATE agent_lease
            SET schema=?, lease_id=?, holder_id=?, status='active', generation=?,
                acquired_at=?, heartbeat_at=?, expires_at=?, released_at=NULL
            WHERE resource=?
            """,
            (
                _LEASE_SCHEMA,
                lease_id,
                holder_id,
                generation,
                now,
                now,
                now + ttl_seconds,
                _RESOURCE,
            ),
        )
        acquired = conn.execute(
            "SELECT * FROM agent_lease WHERE resource=?", (_RESOURCE,)
        ).fetchone()
        conn.commit()
        return _owner_payload(acquired, acquired=True, takeover=takeover)
    finally:
        conn.close()


def heartbeat_local_agent_lease(
    *,
    state_path: Path,
    lease_id: Any,
    holder_id: Any,
    ttl_seconds: Any,
    now_fn: Callable[[], int] = _now,
) -> dict[str, Any]:
    """Extend a live lease only when both ownership identifiers still match."""
    lease_id = _required_text(lease_id, "lease_id")
    holder_id = _required_text(holder_id, "holder_id")
    ttl_seconds = _positive_ttl(ttl_seconds)
    now = int(now_fn())

    conn = _connect(state_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM agent_lease WHERE resource=?", (_RESOURCE,)
        ).fetchone()
        if row is None:
            raise ValueError("local-agent lease does not exist")
        if str(row["lease_id"]) != lease_id or str(row["holder_id"]) != holder_id:
            raise ValueError("caller does not own the local-agent lease")
        if str(row["status"]) != "active":
            raise ValueError("local-agent lease is not active")
        if now >= int(row["expires_at"]):
            raise ValueError("local-agent lease has expired")

        conn.execute(
            """
            UPDATE agent_lease
            SET heartbeat_at=?, expires_at=?
            WHERE resource=? AND lease_id=? AND holder_id=? AND status='active'
            """,
            (now, now + ttl_seconds, _RESOURCE, lease_id, holder_id),
        )
        updated = conn.execute(
            "SELECT * FROM agent_lease WHERE resource=?", (_RESOURCE,)
        ).fetchone()
        conn.commit()
        return _owner_payload(updated, acquired=True)
    finally:
        conn.close()


def release_local_agent_lease(
    *,
    state_path: Path,
    lease_id: Any,
    holder_id: Any,
    now_fn: Callable[[], int] = _now,
) -> dict[str, Any]:
    """Release the current lease; an identical release retry is idempotent."""
    lease_id = _required_text(lease_id, "lease_id")
    holder_id = _required_text(holder_id, "holder_id")
    now = int(now_fn())

    conn = _connect(state_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM agent_lease WHERE resource=?", (_RESOURCE,)
        ).fetchone()
        if row is None:
            raise ValueError("local-agent lease does not exist")
        if str(row["lease_id"]) != lease_id or str(row["holder_id"]) != holder_id:
            raise ValueError("caller does not own the local-agent lease")
        if str(row["status"]) == "released":
            conn.commit()
            return _owner_payload(
                row, acquired=False, idempotent_replay=True
            )
        if str(row["status"]) != "active":
            raise ValueError("local-agent lease is not active")

        conn.execute(
            """
            UPDATE agent_lease
            SET status='released', released_at=?
            WHERE resource=? AND lease_id=? AND holder_id=? AND status='active'
            """,
            (now, _RESOURCE, lease_id, holder_id),
        )
        released = conn.execute(
            "SELECT * FROM agent_lease WHERE resource=?", (_RESOURCE,)
        ).fetchone()
        conn.commit()
        return _owner_payload(released, acquired=False)
    finally:
        conn.close()


def read_local_agent_lease(
    *,
    state_path: Path,
    now_fn: Callable[[], int] = _now,
) -> dict[str, Any]:
    """Read compact lease state without exposing the holder identifier."""
    now = int(now_fn())
    conn = _connect(state_path)
    try:
        row = conn.execute(
            "SELECT * FROM agent_lease WHERE resource=?", (_RESOURCE,)
        ).fetchone()
        if row is None:
            return {
                "ok": True,
                "schema": _LEASE_SCHEMA,
                "resource": _RESOURCE,
                "status": "available",
                "generation": 0,
                "expired": False,
            }
        status = str(row["status"])
        expired = status == "active" and now >= int(row["expires_at"])
        return {
            "ok": True,
            "schema": str(row["schema"]),
            "resource": str(row["resource"]),
            "lease_id": str(row["lease_id"]),
            "status": status,
            "generation": int(row["generation"]),
            "acquired_at": int(row["acquired_at"]),
            "heartbeat_at": int(row["heartbeat_at"]),
            "expires_at": int(row["expires_at"]),
            "released_at": (
                int(row["released_at"])
                if row["released_at"] is not None
                else None
            ),
            "expired": expired,
        }
    finally:
        conn.close()
