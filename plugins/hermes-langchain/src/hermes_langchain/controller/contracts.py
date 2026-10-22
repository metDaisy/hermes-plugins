"""Pure immutable contracts for deterministic workflow topology."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .executors import EXECUTOR_KINDS


ArtifactMap = dict[str, dict[str, Any]]
Projector = Callable[[dict[str, Any], ArtifactMap], dict[str, Any]]
Validator = Callable[[dict[str, Any]], bool]


class FrozenDict(dict[str, Any]):
    """JSON-compatible dictionary that rejects mutation after construction."""

    @staticmethod
    def _immutable(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("frozen mapping does not support mutation")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return FrozenDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


class WorkflowContractError(ValueError):
    """Stable fail-closed error raised by workflow contract validation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ControllerPublicError(ValueError):
    """A user-safe controller error that may cross the plugin boundary."""


@dataclass(frozen=True)
class StepDefinition:
    step_id: str
    goal: str
    allowed_toolsets: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    output_schema: dict[str, Any]
    projector: Projector
    validator: Validator
    executor_kind: str = "agent"
    timeout_seconds: int = 300
    tool_free: bool = False

    def __post_init__(self) -> None:
        if type(self.tool_free) is not bool:
            raise WorkflowContractError(
                "INVALID_TOOL_MODE",
                f"step tool_free must be boolean: {self.step_id}",
            )
        if self.tool_free and self.allowed_toolsets:
            raise WorkflowContractError(
                "CONFLICTING_TOOL_MODE",
                f"tool-free step declares toolsets: {self.step_id}",
            )
        if self.executor_kind not in EXECUTOR_KINDS:
            raise WorkflowContractError(
                "UNKNOWN_EXECUTOR_KIND",
                f"unknown step executor kind: {self.executor_kind}",
            )
        if type(self.timeout_seconds) is not int or self.timeout_seconds < 1:
            raise WorkflowContractError(
                "INVALID_TIMEOUT",
                "step timeout budget must be a positive integer",
            )
        object.__setattr__(self, "allowed_toolsets", tuple(self.allowed_toolsets))
        object.__setattr__(self, "acceptance_criteria", tuple(self.acceptance_criteria))
        object.__setattr__(self, "output_schema", _freeze(self.output_schema))


@dataclass(frozen=True)
class Transition:
    source_step_id: str
    target_step_id: str


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    step_id: str
    sha256: str


@dataclass(frozen=True)
class StepRun:
    step_id: str
    attempt: int
    artifact_ref: ArtifactRef


