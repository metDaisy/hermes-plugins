"""Desktop-owned process lifecycle for the KWC runtime worker."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any


_RUNTIME_HEARTBEAT_MAX_AGE_SECONDS = 15
_RUNTIME_INTERVAL_SECONDS = 2.0


def _now() -> int:
    return int(time.time())


def _windows_pid_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return int(exit_code.value) == still_active
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _connect(state_path: Path) -> sqlite3.Connection:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(state_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_owner (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            generation TEXT NOT NULL,
            pid INTEGER NOT NULL,
            parent_pid INTEGER NOT NULL,
            status TEXT NOT NULL,
            started_at INTEGER NOT NULL,
            heartbeat_at INTEGER NOT NULL,
            stop_requested INTEGER NOT NULL DEFAULT 0,
            stopped_at INTEGER,
            exit_reason TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS board_heartbeat (
            board TEXT PRIMARY KEY,
            last_dispatch_tick_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_reconcile_health (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            attempted_at INTEGER NOT NULL,
            succeeded_at INTEGER,
            error_code TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS workflow_decision (
            board TEXT PRIMARY KEY,
            evaluated_at INTEGER NOT NULL,
            decision_schema TEXT NOT NULL,
            valid INTEGER NOT NULL,
            phase TEXT NOT NULL,
            action_kind TEXT,
            task_id TEXT,
            profile TEXT,
            skill TEXT,
            reason TEXT,
            errors_json TEXT NOT NULL
        )
        """
    )
    return conn


def _row_payload(
    row: sqlite3.Row | None,
    *,
    now: int,
    pid_alive_fn: Callable[[int], bool],
    started: bool = False,
) -> dict[str, Any]:
    if row is None:
        return {
            "running": False,
            "started": started,
            "status": "stopped",
            "generation": None,
            "pid": None,
            "parent_pid": None,
            "heartbeat_at": None,
            "stop_requested": False,
            "exit_reason": None,
        }
    pid = int(row["pid"])
    parent_pid = int(row["parent_pid"])
    heartbeat_at = int(row["heartbeat_at"])
    process_alive = pid_alive_fn(pid)
    parent_alive = pid_alive_fn(parent_pid)
    heartbeat_fresh = 0 <= now - heartbeat_at <= _RUNTIME_HEARTBEAT_MAX_AGE_SECONDS
    running = (
        str(row["status"]) in {"starting", "running"}
        and not bool(row["stop_requested"])
        and process_alive
        and parent_alive
        and heartbeat_fresh
    )
    status = str(row["status"])
    if status in {"starting", "running"} and not running:
        status = "stopping" if bool(row["stop_requested"]) and process_alive else "stale"
    return {
        "running": running,
        "started": started,
        "status": status,
        "generation": str(row["generation"]),
        "pid": pid,
        "parent_pid": int(row["parent_pid"]),
        "heartbeat_at": heartbeat_at,
        "stop_requested": bool(row["stop_requested"]),
        "exit_reason": row["exit_reason"],
    }


def read_runtime_status(
    *,
    state_path: Path,
    now_fn: Callable[[], int] = _now,
    pid_alive_fn: Callable[[int], bool] = _pid_alive,
) -> dict[str, Any]:
    try:
        conn = _connect(state_path)
        row = conn.execute("SELECT * FROM runtime_owner WHERE singleton = 1").fetchone()
    except (OSError, sqlite3.Error):
        return _row_payload(None, now=now_fn(), pid_alive_fn=pid_alive_fn)
    finally:
        if "conn" in locals():
            conn.close()
    return _row_payload(row, now=now_fn(), pid_alive_fn=pid_alive_fn)


