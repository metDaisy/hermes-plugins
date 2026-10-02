"""FastAPI adapter for Main/Compression execution profile bindings."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, HTTPException


@dataclass(frozen=True)
class ExecutionProfileRouteContext:
    snapshot: Callable[[], dict[str, Any]]
    save: Callable[[str, dict[str, Any]], dict[str, Any]]
    start: Callable[[str], dict[str, Any]]
    create_job: Callable[[str, str], dict[str, Any]]
    launch: Callable[[dict[str, Any], Callable[[], None], str], None]
    finish: Callable[[dict[str, Any], str], None]


def create_router(context: ExecutionProfileRouteContext) -> APIRouter:
    router = APIRouter()

    @router.get("/profiles")
    def profiles() -> dict[str, Any]:
        return context.snapshot()

    @router.put("/profiles/{role}")
    def save_profile(role: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return context.save(role, body)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/profiles/start")
    def start_profiles(body: dict[str, Any]) -> dict[str, Any]:
        main_model_id = str(body.get("main_model_id") or "").strip()
        compression_model_id = str(body.get("compression_model_id") or "").strip()
        if not main_model_id and not compression_model_id:
            raise HTTPException(status_code=422, detail="Main 또는 Auxiliary 모델을 하나 이상 선택해야 합니다.")
        try:
            context.save("main", {"model_id": main_model_id})
            context.save("compression", {"model_id": compression_model_id})
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        role = "main" if main_model_id else "compression"
        job = context.create_job("server-start", f"{role} 모델 시작 중")

        def run() -> None:
            context.start(role)
            context.finish(job, f"{role} 모델이 준비되었습니다")

        context.launch(job, run, "llamacpp-server-start")
        return {"ok": True, "server_running": False, "job_id": job["job_id"], "startup_role": role}

    @router.post("/profiles/{role}/start")
    def start_profile(role: str, body: dict[str, Any]) -> dict[str, Any]:
        raise HTTPException(
            status_code=409,
            detail="개별 모델 시작은 지원하지 않습니다. 단일 서버 시작 동작을 사용하세요.",
        )

    return router
