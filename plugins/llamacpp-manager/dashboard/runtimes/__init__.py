"""Runtime adapter interface and registry for llama.cpp backends."""
from __future__ import annotations

from typing import Any

from .llamacpp import LlamaCppRuntime
from .prism_ml import PrismMlRuntime

_RUNTIME_ADAPTERS = {
    "llamacpp": LlamaCppRuntime(),
    "prism_ml": PrismMlRuntime(),
}
_ALIASES = {
    "official": "llamacpp",
    "llama.cpp": "llamacpp",
    "custom": "prism_ml",
    "prism": "prism_ml",
    "prism-ml": "prism_ml",
}


def get_runtime(kind: Any) -> Any:
    key = str(kind or "llamacpp").strip().lower()
    key = _ALIASES.get(key, key)
    try:
        return _RUNTIME_ADAPTERS[key]
    except KeyError as exc:
        raise ValueError(f"unsupported llama.cpp runtime: {kind}") from exc


def runtime_ids() -> tuple[str, ...]:
    return tuple(_RUNTIME_ADAPTERS)
