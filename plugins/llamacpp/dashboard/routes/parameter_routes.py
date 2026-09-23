"""FastAPI adapter for llama-server settings and shared presets."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter


@dataclass(frozen=True)
class ParameterRouteContext:
    """Outer-layer inputs needed by parameter and preset routes."""

    options: Callable[[], list[dict[str, Any]]]
    executable: Callable[[], Any]
    parameters: Callable[[], Any]
    server_requires_restart: Callable[[], bool]
    runtime_kind: Callable[[dict[str, Any]], str]
    state: Callable[[], dict[str, Any]]


def create_router(context: ParameterRouteContext) -> APIRouter:
    """Return the stable settings/preset transport contract."""
    router = APIRouter()

    @router.get("/settings")
    def settings(q: str = "", limit: int = 50) -> dict[str, Any]:
        query = q.strip().lower()
        options = [
            option for option in context.options()
            if query and (query in option["key"].lower() or query in option["name"].lower()
                          or query in option["description"].lower())
        ]
        return {
            "editable": True, "query": q, "options": options[:max(1, min(limit, 100))],
            "runtime": str(context.executable() or ""),
            "message": "plugin이 관리하는 llama-server --help에서 검색합니다.",
        }

    @router.get("/settings/{model_id}")
    def model_settings(model_id: str) -> dict[str, Any]:
        return context.parameters().model_settings(model_id)

    @router.put("/settings/{model_id}")
    def save_model_settings(model_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return context.parameters().save_model(model_id, body.get("options") or {}, context.server_requires_restart())

    @router.get("/presets")
    def presets(model_id: str = "") -> dict[str, Any]:
        return {"presets": context.parameters().presets()}

    @router.post("/presets")
    def create_preset(body: dict[str, Any]) -> dict[str, Any]:
        return {"preset": context.parameters().create_preset(
            body.get("name"), body.get("options"), str(body.get("model_id") or "").strip(),
        )}

    @router.post("/presets/{preset_id}/apply")
    def apply_preset(preset_id: str, body: dict[str, Any]) -> dict[str, Any]:
        stored = next((row for row in context.parameters().presets() if row["id"] == preset_id), None)
        model_id = str(body.get("model_id") or (stored or {}).get("model_id") or "").strip()
        return context.parameters().apply_preset(
            preset_id, model_id, context.runtime_kind(context.state()), context.server_requires_restart(),
        )

    @router.patch("/presets/{preset_id}")
    def rename_preset(preset_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return {"preset": context.parameters().rename_preset(preset_id, body.get("name"))}

    @router.delete("/presets/{preset_id}")
    def delete_preset(preset_id: str) -> dict[str, Any]:
        context.parameters().delete_preset(preset_id)
        return {"ok": True, "preset_id": preset_id}

    return router
