"""Desktop-compatible shell-hook bridge for the agent-audit plugin.

Hermes Python plugin hooks run in CLI/Gateway, while shell hooks also run in
Desktop/TUI/dashboard sessions. This adapter preserves the existing privacy
and validation behavior by forwarding the sanitized shell-hook envelope to the
project-local Python hook implementation.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


_PLUGIN_DIR = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("agent_audit_shell_target", _PLUGIN_DIR / "__init__.py")
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - impossible for a shipped plugin
    raise RuntimeError("agent-audit hook implementation could not be loaded")
_AUDIT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_AUDIT)


_OBSERVER_EVENTS = {"on_skill_lifecycle", "post_tool_call", "on_session_end"}
_PYTHON_PLUGIN_SURFACES = {"cli", "gateway"}


def _extra(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("extra")
    return value if isinstance(value, dict) else {}


def _common(payload: dict[str, Any]) -> dict[str, Any]:
    extra = _extra(payload)
    return {
        "session_id": payload.get("session_id"),
        "profile_name": payload.get("profile"),
        "task_id": extra.get("task_id"),
        "turn_id": extra.get("turn_id"),
    }


def _is_python_plugin_surface(payload: dict[str, Any]) -> bool:
    return _extra(payload).get("platform") in _PYTHON_PLUGIN_SURFACES


def _dispatch(payload: dict[str, Any]) -> dict[str, Any] | None:
    if _is_python_plugin_surface(payload):
        return None
    event = payload.get("hook_event_name")
    extra = _extra(payload)
    common = _common(payload)

    if event == "post_tool_call":
        _AUDIT._on_post_tool_call(
            tool_name=payload.get("tool_name"),
            args=payload.get("tool_input"),
            result=extra.get("result"),
            status=extra.get("status"),
            duration_ms=extra.get("duration_ms"),
            **common,
        )
    elif event == "on_skill_lifecycle":
        _AUDIT._on_skill_lifecycle(
            action=extra.get("action"),
            skill_name=extra.get("skill_name"),
            provenance=extra.get("provenance"),
            use_count=extra.get("use_count"),
            reused=extra.get("reused"),
            **common,
        )
    elif event == "on_session_end":
        _AUDIT._on_session_end(
            completed=extra.get("completed"),
            failed=extra.get("failed"),
            interrupted=extra.get("interrupted"),
            turn_exit_reason=extra.get("turn_exit_reason"),
            **common,
        )
    elif event == "pre_verify":
        return _AUDIT._on_pre_verify(
            coding=extra.get("coding"),
            attempt=extra.get("attempt", 0),
            changed_paths=extra.get("changed_paths"),
            **common,
        )
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        result = _dispatch(payload)
        if result:
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        # Audit is fail-open: logging must never break the agent turn.
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
