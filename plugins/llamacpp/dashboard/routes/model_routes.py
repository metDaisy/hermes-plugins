"""FastAPI adapter for model lifecycle and Hugging Face routes."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException


@dataclass(frozen=True)
class ModelRouteContext:
    """Outer-layer adapters required by the model route group."""

    lifecycle: Callable[[], Any]
    workflow: Callable[[], Any]
    create_job: Callable[[str, str], dict[str, Any]]
    launch: Callable[[dict[str, Any], Callable[[], None], str], None]
    finish: Callable[[dict[str, Any], str], None]
    begin_download: Callable[[dict[str, Any], str], None]


def create_router(context: ModelRouteContext) -> APIRouter:
    """Return the unchanged model-route contract over application workflows."""
    router = APIRouter()

    @router.post("/activate")
    def activate(body: dict[str, Any]) -> dict[str, Any]:
        try:
            return context.lifecycle().activate(str(body.get("model_id") or ""))
        except RuntimeError as exc:
            status_code = 409 if "stop llama-server" in str(exc) else 404
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    @router.post("/eject")
    def eject(body: dict[str, Any]) -> dict[str, Any]:
        return context.lifecycle().eject(str(body.get("model_id") or ""))

    @router.delete("/models/{model_id}")
    def delete(model_id: str) -> dict[str, Any]:
        try:
            return context.lifecycle().delete(model_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.delete("/models/{model_id}/registration")
    def unregister(model_id: str) -> dict[str, Any]:
        try:
            return context.lifecycle().unregister(model_id)
        except RuntimeError as exc:
            status_code = 409 if "stop llama-server" in str(exc) else 404
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    @router.get("/search")
    def search(q: str = "", limit: int = 20) -> dict[str, Any]:
        try:
            return context.workflow().search(q, limit)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Hugging Face search failed: {exc}") from exc

    @router.get("/repo")
    def repo(repo_id: str) -> dict[str, Any]:
        try:
            return context.workflow().repository(repo_id)
        except Exception as exc:  # noqa: BLE001
            if "Prism-ML backend" in str(exc):
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            raise HTTPException(status_code=502, detail=f"Could not list {repo_id}: {exc}") from exc

    @router.post("/download-browsed")
    def download_browsed(body: dict[str, Any]) -> dict[str, Any]:
        repo_id, paths = _selection(body)
        try:
            model_id = context.workflow().model_id_for(repo_id, paths)
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        job = context.create_job("model-download", model_id)
        context.begin_download(job, f"다운로드 준비: {model_id}")

        def run() -> None:
            context.workflow().download(repo_id, paths, job)
            context.finish(job, f"{model_id} downloaded; not registered")

        context.launch(job, run, "llamacpp-hf-download")
        return {"job_id": job["job_id"], "model_id": model_id}

    @router.post("/register")
    def register(body: dict[str, Any]) -> dict[str, Any]:
        repo_id, paths = _selection(body)
        try:
            return context.workflow().register(repo_id, paths)
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/hf-models")
    def hf_models() -> dict[str, Any]:
        return context.workflow().local_models()

    @router.get("/hf-models/files")
    def hf_model_files(repo_id: str) -> dict[str, Any]:
        files, warning = context.workflow().cached_files(repo_id)
        return {"repo_id": repo_id, "files": files, "warning": warning}

    @router.post("/hf-models/delete")
    def delete_hf_model(body: dict[str, Any]) -> dict[str, Any]:
        repo_id = str(body.get("repo_id") or "")
        if not repo_id or repo_id != repo_id.strip():
            raise HTTPException(status_code=422, detail="repo_id is required")
        try:
            return context.workflow().remove_cached_repository(repo_id)
        except RuntimeError as exc:
            message = str(exc)
            status_code = 409 if message.startswith("등록된 모델") else 503 if "CLI was not found" in message else 502
            raise HTTPException(status_code=status_code, detail=message) from exc

    @router.post("/sideload")
    def sideload(body: dict[str, Any]) -> dict[str, Any]:
        try:
            return context.lifecycle().sideload(Path(str(body.get("path") or "")))
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return router


def _selection(body: dict[str, Any]) -> tuple[str, list[str]]:
    repo_id = str(body.get("repo") or "")
    paths = [str(path) for path in body.get("paths") or [] if str(path).lower().endswith(".gguf")]
    if not repo_id or not paths:
        raise HTTPException(status_code=422, detail="repo and .gguf paths are required")
    return repo_id, paths
