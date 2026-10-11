"""Adapter for an externally installed Strata runtime."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .base import WorkerLaunch


class StrataBackend:
    key = "strata"
    label = "Strata"
    description = "Niko1221/Strata managed local runtime"
    repository = "https://github.com/Niko1221/Strata"
    default_port = 18080

    def managed_root(self, machine_root: Path) -> Path:
        return machine_root / "runtimes" / "strata"

    def runtime_version(self, state: dict[str, Any]) -> str | None:
        return str(state.get("strata_version") or "") or None

    def accepts_model(self, repo_id: str, paths: list[str]) -> bool:
        return bool(paths) and all(str(path).lower().endswith(".json") for path in paths)

    def resolve_executable(self, raw_path: Path | str) -> Path:
        root = Path(str(raw_path)).expanduser().resolve()
        candidates = [
            root / ".venv" / "Scripts" / "python.exe",
            root / ".venv" / "bin" / "python",
        ]
        executable = next((candidate for candidate in candidates if candidate.is_file()), None)
        if executable is None:
            raise RuntimeError("Strata Python executable was not found")
        if not (root / "serve" / "server.py").is_file():
            raise RuntimeError("Strata serve/server.py was not found")
        return executable.resolve()

    def build_command(self, executable: Path, port: int, model_id: str, entry: dict[str, Any],
                      options: dict[str, str], model_path: Path | None = None) -> list[str]:
        runtime_root = Path(str(entry.get("runtime_root") or "")).expanduser().resolve()
        server = (runtime_root / "serve" / "server.py").resolve()
        config = Path(str(entry.get("config_path") or model_path or "")).expanduser().resolve()
        if not server.is_file():
            raise RuntimeError("Strata serve/server.py was not found")
        if not config.is_file():
            raise RuntimeError("Strata model config was not found")
        try:
            config_data = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Strata model config is not valid JSON") from exc
        if not isinstance(config_data, dict):
            raise RuntimeError("Strata model config must contain a JSON object")
        if config_data.get("api_key"):
            raise RuntimeError("Authenticated Strata configs are not supported by the loopback worker proxy")
        if int(port) != self.default_port:
            raise RuntimeError(f"Strata worker port must be {self.default_port}")
        return [
            str(executable.resolve()), "-m", "serve.server", "--engine", "strata",
            "--config", str(config), "--host", "127.0.0.1", "--port", str(port),
        ]

    def build_launch(self, executable: Path, port: int, model_id: str, entry: dict[str, Any],
                     options: dict[str, str], serving_model: str, option_args: list[str],
                     model_path: Path | None = None) -> WorkerLaunch:
        runtime_root = Path(str(entry.get("runtime_root") or "")).expanduser().resolve()
        worker_port = int(entry.get("worker_port") or self.default_port)
        command = self.build_command(executable, worker_port, model_id, entry, {}, model_path)
        version = str(entry.get("strata_version") or "Strata")
        environment = {key: value for key, value in os.environ.items() if key.upper() != "STRATA_API_KEY"}
        return WorkerLaunch(
            command, runtime_root, worker_port, version, startup_timeout=180,
            environment=environment,
        )