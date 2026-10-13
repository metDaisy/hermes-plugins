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
        catalog_for_runtime: Callable[[str], list[dict[str, Any]]] | None = None,
        runtime_for_model: Callable[[str], str] | None = None,
        read_context: Callable[[str, str], int | None] | None = None,
        write_context: Callable[[str, str, int], None] | None = None,
    ) -> None:
        self._catalog = catalog
        self._canonical = canonical
        self._load_options = load_options
        self._save_options = save_options
        self._preset_store = preset_store
        self._error = error
        self._now = now
        self._new_id = new_id
        self._catalog_for_runtime = catalog_for_runtime
        self._runtime_for_model = runtime_for_model
        self._read_context = read_context
        self._write_context = write_context

    @staticmethod
    def _format(runtime_kind: str) -> str:
        return "strata" if str(runtime_kind) == "strata" else "llamacpp"

    def _catalog_for(self, runtime_kind: str) -> list[dict[str, Any]]:
        if self._catalog_for_runtime is not None and runtime_kind:
            return self._catalog_for_runtime(runtime_kind)
        return self._catalog()

    def normalize(self, options: Any, runtime_kind: str = "") -> dict[str, str]:
        if not isinstance(options, dict):
            raise self._error("options must be an object")
        catalog = self._catalog_by_key(runtime_kind)
        normalized: dict[str, str] = {}
        for raw_key, value in options.items():
            key = str(raw_key).lstrip("-")
            canonical = self._canonical(catalog.get(key), value)
            # ``reasoning-effort=default`` means the flag is omitted. Do not
            # persist an empty required-value flag that would fail on startup.
            if catalog.get(key, {}).get("requires_value") and not canonical.strip():
                continue
            normalized[key] = canonical
        self.validate(normalized, runtime_kind)
        return normalized

    def validate(self, options: dict[str, str], runtime_kind: str = "") -> None:
        catalog = self._catalog_by_key(runtime_kind)
        unknown = sorted(key for key in options if key not in catalog)
        if unknown:
            label = "Strata request" if self._format(runtime_kind) == "strata" else "llama-server"
            raise self._error(f"unsupported {label} parameter(s): {', '.join(unknown)}")
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

    def model_settings(self, model_id: str, runtime_kind: str = "") -> dict[str, Any]:
        runtime_kind = runtime_kind or (self._runtime_for_model(model_id) if self._runtime_for_model is not None else "")
        stored, catalog = self._load_options().get(model_id, {}), self._catalog_for(runtime_kind)
        ordered = {}
        for option in catalog:
            key = option["key"]
            if key not in stored:
                continue
            value = self._canonical(option, stored[key])
            if option.get("requires_value") and not value.strip():
                continue
            ordered[key] = value
        context_length = self._read_context(model_id, runtime_kind) if self._read_context is not None else None
        if context_length is not None:
            context_key = self._context_key(runtime_kind)
            if context_key not in ordered:
                ordered[context_key] = str(context_length)
        return {"model_id": model_id, "runtime_kind": runtime_kind,
                "parameter_format": self._format(runtime_kind), "context_length": context_length,
                "options": ordered, "order": [option["key"] for option in catalog],
                "metadata": {option["key"]: option for option in catalog}}

    def save_model(self, model_id: str, options: Any, requires_restart: bool) -> dict[str, Any]:
        runtime_kind = self._runtime_for_model(model_id) if self._runtime_for_model is not None else ""
        normalized, all_options = self.normalize(options, runtime_kind), self._load_options()
        all_options[model_id] = normalized
        self._save_options(all_options)
        return {"model_id": model_id, "options": normalized, "applied": False,
                "requires_restart": requires_restart, "job_id": None}

    def presets(self) -> list[dict[str, Any]]:
        rows = [self._preset_row(preset_id, value) for preset_id, value in self._load_presets().items()]
        return sorted(rows, key=lambda item: (item["name"].lower(), item["id"]))

    def create_preset(self, name: Any, options: Any, source_model_id: str = "",
                      runtime_kind: str = "", context_length: Any = None) -> dict[str, Any]:
        name = self._valid_name(name)
        runtime_kind = runtime_kind or (
            self._runtime_for_model(source_model_id)
            if self._runtime_for_model is not None and source_model_id else "official"
        )
        if options is None:
            options = self._load_options().get(source_model_id, {})
        normalized, option_context = self._normalize_preset_options(options, runtime_kind)
        stored_context = self._context_length(context_length) if context_length is not None else option_context
        now, preset_id = self._now(), self._new_id()
        stored = self._preset_store.create(
            preset_id, name, normalized, now,
            self._format(runtime_kind), stored_context,
        )
        return self._preset_row(preset_id, stored)

    def apply_preset(self, preset_id: str, model_id: str, runtime_kind: str, requires_restart: bool) -> dict[str, Any]:
        value = self._load_presets().get(preset_id)
        if value is None:
            raise self._error("preset not found")
        if not model_id:
            raise self._error("model_id is required to apply a preset")
        if str(value.get("runtime_kind") or "llamacpp") != self._format(runtime_kind):
            raise self._error("preset runtime format does not match the selected model runtime format")
        applied = self.normalize(value.get("options") or {}, runtime_kind)
        omitted = sorted(key for key in applied if runtime_kind == "prism_ml" and key.startswith("spec-"))
        applied = {key: value for key, value in applied.items() if key not in omitted}
        self.validate(applied, runtime_kind)
        all_options = self._load_options()
        all_options[model_id] = applied
        self._save_options(all_options)
        self._preset_store.assign(str(model_id), preset_id)
        context_length = value.get("context_length")
        if context_length is not None and self._write_context is not None:
            self._write_context(model_id, runtime_kind, int(context_length))
        return {"preset_id": preset_id, "model_id": model_id, "options": applied, "omitted_options": omitted,
                "context_length": context_length, "applied": False, "requires_restart": requires_restart}

    def rename_preset(self, preset_id: str, name: Any = None, options: Any = None,
                      runtime_kind: str | None = None, context_length: Any = None) -> dict[str, Any]:
        current = self._preset_store.get(preset_id)
        if current is None:
            raise self._error("preset not found")
        preset_runtime = runtime_kind or str(current.get("runtime_kind") or "llamacpp")
        normalized_options = None
        next_context = None
        replace_context = False
        if options is not None:
            normalized_options, next_context = self._normalize_preset_options(options, preset_runtime)
            replace_context = True
        elif context_length is not None:
            next_context = self._context_length(context_length)
            replace_context = True
        stored = self._preset_store.update(
            preset_id,
            self._valid_name(name) if name is not None else None,
            normalized_options,
            self._now(),
            self._format(preset_runtime),
            next_context,
            replace_context,
        )
        return self._preset_row(preset_id, stored or {})

    def delete_preset(self, preset_id: str) -> None:
        if not self._preset_store.delete(preset_id):
            raise self._error("preset not found")

    def _load_presets(self) -> dict[str, dict[str, Any]]:
        return self._preset_store.presets()

    def _catalog_by_key(self, runtime_kind: str = "") -> dict[str, dict[str, Any]]:
        return {option["key"]: option for option in self._catalog_for(runtime_kind)}

    def _context_length(self, value: Any) -> int | None:
        if value is None or str(value).strip() == "":
            return None
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise self._error("context length requires an integer") from exc
        if result <= 0:
            raise self._error("context length must be greater than zero")
        return result

    def _normalize_preset_options(self, options: Any, runtime_kind: str) -> tuple[dict[str, str], int | None]:
        normalized = self.normalize(options, runtime_kind)
        context_key = self._context_key(runtime_kind)
        raw_context = normalized.get(context_key)
        context_length = self._context_length(raw_context) if raw_context is not None else None
        if self._format(runtime_kind) == "strata":
            normalized.pop(context_key, None)
        return normalized, context_length

    def _context_key(self, runtime_kind: str) -> str:
        return "max-context" if self._format(runtime_kind) == "strata" else "ctx-size"

    def _valid_name(self, name: Any) -> str:
        value = str(name or "").strip()
        if not value:
            raise self._error("preset name is required")
        if len(value) > 100:
            raise self._error("preset name must be at most 100 characters")
        return value

    @staticmethod
    def _preset_row(preset_id: str, value: dict[str, Any]) -> dict[str, Any]:
        runtime_kind = str(value.get("runtime_kind") or "llamacpp")
        options = dict(value.get("options") or {})
        context_length = value.get("context_length")
        context_key = "max-context" if runtime_kind == "strata" else "ctx-size"
        if context_length is not None and context_key not in options:
            options[context_key] = str(context_length)
        return {"id": preset_id, "name": str(value.get("name") or preset_id), "model_id": value.get("model_id"),
                "runtime_kind": runtime_kind,
                "context_length": value.get("context_length"),
                "options": options, "created_at": value.get("created_at"),
                "updated_at": value.get("updated_at")}
