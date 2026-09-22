"""Shared primitives for llama.cpp runtime adapters."""
from __future__ import annotations

from pathlib import Path
from typing import Any


def resolve_server_executable(raw_path: Path | str, candidates: list[Path] | None = None) -> Path:
    path = Path(str(raw_path)).expanduser()
    path = path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()
    if path.is_file():
        found = path
    elif path.is_dir():
        choices = candidates or [path / "llama-server.exe", path / "llama-server"]
        found = next((item for item in choices if item.is_file()), None)
    else:
        raise RuntimeError("runtime path for llama-server does not exist")
    if found is None or found.name.lower() not in {"llama-server", "llama-server.exe"}:
        raise RuntimeError("runtime path must point to a directory or llama-server executable")
    return found.resolve()


def build_server_command(executable: Path, port: int, entry: dict[str, Any], model_path: Path | None,
                         extra_args: list[str] | None = None) -> list[str]:
    command = [str(executable), "--host", "127.0.0.1", "--port", str(port)]
    if entry.get("hf_repo"):
        command.extend(["--hf-repo", str(entry["hf_repo"])])
        if entry.get("hf_file"):
            command.extend(["--hf-file", str(entry["hf_file"])])
    elif model_path is not None:
        command.extend(["--model", str(model_path)])
    if extra_args:
        command.extend(extra_args)
    return command
