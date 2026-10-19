"""Contract tests for KWC-owned PM card authoring."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
from tests.support import load_module

SERVICE = load_module("authoring.authoring_service")


class AuthoringServiceTests(unittest.TestCase):
    def test_existing_assignment_is_resumed_independent_of_initiating_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.db"
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            assignment = SERVICE.begin_authoring(
                state_path=state,
                board="issue-151",
                issue_number=151,
                generation=1,
                workspace_path=workspace,
                pm_session_key="pm-session",
            )
            resumed = SERVICE.begin_authoring(
                state_path=state,
                board="issue-151",
                issue_number=151,
                generation=1,
                workspace_path=workspace,
                pm_session_key="other-session",
            )

            self.assertEqual(assignment, resumed)

    def test_fresh_project_manager_session_can_advance_durable_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.db"
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            assignment = SERVICE.begin_authoring(
                state_path=state,
                board="issue-151-fresh-session",
                issue_number=151,
                generation=1,
                workspace_path=workspace,
                pm_session_key="initiating-session",
            )

            next_assignment = SERVICE.complete_triage_stage(
                state_path=state,
                assignment_id=assignment["assignment_id"],
                caller_profile="project-manager",
                caller_session_key="dedicated-authoring-session",
                stage="intake",
                artifact={"path": "intake.json", "sha256": "intake"},
            )

            self.assertEqual("discovery", next_assignment["triage_stage"])

    def test_non_project_manager_profile_cannot_advance_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.db"
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            assignment = SERVICE.begin_authoring(
                state_path=state,
                board="issue-151-wrong-profile",
                issue_number=151,
                generation=1,
                workspace_path=workspace,
                pm_session_key="initiating-session",
            )

            with self.assertRaisesRegex(ValueError, "project-manager"):
                SERVICE.complete_triage_stage(
                    state_path=state,
                    assignment_id=assignment["assignment_id"],
                    caller_profile="coder",
                    caller_session_key="coder-session",
                    stage="intake",
                    artifact={"path": "intake.json", "sha256": "intake"},
                )

    def test_begin_returns_fenced_triage_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "repo"
            workspace.mkdir()
            state = Path(tmp) / "state.db"
            assignment = SERVICE.begin_authoring(
                state_path=state,
                board="issue-151-authoring",
                issue_number=151,
                generation=1,
                workspace_path=workspace,
                pm_session_key="pm-session",
            )

            self.assertEqual("kwc-assignment-v1", assignment["schema"])
            self.assertEqual("triage", assignment["phase"])
            self.assertEqual("intake", assignment["triage_stage"])
            self.assertIsNone(assignment["skill"])
            self.assertEqual(["kwc_advance_triage"], assignment["allowed_tools"])
            self.assertEqual("kwc_advance_triage", assignment["next_action"])
            self.assertEqual("kwc-triage-intake-authoring-v1", assignment["artifact_schema"])
            self.assertEqual(
                ".temp/kwc-authoring/151/generation-1/intake-authoring.json",
                assignment["artifact_file"],
            )
            self.assertIn("요구사항", assignment["instruction"])
            self.assertEqual(str(workspace.resolve()), assignment["workspace_path"])

    def test_completed_board_can_start_exact_next_generation_and_preserves_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "repo"
            workspace.mkdir()
            state = Path(tmp) / "state.db"
            assignment = SERVICE.begin_authoring(
                state_path=state,
                board="issue-151-authoring",
                issue_number=151,
                generation=1,
                workspace_path=workspace,
                pm_session_key="pm-session-1",
            )
            for stage in ("intake", "discovery", "source_confirmation", "analysis"):
                assignment = SERVICE.complete_triage_stage(
                    state_path=state,
                    assignment_id=assignment["assignment_id"],
                    caller_profile="project-manager",
                    caller_session_key="pm-session-1",
                    stage=stage,
                    artifact={"path": f"{stage}.json", "sha256": stage},
                )
            for phase, task_ids in (
                ("triage", ["t_123abc"]),
                ("impl", ["t_234bcd"]),
                ("review", ["t_345cde"]),
                ("summary", ["t_456def"]),
            ):
                assignment = SERVICE.complete_phase(
                    state_path=state,
                    assignment_id=assignment["assignment_id"],
                    caller_profile="project-manager",
                    caller_session_key="pm-session-1",
                    phase=phase,
                    created_task_ids=task_ids,
                )

            restarted = SERVICE.begin_authoring(
                state_path=state,
                board="issue-151-authoring",
                issue_number=151,
                generation=2,
                workspace_path=workspace,
                pm_session_key="pm-session-2",
            )

            self.assertEqual("triage", restarted["phase"])
            self.assertEqual("intake", restarted["triage_stage"])
            self.assertEqual(2, restarted["generation"])
            self.assertEqual([], restarted["sources"]["impl_ids"])
            self.assertIsNone(restarted["sources"]["triage_id"])
            conn = SERVICE._connect(state)
            try:
                history = conn.execute(
                    "SELECT snapshot_json FROM authoring_workflow_history "
                    "WHERE board=? AND generation=1",
                    ("issue-151-authoring",),
                ).fetchone()
            finally:
                conn.close()
            self.assertIsNotNone(history)
            self.assertIn('"phase":"complete"', history["snapshot_json"])

    def test_board_restart_rejects_skipped_generation_or_incomplete_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "repo"
            workspace.mkdir()
            state = Path(tmp) / "state.db"
            SERVICE.begin_authoring(
                state_path=state,
                board="issue-151-authoring",
                issue_number=151,
                generation=1,
                workspace_path=workspace,
                pm_session_key="pm-session-1",
            )
            with self.assertRaisesRegex(ValueError, "different authoring contract"):
                SERVICE.begin_authoring(
                    state_path=state,
                    board="issue-151-authoring",
                    issue_number=151,
                    generation=2,
                    workspace_path=workspace,
                    pm_session_key="pm-session-2",
                )

    def test_explicit_replacement_can_advance_one_generation_after_failed_incomplete_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "repo"
            workspace.mkdir()
            state = Path(tmp) / "state.db"
            first = SERVICE.begin_authoring(
                state_path=state,
                board="issue-151-authoring",
                issue_number=151,
                generation=1,
                workspace_path=workspace,
                pm_session_key="pm-session-1",
            )

            replacement = SERVICE.begin_authoring(
                state_path=state,
                board="issue-151-authoring",
                issue_number=151,
                generation=2,
                workspace_path=workspace,
                pm_session_key="pm-session-2",
                replace_incomplete=True,
            )

            self.assertEqual("triage", replacement["phase"])
            self.assertEqual("intake", replacement["triage_stage"])
            self.assertEqual(2, replacement["generation"])
            self.assertNotEqual(first["assignment_id"], replacement["assignment_id"])
            conn = SERVICE._connect(state)
            try:
                history = conn.execute(
                    "SELECT snapshot_json FROM authoring_workflow_history "
                    "WHERE board=? AND generation=1",
                    ("issue-151-authoring",),
                ).fetchone()
            finally:
                conn.close()
            self.assertIsNotNone(history)
            self.assertIn('\"phase\":\"triage\"', history["snapshot_json"])

    def test_triage_stages_advance_in_order_and_persist_artifact_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "repo"
            workspace.mkdir()
            state = Path(tmp) / "state.db"
            assignment = SERVICE.begin_authoring(
                state_path=state,
                board="issue-151-authoring",
                issue_number=151,
                generation=1,
                workspace_path=workspace,
                pm_session_key="pm-session",
            )

            expected = [
                ("intake", "discovery", "kwc-triage-discovery-v2", "discovery-authoring.json"),
                (
                    "discovery",
                    "source_confirmation",
                    "kwc-triage-source-confirmation-v2",
                    "source-confirmation-authoring.json",
                ),
                (
                    "source_confirmation",
                    "analysis",
                    "kwc-triage-analysis-authoring-v1",
                    "analysis-authoring.json",
                ),
                ("analysis", "materialize", None, None),
            ]
            for stage, next_stage, next_schema, next_file in expected:
                assignment = SERVICE.complete_triage_stage(
                    state_path=state,
                    assignment_id=assignment["assignment_id"],
                    caller_profile="project-manager",
                    caller_session_key="pm-session",
                    stage=stage,
                    artifact={
                        "path": f".temp/kwc-authoring/151/{stage}.json",
                        "sha256": stage * 8,
                    },
                )
                self.assertEqual(next_stage, assignment["triage_stage"])
                self.assertEqual(next_schema, assignment["artifact_schema"])
                expected_file = (
                    f".temp/kwc-authoring/151/generation-1/{next_file}"
                    if next_file is not None
                    else None
                )
                self.assertEqual(expected_file, assignment["artifact_file"])

            self.assertEqual(["kwc_create_triage"], assignment["allowed_tools"])
            self.assertEqual(4, len(assignment["stage_artifacts"]))
            resumed = SERVICE.read_assignment(state_path=state, board="issue-151-authoring")
            self.assertEqual(assignment, resumed)

    def test_rejects_out_of_order_or_stale_triage_stage_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "repo"
            workspace.mkdir()
            state = Path(tmp) / "state.db"
            assignment = SERVICE.begin_authoring(
                state_path=state,
                board="issue-151-authoring",
                issue_number=151,
                generation=1,
                workspace_path=workspace,
                pm_session_key="pm-session",
            )
            with self.assertRaisesRegex(ValueError, "stage is stale"):
                SERVICE.complete_triage_stage(
                    state_path=state,
                    assignment_id=assignment["assignment_id"],
                    caller_profile="project-manager",
                    caller_session_key="pm-session",
                    stage="analysis",
                    artifact={"path": "analysis.json", "sha256": "abc"},
                )

            advanced = SERVICE.complete_triage_stage(
                state_path=state,
                assignment_id=assignment["assignment_id"],
                caller_profile="project-manager",
                caller_session_key="pm-session",
                stage="intake",
                artifact={"path": "intake.json", "sha256": "abc"},
            )
            with self.assertRaisesRegex(ValueError, "stale or unknown"):
                SERVICE.complete_triage_stage(
                    state_path=state,
                    assignment_id=assignment["assignment_id"],
                    caller_profile="project-manager",
                    caller_session_key="pm-session",
                    stage="intake",
                    artifact={"path": "intake.json", "sha256": "abc"},
                )
            self.assertEqual("discovery", advanced["triage_stage"])

    def test_complete_phases_advances_exact_source_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "repo"
            workspace.mkdir()
            state = Path(tmp) / "state.db"
            assignment = SERVICE.begin_authoring(
                state_path=state,
                board="issue-151-authoring",
                issue_number=151,
                generation=1,
                workspace_path=workspace,
                pm_session_key="pm-session",
            )
            for stage in ("intake", "discovery", "source_confirmation", "analysis"):
                assignment = SERVICE.complete_triage_stage(
                    state_path=state,
                    assignment_id=assignment["assignment_id"],
                    caller_profile="project-manager",
                    caller_session_key="pm-session",
                    stage=stage,
                    artifact={"path": f"{stage}.json", "sha256": stage},
                )
            impl = SERVICE.complete_phase(
                state_path=state,
                assignment_id=assignment["assignment_id"],
                caller_profile="project-manager",
                caller_session_key="pm-session",
                phase="triage",
                created_task_ids=["t_123abc"],
            )
            self.assertEqual("impl", impl["phase"])
            self.assertEqual("t_123abc", impl["sources"]["triage_id"])
            self.assertIsNone(impl["skill"])
            self.assertEqual(["kwc_create_impl_graph"], impl["allowed_tools"])
            self.assertEqual(
                ".temp/task-graphs/151/impl-card-batch.json",
                impl["artifact_file"],
            )

            review = SERVICE.complete_phase(
                state_path=state,
                assignment_id=impl["assignment_id"],
                caller_profile="project-manager",
                caller_session_key="pm-session",
                phase="impl",
                created_task_ids=["t_111aaa", "t_222bbb"],
            )
            self.assertEqual("review", review["phase"])
            self.assertIsNone(review["skill"])
            self.assertEqual(["t_111aaa", "t_222bbb"], review["sources"]["impl_ids"])
            self.assertEqual(
                ".temp/task-graphs/151/review-body.json",
                review["artifact_file"],
            )

            summary = SERVICE.complete_phase(
                state_path=state,
                assignment_id=review["assignment_id"],
                caller_profile="project-manager",
                caller_session_key="pm-session",
                phase="review",
                created_task_ids=["t_333ccc"],
            )
            self.assertEqual("summary", summary["phase"])
            self.assertIsNone(summary["skill"])
            self.assertEqual("t_333ccc", summary["sources"]["review_id"])
            self.assertEqual(
                ".temp/task-graphs/151/summary-body.json",
                summary["artifact_file"],
            )

            complete = SERVICE.complete_phase(
                state_path=state,
                assignment_id=summary["assignment_id"],
                caller_profile="project-manager",
                caller_session_key="pm-session",
                phase="summary",
                created_task_ids=["t_444ddd"],
            )
            self.assertEqual("complete", complete["phase"])
            self.assertEqual("t_444ddd", complete["sources"]["summary_id"])
            self.assertEqual([], complete["allowed_tools"])

    def test_impl_assignment_can_replace_only_its_unmaterialized_triage_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "repo"
            workspace.mkdir()
            state = Path(tmp) / "state.db"
            assignment = SERVICE.begin_authoring(
                state_path=state,
                board="issue-151-recovery",
                issue_number=151,
                generation=1,
                workspace_path=workspace,
                pm_session_key="pm-session",
            )
            for stage in ("intake", "discovery", "source_confirmation", "analysis"):
                assignment = SERVICE.complete_triage_stage(
                    state_path=state,
                    assignment_id=assignment["assignment_id"],
                    caller_profile="project-manager",
                    caller_session_key="pm-session",
                    stage=stage,
                    artifact={"path": f"{stage}.json", "sha256": stage},
                )
            assignment = SERVICE.complete_phase(
                state_path=state,
                assignment_id=assignment["assignment_id"],
                caller_profile="project-manager",
                caller_session_key="pm-session",
                phase="triage",
                created_task_ids=["t_oldtriage"],
            )

            recovered = SERVICE.replace_impl_triage_source(
                state_path=state,
                assignment_id=assignment["assignment_id"],
                caller_profile="project-manager",
                caller_session_key="pm-session",
                expected_triage_id="t_oldtriage",
                replacement_triage_id="t_newtriage",
            )

            self.assertEqual("impl", recovered["phase"])
            self.assertEqual("t_newtriage", recovered["sources"]["triage_id"])
            self.assertNotEqual(assignment["assignment_id"], recovered["assignment_id"])

    def test_unreviewed_impl_graph_can_be_reopened_for_validated_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "repo"
            workspace.mkdir()
            state = Path(tmp) / "state.db"
            assignment = SERVICE.begin_authoring(
                state_path=state,
                board="issue-151-authoring",
                issue_number=151,
                generation=1,
                workspace_path=workspace,
                pm_session_key="pm-session",
            )
            for stage in ("intake", "discovery", "source_confirmation", "analysis"):
                assignment = SERVICE.complete_triage_stage(
                    state_path=state,
                    assignment_id=assignment["assignment_id"],
                    caller_profile="project-manager",
                    caller_session_key="pm-session",
                    stage=stage,
                    artifact={"path": f"{stage}.json", "sha256": stage},
                )
            assignment = SERVICE.complete_phase(
                state_path=state,
                assignment_id=assignment["assignment_id"],
                caller_profile="project-manager",
                caller_session_key="pm-session",
                phase="triage",
                created_task_ids=["t_a1b2c3d4"],
            )
            impl_ids = [f"t_{index:08x}" for index in range(1, 6)]
            review = SERVICE.complete_phase(
                state_path=state,
                assignment_id=assignment["assignment_id"],
                caller_profile="project-manager",
                caller_session_key="pm-session",
                phase="impl",
                created_task_ids=impl_ids,
            )

            reopened = SERVICE.reopen_unreviewed_impl_graph(
                state_path=state,
                assignment_id=review["assignment_id"],
                caller_profile="project-manager",
                caller_session_key="pm-session",
                expected_impl_ids=impl_ids,
            )

            self.assertEqual("impl", reopened["phase"])
            self.assertEqual([], reopened["sources"]["impl_ids"])
            self.assertEqual("t_a1b2c3d4", reopened["sources"]["triage_id"])
            self.assertNotEqual(review["assignment_id"], reopened["assignment_id"])

    def test_rejects_incomplete_triage_and_wrong_cardinality(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "repo"
            workspace.mkdir()
            state = Path(tmp) / "state.db"
            assignment = SERVICE.begin_authoring(
                state_path=state,
                board="issue-151-authoring",
                issue_number=151,
                generation=1,
                workspace_path=workspace,
                pm_session_key="pm-session",
            )
            with self.assertRaisesRegex(ValueError, "triage stages are incomplete"):
                SERVICE.complete_phase(
                    state_path=state,
                    assignment_id=assignment["assignment_id"],
                    caller_profile="project-manager",
                    caller_session_key="other-session",
                    phase="triage",
                    created_task_ids=["t_123abc"],
                )
            with self.assertRaisesRegex(ValueError, "exactly one"):
                SERVICE.complete_phase(
                    state_path=state,
                    assignment_id=assignment["assignment_id"],
                    caller_profile="project-manager",
                    caller_session_key="pm-session",
                    phase="triage",
                    created_task_ids=["t_123abc", "t_456def"],
                )


if __name__ == "__main__":
    unittest.main()
