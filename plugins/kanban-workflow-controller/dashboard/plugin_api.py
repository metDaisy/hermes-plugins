"""Runtime health endpoint for Kanban Workflow Controller."""

from __future__ import annotations

import importlib.util
import sqlite3
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

router = APIRouter()
_HEARTBEAT_MAX_AGE_SECONDS = 180


def _now() -> int:
    return int(time.time())


def _state_path() -> Path:
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / "plugin-data" / "kanban-workflow-controller" / "state.db"


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


@router.get("/status")
def status() -> dict[str, object]:
    """Report whether any Kanban board received a recent live dispatcher tick."""
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
        "running": bool(active),
        "version": "0.5.0",
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
