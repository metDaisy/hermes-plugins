"""Project privacy-safe llama-server activity events for the Desktop UI."""
from __future__ import annotations

import json
import re
from typing import Any


_ROLE_LABELS = {"main": "Main", "compression": "Compress", "server": "Server"}
_MODEL_ROLES = {"main-local": "main", "compression-local": "compression"}


def _unwrap(line: str) -> tuple[str, str]:
    text = str(line or "").strip()
    if text.startswith("{"):
        try:
            payload = json.loads(text)
            if isinstance(payload, dict) and payload.get("type") == "log":
                return str(payload.get("msg") or "").strip(), str(payload.get("level") or "info").lower()
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    level = "warning" if re.search(r"(?:^|\s)W\s+(?:srv|cmn|slot)", text) else "error" if re.search(r"(?:^|\s)E\s+(?:srv|cmn|slot)", text) else "info"
    return text, level


def _role_for_model(model: str) -> str:
    return _MODEL_ROLES.get(model.strip(), "server")


def _emit(events: list[dict[str, Any]], lines: list[str], role: str, event: str,
          message: str, level: str = "info", **fields: Any) -> None:
    label = _ROLE_LABELS.get(role, "Server")
    lines.append(f"[{label}] {message}")
    events.append({"role": role, "level": level, "event": event, "message": message, **fields})


def project_activity_lines(raw_lines: list[str]) -> dict[str, Any]:
    """Return allowlisted events; unknown lines and request content are suppressed."""
    events: list[dict[str, Any]] = []
    lines: list[str] = []
    current_role = "server"
    port_roles: dict[str, str] = {}

    for raw in raw_lines:
        text, level = _unwrap(raw)
        if not text:
            continue

        marker = re.search(r"\[(main|compression)\]\s+--- llama-server start", text, re.IGNORECASE)
        if marker:
            current_role = marker.group(1).lower()
            _emit(events, lines, current_role, "server_initializing", "서버 초기화 중")
            continue

        spawn = re.search(r"spawning server instance with name=([^\s]+) on port (\d+)", text)
        if spawn:
            current_role = _role_for_model(spawn.group(1))
            port_roles[spawn.group(2)] = current_role
            _emit(events, lines, current_role, "model_loading", "모델 로딩 중")
            continue

        port_prefix = re.search(r"\[\s*(\d+)\]", text)
        role = port_roles.get(port_prefix.group(1), current_role) if port_prefix else current_role

        listening = re.search(r"listening on http://[^:]+:(\d+)", text)
        if listening:
            _emit(events, lines, "server", "server_ready", f"서버 준비 완료 · :{listening.group(1)}", port=int(listening.group(1)))
            continue

        evict = re.search(r"evicting idle LRU name=([^\s]+)", text)
        if evict:
            evicted_role = _role_for_model(evict.group(1))
            _emit(events, lines, evicted_role, "model_unloading", "모델 언로드 중")
            continue

        stopping = re.search(r"stopping model instance name=([^\s]+)", text)
        if stopping:
            stopped_role = _role_for_model(stopping.group(1))
            _emit(events, lines, stopped_role, "model_unloading", "모델 언로드 중")
            continue

        if re.search(r"(?:model loaded|model is loaded|model loaded successfully)", text, re.IGNORECASE):
            _emit(events, lines, role, "model_loaded", "모델 로드 완료")
            continue

        prompt = re.search(
            r"prompt processing, n_tokens\s*=\s*(\d+), progress\s*=\s*([0-9.]+)(?:,\s*([0-9.]+) tokens per second)?",
            text,
        )
        if prompt:
            tokens, progress = int(prompt.group(1)), float(prompt.group(2))
            rate = float(prompt.group(3)) if prompt.group(3) else None
            suffix = f" · {tokens} tok" + (f" · {rate:.1f} tok/s" if rate is not None else "")
            _emit(events, lines, role, "prompt_progress", f"prompt 처리 {round(progress * 100)}%{suffix}",
                  progress=progress, prompt_tokens=tokens, prompt_tokens_per_second=rate)
            continue

        generated = re.search(r"n_gen\s*=\s*(\d+),\s*tg\s*=\s*([0-9.]+) t/s", text)
        if generated:
            tokens, rate = int(generated.group(1)), float(generated.group(2))
            _emit(events, lines, role, "generation_progress", f"생성 {tokens} tok · {rate:.1f} tok/s",
                  generated_tokens=tokens, generation_tokens_per_second=rate)
            continue

        prompt_done = re.search(r"prompt eval time\s*=\s*([0-9.]+) ms\s*/\s*(\d+) tokens", text)
        if prompt_done:
            elapsed_ms, tokens = float(prompt_done.group(1)), int(prompt_done.group(2))
            _emit(events, lines, role, "prompt_completed", f"prompt 처리 완료 · {tokens} tok · {elapsed_ms / 1000:.2f}초",
                  prompt_tokens=tokens, prompt_eval_ms=elapsed_ms)
            continue

        total = re.search(r"total time\s*=\s*([0-9.]+) ms\s*/\s*(\d+) tokens", text)
        if total:
            elapsed_ms, tokens = float(total.group(1)), int(total.group(2))
            _emit(events, lines, role, "inference_completed", f"요청 완료 · {tokens} tok · {elapsed_ms / 1000:.2f}초",
                  total_tokens=tokens, total_ms=elapsed_ms)
            continue

        if level in {"warning", "warn", "error"} and not any(secret in text.lower() for secret in ("prompt", "authorization", "api key", "token")):
            safe = re.sub(r"[A-Za-z]:[\\/][^\s]+", "[PATH]", text)
            safe = safe[-300:]
            event = "error" if level == "error" else "warning"
            _emit(events, lines, role, event, safe, "error" if level == "error" else "warning")

    return {"lines": lines, "events": events}
