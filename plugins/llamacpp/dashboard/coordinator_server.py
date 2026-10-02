"""Machine-scoped llama.cpp plugin backend process."""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

DASHBOARD_DIR = Path(__file__).resolve().parent
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

from backend_impl import router, shutdown_machine_runtime, update_execution_queue  # noqa: E402
from inference_proxy import execution_busy, router as inference_router  # noqa: E402

try:  # noqa: E402
    from application.coordinator_lock import MachineFileLock
    from application.desktop_leases import DesktopLeaseRegistry
except ImportError:  # pragma: no cover - package import fallback
    from .application.coordinator_lock import MachineFileLock
    from .application.desktop_leases import DesktopLeaseRegistry

RUNTIME_ROOT = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "hermes" / "runtimes" / "llamacpp"
COORDINATOR_PORT = int(os.environ.get("LLAMACPP_COORDINATOR_PORT") or 18380)
LIFETIME_LOCK = RUNTIME_ROOT / ("coordinator.lock" if COORDINATOR_PORT == 18380 else f"coordinator-{COORDINATOR_PORT}.lock")
COORDINATOR_SERVICE = "hermes-llamacpp-coordinator"
COORDINATOR_PROTOCOL = 1
COORDINATOR_BUILD = "0.2.42"
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


app.include_router(inference_router)
app.include_router(router)


if __name__ == "__main__":
    import uvicorn

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
                if not _desktop_leases.should_shutdown(execution_busy()):
                    continue
                try:
                    shutdown_machine_runtime()
                finally:
                    server.should_exit = True
                return

        watchdog = threading.Thread(target=stop_after_last_desktop, daemon=True, name="llamacpp-desktop-lease")
        watchdog.start()
        server.run()
    finally:
        if "watchdog_stop" in locals():
            watchdog_stop.set()
        try:
            shutdown_machine_runtime()
        except Exception as exc:  # noqa: BLE001 - process exit and Job Object remain the final cleanup boundary
            print(f"[coordinator] shutdown cleanup failed: {exc}", flush=True)
        lifetime.release()
