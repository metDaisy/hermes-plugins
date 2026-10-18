"""Contract tests for durable KWC Profile wake delivery."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
from tests.support import load_module

WAKES = load_module("runtime.wake_service")


class WakeServiceTests(unittest.TestCase):
    def contract(self) -> dict:
        return {
            "action_id": "act_wake_1",
            "board": "issue151",
            "task_id": "t_review",
            "profile": "project-manager",
            "skill": "run-workflow",
            "invocation_id": "inv_pm_1",
        }

    def test_enqueue_is_idempotent_only_for_the_same_compact_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            first = WAKES.enqueue_profile_wake(
                state_path=state_path, **self.contract(), now_fn=lambda: 1000
            )
            replay = WAKES.enqueue_profile_wake(
                state_path=state_path, **self.contract(), now_fn=lambda: 1001
            )
            changed = self.contract()
            changed["task_id"] = "t_other"
            with self.assertRaisesRegex(ValueError, "different wake contract"):
                WAKES.enqueue_profile_wake(
                    state_path=state_path, **changed, now_fn=lambda: 1002
                )

        self.assertEqual(first["status"], "pending")
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertNotIn("session_key", first)

    def test_claim_is_board_scoped_fenced_and_supports_stale_takeover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            WAKES.enqueue_profile_wake(
                state_path=state_path, **self.contract(), now_fn=lambda: 1000
            )
            first = WAKES.claim_next_profile_wake(
                state_path=state_path,
                board="issue151",
                claimant_id="gateway-a",
                ttl_seconds=10,
                now_fn=lambda: 1001,
            )
            held = WAKES.claim_next_profile_wake(
                state_path=state_path,
                board="issue151",
                claimant_id="gateway-b",
                ttl_seconds=10,
                now_fn=lambda: 1002,
            )
            takeover = WAKES.claim_next_profile_wake(
                state_path=state_path,
                board="issue151",
                claimant_id="gateway-b",
                ttl_seconds=10,
                now_fn=lambda: 1011,
            )

        self.assertTrue(first["claimed"])
        self.assertEqual(first["claim_generation"], 1)
        self.assertFalse(held["claimed"])
        self.assertEqual(held["reason_code"], "WAKE_HELD")
        self.assertTrue(takeover["claimed"])
        self.assertTrue(takeover["takeover"])
        self.assertEqual(takeover["claim_generation"], 2)

    def test_delivery_completion_is_generation_fenced_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            WAKES.enqueue_profile_wake(
                state_path=state_path, **self.contract(), now_fn=lambda: 1000
            )
            claim = WAKES.claim_next_profile_wake(
                state_path=state_path,
                board="issue151",
                claimant_id="gateway-a",
                ttl_seconds=30,
                now_fn=lambda: 1001,
            )
            delivered = WAKES.complete_profile_wake(
                state_path=state_path,
                action_id="act_wake_1",
                claimant_id="gateway-a",
                claim_generation=claim["claim_generation"],
                now_fn=lambda: 1002,
            )
            replay = WAKES.complete_profile_wake(
                state_path=state_path,
                action_id="act_wake_1",
                claimant_id="gateway-a",
                claim_generation=claim["claim_generation"],
                now_fn=lambda: 1003,
            )

        self.assertEqual(delivered["status"], "delivered")
        self.assertFalse(delivered["idempotent_replay"])
        self.assertTrue(replay["idempotent_replay"])

    def test_schema_contains_no_prompt_body_output_or_reasoning_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            WAKES.enqueue_profile_wake(
                state_path=state_path, **self.contract(), now_fn=lambda: 1000
            )
            columns = WAKES._table_columns(state_path)

        self.assertTrue(
            {
                "action_id",
                "schema",
                "board",
                "task_id",
                "profile",
                "skill",
                "invocation_id",
                "status",
                "claim_generation",
                "claimant_id",
                "created_at",
                "claimed_at",
                "claim_expires_at",
                "delivered_at",
            }.issubset(columns)
        )
        forbidden = {"prompt", "task_body", "reasoning", "raw_output", "session_key"}
        self.assertTrue(forbidden.isdisjoint(columns))


if __name__ == "__main__":
    unittest.main()
