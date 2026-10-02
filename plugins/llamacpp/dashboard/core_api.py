"""Thin, public-HTTP adapter over Hermes Core Local Models.

The plugin does not own a llama-server, model registry, or runtime lifecycle in
this mode.  It projects Core state and delegates every mutation to Core routes.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


RequestJson = Callable[[str, str, Any], Awaitable[dict[str, Any]]]


class CoreApiAdapter:
    def __init__(self, request_json: RequestJson) -> None:
        self._request_json = request_json

    async def _request(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
        return await self._request_json(method, path, body)

    async def snapshot(self) -> dict[str, Any]:
        status = await self._request("GET", "/api/local-models/status")
        hardware = await self._request("GET", "/api/local-models/hardware")
        assignments = await self._request("GET", "/api/model/auxiliary")
        config = await self._request("GET", "/api/config")
        tasks = {
            str(row.get("task") or ""): row
            for row in assignments.get("tasks", [])
            if isinstance(row, dict)
        }
        main = assignments.get("main") if isinstance(assignments.get("main"), dict) else {}
        compression = tasks.get("compression", {})
        models = status.get("models") if isinstance(status.get("models"), list) else []
        local_runtime = config.get("local_runtime") if isinstance(config.get("local_runtime"), dict) else {}
        models_max = int(local_runtime.get("models_max") or 0)
        return {
            **status,
            "mode": "core_native",
            "policy": "models_max_1_autoload" if models_max == 1 else "core_autoload_nonexclusive",
            "models_max": models_max,
            "exclusive_residency": models_max == 1,
            "hardware": hardware,
            "profiles": {
                "main": {
                    "provider": str(main.get("provider") or ""),
                    "model_id": str(main.get("model") or ""),
                },
                "compression": {
                    "provider": str(compression.get("provider") or ""),
                    "model_id": str(compression.get("model") or ""),
                },
            },
            "model_options": [
                {"id": str(row.get("id") or ""), "label": str(row.get("id") or "")}
                for row in models
                if isinstance(row, dict) and row.get("id")
            ],
        }

    async def assign(self, role: str, model_id: str) -> dict[str, Any]:
        normalized_role = role.strip().lower()
        if normalized_role not in {"main", "compression"}:
            raise ValueError("role must be 'main' or 'compression'")
        normalized_model = model_id.strip()
        if not normalized_model:
            raise ValueError("model_id is required")
        return await self._request("POST", "/api/model/set", {
            "scope": "main" if normalized_role == "main" else "auxiliary",
            "task": "" if normalized_role == "main" else "compression",
            "provider": "llamacpp",
            "model": normalized_model,
            "base_url": "",
            "api_key": "",
        })

    async def server(self, action: str) -> dict[str, Any]:
        normalized = action.strip().lower()
        if normalized not in {"start", "stop"}:
            raise ValueError("action must be 'start' or 'stop'")
        return await self._request("POST", "/api/local-models/server", {"action": normalized})

    async def eject(self, model_id: str) -> dict[str, Any]:
        return await self._request("POST", "/api/local-models/eject", {"model_id": model_id.strip()})

    async def activate(self, model_id: str) -> dict[str, Any]:
        return await self._request("POST", "/api/local-models/activate", {"model_id": model_id.strip()})

    async def jobs(self) -> dict[str, Any]:
        return await self._request("GET", "/api/local-models/jobs")

    async def job(self, job_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/local-models/jobs/{job_id}")
