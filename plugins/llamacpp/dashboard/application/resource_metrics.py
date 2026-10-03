"""Build privacy-safe rolling resource metrics from projected activity events."""
from __future__ import annotations

import time
from typing import Any


def aggregate_profile_metrics(events: list[dict[str, Any]], *, now: float | None = None,
                              window_seconds: int = 60) -> dict[str, Any]:
    current = float(now if now is not None else time.time())
    window = max(10, min(int(window_seconds), 300))
    requests: dict[str, dict[str, Any]] = {}
    for event in events:
        observed_at = float(event.get("observed_at") or 0)
        profile = str(event.get("profile") or "")
        if not profile or observed_at < current - window:
            continue
        request_id = str(event.get("request_id") or "")
        key = request_id or f"{profile}:{event.get('session')}:{observed_at}"
        row = requests.setdefault(key, {
            "profile": profile,
            "role": str(event.get("role") or ""),
            "session": str(event.get("session") or ""),
            "observed_at": observed_at,
        })
        row["observed_at"] = max(float(row["observed_at"]), observed_at)
        kind = str(event.get("event") or "")
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
    }