def ensure_runtime(
    *,
    state_path: Path,
    plugin_root: Path,
    parent_pid: int,
    python_executable: str = sys.executable,
    now_fn: Callable[[], int] = _now,
    popen_fn: Callable[..., Any] = subprocess.Popen,
    pid_alive_fn: Callable[[int], bool] = _pid_alive,
) -> dict[str, Any]:
    now = now_fn()
    conn = _connect(state_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM runtime_owner WHERE singleton = 1").fetchone()
        if row is not None:
            current = _row_payload(row, now=now, pid_alive_fn=pid_alive_fn)
            if current["running"]:
                conn.commit()
                return current

        generation = uuid.uuid4().hex
        command = [
            python_executable,
            str(plugin_root / "runtime_service.py"),
            "--worker",
            "--state-db",
            str(state_path),
            "--parent-pid",
            str(parent_pid),
            "--generation",
            generation,
        ]
        popen_kwargs: dict[str, Any] = {
            "cwd": str(plugin_root),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
            "shell": False,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        process = popen_fn(command, **popen_kwargs)
        conn.execute(
            """
            INSERT INTO runtime_owner (
                singleton, generation, pid, parent_pid, status,
                started_at, heartbeat_at, stop_requested, stopped_at, exit_reason
            ) VALUES (1, ?, ?, ?, 'starting', ?, ?, 0, NULL, NULL)
            ON CONFLICT(singleton) DO UPDATE SET
                generation = excluded.generation,
                pid = excluded.pid,
                parent_pid = excluded.parent_pid,
                status = excluded.status,
                started_at = excluded.started_at,
                heartbeat_at = excluded.heartbeat_at,
                stop_requested = 0,
                stopped_at = NULL,
                exit_reason = NULL
            """,
            (generation, int(process.pid), int(parent_pid), now, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM runtime_owner WHERE singleton = 1").fetchone()
        return _row_payload(row, now=now, pid_alive_fn=pid_alive_fn, started=True)
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def request_runtime_stop(
    *,
    state_path: Path,
    generation: str | None = None,
    now_fn: Callable[[], int] = _now,
    pid_alive_fn: Callable[[int], bool] = _pid_alive,
) -> dict[str, Any]:
    conn = _connect(state_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM runtime_owner WHERE singleton = 1").fetchone()
        if row is None:
            conn.commit()
            return _row_payload(None, now=now_fn(), pid_alive_fn=pid_alive_fn)
        if generation is not None and str(row["generation"]) != generation:
            conn.commit()
            return _row_payload(row, now=now_fn(), pid_alive_fn=pid_alive_fn)
        conn.execute(
            "UPDATE runtime_owner SET stop_requested = 1 WHERE singleton = 1 AND generation = ?",
            (str(row["generation"]),),
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM runtime_owner WHERE singleton = 1").fetchone()
        return _row_payload(updated, now=now_fn(), pid_alive_fn=pid_alive_fn)
    finally:
        conn.close()


def read_reconcile_health(*, state_path: Path) -> dict[str, Any]:
    """Return privacy-safe Runtime reconciliation health."""
    conn = _connect(state_path)
    try:
        row = conn.execute(
            "SELECT attempted_at, succeeded_at, error_code "
            "FROM runtime_reconcile_health WHERE singleton = 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {
            "attempted_at": None,
            "succeeded_at": None,
            "healthy": False,
            "error_code": None,
        }
    error_code = row["error_code"]
    return {
        "attempted_at": int(row["attempted_at"]),
        "succeeded_at": int(row["succeeded_at"]) if row["succeeded_at"] is not None else None,
        "healthy": error_code is None and row["succeeded_at"] is not None,
        "error_code": str(error_code) if error_code is not None else None,
    }


def _record_reconcile_health(
    state_path: Path,
    *,
    attempted_at: int,
    error_code: str | None,
) -> None:
    conn = _connect(state_path)
    try:
        conn.execute(
            """
            INSERT INTO runtime_reconcile_health(
                singleton, attempted_at, succeeded_at, error_code
            ) VALUES(1, ?, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET
                attempted_at=excluded.attempted_at,
                succeeded_at=CASE
                    WHEN excluded.error_code IS NULL THEN excluded.attempted_at
                    ELSE runtime_reconcile_health.succeeded_at
                END,
                error_code=excluded.error_code
            """,
            (
                attempted_at,
                attempted_at if error_code is None else None,
                error_code,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def read_workflow_decisions(*, state_path: Path) -> list[dict[str, Any]]:
    """Return the latest privacy-safe dry-run decision for each monitored board."""
    conn = _connect(state_path)
    try:
        rows = conn.execute(
            "SELECT * FROM workflow_decision ORDER BY evaluated_at DESC, board ASC"
        ).fetchall()
    finally:
        conn.close()
    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            errors = json.loads(row["errors_json"])
        except (TypeError, ValueError):
            errors = ["DECISION_ERRORS_CORRUPT"]
        result.append(
            {
                "board": str(row["board"]),
                "evaluated_at": int(row["evaluated_at"]),
                "decision_schema": str(row["decision_schema"]),
                "valid": bool(row["valid"]),
                "phase": str(row["phase"]),
                "action_kind": row["action_kind"],
                "task_id": row["task_id"],
                "profile": row["profile"],
                "skill": row["skill"],
                "reason": row["reason"],
                "errors": errors if isinstance(errors, list) else ["DECISION_ERRORS_CORRUPT"],
            }
        )
    return result


def reconcile_workflows(
    *,
    state_path: Path,
    snapshot_reader: Callable[[str], dict[str, Any]],
    decide_fn: Callable[[Any], dict[str, Any]],
    now_fn: Callable[[], int] = _now,
) -> dict[str, int]:
    """Evaluate monitored boards read-only and persist compact decisions."""
    conn = _connect(state_path)
    try:
        boards = [
            str(row["board"])
            for row in conn.execute("SELECT board FROM board_heartbeat ORDER BY board ASC").fetchall()
        ]
    finally:
        conn.close()
    valid_count = 0
    invalid_count = 0
    evaluated_at = now_fn()
    for board in boards:
        try:
            decision = decide_fn(snapshot_reader(board))
            if not isinstance(decision, dict):
                raise TypeError("decision must be an object")
        except Exception as exc:
            decision = {
                "schema": "kwc-workflow-decision-v1",
                "valid": False,
                "phase": "error",
                "action": None,
                "errors": [f"RECONCILE_ERROR:{type(exc).__name__}"],
            }
        action = decision.get("action")
        action = action if isinstance(action, dict) else {}
        errors = decision.get("errors")
        errors = errors if isinstance(errors, list) else ["DECISION_ERRORS_INVALID"]
        valid = bool(decision.get("valid"))
        valid_count += int(valid)
        invalid_count += int(not valid)
        conn = _connect(state_path)
        try:
            conn.execute(
                """
                INSERT INTO workflow_decision(
                    board, evaluated_at, decision_schema, valid, phase,
                    action_kind, task_id, profile, skill, reason, errors_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(board) DO UPDATE SET
                    evaluated_at=excluded.evaluated_at,
                    decision_schema=excluded.decision_schema,
                    valid=excluded.valid,
                    phase=excluded.phase,
                    action_kind=excluded.action_kind,
                    task_id=excluded.task_id,
                    profile=excluded.profile,
                    skill=excluded.skill,
                    reason=excluded.reason,
                    errors_json=excluded.errors_json
                """,
                (
                    board,
                    evaluated_at,
                    str(decision.get("schema") or "kwc-workflow-decision-v1"),
                    1 if valid else 0,
                    str(decision.get("phase") or "invalid"),
                    action.get("kind"),
                    action.get("task_id"),
                    action.get("profile"),
                    action.get("skill"),
                    action.get("reason"),
                    json.dumps(errors, ensure_ascii=True, separators=(",", ":")),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    return {"boards": len(boards), "valid": valid_count, "invalid": invalid_count}


def _load_sibling_module(unique_name: str, filename: str) -> Any:
    path = Path(__file__).resolve().with_name(filename)
    module = sys.modules.get(unique_name)
    if module is not None:
        if Path(module.__file__).resolve() != path:
            raise ImportError(f"{unique_name} is bound to another path")
        return module
    spec = importlib.util.spec_from_file_location(unique_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load KWC runtime dependency from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


def reconcile_runtime(state_path: Path) -> dict[str, int]:
    """Load the pure engine and canonical snapshot adapter, then evaluate boards."""
    engine = _load_sibling_module(
        "_hermes_kanban_workflow_controller_workflow_engine", "workflow_engine.py"
    )
    snapshots = _load_sibling_module(
        "_hermes_kanban_workflow_controller_snapshot_service", "snapshot_service.py"
    )
    return reconcile_workflows(
        state_path=state_path,
        snapshot_reader=snapshots.read_board_snapshot,
        decide_fn=engine.decide,
    )


def _worker_tick(
    state_path: Path,
    generation: str,
    heartbeat_at: int,
) -> tuple[bool, bool]:
    conn = _connect(state_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM runtime_owner WHERE singleton = 1").fetchone()
        if row is None or str(row["generation"]) != generation:
            conn.commit()
            return False, False
        stop_requested = bool(row["stop_requested"])
        conn.execute(
            "UPDATE runtime_owner SET status = 'running', heartbeat_at = ? "
            "WHERE singleton = 1 AND generation = ?",
            (heartbeat_at, generation),
        )
        conn.commit()
        return True, stop_requested
    finally:
        conn.close()


def _mark_worker_stopped(
    state_path: Path,
    generation: str,
    stopped_at: int,
    reason: str,
) -> None:
    conn = _connect(state_path)
    try:
        conn.execute(
            """
            UPDATE runtime_owner
            SET status = 'stopped', heartbeat_at = ?, stopped_at = ?, exit_reason = ?
            WHERE singleton = 1 AND generation = ?
            """,
            (stopped_at, stopped_at, reason, generation),
        )
        conn.commit()
    finally:
        conn.close()


def run_worker(
    *,
    state_path: Path,
    parent_pid: int,
    generation: str,
    interval_seconds: float = _RUNTIME_INTERVAL_SECONDS,
    now_fn: Callable[[], int] = _now,
    sleep_fn: Callable[[float], None] = time.sleep,
    pid_alive_fn: Callable[[int], bool] = _pid_alive,
    reconcile_fn: Callable[[Path], Any] | None = None,
) -> dict[str, str]:
    reason = "superseded"
    reconcile = reconcile_fn or reconcile_runtime
    try:
        while True:
            if not pid_alive_fn(parent_pid):
                reason = "parent_exited"
                break
            tick_at = now_fn()
            owned, stop_requested = _worker_tick(state_path, generation, tick_at)
            if not owned:
                reason = "superseded"
                break
            if stop_requested:
                reason = "stop_requested"
                break
            try:
                reconcile(state_path)
            except Exception as exc:
                reconcile_error = f"RECONCILE_ERROR:{type(exc).__name__}"
            else:
                reconcile_error = None
            try:
                _record_reconcile_health(
                    state_path,
                    attempted_at=tick_at,
                    error_code=reconcile_error,
                )
            except (OSError, sqlite3.Error):
                pass
            sleep_fn(interval_seconds)
    finally:
        _mark_worker_stopped(state_path, generation, now_fn(), reason)
    return {"reason": reason}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="KWC Desktop-owned runtime")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--state-db", type=Path)
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--generation")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.worker or args.state_db is None or args.parent_pid is None or not args.generation:
        return 2
    run_worker(
        state_path=args.state_db,
        parent_pid=args.parent_pid,
        generation=args.generation,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
