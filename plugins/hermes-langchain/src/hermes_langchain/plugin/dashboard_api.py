"""Runtime health endpoint for Hermes LangChain."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException

from ..authoring import authoring_launcher, authoring_service
from ..kanban.action_service import read_actions as _read_actions
from ..paths import PLUGIN_ROOT
from ..runtime.comment_service import create_comment_request as _create_kwc_comment_request
from ..runtime.comment_service import nudge_dispatch as _nudge_dispatch
from ..runtime.runtime_service import ensure_runtime as _ensure_runtime
from ..runtime.runtime_service import read_reconcile_health as _read_reconcile_health
from ..runtime.runtime_service import read_runtime_status as _read_runtime_status
from ..runtime.runtime_service import read_workflow_decisions as _read_workflow_decisions
from ..runtime.runtime_service import request_runtime_stop as _request_runtime_stop


def _load_authoring_service() -> Any:
    return authoring_service


def _load_authoring_launcher() -> Any:
    return authoring_launcher

router = APIRouter()
_HEARTBEAT_MAX_AGE_SECONDS = 180


def _now() -> int:
    return int(time.time())


def _state_path() -> Path:
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / "plugin-data" / "hermes-langchain" / "state.db"


def _plugin_version(manifest_path: Path | None = None) -> str:
    path = manifest_path or PLUGIN_ROOT / "plugin.yaml"
    try:
        manifest = path.read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    match = re.search(r"(?m)^version:\s*([^\s#]+)", manifest)
    return match.group(1) if match else "unknown"


def _read_heartbeats() -> list[dict[str, object]]:
    try:
        conn = sqlite3.connect(_state_path(), timeout=1.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT board, last_dispatch_tick_at FROM board_heartbeat "
            "ORDER BY last_dispatch_tick_at DESC"
        ).fetchall()
    except (OSError, sqlite3.Error):
        return []
    finally:
        if "conn" in locals():
            conn.close()
    return [dict(row) for row in rows]


@router.post("/runtime/lease")
def runtime_lease() -> dict[str, Any]:
    """Start or reuse the singleton runtime owned by this desktop-serve process."""
    try:
        return _ensure_runtime(
            state_path=_state_path(),
            plugin_root=PLUGIN_ROOT,
            parent_pid=os.getpid(),
        )
    except (OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/runtime/stop")
def runtime_stop() -> dict[str, Any]:
    """Request a graceful stop of the currently owned runtime generation."""
    state_path = _state_path()
    current = _read_runtime_status(state_path=state_path)
    generation = current.get("generation")
    if generation is None:
        return current
    return _request_runtime_stop(
        state_path=state_path,
        generation=generation,
    )


@router.get("/runtime/decisions")
def runtime_decisions() -> dict[str, Any]:
    """Return latest read-only WorkflowEngine decisions without task bodies or logs."""
    items = _read_workflow_decisions(state_path=_state_path())
    return {"items": items, "total": len(items)}


@router.get("/runtime/actions")
def runtime_actions() -> dict[str, Any]:
    """Return the privacy-safe durable dispatch outbox."""
    items = _read_actions(state_path=_state_path())
    return {"items": items, "total": len(items)}


@router.post("/authoring")
def begin_authoring(payload: dict[str, Any]) -> dict[str, Any]:
    """Create one durable assignment and launch its dedicated PM Triage session."""
    state_path = _state_path()
    try:
        board = str(payload.get("board") or "").strip()
        generation = payload.get("generation", 1)
        assignment = _load_authoring_service().begin_authoring(
            state_path=state_path,
            board=board,
            issue_number=payload.get("issue_number"),
            generation=generation,
            workspace_path=Path(str(payload.get("workspace_path") or "")),
            pm_session_key=f"kwc-authoring:{board}:g{generation}",
            replace_incomplete=payload.get("replace_incomplete", False),
        )
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        launch = _load_authoring_launcher().launch_assignment(
            assignment=assignment,
            workspace=Path(str(assignment["workspace_path"])),
            state_path=state_path,
            profile="project-manager",
            hermes_executable=shutil.which("hermes") or "hermes",
        )
    except (OSError, subprocess.SubprocessError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=f"authoring launch failed: {exc}") from exc
    return {**assignment, "launch": launch}


@router.get("/authoring/{board}")
def authoring_assignment(board: str) -> dict[str, Any]:
    assignment = _load_authoring_service().read_assignment(
        state_path=_state_path(), board=board
    )
    if assignment is None:
        raise HTTPException(status_code=404, detail="authoring assignment not found")
    return assignment


@router.get("/status")
def status() -> dict[str, object]:
    """Report Desktop-owned runtime health separately from native dispatcher health."""
    state_path = _state_path()
    runtime = _read_runtime_status(state_path=state_path)
    reconciliation = _read_reconcile_health(state_path=state_path)
    heartbeats = _read_heartbeats()
    now = _now()
    active = [
        item
        for item in heartbeats
        if 0 <= now - int(item["last_dispatch_tick_at"]) <= _HEARTBEAT_MAX_AGE_SECONDS
    ]
    last_tick = max((int(item["last_dispatch_tick_at"]) for item in heartbeats), default=0)
    return {
        "plugin": "hermes-langchain",
        "running": bool(runtime["running"]),
        "version": _plugin_version(),
        "runtime": runtime,
        "reconciliation": reconciliation,
        "dispatcher_running": bool(active),
        "board": active[0]["board"] if active else None,
        "boards": [item["board"] for item in active],
        "last_dispatch_tick_at": last_tick or None,
    }


@router.get("/activity")
def activity(limit: int = 100) -> dict[str, object]:
    """Return recent privacy-safe controller decisions without message or session content."""
    bounded_limit = max(1, min(int(limit), 200))
    try:
        conn = sqlite3.connect(_state_path(), timeout=1.0)
        conn.row_factory = sqlite3.Row
        total_row = conn.execute("SELECT COUNT(*) AS value FROM activity_log").fetchone()
        rows = conn.execute(
            "SELECT id, created_at, board, action, outcome, reason, task_id, "
            "source_event_id, event_kind FROM activity_log ORDER BY id DESC LIMIT ?",
            (bounded_limit,),
        ).fetchall()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return {"items": [], "total": 0}
    finally:
        if "conn" in locals():
            conn.close()
    return {
        "items": [dict(row) for row in rows],
        "total": int(total_row["value"] if total_row else 0),
    }


@router.post("/comments")
def create_comment(payload: dict[str, Any], background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Persist a native Kanban comment, enqueue KWC routing, then nudge recovery immediately."""
    try:
        result = _create_kwc_comment_request(
            _state_path(),
            board=payload.get("board"),
            task_id=payload.get("task_id"),
            body=payload.get("body"),
            author="desktop",
            idempotency_key=payload.get("idempotency_key"),
            reply_to_comment_id=payload.get("reply_to_comment_id"),
            route_requested=True,
        )
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    background_tasks.add_task(_nudge_dispatch, str(result["board"]))
    return result
