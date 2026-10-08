"""Tests for canonical Kanban to KWC workflow snapshot normalization."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


_ROOT = Path(__file__).parent
_SPEC = importlib.util.spec_from_file_location(
    "kanban_workflow_controller_snapshot_service", _ROOT / "snapshot_service.py"
)
assert _SPEC and _SPEC.loader
SNAPSHOTS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(SNAPSHOTS)


class _FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class SnapshotServiceTests(unittest.TestCase):
    def test_hermes_source_root_is_added_before_loading_canonical_modules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp)
            package_dir = source_root / "hermes_cli"
            package_dir.mkdir()
            package_file = package_dir / "__init__.py"
            package_file.write_text("", encoding="utf-8")
            module = SimpleNamespace(__file__=str(package_file))
            original_path = list(sys.path)
            try:
                SNAPSHOTS._ensure_hermes_source_root(module)
                self.assertEqual(str(source_root), sys.path[0])
            finally:
                sys.path[:] = original_path

    def test_normalizes_body_graph_run_and_claim_source_status(self) -> None:
        task = SimpleNamespace(
            id="t_impl",
            body=json.dumps({"schema": "backend-implementation-card-v3"}),
            assignee="coder",
            status="running",
            skills=None,
            block_kind=None,
            current_run_id=7,
        )
        run = SimpleNamespace(
            id=7,
            profile="coder",
            status="running",
            metadata=None,
        )
        claimed = SimpleNamespace(
            run_id=7,
            kind="claimed",
            payload={"source_status": "ready"},
        )

        snapshot = SNAPSHOTS.normalize_snapshot(
            board="issue-151",
            tasks=[task],
            graph_contexts={
                "t_impl": {
                    "parents": [{"id": "t_triage", "status": "done"}],
                    "children": [{"id": "t_review", "status": "todo"}],
                }
            },
            runs_by_task={"t_impl": [run]},
            events_by_task={"t_impl": [claimed]},
        )

        self.assertEqual("kwc-workflow-snapshot-v1", snapshot["schema"])
        self.assertEqual("issue-151", snapshot["board"])
        self.assertEqual(
            {
                "task_id": "t_impl",
                "status": "running",
                "assignee": "coder",
                "body_schema": "backend-implementation-card-v3",
                "parents": ["t_triage"],
                "children": ["t_review"],
                "skills": [],
                "active_run_profile": "coder",
                "active_run_source_status": "ready",
                "latest_run_profile": "coder",
                "latest_run_id": 7,
                "latest_run_status": "running",
                "latest_run_metadata_schema": None,
                "latest_run_result": None,
                "block_kind": None,
            },
            snapshot["tasks"][0],
        )

    def test_reads_snapshot_through_canonical_kanban_functions_and_closes_connection(self) -> None:
        task = SimpleNamespace(
            id="t_review",
            body=json.dumps({"schema": "aggregate-review-card-v3"}),
            assignee="reviewer",
            status="ready",
            skills=["run-review"],
            block_kind=None,
            current_run_id=None,
        )
        connection = _FakeConnection()
        calls: list[tuple[str, object]] = []

        class FakeKanbanDb:
            @staticmethod
            def list_tasks(conn):
                calls.append(("list_tasks", conn))
                return [task]

            @staticmethod
            def task_graph_contexts(conn, task_ids):
                calls.append(("task_graph_contexts", tuple(task_ids)))
                return {"t_review": {"parents": [], "children": []}}

            @staticmethod
            def list_runs(conn, task_id):
                calls.append(("list_runs", task_id))
                return []

            @staticmethod
            def list_events(conn, task_id):
                calls.append(("list_events", task_id))
                return []

        snapshot = SNAPSHOTS.read_board_snapshot(
            "issue-151",
            kanban_db_module=FakeKanbanDb,
            connect_fn=lambda *, board: connection,
        )

        self.assertEqual("issue-151", snapshot["board"])
        self.assertEqual("aggregate-review-card-v3", snapshot["tasks"][0]["body_schema"])
        self.assertTrue(connection.closed)
        self.assertEqual(
            [
                ("list_tasks", connection),
                ("task_graph_contexts", ("t_review",)),
                ("list_runs", "t_review"),
                ("list_events", "t_review"),
            ],
            calls,
        )


if __name__ == "__main__":
    unittest.main()
