"""Build privacy-safe rolling resource metrics from projected activity events."""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Any, Callable


def read_strata_metrics(
    port: int,
    *,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any] | None:
    """Read Strata's JSON metrics and return only privacy-safe numeric/status fields."""
    open_url = opener or urllib.request.urlopen
    request = urllib.request.Request(
        f"http://127.0.0.1:{int(port)}/metrics",
        headers={"Accept": "application/json"},
    )
    try:
        with open_url(request, timeout=0.75) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - unavailable telemetry is represented as missing
        return None
    if not isinstance(payload, dict):
        return None

    live = payload.get("live") if isinstance(payload.get("live"), dict) else {}
    totals = payload.get("totals") if isinstance(payload.get("totals"), dict) else {}
    engine = payload.get("engine") if isinstance(payload.get("engine"), dict) else {}
    hardware = payload.get("hardware") if isinstance(payload.get("hardware"), dict) else {}
    requests = payload.get("requests") if isinstance(payload.get("requests"), list) else []
    last = requests[0] if requests and isinstance(requests[0], dict) else {}

    def number(value: Any) -> float | None:
        if isinstance(value, bool) or value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def integer(value: Any) -> int | None:
        numeric = number(value)
        return int(numeric) if numeric is not None else None

    prompt_tokens = integer(live.get("prompt_tokens"))
    generated = integer(live.get("generated"))
    context_tokens = (
        max(0, prompt_tokens or 0) + max(0, generated or 0)
        if prompt_tokens is not None or generated is not None else None
    )
    offered = integer(totals.get("drafts_offered"))
    accepted = integer(totals.get("drafts_accepted"))
    acceptance = round(accepted / offered * 100.0, 3) if offered and accepted is not None else None
    hit_rate = number(last.get("hit_rate"))

    return {
        "kind": "strata",
        "live": {
            "state": str(live.get("state") or "unknown")[:32],
            "phase": str(live.get("phase") or "")[:64],
            "prompt_tokens": prompt_tokens,
            "prompt_read": integer(live.get("prompt_read")),
            "generated_tokens": generated,
            "context_tokens": context_tokens,
            "prompt_tokens_per_second": number(live.get("prefill_tok_s_mean")),
            "generation_tokens_per_second": number(live.get("tok_s")),
            "queued_requests": integer(live.get("queued")) or 0,
        },
        "totals": {
            "requests": integer(totals.get("requests")) or 0,
            "prompt_tokens": integer(totals.get("prompt_tokens")) or 0,
            "output_tokens": integer(totals.get("output_tokens")) or 0,
            "reused_prompt_tokens": integer(totals.get("reused")) or 0,
            "drafts_offered": offered or 0,
            "drafts_accepted": accepted or 0,
            "draft_acceptance_percent": acceptance,
        },
        "engine": {
            "max_context": integer(engine.get("max_context") or engine.get("context")),
            "mtp_max": integer(engine.get("mtp_max")),
        },
        "hardware": {
            "gpu_util_percent": number(hardware.get("gpu_util")),
            "gpu_memory_used_bytes": integer(hardware.get("gpu_mem_used")),
            "gpu_memory_total_bytes": integer(hardware.get("gpu_mem_total")),
            "gpu_temperature_c": number(hardware.get("gpu_temp")),
            "gpu_power_watts": number(hardware.get("gpu_power")),
            "cpu_percent": number(hardware.get("cpu")),
            "ram_used_bytes": integer(hardware.get("ram_used")),
            "ram_total_bytes": integer(hardware.get("ram_total")),
        },
        "last_request": {
            "decode_tokens_per_second": number(last.get("decode_tok_s")),
            "expert_cache_hit_percent": round(hit_rate * 100.0, 3) if hit_rate is not None else None,
        },
    }


