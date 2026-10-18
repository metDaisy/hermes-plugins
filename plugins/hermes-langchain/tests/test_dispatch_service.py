"""Contract tests for fenced canonical dispatch claims."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace


_ROOT = Path(__file__).resolve().parents[1]
from tests.support import load_module

DISPATCH = load_module("kanban.dispatch_service")
ACTIONS = load_module("kanban.action_service")


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class DispatchServiceTests(unittest.TestCase):
    def snapshot(self, *, status="ready", profile="coder", run_id=None) -> dict:
        return {
            "schema": "kwc-workflow-snapshot-v1",
            "board": "issue151",
            "tasks": [
                {
                    "task_id": "t_impl",
                    "status": status,
                    "assignee": profile,
                    "active_run_profile": profile if status == "running" else None,
                    "latest_run_id": run_id,
                }
            ],
        }

    def action(self, snapshot: dict, *, profile="coder") -> dict:
        return {
            "schema": "kwc-workflow-action-v1",
            "action_id": "act_dispatch_1",
            "snapshot_fingerprint": ACTIONS.fingerprint_snapshot(snapshot),
            "board": "issue151",
            "action_kind": "dispatch-task",
            "task_id": "t_impl",
            "profile": profile,
            "skill": "run-impl-card",
            "reason": "ready_implementation",
            "status": "claimed",
        }

    def test_ready_task_is_claimed_through_native_core_and_read_back(self) -> None:
        snapshot = self.snapshot()
        conn = FakeConnection()
        calls = []
        module = SimpleNamespace(
            claim_task=lambda connection, task_id, **kwargs: calls.append(
                (connection, task_id, kwargs)
            )
            or SimpleNamespace(status="running", current_run_id=17),
            claim_review_task=lambda *_args, **_kwargs: None,
            get_task=lambda _connection, _task_id: SimpleNamespace(
                status="running", current_run_id=17
            ),
        )

        result = DISPATCH.execute_dispatch_action(
            action=self.action(snapshot),
            snapshot_reader=lambda _board: snapshot,
            connect_fn=lambda **_kwargs: conn,
            kanban_db_module=module,
        )

        self.assertEqual(result["result_code"], "DISPATCH_CLAIMED")
        self.assertEqual(result["run_id"], 17)
        self.assertEqual(calls[0][1], "t_impl")
        self.assertEqual(calls[0][2]["claimer"], "kwc:act_dispatch_1")
        self.assertTrue(conn.closed)

    def test_review_task_uses_native_review_claim(self) -> None:
        snapshot = self.snapshot(status="review", profile="project-manager")
        calls = []
        module = SimpleNamespace(
            claim_task=lambda *_args, **_kwargs: None,
            claim_review_task=lambda _connection, task_id, **kwargs: calls.append(
                (task_id, kwargs)
            )
            or SimpleNamespace(status="running", current_run_id=18),
            get_task=lambda _connection, _task_id: SimpleNamespace(
                status="running", current_run_id=18
            ),
        )

        result = DISPATCH.execute_dispatch_action(
            action=self.action(snapshot, profile="project-manager"),
            snapshot_reader=lambda _board: snapshot,
            connect_fn=lambda **_kwargs: FakeConnection(),
            kanban_db_module=module,
        )

        self.assertEqual(result["run_id"], 18)
        self.assertEqual(calls[0][0], "t_impl")

    def test_retry_after_claim_recovers_without_reopening_core_connection(self) -> None:
        original = self.snapshot()
        running = self.snapshot(status="running", run_id=17)

        result = DISPATCH.execute_dispatch_action(
            action=self.action(original),
            snapshot_reader=lambda _board: running,
            connect_fn=lambda **_kwargs: self.fail("must not reconnect after confirmed claim"),
            kanban_db_module=SimpleNamespace(),
        )

        self.assertEqual(result["result_code"], "DISPATCH_ALREADY_CLAIMED")
        self.assertEqual(result["run_id"], 17)
        self.assertTrue(result["idempotent_replay"])

    def test_stale_dispatch_is_rejected_when_task_did_not_enter_expected_run(self) -> None:
        original = self.snapshot()
        changed = self.snapshot(status="blocked", profile="coder")

        with self.assertRaisesRegex(ValueError, "canonical snapshot changed"):
            DISPATCH.execute_dispatch_action(
                action=self.action(original),
                snapshot_reader=lambda _board: changed,
                connect_fn=lambda **_kwargs: self.fail("must fence before connection"),
                kanban_db_module=SimpleNamespace(),
            )


if __name__ == "__main__":
    unittest.main()
