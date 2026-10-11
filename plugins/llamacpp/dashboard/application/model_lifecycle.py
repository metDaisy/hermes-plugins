"""Registered-model lifecycle use case.

The module owns selection, ejection, removal, direct-path registration, and the
server-visible model projection. FastAPI routes retain only HTTP translation.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Protocol


class ModelRegistry(Protocol):
    def rows(self) -> list[dict[str, Any]]: ...
    def active_path(self, model_id: str) -> Path: ...
    def register(self, model_id: str, paths: list[Path], owned: bool, **kwargs: Any) -> None: ...


class ModelLifecycleService:
    """Keep registered-model lifecycle invariants behind one seam."""

    def __init__(
        self,
        registry: ModelRegistry,
        load_state: Callable[[], dict[str, Any]],
        save_state: Callable[[dict[str, Any]], None],
        runtime_kind: Callable[[dict[str, Any]], str],
        accepts: Callable[[str, str, list[str]], bool],
        server_running: Callable[[], bool],
        stop_server: Callable[[], None],
        models_root: Path,
        model_id: Callable[[Path], str],
        model_rows: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> None:
        self._registry = registry
        self._load_state = load_state
        self._save_state = save_state
        self._runtime_kind = runtime_kind
        self._accepts = accepts
        self._server_running = server_running
        self._stop_server = stop_server
        self._models_root = models_root
        self._model_id = model_id
        self._model_rows = model_rows or registry.rows

    def server_rows(self) -> list[dict[str, Any]]:
        kind = self._runtime_kind(self._load_state())
        visible = [
            row for row in self._model_rows()
            if self._accepts(
                kind,
                str(row.get("hf_repo") or ""),
                [str(row.get("hf_file") or path) for path in row.get("paths", [])]
                or [str(row.get("hf_file") or "")],
            )
        ]
        return [
            {key: row[key] for key in ("id", "size_bytes", "size_label", "hf_repo", "hf_file", "paths") if key in row}
            for row in visible
        ]

    def activate(self, model_id: str) -> dict[str, Any]:
        state = self._load_state()
        models = state.get("models")
        entry = models.get(model_id) if isinstance(models, dict) else None
        if not isinstance(entry, dict):
            raise RuntimeError("model is not registered")
        self._registry.active_path(model_id)
        running = self._server_running()
        if running and state.get("active_model_id") != model_id:
            raise RuntimeError("stop llama-server before selecting another model")
        state["active_model_id"] = model_id
        self._save_state(state)
        return {"ok": True, "model_id": model_id, "server_running": running}

    def eject(self, model_id: str) -> dict[str, Any]:
        state = self._load_state()
        if state.get("active_model_id") == model_id:
            self._stop_server()
            state["active_model_id"] = None
            self._save_state(state)
        return {"ok": True, "model_id": model_id}

    def unregister(self, model_id: str) -> dict[str, Any]:
        state = self._load_state()
        models = state.get("models")
        entry = models.get(model_id) if isinstance(models, dict) else None
        if not isinstance(entry, dict):
            raise RuntimeError("model not found")
        if self._server_running() and state.get("active_model_id") == model_id:
            raise RuntimeError("stop llama-server before removing model registration")
        if state.get("active_model_id") == model_id:
            state["active_model_id"] = None
        models.pop(model_id, None)
        profiles = state.get("execution_profiles")
        if isinstance(profiles, dict):
            for profile in profiles.values():
                if isinstance(profile, dict) and profile.get("model_id") == model_id:
                    profile["model_id"] = ""
                    profile["runtime_kind"] = "official"
        self._save_state(state)
        return {"ok": True, "model_id": model_id, "unregistered": True}

    def delete(self, model_id: str) -> dict[str, Any]:
        state = self._load_state()
        if state.get("active_model_id") == model_id:
            self._stop_server()
            state = self._load_state()
            state["active_model_id"] = None
            self._save_state(state)
        models = state.get("models")
        entry = models.get(model_id) if isinstance(models, dict) else None
        rows = [row for row in self._model_rows() if row["id"] == model_id]
        if not entry and not rows:
            raise RuntimeError("model not found")
        if isinstance(entry, dict) and entry.get("owned"):
            for raw_path in entry.get("paths", []):
                Path(str(raw_path)).unlink(missing_ok=True)
        elif not entry:
            for raw_path in rows[0]["paths"] if rows else []:
                path = Path(raw_path)
                if self._models_root in path.parents:
                    path.unlink(missing_ok=True)
        if isinstance(models, dict):
            models.pop(model_id, None)
        self._save_state(state)
        return {"ok": True, "model_id": model_id}

    def sideload(self, source: Path) -> dict[str, Any]:
        if not source.is_file() or source.suffix.lower() != ".gguf":
            raise RuntimeError("Pick a .gguf model file")
        model_id = self._model_id(source)
        self._registry.register(model_id, [source], owned=False)
        return {"ok": True, "model_id": model_id, "link_mode": "direct-path", "path": str(source.resolve())}

    def register_strata(self, model_id: str, runtime_root: Path, config_path: Path,
                        worker_port: int = 18080, serving_model: str = "strata") -> dict[str, Any]:
        if self._server_running():
            raise RuntimeError("stop the managed worker before registering Strata")
        alias = str(model_id or "").strip()
        if not alias:
            raise RuntimeError("model_id is required")
        root = runtime_root.expanduser().resolve()
        config = config_path.expanduser().resolve()
        python_candidates = [root / ".venv" / "Scripts" / "python.exe", root / ".venv" / "bin" / "python"]
        python = next((candidate for candidate in python_candidates if candidate.is_file()), None)
        if not root.is_dir() or not (root / "serve" / "server.py").is_file() or python is None:
            raise RuntimeError("유효한 Strata 설치 폴더를 선택해야 합니다")
        if not config.is_file() or config.suffix.lower() != ".json":
            raise RuntimeError("유효한 Strata model config를 선택해야 합니다")
        try:
            config_data = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Strata model config가 유효한 JSON이 아닙니다") from exc
        if not isinstance(config_data, dict):
            raise RuntimeError("Strata model config는 JSON object여야 합니다")
        if config_data.get("api_key"):
            raise RuntimeError("API key를 사용하는 Strata config는 지원하지 않습니다")
        try:
            port = int(worker_port)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Strata worker port가 올바르지 않습니다") from exc
        if port != 18080:
            raise RuntimeError("Strata worker port는 18080이어야 합니다")
        self._registry.register(
            alias, [config], owned=False, runtime_kind="strata",
            metadata={
                "runtime_root": str(root), "python_path": str(python.resolve()),
                "config_path": str(config), "worker_port": port,
                "serving_model": str(serving_model or "strata").strip() or "strata",
            },
        )
        state = self._load_state()
        profiles = state.get("execution_profiles")
        if not isinstance(profiles, dict):
            profiles = {}
        binding = {"model_id": alias, "runtime_kind": "strata", "preset_id": ""}
        profiles["main"] = dict(binding)
        profiles["compression"] = dict(binding)
        state["execution_profiles"] = profiles
        self._save_state(state)
        return {
            "ok": True, "model_id": alias, "runtime_kind": "strata",
            "config_path": str(config), "worker_port": port,
            "bound_roles": ["main", "compression"],
        }
