"""Machine-scoped Main/Compression execution profile bindings."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

ROLES = ("main", "compression")
RUNTIMES = ("official", "prism_ml")
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
        if "main" not in profiles and active_model:
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
        normalized = {role: self._normalize(role, profiles.get(role)) for role in ROLES}
        return {"execution_mode": "exclusive_swap", "profiles": normalized}

    def save(self, role: str, body: dict[str, Any]) -> dict[str, Any]:
        role = str(role or "").strip().lower()
        if role not in ROLES:
            raise ValueError("role은 main 또는 compression이어야 합니다")
        runtime_kind = str(body.get("runtime_kind") or "").strip().lower()
        if runtime_kind not in RUNTIMES:
            raise ValueError("runtime_kind는 official 또는 prism_ml이어야 합니다")
        model_id = str(body.get("model_id") or "").strip()
        state = self.load_state()
        models = state.get("models") if isinstance(state.get("models"), dict) else {}
        if not model_id or model_id not in models:
            raise ValueError("등록된 model_id를 선택해야 합니다")
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
    def _normalize(role: str, raw: Any) -> dict[str, Any]:
        profile = raw if isinstance(raw, dict) else {}
        model_id = str(profile.get("model_id") or "")
        return {
            "role": role,
            "logical_model": LOGICAL_MODELS[role],
            "runtime_kind": str(profile.get("runtime_kind") or "official"),
            "model_id": model_id,
            "preset_id": str(profile.get("preset_id") or ""),
            "configured": bool(model_id),
        }
