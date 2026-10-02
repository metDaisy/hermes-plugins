"""FastAPI adapter for runtime selection, installation, and inventory."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, HTTPException


@dataclass(frozen=True)
class RuntimeRouteContext:
    """Outer-layer operations required by the runtime route group."""

    status: Callable[[], dict[str, Any]]
    runtime_info: Callable[[], dict[str, Any]]
    save: Callable[[dict[str, Any]], dict[str, Any]]
    hardware: Callable[[], dict[str, Any]]
    catalog: Callable[[], dict[str, Any]]
    open_runtime: Callable[[dict[str, Any]], dict[str, Any]]
    install: Callable[[dict[str, Any]], dict[str, Any]]


def save_runtime(context: RuntimeRouteContext, body: dict[str, Any]) -> dict[str, Any]:
    try:
        return context.save(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=422,
            detail="Prism-ML runtime이 아직 준비되지 않았습니다. 먼저 다운로드를 완료하세요.",
        ) from exc


def open_runtime(context: RuntimeRouteContext, body: dict[str, Any]) -> dict[str, Any]:
    try:
        return context.open_runtime(body)
    except OSError as exc:
        raise HTTPException(status_code=503, detail="runtime folder could not be opened") from exc


def runtime_install(context: RuntimeRouteContext, body: dict[str, Any]) -> dict[str, Any]:
    try:
        return context.install(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"runtime target resolution failed: {exc}") from exc


def create_router(context: RuntimeRouteContext) -> APIRouter:
    """Return the unchanged runtime transport contract."""
    router = APIRouter()

    @router.get("/status")
    def status() -> dict[str, Any]:
        return context.status()

    @router.get("/runtime")
    def runtime_info() -> dict[str, Any]:
        return context.runtime_info()

    @router.put("/runtime")
    def save_runtime_route(body: dict[str, Any]) -> dict[str, Any]:
        return save_runtime(context, body)

    @router.get("/hardware")
    def hardware() -> dict[str, Any]:
        return context.hardware()

    @router.get("/catalog")
    def catalog() -> dict[str, Any]:
        return context.catalog()

    @router.post("/runtime/open")
    def open_runtime_route(body: dict[str, Any]) -> dict[str, Any]:
        return open_runtime(context, body)

    @router.post("/runtime/install")
    def runtime_install_route(body: dict[str, Any]) -> dict[str, Any]:
        return runtime_install(context, body)

    return router
