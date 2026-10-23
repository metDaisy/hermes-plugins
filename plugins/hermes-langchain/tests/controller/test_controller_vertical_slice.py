"""Executable acceptance tests for the controller-owned LangGraph slice."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.support import load_module

CONTROLLER = load_module("controller")


class RecordingExecutor:
    def __init__(self, outputs: dict[str, dict[str, object]]) -> None:
        self.outputs = outputs
        self.invocations: list[object] = []

    def invoke(self, invocation: object) -> object:
        self.invocations.append(invocation)
        step_id = getattr(invocation, "step_id")
        return CONTROLLER.AgentResult(
            agent_id=f"agent-{step_id}",
            output=self.outputs[step_id],
        )


class ReusedSessionExecutor(RecordingExecutor):
    def invoke(self, invocation: object) -> object:
        self.invocations.append(invocation)
        step_id = getattr(invocation, "step_id")
        return CONTROLLER.AgentResult(
            agent_id="reused-agent",
            output=self.outputs[step_id],
        )


class FailingExecutor:
    def invoke(self, invocation: object) -> object:
        raise RuntimeError(f"launch failed for {getattr(invocation, 'step_id')}")


class SensitiveFailingExecutor:
    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.invocations: list[object] = []

    def invoke(self, invocation: object) -> object:
        self.invocations.append(invocation)
        raise RuntimeError(self.marker)


class InputMutationExecutor:
    def __init__(self) -> None:
        self.invocations: list[object] = []
        self.mutation_error: Exception | None = None

    def invoke(self, invocation: object) -> object:
        self.invocations.append(invocation)
        try:
            getattr(invocation, "input")["request"] = "tampered"
        except Exception as exc:
            self.mutation_error = exc
        return CONTROLLER.AgentResult(
            agent_id="agent-immutable-input",
            output={"requirement": "Accepted."},
        )


def two_step_workflow() -> object:
    return CONTROLLER.WorkflowDefinition(
        steps=(
            CONTROLLER.StepDefinition(
                step_id="extract",
                goal="Extract the accepted requirement.",
                allowed_toolsets=("file",),
                acceptance_criteria=("Return one requirement string.",),
                output_schema={
                    "type": "object",
                    "properties": {"requirement": {"type": "string"}},
                    "required": ["requirement"],
                    "additionalProperties": False,
                },
                projector=lambda initial, artifacts: {"request": initial["request"]},
                validator=lambda output: "requirement" in output,
            ),
            CONTROLLER.StepDefinition(
                step_id="review",
                goal="Review only the accepted requirement artifact.",
                allowed_toolsets=("safe",),
                acceptance_criteria=("Decision is approved or rejected.",),
                output_schema={
                    "type": "object",
                    "properties": {
                        "decision": {"type": "string", "enum": ["approved", "rejected"]}
                    },
                    "required": ["decision"],
                    "additionalProperties": False,
                },
                projector=lambda initial, artifacts: {
                    "requirement": artifacts["extract"]["payload"]["requirement"]
                },
                validator=lambda output: output.get("decision") in {"approved", "rejected"},
            ),
        ),
        transitions=(CONTROLLER.Transition("extract", "review"),),
        initial_step_id="extract",
    )


class ControllerVerticalSliceTests(unittest.TestCase):
    def test_runtime_resolves_declared_executor_from_registry(self) -> None:
        executor = RecordingExecutor(
            {"deterministic-step": {"result": "accepted"}}
        )
        workflow = CONTROLLER.WorkflowDefinition(
            steps=(
                CONTROLLER.StepDefinition(
                    step_id="deterministic-step",
                    goal="Run one deterministic step.",
                    allowed_toolsets=("safe",),
                    acceptance_criteria=("Return the accepted result.",),
                    output_schema={"type": "object"},
                    projector=lambda initial, artifacts: {"value": initial["value"]},
                    validator=lambda output: output == {"result": "accepted"},
                    executor_kind="deterministic",
                    timeout_seconds=45,
                ),
            ),
            transitions=(),
            initial_step_id="deterministic-step",
        )
        registry = CONTROLLER.ExecutorRegistry({"deterministic": executor})

        with tempfile.TemporaryDirectory() as tmp:
            runtime = CONTROLLER.ControllerRuntime.open(
                workflow=workflow,
                executor_registry=registry,
                checkpoint_path=Path(tmp) / "controller.db",
            )
            try:
                completed = runtime.start(
                    run_id="run-deterministic",
                    initial_input={"value": "input"},
                )
            finally:
                runtime.close()

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(len(executor.invocations), 1)
        invocation = executor.invocations[0]
        self.assertEqual(getattr(invocation, "executor_kind"), "deterministic")
        self.assertEqual(getattr(invocation, "attempt"), 1)
        self.assertEqual(getattr(invocation, "timeout_seconds"), 45)

    def test_machine_controls_two_step_transition(self) -> None:
        executor = RecordingExecutor(
            {
                "extract": {"requirement": "Ship deterministic orchestration."},
                "review": {"decision": "approved"},
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            runtime = CONTROLLER.ControllerRuntime.open(
                workflow=two_step_workflow(),
                executor=executor,
                checkpoint_path=Path(tmp) / "controller.db",
            )
            try:
                first = runtime.start(
                    run_id="run-1",
                    initial_input={"request": "Build it", "unrelated": "must not leak"},
                )
                completed = runtime.resume(run_id="run-1")
            finally:
                runtime.close()

        self.assertEqual(first["status"], "awaiting_resume")
        self.assertEqual(first["next_step"], "review")
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(
            [getattr(invocation, "step_id") for invocation in executor.invocations],
            ["extract", "review"],
        )
        self.assertEqual(
            [artifact["step_id"] for artifact in completed["artifacts"]],
            ["extract", "review"],
        )

    def test_runtime_uses_explicit_topology_instead_of_step_tuple_order(self) -> None:
        ordered = two_step_workflow()
        workflow = CONTROLLER.WorkflowDefinition(
            steps=tuple(reversed(ordered.steps)),
            transitions=ordered.transitions,
            initial_step_id=ordered.initial_step_id,
        )
        executor = RecordingExecutor(
            {
                "extract": {"requirement": "Ship deterministic orchestration."},
                "review": {"decision": "approved"},
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            runtime = CONTROLLER.ControllerRuntime.open(
                workflow=workflow,
                executor=executor,
                checkpoint_path=Path(tmp) / "controller.db",
            )
            try:
                runtime.start(run_id="run-topology", initial_input={"request": "Build it"})
                completed = runtime.resume(run_id="run-topology")
            finally:
                runtime.close()

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(
            [getattr(invocation, "step_id") for invocation in executor.invocations],
            ["extract", "review"],
        )

    def test_blocked_frontier_ends_without_entering_resume_interrupt(self) -> None:
        initial = CONTROLLER.StepDefinition(
            step_id="initial",
            goal="Produce the initial result.",
            allowed_toolsets=("safe",),
            acceptance_criteria=("Initial result is accepted.",),
            output_schema={"type": "object"},
            projector=lambda source, _: {"request": source["request"]},
            validator=lambda output: output == {"result": "accepted"},
        )
        left = CONTROLLER.StepDefinition(
            step_id="left",
            goal="Ambiguous left branch.",
            allowed_toolsets=("safe",),
            acceptance_criteria=("Left completes.",),
            output_schema={"type": "object"},
            projector=lambda source, artifacts: {},
            validator=lambda output: True,
        )
        right = CONTROLLER.StepDefinition(
            step_id="right",
            goal="Ambiguous right branch.",
            allowed_toolsets=("safe",),
            acceptance_criteria=("Right completes.",),
            output_schema={"type": "object"},
            projector=lambda source, artifacts: {},
            validator=lambda output: True,
        )
        workflow = CONTROLLER.WorkflowDefinition(
            steps=(initial, left, right),
            transitions=(
                CONTROLLER.Transition("initial", "left"),
                CONTROLLER.Transition("initial", "right"),
            ),
            initial_step_id="initial",
        )
        executor = RecordingExecutor({"initial": {"result": "accepted"}})

        with tempfile.TemporaryDirectory() as tmp:
            runtime = CONTROLLER.ControllerRuntime.open(
                workflow=workflow,
                executor=executor,
                checkpoint_path=Path(tmp) / "controller.db",
            )
            try:
                blocked = runtime.start(
                    run_id="run-ambiguous-frontier",
                    initial_input={"request": "Build it"},
                )
                read_back = runtime.resume(run_id="run-ambiguous-frontier")
            finally:
                runtime.close()

        self.assertEqual(blocked, read_back)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(
            blocked["error"],
            "workflow contract failed: AMBIGUOUS_RUNNABLE_FRONTIER",
        )
        self.assertEqual(len(executor.invocations), 1)

    def test_next_step_receives_only_declared_artifact_projection(self) -> None:
        executor = RecordingExecutor(
            {
                "extract": {"requirement": "Ship deterministic orchestration."},
                "review": {"decision": "approved"},
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            runtime = CONTROLLER.ControllerRuntime.open(
                workflow=two_step_workflow(),
                executor=executor,
                checkpoint_path=Path(tmp) / "controller.db",
            )
            try:
                runtime.start(
                    run_id="run-projection",
                    initial_input={"request": "Build it", "unrelated": "must not leak"},
                )
                completed = runtime.resume(run_id="run-projection")
            finally:
                runtime.close()

        extract, review = executor.invocations
        self.assertEqual(getattr(extract, "input"), {"request": "Build it"})
        self.assertEqual(
            getattr(review, "input"),
            {"requirement": "Ship deterministic orchestration."},
        )
        self.assertEqual(getattr(extract, "allowed_toolsets"), ("file",))
        self.assertEqual(getattr(review, "allowed_toolsets"), ("safe",))
        self.assertEqual(
            [item["agent_id"] for item in completed["invocations"]],
            ["agent-extract", "agent-review"],
        )

    def test_reused_agent_session_fails_closed(self) -> None:
        executor = ReusedSessionExecutor(
            {
                "extract": {"requirement": "Ship deterministic orchestration."},
                "review": {"decision": "approved"},
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            artifact_store_path = Path(tmp) / "artifacts.db"
            runtime = CONTROLLER.ControllerRuntime.open(
                workflow=two_step_workflow(),
                executor=executor,
                checkpoint_path=Path(tmp) / "controller.db",
                artifact_store_path=artifact_store_path,
            )
            try:
                runtime.start(run_id="run-session", initial_input={"request": "Build it"})
                blocked = runtime.resume(run_id="run-session")
            finally:
                runtime.close()
            artifact_store = CONTROLLER.SQLiteArtifactStore.open(artifact_store_path)
            try:
                with self.assertRaises(CONTROLLER.ArtifactStoreError) as raised:
                    artifact_store.get("run-session:review:1")
            finally:
                artifact_store.close()

        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["error"], "agent execution must be fresh: reused-agent")
        self.assertEqual(
            [artifact["step_id"] for artifact in blocked["artifacts"]],
            ["extract"],
        )
        self.assertEqual(raised.exception.code, "ARTIFACT_NOT_FOUND")

    def test_checkpointed_agent_task_is_not_reinvoked_after_node_retry(self) -> None:
        class FailAfterTaskOnceRuntime(CONTROLLER.ControllerRuntime):
            should_fail = True

            def _candidate_from_task_result(self, task_result: dict[str, object]) -> dict[str, object]:
                candidate = super()._candidate_from_task_result(task_result)
                if self.should_fail:
                    self.should_fail = False
                    raise RuntimeError("fail after checkpointed task")
                return candidate

        executor = RecordingExecutor(
            {"extract": {"requirement": "Ship deterministic orchestration."}}
        )
        with tempfile.TemporaryDirectory() as tmp:
            runtime = FailAfterTaskOnceRuntime.open(
                workflow=CONTROLLER.WorkflowDefinition(
                    steps=(two_step_workflow().step("extract"),),
                    transitions=(),
                    initial_step_id="extract",
                ),
                executor=executor,
                checkpoint_path=Path(tmp) / "controller.db",
            )
            try:
                with self.assertRaisesRegex(RuntimeError, "fail after checkpointed task"):
                    runtime.start(
                        run_id="run-task-replay",
                        initial_input={"request": "Build it"},
                    )
                completed = runtime.resume(run_id="run-task-replay")
            finally:
                runtime.close()

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(
            [getattr(item, "step_id") for item in executor.invocations],
            ["extract"],
        )

    def test_checkpointed_task_sanitizes_executor_failure_before_replay(self) -> None:
        class FailAfterTaskOnceRuntime(CONTROLLER.ControllerRuntime):
            should_fail = True

            def _candidate_from_task_result(self, task_result: dict[str, object]) -> dict[str, object]:
                candidate = super()._candidate_from_task_result(task_result)
                if self.should_fail:
                    self.should_fail = False
                    raise RuntimeError("fail after checkpointed task")
                return candidate

        marker = "PRIVATE_DIAGNOSTIC_TOKEN_937461"
        executor = SensitiveFailingExecutor(marker)
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint_path = Path(tmp) / "controller.db"
            runtime = FailAfterTaskOnceRuntime.open(
                workflow=CONTROLLER.WorkflowDefinition(
                    steps=(two_step_workflow().step("extract"),),
                    transitions=(),
                    initial_step_id="extract",
                ),
                executor=executor,
                checkpoint_path=checkpoint_path,
            )
            try:
                with self.assertRaisesRegex(RuntimeError, "fail after checkpointed task"):
                    runtime.start(
                        run_id="run-private-failure",
                        initial_input={"request": "Build it"},
                    )
                blocked = runtime.resume(run_id="run-private-failure")
            finally:
                runtime.close()
            database_bytes = checkpoint_path.read_bytes()

        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(
            blocked["error"],
            "agent execution failed for step extract: RuntimeError",
        )
        self.assertEqual(len(executor.invocations), 1)
        self.assertNotIn(marker.encode("utf-8"), database_bytes)

    def test_invalid_output_is_not_written_to_checkpoint_database(self) -> None:
        marker = "OVERSIZED_AGENT_OUTPUT_MARKER_581203"
        step = CONTROLLER.StepDefinition(
            step_id="bounded",
            goal="Return a bounded requirement.",
            allowed_toolsets=("safe",),
            acceptance_criteria=("The requirement has at most 32 characters.",),
            output_schema={
                "type": "object",
                "properties": {
                    "requirement": {"type": "string", "maxLength": 32},
                },
                "required": ["requirement"],
                "additionalProperties": False,
            },
            projector=lambda initial, _: {"request": initial["request"]},
            validator=lambda output: (
                set(output) == {"requirement"}
                and isinstance(output["requirement"], str)
                and len(output["requirement"]) <= 32
            ),
        )
        executor = RecordingExecutor(
            {"bounded": {"requirement": marker + ("x" * 8001)}}
        )
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint_path = Path(tmp) / "controller.db"
            runtime = CONTROLLER.ControllerRuntime.open(
                workflow=CONTROLLER.WorkflowDefinition(
                    steps=(step,),
                    transitions=(),
                    initial_step_id=step.step_id,
                ),
                executor=executor,
                checkpoint_path=checkpoint_path,
            )
            try:
                blocked = runtime.start(
                    run_id="run-invalid-output-checkpoint",
                    initial_input={"request": "Build it"},
                )
            finally:
                runtime.close()
            database_bytes = checkpoint_path.read_bytes()

        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["error"], "invalid output for step: bounded")
        self.assertNotIn(marker.encode("utf-8"), database_bytes)

    def test_checkpoint_stores_only_artifact_refs_and_rehydrates_payload(self) -> None:
        marker = "ACCEPTED_ARTIFACT_PAYLOAD_764291"
        executor = RecordingExecutor(
            {
                "extract": {"requirement": marker},
                "review": {"decision": "approved"},
            }
        )

        with tempfile.TemporaryDirectory() as tmp:
            checkpoint_path = Path(tmp) / "controller.db"
            artifact_store_path = Path(tmp) / "artifacts.db"
            runtime = CONTROLLER.ControllerRuntime.open(
                workflow=two_step_workflow(),
                executor=executor,
                checkpoint_path=checkpoint_path,
                artifact_store_path=artifact_store_path,
            )
            try:
                runtime.start(
                    run_id="run-artifact-owner",
                    initial_input={"request": "Build it"},
                )
                completed = runtime.resume(run_id="run-artifact-owner")
                read_back = runtime.snapshot(run_id="run-artifact-owner")
            finally:
                runtime.close()

            checkpoint_bytes = checkpoint_path.read_bytes()
            artifact_store = CONTROLLER.SQLiteArtifactStore.open(artifact_store_path)
            try:
                stored = artifact_store.get("run-artifact-owner:extract:1")
            finally:
                artifact_store.close()

        self.assertEqual(completed, read_back)
        self.assertEqual(
            completed["artifacts"][0]["payload"],
            {"requirement": marker},
        )
        self.assertEqual(stored.payload, {"requirement": marker})
        self.assertNotIn(marker.encode("utf-8"), checkpoint_bytes)

    def test_artifact_write_replay_is_idempotent_without_executor_reinvocation(self) -> None:
        class CrashAfterArtifactWriteRuntime(CONTROLLER.ControllerRuntime):
            should_crash = True

            def _after_artifact_write(self, record: object) -> None:
                if self.should_crash:
                    self.should_crash = False
                    raise RuntimeError("crash after artifact write")

        step = two_step_workflow().step("extract")
        workflow = CONTROLLER.WorkflowDefinition(
            steps=(step,),
            transitions=(),
            initial_step_id=step.step_id,
        )
        first_executor = RecordingExecutor(
            {"extract": {"requirement": "Persist exactly once."}}
        )
        replay_executor = RecordingExecutor(
            {"extract": {"requirement": "must not run"}}
        )

        with tempfile.TemporaryDirectory() as tmp:
            checkpoint_path = Path(tmp) / "controller.db"
            artifact_store_path = Path(tmp) / "artifacts.db"
            first_runtime = CrashAfterArtifactWriteRuntime.open(
                workflow=workflow,
                executor=first_executor,
                checkpoint_path=checkpoint_path,
                artifact_store_path=artifact_store_path,
            )
            try:
                with self.assertRaisesRegex(RuntimeError, "crash after artifact write"):
                    first_runtime.start(
                        run_id="run-artifact-replay",
                        initial_input={"request": "Build it"},
                    )
            finally:
                first_runtime.close()

            reconstructed = CONTROLLER.ControllerRuntime.open(
                workflow=workflow,
                executor=replay_executor,
                checkpoint_path=checkpoint_path,
                artifact_store_path=artifact_store_path,
            )
            try:
                completed = reconstructed.resume(run_id="run-artifact-replay")
            finally:
                reconstructed.close()

            connection = sqlite3.connect(artifact_store_path)
            try:
                artifact_count = connection.execute(
                    "SELECT COUNT(*) FROM controller_artifacts"
                ).fetchone()[0]
            finally:
                connection.close()

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(
            completed["artifacts"][0]["payload"],
            {"requirement": "Persist exactly once."},
        )
        self.assertEqual(len(first_executor.invocations), 1)
        self.assertEqual(replay_executor.invocations, [])
        self.assertEqual(artifact_count, 1)

    def test_validator_runs_once_inside_sanitized_task_boundary(self) -> None:
        marker = "PRIVATE_VALIDATOR_TOKEN_443821"
        validation_calls = 0

        def validate_once(output: dict[str, object]) -> bool:
            nonlocal validation_calls
            validation_calls += 1
            if validation_calls > 1:
                raise RuntimeError(marker)
            return output == {"requirement": "Accepted."}

        step = CONTROLLER.StepDefinition(
            step_id="validate-once",
            goal="Return one accepted requirement.",
            allowed_toolsets=("safe",),
            acceptance_criteria=("The requirement is accepted.",),
            output_schema={
                "type": "object",
                "properties": {"requirement": {"type": "string"}},
                "required": ["requirement"],
                "additionalProperties": False,
            },
            projector=lambda initial, _: {"request": initial["request"]},
            validator=validate_once,
        )
        executor = RecordingExecutor(
            {"validate-once": {"requirement": "Accepted."}}
        )
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint_path = Path(tmp) / "controller.db"
            runtime = CONTROLLER.ControllerRuntime.open(
                workflow=CONTROLLER.WorkflowDefinition(
                    steps=(step,),
                    transitions=(),
                    initial_step_id=step.step_id,
                ),
                executor=executor,
                checkpoint_path=checkpoint_path,
            )
            try:
                completed = runtime.start(
                    run_id="run-validator-once",
                    initial_input={"request": "Build it"},
                )
            finally:
                runtime.close()
            database_bytes = checkpoint_path.read_bytes()

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(validation_calls, 1)
        self.assertNotIn(marker.encode("utf-8"), database_bytes)

    def test_validator_exception_is_sanitized_without_executor_reinvocation(self) -> None:
        marker = "PRIVATE_VALIDATOR_EXCEPTION_901734"

        def failing_validator(_output: dict[str, object]) -> bool:
            raise RuntimeError(marker)

        step = CONTROLLER.StepDefinition(
            step_id="validator-failure",
            goal="Validate without leaking diagnostics.",
            allowed_toolsets=("safe",),
            acceptance_criteria=("Validation fails closed.",),
            output_schema={"type": "object"},
            projector=lambda initial, _: {"request": initial["request"]},
            validator=failing_validator,
        )
        executor = RecordingExecutor(
            {"validator-failure": {"requirement": "Accepted."}}
        )
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint_path = Path(tmp) / "controller.db"
            runtime = CONTROLLER.ControllerRuntime.open(
                workflow=CONTROLLER.WorkflowDefinition(
                    steps=(step,),
                    transitions=(),
                    initial_step_id=step.step_id,
                ),
                executor=executor,
                checkpoint_path=checkpoint_path,
            )
            try:
                blocked = runtime.start(
                    run_id="run-validator-failure",
                    initial_input={"request": "Build it"},
                )
                read_back = runtime.resume(run_id="run-validator-failure")
            finally:
                runtime.close()
            checkpoint_bytes = checkpoint_path.read_bytes()

        self.assertEqual(blocked, read_back)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(
            blocked["error"],
            "agent execution failed for step validator-failure: RuntimeError",
        )
        self.assertEqual(len(executor.invocations), 1)
        self.assertNotIn(marker.encode("utf-8"), checkpoint_bytes)

    def test_executor_cannot_mutate_projected_input_before_digest(self) -> None:
        executor = InputMutationExecutor()
        step = two_step_workflow().step("extract")
        projected_input = {"request": "Build it"}
        with tempfile.TemporaryDirectory() as tmp:
            artifact_store_path = Path(tmp) / "artifacts.db"
            runtime = CONTROLLER.ControllerRuntime.open(
                workflow=CONTROLLER.WorkflowDefinition(
                    steps=(step,),
                    transitions=(),
                    initial_step_id=step.step_id,
                ),
                executor=executor,
                checkpoint_path=Path(tmp) / "controller.db",
                artifact_store_path=artifact_store_path,
            )
            try:
                completed = runtime.start(
                    run_id="run-immutable-input",
                    initial_input=projected_input,
                )
            finally:
                runtime.close()
            store = CONTROLLER.SQLiteArtifactStore.open(artifact_store_path)
            try:
                artifact = store.get("run-immutable-input:extract:1")
            finally:
                store.close()

        self.assertEqual(completed["status"], "completed")
        self.assertIsInstance(executor.mutation_error, TypeError)
        self.assertEqual(
            artifact.input_sha256,
            CONTROLLER.canonical_json_sha256(projected_input),
        )

    def test_reconstructed_runtime_resumes_without_duplicate_invocation(self) -> None:
        first_executor = RecordingExecutor(
            {"extract": {"requirement": "Ship deterministic orchestration."}}
        )
        second_executor = RecordingExecutor(
            {"review": {"decision": "approved"}}
        )
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint_path = Path(tmp) / "controller.db"
            first_runtime = CONTROLLER.ControllerRuntime.open(
                workflow=two_step_workflow(),
                executor=first_executor,
                checkpoint_path=checkpoint_path,
            )
            try:
                paused = first_runtime.start(
                    run_id="run-restart",
                    initial_input={"request": "Build it"},
                )
            finally:
                first_runtime.close()

            second_runtime = CONTROLLER.ControllerRuntime.open(
                workflow=two_step_workflow(),
                executor=second_executor,
                checkpoint_path=checkpoint_path,
            )
            try:
                resumed = second_runtime.resume(run_id="run-restart")
            finally:
                second_runtime.close()

        self.assertEqual(paused["status"], "awaiting_resume")
        self.assertEqual(
            [getattr(item, "step_id") for item in first_executor.invocations],
            ["extract"],
        )
        self.assertEqual(
            [getattr(item, "step_id") for item in second_executor.invocations],
            ["review"],
        )
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(
            [item["invocation_id"] for item in resumed["invocations"]],
            ["run-restart:extract:1", "run-restart:review:1"],
        )

    def test_invalid_output_fails_closed_before_transition(self) -> None:
        executor = RecordingExecutor({"extract": {"unexpected": "value"}})
        with tempfile.TemporaryDirectory() as tmp:
            runtime = CONTROLLER.ControllerRuntime.open(
                workflow=two_step_workflow(),
                executor=executor,
                checkpoint_path=Path(tmp) / "controller.db",
            )
            try:
                blocked = runtime.start(
                    run_id="run-invalid",
                    initial_input={"request": "Build it"},
                )
            finally:
                runtime.close()

        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["next_step"], None)
        self.assertEqual(blocked["error"], "invalid output for step: extract")
        self.assertEqual(blocked["artifacts"], [])
        self.assertEqual(
            [getattr(item, "step_id") for item in executor.invocations],
            ["extract"],
        )

    def test_executor_failure_is_persisted_as_blocked_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint_path = Path(tmp) / "controller.db"
            runtime = CONTROLLER.ControllerRuntime.open(
                workflow=two_step_workflow(),
                executor=FailingExecutor(),
                checkpoint_path=checkpoint_path,
            )
            try:
                blocked = runtime.start(
                    run_id="run-executor-failure",
                    initial_input={"request": "Build it"},
                )
            finally:
                runtime.close()

            reconstructed = CONTROLLER.ControllerRuntime.open(
                workflow=two_step_workflow(),
                executor=FailingExecutor(),
                checkpoint_path=checkpoint_path,
            )
            try:
                read_back = reconstructed.snapshot(run_id="run-executor-failure")
            finally:
                reconstructed.close()

        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(
            blocked["error"],
            "agent execution failed for step extract: RuntimeError",
        )
        self.assertEqual(blocked["artifacts"], [])
        self.assertEqual(read_back, blocked)

    def test_duplicate_start_is_rejected_before_agent_reinvocation(self) -> None:
        executor = RecordingExecutor(
            {"extract": {"requirement": "Ship deterministic orchestration."}}
        )
        with tempfile.TemporaryDirectory() as tmp:
            runtime = CONTROLLER.ControllerRuntime.open(
                workflow=two_step_workflow(),
                executor=executor,
                checkpoint_path=Path(tmp) / "controller.db",
            )
            try:
                runtime.start(
                    run_id="run-duplicate",
                    initial_input={"request": "Build it"},
                )
                with self.assertRaisesRegex(ValueError, "run already exists: run-duplicate"):
                    runtime.start(
                        run_id="run-duplicate",
                        initial_input={"request": "Build it again"},
                    )
            finally:
                runtime.close()

        self.assertEqual(
            [getattr(item, "step_id") for item in executor.invocations],
            ["extract"],
        )


if __name__ == "__main__":
    unittest.main()
