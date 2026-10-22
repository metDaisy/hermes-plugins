"""Minimal controller-owned LangGraph workflow runtime."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, TypedDict, cast

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.func import task
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .artifact_store import (
    ArtifactRecord,
    ArtifactStoreError,
    SQLiteArtifactStore,
    canonical_json_sha256,
)
from .agent_contracts import AgentExecutor, AgentResult, StepInvocation
from .contracts import (
    ArtifactMap,
    ArtifactRef,
    ControllerPublicError,
    StepDefinition,
    StepRun,
    WorkflowContractError,
    WorkflowDefinition,
    WorkflowRun,
    _freeze,
    select_next_step,
)
from .executors import ExecutorRegistry

class ControllerState(TypedDict, total=False):
    run_id: str
    initial_input: dict[str, Any]
    candidate: dict[str, Any]
    artifacts: list[dict[str, Any]]
    invocations: list[dict[str, Any]]
    status: str
    next_step: str | None
    error: str | None


class ControllerRuntime:
    """Execute a sequential workflow with durable LangGraph checkpoints."""

    def __init__(
        self,
        *,
        workflow: WorkflowDefinition,
        executor_registry: ExecutorRegistry,
        connection: sqlite3.Connection,
        artifact_store: SQLiteArtifactStore,
    ) -> None:
        self._workflow = workflow
        self._executor_registry = executor_registry
        self._connection = connection
        self._artifact_store = artifact_store
        self._agent_task = self._build_agent_task()
        self._graph = self._build_graph(SqliteSaver(connection))

    @classmethod
    def open(
        cls,
        *,
        workflow: WorkflowDefinition,
        checkpoint_path: Path,
        executor: AgentExecutor | None = None,
        executor_registry: ExecutorRegistry | None = None,
        artifact_store_path: Path | None = None,
    ) -> "ControllerRuntime":
        if executor_registry is None:
            if executor is None:
                raise ValueError("controller runtime requires an executor registry")
            executor_registry = ExecutorRegistry({"agent": executor})
        elif executor is not None:
            raise ValueError("provide executor or executor_registry, not both")
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(checkpoint_path), check_same_thread=False)
        store_path = artifact_store_path or checkpoint_path.with_name(
            f"{checkpoint_path.stem}.artifacts.db"
        )
        artifact_store = SQLiteArtifactStore.open(store_path)
        return cls(
            workflow=workflow,
            executor_registry=executor_registry,
            connection=connection,
            artifact_store=artifact_store,
        )

    def start(self, *, run_id: str, initial_input: dict[str, Any]) -> dict[str, Any]:
        if self._graph.get_state(self._config(run_id)).values:
            raise ControllerPublicError(f"run already exists: {run_id}")
        initial_step = select_next_step(self._workflow, WorkflowRun(run_id=run_id))
        if initial_step is None:
            raise WorkflowContractError("EMPTY_FRONTIER", "workflow has no initial runnable step")
        state: ControllerState = {
            "run_id": run_id,
            "initial_input": dict(initial_input),
            "artifacts": [],
            "invocations": [],
            "status": "running",
            "next_step": initial_step.step_id,
            "error": None,
        }
        result = self._graph.invoke(state, self._config(run_id))
        return self._public_state(cast(ControllerState, result))

    def resume(self, *, run_id: str) -> dict[str, Any]:
        self._require_existing_run(run_id)
        result = self._graph.invoke(Command(resume=True), self._config(run_id))
        return self._public_state(cast(ControllerState, result))

    def snapshot(self, *, run_id: str) -> dict[str, Any]:
        snapshot = self._graph.get_state(self._config(run_id))
        if not snapshot.values:
            raise ControllerPublicError(f"controller run not found: {run_id}")
        return self._public_state(cast(ControllerState, snapshot.values))

    def close(self) -> None:
        self._artifact_store.close()
        self._connection.close()

    @staticmethod
    def _config(run_id: str) -> dict[str, dict[str, str]]:
        return {"configurable": {"thread_id": run_id}}

    def _require_existing_run(self, run_id: str) -> None:
        if not self._graph.get_state(self._config(run_id)).values:
            raise ControllerPublicError(f"controller run not found: {run_id}")

    def _build_graph(self, checkpointer: SqliteSaver) -> Any:
        builder = StateGraph(ControllerState)
        builder.add_node("execute_step", self._execute_step)
        builder.add_node("validate_output", self._validate_output)
        builder.add_node("persist_artifact", self._persist_artifact)
        builder.add_node("advance", self._advance)
        builder.add_node("await_resume", self._await_resume)
        builder.add_edge(START, "execute_step")
        builder.add_edge("execute_step", "validate_output")
        builder.add_conditional_edges(
            "validate_output",
            self._route_after_validation,
            {"persist": "persist_artifact", "blocked": END},
        )
        builder.add_edge("persist_artifact", "advance")
        builder.add_conditional_edges(
            "advance",
            self._route_after_advance,
            {"pause": "await_resume", "complete": END},
        )
        builder.add_edge("await_resume", "execute_step")
        return builder.compile(checkpointer=checkpointer)

    def _build_agent_task(self) -> Any:
        steps_by_id = {step.step_id: step for step in self._workflow.steps}

        @task(name="controller_agent_invoke")
        def invoke_agent(payload: dict[str, Any]) -> dict[str, Any]:
            try:
                step = steps_by_id[str(payload["step_id"])]
                artifacts = self._artifact_map(
                    cast(list[dict[str, Any]], payload["artifacts"])
                )
                projected_input = cast(
                    dict[str, Any],
                    _freeze(
                        dict(
                            step.projector(
                                dict(payload["initial_input"]),
                                artifacts,
                            )
                        )
                    ),
                )
                input_sha256 = canonical_json_sha256(projected_input)
                prior_executor_identities = {
                    str(item["agent_id"])
                    for item in cast(list[dict[str, Any]], payload["invocations"])
                }
                attempt = int(payload["attempt"])
                artifact_id = f"{payload['run_id']}:{step.step_id}:{attempt}"
                try:
                    existing = self._artifact_store.get(artifact_id)
                except ArtifactStoreError as exc:
                    if exc.code != "ARTIFACT_NOT_FOUND":
                        raise
                else:
                    self._validate_replay(existing, projected_input, step, attempt)
                    if existing.executor_identity in prior_executor_identities:
                        return {
                            "kind": "reused_executor_identity",
                            "agent_id": existing.executor_identity,
                        }
                    return self._accepted_task_result(existing)

                invocation = StepInvocation(
                    run_id=str(payload["run_id"]),
                    invocation_id=str(payload["invocation_id"]),
                    step_id=str(payload["step_id"]),
                    goal=str(payload["goal"]),
                    allowed_toolsets=tuple(payload["allowed_toolsets"]),
                    acceptance_criteria=tuple(payload["acceptance_criteria"]),
                    output_schema=dict(payload["output_schema"]),
                    input=projected_input,
                    executor_kind=str(payload["executor_kind"]),
                    attempt=int(payload["attempt"]),
                    timeout_seconds=int(payload["timeout_seconds"]),
                )
                executor = self._executor_registry.resolve(invocation.executor_kind)
                result = executor.invoke(invocation)
                output = dict(result.output)
                if not step.validator(output):
                    return {"kind": "invalid_output"}
                if result.agent_id in prior_executor_identities:
                    return {
                        "kind": "reused_executor_identity",
                        "agent_id": result.agent_id,
                    }
                record = ArtifactRecord(
                    artifact_id=artifact_id,
                    run_id=invocation.run_id,
                    step_id=invocation.step_id,
                    attempt=invocation.attempt,
                    executor_kind=invocation.executor_kind,
                    input_sha256=input_sha256,
                    output_sha256=canonical_json_sha256(output),
                    timeout_seconds=invocation.timeout_seconds,
                    validation_result="accepted",
                    executor_identity=result.agent_id,
                    payload=output,
                )
                stored = self._artifact_store.put(record, input_value=projected_input)
            except Exception as exc:
                return {"kind": "failure", "failure": type(exc).__name__}
            self._after_artifact_write(stored)
            return self._accepted_task_result(stored)

        return invoke_agent

    def _step(self, state: ControllerState) -> StepDefinition:
        step_id = state.get("next_step")
        if not step_id:
            raise WorkflowContractError("EMPTY_FRONTIER", "controller has no current step")
        return self._workflow.step(step_id)

    def _execute_step(self, state: ControllerState) -> dict[str, Any]:
        step = self._step(state)
        prior_attempts = sum(
            1 for item in state["invocations"] if item["step_id"] == step.step_id
        )
        invocation = StepInvocation(
            run_id=state["run_id"],
            invocation_id=f"{state['run_id']}:{step.step_id}:{prior_attempts + 1}",
            step_id=step.step_id,
            goal=step.goal,
            allowed_toolsets=step.allowed_toolsets,
            acceptance_criteria=step.acceptance_criteria,
            output_schema=dict(step.output_schema),
            input={},
            executor_kind=step.executor_kind,
            attempt=prior_attempts + 1,
            timeout_seconds=step.timeout_seconds,
        )
        task_result = self._agent_task(
            {
                "run_id": invocation.run_id,
                "invocation_id": invocation.invocation_id,
                "step_id": invocation.step_id,
                "goal": invocation.goal,
                "allowed_toolsets": list(invocation.allowed_toolsets),
                "acceptance_criteria": list(invocation.acceptance_criteria),
                "output_schema": invocation.output_schema,
                "initial_input": state["initial_input"],
                "artifacts": state["artifacts"],
                "invocations": state["invocations"],
                "executor_kind": invocation.executor_kind,
                "attempt": invocation.attempt,
                "timeout_seconds": invocation.timeout_seconds,
            }
        ).result()
        return {"candidate": self._candidate_from_task_result(task_result) | {
            "invocation_id": invocation.invocation_id
        }}

    def _candidate_from_task_result(self, task_result: dict[str, Any]) -> dict[str, Any]:
        kind = task_result.get("kind")
        if kind == "failure":
            return {"failure": str(task_result["failure"])}
        if kind == "invalid_output":
            return {"invalid_output": True}
        if kind == "reused_executor_identity":
            return {"reused_agent_id": str(task_result["agent_id"])}
        if kind != "success":
            return {"failure": "InvalidTaskReceipt"}
        return {
            "agent_id": str(task_result["agent_id"]),
            "artifact": dict(task_result["artifact"]),
        }

    def _validate_output(self, state: ControllerState) -> dict[str, Any]:
        step = self._step(state)
        failure = state["candidate"].get("failure")
        if failure:
            return {
                "status": "blocked",
                "error": f"agent execution failed for step {step.step_id}: {failure}",
                "next_step": None,
            }
        if state["candidate"].get("invalid_output"):
            return {
                "status": "blocked",
                "error": f"invalid output for step: {step.step_id}",
                "next_step": None,
            }
        reused_agent_id = state["candidate"].get("reused_agent_id")
        if reused_agent_id:
            return {
                "status": "blocked",
                "error": f"agent execution must be fresh: {reused_agent_id}",
                "next_step": None,
            }
        agent_id = str(state["candidate"]["agent_id"])
        if any(item["agent_id"] == agent_id for item in state["invocations"]):
            return {
                "status": "blocked",
                "error": f"agent execution must be fresh: {agent_id}",
                "next_step": None,
            }
        return {"status": "validated", "error": None}

    @staticmethod
    def _route_after_validation(state: ControllerState) -> str:
        return "persist" if state["status"] == "validated" else "blocked"

    def _persist_artifact(self, state: ControllerState) -> dict[str, Any]:
        step = self._step(state)
        candidate = state["candidate"]
        artifact = dict(candidate["artifact"])
        invocation = {
            "invocation_id": candidate["invocation_id"],
            "step_id": step.step_id,
            "agent_id": candidate["agent_id"],
        }
        return {
            "artifacts": [*state["artifacts"], artifact],
            "invocations": [*state["invocations"], invocation],
        }

    def _advance(self, state: ControllerState) -> dict[str, Any]:
        step_runs = tuple(
            StepRun(
                step_id=str(artifact["step_id"]),
                attempt=int(artifact["attempt"]),
                artifact_ref=ArtifactRef(
                    artifact_id=str(artifact["artifact_id"]),
                    step_id=str(artifact["step_id"]),
                    sha256=str(artifact["sha256"]),
                ),
            )
            for artifact in state["artifacts"]
        )
        try:
            next_step = select_next_step(
                self._workflow,
                WorkflowRun(run_id=state["run_id"], step_runs=step_runs),
            )
        except WorkflowContractError as exc:
            return {
                "status": "blocked",
                "next_step": None,
                "error": f"workflow contract failed: {exc.code}",
            }
        if next_step is None:
            return {"status": "completed", "next_step": None}
        return {
            "status": "awaiting_resume",
            "next_step": next_step.step_id,
        }

    @staticmethod
    def _route_after_advance(state: ControllerState) -> str:
        if state["status"] in {"completed", "blocked"}:
            return "complete"
        if state["status"] == "awaiting_resume":
            return "pause"
        raise WorkflowContractError(
            "INVALID_RUNTIME_STATUS",
            f"invalid status after advance: {state['status']}",
        )

    @staticmethod
    def _await_resume(state: ControllerState) -> dict[str, Any]:
        interrupt({"run_id": state["run_id"], "next_step": state["next_step"]})
        return {"status": "running"}

    def _public_state(self, state: ControllerState) -> dict[str, Any]:
        return {
            "run_id": state["run_id"],
            "status": state["status"],
            "next_step": state.get("next_step"),
            "artifacts": list(self._artifact_map(state["artifacts"]).values()),
            "invocations": list(state["invocations"]),
            "error": state.get("error"),
        }

    def _artifact_map(self, references: list[dict[str, Any]]) -> ArtifactMap:
        artifacts: ArtifactMap = {}
        for reference in references:
            record = self._artifact_store.get(str(reference["artifact_id"]))
            if (
                record.step_id != reference["step_id"]
                or record.output_sha256 != reference["sha256"]
            ):
                raise ArtifactStoreError(
                    "ARTIFACT_REF_MISMATCH",
                    f"artifact reference mismatch: {record.artifact_id}",
                )
            artifacts[record.step_id] = {
                "artifact_id": record.artifact_id,
                "step_id": record.step_id,
                "payload": dict(record.payload),
                "sha256": record.output_sha256,
                "agent_id": record.executor_identity,
            }
        return artifacts

    @staticmethod
    def _accepted_task_result(record: ArtifactRecord) -> dict[str, Any]:
        return {
            "kind": "success",
            "agent_id": record.executor_identity,
            "artifact": {
                "artifact_id": record.artifact_id,
                "step_id": record.step_id,
                "sha256": record.output_sha256,
                "attempt": record.attempt,
                "executor_kind": record.executor_kind,
                "input_sha256": record.input_sha256,
                "timeout_seconds": record.timeout_seconds,
                "validation_result": record.validation_result,
            },
        }

    @staticmethod
    def _validate_replay(
        record: ArtifactRecord,
        projected_input: dict[str, Any],
        step: StepDefinition,
        attempt: int,
    ) -> None:
        if (
            record.step_id != step.step_id
            or record.attempt != attempt
            or record.executor_kind != step.executor_kind
            or record.timeout_seconds != step.timeout_seconds
            or record.input_sha256 != canonical_json_sha256(projected_input)
        ):
            raise ArtifactStoreError(
                "ARTIFACT_CONFLICT",
                f"artifact replay metadata conflicts: {record.artifact_id}",
            )

    def _after_artifact_write(self, _record: ArtifactRecord) -> None:
        """Testing seam for process failure after the external commit."""
