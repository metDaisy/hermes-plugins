"""Prism-ML runtime installation workflow."""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Callable


class PrismRuntimeInstaller:
    """Install a Prism runtime behind one framework-free interface."""

    def __init__(
        self,
        root: Path,
        load_state: Callable[[], dict[str, Any]],
        save_state: Callable[[dict[str, Any]], None],
        find_git: Callable[[], str | None],
        clone: Callable[[str, str, Path], int],
        backend_detector: Callable[[], str],
        download: Callable[[str, Path, dict[str, Any], int, int], None],
        extract: Callable[[Path, Path], None],
        resolve_executable: Callable[[Path], Path],
    ) -> None:
        self._root = root
        self._load_state = load_state
        self._save_state = save_state
        self._find_git = find_git
        self._clone = clone
        self._backend_detector = backend_detector
        self._download = download
        self._extract = extract
        self._resolve_executable = resolve_executable

    def install(self, job: dict[str, Any]) -> tuple[str, str]:
        job_id = str(job["job_id"])
        staging = self._root.with_name(f".Bonsai-demo-{job_id}")
        archive = self._root.parent / "downloads" / f"prism-{job_id}.zip"
        cuda_archive = self._root.parent / "downloads" / f"prism-cudart-{job_id}.zip"
        archive.parent.mkdir(parents=True, exist_ok=True)
        try:
            git = self._find_git()
            if not git:
                raise RuntimeError("git is required to download Prism-ML Bonsai-demo")
            job.update({"phase": "cloning", "detail": "Downloading PrismML-Eng/Bonsai-demo", "percent": 5})
            if self._clone(git, "https://github.com/PrismML-Eng/Bonsai-demo.git", staging):
                raise RuntimeError("Prism-ML GitHub clone failed")
            tag, cuda_tag = self._release_metadata(staging / "setup.ps1")
            backend = "cuda" if self._backend_detector() == "cuda" else "cpu"
            base_url = f"https://github.com/PrismML-Eng/llama.cpp/releases/download/{tag}"
            asset = f"llama-{tag}-bin-win-cuda-{cuda_tag}-x64.zip" if backend == "cuda" else f"llama-{tag}-bin-win-cpu-x64.zip"
            job.update({"phase": "downloading", "detail": f"Downloading Prism llama-server ({backend})", "percent": 15})
            self._download(f"{base_url}/{asset}", archive, job, 15, 80)
            bin_dir = staging / "bin" / backend
            bin_dir.mkdir(parents=True, exist_ok=True)
            self._extract(archive, bin_dir)
            if backend == "cuda":
                try:
                    self._download(f"{base_url}/cudart-llama-bin-win-cuda-{cuda_tag}-x64.zip", cuda_archive, job, 82, 94)
                    self._extract(cuda_archive, bin_dir)
                except Exception:  # CUDA runtime is optional for a usable staged install.
                    pass
            self._resolve_executable(staging)
            # The cloned repository is metadata-only; keeping .git in the active
            # runtime makes later Windows replacement vulnerable to locked pack
            # files (WinError 5).  Remove it before materializing the runtime.
            shutil.rmtree(staging / ".git", ignore_errors=True)
            install_root = self._root
            if self._root.exists():
                try:
                    shutil.rmtree(self._root)
                except PermissionError:
                    # Preserve a locked working runtime and activate the verified
                    # new build beside it instead of failing the whole update.
                    install_root = self._root.with_name(f"{self._root.name}-{tag}-{job_id}")
                    if install_root.exists():
                        shutil.rmtree(install_root)
            install_root.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staging), str(install_root))
            state = self._load_state()
            path = str(install_root.resolve())
            state.update({
                "runtime_kind": "prism_ml", "runtime_path": path, "runtime_mode": "custom",
                "custom_runtime_path": path, "prism_release_tag": tag, "prism_backend": backend,
            })
            self._save_state(state)
            job["percent"] = 100
            return tag, backend
        finally:
            archive.unlink(missing_ok=True)
            cuda_archive.unlink(missing_ok=True)
            shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def release_metadata_from_text(setup: str) -> tuple[str, str]:
        tag = re.search(r'\$ReleaseTag\s*=\s*"([^"]+)"', setup)
        cuda_tag = re.search(r'\$CudaTag\s*=\s*"([^"]+)"', setup)
        if not tag or not cuda_tag:
            raise RuntimeError("Prism-ML setup metadata did not expose release/CUDA tags")
        return tag.group(1), cuda_tag.group(1)

    @classmethod
    def _release_metadata(cls, setup_path: Path) -> tuple[str, str]:
        return cls.release_metadata_from_text(setup_path.read_text(encoding="utf-8", errors="replace"))
