"""Contract tests for controller-owned native task activation."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
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
from hermes_langchain.controller.kanban_activation import (
    AcceptedTaskActivator,
    HermesNativeKanbanActivationPort,
    KanbanActivationError,
    fingerprint_activation_snapshot,
)


class FakeActivationPort:
    def __init__(self, task: dict) -> None:
        self.task = dict(task)
        self.promotions: list[dict] = []
        self.activation_keys: set[str] = set()
        self.dependency_promoted = False

    def snapshot(self, *, board: str) -> dict:
        return {"board": board, "tasks": [dict(self.task)], "links": []}

    def promote_task(
        self,
        *,
        board: str,
        task_id: str,
        reason: str,
        expected_snapshot_fingerprint: str,
        expected_task: dict | None = None,
        activation_key: str | None = None,
        artifact_id: str | None = None,
        artifact_sha256: str | None = None,
    ) -> dict:
        del expected_task, artifact_id, artifact_sha256
        if activation_key in self.activation_keys:
            if self.task["status"] in {"ready", "running", "review", "done"}:
                return {
                    "task_id": task_id,
                    "previous_status": self.task["status"],
                    "current_status": self.task["status"],
                    "promoted": False,
                }
            raise KanbanActivationError(
                "CONFLICTING_REPLAY",
                "activation has a conflicting replay",
            )
        if self.task["status"] == "ready" and self.dependency_promoted:
            self.activation_keys.add(activation_key)
            return {
                "task_id": task_id,
                "previous_status": "ready",
                "current_status": "ready",
                "promoted": False,
                "attested": True,
            }
        if self.task["status"] in {"ready", "running", "review", "done"}:
            raise RuntimeError("conflicting replay")
        if fingerprint_activation_snapshot(self.snapshot(board=board)) != expected_snapshot_fingerprint:
            raise RuntimeError("snapshot changed")
        self.promotions.append(
            {"board": board, "task_id": task_id, "reason": reason}
        )
        previous_status = self.task["status"]
        self.task["status"] = "ready"
        if activation_key is not None:
            self.activation_keys.add(activation_key)
        return {
            "task_id": task_id,
            "previous_status": previous_status,
            "current_status": "ready",
            "promoted": True,
        }

    def has_activation_receipt(
        self,
        *,
        board: str,
        task_id: str,
        activation_key: str,
    ) -> bool:
        del board, task_id
        return activation_key in self.activation_keys

    def has_dependency_promotion(self, *, board: str, task_id: str) -> bool:
        del board, task_id
        return self.dependency_promoted

    def read_task(self, *, board: str, task_id: str) -> dict:
        del board
        if task_id != self.task["id"]:
            raise KeyError(task_id)
        return dict(self.task)


@dataclass
class NativeTask:
    id: str
    title: str
    body: str
    assignee: str
    status: str
    workspace_kind: str
    workspace_path: str
    skills: list[str]
    idempotency_key: str


class NativeResult:
    def __init__(self, rows: list[dict] | None = None, *, rowcount: int = 0) -> None:
        self._rows = rows or []
        self.rowcount = rowcount

    def fetchall(self) -> list[dict]:
        return list(self._rows)


class NativeConnection:
    def __init__(self, native: "FakeNativeKanban") -> None:
        self.native = native
        self.closed = False

    def execute(self, sql: str, params: tuple = ()) -> NativeResult:
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT parent_id, child_id FROM task_links"):
            return NativeResult(list(self.native.links))
        if normalized.startswith("SELECT payload FROM task_events"):
            task_id = params[0]
            return NativeResult(
                [
                    {"payload": json.dumps(event["payload"])}
                    for event in reversed(self.native.events)
                    if event["task_id"] == task_id
                ]
            )
        if normalized.startswith("SELECT t.id, t.status FROM tasks t"):
            return NativeResult([])
        if normalized.startswith("UPDATE tasks SET status = 'ready'"):
            self.assert_write_locked()
            task_id = params[0]
            if self.native.task.id != task_id or self.native.task.status not in {"todo", "blocked"}:
                return NativeResult(rowcount=0)
            self.native.task.status = "ready"
            return NativeResult(rowcount=1)
        raise AssertionError(f"unexpected SQL: {normalized}")

    def assert_write_locked(self) -> None:
        if not self.native.in_write_txn:
            raise AssertionError("activation mutation was not write-locked")

    def close(self) -> None:
        self.closed = True


class FakeNativeKanban:
    def __init__(self, task: NativeTask) -> None:
        self.task = task
        self.links: list[dict] = []
        self.events: list[dict] = []
        self.in_write_txn = False
        self.mutate_before_lock_check = None

    def list_tasks(self, _conn: NativeConnection, *, include_archived: bool) -> list[NativeTask]:
        self.assert_include_archived = include_archived
        return [self.task]

    def get_task(self, _conn: NativeConnection, task_id: str) -> NativeTask | None:
        return self.task if task_id == self.task.id else None

    @contextmanager
    def write_txn(self, _conn: NativeConnection):
        self.in_write_txn = True
        try:
            if self.mutate_before_lock_check is not None:
                self.mutate_before_lock_check()
                self.mutate_before_lock_check = None
            yield
        finally:
            self.in_write_txn = False

    def _append_event(
        self,
        conn: NativeConnection,
        task_id: str,
        kind: str,
        payload: dict,
    ) -> None:
        conn.assert_write_locked()
        self.events.append({"task_id": task_id, "kind": kind, "payload": dict(payload)})


class ControllerKanbanActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.store = SQLiteArtifactStore.open(self.root / "artifacts.sqlite3")
        self.task = {
            "id": "t_impl_1",
            "title": "G2-Issue42-Impl-1",
            "body": '{"schema":"backend-implementation-card-v6"}\n',
            "assignee": "coder",
            "status": "todo",
            "workspace_kind": "dir",
            "workspace_path": self.workspace.as_posix(),
            "skills": ["kwc-impl-authoring-runtime"],
            "idempotency_key": "controller-impl-1",
        }
        self.port = FakeActivationPort(self.task)
        self.payload = {
            "schema": "controller-task-activation-v1",
            "board": "disposable-controller-activation",
            "issue_number": 42,
            "generation": 2,
            "workspace_path": self.workspace.as_posix(),
            "reason_code": "accepted_graph_root",
            "task": {
                "task_id": self.task["id"],
                "title": self.task["title"],
                "body_sha256": hashlib.sha256(
                    self.task["body"].encode("utf-8")
                ).hexdigest(),
                "assignee": self.task["assignee"],
                "status": "todo",
                "workspace_kind": self.task["workspace_kind"],
                "workspace_path": self.task["workspace_path"],
                "skills": self.task["skills"],
                "idempotency_key": self.task["idempotency_key"],
            },
        }
        self.record = ArtifactRecord(
            artifact_id="artifact-activation-1",
            run_id="run-activation-1",
            step_id="activate_impl_root",
            attempt=1,
            executor_kind="deterministic",
            input_sha256=canonical_json_sha256({"accepted": "graph-root"}),
            output_sha256=canonical_json_sha256(self.payload),
            timeout_seconds=30,
            validation_result="accepted",
            executor_identity="controller-task-activation",
            payload=self.payload,
        )
        self.store.put(self.record, input_value={"accepted": "graph-root"})

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def request(self) -> dict:
        return {
            "schema": "controller-kanban-activation-v1",
            "artifact_id": self.record.artifact_id,
            "artifact_sha256": self.record.output_sha256,
            "board": self.payload["board"],
            "issue_number": self.payload["issue_number"],
            "generation": self.payload["generation"],
            "workspace_path": self.payload["workspace_path"],
            "task_id": self.task["id"],
            "expected_snapshot_fingerprint": fingerprint_activation_snapshot(
                self.port.snapshot(board=self.payload["board"])
            ),
        }

    def activator(self) -> AcceptedTaskActivator:
        return AcceptedTaskActivator(
            artifact_store=self.store,
            kanban_port=self.port,
        )

    def test_snapshot_fencing_and_exact_readback(self) -> None:
        receipt = self.activator().activate(self.request())

        self.assertEqual(
            self.port.promotions,
            [
                {
                    "board": self.payload["board"],
                    "task_id": self.task["id"],
                    "reason": self.payload["reason_code"],
                }
            ],
        )
        self.assertTrue(receipt["verified"])
        self.assertFalse(receipt["idempotent_replay"])
        self.assertEqual(receipt["task_id"], self.task["id"])
        self.assertEqual(receipt["previous_status"], "todo")
        self.assertEqual(receipt["current_status"], "ready")
        self.assertNotIn("body", receipt)
        self.assertNotIn("artifact", receipt)

        self.port = FakeActivationPort(self.task)
        stale = self.request()
        self.port.task["title"] = "drifted before activation"
        with self.assertRaisesRegex(KanbanActivationError, "snapshot changed"):
            self.activator().activate(stale)
        self.assertEqual([], self.port.promotions)

        class TamperedReadbackPort(FakeActivationPort):
            def read_task(self, *, board: str, task_id: str) -> dict:
                task = super().read_task(board=board, task_id=task_id)
                task["body"] += "\n"
                return task

        self.port = TamperedReadbackPort(self.task)
        with self.assertRaisesRegex(KanbanActivationError, "accepted artifact"):
            self.activator().activate(self.request())

    def test_contract_rejects_untrusted_or_mismatched_input(self) -> None:
        unknown = dict(self.request(), unexpected=True)
        with self.assertRaisesRegex(KanbanActivationError, "request"):
            self.activator().activate(unknown)

        mismatch = dict(self.request(), generation=3)
        with self.assertRaisesRegex(KanbanActivationError, "identity"):
            self.activator().activate(mismatch)

        invalid_payload = dict(self.payload, reason_code="free form reason with spaces")
        invalid_record = ArtifactRecord(
            artifact_id="artifact-activation-invalid-reason",
            run_id="run-activation-invalid-reason",
            step_id="activate_impl_root",
            attempt=1,
            executor_kind="deterministic",
            input_sha256=canonical_json_sha256({"accepted": "invalid-root"}),
            output_sha256=canonical_json_sha256(invalid_payload),
            timeout_seconds=30,
            validation_result="accepted",
            executor_identity="controller-task-activation",
            payload=invalid_payload,
        )
        self.store.put(invalid_record, input_value={"accepted": "invalid-root"})
        invalid_request = dict(
            self.request(),
            artifact_id=invalid_record.artifact_id,
            artifact_sha256=invalid_record.output_sha256,
        )
        with self.assertRaisesRegex(KanbanActivationError, "reason"):
            self.activator().activate(invalid_request)

        untrusted_record = ArtifactRecord(
            artifact_id="artifact-activation-untrusted-producer",
            run_id="run-activation-untrusted-producer",
            step_id="activate_impl_root",
            attempt=1,
            executor_kind="deterministic",
            input_sha256=canonical_json_sha256({"accepted": "untrusted-root"}),
            output_sha256=canonical_json_sha256(self.payload),
            timeout_seconds=30,
            validation_result="accepted",
            executor_identity="untrusted-artifact-producer",
            payload=self.payload,
        )
        self.store.put(
            untrusted_record,
            input_value={"accepted": "untrusted-root"},
        )
        untrusted_request = dict(
            self.request(),
            artifact_id=untrusted_record.artifact_id,
            artifact_sha256=untrusted_record.output_sha256,
        )
        with self.assertRaisesRegex(KanbanActivationError, "producer"):
            self.activator().activate(untrusted_request)

        self.assertEqual([], self.port.promotions)

    def test_artifact_rejects_boolean_or_float_numeric_identities(self) -> None:
        cases = (
            ("issue_number", 42.0, 42),
            ("generation", 2.0, 2),
            ("issue_number", True, 1),
            ("generation", True, 1),
        )
        for index, (field, artifact_value, request_value) in enumerate(cases):
            with self.subTest(field=field, value=artifact_value):
                payload = dict(self.payload, **{field: artifact_value})
                record = ArtifactRecord(
                    artifact_id=f"artifact-invalid-numeric-{index}",
                    run_id=f"run-invalid-numeric-{index}",
                    step_id="activate_impl_root",
                    attempt=1,
                    executor_kind="deterministic",
                    input_sha256=canonical_json_sha256({"case": index}),
                    output_sha256=canonical_json_sha256(payload),
                    timeout_seconds=30,
                    validation_result="accepted",
                    executor_identity="controller-task-activation",
                    payload=payload,
                )
                contract = dict(
                    self.request(),
                    artifact_id=record.artifact_id,
                    artifact_sha256=record.output_sha256,
                    **{field: request_value},
                )
                with self.assertRaisesRegex(KanbanActivationError, "artifact"):
                    AcceptedTaskActivator._payload(record, contract)

    def test_exact_replay_is_idempotent(self) -> None:
        request = self.request()
        first = self.activator().activate(request)
        replay = self.activator().activate(request)

        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(first["task_id"], replay["task_id"])
        self.assertRegex(first["activation_key"], r"^[0-9a-f]{64}$")
        self.assertEqual(first["activation_key"], replay["activation_key"])
        self.assertEqual(1, len(self.port.promotions))

        self.port.task["title"] = "conflicting replay"
        with self.assertRaisesRegex(KanbanActivationError, "conflicting replay"):
            self.activator().activate(request)
        self.assertEqual(1, len(self.port.promotions))

    def test_out_of_band_progress_is_not_an_exact_replay(self) -> None:
        request = self.request()
        self.port.task["status"] = "ready"

        with self.assertRaisesRegex(KanbanActivationError, "conflicting replay"):
            self.activator().activate(request)

        self.assertEqual([], self.port.promotions)

    def test_dependency_promoted_ready_task_can_be_attested_once(self) -> None:
        request = self.request()
        self.port.task["status"] = "ready"
        self.port.dependency_promoted = True

        first = self.activator().activate(request)
        replay = self.activator().activate(request)

        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual("ready", first["current_status"])
        self.assertEqual([], self.port.promotions)

    def test_replay_with_changed_original_snapshot_is_a_conflict(self) -> None:
        request = self.request()
        first = self.activator().activate(request)
        self.assertTrue(first["verified"])

        changed = dict(request, expected_snapshot_fingerprint="f" * 64)
        with self.assertRaisesRegex(KanbanActivationError, "conflicting replay"):
            self.activator().activate(changed)

        self.assertEqual(1, len(self.port.promotions))

    def test_retry_after_task_returns_to_original_status_does_not_repromote(self) -> None:
        request = self.request()
        first = self.activator().activate(request)
        self.assertFalse(first["idempotent_replay"])
        self.port.task["status"] = self.payload["task"]["status"]

        with self.assertRaisesRegex(KanbanActivationError, "conflicting replay"):
            self.activator().activate(request)

        self.assertEqual(1, len(self.port.promotions))

    def test_snapshot_fingerprint_includes_dependency_links(self) -> None:
        snapshot = self.port.snapshot(board=self.payload["board"])
        changed = dict(snapshot, links=[{"parent_id": "t_parent", "child_id": self.task["id"]}])

        self.assertNotEqual(
            fingerprint_activation_snapshot(snapshot),
            fingerprint_activation_snapshot(changed),
        )

    def test_request_rejects_unbounded_or_noncanonical_identities(self) -> None:
        request = self.request()
        cases = (
            dict(request, artifact_id="a" * 129),
            dict(request, board="Invalid Board"),
            dict(request, task_id=" t_impl_1"),
            dict(request, workspace_path="relative/workspace"),
        )
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaisesRegex(KanbanActivationError, "request"):
                    AcceptedTaskActivator._request(case)

    def test_native_port_rechecks_snapshot_before_canonical_promotion(self) -> None:
        native_task = NativeTask(**self.task)
        native = FakeNativeKanban(native_task)
        connections: list[NativeConnection] = []

        def connect(**_kwargs) -> NativeConnection:
            connection = NativeConnection(native)
            connections.append(connection)
            return connection

        port = HermesNativeKanbanActivationPort(
            kanban_db_module=native,
            connect_fn=connect,
        )
        snapshot = port.snapshot(board=self.payload["board"])
        receipt = port.promote_task(
            board=self.payload["board"],
            task_id=self.task["id"],
            reason=self.payload["reason_code"],
            expected_snapshot_fingerprint=fingerprint_activation_snapshot(snapshot),
            expected_task=self.payload["task"],
            activation_key="a" * 64,
            artifact_id="artifact-activation-1",
            artifact_sha256=self.record.output_sha256,
        )

        self.assertEqual(receipt["current_status"], "ready")
        self.assertEqual(
            native.events,
            [
                {
                    "task_id": self.task["id"],
                    "kind": "promoted_manual",
                    "payload": {
                        "actor": "hermes-langchain-controller",
                        "reason": self.payload["reason_code"],
                        "controller_activation_key": "a" * 64,
                        "artifact_id": "artifact-activation-1",
                        "artifact_sha256": self.record.output_sha256,
                        "expected_snapshot_fingerprint": fingerprint_activation_snapshot(snapshot),
                    },
                }
            ],
        )
        self.assertTrue(all(connection.closed for connection in connections))

        native.task.status = "todo"
        with self.assertRaisesRegex(KanbanActivationError, "conflicting replay"):
            port.promote_task(
                board=self.payload["board"],
                task_id=self.task["id"],
                reason=self.payload["reason_code"],
                expected_snapshot_fingerprint=fingerprint_activation_snapshot(snapshot),
                expected_task=self.payload["task"],
                activation_key="a" * 64,
                artifact_id="artifact-activation-1",
                artifact_sha256=self.record.output_sha256,
            )
        self.assertEqual("todo", native.task.status)
        self.assertEqual(1, len(native.events))

        native.task.status = "ready"
        stale_snapshot = port.snapshot(board=self.payload["board"])
        native.task.status = "blocked"
        with self.assertRaisesRegex(KanbanActivationError, "snapshot changed"):
            port.promote_task(
                board=self.payload["board"],
                task_id=self.task["id"],
                reason=self.payload["reason_code"],
                expected_snapshot_fingerprint=fingerprint_activation_snapshot(
                    stale_snapshot
                ),
                expected_task=self.payload["task"],
                activation_key="b" * 64,
                artifact_id="artifact-activation-1",
                artifact_sha256=self.record.output_sha256,
            )
        self.assertEqual(1, len(native.events))

        native.task.status = "todo"
        native.task.title = self.task["title"]
        boundary_snapshot = port.snapshot(board=self.payload["board"])
        native.mutate_before_lock_check = lambda: setattr(native.task, "title", "drifted")
        with self.assertRaisesRegex(KanbanActivationError, "snapshot changed"):
            port.promote_task(
                board=self.payload["board"],
                task_id=self.task["id"],
                reason=self.payload["reason_code"],
                expected_snapshot_fingerprint=fingerprint_activation_snapshot(
                    boundary_snapshot
                ),
                expected_task=self.payload["task"],
                activation_key="c" * 64,
                artifact_id="artifact-activation-1",
                artifact_sha256=self.record.output_sha256,
            )
        self.assertEqual("todo", native.task.status)
        self.assertEqual(1, len(native.events))

    def test_native_port_activates_and_reads_back_disposable_board(self) -> None:
        board = f"m11-disposable-{uuid.uuid4().hex[:12]}"
        subprocess.run(
            ["hermes", "kanban", "boards", "create", board],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        try:
            body = '{"schema":"backend-implementation-card-v6"}'
            idempotency_key = f"m11-activation-{uuid.uuid4().hex}"
            created = subprocess.run(
                [
                    "hermes",
                    "kanban",
                    "--board",
                    board,
                    "create",
                    "G2-Issue42-Impl-1",
                    "--body",
                    body,
                    "--assignee",
                    "coder",
                    "--workspace",
                    f"dir:{self.workspace.as_posix()}",
                    "--initial-status",
                    "blocked",
                    "--idempotency-key",
                    idempotency_key,
                    "--json",
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            native_task = json.loads(created.stdout)
            payload = {
                "schema": "controller-task-activation-v1",
                "board": board,
                "issue_number": 42,
                "generation": 2,
                "workspace_path": native_task["workspace_path"],
                "reason_code": "accepted_graph_root",
                "task": {
                    "task_id": native_task["id"],
                    "title": native_task["title"],
                    "body_sha256": hashlib.sha256(
                        native_task["body"].encode("utf-8")
                    ).hexdigest(),
                    "assignee": native_task["assignee"],
                    "status": native_task["status"],
                    "workspace_kind": native_task["workspace_kind"],
                    "workspace_path": native_task["workspace_path"],
                    "skills": native_task["skills"],
                    "idempotency_key": idempotency_key,
                },
            }
            store_path = self.root / "native-activation-artifacts.sqlite3"
            native_store = SQLiteArtifactStore.open(store_path)
            try:
                record = ArtifactRecord(
                    artifact_id="artifact-native-activation",
                    run_id="run-native-activation",
                    step_id="activate_impl_root",
                    attempt=1,
                    executor_kind="deterministic",
                    input_sha256=canonical_json_sha256({"accepted": "graph-root"}),
                    output_sha256=canonical_json_sha256(payload),
                    timeout_seconds=30,
                    validation_result="accepted",
                    executor_identity="controller-task-activation",
                    payload=payload,
                )
                native_store.put(record, input_value={"accepted": "graph-root"})
            finally:
                native_store.close()
            request = {
                "schema": "controller-kanban-activation-v1",
                "artifact_id": record.artifact_id,
                "artifact_sha256": record.output_sha256,
                "board": board,
                "issue_number": payload["issue_number"],
                "generation": payload["generation"],
                "workspace_path": payload["workspace_path"],
                "task_id": native_task["id"],
                "expected_snapshot_fingerprint": "0" * 64,
            }
            request_file = self.root / "native-activation-request.json"
            request_file.write_text(
                json.dumps(request, ensure_ascii=False),
                encoding="utf-8",
            )
            hermes_executable = shutil.which("hermes")
            self.assertIsNotNone(hermes_executable)
            runtime_python = Path(str(hermes_executable)).resolve().parent / (
                "python.exe" if sys.platform == "win32" else "python"
            )
            completed = subprocess.run(
                [
                    str(runtime_python),
                    str(_ROOT / "tests" / "controller_native_activation_worker.py"),
                    str(_ROOT),
                    str(store_path),
                    str(request_file),
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            result = json.loads(completed.stdout)

            self.assertTrue(result["first"]["verified"])
            self.assertFalse(result["first"]["idempotent_replay"])
            self.assertTrue(result["replay"]["idempotent_replay"])
            self.assertEqual("ready", result["task"]["status"])
            self.assertEqual(native_task["id"], result["task"]["id"])
            self.assertEqual(1, len(result["snapshot"]["tasks"]))
        finally:
            subprocess.run(
                ["hermes", "kanban", "boards", "rm", board, "--delete"],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )


if __name__ == "__main__":
    unittest.main()
