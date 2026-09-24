"""Runtime and server-status query projection for the dashboard."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


class RuntimeInspector:
    """Hide runtime metadata assembly and stale-server reconciliation."""

    def __init__(
        self,
        load_state: Callable[[], dict[str, Any]], save_state: Callable[[dict[str, Any]], None],
        runtime_kind: Callable[[dict[str, Any]], str], executable: Callable[[], Path | None],
        installed_target: Callable[[], tuple[str, str] | None], executable_in: Callable[[Path], Path | None],
        backend_detector: Callable[[], str], backend: Callable[[str], Any],
        backend_view: Callable[[str, dict[str, Any], Path], Any], machine_root: Path,
        runtime_root: Path, prism_root: Path, pid_alive: Callable[[Any], bool],
        health: Callable[[int], bool], unregister_endpoint: Callable[[], None],
        devices: Callable[[], list[dict[str, Any]]], server_rows: Callable[[], list[dict[str, Any]]],
        models_root: Path, model_presets: Callable[[], dict[str, str]],
    ) -> None:
        self._load_state, self._save_state = load_state, save_state
        self._runtime_kind, self._executable = runtime_kind, executable
        self._installed_target, self._executable_in = installed_target, executable_in
        self._backend_detector, self._backend, self._backend_view = backend_detector, backend, backend_view
        self._machine_root, self._runtime_root, self._prism_root = machine_root, runtime_root, prism_root
        self._pid_alive, self._health, self._unregister_endpoint = pid_alive, health, unregister_endpoint
        self._devices, self._server_rows, self._models_root = devices, server_rows, models_root
        self._model_presets = model_presets

    def info(self) -> dict[str, Any]:
        state = self._load_state()
        kind = self._runtime_kind(state)
        raw_path = state.get("runtime_path") or state.get("custom_runtime_path")
        path = str(Path(str(raw_path)).expanduser().resolve()) if raw_path else None
        executable = self._executable()
        adapter = self._backend(kind)
        view = self._backend_view(kind, state, self._machine_root)
        return {
            "kind": kind, "mode": "official" if kind == "official" else "custom",
            "label": view.label, "description": adapter.description,
            "repository": getattr(adapter, "repository", None), "path": path,
            "managed_root": view.managed_root, "version": view.version,
            "install_action": view.install_action, "executable": str(executable) if executable else None,
            "installed": executable is not None, "official": kind == "official",
        }


    def status(self) -> dict[str, Any]:
        state = self._load_state()
        runtime = self.info()
        target = self._installed_target() if runtime["kind"] == "official" else None
        tag, backend = target or (str(state.get("installed_tag") or ""), str(state.get("installed_backend") or ""))
        runtime_version = runtime["version"] or (tag if runtime["kind"] == "official" else None)
        process_alive = self._pid_alive(state.get("pid"))
        running = process_alive and self._health(int(state.get("port") or 18434))
        if not process_alive:
            if state.get("pid"):
                state["pid"] = None
                state["custom_endpoint"] = None
                self._save_state(state)
            self._unregister_endpoint()
        elif not running:
            state["custom_endpoint"] = None
            self._save_state(state)
            self._unregister_endpoint()
        return {
            "enabled": True, "tag": tag, "runtime_version": runtime_version,
            "configured_tag": str(state.get("tag") or "latest"), "latest_tag": None,
            "update_available": False, "runtime_installed": bool(self._executable()),
            "runtime_backend": backend or (self._backend_detector() if tag else None),
            "backend": backend or (self._backend_detector() if tag else None),
            "runtime_mode": runtime["mode"], "runtime_path": runtime["path"],
            "runtime_executable": runtime["executable"], "runtime_kind": runtime["kind"],
            "runtime_label": runtime["label"], "runtime_repository": runtime["repository"],
            "runtime_managed_root": runtime["managed_root"], "runtime_install_action": runtime["install_action"],
            "runtime_options": {
                "official": {"version": str(state.get("installed_tag") or "") or None, "installed": self._executable_in(self._runtime_root) is not None},
                "prism_ml": {"version": str(state.get("prism_release_tag") or "") or None, "installed": self._executable_in(self._prism_root) is not None},
            },
            "devices": self._devices(), "server_running": running,
            "model_presets": self._model_presets(),
            "server_base_url": f"http://127.0.0.1:{int(state.get('port') or 18434)}" if running else None,
            "custom_endpoint": state.get("custom_endpoint") if running else None,
            "active_model_id": state.get("active_model_id"), "loaded_models": {},
            "models": self._server_rows(), "models_dir": str(self._models_root), "loading": {}, "placement": {},
        }
