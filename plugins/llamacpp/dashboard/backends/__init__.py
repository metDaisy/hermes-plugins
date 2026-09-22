"""Backend registry for the unified llama.cpp plugin."""
from __future__ import annotations
from typing import Any
from .official import OfficialLlamaCppBackend
from .prism_ml import PrismMlBackend
_BACKENDS = {"official": OfficialLlamaCppBackend(), "prism_ml": PrismMlBackend()}
_ALIASES = {"llamacpp": "official", "llama.cpp": "official", "custom": "prism_ml", "prism": "prism_ml", "prism-ml": "prism_ml"}
def get_backend(kind: Any) -> Any:
    key = _ALIASES.get(str(kind or "official").strip().lower(), str(kind or "official").strip().lower())
    try: return _BACKENDS[key]
    except KeyError as exc: raise ValueError(f"unsupported llama.cpp backend: {kind}") from exc
def backend_ids() -> tuple[str, ...]: return tuple(_BACKENDS)
get_runtime = get_backend
runtime_ids = backend_ids
