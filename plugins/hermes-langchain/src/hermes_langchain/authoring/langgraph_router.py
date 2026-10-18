"""Stable bridge from KWC authoring assignments to the LangGraph router."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from .route_graph import build_authoring_graph


@lru_cache(maxsize=1)
def _graph() -> Any:
    return build_authoring_graph()


def route_authoring(*, phase: Any, triage_stage: Any = None) -> dict[str, Any]:
    """Return one side-effect-free route result for a durable KWC assignment."""

    state: dict[str, Any] = {"phase": phase}
    if triage_stage is not None:
        state["triage_stage"] = triage_stage
    result = _graph().invoke(state)
    return {
        "next_action": result.get("next_action"),
        "errors": list(result.get("errors") or []),
    }
