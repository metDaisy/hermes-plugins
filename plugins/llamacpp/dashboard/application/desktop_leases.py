"""Machine-scoped Hermes Desktop client leases."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable


class DesktopLeaseRegistry:
    """Expire crashed/closed Desktop clients without trusting unload requests."""

    def __init__(self, timeout_seconds: float = 8.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._timeout_seconds = max(1.0, float(timeout_seconds))
        self._clock = clock
        self._lock = threading.RLock()
        self._leases: dict[str, float] = {}
        self._armed = False

    def touch(self, client_id: str) -> int:
        normalized = str(client_id or "").strip()
        if not normalized or len(normalized) > 128:
            raise ValueError("valid desktop client_id is required")
        with self._lock:
            self._armed = True
            self._leases[normalized] = self._clock()
            return self._prune_locked()

    def release(self, client_id: str) -> int:
        normalized = str(client_id or "").strip()
        with self._lock:
            self._leases.pop(normalized, None)
            return self._prune_locked()

    def active_count(self) -> int:
        with self._lock:
            return self._prune_locked()

    def should_shutdown(self, execution_busy: bool = False) -> bool:
        with self._lock:
            return self._armed and self._prune_locked() == 0 and not execution_busy

    def _prune_locked(self) -> int:
        cutoff = self._clock() - self._timeout_seconds
        expired = [client_id for client_id, touched in self._leases.items() if touched < cutoff]
        for client_id in expired:
            self._leases.pop(client_id, None)
        return len(self._leases)