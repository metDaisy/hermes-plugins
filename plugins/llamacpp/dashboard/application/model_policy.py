"""Backend-aware model compatibility policy."""
from __future__ import annotations
from typing import Iterable, Protocol
try:
    from dashboard.domain.prism_catalog import PrismCatalog
except ImportError:
    from domain.prism_catalog import PrismCatalog

class Catalog(Protocol):
    def accepts(self, repository: str, paths: Iterable[str], runtime_version: str | None = None) -> bool: ...

class ModelPolicyService:
    def __init__(self, prism_catalog: Catalog | None = None) -> None:
        self._prism = prism_catalog or PrismCatalog()

    def accepts(self, backend_kind: str, repository: str, paths: Iterable[str], runtime_version: str | None = None) -> bool:
        values = tuple(paths)
        if backend_kind != 'prism_ml': return bool(values) and all(str(path).lower().endswith('.gguf') for path in values)
        return self._prism.accepts(repository, values, runtime_version)

    def visible_repositories(self, backend_kind: str, repositories: Iterable[str]) -> list[str]:
        if backend_kind != 'prism_ml': return list(repositories)
        allowed=set(self._prism.supported_repositories())
        return [repo for repo in repositories if repo in allowed]
