"""Runtime selection, opening, and installation workflow."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


class RuntimeManagementWorkflow:
    """Hide runtime-control state and background-job orchestration."""

    def __init__(
        self,
        load_state: Callable[[], dict[str, Any]],
        save_state: Callable[[dict[str, Any]], None],
        get_backend: Callable[[str], Any],
        machine_root: Path,
        runtime_kind: Callable[[dict[str, Any]], str],
        runtime_info: Callable[[], dict[str, Any]],
        pid_alive: Callable[[Any], bool],
        open_directory: Callable[[Path], None],
        install_prism: Callable[[], dict[str, Any]],
        runtime_target: Callable[[bool, Any], tuple[str, str]],
        create_job: Callable[[str, str], dict[str, Any]],
        launch: Callable[[dict[str, Any], Callable[[], None], str], None],
        finish: Callable[[dict[str, Any], str], None],
        install_official: Callable[[str, str, dict[str, Any], Callable[..., None]], None],
        download_archive: Callable[..., None],
    ) -> None:
        self._load_state = load_state
        self._save_state = save_state
        self._get_backend = get_backend
        self._machine_root = machine_root
        self._runtime_kind = runtime_kind
        self._runtime_info = runtime_info
        self._pid_alive = pid_alive
        self._open_directory = open_directory
        self._install_prism = install_prism
        self._runtime_target = runtime_target
        self._create_job = create_job
        self._launch = launch
        self._finish = finish
        self._install_official = install_official
        self._download_archive = download_archive

    def select(self, body: dict[str, Any]) -> dict[str, Any]:
        requested = body.get("kind") or body.get("mode") or "llamacpp"
        kind = self._get_backend(str(requested)).key
        mode, custom_path = "official", None
        if kind != "official":
            adapter = self._get_backend(kind)
            raw_path = str(body.get("path") or adapter.managed_root(self._machine_root)).strip()
            adapter.resolve_executable(raw_path)
            mode, custom_path = "custom", str(Path(raw_path).expanduser().resolve())
        state = self._load_state()
        state.update({"runtime_kind": kind, "runtime_path": custom_path, "runtime_mode": mode,
                      "custom_runtime_path": custom_path})
        self._save_state(state)
        selected = self._runtime_info()
        selected["installed"] = bool(selected["executable"])
        selected["requires_restart"] = bool(self._pid_alive(state.get("pid")))
        return selected

    def open(self, body: dict[str, Any]) -> dict[str, Any]:
        kind = self._get_backend(str(body.get("kind") or self._runtime_kind(self._load_state()))).key
        target = self._get_backend(kind).managed_root(self._machine_root)
        target.mkdir(parents=True, exist_ok=True)
        self._open_directory(target)
        return {"ok": True, "kind": kind}

    def install(self, body: dict[str, Any]) -> dict[str, Any]:
        kind = self._get_backend(str(body.get("kind") or "official")).key
        if kind == "prism_ml":
            return self._install_prism()
        tag, backend = self._runtime_target(True, body.get("backend"))
        job = self._create_job("runtime-install", f"llama.cpp {tag} ({backend})")

        def run() -> None:
            self._install_official(tag, backend, job, self._download_archive)
            self._finish(job, f"llama.cpp {tag} ready")

        self._launch(job, run, "llamacpp-runtime-install")
        return {"job_id": job["job_id"], "tag": tag, "backend": backend}
