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


# --- Framework-independent transport handlers -----------------------------
#
# Each handler owns the transport-shaped response for one HTTP operation. The
# router in ``create_router`` only wires a request path to one of these. The
# facade re-exports them (bound to its context) so the public entrypoint keeps
# its ``api.<name>(...)`` contract without duplicating any transport logic.

def model_settings(context: ParameterRouteContext, model_id: str) -> dict[str, Any]:
    return context.parameters().model_settings(model_id)


def save_model_settings(context: ParameterRouteContext, model_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return context.parameters().save_model(
        model_id, body.get("options") or {}, context.server_requires_restart(),
    )


def presets(context: ParameterRouteContext, model_id: str = "") -> dict[str, Any]:
    return {"presets": context.parameters().presets()}


def create_preset(context: ParameterRouteContext, body: dict[str, Any]) -> dict[str, Any]:
    return {"preset": context.parameters().create_preset(
        body.get("name"), body.get("options"), str(body.get("model_id") or "").strip(),
    )}


def apply_preset(context: ParameterRouteContext, preset_id: str, body: dict[str, Any]) -> dict[str, Any]:
    row = next((row for row in context.parameters().presets() if row["id"] == preset_id), None)
    model_id = str(body.get("model_id") or (row or {}).get("model_id") or "").strip()
    return context.parameters().apply_preset(
        preset_id, model_id, context.runtime_kind(context.state()),
        context.server_requires_restart(),
    )


def rename_preset(context: ParameterRouteContext, preset_id: str, body: dict[str, Any]) -> dict[str, Any]:
    options = body.get("options") if "options" in body else None
    return {"preset": context.parameters().rename_preset(preset_id, body.get("name"), options)}


def delete_preset(context: ParameterRouteContext, preset_id: str) -> dict[str, Any]:
    context.parameters().delete_preset(preset_id)
    return {"ok": True, "preset_id": preset_id}


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
    def settings_model(model_id: str) -> dict[str, Any]:
        return model_settings(context, model_id)

    @router.put("/settings/{model_id}")
    def save_settings(model_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return save_model_settings(context, model_id, body)

    @router.get("/presets")
    def presets_route(model_id: str = "") -> dict[str, Any]:
        return presets(context, model_id)

    @router.post("/presets")
    def create_preset_route(body: dict[str, Any]) -> dict[str, Any]:
        return create_preset(context, body)

    @router.post("/presets/{preset_id}/apply")
    def apply_preset_route(preset_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return apply_preset(context, preset_id, body)

    @router.patch("/presets/{preset_id}")
    def rename_preset_route(preset_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return rename_preset(context, preset_id, body)

    @router.delete("/presets/{preset_id}")
    def delete_preset_route(preset_id: str) -> dict[str, Any]:
        return delete_preset(context, preset_id)

    return router
