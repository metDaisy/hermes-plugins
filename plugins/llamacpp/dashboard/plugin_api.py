"""Profile-local proxy to the machine-scoped llama.cpp backend."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import httpx
import anyio
import websockets
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

router = APIRouter()

RUNTIME_ROOT = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "hermes" / "runtimes" / "llamacpp"
COORDINATOR_PORT = 18380
COORDINATOR_URL = f"http://127.0.0.1:{COORDINATOR_PORT}"
COORDINATOR_WS_URL = f"ws://127.0.0.1:{COORDINATOR_PORT}/events"
COORDINATOR_SCRIPT = Path(__file__).with_name("coordinator_server.py")
START_LOCK = RUNTIME_ROOT / "backend-start.lock"
COORDINATOR_STOP_MARKER = RUNTIME_ROOT / "coordinator-stopped"
COORDINATOR_LOG = RUNTIME_ROOT / "logs" / "activity.log"
_HEALTH_PATH = "/__llamacpp_backend_health"
_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}
COORDINATOR_SERVICE = "hermes-llamacpp-coordinator"
COORDINATOR_PROTOCOL = 1
COORDINATOR_BUILD = "0.3.9"
_READ_ONLY_BACKEND_MODULE = "llamacpp_profile_read_only_backend"
_read_only_backend_lock = threading.RLock()


def _health_payload() -> dict[str, object] | None:
    try:
        with urllib.request.urlopen(COORDINATOR_URL + _HEALTH_PATH, timeout=0.7) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return payload if response.status == 200 and isinstance(payload, dict) else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def _trusted_coordinator_script(argument: str) -> bool:
    try:
        candidate = Path(argument).resolve()
        if candidate == COORDINATOR_SCRIPT.resolve():
            return True
        relative = candidate.relative_to((RUNTIME_ROOT.parents[1] / "profiles").resolve())
        return (
            len(relative.parts) == 5
            and relative.parts[1:] == ("plugins", "llamacpp", "dashboard", "coordinator_server.py")
        )
    except (OSError, ValueError):
        return False


def _coordinator_process_ok(payload: dict[str, object]) -> bool:
    try:
        import psutil
        pid = int(payload.get("pid") or 0)
        if pid <= 0:
            return False
        process = psutil.Process(pid)
        actual_executable = os.path.normcase(os.path.abspath(process.exe()))
        executable_name = Path(actual_executable).name.lower()
        command = process.cmdline()
        reported_executable = str(payload.get("executable") or actual_executable)
        reported_name = Path(reported_executable).name.lower()
        try:
            command_uses_reported_executable = os.path.samefile(str(command[0]), reported_executable)
        except (OSError, IndexError):
            command_uses_reported_executable = bool(
                command
                and os.path.normcase(os.path.abspath(str(command[0])))
                == os.path.normcase(os.path.abspath(reported_executable))
            )
        owns_port = any(
            connection.status == psutil.CONN_LISTEN
            and connection.laddr and int(connection.laddr.port) == COORDINATOR_PORT
            for connection in process.net_connections(kind="tcp")
        )
        return (
            owns_port
            and executable_name in {"python", "python.exe", "python3", "python3.exe", "pythonw.exe"}
            and reported_name in {"python", "python.exe", "python3", "python3.exe", "pythonw.exe"}
            and len(command) >= 2
            and command_uses_reported_executable
            and _trusted_coordinator_script(str(command[1]))
        )
    except Exception:  # noqa: BLE001 - identity failure must fail closed
        return False


def _healthy() -> bool:
    payload = _health_payload()
    return bool(
        payload
        and payload.get("ok") is True
        and payload.get("service") == COORDINATOR_SERVICE
        and payload.get("protocol") == COORDINATOR_PROTOCOL
        and _coordinator_process_ok(payload)
    )


def _stop_incompatible_coordinator() -> None:
    payload = _health_payload()
    if not payload:
        return
    try:
        import psutil
        pid = int(payload.get("pid") or 0)
        if pid <= 0:
            for connection in psutil.net_connections(kind="tcp"):
                local = getattr(connection, "laddr", None)
                port = getattr(local, "port", local[1] if isinstance(local, tuple) and len(local) > 1 else 0)
                if (connection.status == psutil.CONN_LISTEN and int(port or 0) == COORDINATOR_PORT
                        and int(getattr(connection, "pid", 0) or 0) > 0):
                    pid = int(connection.pid)
                    break
        if pid <= 0:
            return
        process = psutil.Process(pid)
        owns_port = any(
            connection.status == psutil.CONN_LISTEN
            and connection.laddr and int(connection.laddr.port) == COORDINATOR_PORT
            for connection in process.net_connections(kind="tcp")
        )
        if not owns_port or not _coordinator_process_ok({"pid": pid}):
            return
        process.terminate()
        process.wait(timeout=5)
    except Exception:  # noqa: BLE001 - replacement is best-effort and process identity checked first
        return


def _release_start_lock() -> None:
    try:
        START_LOCK.unlink(missing_ok=True)
    except OSError:
        pass


def _try_start_coordinator() -> bool:
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    lock_fd: int | None = None
    try:
        acquired = False
        for attempt in range(2):
            try:
                lock_fd = os.open(str(START_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(lock_fd, str(os.getpid()).encode("ascii", errors="ignore"))
                acquired = True
                break
            except FileExistsError:
                removed = False
                try:
                    if time.time() - START_LOCK.stat().st_mtime > 30 and not _healthy():
                        START_LOCK.unlink(missing_ok=True)
                        removed = True
                except OSError:
                    pass
                if not removed or attempt == 1:
                    return False
        if not acquired:
            return False
        if _healthy():
            _release_start_lock()
            return False
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        log_path = COORDINATOR_LOG
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab", buffering=0) as output:
            subprocess.Popen(
                [sys.executable, str(COORDINATOR_SCRIPT)],
                cwd=str(COORDINATOR_SCRIPT.parent),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
        return True
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


def _ensure_coordinator(*, allow_start: bool = False) -> None:
    restarting_after_stop = allow_start and COORDINATOR_STOP_MARKER.exists()
    if allow_start:
        COORDINATOR_STOP_MARKER.unlink(missing_ok=True)
    elif COORDINATOR_STOP_MARKER.exists():
        raise RuntimeError("shared llama.cpp backend was intentionally stopped")
    if restarting_after_stop:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _healthy():
            time.sleep(0.1)
    elif _healthy():
        return
    _stop_incompatible_coordinator()
    started = _try_start_coordinator()
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            if _healthy():
                _release_start_lock()
                return
            time.sleep(0.2)
    finally:
        if started:
            _release_start_lock()
    raise RuntimeError("shared llama.cpp backend did not become healthy")


def _coordinator_start_requested(method: str, path: str, body: bytes) -> bool:
    normalized = path.strip("/")
    if method != "POST":
        return False
    if normalized in {"profiles/start", "runtime/install"}:
        return True
    if normalized != "server":
        return False
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, ValueError):
        return False
    return str(payload.get("action") or "") == "start"


def _read_only_backend():
    with _read_only_backend_lock:
        loaded = sys.modules.get(_READ_ONLY_BACKEND_MODULE)
        if loaded is not None:
            return loaded
        backend_path = Path(__file__).with_name("backend_impl.py")
        spec = importlib.util.spec_from_file_location(_READ_ONLY_BACKEND_MODULE, backend_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load read-only llama.cpp backend from {backend_path}")
        backend = importlib.util.module_from_spec(spec)
        sys.modules[_READ_ONLY_BACKEND_MODULE] = backend
        try:
            spec.loader.exec_module(backend)
        except BaseException:
            sys.modules.pop(_READ_ONLY_BACKEND_MODULE, None)
            raise
        return backend


def _stopped_payload(
    path: str,
    query: object,
    method: str = "GET",
    body: bytes = b"",
) -> dict[str, object] | None:
    """Serve snapshots and safe parameter persistence while the coordinator is stopped."""
    normalized = path.strip("/")
    backend = _read_only_backend()
    getter = getattr(query, "get", lambda *_args: None)
    if method == "GET":
        if normalized == "status":
            payload = backend.status()
            payload["server_running"] = False
            payload["coordinator"] = {
                "ok": False,
                "stopped": True,
                "port": COORDINATOR_PORT,
                "singleton": True,
                "inference_base_url": f"http://127.0.0.1:{COORDINATOR_PORT}/v1",
                "cancellation_propagation": True,
            }
            return payload
        if normalized == "logs":
            return backend.activity_logs(max(1, min(int(getter("limit") or 250), 1000)))
        if normalized == "metrics":
            return backend.resource_metrics(int(getter("window") or 60))
        if normalized == "jobs":
            return {"jobs": []}
        if normalized == "hf-models":
            return backend._local_hf_models()
        if normalized == "presets":
            return backend.presets(str(getter("model_id") or ""))
        if normalized == "settings":
            query_text = str(getter("q") or "").strip().lower()
            limit = max(1, min(int(getter("limit") or 50), 100))
            options = [
                option for option in backend._option_list()
                if query_text and (
                    query_text in option["key"].lower()
                    or query_text in option["name"].lower()
                    or query_text in option["description"].lower()
                )
            ]
            return {
                "editable": True,
                "query": str(getter("q") or ""),
                "options": options[:limit],
                "runtime": str(backend._server_executable() or ""),
                "message": "plugin이 관리하는 llama-server --help에서 검색합니다.",
            }
        if normalized.startswith("settings/"):
            return backend.model_settings(normalized.removeprefix("settings/"))
        return None

    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")

    if method == "PUT" and normalized.startswith("settings/"):
        return backend.save_model_settings(normalized.removeprefix("settings/"), payload)
    if method == "POST" and normalized == "presets":
        return backend.create_preset(payload)

    parts = normalized.split("/")
    if len(parts) >= 2 and parts[0] == "presets":
        preset_id = parts[1]
        if method == "POST" and len(parts) == 3 and parts[2] == "apply":
            return backend.apply_preset(preset_id, payload)
        if method == "PATCH" and len(parts) == 2:
            return backend.rename_preset(preset_id, payload)
        if method == "DELETE" and len(parts) == 2:
            return backend.delete_preset(preset_id)
    return None


def _ws_upgrade_authorized(websocket: WebSocket) -> bool:
    """Use the dashboard's canonical token/ticket gate when loaded by Hermes."""
    try:
        from hermes_cli import web_server_chat as websocket_auth
    except Exception:
        return True
    return bool(websocket_auth._ws_auth_ok(websocket))


