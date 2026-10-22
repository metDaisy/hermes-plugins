"""Accepted-artifact fencing for native Kanban materialization."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hermes_langchain.controller.artifact_store import (
    ArtifactRecord,
    SQLiteArtifactStore,
    canonical_json_sha256,
)
from hermes_langchain.controller.kanban_materialization import (
    AcceptedMilestoneMaterializer,
    KanbanMaterializationError,
    fingerprint_native_snapshot,
)


class FakeKanbanPort:
    def __init__(self) -> None:
        self.tasks: dict[str, dict] = {}
        self.created: list[dict] = []
        self.archived: list[str] = []
        self.next_id = 1
        self.tamper_readback = False
        self.fail_readback = False
        self.mutate_before_create = False
        self.exact_replay_before_create = False
        self.malformed_create_receipt = False
        self.fail_after_create_commit = False

    def snapshot(self, *, board: str) -> dict:
        return {
            "board": board,
            "tasks": [
                copy.deepcopy(task)
                for task in self.tasks.values()
            ],
        }

    def create_triage(
        self,
        *,
        board: str,
        card: dict,
        expected_snapshot_fingerprint: str | None = None,
    ) -> dict:
        if self.mutate_before_create:
            self.tasks["t_race"] = {
                "id": "t_race",
                "title": "Concurrent mutation",
                "body": "{}",
                "assignee": "project-manager",
                "status": "todo",
                "workspace_kind": "dir",
                "workspace_path": card["workspace_path"],
                "skills": [],
                "idempotency_key": "concurrent-mutation",
            }
        if self.exact_replay_before_create:
            self.tasks["t_concurrent_exact"] = {
                "id": "t_concurrent_exact",
                "title": card["title"],
                "body": card["body_text"],
                "assignee": card["assignee"],
                "status": "triage",
                "workspace_kind": "dir",
                "workspace_path": card["workspace_path"],
                "skills": [],
                "idempotency_key": card["idempotency_key"],
            }
        existing = next(
            (
                task
                for task in self.tasks.values()
                if task["idempotency_key"] == card["idempotency_key"]
            ),
            None,
        )
        if existing is not None:
            return {"task_id": existing["id"], "created": False}
        if (
            expected_snapshot_fingerprint is not None
            and fingerprint_native_snapshot(self.snapshot(board=board))
            != expected_snapshot_fingerprint
        ):
            raise KanbanMaterializationError(
                "SNAPSHOT_CHANGED",
                "canonical snapshot changed before materialization",
            )
        task_id = f"t_native_{self.next_id}"
        self.next_id += 1
        task = {
            "id": task_id,
            "title": card["title"],
            "body": card["body_text"],
            "assignee": card["assignee"],
            "status": "triage",
            "workspace_kind": "dir",
            "workspace_path": card["workspace_path"],
            "skills": [],
            "idempotency_key": card["idempotency_key"],
        }
        self.tasks[task_id] = task
        self.created.append(copy.deepcopy(task))
        if self.fail_after_create_commit:
            failure = KanbanMaterializationError(
                "NATIVE_CREATE_COMMITTED",
                "native create failed after commit",
            )
            failure.created_task_id = task_id
            raise failure
        if self.malformed_create_receipt:
            return {"task_id": task_id, "created": True, "unexpected": True}
        return {"task_id": task_id, "created": True}

    def read_task(self, *, board: str, task_id: str) -> dict:
        del board
        if self.fail_readback and self.tasks[task_id]["status"] != "archived":
            raise RuntimeError("private readback failure")
        task = copy.deepcopy(self.tasks[task_id])
        if self.tamper_readback and task["status"] != "archived":
            task["body"] += "\n"
        return task

    def archive_task(self, *, board: str, task_id: str) -> None:
        del board
        self.tasks[task_id]["status"] = "archived"
        self.archived.append(task_id)


class ControllerKanbanMaterializationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.store = SQLiteArtifactStore.open(self.root / "artifacts.sqlite3")
        self.port = FakeKanbanPort()
        self.payload = {
            "schema": "controller-triage-milestone-v1",
            "board": "disposable-controller-board",
            "issue_number": 42,
            "generation": 2,
            "workspace_path": self.workspace.resolve().as_posix(),
            "title": "G2-Issue42-Triage",
            "assignee": "project-manager",
            "status": "triage",
            "body_text": (
                '{"schema":"triage-v7","issue":{"number":42},'
                '"frozen_digest":"' + "a" * 64 + '"}\n'
            ),
            "frozen_digest": "a" * 64,
        }
        self.input_value = {"accepted": "triage"}
        self.record_payload = self.accepted_payload(self.payload)
        self.record = ArtifactRecord(
            artifact_id="artifact-triage-accepted",
            run_id="run-materialization-1",
            step_id="triage_materialize",
            attempt=1,
            executor_kind="materialize",
            input_sha256=canonical_json_sha256(self.input_value),
            output_sha256=canonical_json_sha256(self.record_payload),
            timeout_seconds=300,
            validation_result="accepted",
            executor_identity="controller-triage-materialize",
            payload=self.record_payload,
        )
        self.store.put(self.record, input_value=self.input_value)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def request(self) -> dict:
        snapshot = self.port.snapshot(board=self.payload["board"])
        return {
            "schema": "controller-kanban-materialization-v1",
            "artifact_id": self.record.artifact_id,
            "artifact_sha256": self.record.output_sha256,
            "board": self.payload["board"],
            "issue_number": self.payload["issue_number"],
            "generation": self.payload["generation"],
            "workspace_path": self.payload["workspace_path"],
            "expected_snapshot_fingerprint": fingerprint_native_snapshot(snapshot),
        }

    @staticmethod
    def accepted_payload(milestone: dict) -> dict:
        body_text = milestone["body_text"]
        frozen_body_file = ".temp/controller-triage/frozen-triage.json"
        return {
            "stage": "materialize",
            "artifact": {
                "path": frozen_body_file,
                "sha256": hashlib.sha256(body_text.encode("utf-8")).hexdigest(),
            },
            "body": json.loads(body_text),
            "body_text": body_text,
            "frozen_digest": milestone["frozen_digest"],
            "read_back": {
                "valid": True,
                "verified": True,
                "task_id": "t_parity_fixture",
                "title": milestone["title"],
                "status": "triage",
                "frozen_digest": milestone["frozen_digest"],
                "frozen_body_file": frozen_body_file,
            },
            "milestone": milestone,
        }

    def materializer(self) -> AcceptedMilestoneMaterializer:
        return AcceptedMilestoneMaterializer(
            artifact_store=self.store,
            kanban_port=self.port,
        )

    def test_accepted_artifact_is_generation_and_snapshot_fenced(self) -> None:
        request = self.request()
        changed = copy.deepcopy(request)
        changed["generation"] = 3
        with self.assertRaisesRegex(KanbanMaterializationError, "artifact identity"):
            self.materializer().materialize(changed)
        self.assertEqual([], self.port.created)

        stale = self.request()
        self.port.tasks["t_unrelated"] = {
            "id": "t_unrelated",
            "title": "Unrelated",
            "body": "{}",
            "assignee": "project-manager",
            "status": "todo",
            "workspace_kind": "dir",
            "workspace_path": self.workspace.resolve().as_posix(),
            "skills": [],
            "idempotency_key": "unrelated",
        }
        with self.assertRaisesRegex(KanbanMaterializationError, "snapshot changed"):
            self.materializer().materialize(stale)
        self.assertEqual([], self.port.created)

    def test_native_create_rechecks_snapshot_at_mutation_boundary(self) -> None:
        request = self.request()
        self.port.mutate_before_create = True

        with self.assertRaisesRegex(KanbanMaterializationError, "snapshot changed"):
            self.materializer().materialize(request)

        self.assertEqual([], self.port.created)

    def test_concurrent_exact_create_returns_idempotent_replay(self) -> None:
        request = self.request()
        self.port.exact_replay_before_create = True

        receipt = self.materializer().materialize(request)

        self.assertTrue(receipt["idempotent_replay"])
        self.assertEqual("t_concurrent_exact", receipt["task_id"])
        self.assertEqual([], self.port.created)

    def test_milestone_title_is_bound_to_issue_generation(self) -> None:
        store = SQLiteArtifactStore.open(self.root / "invalid-title-artifacts.sqlite3")
        try:
            milestone = dict(self.payload, title="Triage")
            accepted_payload = self.accepted_payload(milestone)
            record = ArtifactRecord(
                artifact_id="artifact-invalid-generation-title",
                run_id="run-invalid-generation-title",
                step_id="triage_materialize",
                attempt=1,
                executor_kind="materialize",
                input_sha256=canonical_json_sha256(self.input_value),
                output_sha256=canonical_json_sha256(accepted_payload),
                timeout_seconds=300,
                validation_result="accepted",
                executor_identity="controller-triage-materialize",
                payload=accepted_payload,
            )
            store.put(record, input_value=self.input_value)
            request = {
                "schema": "controller-kanban-materialization-v1",
                "artifact_id": record.artifact_id,
                "artifact_sha256": record.output_sha256,
                "board": milestone["board"],
                "issue_number": milestone["issue_number"],
                "generation": milestone["generation"],
                "workspace_path": milestone["workspace_path"],
                "expected_snapshot_fingerprint": fingerprint_native_snapshot(
                    self.port.snapshot(board=milestone["board"])
                ),
            }
            materializer = AcceptedMilestoneMaterializer(
                artifact_store=store,
                kanban_port=self.port,
            )

            with self.assertRaisesRegex(KanbanMaterializationError, "generation title"):
                materializer.materialize(request)

            self.assertEqual([], self.port.created)
        finally:
            store.close()

    def test_exact_replay_is_idempotent_and_conflicting_replay_is_rejected(self) -> None:
        request = self.request()
        first = self.materializer().materialize(request)
        replay = self.materializer().materialize(request)

        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(first["task_id"], replay["task_id"])
        self.assertEqual(1, len(self.port.created))

        self.port.tasks[first["task_id"]]["body"] = "{}"
        with self.assertRaisesRegex(KanbanMaterializationError, "conflicting replay"):
            self.materializer().materialize(request)
        self.assertEqual(1, len(self.port.created))

    def test_exact_replay_reuses_archived_native_task(self) -> None:
        request = self.request()
        first = self.materializer().materialize(request)
        self.port.archive_task(board=request["board"], task_id=first["task_id"])

        replay = self.materializer().materialize(request)

        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(first["task_id"], replay["task_id"])
        self.assertEqual(1, len(self.port.created))

    def test_exact_readback_and_verified_rollback(self) -> None:
        request = self.request()
        receipt = self.materializer().materialize(request)
        task = self.port.read_task(board=request["board"], task_id=receipt["task_id"])
        self.assertTrue(receipt["verified"])
        self.assertEqual(self.payload["body_text"], task["body"])
        self.assertEqual(self.payload["workspace_path"], task["workspace_path"])

        second_store = SQLiteArtifactStore.open(self.root / "second-artifacts.sqlite3")
        try:
            second_payload = dict(self.payload, generation=3, title="G3-Issue42-Triage")
            second_record_payload = self.accepted_payload(second_payload)
            second_record = ArtifactRecord(
                artifact_id="artifact-triage-readback-failure",
                run_id="run-materialization-2",
                step_id="triage_materialize",
                attempt=1,
                executor_kind="materialize",
                input_sha256=canonical_json_sha256(self.input_value),
                output_sha256=canonical_json_sha256(second_record_payload),
                timeout_seconds=300,
                validation_result="accepted",
                executor_identity="controller-triage-materialize",
                payload=second_record_payload,
            )
            second_store.put(second_record, input_value=self.input_value)
            failing_port = FakeKanbanPort()
            failing_port.tamper_readback = True
            failing = AcceptedMilestoneMaterializer(
                artifact_store=second_store,
                kanban_port=failing_port,
            )
            failing_request = {
                "schema": "controller-kanban-materialization-v1",
                "artifact_id": second_record.artifact_id,
                "artifact_sha256": second_record.output_sha256,
                "board": second_payload["board"],
                "issue_number": second_payload["issue_number"],
                "generation": second_payload["generation"],
                "workspace_path": second_payload["workspace_path"],
                "expected_snapshot_fingerprint": fingerprint_native_snapshot(
                    failing_port.snapshot(board=second_payload["board"])
                ),
            }
            with self.assertRaisesRegex(KanbanMaterializationError, "read-back"):
                failing.materialize(failing_request)
            self.assertEqual(["t_native_1"], failing_port.archived)
            rolled_back = failing_port.read_task(
                board=second_payload["board"], task_id="t_native_1"
            )
            self.assertEqual("archived", rolled_back["status"])
        finally:
            second_store.close()

    def test_malformed_create_receipt_rolls_back_created_task(self) -> None:
        self.port.malformed_create_receipt = True

        with self.assertRaisesRegex(KanbanMaterializationError, "create receipt"):
            self.materializer().materialize(self.request())

        self.assertEqual(1, len(self.port.created))
        created_id = self.port.created[0]["id"]
        self.assertEqual([created_id], self.port.archived)
        rolled_back = self.port.read_task(board=self.payload["board"], task_id=created_id)
        self.assertEqual("archived", rolled_back["status"])

    def test_post_commit_create_failure_rolls_back_created_task(self) -> None:
        self.port.fail_after_create_commit = True

        with self.assertRaisesRegex(KanbanMaterializationError, "after commit"):
            self.materializer().materialize(self.request())

        self.assertEqual(1, len(self.port.created))
        created_id = self.port.created[0]["id"]
        self.assertEqual([created_id], self.port.archived)
        rolled_back = self.port.read_task(board=self.payload["board"], task_id=created_id)
        self.assertEqual("archived", rolled_back["status"])

    def test_native_port_materializes_and_reads_back_disposable_board(self) -> None:
        board = f"m9-disposable-{uuid.uuid4().hex[:12]}"
        subprocess.run(
            ["hermes", "kanban", "boards", "create", board],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        native_store = SQLiteArtifactStore.open(self.root / "native-artifacts.sqlite3")
        try:
            native_payload = dict(self.payload, board=board)
            native_record_payload = self.accepted_payload(native_payload)
            native_record = ArtifactRecord(
                artifact_id="artifact-native-triage-accepted",
                run_id="run-native-materialization-1",
                step_id="triage_materialize",
                attempt=1,
                executor_kind="materialize",
                input_sha256=canonical_json_sha256(self.input_value),
                output_sha256=canonical_json_sha256(native_record_payload),
                timeout_seconds=300,
                validation_result="accepted",
                executor_identity="controller-triage-materialize",
                payload=native_record_payload,
            )
            native_store.put(native_record, input_value=self.input_value)
            request = {
                "schema": "controller-kanban-materialization-v1",
                "artifact_id": native_record.artifact_id,
                "artifact_sha256": native_record.output_sha256,
                "board": board,
                "issue_number": native_payload["issue_number"],
                "generation": native_payload["generation"],
                "workspace_path": native_payload["workspace_path"],
                "expected_snapshot_fingerprint": fingerprint_native_snapshot(
                    {"board": board, "tasks": []}
                ),
            }
            request_file = self.root / "native-request.json"
            request_file.write_text(
                json.dumps(request, ensure_ascii=False), encoding="utf-8"
            )
            hermes_executable = shutil.which("hermes")
            self.assertIsNotNone(hermes_executable)
            runtime_python = Path(str(hermes_executable)).resolve().parent / (
                "python.exe" if sys.platform == "win32" else "python"
            )
            completed = subprocess.run(
                [
                    str(runtime_python),
                    str(_ROOT / "tests" / "controller_native_materialization_worker.py"),
                    str(_ROOT),
                    str(self.root / "native-artifacts.sqlite3"),
                    str(request_file),
                    "success",
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            result = json.loads(completed.stdout)
            first = result["first"]
            replay = result["replay"]
            archived_replay = result["archived_replay"]
            native = result["task"]
            self.assertTrue(first["verified"])
            self.assertFalse(first["idempotent_replay"])
            self.assertTrue(replay["idempotent_replay"])
            self.assertEqual(first["task_id"], replay["task_id"])
            self.assertTrue(archived_replay["idempotent_replay"])
            self.assertEqual(first["task_id"], archived_replay["task_id"])
            self.assertEqual("archived", native["status"])
            self.assertEqual(1, len(result["snapshot"]["tasks"]))
            self.assertEqual(native_payload["body_text"], native["body"])
            self.assertEqual(native_payload["workspace_path"], native["workspace_path"])
        finally:
            native_store.close()
            subprocess.run(
                ["hermes", "kanban", "boards", "rm", board, "--delete"],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )


    def test_native_port_rolls_back_failed_readback_on_disposable_board(self) -> None:
        board = f"m9-rollback-{uuid.uuid4().hex[:12]}"
        subprocess.run(
            ["hermes", "kanban", "boards", "create", board],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        native_store = SQLiteArtifactStore.open(self.root / "rollback-artifacts.sqlite3")
        try:
            native_payload = dict(self.payload, board=board)
            native_record_payload = self.accepted_payload(native_payload)
            native_record = ArtifactRecord(
                artifact_id="artifact-native-triage-rollback",
                run_id="run-native-materialization-rollback",
                step_id="triage_materialize",
                attempt=1,
                executor_kind="materialize",
                input_sha256=canonical_json_sha256(self.input_value),
                output_sha256=canonical_json_sha256(native_record_payload),
                timeout_seconds=300,
                validation_result="accepted",
                executor_identity="controller-triage-materialize",
                payload=native_record_payload,
            )
            native_store.put(native_record, input_value=self.input_value)
            request = {
                "schema": "controller-kanban-materialization-v1",
                "artifact_id": native_record.artifact_id,
                "artifact_sha256": native_record.output_sha256,
                "board": board,
                "issue_number": native_payload["issue_number"],
                "generation": native_payload["generation"],
                "workspace_path": native_payload["workspace_path"],
                "expected_snapshot_fingerprint": fingerprint_native_snapshot(
                    {"board": board, "tasks": []}
                ),
            }
            request_file = self.root / "rollback-request.json"
            request_file.write_text(
                json.dumps(request, ensure_ascii=False), encoding="utf-8"
            )
            hermes_executable = shutil.which("hermes")
            self.assertIsNotNone(hermes_executable)
            runtime_python = Path(str(hermes_executable)).resolve().parent / (
                "python.exe" if sys.platform == "win32" else "python"
            )
            completed = subprocess.run(
                [
                    str(runtime_python),
                    str(_ROOT / "tests" / "controller_native_materialization_worker.py"),
                    str(_ROOT),
                    str(self.root / "rollback-artifacts.sqlite3"),
                    str(request_file),
                    "rollback",
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            result = json.loads(completed.stdout)
            self.assertEqual("CONFLICTING_REPLAY", result["error_code"])
            self.assertEqual("archived", result["task"]["status"])
            self.assertEqual(
                [],
                [
                    task
                    for task in result["active_snapshot"]["tasks"]
                    if task["status"] != "archived"
                ],
            )
        finally:
            native_store.close()
            subprocess.run(
                ["hermes", "kanban", "boards", "rm", board, "--delete"],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )

    def test_native_port_rolls_back_post_commit_integrity_failure(self) -> None:
        self._assert_native_post_commit_failure_rolls_back("post-commit-failure")

    def test_native_port_preserves_rollback_identity_when_close_also_fails(self) -> None:
        self._assert_native_post_commit_failure_rolls_back("post-commit-close-failure")

    def _assert_native_post_commit_failure_rolls_back(self, mode: str) -> None:
        board = f"m9-{mode}-{uuid.uuid4().hex[:12]}"
        subprocess.run(
            ["hermes", "kanban", "boards", "create", board],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        store_path = self.root / f"{mode}-artifacts.sqlite3"
        native_store = SQLiteArtifactStore.open(store_path)
        try:
            native_payload = dict(self.payload, board=board)
            native_record_payload = self.accepted_payload(native_payload)
            native_record = ArtifactRecord(
                artifact_id=f"artifact-native-triage-{mode}",
                run_id=f"run-native-materialization-{mode}",
                step_id="triage_materialize",
                attempt=1,
                executor_kind="materialize",
                input_sha256=canonical_json_sha256(self.input_value),
                output_sha256=canonical_json_sha256(native_record_payload),
                timeout_seconds=300,
                validation_result="accepted",
                executor_identity="controller-triage-materialize",
                payload=native_record_payload,
            )
            native_store.put(native_record, input_value=self.input_value)
            request = {
                "schema": "controller-kanban-materialization-v1",
                "artifact_id": native_record.artifact_id,
                "artifact_sha256": native_record.output_sha256,
                "board": board,
                "issue_number": native_payload["issue_number"],
                "generation": native_payload["generation"],
                "workspace_path": native_payload["workspace_path"],
                "expected_snapshot_fingerprint": fingerprint_native_snapshot(
                    {"board": board, "tasks": []}
                ),
            }
            request_file = self.root / f"{mode}-request.json"
            request_file.write_text(
                json.dumps(request, ensure_ascii=False), encoding="utf-8"
            )
            hermes_executable = shutil.which("hermes")
            self.assertIsNotNone(hermes_executable)
            runtime_python = Path(str(hermes_executable)).resolve().parent / (
                "python.exe" if sys.platform == "win32" else "python"
            )
            completed = subprocess.run(
                [
                    str(runtime_python),
                    str(_ROOT / "tests" / "controller_native_materialization_worker.py"),
                    str(_ROOT),
                    str(store_path),
                    str(request_file),
                    mode,
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(1, len(result["snapshot"]["tasks"]))
            self.assertEqual("archived", result["snapshot"]["tasks"][0]["status"])
        finally:
            native_store.close()
            subprocess.run(
                ["hermes", "kanban", "boards", "rm", board, "--delete"],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )


if __name__ == "__main__":
    unittest.main()
