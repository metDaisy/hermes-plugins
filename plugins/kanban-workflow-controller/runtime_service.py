"""Desktop-owned process lifecycle for the KWC runtime worker."""

from __future__ import annotations

import argparse
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
) -> dict[str, str]:
    reason = "superseded"
    try:
        while True:
            if not pid_alive_fn(parent_pid):
                reason = "parent_exited"
                break
            owned, stop_requested = _worker_tick(state_path, generation, now_fn())
            if not owned:
                reason = "superseded"
                break
            if stop_requested:
                reason = "stop_requested"
                break
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
