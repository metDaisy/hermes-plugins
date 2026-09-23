"""FastAPI adapter for managed server control, jobs, and logs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, HTTPException


@dataclass(frozen=True)
class ServerRouteContext:
    """Outer-layer operations required by the server route group."""

    state: Callable[[], dict[str, Any]]
    stop: Callable[[], None]
    start: Callable[[], None]
    create_job: Callable[[str, str], dict[str, Any]]
    launch: Callable[[dict[str, Any], Callable[[], None], str], None]
    finish: Callable[[dict[str, Any], str], None]
    recent_jobs: Callable[[], list[dict[str, Any]]]
    find_job: Callable[[str], dict[str, Any] | None]
    logs: Callable[[int], dict[str, Any]]


def create_router(context: ServerRouteContext) -> APIRouter:
    """Return the stable server, job, and log transport contract."""
    router = APIRouter()

    @router.post("/server")
    def server(body: dict[str, Any]) -> dict[str, Any]:
        action = str(body.get("action") or "")
        if action == "stop":
            context.stop()
            return {"ok": True, "server_running": False}
        if action == "start":
            if not context.state().get("active_model_id"):
                raise HTTPException(status_code=400, detail="select a model before starting llama-server")
            job = context.create_job("server-start", "llama-server 시작 중")

            def run() -> None:
                context.start()
                context.finish(job, "llama-server is healthy")

            context.launch(job, run, "llamacpp-server-start")
            return {"ok": True, "server_running": False, "job_id": job["job_id"]}
        raise HTTPException(status_code=400, detail="action must be start or stop")

    @router.get("/jobs")
    def jobs() -> dict[str, Any]:
        return {"jobs": context.recent_jobs()}

    @router.get("/jobs/{job_id}")
    def job(job_id: str) -> dict[str, Any]:
        value = context.find_job(job_id)
        if value is None:
            raise HTTPException(status_code=404, detail="job not found")
        return value

    @router.get("/logs")
    def logs(limit: int = 250) -> dict[str, Any]:
        return context.logs(limit)

    return router
