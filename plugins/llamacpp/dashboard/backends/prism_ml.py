"""Adapter for Prism-ML's llama.cpp fork and Bonsai runtime layout."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import build_server_command, resolve_server_executable
try:
    from dashboard.domain.prism_catalog import PrismCatalog
except ImportError:
    from domain.prism_catalog import PrismCatalog


class PrismMlBackend:
    key = "prism_ml"
    label = "Prism-ML llama.cpp"
    description = "PrismML-Eng/Bonsai-demo compatible llama-server"
    repository = "https://github.com/PrismML-Eng/Bonsai-demo"

    def managed_root(self, machine_root: Path) -> Path:
        return machine_root / "runtimes" / "prism-ml"

    def runtime_version(self, state: dict[str, Any]) -> str | None:
        return str(state.get("prism_release_tag") or "") or None

    def __init__(self, catalog: PrismCatalog | None = None) -> None:
        self._catalog = catalog or PrismCatalog()

    def accepts_model(self, repo_id: str, paths: list[str]) -> bool:
        return self._catalog.accepts(repo_id, paths)

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
        model_name = str(model_id or "").lower()
        is_bonsai_2 = "bonsai-2" in repo or "bonsai-2-27b" in model_name
        if is_bonsai_2 and "jinja" not in {str(key).lstrip("-") for key in options}:
            # Bonsai 2's native OpenAI tool-call format requires --jinja.
            command[5:5] = ["--jinja"]
        return command
