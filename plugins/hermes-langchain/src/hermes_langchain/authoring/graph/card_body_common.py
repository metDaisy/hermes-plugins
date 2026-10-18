"""Shared helpers for type-specific card-body validation."""

from __future__ import annotations

import re
from typing import Any


SHA = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
KOREAN = re.compile(r"[가-힣]")
EPHEMERAL_COORDINATOR_MARKERS = (
    "이번 단계",
    "이번 호출",
    "이번 세션",
    "이번 graph recovery",
    "이번 그래프 복구",
    "this phase",
    "this invocation",
    "this call",
    "this session",
    "graph recovery phase",
)


def non_empty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def descriptive_korean(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value.strip()) >= 20
        and len(KOREAN.findall(value)) >= 5
    )


def strings(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(non_empty(item) for item in value)


def unique_strings(value: Any, *, allow_empty: bool = False) -> bool:
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and all(non_empty(item) for item in value)
        and len(value) == len(set(value))
    )


def append_error(errors: list[str], condition: bool, code: str) -> None:
    if not condition:
        errors.append(code)


def check_exact_fields(
    value: dict[str, Any],
    allowed: set[str],
    errors: list[str],
    prefix: str,
) -> None:
    for field in sorted(set(value) - allowed):
        errors.append(f"{prefix}:{field}")


def contains_ephemeral_coordinator_context(value: Any) -> bool:
    if isinstance(value, str):
        normalized = value.casefold()
        return any(marker.casefold() in normalized for marker in EPHEMERAL_COORDINATOR_MARKERS)
    if isinstance(value, dict):
        return any(contains_ephemeral_coordinator_context(child) for child in value.values())
    if isinstance(value, list):
        return any(contains_ephemeral_coordinator_context(child) for child in value)
    return False
