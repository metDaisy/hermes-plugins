"""Versioned, explicit Prism-ML GGUF compatibility catalog."""
from __future__ import annotations
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Iterable

@dataclass(frozen=True)
class PrismModelSpec:
    family: str
    size: str
    repository: str
    filename_patterns: tuple[str, ...]
    preferred_backends: tuple[str, ...] = ()

class PrismCatalog:
    """Pure compatibility policy; no HTTP, state, filesystem, or UI concerns."""
    minimum_runtime_build = 10658
    specs = (
        PrismModelSpec("bonsai2", "27B", "prism-ml/Ternary-Bonsai-2-27B-gguf", ("*-PQ2_0.gguf",)),
        PrismModelSpec("ternary", "27B", "prism-ml/Ternary-Bonsai-27B-gguf", ("*-PQ2_0.gguf", "*-Q2_g64.gguf")),
        PrismModelSpec("ternary", "8B", "prism-ml/Ternary-Bonsai-8B-gguf", ("*-PQ2_0.gguf", "*-Q2_0_g64.gguf")),
        PrismModelSpec("ternary", "4B", "prism-ml/Ternary-Bonsai-4B-gguf", ("*-PQ2_0.gguf", "*-Q2_0_g64.gguf")),
        PrismModelSpec("ternary", "1.7B", "prism-ml/Ternary-Bonsai-1.7B-gguf", ("*-PQ2_0.gguf", "*-Q2_0_g64.gguf")),
        PrismModelSpec("bonsai", "27B", "prism-ml/Bonsai-27B-gguf", ("*-Q1_0.gguf",)),
        PrismModelSpec("bonsai", "8B", "prism-ml/Bonsai-8B-gguf", ("*-Q1_0.gguf",)),
        PrismModelSpec("bonsai", "4B", "prism-ml/Bonsai-4B-gguf", ("*-Q1_0.gguf",)),
        PrismModelSpec("bonsai", "1.7B", "prism-ml/Bonsai-1.7B-gguf", ("*-Q1_0.gguf",)),
    )

    def supported_repositories(self) -> tuple[str, ...]:
        return tuple(spec.repository for spec in self.specs)

    def candidates(self, backend: str = "cuda") -> tuple[PrismModelSpec, ...]:
        return self.specs

    def accepts(self, repository: str, paths: Iterable[str], runtime_version: str | None = None) -> bool:
        spec = next((item for item in self.specs if item.repository.lower() == str(repository).lower()), None)
        files = tuple(str(path).replace('\\', '/').split('/')[-1] for path in paths)
        if spec is None or not files or not all(name.lower().endswith('.gguf') for name in files): return False
        # Never permit legacy un-suffixed ternary Q2_0 files on current Prism builds.
        if spec.family == 'ternary' and any(name.endswith('-Q2_0.gguf') for name in files): return False
        return all(any(fnmatch(name, pattern) for pattern in spec.filename_patterns) for name in files)
