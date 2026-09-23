"""Registered-model application service.

Hides state-shape migration and local/Hugging Face inventory reconciliation behind
one narrow interface used by routes and server lifecycle code.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


class RegisteredModelService:
    """Manages explicit model registrations; inventory is never registration."""

    def __init__(
        self,
        load_state: Callable[[], dict[str, Any]],
        save_state: Callable[[dict[str, Any]], None],
        cached_files: Callable[[str], tuple[list[dict[str, Any]], str | None]],
    ) -> None:
        self._load_state = load_state
        self._save_state = save_state
        self._cached_files = cached_files

    def rows(self) -> list[dict[str, Any]]:
        state = self._load_state()
        registered = state.get("models", {})
        if not isinstance(registered, dict):
            return []
        rows: list[dict[str, Any]] = []
        state_dirty = False
        active = state.get("active_model_id")
        for model_id, entry in registered.items():
            if not isinstance(entry, dict):
                continue
            paths, size_bytes = self._valid_paths(entry)
            if not size_bytes and entry.get("hf_repo") and entry.get("hf_file"):
                groups, _ = self._cached_files(str(entry["hf_repo"]))
                cached = next((group for group in groups if str(entry["hf_file"]) in group.get("paths", [])), None)
                if cached:
                    size_bytes = int(cached.get("total_bytes") or 0)
                    if size_bytes:
                        entry["size_bytes"] = size_bytes
                        state_dirty = True
            row: dict[str, Any] = {
                "id": str(model_id), "model_id": str(model_id), "paths": sorted(paths),
                "size_bytes": size_bytes, "owned": bool(entry.get("owned", False)),
                "active": str(model_id) == str(active), "path": paths[0] if paths else None,
                "size_label": f"{size_bytes / (1 << 30):.1f} GB" if size_bytes else "size unknown",
            }
            if entry.get("hf_repo"):
                row["hf_repo"] = str(entry["hf_repo"])
            if entry.get("hf_file"):
                row["hf_file"] = str(entry["hf_file"])
            rows.append(row)
        if state_dirty:
            self._save_state(state)
        return sorted(rows, key=lambda item: item["id"].lower())

    def register(self, model_id: str, paths: list[Path], owned: bool, hf_repo: str | None = None,
                 hf_file: str | None = None, size_bytes: int = 0) -> None:
        state = self._load_state()
        models = state.setdefault("models", {})
        entry: dict[str, Any] = {"paths": [str(path.resolve()) for path in paths], "owned": owned}
        if size_bytes > 0:
            entry["size_bytes"] = size_bytes
        if hf_repo:
            entry["hf_repo"] = hf_repo
        if hf_file:
            entry["hf_file"] = hf_file
        models[model_id] = entry
        self._save_state(state)

    def active_path(self, model_id: str) -> Path:
        for row in self.rows():
            if row["id"] == model_id:
                if row["paths"]:
                    return Path(row["paths"][0])
                break
        raise RuntimeError(f"model is not registered: {model_id}")

    @staticmethod
    def _valid_paths(entry: dict[str, Any]) -> tuple[list[str], int]:
        paths: list[str] = []
        size_bytes = 0
        for raw_path in entry.get("paths", []):
            path = Path(str(raw_path))
            if path.suffix.lower() != ".gguf" or path.name.lower().startswith("mmproj"):
                continue
            resolved = str(path.resolve())
            if resolved in paths:
                continue
            paths.append(resolved)
            try:
                size_bytes += path.stat().st_size
            except OSError:
                pass
        if not size_bytes:
            try:
                size_bytes = max(0, int(entry.get("size_bytes") or 0))
            except (TypeError, ValueError):
                size_bytes = 0
        return paths, size_bytes
