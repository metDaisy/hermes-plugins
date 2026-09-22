"""Adapter for the official llama.cpp runtime."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import build_server_command, resolve_server_executable


class OfficialLlamaCppBackend:
    key = "official"
    label = "llama.cpp official"
    description = "Official ggml-org llama.cpp runtime"

    def resolve_executable(self, raw_path: Path | str) -> Path:
        path = Path(str(raw_path)).expanduser()
        candidates = [path / "llama-server.exe", path / "llama-server",
                      path / "bin" / "llama-server.exe", path / "bin" / "llama-server"]
        return resolve_server_executable(path, candidates)

    def build_command(self, executable: Path, port: int, model_id: str, entry: dict[str, Any],
                      options: dict[str, str], model_path: Path | None = None) -> list[str]:
        return build_server_command(executable, port, entry, model_path)
