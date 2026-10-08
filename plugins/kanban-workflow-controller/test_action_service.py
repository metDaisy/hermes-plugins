"""Contract tests for durable KWC decision-action dispatch."""

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
    "kanban_workflow_controller_action_service", _ROOT / "action_service.py"
)
assert _SPEC and _SPEC.loader
ACTIONS = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = ACTIONS
_SPEC.loader.exec_module(ACTIONS)


class ActionServiceTests(unittest.TestCase):
    def snapshot(self, *, status: str = "ready") -> dict:
        return {
            "schema": "kwc-workflow-snapshot-v1",
            "board": "issue151",
            "tasks": [
                {
                    "task_id": "t_impl",
                    "status": status,
                    "assignee": "coder",
                    "body_schema": "backend-implementation-card-v3",
                }
            ],
        }

    def decision(self, *, kind: str = "dispatch-task", valid: bool = True) -> dict:
        return {
            "schema": "kwc-workflow-decision-v1",
            "board": "issue151",
            "valid": valid,
            "phase": "ready",
            "action": {
                "kind": kind,
                "task_id": "t_impl",
                "profile": "coder",
                "skill": "run-impl-card",
                "reason": "ready_implementation",
            },
            "errors": [],
        }

    def _materialize(
        self,
        state_path: Path,
        *,
        snapshot: dict | None = None,
        decision: dict | None = None,
        now: int = 1000,
    ) -> dict:
        return ACTIONS.materialize_decision_action(
            state_path=state_path,
            snapshot=snapshot or self.snapshot(),
            decision=decision or self.decision(),
            now_fn=lambda: now,
        )

    def test_actionable_decision_materializes_once_for_the_same_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            first = self._materialize(state_path)
            replay = self._materialize(state_path, now=1001)
            actions = ACTIONS.read_actions(state_path=state_path)

        self.assertTrue(first["materialized"])
        self.assertFalse(first["idempotent_replay"])
        self.assertEqual(first["status"], "pending")
        self.assertEqual(first["action_kind"], "dispatch-task")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(first["action_id"], replay["action_id"])
        self.assertEqual(len(actions), 1)
        self.assertNotIn("claimant_id", actions[0])

    def test_changed_snapshot_materializes_a_new_action_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            first = self._materialize(state_path)
            second = self._materialize(
                state_path,
                snapshot=self.snapshot(status="review"),
                now=1001,
            )
            actions = ACTIONS.read_actions(state_path=state_path)

        self.assertNotEqual(first["snapshot_fingerprint"], second["snapshot_fingerprint"])
        self.assertNotEqual(first["action_id"], second["action_id"])
        self.assertEqual(len(actions), 2)

    def test_non_actionable_or_invalid_decision_does_not_enter_the_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            waiting = self._materialize(
                state_path,
                decision=self.decision(kind="wait-for-profile"),
            )
            invalid = self._materialize(
                state_path,
                decision=self.decision(valid=False),
                now=1001,
            )
            actions = ACTIONS.read_actions(state_path=state_path)

        self.assertFalse(waiting["materialized"])
        self.assertEqual(waiting["reason_code"], "NO_EXTERNAL_ACTION")
        self.assertFalse(invalid["materialized"])
        self.assertEqual(invalid["reason_code"], "INVALID_DECISION")
        self.assertEqual(actions, [])

    def test_action_fields_reject_free_form_reason_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            decision = self.decision()
            decision["action"]["reason"] = "The prompt contained a secret"
            with self.assertRaisesRegex(ValueError, "action.reason"):
                self._materialize(state_path, decision=decision)
            self.assertEqual(ACTIONS.read_actions(state_path=state_path), [])

    def test_decision_valid_flag_must_be_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            decision = self.decision()
            decision["valid"] = "true"
            with self.assertRaisesRegex(ValueError, "decision.valid"):
                self._materialize(state_path, decision=decision)
            self.assertEqual(ACTIONS.read_actions(state_path=state_path), [])

    def test_fractional_claim_generation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            action = self._materialize(state_path)
            ACTIONS.claim_next_action(
                state_path=state_path,
                claimant_id="runtime-a",
                ttl_seconds=30,
                now_fn=lambda: 1001,
            )
            with self.assertRaisesRegex(ValueError, "claim_generation"):
                ACTIONS.complete_action(
                    state_path=state_path,
                    action_id=action["action_id"],
                    claimant_id="runtime-a",
                    claim_generation=1.5,
                    result_code="DISPATCHED",
                    now_fn=lambda: 1002,
                )

    def test_claim_is_capacity_one_idempotent_and_supports_stale_takeover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._materialize(state_path)
            first = ACTIONS.claim_next_action(
                state_path=state_path,
                claimant_id="runtime-a",
                ttl_seconds=10,
                now_fn=lambda: 1001,
            )
            replay = ACTIONS.claim_next_action(
                state_path=state_path,
                claimant_id="runtime-a",
                ttl_seconds=10,
                now_fn=lambda: 1002,
            )
            held = ACTIONS.claim_next_action(
                state_path=state_path,
                claimant_id="runtime-b",
                ttl_seconds=10,
                now_fn=lambda: 1002,
            )
            takeover = ACTIONS.claim_next_action(
                state_path=state_path,
                claimant_id="runtime-b",
                ttl_seconds=10,
                now_fn=lambda: 1011,
            )

        self.assertTrue(first["claimed"])
        self.assertEqual(first["claim_generation"], 1)
        self.assertTrue(replay["idempotent_replay"])
        self.assertFalse(held["claimed"])
        self.assertEqual(held["reason_code"], "ACTION_HELD")
        self.assertTrue(takeover["claimed"])
        self.assertTrue(takeover["takeover"])
        self.assertEqual(takeover["claim_generation"], 2)

    def test_claim_heartbeat_extends_only_the_current_unexpired_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            action = self._materialize(state_path)
            claim = ACTIONS.claim_next_action(
                state_path=state_path,
                claimant_id="runtime-a",
                ttl_seconds=10,
                now_fn=lambda: 1001,
            )
            heartbeat = ACTIONS.heartbeat_action_claim(
                state_path=state_path,
                action_id=action["action_id"],
                claimant_id="runtime-a",
                claim_generation=claim["claim_generation"],
                ttl_seconds=20,
                now_fn=lambda: 1005,
            )
            with self.assertRaisesRegex(ValueError, "claimant or generation"):
                ACTIONS.heartbeat_action_claim(
                    state_path=state_path,
                    action_id=action["action_id"],
                    claimant_id="runtime-b",
                    claim_generation=claim["claim_generation"],
                    ttl_seconds=20,
                    now_fn=lambda: 1006,
                )
            with self.assertRaisesRegex(ValueError, "expired"):
                ACTIONS.heartbeat_action_claim(
                    state_path=state_path,
                    action_id=action["action_id"],
                    claimant_id="runtime-a",
                    claim_generation=claim["claim_generation"],
                    ttl_seconds=20,
                    now_fn=lambda: 1025,
                )

        self.assertEqual(heartbeat["claim_expires_at"], 1025)
        self.assertEqual(heartbeat["status"], "claimed")

    def test_completion_is_bound_to_claimant_generation_and_exact_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            action = self._materialize(state_path)
            claim = ACTIONS.claim_next_action(
                state_path=state_path,
                claimant_id="runtime-a",
                ttl_seconds=60,
                now_fn=lambda: 1001,
            )
            completed = ACTIONS.complete_action(
                state_path=state_path,
                action_id=action["action_id"],
                claimant_id="runtime-a",
                claim_generation=claim["claim_generation"],
                result_code="INVOCATION_CREATED",
                now_fn=lambda: 1002,
            )
            replay = ACTIONS.complete_action(
                state_path=state_path,
                action_id=action["action_id"],
                claimant_id="runtime-a",
                claim_generation=claim["claim_generation"],
                result_code="INVOCATION_CREATED",
                now_fn=lambda: 1003,
            )
            with self.assertRaisesRegex(ValueError, "different result"):
                ACTIONS.complete_action(
                    state_path=state_path,
                    action_id=action["action_id"],
                    claimant_id="runtime-a",
                    claim_generation=claim["claim_generation"],
                    result_code="KANBAN_MUTATED",
                    now_fn=lambda: 1004,
                )

        self.assertEqual(completed["status"], "completed")
        self.assertFalse(completed["idempotent_replay"])
        self.assertTrue(replay["idempotent_replay"])

    def test_stale_claimant_cannot_complete_after_takeover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            action = self._materialize(state_path)
            first = ACTIONS.claim_next_action(
                state_path=state_path,
                claimant_id="runtime-a",
                ttl_seconds=10,
                now_fn=lambda: 1001,
            )
            ACTIONS.claim_next_action(
                state_path=state_path,
                claimant_id="runtime-b",
                ttl_seconds=10,
                now_fn=lambda: 1011,
            )
            with self.assertRaisesRegex(ValueError, "does not own"):
                ACTIONS.complete_action(
                    state_path=state_path,
                    action_id=action["action_id"],
                    claimant_id="runtime-a",
                    claim_generation=first["claim_generation"],
                    result_code="INVOCATION_CREATED",
                    now_fn=lambda: 1012,
                )

    def test_store_uses_wal_and_has_no_prompt_session_or_raw_payload_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._materialize(state_path)
            with closing(sqlite3.connect(state_path)) as conn:
                journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(workflow_action)").fetchall()
                }

        self.assertEqual(journal_mode.lower(), "wal")
        forbidden = {
            "prompt",
            "messages",
            "transcript",
            "reasoning",
            "raw_output",
            "raw_payload",
            "command",
            "credentials",
            "session_key",
            "task_body",
        }
        self.assertTrue(forbidden.isdisjoint(columns))


if __name__ == "__main__":
    unittest.main()
