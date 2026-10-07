"""Persist bounded, privacy-safe llama-server resource telemetry."""
from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path
from typing import Any, Callable

_ALLOWED_FIELDS = {
    "at", "workerPid", "workerRssBytes", "workerPrivateBytes", "modelId", "activeRole",
    "active", "queued", "mainPrompt", "mainGeneration", "mainContextPercent",
}
_PEAK_FIELDS = {"workerRssBytes", "workerPrivateBytes", "active", "queued"}


class ResourceSamplerSignal:
    """Wake adaptive sampling on runtime transitions and stop it promptly."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._generation = 0
        self._stopped = False

    def wake(self) -> None:
        with self._condition:
            self._generation += 1
            self._condition.notify_all()

    def set(self) -> None:
        with self._condition:
            self._stopped = True
            self._condition.notify_all()

    def is_set(self) -> bool:
        with self._condition:
            return self._stopped

    def generation(self) -> int:
        with self._condition:
            return self._generation

    def wait(self, timeout: float, *, after_generation: int | None = None) -> bool:
        with self._condition:
            generation = self._generation if after_generation is None else int(after_generation)
            self._condition.wait_for(
                lambda: self._stopped or self._generation != generation,
                timeout=max(0.0, float(timeout)),
            )
            return self._stopped


RESOURCE_SAMPLER_SIGNAL = ResourceSamplerSignal()


def worker_memory_sample(pid: int | None, process_factory: Callable[[int], Any] | None = None) -> dict[str, Any]:
    """Read OS-reported working set and private bytes for the owned worker."""
    if not pid:
        return {}
    if process_factory is None:
        import psutil
        process_factory = psutil.Process
    try:
        memory = process_factory(int(pid)).memory_info()
    except Exception:  # noqa: BLE001 - stale process state is represented as unavailable telemetry
        return {}
    result: dict[str, Any] = {
        "workerPid": int(pid),
        "workerRssBytes": max(0, int(getattr(memory, "rss", 0) or 0)),
    }
    private = getattr(memory, "private", None)
    if private is not None:
        result["workerPrivateBytes"] = max(0, int(private or 0))
    return result


class ResourceHistoryStore:
    """Append JSONL samples and return downsampled rolling windows."""

    def __init__(self, path: Path, retention_seconds: int = 14 * 24 * 3600) -> None:
        self._path = path
        self._retention_seconds = max(3600, int(retention_seconds))
        self._lock = threading.RLock()
        self._last_compaction = 0.0
        self._cached_rows: list[dict[str, Any]] | None = None

    def append(self, sample: dict[str, Any]) -> None:
        row = {key: value for key, value in sample.items() if key in _ALLOWED_FIELDS and value is not None}
        row["at"] = float(row.get("at") or time.time())
        encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(encoded + "\n")
            if self._cached_rows is not None:
                self._cached_rows.append(dict(row))
            now = time.time()
            if now - self._last_compaction >= 3600 and self._path.stat().st_size >= 8 * 1024 * 1024:
                self._compact(now)

    def read(self, window_seconds: int, *, now: float | None = None, max_points: int = 360) -> list[dict[str, Any]]:
        current = float(now if now is not None else time.time())
        cutoff = current - max(5, min(int(window_seconds), 14 * 24 * 3600))
        with self._lock:
            rows = [row for row in self._rows() if float(row.get("at") or 0) >= cutoff]
        rows.sort(key=lambda row: float(row.get("at") or 0))
        return self._downsample(rows, max(2, int(max_points)))

    def _rows(self) -> list[dict[str, Any]]:
        # The singleton coordinator is the sole writer. Parse existing JSONL once,
        # then keep appends in memory so range switches do not re-read the file.
        if self._cached_rows is None:
            self._cached_rows = self._read_rows()
        return self._cached_rows

    def _read_rows(self) -> list[dict[str, Any]]:
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows: list[dict[str, Any]] = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("at") is not None:
                rows.append(value)
        return rows

    def _compact(self, now: float) -> None:
        cutoff = now - self._retention_seconds
        rows = [row for row in self._rows() if float(row.get("at") or 0) >= cutoff]
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(
            "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
            encoding="utf-8",
        )
        temporary.replace(self._path)
        self._cached_rows = rows
        self._last_compaction = now

    @staticmethod
    def _downsample(rows: list[dict[str, Any]], max_points: int) -> list[dict[str, Any]]:
        if len(rows) <= max_points:
            return rows
        bucket_size = math.ceil(len(rows) / max_points)
        result: list[dict[str, Any]] = []
        for start in range(0, len(rows), bucket_size):
            bucket = rows[start:start + bucket_size]
            merged: dict[str, Any] = {"at": max(float(row.get("at") or 0) for row in bucket)}
            keys = set().union(*(row.keys() for row in bucket)) - {"at"}
            for key in keys:
                values = [row[key] for row in bucket if row.get(key) is not None]
                numeric = [float(value) for value in values if isinstance(value, (int, float))]
                if numeric:
                    merged[key] = max(numeric) if key in _PEAK_FIELDS else sum(numeric) / len(numeric)
                elif values:
                    merged[key] = values[-1]
            result.append(merged)
        return result


def start_resource_sampler(
    capture: Callable[[], Any],
    *,
    interval_seconds: float | Callable[[Any], float] = 10.0,
    signal: ResourceSamplerSignal | None = None,
) -> tuple[ResourceSamplerSignal, threading.Thread]:
    """Run adaptive telemetry capture inside the coordinator process."""
    stopped = signal or ResourceSamplerSignal()

    def sample_loop() -> None:
        while not stopped.is_set():
            generation = stopped.generation()
            sample = None
            try:
                sample = capture()
            except Exception:  # noqa: BLE001 - telemetry must never stop model serving
                pass
            delay = interval_seconds(sample) if callable(interval_seconds) else interval_seconds
            stopped.wait(max(1.0, float(delay)), after_generation=generation)

    thread = threading.Thread(target=sample_loop, daemon=True, name="llamacpp-resource-sampler")
    thread.start()
    return stopped, thread


def resource_sample_interval(sample: Any) -> float:
    """Sample quickly during inference and back off while idle or stopped."""
    if not isinstance(sample, dict) or not sample.get("workerPid"):
        return 30.0
    if int(sample.get("active") or 0) > 0 or int(sample.get("queued") or 0) > 0:
        return 2.0
    return 10.0
