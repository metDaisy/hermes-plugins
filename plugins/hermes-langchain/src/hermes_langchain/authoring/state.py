from typing import Literal, TypedDict

AuthoringPhase = Literal["triage", "impl", "review", "summary", "complete"]
TriageStage = Literal[
    "intake",
    "discovery",
    "source_confirmation",
    "analysis",
    "materialize",
]


class AuthoringState(TypedDict, total=False):
    """Small, persistence-safe state passed through the authoring graph."""

    phase: AuthoringPhase
    triage_stage: TriageStage
    next_action: str | None
    errors: list[str]
