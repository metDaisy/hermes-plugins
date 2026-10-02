"""Backend registry for the unified llama.cpp plugin."""
from __future__ import annotations
from pathlib import Path
from typing import Any, Mapping
from .base import BackendView
from .official import OfficialLlamaCppBackend
from .prism_ml import PrismMlBackend
_BACKENDS = {"official": OfficialLlamaCppBackend(), "prism_ml": PrismMlBackend()}
_ALIASES = {"llamacpp": "official", "llama.cpp": "official", "custom": "prism_ml", "prism": "prism_ml", "prism-ml": "prism_ml"}
def get_backend(kind: Any) -> Any:
    raw=str(kind or "official").strip().lower(); key=_ALIASES.get(raw, raw)
    try: return _BACKENDS[key]
    except KeyError as exc: raise ValueError(f"unsupported llama.cpp backend: {kind}") from exc
def backend_ids() -> tuple[str, ...]: return tuple(_BACKENDS)
def backend_view(kind: Any, state: Mapping[str, Any], machine_root: Path) -> BackendView:
    backend=get_backend(kind)
    return BackendView(backend.key, backend.label, str(backend.managed_root(machine_root)), backend.runtime_version(dict(state)), "update" if backend.runtime_version(dict(state)) else "install")
get_runtime=get_backend
runtime_ids=backend_ids
