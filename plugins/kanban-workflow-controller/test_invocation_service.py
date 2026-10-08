"""Contract tests for KWC workflow invocation responses."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


_ROOT = Path(__file__).parent
_SPEC = importlib.util.spec_from_file_location(
    "kanban_workflow_controller_invocation_service", _ROOT / "invocation_service.py"
)
assert _SPEC and _SPEC.loader
INVOCATIONS = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = INVOCATIONS
_SPEC.loader.exec_module(INVOCATIONS)


class InvocationServiceTests(unittest.TestCase):
    def _create(self, state_path: Path) -> dict:
        return INVOCATIONS.create_invocation(
            state_path=state_path,
            invocation_id="inv_pm_1",
            workflow_instance_id="issue151:g1",
            board="issue151",
            profile="project-manager",
            session_key="pm-session-key",
            expected_response="checkpoint-decision",
            allowed_outcomes=["checkpoint-approved", "changes-requested", "blocked"],
            snapshot_fingerprint="sha256:snapshot-1",
            now_fn=lambda: 1000,
        )

    def test_pending_pm_invocation_accepts_one_structured_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            created = self._create(state_path)
            accepted = INVOCATIONS.submit_pm_response(
                state_path=state_path,
                invocation_id="inv_pm_1",
                caller_profile="project-manager",
                caller_session_key="pm-session-key",
                outcome="checkpoint-approved",
                task_id="t_impl1",
                reason_code="VERIFICATION_PASSED",
                now_fn=lambda: 1001,
            )
            stored = INVOCATIONS.read_invocation(
                state_path=state_path, invocation_id="inv_pm_1"
            )

        self.assertEqual(created["status"], "pending")
        self.assertTrue(accepted["ok"])
        self.assertFalse(accepted["idempotent_replay"])
        self.assertEqual(accepted["expected_response"], "checkpoint-decision")
        self.assertEqual(stored["status"], "accepted")
        self.assertEqual(stored["outcome"], "checkpoint-approved")
        self.assertEqual(stored["task_id"], "t_impl1")
        self.assertEqual(stored["reason_code"], "VERIFICATION_PASSED")
        self.assertNotIn("session_key", accepted)

    def test_invocation_store_uses_wal_for_cross_process_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._create(state_path)
            with closing(sqlite3.connect(state_path)) as conn:
                journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]

        self.assertEqual(journal_mode.lower(), "wal")

    def test_identical_response_is_idempotent_but_conflicting_replay_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._create(state_path)
            arguments = {
                "state_path": state_path,
                "invocation_id": "inv_pm_1",
                "caller_profile": "project-manager",
                "caller_session_key": "pm-session-key",
                "outcome": "blocked",
                "task_id": "t_impl1",
                "reason_code": "MISSING_INPUT",
                "now_fn": lambda: 1001,
            }
            first = INVOCATIONS.submit_pm_response(**arguments)
            replay = INVOCATIONS.submit_pm_response(**arguments)
            with self.assertRaisesRegex(ValueError, "different response"):
                INVOCATIONS.submit_pm_response(
                    **{**arguments, "outcome": "checkpoint-approved"}
                )

        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(replay["idempotent_replay"])

    def test_response_is_bound_to_expected_profile_session_and_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._create(state_path)
            base = {
                "state_path": state_path,
                "invocation_id": "inv_pm_1",
                "caller_profile": "project-manager",
                "caller_session_key": "pm-session-key",
                "outcome": "checkpoint-approved",
                "task_id": "t_impl1",
                "reason_code": "VERIFICATION_PASSED",
                "now_fn": lambda: 1001,
            }
            with self.assertRaisesRegex(ValueError, "profile"):
                INVOCATIONS.submit_pm_response(
                    **{**base, "caller_profile": "coder"}
                )
            with self.assertRaisesRegex(ValueError, "session"):
                INVOCATIONS.submit_pm_response(
                    **{**base, "caller_session_key": "another-session"}
                )
            with self.assertRaisesRegex(ValueError, "outcome"):
                INVOCATIONS.submit_pm_response(
                    **{**base, "outcome": "invented-result"}
                )

    def test_reason_code_must_be_a_compact_stable_identifier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._create(state_path)
            with self.assertRaisesRegex(ValueError, "reason_code"):
                INVOCATIONS.submit_pm_response(
                    state_path=state_path,
                    invocation_id="inv_pm_1",
                    caller_profile="project-manager",
                    caller_session_key="pm-session-key",
                    outcome="blocked",
                    task_id="t_impl1",
                    reason_code="The prompt included a secret",
                    now_fn=lambda: 1001,
                )

    def test_configured_coordinator_profile_can_submit_its_bound_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            INVOCATIONS.create_invocation(
                state_path=state_path,
                invocation_id="inv_pm_custom",
                workflow_instance_id="issue151:g1",
                board="issue151",
                profile="workflow-pm",
                session_key="custom-pm-session",
                expected_response="checkpoint-decision",
                allowed_outcomes=["approved"],
                snapshot_fingerprint="sha256:snapshot-1",
                now_fn=lambda: 1000,
            )
            accepted = INVOCATIONS.submit_pm_response(
                state_path=state_path,
                invocation_id="inv_pm_custom",
                caller_profile="workflow-pm",
                caller_session_key="custom-pm-session",
                outcome="approved",
                reason_code="VERIFICATION_PASSED",
                now_fn=lambda: 1001,
            )

        self.assertTrue(accepted["ok"])
        self.assertEqual(accepted["profile"], "workflow-pm")

    def test_non_coordinator_profile_can_submit_its_bound_typed_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            INVOCATIONS.create_invocation(
                state_path=state_path,
                invocation_id="inv_coder_1",
                workflow_instance_id="issue151:g1",
                board="issue151",
                profile="coder",
                session_key="coder-session-key",
                expected_response="implementation-result",
                allowed_outcomes=["review-requested", "blocked"],
                snapshot_fingerprint="sha256:snapshot-1",
                now_fn=lambda: 1000,
            )

            accepted = INVOCATIONS.submit_profile_response(
                state_path=state_path,
                invocation_id="inv_coder_1",
                caller_profile="coder",
                caller_session_key="coder-session-key",
                outcome="review-requested",
                task_id="t_impl1",
                reason_code="IMPLEMENTATION_VERIFIED",
                now_fn=lambda: 1001,
            )

        self.assertTrue(accepted["ok"])
        self.assertEqual(accepted["profile"], "coder")
        self.assertEqual(accepted["status"], "accepted")

    def test_invocation_creation_is_idempotent_only_for_the_same_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            first = self._create(state_path)
            replay = self._create(state_path)
            with self.assertRaisesRegex(ValueError, "different contract"):
                INVOCATIONS.create_invocation(
                    state_path=state_path,
                    invocation_id="inv_pm_1",
                    workflow_instance_id="issue151:g1",
                    board="issue151",
                    profile="project-manager",
                    session_key="pm-session-key",
                    expected_response="summary-decision",
                    allowed_outcomes=["summary-ready"],
                    snapshot_fingerprint="sha256:snapshot-1",
                    now_fn=lambda: 1002,
                )

        self.assertEqual(first, replay)

    def test_allowed_outcomes_rejects_a_scalar_string(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "allowed_outcomes"):
                INVOCATIONS.create_invocation(
                    state_path=Path(tmp) / "state.db",
                    invocation_id="inv_pm_2",
                    workflow_instance_id="issue151:g1",
                    board="issue151",
                    profile="project-manager",
                    session_key="pm-session-key",
                    expected_response="checkpoint-decision",
                    allowed_outcomes="approved",
                    snapshot_fingerprint="sha256:snapshot-1",
                    now_fn=lambda: 1000,
                )


if __name__ == "__main__":
    unittest.main()
