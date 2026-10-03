"""Cancellation-aware OpenAI-compatible proxy to the active llama-server worker."""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

router = APIRouter()

COORDINATOR_PORT = 18380
RUNTIME_ROOT = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "hermes" / "runtimes" / "llamacpp"
STATE_PATH = RUNTIME_ROOT / "state.json"
ACTIVITY_LOG_PATH = RUNTIME_ROOT / "logs" / "activity.log"
REQUEST_CONTEXT_LOG_PATH = RUNTIME_ROOT / "logs" / "request-context.jsonl"
_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}
_CONTEXT_VALUE_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_activity_log_lock = threading.Lock()
_transition_lock = asyncio.Lock()
_counter_lock = asyncio.Lock()
_active_main_requests = 0
_queued_main_requests = 0


def execution_busy() -> bool:
    """Return whether coordinator shutdown must wait for an in-flight request."""
    return bool(_active_main_requests or _queued_main_requests or _transition_lock.locked())


def _worker_state() -> dict[str, Any]:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=503, detail="llama.cpp worker state is unavailable") from exc
    return state


def _worker_identity_ok(state: dict[str, Any]) -> bool:
    identity = state.get("worker_identity")
    if not isinstance(identity, dict):
        return False
    try:
        import psutil
        pid = int(state.get("pid") or 0)
        expected_pid = int(identity.get("pid") or 0)
        if expected_pid != pid:
            return False
        process = psutil.Process(pid)
        expected_time = float(identity.get("create_time"))
        expected_executable = os.path.normcase(os.path.abspath(str(identity.get("executable") or "")))
        actual_executable = os.path.normcase(os.path.abspath(process.exe()))
        port = int(state.get("port") or 0)
        owns_port = any(
            connection.status == psutil.CONN_LISTEN
            and connection.laddr and int(connection.laddr.port) == port
            for connection in process.net_connections(kind="tcp")
        )
        return (port > 0 and owns_port and abs(process.create_time() - expected_time) < 0.01
                and actual_executable == expected_executable)
    except (ImportError, OSError, TypeError, ValueError):
        return False


def _worker_url(path: str, query: str = "", state: dict[str, Any] | None = None) -> str:
    current = state or _worker_state()
    try:
        port = int(current.get("port") or 0)
        pid = int(current.get("pid") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="llama.cpp worker state is unavailable") from exc
    if port <= 0 or pid <= 0 or port == COORDINATOR_PORT or not _worker_identity_ok(current):
        raise HTTPException(status_code=503, detail="llama.cpp worker is not running")
    target = f"http://127.0.0.1:{port}/v1/{path.lstrip('/')}"
    return target + ("?" + query if query else "")


def logical_role(body: bytes) -> str | None:
    try:
        payload = json.loads(body)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return {"main-local": "main", "compression-local": "compression"}.get(str(payload.get("model") or ""))


def _safe_context_value(value: Any) -> str:
    return _CONTEXT_VALUE_RE.sub("_", str(value or "").strip())[:128].strip("_")


def record_request_context(role: str, profile: str, session: str) -> bool:
    """Journal a privacy-safe identity at the current activity byte offset."""
    safe_role = str(role or "").strip().lower()
    safe_profile = _safe_context_value(profile)
    safe_session = _safe_context_value(session)
    if safe_role not in {"main", "compression"} or not safe_profile or not safe_session:
        return False
    event = {
        "type": "hermes_request_context",
        "role": safe_role,
        "profile": safe_profile,
        "session": safe_session,
        "activity_offset": ACTIVITY_LOG_PATH.stat().st_size if ACTIVITY_LOG_PATH.exists() else 0,
    }
    REQUEST_CONTEXT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _activity_log_lock, REQUEST_CONTEXT_LOG_PATH.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    return True


def validate_logical_model(body: bytes) -> str:
    """Reject physical model ids so every inference request uses role locking."""
    try:
        payload = json.loads(body)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="llama.cpp requests must use a configured logical model") from exc
    if not isinstance(payload, dict) or "model" not in payload:
        raise HTTPException(status_code=400, detail="llama.cpp requests must use a configured logical model")
    model = str(payload.get("model") or "")
    role = {"main-local": "main", "compression-local": "compression"}.get(model)
    if role is None:
        raise HTTPException(status_code=400, detail="llama.cpp requests must use a configured logical model")
    return role


