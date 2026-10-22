"""Pure executor registry boundary for workflow steps."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

EXECUTOR_KINDS = ("agent", "deterministic", "gateway", "materialize")
_EXECUTOR_KIND_SET = frozenset(EXECUTOR_KINDS)


class StepExecutor(Protocol):
    def invoke(self, invocation: object) -> object: ...


class ExecutorRegistryError(ValueError):
    """Stable fail-closed executor registry error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, init=False)
class ExecutorRegistry:
    """Immutable mapping from a declared executor kind to its adapter."""

    _executors: Mapping[str, StepExecutor]

    def __init__(self, executors: Mapping[str, StepExecutor]) -> None:
        copied = dict(executors)
        unknown = [
            kind
            for kind in copied
            if not isinstance(kind, str) or kind not in _EXECUTOR_KIND_SET
        ]
        if unknown:
            raise ExecutorRegistryError(
                "UNKNOWN_EXECUTOR_KIND",
                "unknown executor kinds: "
                + ", ".join(sorted(repr(kind) for kind in unknown)),
            )
        invalid = sorted(
            kind
            for kind, executor in copied.items()
            if not callable(getattr(executor, "invoke", None))
        )
        if invalid:
            raise ExecutorRegistryError(
                "INVALID_EXECUTOR",
                f"executor has no callable invoke method: {', '.join(invalid)}",
            )
        object.__setattr__(self, "_executors", MappingProxyType(copied))

    @property
    def registered_kinds(self) -> tuple[str, ...]:
        return tuple(kind for kind in EXECUTOR_KINDS if kind in self._executors)

    def resolve(self, kind: str) -> StepExecutor:
        if kind not in _EXECUTOR_KIND_SET:
            raise ExecutorRegistryError(
                "UNKNOWN_EXECUTOR_KIND",
                f"unknown executor kind: {kind}",
            )
        executor = self._executors.get(kind)
        if executor is None:
            raise ExecutorRegistryError(
                "UNREGISTERED_EXECUTOR",
                f"executor kind is not registered: {kind}",
            )
        return executor
