"""llama.cpp Manager plugin package."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


_COORDINATOR_CONTEXT_URL = "http://127.0.0.1:18380/__llamacpp/request-context"


def _profile_name() -> str:
    try:
        from hermes_constants import get_hermes_home

        home = get_hermes_home().resolve()
    except (ImportError, OSError):
        home = Path(os.environ.get("HERMES_HOME") or "").resolve()
    if home.parent.name.lower() == "profiles" and home.name:
        return home.name
    return "main"


def _session_id(hook_session_id: Any) -> str:
    """Resolve the request-local session before the reusable agent's cached id."""
    try:
        from gateway.session_context import get_session_env

        current = str(get_session_env("HERMES_SESSION_ID", "") or "").strip()
        if current:
            return current
    except (ImportError, OSError):
        pass
    return str(hook_session_id or "").strip()


def _is_local_request(provider: Any, base_url: Any, model: Any) -> bool:
    return (
        str(provider or "") in {"custom", "llamacpp-local"}
        and str(model or "") in {"main-local", "compression-local"}
        and str(base_url or "").startswith("http://127.0.0.1:18380/")
    )


def _publish_resolved_request_context(session: str, **kwargs: Any) -> None:
    if not _is_local_request(kwargs.get("provider"), kwargs.get("base_url"), kwargs.get("model")):
        return
    if not session:
        return
    payload = json.dumps({
        "model": str(kwargs.get("model") or ""),
        "profile": _profile_name(),
        "session": session,
    }, separators=(",", ":")).encode("utf-8")
    request = Request(
        _COORDINATOR_CONTEXT_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=2.0):
            pass
    except OSError:
        # Observability must never block or fail the model request.
        return


def _tag_request_context(request: dict[str, Any], **kwargs: Any) -> dict[str, Any] | None:
    """Attach Core request identity to the actual coordinator request."""
    if not _is_local_request(kwargs.get("provider"), kwargs.get("base_url"), kwargs.get("model")):
        return None
    session = _session_id(kwargs.get("session_id"))
    if not session:
        return None
    updated = dict(request)
    headers = dict(updated.get("extra_headers") or {})
    headers["x-hermes-profile"] = _profile_name()
    headers["x-hermes-session"] = session
    updated["extra_headers"] = headers
    return {
        "request": updated,
        "source": "llamacpp",
        "reason": "attach request-local profile and session identity",
    }


def _publish_auxiliary_request_context(**kwargs: Any) -> None:
    """Prefer the auxiliary hook's parent-turn identity over ambient process context."""
    parent_turn_session = str(kwargs.get("session_id") or "").strip()
    session = parent_turn_session or _session_id(None)
    _publish_resolved_request_context(session, **kwargs)


def register(ctx) -> None:
    """Register request metadata propagation for privacy-safe runtime log attribution."""
    ctx.register_middleware("llm_request", _tag_request_context)
    ctx.register_hook("pre_auxiliary_call", _publish_auxiliary_request_context)
