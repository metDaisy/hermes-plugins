"""Build a native llama-server router plan with one resident model."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any


_CONTROLLED_OPTIONS = {
    "alias", "api-key", "host", "log-colors", "log-file", "log-jsonl", "log-verbosity",
    "model", "models-autoload", "models-dir", "models-max", "models-preset", "no-models-autoload",
    "load-on-startup", "no-ui", "port", "rpc", "ssl-cert-file", "ssl-key-file",
}
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class NativeRouterPlan:
    command: list[str]
    preset_text: str
    default_model: str = "main-local"
    port: int = 18434


def _preset_value(value: Any) -> str | None:
    text = str(value).strip()
    if not text or "\n" in text or "\r" in text:
        return None
    if text == "on":
        return "true"
    if text == "off":
        return "false"
    return text


def _safe_options(options: dict[str, Any]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for raw_key, raw_value in sorted(options.items()):
        key = str(raw_key).strip()
        lowered = key.lower()
        if (
            not _KEY_RE.fullmatch(key)
            or lowered in _CONTROLLED_OPTIONS
            or any(secret in lowered for secret in ("password", "secret", "token", "api-key"))
        ):
            continue
        value = _preset_value(raw_value)
        if value is not None:
            result.append((key, value))
    return result


def build_native_router_plan(
    *,
    executable: Path,
    port: int,
    preset_path: Path,
    profiles: dict[str, dict[str, Any]],
    model_paths: dict[str, Path],
    model_options: dict[str, dict[str, Any]],
) -> NativeRouterPlan | None:
    """Return a native router plan when both roles can share one official binary."""
    required = ("main", "compression")
    selected: list[dict[str, Any]] = []
    for role in required:
        profile = profiles.get(role)
        if not isinstance(profile, dict) or not profile.get("configured"):
            return None
        selected.append(profile)
    if {str(profile.get("runtime_kind") or "") for profile in selected} != {"official"}:
        return None
    if any(role not in model_paths for role in required):
        return None

    lines = ["version = 1", "", "[*]", "stop-timeout = 30"]
    for role in required:
        logical_model = str(profiles[role].get("logical_model") or f"{role}-local")
        model_path = model_paths[role].resolve().as_posix()
        lines.extend(["", f"[{logical_model}]", f"model = {model_path}"])
        if role == "main":
            lines.append("load-on-startup = true")
        for key, value in _safe_options(model_options.get(role, {})):
            lines.append(f"{key} = {value}")

    command = [
        executable.as_posix(), "--host", "127.0.0.1", "--port", str(int(port)),
        "--models-preset", preset_path.as_posix(), "--models-max", "1", "--models-autoload",
        "--no-ui", "--log-verbosity", "3", "--log-jsonl",
    ]
    return NativeRouterPlan(command=command, preset_text="\n".join(lines) + "\n", port=int(port))
