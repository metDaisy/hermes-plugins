"""Verified operations for a plugin-owned worker process."""
from __future__ import annotations

from pathlib import Path
from typing import Any


def terminate_owned_process(process: Any) -> bool:
    """Terminate an owned handle and return only after exit is confirmed."""
    try:
        process.terminate()
        process.wait(timeout=10)
        return True
    except Exception:  # noqa: BLE001
        try:
            process.kill()
            process.wait(timeout=5)
            return True
        except Exception:  # noqa: BLE001
            try:
                return process.poll() is not None
            except Exception:  # noqa: BLE001
                return False


def capture_process_identity(process: Any) -> dict[str, Any]:
    """Capture the mandatory identity fields for a newly spawned worker."""
    import psutil

    worker = psutil.Process(process.pid)
    return {
        "pid": process.pid,
        "create_time": worker.create_time(),
        "executable": str(Path(worker.exe()).resolve()),
    }
