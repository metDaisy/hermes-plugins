"""Prism-ML llama.cpp release discovery and installation."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any, Callable


DownloadArchive = Callable[[str, Path, dict[str, Any], int, int], None]


class PrismRuntimeInstaller:
    """Install verified PrismML-Eng/llama.cpp GitHub release assets."""

    def __init__(
        self,
        root: Path,
        load_state: Callable[[], dict[str, Any]],
        save_state: Callable[[dict[str, Any]], None],
        release_loader: Callable[[], Any],
        backend_detector: Callable[[], str],
        platform_name: Callable[[], str] = platform.system,
        architecture: Callable[[], str] = platform.machine,
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
        self._latest_cache: tuple[float, tuple[str, str]] | None = None

    def latest_target(self, requested_backend: Any = None, force: bool = False) -> tuple[str, str]:
        backend = str(requested_backend or "auto").lower()
        if backend not in {"auto", "cuda", "cpu", "vulkan"}:
            backend = "auto"
        if backend == "auto":
            backend = "cuda" if self._backend_detector() == "cuda" else "cpu"
        now = self._now()
        if not force and self._latest_cache and now - self._latest_cache[0] < 300:
            cached = self._latest_cache[1]
            if cached[1] == backend:
                return cached
        candidates: list[str] = []
        payload = self._release_loader()
        for release in payload if isinstance(payload, list) else []:
            if not isinstance(release, dict) or release.get("draft"):
                continue
            tag = str(release.get("tag_name") or "")
            names = {
                str(item.get("name") or "")
                for item in release.get("assets", [])
                if isinstance(item, dict)
            }
            if self._is_release_tag(tag) and all(asset in names for asset in self.asset_names(tag, backend)):
                candidates.append(tag)
        if not candidates:
            raise RuntimeError(f"GitHub has no complete Prism-ML llama.cpp build for {backend}")
        target = (max(candidates, key=self._release_number), backend)
        self._latest_cache = (now, target)
        return target

    def install(
        self,
        tag: str,
        backend: str,
        job: dict[str, Any],
        download: DownloadArchive,
    ) -> tuple[str, str]:
        assets = self.asset_names(tag, backend)
        job_id = str(job.get("job_id") or "runtime")
        download_root = self._root / "downloads"
        extract_root = self._root / f".{tag}-{backend}-{job_id}"
        install_root = self._root / tag / backend
        download_root.mkdir(parents=True, exist_ok=True)
        extract_root.mkdir(parents=True, exist_ok=True)
        try:
            for index, asset in enumerate(assets):
                archive = download_root / asset
                job.update({"phase": "downloading", "detail": f"Downloading {asset}"})
                download(
                    f"https://github.com/PrismML-Eng/llama.cpp/releases/download/{tag}/{asset}",
                    archive,
                    job,
                    round(index / len(assets) * 90),
                    round((index + 1) / len(assets) * 90),
                )
                job["phase"] = "extracting"
                self.extract_archive(archive, extract_root)
            if self.executable_in(extract_root) is None:
                raise RuntimeError("Prism-ML runtime archive did not contain llama-server.exe")
            install_root.parent.mkdir(parents=True, exist_ok=True)
            if install_root.exists():
                try:
                    shutil.rmtree(install_root)
                except PermissionError:
                    install_root = self._root / tag / f"{backend}-{job_id}"
                    if install_root.exists():
                        shutil.rmtree(install_root)
            shutil.move(str(extract_root), str(install_root))
            self._write_manifest(install_root, {
                "tag": tag,
                "backend": backend,
                "verified_version": tag,
                "repository": "PrismML-Eng/llama.cpp",
            })
            state = self._load_state()
            path = str(install_root.resolve())
            state.update({
                "runtime_kind": "prism_ml",
                "runtime_path": path,
                "runtime_mode": "custom",
                "custom_runtime_path": path,
                "prism_release_tag": tag,
                "prism_backend": backend,
            })
            self._save_state(state)
            job["percent"] = 100
            return tag, backend
        finally:
            shutil.rmtree(extract_root, ignore_errors=True)

    def asset_names(self, tag: str, backend: str) -> list[str]:
        if self._platform_name().lower() != "windows":
            raise RuntimeError("Prism-ML runtime installer currently supports Windows only")
        arch = "arm64" if self._architecture().lower() in {"arm64", "aarch64"} else "x64"
        if backend == "cuda":
            cuda = "13.4" if arch == "arm64" else "12.4"
            return [
                f"llama-{tag}-bin-win-cuda-{cuda}-{arch}.zip",
                f"cudart-llama-bin-win-cuda-{cuda}-{arch}.zip",
            ]
        if backend == "cpu":
            return [f"llama-{tag}-bin-win-cpu-{arch}.zip"]
        if backend == "vulkan" and arch == "x64":
            return [f"llama-{tag}-bin-win-vulkan-x64.zip"]
        raise RuntimeError(f"unsupported Windows Prism-ML backend: {backend}")

    @staticmethod
    def executable_in(root: Path) -> Path | None:
        if not root.is_dir():
            return None
        candidates = [root / "llama-server.exe", root / "llama-server", *root.rglob("llama-server.exe")]
        return next((candidate for candidate in candidates if candidate.is_file()), None)

    @staticmethod
    def _is_release_tag(tag: str) -> bool:
        return re.fullmatch(r"prism-b\d+-[0-9a-f]+", tag) is not None

    @staticmethod
    def _release_number(tag: str) -> int:
        match = re.search(r"-b(\d+)-", tag)
        return int(match.group(1)) if match else -1

    @staticmethod
    def _write_manifest(root: Path, value: dict[str, Any]) -> None:
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