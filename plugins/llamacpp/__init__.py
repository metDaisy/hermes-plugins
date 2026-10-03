"""llama.cpp Manager plugin package."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


_COORDINATOR_CONTEXT_URL = "http://127.0.0.1:18380/__llamacpp/request-context"


def _profile_name() -> str:
    home = Path(os.environ.get("HERMES_HOME") or "").resolve()
    if home.parent.name.lower() == "profiles" and home.name:
        return home.name
    return "main"


def _is_local_request(provider: Any, base_url: Any, model: Any) -> bool:
    return (
        str(provider or "") in {"custom", "llamacpp-local"}
        and str(model or "") in {"main-local", "compression-local"}
        and str(base_url or "").startswith("http://127.0.0.1:18380/")
    )


def _attach_request_context(**kwargs: Any) -> dict[str, Any] | None:
    request = kwargs.get("request")
    if not isinstance(request, dict) or not _is_local_request(
        kwargs.get("provider"), kwargs.get("base_url"), kwargs.get("model")
    ):
        return None
    session = str(kwargs.get("session_id") or "").strip()
    if not session:
        return None
    updated = dict(request)
    headers = dict(updated.get("extra_headers") or {})
    headers["X-Hermes-Profile"] = _profile_name()
    headers["X-Hermes-Session"] = session
    updated["extra_headers"] = headers
    return {
        "request": updated,
        "source": "llamacpp",
        "reason": "attach Hermes profile/session log context",
    }


def _publish_auxiliary_context(**kwargs: Any) -> None:
    if not _is_local_request(kwargs.get("provider"), kwargs.get("base_url"), kwargs.get("model")):
        return
    session = str(kwargs.get("session_id") or "").strip()
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


def register(ctx) -> None:
    """Register request metadata propagation for privacy-safe runtime log attribution."""
    ctx.register_middleware("llm_request", _attach_request_context)
    ctx.register_hook("pre_auxiliary_call", _publish_auxiliary_context)
