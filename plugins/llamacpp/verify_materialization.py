"""Verify repository and installed llama.cpp plugin copies are synchronized."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

REPO_PLUGIN = Path(__file__).resolve().parent
HERMES_ROOT = Path.home() / "AppData" / "Local" / "hermes"
REQUIRED = (
    "plugin.yaml",
    "pack.yml",
    "dashboard/plugin_api.py",
    "dashboard/coordinator_server.py",
    "dashboard/inference_proxy.py",
    "dashboard/backend_impl.py",
    "dashboard/application/coordinator_lock.py",
    "dashboard/application/desktop_leases.py",
    "dashboard/application/execution_profiles.py",
    "dashboard/application/profile_endpoint.py",
    "dashboard/application/server_lifecycle.py",
    "dashboard/application/server_startup.py",
    "dashboard/application/state_store.py",
    "dashboard/child_lifecycle.py",
    "dashboard/routes/execution_profile_routes.py",
    "desktop/plugin.js",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def version(path: Path) -> str:
    return next(line.split(":", 1)[1].strip() for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("version:"))


def main() -> None:
    expected_version = version(REPO_PLUGIN / "plugin.yaml")
    if version(REPO_PLUGIN / "pack.yml") != expected_version:
        raise SystemExit("repository manifest versions differ")
    profile_plugins = sorted((HERMES_ROOT / "profiles").glob("*/plugins/llamacpp"))
    if not profile_plugins:
        raise SystemExit("no profile llama.cpp plugin copies found")
    for installed in profile_plugins:
        if version(installed / "plugin.yaml") != expected_version:
            raise SystemExit(f"version mismatch: {installed}")
        for relative in REQUIRED:
            source, target = REPO_PLUGIN / relative, installed / relative
            if not target.is_file() or digest(source) != digest(target):
                raise SystemExit(f"materialization mismatch: {target}")
    app = HERMES_ROOT / "desktop-plugins" / "llamacpp"
    if digest(REPO_PLUGIN / "desktop" / "plugin.js") != digest(app / "plugin.js"):
        raise SystemExit("app Desktop bundle does not match repository source")
    marker = json.loads((app / ".hermes-package.json").read_text(encoding="utf-8"))
    allowed_sources = {str((REPO_PLUGIN / "desktop").resolve())}
    allowed_sources.update(str((installed / "desktop").resolve()) for installed in profile_plugins)
    if marker.get("package") != "llamacpp" or str(marker.get("source")) not in allowed_sources:
        raise SystemExit("app Desktop package marker does not name a synchronized llama.cpp source")
    print(f"llamacpp materialization verified: version={expected_version}, profiles={len(profile_plugins)}")


if __name__ == "__main__":
    main()
