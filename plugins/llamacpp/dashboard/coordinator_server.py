"""Machine-scoped llama.cpp plugin backend process."""
from __future__ import annotations

import asyncio
import os
import sys
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

DASHBOARD_DIR = Path(__file__).resolve().parent
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

from backend_impl import (  # noqa: E402
    coordinator_shutdown_requested, record_resource_sample, router,
    shutdown_machine_runtime, update_execution_queue,
)
from inference_proxy import execution_busy, router as inference_router  # noqa: E402

try:  # noqa: E402
    from application.coordinator_lock import MachineFileLock
    from application.desktop_leases import DesktopLeaseRegistry
    from application.event_bus import EVENT_BUS
    from application.resource_history import start_resource_sampler
except ImportError:  # pragma: no cover - package import fallback
    from .application.coordinator_lock import MachineFileLock
    from .application.desktop_leases import DesktopLeaseRegistry
    from .application.event_bus import EVENT_BUS
    from .application.resource_history import start_resource_sampler

RUNTIME_ROOT = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "hermes" / "runtimes" / "llamacpp"
COORDINATOR_PORT = int(os.environ.get("LLAMACPP_COORDINATOR_PORT") or 18380)
LIFETIME_LOCK = RUNTIME_ROOT / ("coordinator.lock" if COORDINATOR_PORT == 18380 else f"coordinator-{COORDINATOR_PORT}.lock")
COORDINATOR_STOP_MARKER = RUNTIME_ROOT / "coordinator-stopped"
COORDINATOR_SERVICE = "hermes-llamacpp-coordinator"
COORDINATOR_PROTOCOL = 1
COORDINATOR_BUILD = "0.2.81"
DESKTOP_LEASE_TIMEOUT_SECONDS = 8.0
_desktop_leases = DesktopLeaseRegistry(DESKTOP_LEASE_TIMEOUT_SECONDS)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/__llamacpp_backend_health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": COORDINATOR_SERVICE,
        "protocol": COORDINATOR_PROTOCOL,
        "build": COORDINATOR_BUILD,
        "pid": os.getpid(),
        "executable": sys.executable,
        "desktop_clients": _desktop_leases.active_count(),
    }


@app.post("/lifecycle/lease")
def desktop_lease(body: dict[str, Any]) -> dict[str, Any]:
    try:
        clients = _desktop_leases.touch(str(body.get("client_id") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "desktop_clients": clients, "timeout_seconds": DESKTOP_LEASE_TIMEOUT_SECONDS}


@app.delete("/lifecycle/lease")
def release_desktop_lease(body: dict[str, Any]) -> dict[str, Any]:
    clients = _desktop_leases.release(str(body.get("client_id") or ""))
    return {"ok": True, "desktop_clients": clients}


@app.websocket("/events")
async def coordinator_events(websocket: WebSocket) -> None:
    await websocket.accept()
    cursor = EVENT_BUS.latest_id()
    await websocket.send_json({
        "id": cursor,
        "type": "connected",
        "refresh": ["status", "jobs", "logs", "metrics"],
    })
    try:
        while True:
            batch = await asyncio.to_thread(EVENT_BUS.wait_after, cursor, 15.0)
            if batch["gap"]:
                await websocket.send_json({
                    "id": batch["cursor"],
                    "type": "gap",
                    "refresh": ["status", "jobs", "logs", "metrics"],
                })
            for event in batch["events"]:
                await websocket.send_json(event)
            cursor = int(batch["cursor"])
            if not batch["events"]:
                await websocket.send_json({"id": cursor, "type": "heartbeat", "refresh": []})
    except WebSocketDisconnect:
        return


app.include_router(inference_router)
app.include_router(router)


if __name__ == "__main__":
    import uvicorn

    if COORDINATOR_STOP_MARKER.exists():
        raise SystemExit(0)
    lifetime = MachineFileLock(LIFETIME_LOCK)
    if not lifetime.acquire():
        raise SystemExit(0)
    try:
        update_execution_queue(0, 0)
        print(f"{COORDINATOR_SERVICE} protocol={COORDINATOR_PROTOCOL} build={COORDINATOR_BUILD} pid={os.getpid()}", flush=True)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=COORDINATOR_PORT, log_level="warning"))
        watchdog_stop = threading.Event()

        def stop_after_last_desktop() -> None:
            while not watchdog_stop.wait(0.5):
                if coordinator_shutdown_requested():
                    try:
                        shutdown_machine_runtime()
                    finally:
                        server.should_exit = True
                    return
                if not _desktop_leases.should_shutdown(execution_busy()):
                    continue
                try:
                    shutdown_machine_runtime()
                finally:
                    server.should_exit = True
                return

        watchdog = threading.Thread(target=stop_after_last_desktop, daemon=True, name="llamacpp-desktop-lease")
        watchdog.start()
        telemetry_stop, _telemetry_thread = start_resource_sampler(record_resource_sample, interval_seconds=10.0)
        server.run()
    finally:
        if "telemetry_stop" in locals():
            telemetry_stop.set()
        if "watchdog_stop" in locals():
            watchdog_stop.set()
        try:
            shutdown_machine_runtime()
        except Exception as exc:  # noqa: BLE001 - process exit and Job Object remain the final cleanup boundary
            print(f"[coordinator] shutdown cleanup failed: {exc}", flush=True)
        lifetime.release()
