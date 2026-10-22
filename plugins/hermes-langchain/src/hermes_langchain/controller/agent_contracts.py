"""Framework-independent fresh-Agent invocation contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class StepInvocation:
    run_id: str
    invocation_id: str
    step_id: str
    goal: str
    allowed_toolsets: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    output_schema: dict[str, Any]
    input: dict[str, Any]
    executor_kind: str = "agent"
    attempt: int = 1
    timeout_seconds: int = 300
    working_directory: str | None = None
    skills: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentResult:
    agent_id: str
    output: dict[str, Any]


class AgentExecutor(Protocol):
    def invoke(self, invocation: StepInvocation) -> AgentResult: ...
