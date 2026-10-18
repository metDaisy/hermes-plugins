"""Small project-independent KWC activity and port contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")


class WorkflowStateStore(Protocol):
    def load(self, workflow_id: str) -> dict[str, Any] | None: ...
    def save(self, workflow_id: str, state: dict[str, Any]) -> None: ...


class ArtifactStore(Protocol):
    def read_json(self, path: Path) -> Any: ...
    def write_json(self, path: Path, value: Any) -> None: ...


class KanbanPort(Protocol):
    def list_active_triage(self, board: str) -> list[dict[str, Any]]: ...
    def create_triage(self, *, board: str, card: dict[str, Any]) -> str: ...
    def read_task(self, *, board: str, task_id: str) -> dict[str, Any]: ...
    def archive_task(self, *, board: str, task_id: str) -> None: ...


class Activity(ABC, Generic[InputT, OutputT]):
    @abstractmethod
    def execute(self, request: InputT) -> OutputT:
        raise NotImplementedError


@dataclass(frozen=True)
class ActivityResult(Generic[OutputT]):
    value: OutputT


class ActivityExecutor:
    def execute(self, activity: Activity[InputT, OutputT], request: InputT) -> ActivityResult[OutputT]:
        return ActivityResult(activity.execute(request))


class ObjectContainer:
    def __init__(self) -> None:
        self._values: dict[type[Any], Any] = {}

    def bind(self, contract: type[InputT], value: InputT) -> None:
        if contract in self._values:
            raise ValueError(f"duplicate binding: {contract.__name__}")
        self._values[contract] = value

    def require(self, contract: type[InputT]) -> InputT:
        try:
            return self._values[contract]
        except KeyError as exc:
            raise ValueError(f"missing binding: {contract.__name__}") from exc
