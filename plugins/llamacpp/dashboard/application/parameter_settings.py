"""Model parameter settings and shared-preset application service."""
from __future__ import annotations

import re
from typing import Any, Callable


class ParameterSettingsService:
    """Owns validation, persistence, and shared-preset merge semantics."""

    def __init__(
        self,
        catalog: Callable[[], list[dict[str, Any]]],
        canonical: Callable[[dict[str, Any] | None, Any], str],
        load_options: Callable[[], dict[str, dict[str, str]]],
        save_options: Callable[[dict[str, dict[str, str]]], None],
        load_state: Callable[[], dict[str, Any]],
        save_state: Callable[[dict[str, Any]], None],
        error: Callable[[str], Exception],
        now: Callable[[], float],
        new_id: Callable[[], str],
    ) -> None:
        self._catalog = catalog
        self._canonical = canonical
        self._load_options = load_options
        self._save_options = save_options
        self._load_state = load_state
        self._save_state = save_state
        self._error = error
        self._now = now
        self._new_id = new_id

    def normalize(self, options: Any) -> dict[str, str]:
        if not isinstance(options, dict):
            raise self._error("options must be an object")
        catalog = self._catalog_by_key()
        normalized = {str(key).lstrip("-"): self._canonical(catalog.get(str(key).lstrip("-")), value)
                      for key, value in options.items()}
        self.validate(normalized)
        return normalized

    def validate(self, options: dict[str, str]) -> None:
        catalog = self._catalog_by_key()
        unknown = sorted(key for key in options if key not in catalog)
        if unknown:
            raise self._error(f"unsupported llama-server parameter(s): {', '.join(unknown)}")
        for key, value in options.items():
            metadata, text = catalog[key], str(value)
            if metadata["requires_value"] and not text.strip():
                raise self._error(f"parameter '{key}' requires a value")
            if not metadata["requires_value"] and text.strip():
                raise self._error(f"parameter '{key}' does not accept a value")
            if metadata["choices"] and text not in metadata["choices"]:
                raise self._error(f"parameter '{key}' must be one of: {', '.join(metadata['choices'])}")
            if metadata["value_kind"] == "integer" and not re.fullmatch(r"[+-]?\d+", text):
                raise self._error(f"parameter '{key}' requires an integer")
            if metadata["value_kind"] == "number":
                try:
                    float(text)
                except ValueError as exc:
                    raise self._error(f"parameter '{key}' requires a number") from exc

    def model_settings(self, model_id: str) -> dict[str, Any]:
        stored, catalog = self._load_options().get(model_id, {}), self._catalog()
        ordered = {option["key"]: self._canonical(option, stored[option["key"]])
                   for option in catalog if option["key"] in stored}
        return {"model_id": model_id, "options": ordered, "order": [option["key"] for option in catalog],
                "metadata": {option["key"]: option for option in catalog if option["key"] in ordered}}

    def save_model(self, model_id: str, options: Any, requires_restart: bool) -> dict[str, Any]:
        normalized, all_options = self.normalize(options), self._load_options()
        all_options[model_id] = normalized
        self._save_options(all_options)
        return {"model_id": model_id, "options": normalized, "applied": False,
                "requires_restart": requires_restart, "job_id": None}

    def presets(self) -> list[dict[str, Any]]:
        rows = [self._preset_row(preset_id, value) for preset_id, value in self._load_presets().items()]
        return sorted(rows, key=lambda item: (item["name"].lower(), item["id"]))

    def create_preset(self, name: Any, options: Any, source_model_id: str = "") -> dict[str, Any]:
        name = self._valid_name(name)
        if options is None:
            options = self._load_options().get(source_model_id, {})
        now, preset_id, state = self._now(), self._new_id(), self._load_state()
        stored = state.setdefault("parameter_presets", {})
        stored[preset_id] = {"name": name, "model_id": None, "options": self.normalize(options),
                             "created_at": now, "updated_at": now}
        self._save_state(state)
        return self._preset_row(preset_id, stored[preset_id])

    def apply_preset(self, preset_id: str, model_id: str, runtime_kind: str, requires_restart: bool) -> dict[str, Any]:
        value = self._load_presets().get(preset_id)
        if value is None:
            raise self._error("preset not found")
        if not model_id:
            raise self._error("model_id is required to apply a preset")
        merged = dict(self._load_options().get(model_id, {}))
        merged.update(self.normalize(value.get("options") or {}))
        omitted = sorted(key for key in merged if runtime_kind == "prism_ml" and key.startswith("spec-"))
        merged = {key: value for key, value in merged.items() if key not in omitted}
        self.validate(merged)
        all_options = self._load_options()
        all_options[model_id] = merged
        self._save_options(all_options)
        return {"preset_id": preset_id, "model_id": model_id, "options": merged, "omitted_options": omitted,
                "applied": False, "requires_restart": requires_restart}

    def rename_preset(self, preset_id: str, name: Any) -> dict[str, Any]:
        state = self._load_state()
        stored = state.get("parameter_presets")
        if not isinstance(stored, dict) or preset_id not in stored or not isinstance(stored[preset_id], dict):
            raise self._error("preset not found")
        stored[preset_id]["name"], stored[preset_id]["updated_at"] = self._valid_name(name), self._now()
        state["parameter_presets"] = stored
        self._save_state(state)
        return self._preset_row(preset_id, stored[preset_id])

    def delete_preset(self, preset_id: str) -> None:
        state = self._load_state()
        stored = state.get("parameter_presets")
        if not isinstance(stored, dict) or preset_id not in stored:
            raise self._error("preset not found")
        stored.pop(preset_id, None)
        state["parameter_presets"] = stored
        self._save_state(state)

    def _load_presets(self) -> dict[str, dict[str, Any]]:
        state = self._load_state()
        raw = state.get("parameter_presets")
        if not isinstance(raw, dict):
            state["parameter_presets"] = {}
            self._save_state(state)
            return {}
        presets, migrated = {}, False
        for preset_id, value in raw.items():
            if not isinstance(value, dict):
                continue
            preset = dict(value)
            if preset.get("model_id") is not None:
                preset["model_id"] = None
                raw[preset_id], migrated = preset, True
            presets[str(preset_id)] = preset
        if migrated:
            state["parameter_presets"] = raw
            self._save_state(state)
        return presets

    def _catalog_by_key(self) -> dict[str, dict[str, Any]]:
        return {option["key"]: option for option in self._catalog()}

    def _valid_name(self, name: Any) -> str:
        value = str(name or "").strip()
        if not value:
            raise self._error("preset name is required")
        if len(value) > 100:
            raise self._error("preset name must be at most 100 characters")
        return value

    @staticmethod
    def _preset_row(preset_id: str, value: dict[str, Any]) -> dict[str, Any]:
        return {"id": preset_id, "name": str(value.get("name") or preset_id), "model_id": value.get("model_id"),
                "options": dict(value.get("options") or {}), "created_at": value.get("created_at"),
                "updated_at": value.get("updated_at")}
