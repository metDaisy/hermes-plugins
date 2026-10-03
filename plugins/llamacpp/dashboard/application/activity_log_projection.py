"""Project privacy-safe llama-server activity events for the Desktop UI."""
from __future__ import annotations

import json
import re
from pathlib import Path
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
          message: str, level: str = "info", contexts: dict[str, tuple[str, str]] | None = None,
          **fields: Any) -> None:
    label = _ROLE_LABELS.get(role, "Server")
    profile, session = (contexts or {}).get(role, ("", ""))
    identity = f"[{profile}][{session}]" if profile and session else ""
    lines.append(f"[{label}]{identity} {message}")
    events.append({
        "role": role,
        "level": level,
        "event": event,
        "message": message,
        **({"profile": profile, "session": session} if identity else {}),
        **fields,
    })


def contextual_activity_tail(activity_path: Path, context_path: Path,
                             limit: int = 250) -> dict[str, Any]:
    """Merge request identities journaled at activity-log byte offsets."""
    bounded = max(1, min(int(limit), 500))
    try:
        size_bytes = activity_path.stat().st_size
        read_start = max(0, size_bytes - 256 * 1024)
        with activity_path.open("rb") as stream:
            stream.seek(read_start)
            data = stream.read()
    except OSError:
        return {"path": str(activity_path), "lines": [], "size_bytes": 0}

    positioned: list[tuple[int, str]] = []
    offset = read_start
    for index, chunk in enumerate(data.splitlines(keepends=True)):
        start = offset
        offset += len(chunk)
        if read_start and index == 0:
            continue
        positioned.append((start, chunk.decode("utf-8", errors="replace").rstrip("\r\n")))
    positioned = positioned[-bounded:]
    if not positioned:
        return {"path": str(activity_path), "lines": [], "size_bytes": size_bytes}

    contexts: list[dict[str, Any]] = []
    try:
        for line in context_path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            if isinstance(value, dict) and value.get("type") == "hermes_request_context":
                contexts.append(value)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        contexts = []
    contexts.sort(key=lambda value: int(value.get("activity_offset") or 0))

    first_offset = positioned[0][0]
    seeded: dict[str, dict[str, Any]] = {}
    pending: list[dict[str, Any]] = []
    for context in contexts:
        if int(context.get("activity_offset") or 0) <= first_offset:
            seeded[str(context.get("role") or "")] = context
        else:
            pending.append(context)
    merged = [json.dumps(value, ensure_ascii=False, separators=(",", ":"))
              for value in seeded.values()]
    context_index = 0
    for line_offset, line in positioned:
        while context_index < len(pending) and int(pending[context_index].get("activity_offset") or 0) <= line_offset:
            merged.append(json.dumps(pending[context_index], ensure_ascii=False, separators=(",", ":")))
            context_index += 1
        merged.append(line)
    return {"path": str(activity_path), "lines": merged, "size_bytes": size_bytes}


def project_activity_lines(raw_lines: list[str]) -> dict[str, Any]:
    """Return allowlisted events; unknown lines and request content are suppressed."""
    events: list[dict[str, Any]] = []
    lines: list[str] = []
    current_role = "server"
    port_roles: dict[str, str] = {}
    contexts: dict[str, tuple[str, str]] = {}

    for raw in raw_lines:
        try:
            context = json.loads(str(raw or "").strip())
        except (json.JSONDecodeError, TypeError, ValueError):
            context = None
        if isinstance(context, dict) and context.get("type") == "hermes_request_context":
            context_role = str(context.get("role") or "").lower()
            profile = str(context.get("profile") or "")
            session = str(context.get("session") or "")
            if context_role in {"main", "compression"} and profile and session:
                contexts[context_role] = (profile, session)
                current_role = context_role
            continue

        text, level = _unwrap(raw)
        if not text:
            continue

        marker = re.search(r"\[(main|compression)\]\s+--- llama-server start", text, re.IGNORECASE)
        if marker:
            current_role = marker.group(1).lower()
            _emit(events, lines, current_role, "server_initializing", "서버 초기화 중", contexts=contexts)
            continue

        spawn = re.search(r"spawning server instance with name=([^\s]+) on port (\d+)", text)
        if spawn:
            current_role = _role_for_model(spawn.group(1))
            port_roles[spawn.group(2)] = current_role
            _emit(events, lines, current_role, "model_loading", "모델 로딩 중", contexts=contexts)
            continue

        port_prefix = re.search(r"\[\s*(\d+)\]", text)
        role = port_roles.get(port_prefix.group(1), current_role) if port_prefix else current_role

        listening = re.search(r"listening on http://[^:]+:(\d+)", text)
        if listening:
            _emit(events, lines, "server", "server_ready", f"서버 준비 완료 · :{listening.group(1)}", contexts=contexts, port=int(listening.group(1)))
            continue

        evict = re.search(r"evicting idle LRU name=([^\s]+)", text)
        if evict:
            evicted_role = _role_for_model(evict.group(1))
            _emit(events, lines, evicted_role, "model_unloading", "모델 언로드 중", contexts=contexts)
            continue

        stopping = re.search(r"stopping model instance name=([^\s]+)", text)
        if stopping:
            stopped_role = _role_for_model(stopping.group(1))
            _emit(events, lines, stopped_role, "model_unloading", "모델 언로드 중", contexts=contexts)
            continue

        if re.search(r"(?:model loaded|model is loaded|model loaded successfully)", text, re.IGNORECASE):
            _emit(events, lines, role, "model_loaded", "모델 로드 완료", contexts=contexts)
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
                  contexts=contexts, progress=progress, prompt_tokens=tokens, prompt_tokens_per_second=rate)
            continue

        generated = re.search(r"n_gen\s*=\s*(\d+),\s*tg\s*=\s*([0-9.]+) t/s", text)
        if generated:
            tokens, rate = int(generated.group(1)), float(generated.group(2))
            _emit(events, lines, role, "generation_progress", f"생성 {tokens} tok · {rate:.1f} tok/s",
                  contexts=contexts, generated_tokens=tokens, generation_tokens_per_second=rate)
            continue

        prompt_done = re.search(r"prompt eval time\s*=\s*([0-9.]+) ms\s*/\s*(\d+) tokens", text)
        if prompt_done:
            elapsed_ms, tokens = float(prompt_done.group(1)), int(prompt_done.group(2))
            _emit(events, lines, role, "prompt_completed", f"prompt 처리 완료 · {tokens} tok · {elapsed_ms / 1000:.2f}초",
                  contexts=contexts, prompt_tokens=tokens, prompt_eval_ms=elapsed_ms)
            continue

        total = re.search(r"total time\s*=\s*([0-9.]+) ms\s*/\s*(\d+) tokens", text)
        if total:
            elapsed_ms, tokens = float(total.group(1)), int(total.group(2))
            _emit(events, lines, role, "inference_completed", f"요청 완료 · {tokens} tok · {elapsed_ms / 1000:.2f}초",
                  contexts=contexts, total_tokens=tokens, total_ms=elapsed_ms)
            continue

        if level in {"warning", "warn", "error"} and not any(secret in text.lower() for secret in ("prompt", "authorization", "api key", "token")):
            safe = re.sub(r"[A-Za-z]:[\\/][^\s]+", "[PATH]", text)
            safe = safe[-300:]
            event = "error" if level == "error" else "warning"
            _emit(events, lines, role, event, safe, "error" if level == "error" else "warning", contexts=contexts)

    return {"lines": lines, "events": events}
