"""Shared audit database path resolution for runtime and dashboard surfaces."""
from __future__ import annotations

import os
from pathlib import Path


def _git_root(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        if (candidate / ".git").exists():
            return candidate
    return None


def storage_root(module_file: str | Path) -> Path:
    """Resolve the repository/profile root used by the audit database."""
    module_path = Path(module_file).resolve()
    module_root = _git_root(list(module_path.parents))
    if module_root is not None:
        return module_root

    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        return Path(hermes_home).resolve().parent

    cwd_root = _git_root([Path.cwd().resolve(), *Path.cwd().resolve().parents])
    if cwd_root is not None:
        return cwd_root

    return module_path.parents[3]


def database_path(module_file: str | Path) -> Path:
    """Return the shared privacy-safe SQLite audit database path."""
    return storage_root(module_file) / ".hermes" / "audit.db"
