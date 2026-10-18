"""Tests for the pure KWC workflow decision engine."""

from __future__ import annotations

import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
from tests.support import load_module

ENGINE = load_module("kanban.workflow_engine")


class WorkflowEngineTests(unittest.TestCase):
    def task(self, **overrides):
        value = {
            "task_id": "t_impl",
            "status": "ready",
            "assignee": "coder",
            "body_schema": "backend-implementation-card-v5",
            "parents": [],
            "active_run_profile": None,
            "active_run_source_status": None,
            "latest_run_profile": None,
            "latest_run_id": None,
            "latest_run_status": None,
            "latest_run_metadata_schema": None,
            "latest_run_result": None,
            "block_kind": None,
            "skills": [],
        }
        value.update(overrides)
        return value

    def snapshot(self, *tasks):
        return {
            "schema": "kwc-workflow-snapshot-v1",
            "board": "issue151",
            "tasks": list(tasks),
        }

    def test_ready_implementation_dispatches_only_the_coder_contract(self) -> None:
        decision = ENGINE.decide(self.snapshot(self.task()))

        self.assertTrue(decision["valid"])
        self.assertEqual("ready", decision["phase"])
        self.assertEqual(
            {
                "kind": "dispatch-task",
                "task_id": "t_impl",
                "profile": "coder",
                "skill": "run-impl-card",
                "reason": "ready_implementation",
            },
            decision["action"],
        )
        self.assertEqual([], decision["errors"])

    def test_ready_task_rejects_a_forced_skill_outside_its_profile_contract(self) -> None:
        implementation = self.task(skills=["run-review"])

        decision = ENGINE.decide(self.snapshot(implementation))

        self.assertFalse(decision["valid"])
        self.assertIsNone(decision["action"])
        self.assertEqual(["TASK_SKILL_MISMATCH:t_impl"], decision["errors"])

    def test_ready_aggregate_review_dispatches_only_the_reviewer_contract(self) -> None:
        review = self.task(
            task_id="t_review",
            assignee="reviewer",
            body_schema="aggregate-review-card-v5",
        )

        decision = ENGINE.decide(self.snapshot(review))

        self.assertTrue(decision["valid"])
        self.assertEqual(
            {
                "kind": "dispatch-task",
                "task_id": "t_review",
                "profile": "reviewer",
                "skill": "run-review",
                "reason": "ready_aggregate_review",
            },
            decision["action"],
        )
    def test_implementation_review_queue_dispatches_the_pm_checkpoint(self) -> None:
        implementation = self.task(
            status="review",
            assignee="project-manager",
            latest_run_profile="coder",
            latest_run_status="review_requested",
        )

        decision = ENGINE.decide(self.snapshot(implementation))

        self.assertTrue(decision["valid"])
        self.assertEqual("checkpoint", decision["phase"])
        self.assertEqual(
            {
                "kind": "dispatch-task",
                "task_id": "t_impl",
                "profile": "project-manager",
                "skill": "run-workflow",
                "reason": "implementation_checkpoint",
            },
            decision["action"],
        )
    def test_running_implementation_waits_for_the_coder_without_dispatching_again(self) -> None:
        implementation = self.task(
            status="running",
            active_run_profile="coder",
            active_run_source_status="ready",
        )

        decision = ENGINE.decide(self.snapshot(implementation))

        self.assertTrue(decision["valid"])
        self.assertEqual("active", decision["phase"])
        self.assertEqual(
            {
                "kind": "wait-for-profile",
                "task_id": "t_impl",
                "profile": "coder",
                "skill": "run-impl-card",
                "reason": "active_implementation",
            },
            decision["action"],
        )

    def test_running_aggregate_review_waits_for_reviewer(self) -> None:
        review = self.task(
            task_id="t_review",
            status="running",
            assignee="reviewer",
            body_schema="aggregate-review-card-v5",
            active_run_profile="reviewer",
            active_run_source_status="ready",
        )

        decision = ENGINE.decide(self.snapshot(review))

        self.assertTrue(decision["valid"])
        self.assertEqual("active", decision["phase"])
        self.assertEqual(
            {
                "kind": "wait-for-profile",
                "task_id": "t_review",
                "profile": "reviewer",
                "skill": "run-review",
                "reason": "active_aggregate_review",
            },
            decision["action"],
        )

    def test_running_pm_checkpoint_waits_for_project_manager(self) -> None:
        checkpoint = self.task(
            status="running",
            assignee="project-manager",
            active_run_profile="project-manager",
            active_run_source_status="review",
        )

        decision = ENGINE.decide(self.snapshot(checkpoint))

        self.assertTrue(decision["valid"])
        self.assertEqual("active", decision["phase"])
        self.assertEqual(
            {
                "kind": "wait-for-profile",
                "task_id": "t_impl",
                "profile": "project-manager",
                "skill": "run-workflow",
                "reason": "active_implementation_checkpoint",
            },
            decision["action"],
        )

    def test_blocked_worker_task_wakes_the_project_manager(self) -> None:
        implementation = self.task(
            status="blocked",
            latest_run_profile="coder",
            latest_run_status="blocked",
            block_kind="needs_input",
        )

        decision = ENGINE.decide(self.snapshot(implementation))

        self.assertTrue(decision["valid"])
        self.assertEqual("blocked", decision["phase"])
        self.assertEqual(
            {
                "kind": "wake-profile",
                "task_id": "t_impl",
                "profile": "project-manager",
                "skill": "run-workflow",
                "reason": "worker_needs_input",
            },
            decision["action"],
        )
    def test_changes_required_review_wakes_pm_before_summary_progression(self) -> None:
        review = self.task(
            task_id="t_review",
            status="done",
            assignee="reviewer",
            body_schema="aggregate-review-card-v5",
            latest_run_profile="reviewer",
            latest_run_status="completed",
            latest_run_metadata_schema="aggregate-review-result-v2",
            latest_run_result="changes-required",
        )
        summary = self.task(
            task_id="t_summary",
            status="todo",
            assignee="project-manager",
            body_schema="issue-summary-card-v3",
            parents=["t_review"],
        )

        decision = ENGINE.decide(self.snapshot(review, summary))

        self.assertTrue(decision["valid"])
        self.assertEqual("review-findings", decision["phase"])
        self.assertEqual(
            {
                "kind": "wake-profile",
                "task_id": "t_review",
                "profile": "project-manager",
                "skill": "run-workflow",
                "reason": "route_review_findings",
            },
            decision["action"],
        )
    def test_routed_review_findings_do_not_mask_a_corrective_ready_task(self) -> None:
        review = self.task(
            task_id="t_review",
            status="done",
            assignee="reviewer",
            body_schema="aggregate-review-card-v5",
            latest_run_profile="reviewer",
            latest_run_id=10,
            latest_run_status="completed",
            latest_run_metadata_schema="aggregate-review-result-v2",
            latest_run_result="changes-required",
        )
        correction = self.task(
            task_id="t_correction",
            status="ready",
            parents=["t_review"],
        )
        summary = self.task(
            task_id="t_summary",
            status="todo",
            assignee="project-manager",
            body_schema="issue-summary-card-v3",
            parents=["t_review"],
        )

        decision = ENGINE.decide(self.snapshot(review, correction, summary))

        self.assertTrue(decision["valid"])
        self.assertEqual("ready", decision["phase"])
        self.assertEqual("t_correction", decision["action"]["task_id"])
        self.assertEqual("ready_implementation", decision["action"]["reason"])

    def test_todo_implementation_with_done_parents_is_the_unique_promotion(self) -> None:
        implementation = self.task(status="todo")

        decision = ENGINE.decide(self.snapshot(implementation))

        self.assertTrue(decision["valid"])
        self.assertEqual("promotion", decision["phase"])
        self.assertEqual(
            {
                "kind": "promote-task",
                "task_id": "t_impl",
                "profile": "coder",
                "skill": "run-impl-card",
                "reason": "eligible_implementation",
            },
            decision["action"],
        )

    def test_todo_task_rejects_forced_skill_before_promotion(self) -> None:
        implementation = self.task(status="todo", skills=["run-review"])

        decision = ENGINE.decide(self.snapshot(implementation))

        self.assertFalse(decision["valid"])
        self.assertIsNone(decision["action"])
        self.assertEqual(["TASK_SKILL_MISMATCH:t_impl"], decision["errors"])

    def test_multiple_eligible_todo_tasks_fail_closed(self) -> None:
        first = self.task(status="todo")
        second = self.task(task_id="t_impl_2", status="todo")

        decision = ENGINE.decide(self.snapshot(first, second))

        self.assertFalse(decision["valid"])
        self.assertIsNone(decision["action"])
        self.assertEqual(["MULTIPLE_ELIGIBLE_TASKS"], decision["errors"])

    def test_latest_review_is_selected_by_run_id_not_task_list_order(self) -> None:
        implementation = self.task(
            status="done",
            assignee="project-manager",
            latest_run_profile="project-manager",
            latest_run_id=4,
            latest_run_status="completed",
        )
        latest_review = self.task(
            task_id="t_review_latest",
            status="done",
            assignee="reviewer",
            body_schema="aggregate-review-card-v5",
            latest_run_profile="reviewer",
            latest_run_id=10,
            latest_run_status="completed",
            latest_run_metadata_schema="aggregate-review-result-v2",
            latest_run_result="approved",
        )
        older_review = self.task(
            task_id="t_review_older",
            status="done",
            assignee="reviewer",
            body_schema="aggregate-review-card-v5",
            latest_run_profile="reviewer",
            latest_run_id=5,
            latest_run_status="completed",
            latest_run_metadata_schema="aggregate-review-result-v2",
            latest_run_result="changes-required",
        )
        summary = self.task(
            task_id="t_summary",
            status="todo",
            assignee="project-manager",
            body_schema="issue-summary-card-v3",
            parents=["t_impl", "t_review_latest", "t_review_older"],
        )

        decision = ENGINE.decide(
            self.snapshot(implementation, latest_review, older_review, summary)
        )

        self.assertTrue(decision["valid"])
        self.assertEqual("promotion", decision["phase"])
        self.assertEqual("approved_review_summary", decision["action"]["reason"])

    def test_all_terminal_tasks_produce_complete_without_an_action(self) -> None:
        implementation = self.task(
            status="done",
            assignee="project-manager",
            latest_run_profile="project-manager",
            latest_run_id=4,
            latest_run_status="completed",
        )
        review = self.task(
            task_id="t_review",
            status="done",
            assignee="reviewer",
            body_schema="aggregate-review-card-v5",
            latest_run_profile="reviewer",
            latest_run_id=5,
            latest_run_status="completed",
            latest_run_metadata_schema="aggregate-review-result-v2",
            latest_run_result="approved",
        )
        summary = self.task(
            task_id="t_summary",
            status="done",
            assignee="project-manager",
            body_schema="issue-summary-card-v3",
            latest_run_profile="project-manager",
            latest_run_id=6,
            latest_run_status="completed",
        )

        decision = ENGINE.decide(self.snapshot(implementation, review, summary))

        self.assertTrue(decision["valid"])
        self.assertEqual("complete", decision["phase"])
        self.assertIsNone(decision["action"])
        self.assertEqual([], decision["errors"])

    def test_approved_review_promotes_the_eligible_summary_to_pm(self) -> None:
        implementation = self.task(
            status="done",
            assignee="project-manager",
            latest_run_profile="project-manager",
            latest_run_status="completed",
            latest_run_metadata_schema="backend-implementation-checkpoint-v1",
            latest_run_result="pass",
        )
        review = self.task(
            task_id="t_review",
            status="done",
            assignee="reviewer",
            body_schema="aggregate-review-card-v5",
            parents=["t_impl"],
            latest_run_profile="reviewer",
            latest_run_status="completed",
            latest_run_metadata_schema="aggregate-review-result-v2",
            latest_run_result="approved",
        )
        summary = self.task(
            task_id="t_summary",
            status="todo",
            assignee="project-manager",
            body_schema="issue-summary-card-v3",
            parents=["t_impl", "t_review"],
        )

        decision = ENGINE.decide(self.snapshot(implementation, review, summary))

        self.assertTrue(decision["valid"])
        self.assertEqual("promotion", decision["phase"])
        self.assertEqual(
            {
                "kind": "promote-task",
                "task_id": "t_summary",
                "profile": "project-manager",
                "skill": "run-workflow",
                "reason": "approved_review_summary",
            },
            decision["action"],
        )


if __name__ == "__main__":
    unittest.main()
