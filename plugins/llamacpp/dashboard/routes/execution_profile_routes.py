"""FastAPI adapter for Main/Compression execution profile bindings."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, HTTPException


@dataclass(frozen=True)
class ExecutionProfileRouteContext:
    snapshot: Callable[[], dict[str, Any]]
    save: Callable[[str, dict[str, Any]], dict[str, Any]]


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

    return router
