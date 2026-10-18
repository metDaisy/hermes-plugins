"""Contract tests for canonical Kanban mutations executed from KWC actions."""

from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import dataclass
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
from tests.support import load_module

MUTATIONS = load_module("kanban.mutation_service")


def _fingerprint(snapshot: dict) -> str:
    encoded = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@dataclass
class _Task:
    id: str
    status: str


class _Connection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Kanban:
    def __init__(
        self,
        *,
        initial_status: str = "todo",
        promoted_status: str = "ready",
        promote_result: tuple[bool, str | None] = (True, None),
    ) -> None:
        self.task = _Task("t_summary", initial_status)
        self.promoted_status = promoted_status
        self.promote_result = promote_result
        self.promote_calls: list[dict] = []

    def get_task(self, _conn: _Connection, task_id: str) -> _Task | None:
        return self.task if task_id == self.task.id else None

    def promote_task(
        self,
        _conn: _Connection,
        task_id: str,
        *,
        actor: str,
        reason: str,
        dry_run: bool,
    ) -> tuple[bool, str | None]:
        self.promote_calls.append(
            {
                "task_id": task_id,
                "actor": actor,
                "reason": reason,
                "dry_run": dry_run,
            }
        )
        if self.promote_result[0]:
            self.task.status = self.promoted_status
        return self.promote_result


class MutationServiceTests(unittest.TestCase):
    def snapshot(self, *, status: str = "todo") -> dict:
        return {
            "schema": "kwc-workflow-snapshot-v1",
            "board": "issue-151",
            "tasks": [{"task_id": "t_summary", "status": status}],
        }

    def action(self, *, snapshot: dict | None = None, status: str = "claimed") -> dict:
        snapshot = snapshot or self.snapshot()
        return {
            "schema": "kwc-workflow-action-v1",
            "action_id": "act_promote_1",
            "board": "issue-151",
            "snapshot_fingerprint": _fingerprint(snapshot),
            "decision_schema": "kwc-workflow-decision-v1",
            "action_kind": "promote-task",
            "task_id": "t_summary",
            "profile": "project-manager",
            "skill": "run-workflow",
            "reason": "approved_review_summary",
            "status": status,
            "claim_generation": 1,
        }

    def test_promote_action_uses_canonical_api_and_reads_back_ready(self) -> None:
        snapshot = self.snapshot()
        action = self.action(snapshot=snapshot)
        conn = _Connection()
        kanban = _Kanban()

        result = MUTATIONS.execute_promote_action(
            action=action,
            snapshot_reader=lambda _board: snapshot,
            connect_fn=lambda **_kwargs: conn,
            kanban_db_module=kanban,
        )

        self.assertEqual(
            kanban.promote_calls,
            [
                {
                    "task_id": "t_summary",
                    "actor": "hermes-langchain",
                    "reason": "approved_review_summary",
                    "dry_run": False,
                }
            ],
        )
        self.assertTrue(conn.closed)
        self.assertEqual(result["result_code"], "PROMOTED")
        self.assertEqual(result["previous_status"], "todo")
        self.assertEqual(result["current_status"], "ready")
        self.assertNotIn("body", result)
        self.assertNotIn("snapshot", result)

    def test_stale_snapshot_is_rejected_before_opening_canonical_store(self) -> None:
        original = self.snapshot()
        changed = self.snapshot(status="blocked")
        opened = False

        def connect_fn(**_kwargs):
            nonlocal opened
            opened = True
            return _Connection()

        with self.assertRaisesRegex(ValueError, "snapshot"):
            MUTATIONS.execute_promote_action(
                action=self.action(snapshot=original),
                snapshot_reader=lambda _board: changed,
                connect_fn=connect_fn,
                kanban_db_module=_Kanban(),
            )

        self.assertFalse(opened)

    def test_already_promoted_or_progressed_task_is_idempotent(self) -> None:
        snapshot = self.snapshot()
        for status in ("ready", "running", "review", "done"):
            with self.subTest(status=status):
                conn = _Connection()
                kanban = _Kanban(initial_status=status)
                result = MUTATIONS.execute_promote_action(
                    action=self.action(snapshot=snapshot),
                    snapshot_reader=lambda _board: snapshot,
                    connect_fn=lambda **_kwargs: conn,
                    kanban_db_module=kanban,
                )
                self.assertEqual(result["result_code"], "PROMOTION_ALREADY_APPLIED")
                self.assertEqual(result["current_status"], status)
                self.assertEqual(kanban.promote_calls, [])
                self.assertTrue(conn.closed)

    def test_retry_after_committed_promotion_accepts_the_changed_snapshot(self) -> None:
        original = self.snapshot(status="todo")
        progressed = self.snapshot(status="ready")
        opened = False

        def connect_fn(**_kwargs):
            nonlocal opened
            opened = True
            return _Connection()

        result = MUTATIONS.execute_promote_action(
            action=self.action(snapshot=original),
            snapshot_reader=lambda _board: progressed,
            connect_fn=connect_fn,
            kanban_db_module=_Kanban(initial_status="ready"),
        )

        self.assertEqual(result["result_code"], "PROMOTION_ALREADY_APPLIED")
        self.assertEqual(result["current_status"], "ready")
        self.assertTrue(result["idempotent_replay"])
        self.assertFalse(opened)

    def test_action_must_be_a_claimed_promote_action(self) -> None:
        snapshot = self.snapshot()
        for field, value, message in (
            ("schema", "wrong", "schema"),
            ("action_kind", "dispatch-task", "action_kind"),
            ("status", "pending", "claimed"),
        ):
            with self.subTest(field=field):
                action = self.action(snapshot=snapshot)
                action[field] = value
                with self.assertRaisesRegex(ValueError, message):
                    MUTATIONS.execute_promote_action(
                        action=action,
                        snapshot_reader=lambda _board: snapshot,
                        connect_fn=lambda **_kwargs: _Connection(),
                        kanban_db_module=_Kanban(),
                    )

    def test_failed_canonical_promotion_is_not_reported_as_success(self) -> None:
        snapshot = self.snapshot()
        conn = _Connection()
        kanban = _Kanban(promote_result=(False, "unsatisfied parent dependencies"))

        with self.assertRaisesRegex(ValueError, "canonical promotion failed"):
            MUTATIONS.execute_promote_action(
                action=self.action(snapshot=snapshot),
                snapshot_reader=lambda _board: snapshot,
                connect_fn=lambda **_kwargs: conn,
                kanban_db_module=kanban,
            )

        self.assertTrue(conn.closed)

    def test_post_mutation_readback_must_confirm_ready(self) -> None:
        snapshot = self.snapshot()
        conn = _Connection()
        kanban = _Kanban(promoted_status="todo")

        with self.assertRaisesRegex(ValueError, "read-back"):
            MUTATIONS.execute_promote_action(
                action=self.action(snapshot=snapshot),
                snapshot_reader=lambda _board: snapshot,
                connect_fn=lambda **_kwargs: conn,
                kanban_db_module=kanban,
            )

        self.assertTrue(conn.closed)


if __name__ == "__main__":
    unittest.main()
