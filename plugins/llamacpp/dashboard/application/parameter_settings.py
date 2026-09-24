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
        preset_store: Any,
        error: Callable[[str], Exception],
        now: Callable[[], float],
        new_id: Callable[[], str],
    ) -> None:
        self._catalog = catalog
        self._canonical = canonical
        self._load_options = load_options
        self._save_options = save_options
        self._preset_store = preset_store
        self._error = error
        self._now = now
        self._new_id = new_id

    def normalize(self, options: Any) -> dict[str, str]:
        if not isinstance(options, dict):
            raise self._error("options must be an object")
        catalog = self._catalog_by_key()
        normalized: dict[str, str] = {}
        for raw_key, value in options.items():
            key = str(raw_key).lstrip("-")
            canonical = self._canonical(catalog.get(key), value)
            # ``reasoning-effort=default`` means the flag is omitted. Do not
            # persist an empty required-value flag that would fail on startup.
            if catalog.get(key, {}).get("requires_value") and not canonical.strip():
                continue
            normalized[key] = canonical
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
        ordered = {}
        for option in catalog:
            key = option["key"]
            if key not in stored:
                continue
            value = self._canonical(option, stored[key])
            if option.get("requires_value") and not value.strip():
                continue
            ordered[key] = value
        return {"model_id": model_id, "options": ordered, "order": [option["key"] for option in catalog],
                "metadata": {option["key"]: option for option in catalog}}

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
        now, preset_id = self._now(), self._new_id()
        stored = self._preset_store.create(preset_id, name, self.normalize(options), now)
        return self._preset_row(preset_id, stored)

    def apply_preset(self, preset_id: str, model_id: str, runtime_kind: str, requires_restart: bool) -> dict[str, Any]:
        value = self._load_presets().get(preset_id)
        if value is None:
            raise self._error("preset not found")
        if not model_id:
            raise self._error("model_id is required to apply a preset")
        applied = self.normalize(value.get("options") or {})
        omitted = sorted(key for key in applied if runtime_kind == "prism_ml" and key.startswith("spec-"))
        applied = {key: value for key, value in applied.items() if key not in omitted}
        self.validate(applied)
        all_options = self._load_options()
        all_options[model_id] = applied
        self._save_options(all_options)
        self._preset_store.assign(str(model_id), preset_id)
        return {"preset_id": preset_id, "model_id": model_id, "options": applied, "omitted_options": omitted,
                "applied": False, "requires_restart": requires_restart}

    def rename_preset(self, preset_id: str, name: Any = None, options: Any = None) -> dict[str, Any]:
        if self._preset_store.get(preset_id) is None:
            raise self._error("preset not found")
        stored = self._preset_store.update(
            preset_id,
            self._valid_name(name) if name is not None else None,
            self.normalize(options) if options is not None else None,
            self._now(),
        )
        return self._preset_row(preset_id, stored or {})

    def delete_preset(self, preset_id: str) -> None:
        if not self._preset_store.delete(preset_id):
            raise self._error("preset not found")

    def _load_presets(self) -> dict[str, dict[str, Any]]:
        return self._preset_store.presets()

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