@router.websocket("/events")
async def proxy_events(websocket: WebSocket) -> None:
    if not _ws_upgrade_authorized(websocket):
        await websocket.close(code=4401)
        return
    try:
        await asyncio.to_thread(_ensure_coordinator)
    except Exception:
        await websocket.close(code=1013)
        return
    await websocket.accept()
    try:
        async with websockets.connect(
            COORDINATOR_WS_URL,
            open_timeout=5,
            close_timeout=2,
            ping_interval=20,
            max_size=1 << 20,
        ) as upstream:
            async for message in upstream:
                await websocket.send_text(str(message))
    except (WebSocketDisconnect, websockets.ConnectionClosed):
        return


def _filtered_headers(headers: object) -> dict[str, str]:
    items = getattr(headers, "items")()
    excluded = set(_HOP_BY_HOP) | {"host", "content-length", "authorization"}
    return {str(key): str(value) for key, value in items if str(key).lower() not in excluded}


async def _await_uncancelled(awaitable: object) -> object:
    task = asyncio.ensure_future(awaitable)  # type: ignore[arg-type]
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        current = asyncio.current_task()
        if current is not None and hasattr(current, "uncancel"):
            while current.cancelling():
                current.uncancel()
        try:
            return await task
        except BaseException as cleanup_error:  # noqa: BLE001
            raise cleanup_error from cancelled


