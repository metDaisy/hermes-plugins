"""Profile-local proxy to the machine-scoped llama.cpp backend."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

router = APIRouter()

RUNTIME_ROOT = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "hermes" / "runtimes" / "llamacpp"
COORDINATOR_PORT = 18380
COORDINATOR_URL = f"http://127.0.0.1:{COORDINATOR_PORT}"
COORDINATOR_SCRIPT = Path(__file__).with_name("coordinator_server.py")
START_LOCK = RUNTIME_ROOT / "backend-start.lock"
_HEALTH_PATH = "/__llamacpp_backend_health"
_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}


def _healthy() -> bool:
    try:
        with urllib.request.urlopen(COORDINATOR_URL + _HEALTH_PATH, timeout=0.7) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _try_start_coordinator() -> None:
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    lock_fd: int | None = None
    try:
        try:
            lock_fd = os.open(str(START_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(lock_fd, str(os.getpid()).encode("ascii", errors="ignore"))
        except FileExistsError:
            try:
                if time.time() - START_LOCK.stat().st_mtime > 30 and not _healthy():
                    START_LOCK.unlink(missing_ok=True)
            except OSError:
                pass
            return
        if _healthy():
            return
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(
            [sys.executable, str(COORDINATOR_SCRIPT)],
            cwd=str(COORDINATOR_SCRIPT.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
            try:
                START_LOCK.unlink(missing_ok=True)
            except OSError:
                pass


def _ensure_coordinator() -> None:
    if _healthy():
        return
    _try_start_coordinator()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if _healthy():
            return
        time.sleep(0.2)
    raise RuntimeError("shared llama.cpp backend did not become healthy")


def _request_backend(method: str, path: str, query: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, str], bytes]:
    target = COORDINATOR_URL + "/" + path.lstrip("/")
    if query:
        target += "?" + query
    request = urllib.request.Request(target, data=body or None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers.items()), error.read()


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"], include_in_schema=False)
async def proxy(request: Request, path: str) -> Response:
    try:
        await asyncio.to_thread(_ensure_coordinator)
        body = await request.body()
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in _HOP_BY_HOP and key.lower() not in {"host", "content-length"}
        }
        status, response_headers, response_body = await asyncio.to_thread(
            _request_backend, request.method, path, request.url.query, headers, body
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"shared llama.cpp backend unavailable: {exc}") from exc
    filtered_headers = {
        key: value
        for key, value in response_headers.items()
        if key.lower() not in _HOP_BY_HOP and key.lower() not in {"content-length", "content-encoding"}
    }
    return Response(content=response_body, status_code=status, headers=filtered_headers)