def read_worker_slot_context(
    port: int,
    *,
    model_alias: str = "",
    opener: Callable[..., Any] | None = None,
) -> dict[str, int] | None:
    """Read active slot counts from a worker or an Official native router child."""
    open_url = opener or urllib.request.urlopen

    def read_json(target_port: int, path: str) -> tuple[bool, Any]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{int(target_port)}/{path.lstrip('/')}",
            headers={"Accept": "application/json"},
        )
        try:
            with open_url(request, timeout=0.5) as response:
                return True, json.loads(response.read().decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 - unavailable telemetry is represented as missing
            return False, None

    def active_context(payload: Any) -> dict[str, int] | None:
        if not isinstance(payload, list):
            return None
        active = [slot for slot in payload if isinstance(slot, dict) and slot.get("is_processing") is True]
        if not active:
            return None
        context_tokens = max(max(0, int(slot.get("n_prompt_tokens") or 0)) for slot in active)
        context_limit = max(max(0, int(slot.get("n_ctx") or 0)) for slot in active)
        if context_limit <= 0:
            return None
        return {"context_tokens": context_tokens, "context_limit": context_limit}

    direct_ok, direct_payload = read_json(port, "slots")
    if direct_ok:
        return active_context(direct_payload)
    if not model_alias:
        return None

    models_ok, models_payload = read_json(port, "models")
    rows = models_payload.get("data") if models_ok and isinstance(models_payload, dict) else None
    if not isinstance(rows, list):
        return None
    worker_port = 0
    for row in rows:
        if not isinstance(row, dict) or str(row.get("id") or "") != model_alias:
            continue
        status = row.get("status") if isinstance(row.get("status"), dict) else {}
        args = status.get("args") if isinstance(status.get("args"), list) else []
        for index, value in enumerate(args[:-1]):
            if str(value) == "--port":
                try:
                    worker_port = int(args[index + 1])
                except (TypeError, ValueError):
                    worker_port = 0
                break
        break
    if not 0 < worker_port <= 65535 or worker_port == int(port):
        return None
    worker_ok, worker_payload = read_json(worker_port, "slots")
    return active_context(worker_payload) if worker_ok else None


def aggregate_profile_metrics(events: list[dict[str, Any]], *, now: float | None = None,
                              window_seconds: int = 60) -> dict[str, Any]:
    current = float(now if now is not None else time.time())
    window = max(5, min(int(window_seconds), 300))
    requests: dict[str, dict[str, Any]] = {}
    compression_requests: dict[str, dict[str, Any]] = {}
    for event in events:
        observed_at = float(event.get("observed_at") or 0)
        profile = str(event.get("profile") or "")
        if not profile:
            continue
        request_id = str(event.get("request_id") or "")
        key = request_id or f"{profile}:{event.get('session')}:{observed_at}"
        kind = str(event.get("event") or "")
        if str(event.get("role") or "") == "compression":
            compression = compression_requests.setdefault(key, {
                "request_id": request_id,
                "profile": profile,
                "session": str(event.get("session") or ""),
                "completed_at": observed_at,
            })
            compression["completed_at"] = max(float(compression["completed_at"]), observed_at)
            if kind == "prompt_completed":
                compression["input_context_tokens"] = int(event.get("prompt_tokens") or 0)
            elif kind == "inference_completed":
                compression["duration_ms"] = float(event.get("total_ms") or 0)
        if observed_at < current - window:
            continue
        row = requests.setdefault(key, {
            "profile": profile,
            "role": str(event.get("role") or ""),
            "session": str(event.get("session") or ""),
            "observed_at": observed_at,
        })
        row["observed_at"] = max(float(row["observed_at"]), observed_at)
        if kind == "prompt_completed":
            row["prompt_tokens"] = int(event.get("prompt_tokens") or 0)
            row["prompt_eval_ms"] = float(event.get("prompt_eval_ms") or 0)
        elif kind == "prompt_progress":
            row["prompt_progress_tokens"] = int(event.get("prompt_tokens") or 0)
            row["prompt_tokens_per_second"] = float(event.get("prompt_tokens_per_second") or 0)
        elif kind == "generation_progress":
            row["generated_tokens"] = int(event.get("generated_tokens") or 0)
            row["generation_tokens_per_second"] = float(event.get("generation_tokens_per_second") or 0)
        elif kind == "context_usage":
            row["context_tokens"] = int(event.get("context_tokens") or 0)

    compression_calls = sorted(({
        "request_id": str(row.get("request_id") or ""),
        "profile": str(row.get("profile") or ""),
        "session": str(row.get("session") or ""),
        "input_context_tokens": int(row.get("input_context_tokens") or 0),
        "duration_ms": float(row["duration_ms"]) if row.get("duration_ms") is not None else None,
        "completed_at": float(row.get("completed_at") or 0),
    } for row in compression_requests.values()
        if row.get("input_context_tokens") is not None or row.get("duration_ms") is not None),
        key=lambda row: row["completed_at"], reverse=True)[:5]

    def aggregate(group_field: str) -> dict[str, dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for request in requests.values():
            group = str(request.get(group_field) or "")
            if not group:
                continue
            row = grouped.setdefault(group, {
                group_field: group,
                "prompt_tokens": 0,
                "prompt_eval_ms": 0.0,
                "prompt_weighted_rate": 0.0,
                "generated_tokens": 0,
                "generation_weighted_rate": 0.0,
                "context_tokens": 0,
                "session": "",
                "last_activity_at": 0.0,
            })
            prompt_tokens = int(request.get("prompt_tokens") or request.get("prompt_progress_tokens") or 0)
            generated_tokens = int(request.get("generated_tokens") or 0)
            generation_rate = float(request.get("generation_tokens_per_second") or 0)
            row["prompt_tokens"] += prompt_tokens
            row["prompt_eval_ms"] += float(request.get("prompt_eval_ms") or 0)
            row["prompt_weighted_rate"] += float(request.get("prompt_tokens_per_second") or 0) * prompt_tokens
            row["generated_tokens"] += generated_tokens
            row["generation_weighted_rate"] += generation_rate * generated_tokens
            observed_at = float(request.get("observed_at") or 0)
            if observed_at >= row["last_activity_at"]:
                row["last_activity_at"] = observed_at
                row["context_tokens"] = int(request.get("context_tokens") or row["context_tokens"])
                row["session"] = str(request.get("session") or "")

        result: dict[str, Any] = {}
        for group, row in grouped.items():
            prompt_ms = float(row.pop("prompt_eval_ms"))
            prompt_weighted = float(row.pop("prompt_weighted_rate"))
            generation_weighted = float(row.pop("generation_weighted_rate"))
            generated_tokens = int(row["generated_tokens"])
            prompt_tokens = int(row["prompt_tokens"])
            row["prompt_tokens_per_second"] = (
                prompt_tokens / (prompt_ms / 1000.0) if prompt_tokens and prompt_ms > 0
                else prompt_weighted / prompt_tokens if prompt_tokens and prompt_weighted > 0
                else None
            )
            row["generation_tokens_per_second"] = (
                generation_weighted / generated_tokens if generated_tokens else None
            )
            row["tokens_per_minute"] = prompt_tokens + generated_tokens
            result[group] = row
        return result

    return {
        "window_seconds": window,
        "sampled_at": current,
        "profiles": aggregate("profile"),
        "roles": aggregate("role"),
        "compression_calls": compression_calls,
    }
