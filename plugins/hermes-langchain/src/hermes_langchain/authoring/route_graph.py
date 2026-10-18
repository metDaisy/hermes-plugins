from __future__ import annotations

from collections.abc import Callable

from langgraph.graph import END, START, StateGraph

from .state import AuthoringState

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


def _route(state: AuthoringState) -> str:
    phase = state.get("phase")
    route = (
        f"triage_{state.get('triage_stage')}"
        if phase == "triage"
        else str(phase)
    )
    return route if route in _ACTION_BY_ROUTE else "invalid"


def _action_node(action: str) -> Callable[[AuthoringState], AuthoringState]:
    def run(_state: AuthoringState) -> AuthoringState:
        return {"next_action": action, "errors": []}

    return run


def _invalid_node(_state: AuthoringState) -> AuthoringState:
    return {"next_action": None, "errors": ["INVALID_AUTHORING_STATE"]}


def build_authoring_graph():
    """Compile the side-effect-free authoring routing graph."""

    builder = StateGraph(AuthoringState)
    for route, action in _ACTION_BY_ROUTE.items():
        builder.add_node(route, _action_node(action))
        builder.add_edge(route, END)
    builder.add_node("invalid", _invalid_node)
    builder.add_edge("invalid", END)
    routes = {route: route for route in _ACTION_BY_ROUTE}
    routes["invalid"] = "invalid"
    builder.add_conditional_edges(START, _route, routes)
    return builder.compile()
