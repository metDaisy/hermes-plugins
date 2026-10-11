"""Resolve provider context metadata from model-owned runtime configuration."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


DEFAULT_CONTEXT_LENGTH = 65536


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _strata_context_length(entry: Mapping[str, Any]) -> int | None:
    config_path = str(entry.get("config_path") or "").strip()
    if not config_path:
        return None
    try:
        payload = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    args = payload.get("args") if isinstance(payload, dict) else None
    if not isinstance(args, list):
        return None
    for index, item in enumerate(args[:-1]):
        if str(item) == "--max-context":
            return _positive_int(args[index + 1])
    return None


def model_context_length(
    state: Mapping[str, Any], model_id: str, default: int = DEFAULT_CONTEXT_LENGTH,
) -> int:
    """Return the effective context advertised for a registered physical model."""
    settings_by_model = state.get("model_settings")
    settings = settings_by_model.get(model_id) if isinstance(settings_by_model, Mapping) else None
    if isinstance(settings, Mapping):
        configured = _positive_int(settings.get("ctx-size"))
        if configured is not None:
            return configured

    models = state.get("models")
    entry = models.get(model_id) if isinstance(models, Mapping) else None
    if isinstance(entry, Mapping) and str(entry.get("runtime_kind") or "") == "strata":
        configured = _strata_context_length(entry)
        if configured is not None:
            return configured

    return int(default)
