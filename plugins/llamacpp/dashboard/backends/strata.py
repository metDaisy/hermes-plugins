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

    @staticmethod
    def read_context_length(config: Path) -> int | None:
        try:
            data = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        args = data.get("args") if isinstance(data, dict) else None
        if not isinstance(args, list):
            return None
        for index, value in enumerate(args[:-1]):
            if value == "--max-context":
                try:
                    return int(args[index + 1])
                except (TypeError, ValueError):
                    return None
        return None

    @staticmethod
    def write_context_length(config: Path, context_length: int) -> None:
        if int(context_length) <= 0:
            raise ValueError("context length must be greater than zero")
        try:
            data = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Strata model config is not valid JSON") from exc
        if not isinstance(data, dict):
            raise RuntimeError("Strata model config must contain a JSON object")
        args = data.get("args")
        if not isinstance(args, list):
            raise RuntimeError("Strata model config args must be an array")
        next_args = list(args)
        try:
            index = next_args.index("--max-context")
        except ValueError:
            next_args.extend(["--max-context", str(int(context_length))])
        else:
            if index + 1 >= len(next_args):
                raise RuntimeError("Strata --max-context value is missing")
            next_args[index + 1] = str(int(context_length))
        data["args"] = next_args
        temporary = config.with_suffix(config.suffix + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(config)

    @staticmethod
    def ensure_vision_enabled(config: Path) -> None:
        try:
            data = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Strata model config is not valid JSON") from exc
        if not isinstance(data, dict):
            raise RuntimeError("Strata model config must contain a JSON object")
        args = data.get("args")
        if not isinstance(args, list):
            raise RuntimeError("Strata model config args must be an array")
        vision = data.get("vision")
        if not isinstance(vision, dict) or not all(str(vision.get(key) or "").strip() for key in ("exe", "mmproj", "model")):
            raise RuntimeError("Strata vision encoder and mmproj are not configured")
        if "--vision" in args:
            return
        data["args"] = [*args, "--vision"]
        temporary = config.with_suffix(config.suffix + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(config)

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
        config = Path(str(entry.get("config_path") or model_path or "")).expanduser().resolve()
        if "vision" in options:
            self.ensure_vision_enabled(config)
        command = self.build_command(executable, worker_port, model_id, entry, {}, model_path)
        version = str(entry.get("strata_version") or "Strata")
        environment = {key: value for key, value in os.environ.items() if key.upper() != "STRATA_API_KEY"}
        return WorkerLaunch(
            command, runtime_root, worker_port, version, startup_timeout=180,
            environment=environment,
        )