@dataclass(frozen=True)
class WorkflowRun:
    run_id: str
    step_runs: tuple[StepRun, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_runs", tuple(self.step_runs))


@dataclass(frozen=True)
class WorkflowDefinition:
    steps: tuple[StepDefinition, ...]
    transitions: tuple[Transition, ...]
    initial_step_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "steps", tuple(self.steps))
        object.__setattr__(self, "transitions", tuple(self.transitions))
        if not self.steps:
            raise WorkflowContractError("EMPTY_WORKFLOW", "workflow requires at least one step")
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise WorkflowContractError(
                "DUPLICATE_STEP_ID",
                "workflow step ids must be unique",
            )
        if self.initial_step_id not in set(step_ids):
            raise WorkflowContractError(
                "INVALID_INITIAL_FRONTIER",
                f"initial step does not exist: {self.initial_step_id}",
            )
        for step in self.steps:
            if not step.allowed_toolsets and not step.tool_free:
                raise WorkflowContractError(
                    "MISSING_TOOLSET",
                    f"step requires explicit allowed toolsets or tool-free mode: {step.step_id}",
                )
            if not step.acceptance_criteria:
                raise WorkflowContractError(
                    "MISSING_ACCEPTANCE_CRITERIA",
                    f"step requires acceptance criteria: {step.step_id}",
                )
            if not step.output_schema:
                raise WorkflowContractError(
                    "MISSING_OUTPUT_SCHEMA",
                    f"step requires an output schema: {step.step_id}",
                )
        self._validate_topology(frozenset(step_ids))

    def _validate_topology(self, step_ids: frozenset[str]) -> None:
        transition_keys: set[tuple[str, str]] = set()
        adjacency = {step_id: set() for step_id in step_ids}
        for transition in self.transitions:
            if transition.source_step_id not in step_ids:
                raise WorkflowContractError(
                    "UNKNOWN_TRANSITION_SOURCE",
                    f"transition source does not exist: {transition.source_step_id}",
                )
            if transition.target_step_id not in step_ids:
                raise WorkflowContractError(
                    "UNKNOWN_TRANSITION_TARGET",
                    f"transition target does not exist: {transition.target_step_id}",
                )
            key = (transition.source_step_id, transition.target_step_id)
            if key in transition_keys:
                raise WorkflowContractError(
                    "DUPLICATE_TRANSITION",
                    f"duplicate workflow transition: {key[0]} -> {key[1]}",
                )
            transition_keys.add(key)
            adjacency[key[0]].add(key[1])

        if any(
            transition.target_step_id == self.initial_step_id
            for transition in self.transitions
        ):
            raise WorkflowContractError(
                "INVALID_INITIAL_FRONTIER",
                f"initial step has an incoming transition: {self.initial_step_id}",
            )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise WorkflowContractError(
                    "WORKFLOW_CYCLE",
                    f"workflow contains a cycle at step: {step_id}",
                )
            if step_id in visited:
                return
            visiting.add(step_id)
            for target_step_id in adjacency[step_id]:
                visit(target_step_id)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in step_ids:
            visit(step_id)

        reachable: set[str] = set()
        pending = [self.initial_step_id]
        while pending:
            step_id = pending.pop()
            if step_id in reachable:
                continue
            reachable.add(step_id)
            pending.extend(adjacency[step_id])
        if reachable != set(step_ids):
            unreachable = sorted(step_ids - reachable)
            raise WorkflowContractError(
                "UNREACHABLE_STEP",
                f"workflow contains unreachable steps: {', '.join(unreachable)}",
            )

    def step(self, step_id: str) -> StepDefinition:
        for step in self.steps:
            if step.step_id == step_id:
                return step
        raise WorkflowContractError("UNKNOWN_STEP", f"unknown workflow step: {step_id}")

    def predecessors(self, step_id: str) -> frozenset[str]:
        return frozenset(
            transition.source_step_id
            for transition in self.transitions
            if transition.target_step_id == step_id
        )


def select_next_step(
    workflow: WorkflowDefinition,
    run: WorkflowRun,
) -> StepDefinition | None:
    """Select the only runnable Step from accepted Step runs."""

    workflow_step_ids = {step.step_id for step in workflow.steps}
    accepted: set[str] = set()
    for step_run in run.step_runs:
        step_id = step_run.step_id
        if step_id not in workflow_step_ids:
            raise WorkflowContractError(
                "UNKNOWN_RUN_STEP",
                f"run contains unknown step: {step_id}",
            )
        if step_id in accepted:
            raise WorkflowContractError(
                "DUPLICATE_STEP_RUN",
                f"run contains duplicate accepted step: {step_id}",
            )
        if step_run.artifact_ref.step_id != step_id:
            raise WorkflowContractError(
                "ARTIFACT_STEP_MISMATCH",
                (
                    f"artifact step {step_run.artifact_ref.step_id} does not match "
                    f"run step {step_id}"
                ),
            )
        missing_predecessors = workflow.predecessors(step_id) - accepted
        if missing_predecessors:
            raise WorkflowContractError(
                "INCONSISTENT_STEP_RUN",
                (
                    f"accepted step {step_id} is missing accepted predecessors: "
                    f"{', '.join(sorted(missing_predecessors))}"
                ),
            )
        accepted.add(step_id)
    candidates = [
        step
        for step in workflow.steps
        if step.step_id not in accepted
        and workflow.predecessors(step.step_id).issubset(accepted)
    ]
    if not candidates:
        return None
    if len(candidates) != 1:
        raise WorkflowContractError(
            "AMBIGUOUS_RUNNABLE_FRONTIER",
            "workflow has more than one runnable step",
        )
    return candidates[0]
