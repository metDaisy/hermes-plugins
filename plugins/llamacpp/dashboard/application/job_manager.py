"""In-memory background-job coordination for dashboard workflows."""
from __future__ import annotations

from typing import Any, Callable, Protocol


class ThreadLike(Protocol):
    def start(self) -> None: ...


class JobManager:
    """Hide job creation, terminal transitions, ordering, and thread failures."""

    def __init__(
        self,
        jobs: dict[str, dict[str, Any]],
        lock: Any,
        new_id: Callable[[], str],
        now: Callable[[], float],
        thread_factory: Callable[..., ThreadLike],
    ) -> None:
        self._jobs = jobs
        self._lock = lock
        self._new_id = new_id
        self._now = now
        self._thread_factory = thread_factory

    def create(self, kind: str, detail: str) -> dict[str, Any]:
        job = {
            "job_id": self._new_id(), "kind": kind, "detail": detail,
            "status": "running", "phase": "starting", "percent": None,
            "created_at": self._now(),
        }
        with self._lock:
            self._jobs[job["job_id"]] = job
        return job

    @staticmethod
    def finish(job: dict[str, Any], detail: str) -> None:
        job.update({"status": "done", "phase": "done", "percent": 100, "detail": detail})

    @staticmethod
    def fail(job: dict[str, Any], exc: Exception) -> None:
        job.update({"status": "error", "phase": "error", "detail": "작업 실패", "error": str(exc)})

    def launch(self, job: dict[str, Any], name: str, work: Callable[[], None]) -> None:
        def run() -> None:
            try:
                work()
            except Exception as exc:  # noqa: BLE001
                self.fail(job, exc)

        self._thread_factory(target=run, daemon=True, name=name).start()

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            values = list(self._jobs.values())
        values.sort(key=lambda item: (item.get("status") != "running", -float(item.get("created_at") or 0)))
        return values[:limit]

    def find(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._jobs.get(job_id)
