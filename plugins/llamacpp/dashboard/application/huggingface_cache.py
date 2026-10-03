"""Hugging Face CLI cache inventory for registered-model workflows."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable


class HuggingFaceCacheService:
    """Hide HF cache JSON shape and GGUF split-file grouping behind one seam."""

    _split_suffix = re.compile(r"-\d{5}-of-\d{5}$", re.IGNORECASE)

    def __init__(
        self,
        find_cli: Callable[[], str | None],
        run: Callable[[list[str]], tuple[int, str]],
        cache_root: Callable[[], Path] | None = None,
    ) -> None:
        self._find_cli = find_cli
        self._run = run
        self._cache_root = cache_root

    def downloaded_models(self) -> tuple[list[dict[str, str]], str | None, str | None]:
        executable = self._find_cli()
        if not executable:
            return [], None, "hf CLI was not found on PATH"
        code, output = self._run([executable, "cache", "ls", "--format", "json"])
        if code != 0:
            return [], executable, "hf cache listing failed"
        payload = self._json_list(output, "hf cache listing")
        if isinstance(payload, str):
            return [], executable, payload
        models = [
            {"repo_id": repo_id, "size": size}
            for item in payload
            if isinstance(item, dict)
            and isinstance(repo_id := item.get("repo_id"), str)
            and repo_id
            and isinstance(size := item.get("size"), str)
        ]
        known = {model["repo_id"].casefold() for model in models}
        for model in self._discovered_snapshot_models():
            if model["repo_id"].casefold() not in known:
                models.append(model)
                known.add(model["repo_id"].casefold())
        return models, executable, None

    def cached_files(self, repo_id: str) -> tuple[list[dict[str, Any]], str | None]:
        snapshot, warning = self._snapshot(repo_id)
        if snapshot is None:
            return [], warning
        grouped: dict[str, dict[str, Any]] = {}
        for path in snapshot.rglob("*.gguf"):
            if not path.is_file() or "mmproj" in path.name.lower() or "draft" in path.name.lower():
                continue
            relative = path.relative_to(snapshot).as_posix()
            label = self._split_suffix.sub("", path.stem)
            group = grouped.setdefault(label, {"label": label, "paths": [], "total_bytes": 0, "fit": "downloaded"})
            group["paths"].append(relative)
            group["total_bytes"] += path.stat().st_size
        for group in grouped.values():
            group["paths"].sort()
        return sorted(grouped.values(), key=lambda item: item["total_bytes"], reverse=True), None

    def cached_paths(self, repo_id: str, selected_paths: list[str]) -> tuple[list[Path], str | None]:
        """Resolve a selected cache group to existing local GGUF files only."""
        snapshot, warning = self._snapshot(repo_id)
        if snapshot is None:
            return [], warning
        paths: list[Path] = []
        for raw_path in selected_paths:
            relative = Path(raw_path)
            if relative.is_absolute():
                return [], "선택한 GGUF 경로가 올바르지 않습니다."
            path = (snapshot / relative).resolve()
            if snapshot not in path.parents or not path.is_file() or path.suffix.lower() != ".gguf":
                return [], "선택한 GGUF가 다운로드 완료 cache에 없습니다."
            paths.append(path)
        return paths, None

    def _snapshot(self, repo_id: str) -> tuple[Path | None, str | None]:
        executable = self._find_cli()
        if not executable:
            return None, "hf CLI was not found on PATH"
        code, output = self._run([executable, "cache", "ls", "--revisions", "--format", "json"])
        if code != 0:
            return None, "HF cache revision listing failed"
        payload = self._json_list(output, "HF cache revision listing")
        if isinstance(payload, str):
            return None, payload
        revision = next((item for item in payload if isinstance(item, dict) and item.get("repo_id") == repo_id and item.get("snapshot_path")), None)
        if not revision:
            snapshot = self._snapshot_from_cache(repo_id)
            if snapshot is not None:
                return snapshot, None
            return None, "다운로드가 완료된 cache snapshot을 찾을 수 없습니다."
        snapshot = Path(str(revision["snapshot_path"])).resolve()
        if not snapshot.is_dir():
            return None, "다운로드가 완료된 cache snapshot을 찾을 수 없습니다."
        return snapshot, None

    def _discovered_snapshot_models(self) -> list[dict[str, str]]:
        root = self._resolved_cache_root()
        if root is None:
            return []
        models: list[dict[str, str]] = []
        for repository in sorted(root.glob("models--*"), key=lambda path: path.name.casefold()):
            repo_id = self._repo_id(repository.name)
            snapshot = self._repository_snapshot(repository)
            if not repo_id or snapshot is None:
                continue
            paths = list(snapshot.rglob("*.gguf"))
            if not any(
                path.is_file()
                and "mmproj" not in path.name.casefold()
                and "draft" not in path.name.casefold()
                for path in paths
            ):
                continue
            unique: dict[str, Path] = {}
            for path in paths:
                if path.is_file():
                    unique[str(path.resolve()).casefold()] = path
            size_bytes = sum(path.stat().st_size for path in unique.values())
            models.append({"repo_id": repo_id, "size": self._format_size(size_bytes)})
        return models

    def _snapshot_from_cache(self, repo_id: str) -> Path | None:
        root = self._resolved_cache_root()
        parts = repo_id.split("/")
        if root is None or len(parts) != 2 or any(
            not part or part in {".", ".."} or "\\" in part for part in parts
        ):
            return None
        repository = (root / ("models--" + "--".join(parts))).resolve()
        if root not in repository.parents:
            return None
        return self._repository_snapshot(repository)

    def _resolved_cache_root(self) -> Path | None:
        if self._cache_root is None:
            return None
        root = self._cache_root().resolve()
        return root if root.is_dir() else None

    @staticmethod
    def _repository_snapshot(repository: Path) -> Path | None:
        snapshots = repository / "snapshots"
        if not snapshots.is_dir():
            return None
        ref = repository / "refs" / "main"
        if ref.is_file():
            candidate = snapshots / ref.read_text(encoding="utf-8", errors="replace").strip()
            if candidate.is_dir() and HuggingFaceCacheService._has_model_gguf(candidate):
                return candidate.resolve()
        candidates = [
            path for path in snapshots.iterdir()
            if path.is_dir() and HuggingFaceCacheService._has_model_gguf(path)
        ]
        return max(candidates, key=lambda path: path.stat().st_mtime).resolve() if candidates else None

    @staticmethod
    def _has_model_gguf(snapshot: Path) -> bool:
        return any(
            path.is_file()
            and "mmproj" not in path.name.casefold()
            and "draft" not in path.name.casefold()
            for path in snapshot.rglob("*.gguf")
        )

    @staticmethod
    def _repo_id(directory_name: str) -> str | None:
        parts = directory_name.removeprefix("models--").split("--", 1)
        if not directory_name.startswith("models--") or len(parts) != 2 or not all(parts):
            return None
        return "/".join(parts)

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        value = float(size_bytes)
        for unit in ("B", "K", "M", "G", "T"):
            if value < 1024 or unit == "T":
                return f"{int(value)}{unit}" if unit == "B" else f"{value:.1f}{unit}"
            value /= 1024
        return f"{size_bytes}B"

    @staticmethod
    def _json_list(output: str, operation: str) -> list[Any] | str:
        try:
            payload = json.loads(output or "[]")
        except json.JSONDecodeError:
            return f"{operation} returned invalid JSON"
        if isinstance(payload, dict):
            payload = payload.get("items", [])
        return payload if isinstance(payload, list) else f"{operation} returned an unexpected JSON shape"
