"""Adapter for Prism-ML's llama.cpp fork and Bonsai runtime layout."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import build_server_command, resolve_server_executable


class PrismMlRuntime:
    key = "prism_ml"
    label = "Prism-ML llama.cpp"
    description = "PrismML-Eng/Bonsai-demo compatible llama-server"
    repository = "https://github.com/PrismML-Eng/Bonsai-demo"

    def resolve_executable(self, raw_path: Path | str) -> Path:
        path = Path(str(raw_path)).expanduser()
        known = [
            path / "llama-server.exe", path / "llama-server",
            path / "bin" / "llama-server.exe", path / "bin" / "llama-server",
        ]
        for backend in ("cuda", "vulkan", "cpu", "rocm", "hip"):
            known.extend([path / "bin" / backend / "llama-server.exe",
                          path / "bin" / backend / "llama-server"])
        if path.is_dir():
            known.extend(sorted(path.rglob("llama-server.exe")))
            known.extend(sorted(path.rglob("llama-server")))
        return resolve_server_executable(path, known)

    def build_command(self, executable: Path, port: int, model_id: str, entry: dict[str, Any],
                      options: dict[str, str], model_path: Path | None = None) -> list[str]:
        command = build_server_command(executable, port, entry, model_path)
        repo = str(entry.get("hf_repo") or "").lower()
        if "bonsai-2" in repo and "jinja" not in {str(key).lstrip("-") for key in options}:
            # Bonsai's chat/tool template is required for its agentic prompt format.
            command[5:5] = ["--jinja"]
        return command
