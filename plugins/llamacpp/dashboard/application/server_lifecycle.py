"""Managed llama-server lifecycle operations.

The module owns process-state cleanup, endpoint cleanup, log retention, and
bounded log inspection. The backend facade supplies platform process control and
endpoint registration adapters.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable


class ServerLifecycleService:
    """Keep server lifecycle state transitions consistent across callers."""

    def __init__(
        self,
        load_state: Callable[[], dict[str, Any]],
        save_state: Callable[[dict[str, Any]], None],
        pid_alive: Callable[[Any], bool],
        terminate: Callable[[int], None],
        unregister_endpoint: Callable[[], None],
        log_path: Path,
    ) -> None:
        self._load_state = load_state
        self._save_state = save_state
        self._pid_alive = pid_alive
        self._terminate = terminate
        self._unregister_endpoint = unregister_endpoint
        self._log_path = log_path

    def stop(self, preserve_log: bool = False) -> None:
        state = self._load_state()
        pid = state.get("pid")
        if self._pid_alive(pid):
            self._terminate(pid)
        self._clear_owned_state(state)
        self._unregister_endpoint()
        if not preserve_log:
            self._remove_log_when_released()

    def _remove_log_when_released(self) -> None:
        """Avoid reporting a failed stop for Windows' short-lived file-handle race."""
        for attempt in range(20):
            try:
                self._log_path.unlink(missing_ok=True)
                return
            except PermissionError:
                if attempt == 19:
                    return
                time.sleep(0.1)

    def watch(self, process: Any) -> None:
        def wait_for_exit() -> None:
            process.wait()
            state = self._load_state()
            if state.get("pid") != process.pid:
                return
            self._clear_owned_state(state)
            self._unregister_endpoint()

        threading.Thread(target=wait_for_exit, daemon=True, name="llamacpp-server-watch").start()

    def log_tail(self, limit: int = 250) -> dict[str, Any]:
        bounded = max(1, min(int(limit), 500))
        try:
            size_bytes = self._log_path.stat().st_size
            with self._log_path.open("rb") as stream:
                stream.seek(max(0, size_bytes - 256 * 1024))
                text = stream.read().decode("utf-8", errors="replace")
        except OSError:
            return {"path": str(self._log_path), "lines": [], "size_bytes": 0}
        return {"path": str(self._log_path), "lines": text.splitlines()[-bounded:], "size_bytes": size_bytes}

    def _clear_owned_state(self, state: dict[str, Any]) -> None:
        state["pid"] = None
        state["custom_endpoint"] = None
        self._save_state(state)