def rewrite_logical_model(body: bytes, state: dict[str, Any]) -> bytes:
    if not body:
        return body
    try:
        payload = json.loads(body)
    except (TypeError, ValueError, json.JSONDecodeError):
        return body
    if not isinstance(payload, dict):
        return body
    role = logical_role(body)
    if not role:
        return body
    profiles = state.get("execution_profiles")
    profile = profiles.get(role) if isinstance(profiles, dict) else None
    if role == "main" and not isinstance(profile, dict) and state.get("active_model_id"):
        profile = {"model_id": state.get("active_model_id")}
    model_id = str(profile.get("model_id") or "") if isinstance(profile, dict) else ""
    if not model_id:
        raise HTTPException(status_code=503, detail=f"{role} execution profile is not configured")

    model_settings = state.get("model_settings")
    settings = model_settings.get(model_id) if isinstance(model_settings, dict) else None
    saved_effort = settings.get("reasoning-effort") if isinstance(settings, dict) else None
    normalized_effort = str(saved_effort).strip() if saved_effort is not None else ""
    if normalized_effort:
        payload["reasoning_effort"] = normalized_effort
    else:
        payload.pop("reasoning_effort", None)

    if state.get("execution_mode") != "native_router":
        payload["model"] = model_id
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _filtered_headers(headers: Any) -> dict[str, str]:
    excluded = set(_HOP_BY_HOP) | {
        "host", "content-length", "authorization", "x-hermes-profile", "x-hermes-session",
    }
    return {str(key): str(value) for key, value in headers.items() if str(key).lower() not in excluded}


@router.post("/__llamacpp/request-context", include_in_schema=False)
async def publish_request_context(body: dict[str, Any]) -> dict[str, Any]:
    role = {"main-local": "main", "compression-local": "compression"}.get(
        str(body.get("model") or "")
    )
    accepted = record_request_context(role or "", body.get("profile"), body.get("session"))
    if not accepted:
        raise HTTPException(status_code=400, detail="invalid Hermes request context")
    return {"ok": True}


def streaming_response(client: Any, upstream: Any,
                       finalize: Callable[[], Awaitable[None]] | None = None) -> StreamingResponse:
    """Close the upstream socket and execution lease with the downstream body."""

    cleanup_lock = asyncio.Lock()
    cleaned = False

    async def cleanup() -> None:
        nonlocal cleaned
        async with cleanup_lock:
            if cleaned:
                return
            cleaned = True
            with anyio.CancelScope(shield=True):
                try:
                    await _await_uncancelled(upstream.aclose(), propagate_cancel=False)
                except BaseException:  # noqa: BLE001 - one failed close must not skip remaining cleanup
                    pass
                try:
                    await _await_uncancelled(client.aclose(), propagate_cancel=False)
                except BaseException:  # noqa: BLE001 - execution finalization is the stronger invariant
                    pass
                if finalize is not None:
                    await finalize()

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        except asyncio.CancelledError:
            raise
        finally:
            await cleanup()

    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        headers=_filtered_headers(upstream.headers),
        media_type=None,
        background=BackgroundTask(cleanup),
    )


def _execution_backend() -> Any:
    try:
        import backend_impl
    except ImportError:  # pragma: no cover - package import fallback
        from . import backend_impl
    return backend_impl


async def _publish_counts() -> None:
    await asyncio.to_thread(
        _execution_backend().update_execution_queue,
        _queued_main_requests,
        _active_main_requests,
    )


async def _await_uncancelled(awaitable: Awaitable[Any], propagate_cancel: bool = True) -> Any:
    """Finish an owned lifecycle operation before propagating caller cancellation."""
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        current = asyncio.current_task()
        if current is not None and hasattr(current, "uncancel"):
            while current.cancelling():
                current.uncancel()
        try:
            result = await task
        except BaseException as cleanup_error:  # noqa: BLE001
            raise cleanup_error from cancelled
        if propagate_cancel:
            raise cancelled
        return result


async def _ensure_role(role: str) -> None:
    loop = asyncio.get_running_loop()
    operation = loop.run_in_executor(None, _execution_backend().ensure_execution_role, role)
    await _await_uncancelled(operation)


