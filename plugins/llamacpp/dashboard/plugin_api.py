"""Profile-local proxy to the machine-scoped llama.cpp backend."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import httpx
import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

router = APIRouter()

RUNTIME_ROOT = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "hermes" / "runtimes" / "llamacpp"
COORDINATOR_PORT = 18380
COORDINATOR_URL = f"http://127.0.0.1:{COORDINATOR_PORT}"
COORDINATOR_SCRIPT = Path(__file__).with_name("coordinator_server.py")
START_LOCK = RUNTIME_ROOT / "backend-start.lock"
COORDINATOR_LOG = RUNTIME_ROOT / "logs" / "activity.log"
_HEALTH_PATH = "/__llamacpp_backend_health"
_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}
COORDINATOR_SERVICE = "hermes-llamacpp-coordinator"
COORDINATOR_PROTOCOL = 1
COORDINATOR_BUILD = "0.2.44"


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
        and payload.get("build") == COORDINATOR_BUILD
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


def _ensure_coordinator() -> None:
    if _healthy():
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
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=None, write=120.0, pool=5.0),
        trust_env=False,
        follow_redirects=False,
    )
    try:
        await asyncio.to_thread(_ensure_coordinator)
        body = await request.body()
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