def _streaming_response(client: httpx.AsyncClient, upstream: httpx.Response) -> StreamingResponse:
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
                    await _await_uncancelled(upstream.aclose())
                except BaseException:  # noqa: BLE001 - client close must still run
                    pass
                try:
                    await _await_uncancelled(client.aclose())
                except BaseException:  # noqa: BLE001 - cleanup is best-effort and idempotent
                    pass

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        except asyncio.CancelledError:
            raise
        finally:
            await cleanup()

    return StreamingResponse(
        body(), status_code=upstream.status_code,
        headers=_filtered_headers(upstream.headers), media_type=None,
        background=BackgroundTask(cleanup),
    )


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"], include_in_schema=False)
async def proxy(request: Request, path: str) -> StreamingResponse:
    body = await request.body()
    if COORDINATOR_STOP_MARKER.exists():
        stopped_payload = await asyncio.to_thread(
            _stopped_payload, path, request.query_params, request.method, body,
        )
        if stopped_payload is not None:
            return JSONResponse(stopped_payload)
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=None, write=120.0, pool=5.0),
        trust_env=False,
        follow_redirects=False,
    )
    try:
        await asyncio.to_thread(
            _ensure_coordinator,
            allow_start=_coordinator_start_requested(request.method, path, body),
        )
        target = COORDINATOR_URL + "/" + path.lstrip("/")
        if request.url.query:
            target += "?" + request.url.query
        upstream_request = client.build_request(
            request.method, target, headers=_filtered_headers(request.headers), content=body,
        )
        upstream = await client.send(upstream_request, stream=True)
    except asyncio.CancelledError:
        await client.aclose()
        raise
    except Exception as exc:  # noqa: BLE001
        await client.aclose()
        raise HTTPException(status_code=503, detail=f"shared llama.cpp backend unavailable: {exc}") from exc
    return _streaming_response(client, upstream)
