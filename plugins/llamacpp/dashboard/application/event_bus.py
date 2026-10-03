"""Bounded machine-local event stream for llama.cpp coordinator updates."""
from __future__ import annotations

from collections import deque
from pathlib import Path
from threading import Condition, Thread
from time import time
from typing import Any, Callable


class CoordinatorEventBus:
    """Keep a bounded ordered event history and wake streaming subscribers."""

    def __init__(self, max_events: int = 512) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=max(1, int(max_events)))
        self._condition = Condition()
        self._next_id = 1

    def publish(self, event_type: str, **payload: Any) -> dict[str, Any]:
        with self._condition:
            event = {
                "id": self._next_id,
                "type": str(event_type),
                "timestamp": time(),
                **payload,
            }
            self._next_id += 1
            self._events.append(event)
            self._condition.notify_all()
            return dict(event)

    def latest_id(self) -> int:
        with self._condition:
            return self._events[-1]["id"] if self._events else self._next_id - 1

    def events_after(self, cursor: int) -> dict[str, Any]:
        with self._condition:
            normalized = max(0, int(cursor))
            oldest = self._events[0]["id"] if self._events else self._next_id
            events = [dict(event) for event in self._events if event["id"] > normalized]
            latest = self._events[-1]["id"] if self._events else self._next_id - 1
            return {
                "cursor": latest,
                "gap": bool(self._events and normalized < oldest - 1),
                "events": events,
            }

    def wait_after(self, cursor: int, timeout: float = 15.0) -> dict[str, Any]:
        with self._condition:
            if not any(event["id"] > cursor for event in self._events):
                self._condition.wait(timeout=max(0.0, float(timeout)))
        return self.events_after(cursor)


EVENT_BUS = CoordinatorEventBus()


def publish_event(event_type: str, refresh: list[str] | tuple[str, ...] = (), **payload: Any) -> dict[str, Any]:
    unique_refresh = list(dict.fromkeys(str(item) for item in refresh if item))
    return EVENT_BUS.publish(event_type, refresh=unique_refresh, **payload)


def classify_activity_line(line: str) -> tuple[str, list[str]]:
    lowered = str(line or "").lower()
    if "model loaded" in lowered or "listening on http" in lowered or "server is listening" in lowered:
        return "worker_state", ["logs", "status", "metrics"]
    if "prompt processing" in lowered:
        return "prompt_progress", ["logs", "metrics"]
    if "prompt eval time" in lowered:
        return "prompt_completed", ["logs", "metrics"]
    if "eval time" in lowered or "n_gen =" in lowered:
        return "generation_progress", ["logs", "metrics"]
    if "stop processing" in lowered:
        return "request_finished", ["logs", "metrics"]
    return "log", ["logs"]


def publish_activity_line(line: str) -> dict[str, Any]:
    event_type, refresh = classify_activity_line(line)
    return publish_event(event_type, refresh=refresh)


def start_activity_pump(
    process: Any,
    log_path: Path,
    emit: Callable[[str, list[str]], Any] | None = None,
) -> Thread:
    """Persist worker stdout and publish privacy-safe cache invalidations."""
    publisher = emit or (lambda event_type, refresh: publish_event(event_type, refresh=refresh))

    def pump() -> None:
        stdout = getattr(process, "stdout", None)
        if stdout is None:
            return
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with log_path.open("ab", buffering=0) as sink:
                while True:
                    raw = stdout.readline()
                    if not raw:
                        break
                    if not isinstance(raw, (bytes, str)):
                        break
                    encoded = raw if isinstance(raw, bytes) else str(raw).encode("utf-8", errors="replace")
                    sink.write(encoded)
                    line = encoded.decode("utf-8", errors="replace").rstrip("\r\n")
                    event_type, refresh = classify_activity_line(line)
                    publisher(event_type, refresh)
        finally:
            try:
                stdout.close()
            except Exception:  # noqa: BLE001 - process cleanup owns the final handle boundary
                pass
            publisher("worker_output_closed", ["logs", "status", "metrics"])

    thread = Thread(target=pump, daemon=True, name="llamacpp-activity-pump")
    thread.start()
    return thread
