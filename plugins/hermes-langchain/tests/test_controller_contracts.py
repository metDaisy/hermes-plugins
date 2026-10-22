"""Pure contract tests for deterministic controller workflow topology."""

from __future__ import annotations

import unittest

from tests.support import load_module

CONTROLLER = load_module("controller")


def step(
    step_id: str,
    *,
    executor_kind: str = "agent",
    timeout_seconds: int = 300,
) -> object:
    return CONTROLLER.StepDefinition(
        step_id=step_id,
        goal=f"Execute {step_id}.",
        allowed_toolsets=("safe",),
        acceptance_criteria=(f"{step_id} is accepted.",),
        output_schema={"type": "object"},
        projector=lambda initial, artifacts: {},
        validator=lambda output: True,
        executor_kind=executor_kind,
        timeout_seconds=timeout_seconds,
    )


def accepted(step_id: str) -> object:
    return CONTROLLER.StepRun(
        step_id=step_id,
        attempt=1,
        artifact_ref=CONTROLLER.ArtifactRef(
            artifact_id=f"run-contract:{step_id}",
            step_id=step_id,
            sha256="c" * 64,
        ),
    )


class ControllerContractTests(unittest.TestCase):
    def test_rejects_invalid_step_execution_metadata_with_stable_codes(self) -> None:
        cases = (
            ("UNKNOWN_EXECUTOR_KIND", {"executor_kind": "shell"}),
            ("INVALID_TIMEOUT", {"timeout_seconds": 0}),
            ("INVALID_TIMEOUT", {"timeout_seconds": 1.5}),
            ("INVALID_TIMEOUT", {"timeout_seconds": True}),
        )
        for code, arguments in cases:
            with self.subTest(code=code, arguments=arguments):
                with self.assertRaises(CONTROLLER.WorkflowContractError) as raised:
                    step("invalid", **arguments)
                self.assertEqual(raised.exception.code, code)

    def test_selects_single_runnable_step_until_workflow_completion(self) -> None:
        mutable_schema = {
            "type": "object",
            "properties": {"requirement": {"type": "string"}},
        }
        mutable_steps = [
            CONTROLLER.StepDefinition(
                step_id="extract",
                goal="Execute extract.",
                allowed_toolsets=["safe"],
                acceptance_criteria=["extract is accepted."],
                output_schema=mutable_schema,
                projector=lambda initial, artifacts: {},
                validator=lambda output: True,
            ),
            step("review"),
        ]
        mutable_transitions = [CONTROLLER.Transition("extract", "review")]
        workflow = CONTROLLER.WorkflowDefinition(
            steps=mutable_steps,
            transitions=mutable_transitions,
            initial_step_id="extract",
        )
        mutable_schema["properties"]["requirement"]["type"] = "integer"
        mutable_steps.clear()
        mutable_transitions.clear()

        self.assertIsInstance(workflow.steps, tuple)
        self.assertIsInstance(workflow.transitions, tuple)
        self.assertEqual(
            workflow.step("extract").output_schema["properties"]["requirement"]["type"],
            "string",
        )
        with self.assertRaises(TypeError):
            workflow.step("extract").output_schema["type"] = "array"

        initial = CONTROLLER.WorkflowRun(run_id="run-contract")
        self.assertEqual(CONTROLLER.select_next_step(workflow, initial).step_id, "extract")

        extract_artifact = CONTROLLER.ArtifactRef(
            artifact_id="run-contract:extract",
            step_id="extract",
            sha256="a" * 64,
        )
        after_extract = CONTROLLER.WorkflowRun(
            run_id="run-contract",
            step_runs=(
                CONTROLLER.StepRun(
                    step_id="extract",
                    attempt=1,
                    artifact_ref=extract_artifact,
                ),
            ),
        )
        self.assertEqual(
            CONTROLLER.select_next_step(workflow, after_extract).step_id,
            "review",
        )

        review_artifact = CONTROLLER.ArtifactRef(
            artifact_id="run-contract:review",
            step_id="review",
            sha256="b" * 64,
        )
        completed = CONTROLLER.WorkflowRun(
            run_id="run-contract",
            step_runs=(
                *after_extract.step_runs,
                CONTROLLER.StepRun(
                    step_id="review",
                    attempt=1,
                    artifact_ref=review_artifact,
                ),
            ),
        )
        self.assertIsNone(CONTROLLER.select_next_step(workflow, completed))

    def test_rejects_invalid_topologies_with_stable_codes(self) -> None:
        cases = (
            (
                "DUPLICATE_STEP_ID",
                (step("a"), step("a")),
                (),
                "a",
            ),
            (
                "UNKNOWN_TRANSITION_SOURCE",
                (step("a"),),
                (CONTROLLER.Transition("missing", "a"),),
                "a",
            ),
            (
                "UNKNOWN_TRANSITION_TARGET",
                (step("a"),),
                (CONTROLLER.Transition("a", "missing"),),
                "a",
            ),
            (
                "DUPLICATE_TRANSITION",
                (step("a"), step("b")),
                (
                    CONTROLLER.Transition("a", "b"),
                    CONTROLLER.Transition("a", "b"),
                ),
                "a",
            ),
            (
                "WORKFLOW_CYCLE",
                (step("a"), step("b"), step("c")),
                (
                    CONTROLLER.Transition("a", "b"),
                    CONTROLLER.Transition("b", "c"),
                    CONTROLLER.Transition("c", "b"),
                ),
                "a",
            ),
            (
                "UNREACHABLE_STEP",
                (step("a"), step("b"), step("c")),
                (CONTROLLER.Transition("b", "c"),),
                "a",
            ),
            (
                "INVALID_INITIAL_FRONTIER",
                (step("a"), step("b")),
                (CONTROLLER.Transition("b", "a"),),
                "a",
            ),
        )

        for code, steps, transitions, initial_step_id in cases:
            with self.subTest(code=code):
                with self.assertRaises(CONTROLLER.WorkflowContractError) as raised:
                    CONTROLLER.WorkflowDefinition(
                        steps=steps,
                        transitions=transitions,
                        initial_step_id=initial_step_id,
                    )
                self.assertEqual(raised.exception.code, code)

    def test_rejects_invalid_run_frontiers_with_stable_codes(self) -> None:
        linear = CONTROLLER.WorkflowDefinition(
            steps=(step("a"), step("b")),
            transitions=(CONTROLLER.Transition("a", "b"),),
            initial_step_id="a",
        )

        with self.assertRaises(CONTROLLER.WorkflowContractError) as unknown:
            CONTROLLER.select_next_step(
                linear,
                CONTROLLER.WorkflowRun(
                    run_id="run-unknown",
                    step_runs=(accepted("missing"),),
                ),
            )
        self.assertEqual(unknown.exception.code, "UNKNOWN_RUN_STEP")

        with self.assertRaises(CONTROLLER.WorkflowContractError) as inconsistent:
            CONTROLLER.select_next_step(
                linear,
                CONTROLLER.WorkflowRun(
                    run_id="run-inconsistent",
                    step_runs=(accepted("b"),),
                ),
            )
        self.assertEqual(inconsistent.exception.code, "INCONSISTENT_STEP_RUN")

        with self.assertRaises(CONTROLLER.WorkflowContractError) as out_of_order:
            CONTROLLER.select_next_step(
                linear,
                CONTROLLER.WorkflowRun(
                    run_id="run-out-of-order",
                    step_runs=(accepted("b"), accepted("a")),
                ),
            )
        self.assertEqual(out_of_order.exception.code, "INCONSISTENT_STEP_RUN")

        with self.assertRaises(CONTROLLER.WorkflowContractError) as duplicate:
            CONTROLLER.select_next_step(
                linear,
                CONTROLLER.WorkflowRun(
                    run_id="run-duplicate",
                    step_runs=(accepted("a"), accepted("a")),
                ),
            )
        self.assertEqual(duplicate.exception.code, "DUPLICATE_STEP_RUN")

        mismatched_artifact = CONTROLLER.StepRun(
            step_id="a",
            attempt=1,
            artifact_ref=CONTROLLER.ArtifactRef(
                artifact_id="run-mismatch:other",
                step_id="other",
                sha256="d" * 64,
            ),
        )
        with self.assertRaises(CONTROLLER.WorkflowContractError) as mismatch:
            CONTROLLER.select_next_step(
                linear,
                CONTROLLER.WorkflowRun(
                    run_id="run-mismatch",
                    step_runs=(mismatched_artifact,),
                ),
            )
        self.assertEqual(mismatch.exception.code, "ARTIFACT_STEP_MISMATCH")

        branching = CONTROLLER.WorkflowDefinition(
            steps=(step("a"), step("b"), step("c")),
            transitions=(
                CONTROLLER.Transition("a", "b"),
                CONTROLLER.Transition("a", "c"),
            ),
            initial_step_id="a",
        )
        with self.assertRaises(CONTROLLER.WorkflowContractError) as ambiguous:
            CONTROLLER.select_next_step(
                branching,
                CONTROLLER.WorkflowRun(
                    run_id="run-ambiguous",
                    step_runs=(accepted("a"),),
                ),
            )
        self.assertEqual(ambiguous.exception.code, "AMBIGUOUS_RUNNABLE_FRONTIER")


if __name__ == "__main__":
    unittest.main()
