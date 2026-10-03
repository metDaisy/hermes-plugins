"""Machine-scoped Main/Compression execution profile bindings."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

ROLES = ("main", "compression")
LOGICAL_MODELS = {"main": "main-local", "compression": "compression-local"}


@dataclass(frozen=True)
class ExecutionProfileService:
    load_state: Callable[[], dict[str, Any]]
    save_state: Callable[[dict[str, Any]], None]
    accepts: Callable[[str, str], bool]
    mutate_state: Callable[[Callable[[dict[str, Any]], None]], dict[str, Any]] | None = None

    def snapshot(self) -> dict[str, Any]:
        state = self.load_state()
        stored = state.get("execution_profiles")
        profiles = dict(stored) if isinstance(stored, dict) else {}
        active_model = str(state.get("active_model_id") or "")
        if not isinstance(stored, dict) and "main" not in profiles and active_model:
            legacy_main = {
                "runtime_kind": str(state.get("runtime_kind") or "official"),
                "model_id": active_model,
                "preset_id": "",
            }
            profiles["main"] = legacy_main

            def persist(current: dict[str, Any]) -> None:
                stored_profiles = current.get("execution_profiles")
                next_profiles = dict(stored_profiles) if isinstance(stored_profiles, dict) else {}
                next_profiles.setdefault("main", legacy_main)
                current["execution_mode"] = "exclusive_swap"
                current["execution_profiles"] = next_profiles

            if self.mutate_state is not None:
                self.mutate_state(persist)
            else:
                persist(state)
                self.save_state(state)
        models = state.get("models") if isinstance(state.get("models"), dict) else {}
        normalized = {role: self._normalize(role, profiles.get(role)) for role in ROLES}
        for profile in normalized.values():
            model_id = profile["model_id"]
            entry = models.get(model_id) if isinstance(models.get(model_id), dict) else {}
            stored_runtime = str(entry.get("runtime_kind") or "")
            profile["runtime_kind"] = (
                stored_runtime if stored_runtime in {"official", "prism_ml"}
                else self._runtime_for_model(model_id)
            )
        normalized_storage = {
            role: {
                "runtime_kind": profile["runtime_kind"],
                "model_id": profile["model_id"],
                "preset_id": profile["preset_id"],
            }
            for role, profile in normalized.items()
            if profile["configured"]
        }
        if normalized_storage != profiles:
            def persist_normalized(current: dict[str, Any]) -> None:
                current["execution_mode"] = "exclusive_swap"
                current["execution_profiles"] = normalized_storage

            if self.mutate_state is not None:
                self.mutate_state(persist_normalized)
            else:
                persist_normalized(state)
                self.save_state(state)
        current = self.load_state()
        return {"execution_mode": str(current.get("execution_mode") or "exclusive_swap"),
                "profiles": normalized}

    def save(self, role: str, body: dict[str, Any]) -> dict[str, Any]:
        role = str(role or "").strip().lower()
        if role not in ROLES:
            raise ValueError("role은 main 또는 compression이어야 합니다")
        model_id = str(body.get("model_id") or "").strip()
        state = self.load_state()
        if not model_id:
            def clear(current: dict[str, Any]) -> None:
                stored = current.get("execution_profiles")
                profiles = dict(stored) if isinstance(stored, dict) else {}
                previous = profiles.pop(role, None)
                previous_model = str(previous.get("model_id") or "") if isinstance(previous, dict) else ""
                current["execution_mode"] = "exclusive_swap"
                current["execution_profiles"] = profiles
                if previous_model and current.get("active_model_id") == previous_model:
                    current["active_model_id"] = None

            if self.mutate_state is not None:
                self.mutate_state(clear)
            else:
                clear(state)
                self.save_state(state)
            return self.snapshot()
        models = state.get("models") if isinstance(state.get("models"), dict) else {}
        if model_id not in models:
            raise ValueError("등록된 model_id를 선택해야 합니다")
        entry = models.get(model_id) if isinstance(models.get(model_id), dict) else {}
        stored_runtime = str(entry.get("runtime_kind") or "")
        runtime_kind = stored_runtime if stored_runtime in {"official", "prism_ml"} else self._runtime_for_model(model_id)
        if not self.accepts(runtime_kind, model_id):
            raise ValueError("선택한 모델은 해당 runtime과 호환되지 않습니다")
        profile = {
            "runtime_kind": runtime_kind,
            "model_id": model_id,
            "preset_id": str(body.get("preset_id") or "").strip(),
        }
        def apply(current: dict[str, Any]) -> None:
            stored = current.get("execution_profiles")
            profiles = dict(stored) if isinstance(stored, dict) else {}
            profiles[role] = profile
            current["execution_mode"] = "exclusive_swap"
            current["execution_profiles"] = profiles

        if self.mutate_state is not None:
            self.mutate_state(apply)
        else:
            apply(state)
            self.save_state(state)
        return self.snapshot()

    @staticmethod
    def _runtime_for_model(model_id: str) -> str:
        return "prism_ml" if model_id.startswith("Ternary-Bonsai") else "official"

    @classmethod
    def _normalize(cls, role: str, raw: Any) -> dict[str, Any]:
        profile = raw if isinstance(raw, dict) else {}
        model_id = str(profile.get("model_id") or "")
        return {
            "role": role,
            "logical_model": LOGICAL_MODELS[role],
            "runtime_kind": cls._runtime_for_model(model_id),
            "model_id": model_id,
            "preset_id": str(profile.get("preset_id") or ""),
            "configured": bool(model_id),
        }
