"""Official llama.cpp runtime discovery and installation.

Keeps GitHub release selection and machine-scoped archive installation behind one
interface. Route code supplies transport and job orchestration, while this
module owns the verified-runtime layout and manifest invariants.
"""
from __future__ import annotations

import os
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any, Callable


DownloadArchive = Callable[[str, Path, dict[str, Any], int, int], None]


class OfficialRuntimeService:
    """Resolve, discover, and install official Windows llama.cpp builds."""

    def __init__(
        self,
        root: Path,
        load_state: Callable[[], dict[str, Any]],
        save_state: Callable[[dict[str, Any]], None],
        release_loader: Callable[[], Any],
        backend_detector: Callable[[], str],
        platform_name: Callable[[], str],
        architecture: Callable[[], str],
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._root = root
        self._load_state = load_state
        self._save_state = save_state
        self._release_loader = release_loader
        self._backend_detector = backend_detector
        self._platform_name = platform_name
        self._architecture = architecture
        self._now = now
        self._latest_cache: tuple[float, str] | None = None

    def resolve_target(self, force_latest: bool = False, requested_backend: Any = None) -> tuple[str, str]:
        state = self._load_state()
        requested = str(requested_backend or "auto").lower()
        backend = requested if requested in {"auto", "cuda", "cpu", "vulkan"} else "auto"
        if backend == "auto":
            backend = self._backend_detector()
        configured = str(state.get("tag") or "latest")
        return (self._latest_build(backend, force_latest) if configured == "latest" else configured, backend)

    def installed_target(self) -> tuple[str, str] | None:
        state = self._load_state()
        preferred = (str(state.get("installed_tag") or ""), str(state.get("installed_backend") or ""))
        candidates = [preferred] if all(preferred) else []
        if self._root.is_dir():
            discovered: list[tuple[str, str]] = []
            for tag_dir in self._root.iterdir():
                if not tag_dir.is_dir() or not self._is_release_tag(tag_dir.name):
                    continue
                for backend_dir in tag_dir.iterdir():
                    if not backend_dir.is_dir():
                        continue
                    manifest = self._read_manifest(backend_dir)
                    if manifest.get("verified_version") and self.executable_in(backend_dir):
                        discovered.append((tag_dir.name, backend_dir.name))
            candidates.extend(sorted(discovered, key=lambda item: self._release_number(item[0]), reverse=True))
        for tag, backend in candidates:
            if tag and backend and self.executable_in(self._root / tag / backend):
                return tag, backend
        return None

    def latest_target(self, requested_backend: Any = None) -> tuple[str, str]:
        requested = str(requested_backend or "auto").lower()
        backend = requested if requested in {"auto", "cuda", "cpu", "vulkan"} else "auto"
        if backend == "auto":
            backend = self._backend_detector()
        return self._latest_build(backend, False), backend

    def executable_in(self, root: Path) -> Path | None:
        if not root.is_dir():
            return None
        candidates = [root / "llama-server.exe", root / "llama-server", *root.rglob("llama-server.exe")]
        return next((candidate for candidate in candidates if candidate.is_file()), None)

    def install(self, tag: str, backend: str, job: dict[str, Any], download: DownloadArchive) -> None:
        assets = self._asset_names(tag, backend)
        download_root = self._root / "downloads"
        download_root.mkdir(parents=True, exist_ok=True)
        extract_root = self._root / f".{tag}-{backend}-{job.get('job_id', 'runtime')}"
        install_root = self._root / tag / backend
        extract_root.mkdir(parents=True, exist_ok=True)
        try:
            for index, asset in enumerate(assets):
                archive = download_root / asset
                job.update({"phase": "downloading", "detail": f"Downloading {asset}"})
                download(
                    f"https://github.com/ggml-org/llama.cpp/releases/download/{tag}/{asset}",
                    archive,
                    job,
                    round(index / len(assets) * 80),
                    round((index + 1) / len(assets) * 80),
                )
                job["phase"] = "extracting"
                self.extract_archive(archive, extract_root)
            if self.executable_in(extract_root) is None:
                raise RuntimeError("runtime archive did not contain llama-server.exe")
            install_root.parent.mkdir(parents=True, exist_ok=True)
            if install_root.exists():
                shutil.rmtree(install_root)
            shutil.move(str(extract_root), str(install_root))
            self._write_manifest(install_root, {"tag": tag, "backend": backend, "verified_version": tag})
            state = self._load_state()
            state.update({"installed_tag": tag, "installed_backend": backend})
            self._save_state(state)
            job["percent"] = 100
        finally:
            shutil.rmtree(extract_root, ignore_errors=True)

    def _latest_build(self, backend: str, force: bool) -> str:
        now = self._now()
        if not force and self._latest_cache and now - self._latest_cache[0] < 300:
            return self._latest_cache[1]
        candidates: list[str] = []
        payload = self._release_loader()
        for release in payload if isinstance(payload, list) else []:
            if not isinstance(release, dict) or release.get("draft"):
                continue
            tag = str(release.get("tag_name") or "")
            names = {str(item.get("name") or "") for item in release.get("assets", []) if isinstance(item, dict)}
            if self._is_release_tag(tag) and all(asset in names for asset in self._asset_names(tag, backend)):
                candidates.append(tag)
        if not candidates:
            raise RuntimeError(f"GitHub has no complete llama.cpp build for {backend}")
        tag = max(candidates, key=self._release_number)
        self._latest_cache = (now, tag)
        return tag

    def _asset_names(self, tag: str, backend: str) -> list[str]:
        if self._platform_name().lower() != "windows":
            raise RuntimeError("standalone runtime installer currently supports Windows only")
        arch = "arm64" if self._architecture().lower() in {"arm64", "aarch64"} else "x64"
        if backend == "cuda":
            # Current Windows releases publish CUDA 12.4 x64 and CUDA 13.4
            # variants.  Use the broadly compatible 12.4 package consistently;
            # it includes the matching cudart companion and is available in the
            # complete release set used for update discovery.
            version = "12.4" if arch == "x64" else "13.4"
            return [f"llama-{tag}-bin-win-cuda-{version}-{arch}.zip", f"cudart-llama-bin-win-cuda-{version}-{arch}.zip"]
        if backend == "cpu":
            return [f"llama-{tag}-bin-win-cpu-{arch}.zip"]
        if backend == "vulkan":
            return [f"llama-{tag}-bin-win-vulkan-x64.zip"]
        raise RuntimeError(f"unsupported Windows backend: {backend}")

    @staticmethod
    def _is_release_tag(tag: str) -> bool:
        return tag.startswith("b") and tag[1:].isdigit()

    @staticmethod
    def _release_number(tag: str) -> int:
        return int(tag[1:])

    @staticmethod
    def _read_manifest(root: Path) -> dict[str, Any]:
        try:
            import json
            value = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _write_manifest(root: Path, value: dict[str, Any]) -> None:
        import json
        path = root / "manifest.json"
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)

    @staticmethod
    def extract_archive(archive: Path, destination: Path) -> None:
        destination_root = destination.resolve()
        with zipfile.ZipFile(archive) as package:
            for member in package.infolist():
                target = (destination / member.filename).resolve()
                if target != destination_root and destination_root not in target.parents:
                    raise RuntimeError("runtime archive contains an unsafe path")
            package.extractall(destination)
