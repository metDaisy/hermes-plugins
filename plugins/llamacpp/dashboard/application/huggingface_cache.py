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
    ) -> None:
        self._find_cli = find_cli
        self._run = run

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
        return models, executable, None

    def cached_files(self, repo_id: str) -> tuple[list[dict[str, Any]], str | None]:
        executable = self._find_cli()
        if not executable:
            return [], "hf CLI was not found on PATH"
        code, output = self._run([executable, "cache", "ls", "--revisions", "--format", "json"])
        if code != 0:
            return [], "HF cache revision listing failed"
        payload = self._json_list(output, "HF cache revision listing")
        if isinstance(payload, str):
            return [], payload
        revision = next((item for item in payload if isinstance(item, dict) and item.get("repo_id") == repo_id and item.get("snapshot_path")), None)
        if not revision:
            return [], "다운로드가 완료된 cache snapshot을 찾을 수 없습니다."
        snapshot = Path(str(revision["snapshot_path"]))
        if not snapshot.is_dir():
            return [], "다운로드가 완료된 cache snapshot을 찾을 수 없습니다."
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

    @staticmethod
    def _json_list(output: str, operation: str) -> list[Any] | str:
        try:
            payload = json.loads(output or "[]")
        except json.JSONDecodeError:
            return f"{operation} returned invalid JSON"
        if isinstance(payload, dict):
            payload = payload.get("items", [])
        return payload if isinstance(payload, list) else f"{operation} returned an unexpected JSON shape"
