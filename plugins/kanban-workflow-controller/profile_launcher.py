"""Profile-scoped session creation and detached KWC worker launch."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any


_STABLE_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def _stable_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text or not _STABLE_TEXT.fullmatch(text):
        raise ValueError(f"{field} must be a compact stable identifier")
    return text


def resolve_profile_home(profile: Any, *, active_home: Path | None = None) -> Path:
    profile = _stable_text(profile, "profile")
    if active_home is None:
        from hermes_constants import get_default_hermes_root

        active_home = get_default_hermes_root()
    active_home = Path(active_home)
    if active_home.parent.name == "profiles":
        hermes_root = active_home.parent.parent
    else:
        hermes_root = active_home
    return hermes_root / "profiles" / profile


def reserve_session_row(
    *,
    profile_home: Path,
    session_id: str,
    profile: str,
    workspace_path: Path,
    title: str,
) -> None:
    """Reserve a compact Hermes CLI session when the Core SessionDB import is unavailable."""
    state_path = Path(profile_home) / "state.db"
    if not state_path.is_file():
        raise RuntimeError("Profile state.db is required for session reservation")
    with closing(sqlite3.connect(state_path, timeout=5.0)) as conn:
        conn.execute("PRAGMA busy_timeout = 5000")
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        required = {
            "id",
            "source",
            "created_source",
            "started_at",
            "cwd",
            "profile_name",
            "title",
            "title_source",
        }
        if not required.issubset(columns):
            raise RuntimeError("Profile session store is incompatible")
        if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone():
            raise RuntimeError("Session id already exists")
        conn.execute(
            """INSERT INTO sessions (
                id, source, created_source, started_at, cwd,
                profile_name, title, title_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                "cli",
                "cli",
                time.time(),
                str(workspace_path),
                profile,
                title,
                "user",
            ),
        )
        conn.commit()


def create_task_session(
    *,
    profile: Any,
    task_id: Any,
    workspace_path: Path,
    profile_home: Path | None = None,
    session_id_factory: Callable[[], str] | None = None,
    session_db_factory: Callable[[Path], Any] | None = None,
) -> dict[str, Any]:
    profile = _stable_text(profile, "profile")
    task_id = _stable_text(task_id, "task_id")
    workspace_path = Path(workspace_path).resolve()
    if not workspace_path.is_dir():
        raise ValueError("workspace_path must be an existing directory")
    profile_home = Path(profile_home) if profile_home is not None else resolve_profile_home(profile)
    if not profile_home.is_dir():
        raise ValueError("profile_home must be an existing Profile directory")

    if session_id_factory is None:
        from hermes_state_ids import new_session_id

        session_id_factory = new_session_id
    session_id = _stable_text(session_id_factory(), "session_id")
    title = f"KWC task {task_id} {session_id[-8:]}"
    if session_db_factory is None:
        try:
            from hermes_state import SessionDB
        except ModuleNotFoundError:
            reserve_session_row(
                profile_home=profile_home,
                session_id=session_id,
                profile=profile,
                workspace_path=workspace_path,
                title=title,
            )
        else:
            db = SessionDB(profile_home / "state.db")
            try:
                db.create_session(
                    session_id,
                    source="cli",
                    cwd=str(workspace_path),
                    profile_name=profile,
                )
                db.set_session_title(session_id, title)
            finally:
                db.close()
    else:
        db = session_db_factory(profile_home / "state.db")
        try:
            db.create_session(
                session_id,
                source="cli",
                cwd=str(workspace_path),
                profile_name=profile,
            )
            db.set_session_title(session_id, title)
        finally:
            db.close()
    return {
        "session_id": session_id,
        "profile": profile,
        "task_id": task_id,
        "workspace_path": str(workspace_path),
    }


def launch_profile_worker(
    *,
    plugin_root: Path,
    state_path: Path,
    action_id: Any,
    action_claimant_id: Any,
    action_claim_generation: Any,
    lease_id: Any,
    invocation_id: Any,
    session_id: Any,
    profile: Any,
    skill: Any,
    board: Any,
    task_id: Any,
    workspace_path: Path,
    run_id: Any,
    parent_pid: Any,
    mode: str = "dispatch",
    uv_executable: str | None = None,
    hermes_project_root: Path | None = None,
    hermes_executable: str,
    popen_fn: Callable[..., Any] = subprocess.Popen,
) -> dict[str, Any]:
    values = {
        "action_id": _stable_text(action_id, "action_id"),
        "action_claimant_id": _stable_text(action_claimant_id, "action_claimant_id"),
        "lease_id": _stable_text(lease_id, "lease_id"),
        "invocation_id": _stable_text(invocation_id, "invocation_id"),
        "session_id": _stable_text(session_id, "session_id"),
        "profile": _stable_text(profile, "profile"),
        "skill": _stable_text(skill, "skill"),
        "board": _stable_text(board, "board"),
        "task_id": _stable_text(task_id, "task_id"),
    }
    action_claim_generation = int(action_claim_generation)
    run_id = int(run_id)
    parent_pid = int(parent_pid)
    if mode not in {"dispatch", "wake"}:
        raise ValueError("mode must be dispatch or wake")
    if action_claim_generation <= 0 or parent_pid <= 0 or (mode == "dispatch" and run_id <= 0):
        raise ValueError("claim generation, parent pid, and dispatch run id must be positive")
    plugin_root = Path(plugin_root).resolve()
    workspace_path = Path(workspace_path).resolve()
    state_path = Path(state_path).resolve()
    worker_path = plugin_root / "profile_worker.py"
    if not workspace_path.is_dir():
        raise ValueError("workspace_path must be an existing directory")
    if not worker_path.is_file():
        raise ValueError("profile_worker.py is missing")
    if not str(hermes_executable).strip():
        raise ValueError("explicit Hermes executable is required")
    if uv_executable is None:
        uv_executable = shutil.which("uv")
    if not str(uv_executable or "").strip():
        raise ValueError("explicit uv executable is required")
    if hermes_project_root is None:
        executable_path = Path(hermes_executable).resolve()
        if len(executable_path.parents) < 3:
            raise ValueError("Hermes project root cannot be derived")
        hermes_project_root = executable_path.parents[2]
    hermes_project_root = Path(hermes_project_root).resolve()

    command = [
        str(uv_executable),
        "run",
        "--frozen",
        "--no-sync",
        "--project",
        str(hermes_project_root),
        "python",
        str(worker_path),
        "--state-db",
        str(state_path),
        "--action-id",
        values["action_id"],
        "--action-claimant-id",
        values["action_claimant_id"],
        "--action-claim-generation",
        str(action_claim_generation),
        "--lease-id",
        values["lease_id"],
        "--invocation-id",
        values["invocation_id"],
        "--session-id",
        values["session_id"],
        "--profile",
        values["profile"],
        "--skill",
        values["skill"],
        "--board",
        values["board"],
        "--task-id",
        values["task_id"],
        "--workspace",
        str(workspace_path),
        "--run-id",
        str(run_id),
        "--parent-pid",
        str(parent_pid),
        "--mode",
        mode,
        "--hermes-executable",
        str(hermes_executable),
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
    return {
        "ok": True,
        "pid": int(process.pid),
        "action_id": values["action_id"],
        "invocation_id": values["invocation_id"],
        "session_id": values["session_id"],
        "profile": values["profile"],
    }
