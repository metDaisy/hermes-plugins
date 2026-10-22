"""Stable bridge from KWC authoring assignments to the LangGraph router."""

from __future__ import annotations

from functools import lru_cache
from typing import Any


_ACTION_BY_ROUTE = {
    "triage_intake": "kwc_advance_triage",
    "triage_discovery": "kwc_advance_triage",
    "triage_source_confirmation": "kwc_advance_triage",
    "triage_analysis": "kwc_advance_triage",
    "triage_materialize": "kwc_create_triage",
    "impl": "kwc_create_impl_graph",
    "review": "kwc_create_review",
    "summary": "kwc_create_summary",
    "complete": "complete",
}


@lru_cache(maxsize=1)
def _graph() -> Any:
    from .route_graph import build_authoring_graph

    return build_authoring_graph()


def _deterministic_route(*, phase: Any, triage_stage: Any = None) -> dict[str, Any]:
    route = f"triage_{triage_stage}" if phase == "triage" else str(phase)
    action = _ACTION_BY_ROUTE.get(route)
    if action is None:
        return {"next_action": None, "errors": ["INVALID_AUTHORING_STATE"]}
    return {"next_action": action, "errors": []}


def route_authoring(*, phase: Any, triage_stage: Any = None) -> dict[str, Any]:
    """Return one side-effect-free route result for a durable KWC assignment."""

    state: dict[str, Any] = {"phase": phase}
    if triage_stage is not None:
        state["triage_stage"] = triage_stage
    try:
        result = _graph().invoke(state)
    except ModuleNotFoundError as exc:
        if exc.name != "langgraph" and not str(exc.name).startswith("langgraph."):
            raise
        return _deterministic_route(phase=phase, triage_stage=triage_stage)
    return {
        "next_action": result.get("next_action"),
        "errors": list(result.get("errors") or []),
    }
