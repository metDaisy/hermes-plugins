"""Hugging Face model-acquisition use case.

Composes remote Hub discovery, cache inventory, download, explicit registration,
and cache removal verification behind one model-acquisition seam. Transport
adapters and FastAPI exception translation remain outside this module.
"""
from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any, Callable


class HuggingFaceModelWorkflow:
    """Own GGUF acquisition rules without knowing HTTP or subprocess details."""

    def __init__(
        self,
        load_state: Callable[[], dict[str, Any]],
        runtime_kind: Callable[[dict[str, Any]], str],
        accepts: Callable[[str, str, list[str], str | None], bool],
        cache_models: Callable[[], tuple[list[dict[str, str]], str | None, str | None]],
        cached_files: Callable[[str], tuple[list[dict[str, Any]], str | None]],
        cached_paths: Callable[[str, list[str]], tuple[list[Path], str | None]],
        http_json: Callable[[str], Any],
        download: Callable[..., Path | None],
        register: Callable[..., None],
        remove_cache: Callable[[str], bool],
        model_id: Callable[[Path], str],
        visible_repositories: Callable[[str, list[str]], list[str]] | None = None,
    ) -> None:
        self._load_state = load_state
        self._runtime_kind = runtime_kind
        self._accepts = accepts
        self._cache_models = cache_models
        self._cached_files = cached_files
        self._cached_paths = cached_paths
        self._http_json = http_json
        self._download = download
        self._register = register
        self._remove_cache = remove_cache
        self._model_id = model_id
        self._visible_repositories = visible_repositories or (lambda _kind, repositories: repositories)

    def local_models(self) -> dict[str, Any]:
        models, _executable, warning = self._cache_models()
        kind = self._runtime_kind(self._load_state())
        allowed = set(self._visible_repositories(kind, [str(model.get("repo_id") or "") for model in models]))
        return {
            "models": [model for model in models if str(model.get("repo_id") or "") in allowed],
            "warning": warning,
            "runtime_kind": kind,
        }

    def cached_files(self, repo_id: str) -> tuple[list[dict[str, Any]], str | None]:
        return self._cached_files(repo_id)

    def remove_cached_repository(self, repo_id: str) -> dict[str, Any]:
        state = self._load_state()
        registered = [
            model_id for model_id, entry in (state.get("models") or {}).items()
            if isinstance(entry, dict) and entry.get("hf_repo") == repo_id
        ]
        if registered:
            raise RuntimeError("등록된 모델을 먼저 삭제해야 cache를 삭제할 수 있습니다: " + ", ".join(registered))
        if not self._remove_cache(repo_id):
            raise RuntimeError("HF cache deletion failed")
        remaining, _executable, warning = self._cache_models()
        if warning:
            raise RuntimeError("HF cache deletion completed but verification failed")
        if any(item.get("repo_id") == repo_id for item in remaining):
            raise RuntimeError("HF cache deletion could not be verified")
        return {"ok": True, "repo_id": repo_id, "deleted": True}

    def search(self, query: str, limit: int) -> dict[str, Any]:
        if not query.strip():
            return {"hits": []}
        payload = self._http_json(
            "https://huggingface.co/api/models?" + urllib.parse.urlencode({
                "search": query, "filter": "gguf", "sort": "downloads", "direction": "-1",
                "limit": max(1, min(limit, 50)),
            })
        )
        kind, version = self._runtime_context()
        hits = [
            {"repo": str(item.get("id", "")), "downloads": int(item.get("downloads") or 0)}
            for item in payload if isinstance(item, dict) and item.get("id")
        ]
        return {"hits": [hit for hit in hits if self._accepts(kind, hit["repo"], ["candidate.gguf"], version)]}

    def repository(self, repo_id: str) -> dict[str, Any]:
        url = (
            "https://huggingface.co/api/models/"
            f"{urllib.parse.quote(repo_id, safe='/')}/tree/main?recursive=true&expand=true"
        )
        payload = self._http_json(url)
        groups: dict[str, dict[str, Any]] = {}
        for item in payload if isinstance(payload, list) else []:
            path = str(item.get("path", ""))
            lowered = path.lower()
            if not lowered.endswith(".gguf") or "mmproj" in lowered or "draft" in lowered:
                continue
            label = self._model_id(Path(path))
            row = groups.setdefault(label, {"label": label, "paths": [], "total_bytes": 0, "fit": "available"})
            row["paths"].append(path)
            row["total_bytes"] += int(item.get("size") or 0)
        files = sorted(groups.values(), key=lambda item: item["total_bytes"], reverse=True)
        if files:
            self._require_compatible(repo_id, [path for group in files for path in group["paths"]])
        return {"files": files}

    def download(self, repo_id: str, paths: list[str], job: dict[str, Any] | None = None) -> str:
        model_id = self.model_id_for(repo_id, paths)
        for index, path in enumerate(paths):
            self._download(repo_id, path, job=job, part_index=index, part_count=len(paths))
        return model_id

    def model_id_for(self, repo_id: str, paths: list[str]) -> str:
        self._require_compatible(repo_id, paths)
        return self._model_id(Path(paths[0]))

    def register(self, repo_id: str, paths: list[str]) -> dict[str, Any]:
        self._require_compatible(repo_id, paths)
        model_id = self._model_id(Path(paths[0]))
        groups, warning = self._cached_files(repo_id)
        selected = next((group for group in groups if set(group["paths"]) == set(paths)), None)
        if selected is None:
            raise RuntimeError(warning or "선택한 GGUF가 HF cache에 없습니다")
        size_bytes = int(selected.get("total_bytes") or 0)
        local_paths, warning = self._cached_paths(repo_id, paths)
        if warning or len(local_paths) != len(paths):
            raise RuntimeError(warning or "선택한 GGUF의 로컬 cache 경로를 확인할 수 없습니다")
        self._register(model_id, local_paths, False, hf_repo=repo_id, hf_file=paths[0], size_bytes=size_bytes)
        return {"ok": True, "model_id": model_id, "registered": True, "downloaded": False, "size_bytes": size_bytes}

    def _require_compatible(self, repo_id: str, paths: list[str]) -> None:
        kind, version = self._runtime_context()
        if not self._accepts(kind, repo_id, paths, version):
            raise RuntimeError("Prism-ML backend에는 catalog에 등록된 Prism Bonsai/Ternary GGUF quant만 등록할 수 있습니다")

    def _runtime_context(self) -> tuple[str, str | None]:
        state = self._load_state()
        return self._runtime_kind(state), str(state.get("prism_release_tag") or "") or None
