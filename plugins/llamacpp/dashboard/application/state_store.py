"""Thread-safe state persistence for the llamacpp dashboard.

Owns the single reason to change for machine-scoped runtime state: reading and
writing ``state.json`` with atomic writes and a process lock. All other
concerns (jobs, downloads, options, server lifecycle) read state through the
services that build on this.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Callable


class StateStore:
    """Reads and writes the process state file atomically under a lock."""

    def __init__(
        self,
        path: Path,
        defaults: Callable[[], dict[str, Any]],
    ) -> None:
        self._path = path
        self._defaults = defaults
        self._lock = threading.RLock()

    def read(self, fallback: Any = None, path: Path | None = None) -> Any:
        target = path or self._path
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return fallback if fallback is not None else self._defaults()

    def write(self, path: Path, value: Any) -> None:
        target = path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, target)

    def load(self) -> dict[str, Any]:
        """Return a merged state dict, applying defaults over any file contents."""
        with self._lock:
            value = self.read(None, self._path)
            state = self._defaults()
            if isinstance(value, dict):
                state.update(value)
            return state

    def save(self, state: dict[str, Any]) -> None:
        with self._lock:
            self.write(self._path, state)