def _role_configured(role: str) -> bool:
    checker = getattr(_execution_backend(), "execution_role_configured", None)
    return True if checker is None else bool(checker(role))


async def _abort_transition(role: str, error: BaseException) -> None:
    backend = _execution_backend()
    abort = getattr(backend, "abort_execution_transition", None)
    if abort is not None:
        loop = asyncio.get_running_loop()
        operation = loop.run_in_executor(None, abort, role, str(error))
        await _await_uncancelled(operation)


async def _begin_execution(role: str | None) -> Callable[[], Awaitable[None]] | None:
    global _active_main_requests, _queued_main_requests
    transition_lock = _transition_lock
    counter_lock = _counter_lock
    if role is None:
        return None
    if role == "main":
        queued = False
        active = False
        acquired = False
        try:
            async with counter_lock:
                _queued_main_requests += 1
                queued = True
                await _publish_counts()
            await transition_lock.acquire()
            acquired = True
            await _ensure_role("main")
            async with counter_lock:
                _queued_main_requests -= 1
                queued = False
                _active_main_requests += 1
                active = True
                await _publish_counts()
        except BaseException:
            async with counter_lock:
                changed = False
                if queued:
                    _queued_main_requests = max(0, _queued_main_requests - 1)
                    queued = False
                    changed = True
                if active:
                    _active_main_requests = max(0, _active_main_requests - 1)
                    active = False
                    changed = True
                if changed:
                    try:
                        with anyio.CancelScope(shield=True):
                            await _await_uncancelled(_publish_counts(), propagate_cancel=False)
                    except BaseException:  # noqa: BLE001 - preserve the original request failure
                        pass
            if acquired:
                transition_lock.release()
            raise
        transition_lock.release()

        async def finish_main() -> None:
            global _active_main_requests
            async with counter_lock:
                _active_main_requests = max(0, _active_main_requests - 1)
                await _publish_counts()

        return finish_main

    restore_main = _role_configured("main")
    await transition_lock.acquire()
    try:
        while True:
            async with counter_lock:
                if _active_main_requests == 0:
                    break
            await asyncio.sleep(0.02)
        await _ensure_role("compression")
    except BaseException as original_error:
        try:
            if restore_main:
                await _ensure_role("main")
        except asyncio.CancelledError:
            pass
        except BaseException as restore_error:
            await _abort_transition("compression", restore_error)
            original_error.add_note(f"Main restoration also failed: {restore_error}")
        finally:
            transition_lock.release()
        raise

    async def finish_compression() -> None:
        try:
            if restore_main:
                try:
                    await _ensure_role("main")
                except asyncio.CancelledError:
                    raise
                except BaseException as restore_error:
                    await _abort_transition("compression", restore_error)
                    raise
        finally:
            transition_lock.release()

    return finish_compression


@router.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"], include_in_schema=False)
async def proxy_inference(request: Request, path: str) -> StreamingResponse:
    finalize: Callable[[], Awaitable[None]] | None = None
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=None, write=120.0, pool=5.0),
        trust_env=False,
        follow_redirects=False,
    )
    try:
        raw_body = await request.body()
        role = validate_logical_model(raw_body) if request.method not in {"GET", "DELETE"} else None
        if role:
            record_request_context(
                role,
                request.headers.get("x-hermes-profile"),
                request.headers.get("x-hermes-session"),
            )
        finalize = await _begin_execution(role)
        state = _worker_state()
        body = rewrite_logical_model(raw_body, state)
        upstream_request = client.build_request(
            request.method,
            _worker_url(path, request.url.query, state),
            headers=_filtered_headers(request.headers),
            content=body,
        )
        upstream = await client.send(upstream_request, stream=True)
    except asyncio.CancelledError:
        async def cleanup() -> None:
            try:
                await client.aclose()
            finally:
                if finalize is not None:
                    await finalize()
        with anyio.CancelScope(shield=True):
            await cleanup()
        raise
    except Exception as exc:  # noqa: BLE001
        async def cleanup() -> None:
            try:
                await client.aclose()
            finally:
                if finalize is not None:
                    await finalize()
        with anyio.CancelScope(shield=True):
            await cleanup()
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=503, detail=f"llama.cpp worker unavailable: {exc}") from exc
    return streaming_response(client, upstream, finalize)
