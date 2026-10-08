"""Runtime health endpoint for Kanban Workflow Controller."""

from __future__ import annotations

import importlib.util
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException

try:
    from ..comment_service import create_comment_request as _create_kwc_comment_request
    from ..comment_service import nudge_dispatch as _nudge_dispatch
except ImportError:
    _service_path = Path(__file__).resolve().parents[1] / "comment_service.py"
    _service_name = "_hermes_kanban_workflow_controller_comment_service"
    _service_module = sys.modules.get(_service_name)
    if _service_module is None:
        _service_spec = importlib.util.spec_from_file_location(_service_name, _service_path)
        if _service_spec is None or _service_spec.loader is None:
            raise ImportError(f"cannot load KWC comment service from {_service_path}")
        _service_module = importlib.util.module_from_spec(_service_spec)
        sys.modules[_service_name] = _service_module
        _service_spec.loader.exec_module(_service_module)
    elif Path(_service_module.__file__).resolve() != _service_path:
        raise ImportError("KWC comment service module name is already bound to another path")
    _create_kwc_comment_request = _service_module.create_comment_request
    _nudge_dispatch = _service_module.nudge_dispatch

try:
    from ..runtime_service import ensure_runtime as _ensure_runtime
    from ..runtime_service import read_reconcile_health as _read_reconcile_health
    from ..runtime_service import read_runtime_status as _read_runtime_status
    from ..runtime_service import read_workflow_decisions as _read_workflow_decisions
    from ..runtime_service import request_runtime_stop as _request_runtime_stop
except ImportError:
    _runtime_path = Path(__file__).resolve().parents[1] / "runtime_service.py"
    _runtime_name = "_hermes_kanban_workflow_controller_runtime_service"
    _runtime_module = sys.modules.get(_runtime_name)
    if _runtime_module is None:
        _runtime_spec = importlib.util.spec_from_file_location(_runtime_name, _runtime_path)
        if _runtime_spec is None or _runtime_spec.loader is None:
            raise ImportError(f"cannot load KWC runtime service from {_runtime_path}")
        _runtime_module = importlib.util.module_from_spec(_runtime_spec)
        sys.modules[_runtime_name] = _runtime_module
        _runtime_spec.loader.exec_module(_runtime_module)
    elif Path(_runtime_module.__file__).resolve() != _runtime_path:
        raise ImportError("KWC runtime service module name is already bound to another path")
    _ensure_runtime = _runtime_module.ensure_runtime
    _read_reconcile_health = _runtime_module.read_reconcile_health
    _read_runtime_status = _runtime_module.read_runtime_status
    _read_workflow_decisions = _runtime_module.read_workflow_decisions
    _request_runtime_stop = _runtime_module.request_runtime_stop

try:
    from ..action_service import read_actions as _read_actions
except ImportError:
    _action_path = Path(__file__).resolve().parents[1] / "action_service.py"
    _action_name = "_hermes_kanban_workflow_controller_action_service"
    _action_module = sys.modules.get(_action_name)
    if _action_module is None:
        _action_spec = importlib.util.spec_from_file_location(_action_name, _action_path)
        if _action_spec is None or _action_spec.loader is None:
            raise ImportError(f"cannot load KWC action service from {_action_path}")
        _action_module = importlib.util.module_from_spec(_action_spec)
        sys.modules[_action_name] = _action_module
        _action_spec.loader.exec_module(_action_module)
    elif Path(_action_module.__file__).resolve() != _action_path:
        raise ImportError("KWC action service module name is already bound to another path")
    _read_actions = _action_module.read_actions

router = APIRouter()
_HEARTBEAT_MAX_AGE_SECONDS = 180


def _now() -> int:
    return int(time.time())


def _state_path() -> Path:
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / "plugin-data" / "kanban-workflow-controller" / "state.db"


def _plugin_version(manifest_path: Path | None = None) -> str:
    path = manifest_path or Path(__file__).resolve().parents[1] / "plugin.yaml"
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
            plugin_root=Path(__file__).resolve().parents[1],
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
        "plugin": "kanban-workflow-controller",
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
