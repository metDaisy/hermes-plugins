"""Contract tests for controller-owned native dispatch claims."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hermes_langchain.controller.artifact_store import (
    ArtifactRecord,
    ArtifactStoreError,
    SQLiteArtifactStore,
    canonical_json_sha256,
)
from hermes_langchain.controller.kanban_activation import fingerprint_activation_snapshot
from hermes_langchain.controller.kanban_materialization import accepted_triage_idempotency_key
from hermes_langchain.controller.kanban_dispatch import (
    AcceptedTaskDispatcher,
    KanbanDispatchError,
)
from hermes_langchain.controller.kanban_agent_delivery import (
    AcceptedTaskAgentInvoker,
    KanbanAgentDeliveryError,
)
from hermes_langchain.controller.kanban_completion import (
    AcceptedTaskCompleter,
    KanbanCompletionError,
)
from hermes_langchain.controller.runtime import AgentResult


class FakeDispatchPort:
    def __init__(self, snapshot: dict) -> None:
        self.value = json.loads(json.dumps(snapshot))
        self.claims = 0
        self.receipt: dict | None = None

    def snapshot(self, *, board: str) -> dict:
        assert board == self.value["board"]
        return json.loads(json.dumps(self.value))

    def claim_task(self, **kwargs) -> dict:
        if self.receipt is not None:
            return {**self.receipt, "claimed": False}
        if fingerprint_activation_snapshot(self.value) != kwargs["expected_snapshot_fingerprint"]:
            raise KanbanDispatchError("SNAPSHOT_CHANGED", "snapshot changed")
        self.claims += 1
        task = next(item for item in self.value["tasks"] if item["id"] == kwargs["task_id"])
        task["status"] = "running"
        self.receipt = {
            "task_id": kwargs["task_id"],
            "run_id": 41,
            "previous_status": "ready",
            "current_status": "running",
            "dispatch_key": kwargs["dispatch_key"],
            "claim_lock": f"controller:{kwargs['dispatch_key']}",
            "activation_key": kwargs["activation_key"],
            "graph_artifact_id": kwargs["graph_artifact_id"],
            "graph_artifact_sha256": kwargs["graph_artifact_sha256"],
            "activation_artifact_id": kwargs["activation_artifact_id"],
            "activation_artifact_sha256": kwargs["activation_artifact_sha256"],
            "claimed": True,
        }
        return dict(self.receipt)

    def read_claim(self, **kwargs) -> dict:
        if self.receipt is None or self.receipt["dispatch_key"] != kwargs["dispatch_key"]:
            raise KanbanDispatchError("NATIVE_CLAIM_NOT_FOUND", "claim missing")
        return {
            **self.receipt,
            "claimed": False,
            "task": next(
                item for item in self.value["tasks"] if item["id"] == self.receipt["task_id"]
            ),
        }


class FakeAgentExecutor:
    def __init__(self, *, invalid: bool = False) -> None:
        self.invalid = invalid
        self.calls = []
        self.acknowledgements = []

    def invoke(self, invocation):
        self.calls.append(invocation)
        claim = invocation.input["native_claim"]
        result_payload = {"schema": "implementation-result-v1", "commit": "abc123"}
        output = {
            "schema": "controller-task-agent-delivery-v1",
            "status": "completed",
            "task_id": claim["task_id"],
            "run_id": claim["run_id"],
            "dispatch_key": claim["dispatch_key"],
            "result_artifacts": [
                {
                    "artifact_id": "implementation-result",
                    "step_id": "implementation_delivery",
                    "sha256": canonical_json_sha256(result_payload),
                    "payload": result_payload,
                }
            ],
        }
        if self.invalid:
            output["run_id"] += 1
        return AgentResult(agent_id="agent-m13-1", output=output)

    def acknowledge_persisted_result(self, invocation_id: str) -> None:
        self.acknowledgements.append(invocation_id)


class FakeClaimLeasePort:
    def __init__(self, *, lose_after: int | None = None) -> None:
        self.lose_after = lose_after
        self.calls = []

    def heartbeat(self, **kwargs) -> bool:
        self.calls.append(dict(kwargs))
        return self.lose_after is None or len(self.calls) <= self.lose_after


class FakeCompletionPort:
    def __init__(self, dispatch_port: FakeDispatchPort) -> None:
        self.dispatch_port = dispatch_port
        self.completions = 0
        self.receipt = None
        self.last_completion = None

    def snapshot(self, *, board: str) -> dict:
        return self.dispatch_port.snapshot(board=board)

    def complete_task(self, **kwargs) -> dict:
        if self.receipt is not None:
            return {**self.receipt, "completed": False}
        self.last_completion = dict(kwargs)
        if fingerprint_activation_snapshot(self.dispatch_port.value) != kwargs[
            "expected_snapshot_fingerprint"
        ]:
            raise KanbanCompletionError("SNAPSHOT_CHANGED", "snapshot changed")
        task = next(
            item
            for item in self.dispatch_port.value["tasks"]
            if item["id"] == kwargs["task_id"]
        )
        if task["status"] != "running" or kwargs["run_id"] != self.dispatch_port.receipt[
            "run_id"
        ]:
            raise KanbanCompletionError("NATIVE_COMPLETION_FAILED", "claim mismatch")
        task["status"] = "done"
        task["result"] = f"Accepted controller delivery artifact {kwargs['delivery_artifact_id']}"
        task["claim_lock"] = None
        task["claim_expires"] = None
        task["worker_pid"] = None
        self.completions += 1
        self.receipt = {
            "task_id": kwargs["task_id"],
            "run_id": kwargs["run_id"],
            "completion_key": kwargs["completion_key"],
            "task_status": "done",
            "run_status": "done",
            "run_outcome": "completed",
            "dispatch_artifact_id": kwargs["dispatch_artifact_id"],
            "dispatch_artifact_sha256": kwargs["dispatch_artifact_sha256"],
            "delivery_artifact_id": kwargs["delivery_artifact_id"],
            "delivery_artifact_sha256": kwargs["delivery_artifact_sha256"],
            "result_artifacts": kwargs["result_artifacts"],
            "completed": True,
        }
        return dict(self.receipt)

    def read_completion(self, **kwargs) -> dict:
        if self.receipt is None or self.receipt["completion_key"] != kwargs["completion_key"]:
            raise KanbanCompletionError("NATIVE_COMPLETION_NOT_FOUND", "completion missing")
        task = next(
            item
            for item in self.dispatch_port.value["tasks"]
            if item["id"] == kwargs["task_id"]
        )
        expected = kwargs["expected_task"]
        if (
            task.get("status") != "done"
            or task.get("title") != expected.get("title")
            or hashlib.sha256(str(task.get("body") or "").encode()).hexdigest()
            != expected.get("body_sha256")
            or task.get("assignee") != expected.get("assignee")
            or task.get("workspace_kind") != expected.get("workspace_kind")
            or task.get("workspace_path") != expected.get("workspace_path")
            or list(task.get("skills") or []) != list(expected.get("skills") or [])
            or task.get("idempotency_key") != expected.get("idempotency_key")
            or task.get("result") != kwargs["expected_result_text"]
            or task.get("claim_lock") is not None
            or task.get("claim_expires") is not None
            or task.get("worker_pid") is not None
        ):
            raise KanbanCompletionError(
                "NATIVE_READBACK_MISMATCH", "terminal task provenance mismatch"
            )
        return {**self.receipt, "completed": False}


class ControllerKanbanDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.store = SQLiteArtifactStore.open(self.workspace / "artifacts.db")
        self.addCleanup(self.store.close)
        self.result_store = SQLiteArtifactStore.open(self.workspace / "result-artifacts.db")
        self.addCleanup(self.result_store.close)
        self.hydration_root = self.workspace / "invocations"
        self.claim_lease = FakeClaimLeasePort()
        self.body = json.dumps(
            {"schema": "backend-implementation-card-v6"},
            sort_keys=True,
            separators=(",", ":"),
        )
        self.graph_payload = {
            "schema": "controller-task-graph-v1",
            "board": "m13-test",
            "issue_number": 151,
            "generation": 4,
            "workspace_path": self.workspace.as_posix(),
            "source_task": {
                "task_id": "t-triage",
                "title": "G4-Issue151-Triage",
                "body_sha256": "a" * 64,
                "status": "triage",
                "assignee": "project-manager",
                "workspace_kind": "dir",
                "workspace_path": self.workspace.as_posix(),
                "skills": [],
                "idempotency_key": accepted_triage_idempotency_key(
                    board="m13-test",
                    issue_number=151,
                    generation=4,
                    artifact_sha256="b" * 64,
                ),
                "accepted_artifact_id": "triage-artifact",
                "accepted_artifact_sha256": "b" * 64,
            },
            "activation_candidate_key": "impl-1",
            "cards": [
                {
                    "key": "impl-1",
                    "role": "implementation",
                    "title": "G4-Issue151-Impl1",
                    "body_text": self.body,
                    "body_sha256": hashlib.sha256(self.body.encode()).hexdigest(),
                    "assignee": "coder",
                    "skills": [],
                },
                {
                    "key": "review-1",
                    "role": "review",
                    "title": "G4-Issue151-Review",
                    "body_text": json.dumps({"schema": "aggregate-review-card-v5"}),
                    "body_sha256": hashlib.sha256(
                        json.dumps({"schema": "aggregate-review-card-v5"}).encode()
                    ).hexdigest(),
                    "assignee": "reviewer",
                    "skills": [],
                },
                {
                    "key": "summary-1",
                    "role": "summary",
                    "title": "G4-Issue151-Summary",
                    "body_text": json.dumps({"schema": "issue-summary-card-v3"}),
                    "body_sha256": hashlib.sha256(
                        json.dumps({"schema": "issue-summary-card-v3"}).encode()
                    ).hexdigest(),
                    "assignee": "project-manager",
                    "skills": [],
                },
            ],
            "links": [
                {"parent_key": "source", "child_key": "impl-1"},
                {"parent_key": "impl-1", "child_key": "review-1"},
                {"parent_key": "impl-1", "child_key": "summary-1"},
                {"parent_key": "review-1", "child_key": "summary-1"},
            ],
        }
        self.graph_record = self._put(
            artifact_id="graph-artifact",
            run_id="graph-run",
            step_id="task_graph_materialize",
            executor_identity="controller-task-graph-projection-v1",
            payload=self.graph_payload,
        )
        key_digest = hashlib.sha256(b"impl-1").hexdigest()[:16]
        task_key = (
            f"controller-graph:151:4:{self.graph_record.output_sha256[:16]}:{key_digest}"
        )
        self.activation_payload = {
            "schema": "controller-task-activation-v1",
            "board": "m13-test",
            "issue_number": 151,
            "generation": 4,
            "workspace_path": self.workspace.as_posix(),
            "reason_code": "accepted_graph_candidate",
            "task": {
                "task_id": "t-impl-1",
                "title": "G4-Issue151-Impl1",
                "body_sha256": hashlib.sha256(self.body.encode()).hexdigest(),
                "assignee": "coder",
                "status": "todo",
                "workspace_kind": "dir",
                "workspace_path": self.workspace.as_posix(),
                "skills": [],
                "idempotency_key": task_key,
            },
        }
        self.activation_record = self._put(
            artifact_id="activation-artifact",
            run_id="activation-run",
            step_id="task_activation",
            executor_identity="controller-task-activation",
            payload=self.activation_payload,
        )
        self.snapshot = {
            "board": "m13-test",
            "tasks": [
                {
                    "id": "t-impl-1",
                    "title": "G4-Issue151-Impl1",
                    "body": self.body,
                    "assignee": "coder",
                    "status": "ready",
                    "workspace_kind": "dir",
                    "workspace_path": self.workspace.as_posix(),
                    "skills": [],
                    "idempotency_key": task_key,
                }
            ],
            "links": [],
        }
        self.port = FakeDispatchPort(self.snapshot)

    def _put(
        self,
        *,
        artifact_id: str,
        run_id: str,
        step_id: str,
        executor_identity: str,
        payload: dict,
    ) -> ArtifactRecord:
        input_value = {"accepted": artifact_id}
        record = ArtifactRecord(
            artifact_id=artifact_id,
            run_id=run_id,
            step_id=step_id,
            attempt=1,
            executor_kind="deterministic",
            input_sha256=canonical_json_sha256(input_value),
            output_sha256=canonical_json_sha256(payload),
            timeout_seconds=30,
            validation_result="accepted",
            executor_identity=executor_identity,
            payload=payload,
        )
        return self.store.put(record, input_value=input_value)

    def request(self) -> dict:
        return {
            "schema": "controller-kanban-dispatch-v1",
            "artifact_id": "dispatch-artifact",
            "graph_artifact_id": self.graph_record.artifact_id,
            "graph_artifact_sha256": self.graph_record.output_sha256,
            "activation_artifact_id": self.activation_record.artifact_id,
            "activation_artifact_sha256": self.activation_record.output_sha256,
            "activation_key": "c" * 64,
            "board": "m13-test",
            "issue_number": 151,
            "generation": 4,
            "workspace_path": self.workspace.as_posix(),
            "task_id": "t-impl-1",
            "expected_snapshot_fingerprint": fingerprint_activation_snapshot(self.snapshot),
        }

    def dispatcher(self) -> AcceptedTaskDispatcher:
        return AcceptedTaskDispatcher(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            kanban_port=self.port,
        )

    def test_claims_once_persists_receipt_and_replays_without_duplicate_run(self) -> None:
        first = self.dispatcher().dispatch(self.request())
        replay = self.dispatcher().dispatch(self.request())

        self.assertEqual(41, first["run_id"])
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(1, self.port.claims)
        stored = self.store.get("dispatch-artifact")
        self.assertEqual("controller-task-dispatch-receipt-v1", stored.payload["schema"])
        self.assertEqual(41, stored.payload["run_id"])

    def test_stale_snapshot_is_rejected_before_native_claim(self) -> None:
        request = self.request()
        request["expected_snapshot_fingerprint"] = "0" * 64

        with self.assertRaisesRegex(KanbanDispatchError, "snapshot"):
            self.dispatcher().dispatch(request)

        self.assertEqual(0, self.port.claims)

    def test_dispatch_readback_rejects_claim_lock_or_provenance_tampering(self) -> None:
        receipt = self.dispatcher().dispatch(self.request())
        native = self.port.read_claim(
            board="m13-test",
            task_id=receipt["task_id"],
            dispatch_key=receipt["dispatch_key"],
        )
        payload = self.store.get(receipt["artifact_id"]).payload
        expected_task = dict(self.activation_payload["task"])
        expected_task["status"] = "ready"

        for field in (
            "claim_lock",
            "activation_key",
            "graph_artifact_id",
            "graph_artifact_sha256",
            "activation_artifact_id",
            "activation_artifact_sha256",
        ):
            tampered = dict(native)
            tampered[field] = "tampered"
            with self.assertRaises(KanbanDispatchError):
                AcceptedTaskDispatcher._verify_native_claim(tampered, payload, expected_task)

    def test_restart_after_claim_before_artifact_write_reuses_native_run(self) -> None:
        class FailFirstPut:
            def __init__(self, delegate: SQLiteArtifactStore) -> None:
                self.delegate = delegate
                self.failed = False

            def get(self, artifact_id: str):
                return self.delegate.get(artifact_id)

            def put(self, record, *, input_value):
                if not self.failed:
                    self.failed = True
                    raise RuntimeError("injected post-claim artifact failure")
                return self.delegate.put(record, input_value=input_value)

        dispatcher = AcceptedTaskDispatcher(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=FailFirstPut(self.store),
            kanban_port=self.port,
        )
        with self.assertRaisesRegex(RuntimeError, "post-claim"):
            dispatcher.dispatch(self.request())

        replay = self.dispatcher().dispatch(self.request())

        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(41, replay["run_id"])
        self.assertEqual(1, self.port.claims)
        self.assertEqual(41, self.store.get("dispatch-artifact").payload["run_id"])

    def test_fresh_agent_receipt_uses_only_artifact_refs_and_replays_without_relaunch(self) -> None:
        dispatch = self.dispatcher().dispatch(self.request())
        executor = FakeAgentExecutor()
        invoker = AcceptedTaskAgentInvoker(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            hydration_root=self.hydration_root,
            claim_lease_port=self.claim_lease,
            executor=executor,
        )
        request = {
            "schema": "controller-task-agent-invocation-v1",
            "artifact_id": "delivery-artifact",
            "dispatch_artifact_id": dispatch["artifact_id"],
            "dispatch_artifact_sha256": dispatch["artifact_sha256"],
        }

        first = invoker.invoke(request)
        replay = invoker.invoke(request)

        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(1, len(executor.calls))
        self.assertEqual(
            [executor.calls[0].invocation_id],
            executor.acknowledgements,
        )
        invocation = executor.calls[0]
        self.assertEqual(self.workspace.as_posix(), invocation.working_directory)
        self.assertEqual(("kwc-implementation-delivery-runtime",), invocation.skills)
        self.assertEqual(
            ("file", "gradle-mcp", "codebase-memory-mcp", "semble-mcp"),
            invocation.allowed_toolsets,
        )
        self.assertEqual(
            {
                "schema",
                "accepted_artifacts",
                "native_claim",
                "artifact_directory",
                "predecessor_evidence",
            },
            set(invocation.input),
        )
        self.assertNotIn(self.body, json.dumps(invocation.input))
        artifact_directory = Path(invocation.input["artifact_directory"])
        hydrated_graph = json.loads((artifact_directory / "graph.json").read_text())
        self.assertEqual(self.body, hydrated_graph["cards"][0]["body_text"])
        hydrated_activation = json.loads(
            (artifact_directory / "activation.json").read_text()
        )
        hydrated_dispatch = json.loads((artifact_directory / "dispatch.json").read_text())
        manifest = json.loads((artifact_directory / "manifest.json").read_text())
        predecessors = json.loads((artifact_directory / "predecessors.json").read_text())
        self.assertEqual("t-impl-1", hydrated_activation["task"]["task_id"])
        self.assertEqual(dispatch["dispatch_key"], hydrated_dispatch["dispatch_key"])
        self.assertEqual("controller-task-agent-hydration-v1", manifest["schema"])
        self.assertEqual("kwc-implementation-delivery-runtime", manifest["skill"])
        self.assertEqual(
            {"schema": "controller-task-predecessor-evidence-v1", "parents": []},
            predecessors,
        )
        self.assertEqual(
            (_ROOT / "skills" / "kwc-implementation-delivery-runtime" / "SKILL.md").read_text(
                encoding="utf-8"
            ),
            (artifact_directory / "skill.md").read_text(encoding="utf-8"),
        )
        stored = self.store.get("delivery-artifact")
        self.assertEqual("controller-task-agent-invocation-receipt-v1", stored.payload["schema"])
        self.assertEqual("agent-m13-1", stored.payload["agent_id"])

    def test_restart_after_agent_delivery_before_final_receipt_does_not_relaunch(self) -> None:
        dispatch = self.dispatcher().dispatch(self.request())
        executor = FakeAgentExecutor()

        class FailFirstFinalPut:
            def __init__(self, delegate: SQLiteArtifactStore) -> None:
                self.delegate = delegate
                self.failed = False

            def get(self, artifact_id: str):
                return self.delegate.get(artifact_id)

            def put(self, record, *, input_value):
                if record.artifact_id == "delivery-restart" and not self.failed:
                    self.failed = True
                    raise RuntimeError("injected post-delivery receipt failure")
                return self.delegate.put(record, input_value=input_value)

        invoker = AcceptedTaskAgentInvoker(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=FailFirstFinalPut(self.store),
            result_artifact_store=self.result_store,
            hydration_root=self.hydration_root,
            claim_lease_port=self.claim_lease,
            executor=executor,
        )
        request = {
            "schema": "controller-task-agent-invocation-v1",
            "artifact_id": "delivery-restart",
            "dispatch_artifact_id": dispatch["artifact_id"],
            "dispatch_artifact_sha256": dispatch["artifact_sha256"],
        }

        with self.assertRaisesRegex(RuntimeError, "post-delivery"):
            invoker.invoke(request)
        resumed = invoker.invoke(request)

        self.assertEqual(1, len(executor.calls))
        self.assertFalse(resumed["idempotent_replay"])
        self.assertEqual("completed", resumed["delivery_status"])

    def test_partial_result_materialization_recovers_without_agent_relaunch(self) -> None:
        dispatch = self.dispatcher().dispatch(self.request())
        executor = FakeAgentExecutor()

        class FailFirstTaskResultPut:
            def __init__(self, delegate: SQLiteArtifactStore) -> None:
                self.delegate = delegate
                self.failed = False

            def get(self, artifact_id: str):
                return self.delegate.get(artifact_id)

            def put(self, record, *, input_value):
                if record.artifact_id.startswith("task-result:") and not self.failed:
                    self.failed = True
                    raise RuntimeError("injected task result materialization failure")
                return self.delegate.put(record, input_value=input_value)

        result_store = FailFirstTaskResultPut(self.result_store)
        invoker = AcceptedTaskAgentInvoker(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=result_store,
            hydration_root=self.hydration_root,
            claim_lease_port=self.claim_lease,
            executor=executor,
        )
        request = {
            "schema": "controller-task-agent-invocation-v1",
            "artifact_id": "delivery-partial-result-recovery",
            "dispatch_artifact_id": dispatch["artifact_id"],
            "dispatch_artifact_sha256": dispatch["artifact_sha256"],
        }

        with self.assertRaisesRegex(RuntimeError, "materialization failure"):
            invoker.invoke(request)
        resumed = invoker.invoke(request)

        self.assertEqual(1, len(executor.calls))
        self.assertEqual("completed", resumed["delivery_status"])

    def test_agent_delivery_identity_mismatch_is_rejected(self) -> None:
        dispatch = self.dispatcher().dispatch(self.request())
        executor = FakeAgentExecutor(invalid=True)
        invoker = AcceptedTaskAgentInvoker(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            hydration_root=self.hydration_root,
            claim_lease_port=self.claim_lease,
            executor=executor,
        )

        with self.assertRaises(KanbanAgentDeliveryError) as raised:
            invoker.invoke(
                {
                    "schema": "controller-task-agent-invocation-v1",
                    "artifact_id": "invalid-delivery-artifact",
                    "dispatch_artifact_id": dispatch["artifact_id"],
                    "dispatch_artifact_sha256": dispatch["artifact_sha256"],
                }
            )

        self.assertEqual("DELIVERY_IDENTITY_MISMATCH", raised.exception.code)
        self.assertEqual([], executor.acknowledgements)

    def test_result_store_failure_does_not_release_executor_recovery_fence(self) -> None:
        dispatch = self.dispatcher().dispatch(self.request())
        executor = FakeAgentExecutor()

        class RejectResultPut:
            def get(self, artifact_id: str):
                raise ArtifactStoreError("ARTIFACT_NOT_FOUND", artifact_id)

            def put(self, record, *, input_value):
                del record, input_value
                raise RuntimeError("injected result persistence failure")

        invoker = AcceptedTaskAgentInvoker(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=RejectResultPut(),
            hydration_root=self.hydration_root,
            claim_lease_port=self.claim_lease,
            executor=executor,
        )

        with self.assertRaisesRegex(RuntimeError, "result persistence failure"):
            invoker.invoke(
                {
                    "schema": "controller-task-agent-invocation-v1",
                    "artifact_id": "failed-result-delivery",
                    "dispatch_artifact_id": dispatch["artifact_id"],
                    "dispatch_artifact_sha256": dispatch["artifact_sha256"],
                }
            )

        self.assertEqual(1, len(executor.calls))
        self.assertEqual([], executor.acknowledgements)

    def test_agent_delivery_fails_closed_after_claim_ownership_loss(self) -> None:
        dispatch = self.dispatcher().dispatch(self.request())
        executor = FakeAgentExecutor()
        invoker = AcceptedTaskAgentInvoker(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            hydration_root=self.hydration_root,
            claim_lease_port=FakeClaimLeasePort(lose_after=1),
            executor=executor,
        )

        with self.assertRaises(KanbanAgentDeliveryError) as raised:
            invoker.invoke(
                {
                    "schema": "controller-task-agent-invocation-v1",
                    "artifact_id": "ownership-lost-delivery",
                    "dispatch_artifact_id": dispatch["artifact_id"],
                    "dispatch_artifact_sha256": dispatch["artifact_sha256"],
                }
            )

        self.assertEqual("CLAIM_OWNERSHIP_LOST", raised.exception.code)
        self.assertEqual(1, len(executor.calls))
        with self.assertRaises(ArtifactStoreError):
            self.store.get("ownership-lost-delivery")

    def test_agent_logical_result_ids_are_namespaced_by_invocation(self) -> None:
        dispatch = self.dispatcher().dispatch(self.request())
        invoker = AcceptedTaskAgentInvoker(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            hydration_root=self.hydration_root,
            claim_lease_port=self.claim_lease,
            executor=FakeAgentExecutor(),
        )
        dispatch_payload = self.store.get(dispatch["artifact_id"]).payload
        result_payload = {"schema": "implementation-result-v1", "commit": "abc123"}
        output = {
            "schema": "controller-task-agent-delivery-v1",
            "status": "completed",
            "task_id": dispatch["task_id"],
            "run_id": dispatch["run_id"],
            "dispatch_key": dispatch["dispatch_key"],
            "result_artifacts": [{
                "artifact_id": "implementation-result",
                "step_id": "implementation_delivery",
                "sha256": canonical_json_sha256(result_payload),
                "payload": result_payload,
            }],
        }

        first = invoker._delivery(
            output, dispatch_payload, invocation_key="a" * 64,
            agent_id="agent-m13-1", expected_step_id="implementation_delivery",
        )
        second = invoker._delivery(
            output, dispatch_payload, invocation_key="b" * 64,
            agent_id="agent-m13-1", expected_step_id="implementation_delivery",
        )

        first_id = first["result_artifacts"][0]["artifact_id"]
        second_id = second["result_artifacts"][0]["artifact_id"]
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(
            "implementation-result",
            self.result_store.get(first_id).payload["logical_id"],
        )

    def test_delivery_replay_rejects_tampered_invocation_scoped_result_contract(self) -> None:
        dispatch = self.dispatcher().dispatch(self.request())
        invoker = AcceptedTaskAgentInvoker(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            hydration_root=self.hydration_root,
            claim_lease_port=self.claim_lease,
            executor=FakeAgentExecutor(),
        )
        request = {
            "schema": "controller-task-agent-invocation-v1",
            "artifact_id": "delivery-result-contract-tamper",
            "dispatch_artifact_id": dispatch["artifact_id"],
            "dispatch_artifact_sha256": dispatch["artifact_sha256"],
        }
        invoker.invoke(request)
        delivery_payload = self.store.get(request["artifact_id"]).payload
        result_id = delivery_payload["delivery"]["result_artifacts"][0]["artifact_id"]
        record = self.result_store.get(result_id)
        tampered = dict(record.payload)
        tampered["invocation_key"] = "a" * 64
        self.result_store._connection.execute(
            "UPDATE controller_artifacts SET payload_json = ?, output_sha256 = ? WHERE artifact_id = ?",
            (
                json.dumps(tampered, sort_keys=True, separators=(",", ":")),
                canonical_json_sha256(tampered),
                result_id,
            ),
        )
        self.result_store._connection.commit()

        with self.assertRaises(KanbanAgentDeliveryError) as raised:
            invoker.invoke(request)

        self.assertEqual("CONFLICTING_REPLAY", raised.exception.code)

    def test_delivery_replay_rejects_tampered_complete_receipt_contract(self) -> None:
        dispatch = self.dispatcher().dispatch(self.request())
        invoker = AcceptedTaskAgentInvoker(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            hydration_root=self.hydration_root,
            claim_lease_port=self.claim_lease,
            executor=FakeAgentExecutor(),
        )
        request = {
            "schema": "controller-task-agent-invocation-v1",
            "artifact_id": "delivery-receipt-contract-tamper",
            "dispatch_artifact_id": dispatch["artifact_id"],
            "dispatch_artifact_sha256": dispatch["artifact_sha256"],
        }
        invoker.invoke(request)
        record = self.store.get(request["artifact_id"])
        tampered = json.loads(json.dumps(record.payload))
        tampered["workspace_path"] = (
            self.workspace.parent / "other-workspace"
        ).resolve().as_posix()
        tampered["accepted_artifacts"]["graph"]["artifact_id"] = "other-graph"
        self.store._connection.execute(
            "UPDATE controller_artifacts SET payload_json = ?, output_sha256 = ? WHERE artifact_id = ?",
            (
                json.dumps(tampered, sort_keys=True, separators=(",", ":")),
                canonical_json_sha256(tampered),
                request["artifact_id"],
            ),
        )
        self.store._connection.commit()

        with self.assertRaises(KanbanAgentDeliveryError) as raised:
            invoker.invoke(request)

        self.assertEqual("CONFLICTING_REPLAY", raised.exception.code)

    def test_completed_delivery_requires_at_least_one_typed_result(self) -> None:
        dispatch = self.dispatcher().dispatch(self.request())

        class EmptyResultExecutor(FakeAgentExecutor):
            def invoke(self, invocation):
                result = super().invoke(invocation)
                return AgentResult(
                    agent_id=result.agent_id,
                    output={**result.output, "result_artifacts": []},
                )

        invoker = AcceptedTaskAgentInvoker(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            hydration_root=self.hydration_root,
            claim_lease_port=self.claim_lease,
            executor=EmptyResultExecutor(),
        )

        with self.assertRaises(KanbanAgentDeliveryError) as raised:
            invoker.invoke(
                {
                    "schema": "controller-task-agent-invocation-v1",
                    "artifact_id": "delivery-empty-results",
                    "dispatch_artifact_id": dispatch["artifact_id"],
                    "dispatch_artifact_sha256": dispatch["artifact_sha256"],
                }
            )

        self.assertEqual("DELIVERY_INVALID", raised.exception.code)

    def test_delivery_replay_rejects_stored_completed_delivery_without_results(self) -> None:
        dispatch = self.dispatcher().dispatch(self.request())
        invoker = AcceptedTaskAgentInvoker(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            hydration_root=self.hydration_root,
            claim_lease_port=self.claim_lease,
            executor=FakeAgentExecutor(),
        )
        request = {
            "schema": "controller-task-agent-invocation-v1",
            "artifact_id": "delivery-stored-empty-results",
            "dispatch_artifact_id": dispatch["artifact_id"],
            "dispatch_artifact_sha256": dispatch["artifact_sha256"],
        }
        invoker.invoke(request)
        record = self.store.get(request["artifact_id"])
        tampered = json.loads(json.dumps(record.payload))
        tampered["delivery"]["result_artifacts"] = []
        self.store._connection.execute(
            "UPDATE controller_artifacts SET payload_json = ?, output_sha256 = ? WHERE artifact_id = ?",
            (
                json.dumps(tampered, sort_keys=True, separators=(",", ":")),
                canonical_json_sha256(tampered),
                request["artifact_id"],
            ),
        )
        self.store._connection.commit()

        with self.assertRaises(KanbanAgentDeliveryError) as raised:
            invoker.invoke(request)

        self.assertEqual("CONFLICTING_REPLAY", raised.exception.code)

    def test_delivery_replay_rejects_nonaccepted_result_record(self) -> None:
        dispatch = self.dispatcher().dispatch(self.request())
        invoker = AcceptedTaskAgentInvoker(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            hydration_root=self.hydration_root,
            claim_lease_port=self.claim_lease,
            executor=FakeAgentExecutor(),
        )
        request = {
            "schema": "controller-task-agent-invocation-v1",
            "artifact_id": "delivery-result-record-rejected",
            "dispatch_artifact_id": dispatch["artifact_id"],
            "dispatch_artifact_sha256": dispatch["artifact_sha256"],
        }
        invoker.invoke(request)
        result_id = self.store.get(request["artifact_id"]).payload["delivery"]["result_artifacts"][0]["artifact_id"]
        self.result_store._connection.execute(
            "UPDATE controller_artifacts SET validation_result = 'rejected' WHERE artifact_id = ?",
            (result_id,),
        )
        self.result_store._connection.commit()

        with self.assertRaises(KanbanAgentDeliveryError) as raised:
            invoker.invoke(request)

        self.assertEqual("CONFLICTING_REPLAY", raised.exception.code)
        self.result_store._connection.execute(
            "UPDATE controller_artifacts SET validation_result = 'accepted', executor_kind = 'deterministic' WHERE artifact_id = ?",
            (result_id,),
        )
        self.result_store._connection.commit()

        with self.assertRaises(KanbanAgentDeliveryError) as raised:
            invoker.invoke(request)

        self.assertEqual("CONFLICTING_REPLAY", raised.exception.code)

    def test_provisional_agent_result_recovery_rejects_extra_fields_and_wrong_run(self) -> None:
        dispatch = self.dispatcher().dispatch(self.request())
        invoker = AcceptedTaskAgentInvoker(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            hydration_root=self.hydration_root,
            claim_lease_port=self.claim_lease,
            executor=FakeAgentExecutor(),
        )
        request = {
            "schema": "controller-task-agent-invocation-v1",
            "artifact_id": "delivery-provisional-contract",
            "dispatch_artifact_id": dispatch["artifact_id"],
            "dispatch_artifact_sha256": dispatch["artifact_sha256"],
        }
        invoker.invoke(request)
        dispatch_payload = self.store.get(dispatch["artifact_id"]).payload
        invocation_key = canonical_json_sha256({
            "dispatch_artifact_id": dispatch["artifact_id"],
            "dispatch_artifact_sha256": dispatch["artifact_sha256"],
            "task_id": dispatch["task_id"],
            "run_id": dispatch["run_id"],
        })
        result_input = {
            "schema": "controller-task-agent-result-v1",
            "invocation_key": invocation_key,
            "receipt_artifact_id": request["artifact_id"],
            "dispatch_artifact_id": dispatch["artifact_id"],
            "dispatch_artifact_sha256": dispatch["artifact_sha256"],
        }
        record = self.result_store.get(f"agent-result:{invocation_key}")
        extra_payload = {**dict(record.payload), "unexpected": True}
        malformed = (
            replace(record, payload=extra_payload, output_sha256=canonical_json_sha256(extra_payload)),
            replace(record, run_id="999"),
        )

        for candidate in malformed:
            with self.subTest(candidate=candidate.run_id, fields=set(candidate.payload)):
                with self.assertRaises(KanbanAgentDeliveryError) as raised:
                    invoker._result_payload(
                        candidate,
                        result_input,
                        invocation_key,
                        dispatch_payload,
                        expected_step_id="implementation_delivery",
                    )
                self.assertEqual("CONFLICTING_REPLAY", raised.exception.code)

    def test_delivery_replay_rejects_more_than_32_result_refs(self) -> None:
        dispatch = self.dispatcher().dispatch(self.request())
        dispatch_payload = self.store.get(dispatch["artifact_id"]).payload
        invoker = AcceptedTaskAgentInvoker(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            hydration_root=self.hydration_root,
            claim_lease_port=self.claim_lease,
            executor=FakeAgentExecutor(),
        )
        delivery = {
            "schema": "controller-task-agent-delivery-v1",
            "status": "completed",
            "task_id": dispatch["task_id"],
            "run_id": dispatch["run_id"],
            "dispatch_key": dispatch["dispatch_key"],
            "result_artifacts": [
                {"artifact_id": f"result-{index}", "step_id": "implementation_delivery", "sha256": "a" * 64}
                for index in range(33)
            ],
        }

        with self.assertRaises(KanbanAgentDeliveryError) as raised:
            invoker._verified_delivery_refs(
                delivery,
                dispatch_payload,
                invocation_key="a" * 64,
                expected_step_id="implementation_delivery",
                expected_agent_id="agent-m13-1",
            )

        self.assertEqual("CONFLICTING_REPLAY", raised.exception.code)

    def test_delivery_rejects_dispatch_receipt_with_extra_field(self) -> None:
        dispatch = self.dispatcher().dispatch(self.request())
        record = self.store.get(dispatch["artifact_id"])
        tampered = dict(record.payload)
        tampered["unexpected"] = "field"
        digest = canonical_json_sha256(tampered)
        self.store._connection.execute(
            "UPDATE controller_artifacts SET payload_json = ?, output_sha256 = ? WHERE artifact_id = ?",
            (
                json.dumps(tampered, sort_keys=True, separators=(",", ":")),
                digest,
                dispatch["artifact_id"],
            ),
        )
        self.store._connection.commit()
        invoker = AcceptedTaskAgentInvoker(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            hydration_root=self.hydration_root,
            claim_lease_port=self.claim_lease,
            executor=FakeAgentExecutor(),
        )

        with self.assertRaises(KanbanAgentDeliveryError) as raised:
            invoker.invoke({
                "schema": "controller-task-agent-invocation-v1",
                "artifact_id": "delivery-extra-dispatch-field",
                "dispatch_artifact_id": dispatch["artifact_id"],
                "dispatch_artifact_sha256": digest,
            })

        self.assertEqual("DISPATCH_ARTIFACT_INVALID", raised.exception.code)

    def test_completion_rejects_nested_delivery_identity_mismatch(self) -> None:
        dispatch = self.dispatcher().dispatch(self.request())
        delivery = AcceptedTaskAgentInvoker(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            hydration_root=self.hydration_root,
            claim_lease_port=self.claim_lease,
            executor=FakeAgentExecutor(),
        ).invoke({
            "schema": "controller-task-agent-invocation-v1",
            "artifact_id": "delivery-nested-identity-tamper",
            "dispatch_artifact_id": dispatch["artifact_id"],
            "dispatch_artifact_sha256": dispatch["artifact_sha256"],
        })
        record = self.store.get(delivery["artifact_id"])
        tampered = json.loads(json.dumps(record.payload))
        tampered["delivery"]["task_id"] = "other-task"
        digest = canonical_json_sha256(tampered)
        self.store._connection.execute(
            "UPDATE controller_artifacts SET payload_json = ?, output_sha256 = ? WHERE artifact_id = ?",
            (
                json.dumps(tampered, sort_keys=True, separators=(",", ":")),
                digest,
                delivery["artifact_id"],
            ),
        )
        self.store._connection.commit()
        port = FakeCompletionPort(self.port)
        completer = AcceptedTaskCompleter(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            completion_artifact_store=self.store,
            kanban_port=port,
        )

        with self.assertRaises(KanbanCompletionError) as raised:
            completer.complete({
                "schema": "controller-kanban-completion-v1",
                "artifact_id": "completion-nested-identity-tamper",
                "dispatch_artifact_id": dispatch["artifact_id"],
                "dispatch_artifact_sha256": dispatch["artifact_sha256"],
                "delivery_artifact_id": delivery["artifact_id"],
                "delivery_artifact_sha256": digest,
                "expected_snapshot_fingerprint": fingerprint_activation_snapshot(
                    port.snapshot(board="m13-test")
                ),
            })

        self.assertEqual("DELIVERY_ARTIFACT_INVALID", raised.exception.code)
        self.assertEqual(0, port.completions)

    def test_completion_rejects_empty_authenticated_agent_identity(self) -> None:
        dispatch = self.dispatcher().dispatch(self.request())
        delivery = AcceptedTaskAgentInvoker(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            hydration_root=self.hydration_root,
            claim_lease_port=self.claim_lease,
            executor=FakeAgentExecutor(),
        ).invoke({
            "schema": "controller-task-agent-invocation-v1",
            "artifact_id": "delivery-empty-agent-identity",
            "dispatch_artifact_id": dispatch["artifact_id"],
            "dispatch_artifact_sha256": dispatch["artifact_sha256"],
        })
        record = self.store.get(delivery["artifact_id"])
        tampered = dict(record.payload)
        tampered["agent_id"] = ""
        digest = canonical_json_sha256(tampered)
        self.store._connection.execute(
            "UPDATE controller_artifacts SET payload_json = ?, output_sha256 = ?, executor_identity = '' WHERE artifact_id = ?",
            (
                json.dumps(tampered, sort_keys=True, separators=(",", ":")),
                digest,
                delivery["artifact_id"],
            ),
        )
        self.store._connection.commit()
        port = FakeCompletionPort(self.port)
        completer = AcceptedTaskCompleter(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            completion_artifact_store=self.store,
            kanban_port=port,
        )

        with self.assertRaises(KanbanCompletionError) as raised:
            completer.complete({
                "schema": "controller-kanban-completion-v1",
                "artifact_id": "completion-empty-agent-identity",
                "dispatch_artifact_id": dispatch["artifact_id"],
                "dispatch_artifact_sha256": dispatch["artifact_sha256"],
                "delivery_artifact_id": delivery["artifact_id"],
                "delivery_artifact_sha256": digest,
                "expected_snapshot_fingerprint": fingerprint_activation_snapshot(
                    port.snapshot(board="m13-test")
                ),
            })

        self.assertEqual("DELIVERY_ARTIFACT_INVALID", raised.exception.code)
        self.assertEqual(0, port.completions)

    def test_completion_rejects_nondeterministic_dispatch_record(self) -> None:
        dispatch = self.dispatcher().dispatch(self.request())
        delivery = AcceptedTaskAgentInvoker(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            hydration_root=self.hydration_root,
            claim_lease_port=self.claim_lease,
            executor=FakeAgentExecutor(),
        ).invoke({
            "schema": "controller-task-agent-invocation-v1",
            "artifact_id": "delivery-agent-dispatch-record",
            "dispatch_artifact_id": dispatch["artifact_id"],
            "dispatch_artifact_sha256": dispatch["artifact_sha256"],
        })
        self.store._connection.execute(
            "UPDATE controller_artifacts SET executor_kind = 'agent' WHERE artifact_id = ?",
            (dispatch["artifact_id"],),
        )
        self.store._connection.commit()
        port = FakeCompletionPort(self.port)
        completer = AcceptedTaskCompleter(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            completion_artifact_store=self.store,
            kanban_port=port,
        )

        with self.assertRaises(KanbanCompletionError) as raised:
            completer.complete({
                "schema": "controller-kanban-completion-v1",
                "artifact_id": "completion-agent-dispatch-record",
                "dispatch_artifact_id": dispatch["artifact_id"],
                "dispatch_artifact_sha256": dispatch["artifact_sha256"],
                "delivery_artifact_id": delivery["artifact_id"],
                "delivery_artifact_sha256": delivery["artifact_sha256"],
                "expected_snapshot_fingerprint": fingerprint_activation_snapshot(
                    port.snapshot(board="m13-test")
                ),
            })

        self.assertEqual("DISPATCH_ARTIFACT_INVALID", raised.exception.code)
        self.assertEqual(0, port.completions)

    def test_completion_rejects_tampered_delivery_execution_and_artifact_contract(self) -> None:
        dispatch = self.dispatcher().dispatch(self.request())
        delivery = AcceptedTaskAgentInvoker(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            hydration_root=self.hydration_root,
            claim_lease_port=self.claim_lease,
            executor=FakeAgentExecutor(),
        ).invoke(
            {
                "schema": "controller-task-agent-invocation-v1",
                "artifact_id": "delivery-full-contract-tamper",
                "dispatch_artifact_id": dispatch["artifact_id"],
                "dispatch_artifact_sha256": dispatch["artifact_sha256"],
            }
        )
        record = self.store.get(delivery["artifact_id"])
        tampered = json.loads(json.dumps(record.payload))
        tampered["execution_contract"]["allowed_toolsets"] = []
        tampered["accepted_artifacts"]["graph"]["artifact_id"] = "other-graph"
        self.store._connection.execute(
            "UPDATE controller_artifacts SET payload_json = ?, output_sha256 = ? WHERE artifact_id = ?",
            (
                json.dumps(tampered, sort_keys=True, separators=(",", ":")),
                canonical_json_sha256(tampered),
                delivery["artifact_id"],
            ),
        )
        self.store._connection.commit()
        port = FakeCompletionPort(self.port)
        completer = AcceptedTaskCompleter(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            completion_artifact_store=self.store,
            kanban_port=port,
        )

        with self.assertRaises(KanbanCompletionError) as raised:
            completer.complete(
                {
                    "schema": "controller-kanban-completion-v1",
                    "artifact_id": "completion-full-contract-tamper",
                    "dispatch_artifact_id": dispatch["artifact_id"],
                    "dispatch_artifact_sha256": dispatch["artifact_sha256"],
                    "delivery_artifact_id": delivery["artifact_id"],
                    "delivery_artifact_sha256": canonical_json_sha256(tampered),
                    "expected_snapshot_fingerprint": fingerprint_activation_snapshot(
                        port.snapshot(board="m13-test")
                    ),
                }
            )

        self.assertEqual("DELIVERY_ARTIFACT_INVALID", raised.exception.code)

    def test_agent_result_step_must_match_execution_contract(self) -> None:
        dispatch = self.dispatcher().dispatch(self.request())
        executor = FakeAgentExecutor()
        original_invoke = executor.invoke

        def wrong_step(invocation):
            result = original_invoke(invocation)
            result.output["result_artifacts"][0]["step_id"] = "unrelated_step"
            return result

        executor.invoke = wrong_step
        invoker = AcceptedTaskAgentInvoker(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            hydration_root=self.hydration_root,
            claim_lease_port=self.claim_lease,
            executor=executor,
        )

        with self.assertRaises(KanbanAgentDeliveryError) as raised:
            invoker.invoke({
                "schema": "controller-task-agent-invocation-v1",
                "artifact_id": "wrong-step-delivery",
                "dispatch_artifact_id": dispatch["artifact_id"],
                "dispatch_artifact_sha256": dispatch["artifact_sha256"],
            })

        self.assertEqual("DELIVERY_INVALID", raised.exception.code)

    def test_typed_delivery_completes_exact_native_run_once(self) -> None:
        dispatch = self.dispatcher().dispatch(self.request())
        delivery = AcceptedTaskAgentInvoker(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            hydration_root=self.hydration_root,
            claim_lease_port=self.claim_lease,
            executor=FakeAgentExecutor(),
        ).invoke(
            {
                "schema": "controller-task-agent-invocation-v1",
                "artifact_id": "delivery-artifact-completion",
                "dispatch_artifact_id": dispatch["artifact_id"],
                "dispatch_artifact_sha256": dispatch["artifact_sha256"],
            }
        )
        port = FakeCompletionPort(self.port)
        completer = AcceptedTaskCompleter(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            completion_artifact_store=self.store,
            kanban_port=port,
        )
        request = {
            "schema": "controller-kanban-completion-v1",
            "artifact_id": "completion-artifact",
            "dispatch_artifact_id": dispatch["artifact_id"],
            "dispatch_artifact_sha256": dispatch["artifact_sha256"],
            "delivery_artifact_id": delivery["artifact_id"],
            "delivery_artifact_sha256": delivery["artifact_sha256"],
            "expected_snapshot_fingerprint": fingerprint_activation_snapshot(port.snapshot(board="m13-test")),
        }

        first = completer.complete(request)
        replay = completer.complete(request)

        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(1, port.completions)
        self.assertEqual(41, first["run_id"])
        self.assertEqual("done", self.port.value["tasks"][0]["status"])
        result_ref = self.store.get(delivery["artifact_id"]).payload["delivery"][
            "result_artifacts"
        ][0]
        self.assertEqual(
            [
                {
                    "artifact_id": result_ref["artifact_id"],
                    "step_id": "implementation_delivery",
                    "sha256": result_ref["sha256"],
                }
            ],
            port.last_completion["result_artifacts"],
        )

    def test_completion_rejects_blocked_delivery_without_native_mutation(self) -> None:
        dispatch = self.dispatcher().dispatch(self.request())
        executor = FakeAgentExecutor()
        original_invoke = executor.invoke

        def blocked(invocation):
            result = original_invoke(invocation)
            result.output["status"] = "blocked"
            return result

        executor.invoke = blocked
        delivery = AcceptedTaskAgentInvoker(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            hydration_root=self.hydration_root,
            claim_lease_port=self.claim_lease,
            executor=executor,
        ).invoke(
            {
                "schema": "controller-task-agent-invocation-v1",
                "artifact_id": "blocked-delivery-artifact",
                "dispatch_artifact_id": dispatch["artifact_id"],
                "dispatch_artifact_sha256": dispatch["artifact_sha256"],
            }
        )
        port = FakeCompletionPort(self.port)
        completer = AcceptedTaskCompleter(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            completion_artifact_store=self.store,
            kanban_port=port,
        )

        with self.assertRaises(KanbanCompletionError) as raised:
            completer.complete(
                {
                    "schema": "controller-kanban-completion-v1",
                    "artifact_id": "blocked-completion-artifact",
                    "dispatch_artifact_id": dispatch["artifact_id"],
                    "dispatch_artifact_sha256": dispatch["artifact_sha256"],
                    "delivery_artifact_id": delivery["artifact_id"],
                    "delivery_artifact_sha256": delivery["artifact_sha256"],
                    "expected_snapshot_fingerprint": fingerprint_activation_snapshot(
                        port.snapshot(board="m13-test")
                    ),
                }
            )

        self.assertEqual("DELIVERY_NOT_COMPLETED", raised.exception.code)
        self.assertEqual(0, port.completions)

    def test_completion_resolves_every_result_artifact_before_native_mutation(self) -> None:
        dispatch = self.dispatcher().dispatch(self.request())
        delivery = AcceptedTaskAgentInvoker(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            hydration_root=self.hydration_root,
            claim_lease_port=self.claim_lease,
            executor=FakeAgentExecutor(),
        ).invoke(
            {
                "schema": "controller-task-agent-invocation-v1",
                "artifact_id": "delivery-with-unresolved-result",
                "dispatch_artifact_id": dispatch["artifact_id"],
                "dispatch_artifact_sha256": dispatch["artifact_sha256"],
            }
        )
        missing_result_store = SQLiteArtifactStore.open(self.workspace / "missing-results.db")
        self.addCleanup(missing_result_store.close)
        port = FakeCompletionPort(self.port)
        completer = AcceptedTaskCompleter(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=missing_result_store,
            completion_artifact_store=self.store,
            kanban_port=port,
        )

        with self.assertRaises(KanbanCompletionError) as raised:
            completer.complete(
                {
                    "schema": "controller-kanban-completion-v1",
                    "artifact_id": "completion-with-unresolved-result",
                    "dispatch_artifact_id": dispatch["artifact_id"],
                    "dispatch_artifact_sha256": dispatch["artifact_sha256"],
                    "delivery_artifact_id": delivery["artifact_id"],
                    "delivery_artifact_sha256": delivery["artifact_sha256"],
                    "expected_snapshot_fingerprint": fingerprint_activation_snapshot(
                        port.snapshot(board="m13-test")
                    ),
                }
            )

        self.assertEqual("RESULT_ARTIFACT_INVALID", raised.exception.code)
        self.assertEqual(0, port.completions)

    def test_completion_rejects_result_from_a_different_agent(self) -> None:
        dispatch = self.dispatcher().dispatch(self.request())
        delivery = AcceptedTaskAgentInvoker(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            hydration_root=self.hydration_root,
            claim_lease_port=self.claim_lease,
            executor=FakeAgentExecutor(),
        ).invoke({
            "schema": "controller-task-agent-invocation-v1",
            "artifact_id": "producer-bound-delivery",
            "dispatch_artifact_id": dispatch["artifact_id"],
            "dispatch_artifact_sha256": dispatch["artifact_sha256"],
        })
        delivery_record = self.store.get(delivery["artifact_id"])
        original_ref = delivery_record.payload["delivery"]["result_artifacts"][0]
        original_result = self.result_store.get(original_ref["artifact_id"])
        evil_input = {"producer": "evil-agent"}
        evil_result = ArtifactRecord(
            artifact_id="producer-mismatch-result",
            run_id=original_result.run_id,
            step_id=original_result.step_id,
            attempt=1,
            executor_kind="agent",
            input_sha256=canonical_json_sha256(evil_input),
            output_sha256=original_result.output_sha256,
            timeout_seconds=300,
            validation_result="accepted",
            executor_identity="evil-agent",
            payload=original_result.payload,
        )
        self.result_store.put(evil_result, input_value=evil_input)
        evil_payload = json.loads(json.dumps(delivery_record.payload))
        evil_payload["delivery"]["result_artifacts"][0]["artifact_id"] = evil_result.artifact_id
        evil_delivery_input = {"delivery": "producer-mismatch"}
        evil_delivery = ArtifactRecord(
            artifact_id="producer-mismatch-delivery",
            run_id=delivery_record.run_id,
            step_id="task_agent_delivery",
            attempt=1,
            executor_kind="agent",
            input_sha256=canonical_json_sha256(evil_delivery_input),
            output_sha256=canonical_json_sha256(evil_payload),
            timeout_seconds=300,
            validation_result="accepted",
            executor_identity=delivery_record.executor_identity,
            payload=evil_payload,
        )
        self.store.put(evil_delivery, input_value=evil_delivery_input)
        port = FakeCompletionPort(self.port)
        completer = AcceptedTaskCompleter(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            completion_artifact_store=self.store,
            kanban_port=port,
        )

        with self.assertRaises(KanbanCompletionError) as raised:
            completer.complete({
                "schema": "controller-kanban-completion-v1",
                "artifact_id": "producer-mismatch-completion",
                "dispatch_artifact_id": dispatch["artifact_id"],
                "dispatch_artifact_sha256": dispatch["artifact_sha256"],
                "delivery_artifact_id": evil_delivery.artifact_id,
                "delivery_artifact_sha256": evil_delivery.output_sha256,
                "expected_snapshot_fingerprint": fingerprint_activation_snapshot(
                    port.snapshot(board="m13-test")
                ),
            })

        self.assertEqual("RESULT_ARTIFACT_INVALID", raised.exception.code)
        self.assertEqual(0, port.completions)

    def test_completion_event_rejects_duplicate_top_level_provenance(self) -> None:
        metadata = {
            "controller_completion_key": "c" * 64,
            "dispatch_artifact_id": "dispatch-artifact",
            "dispatch_artifact_sha256": "d" * 64,
            "delivery_artifact_id": "delivery-artifact",
            "delivery_artifact_sha256": "e" * 64,
            "result_artifacts": [],
        }
        receipt = {
            "run_id": 41,
            "payload": {
                "result": "Accepted controller delivery artifact delivery-artifact",
                "summary": "Accepted controller delivery artifact delivery-artifact",
                "metadata": metadata,
                "dispatch_artifact_id": "tampered",
            },
        }

        with self.assertRaises(KanbanCompletionError) as raised:
            from hermes_langchain.controller.kanban_completion import (
                HermesNativeKanbanCompletionPort,
            )

            HermesNativeKanbanCompletionPort._event_metadata(receipt)

        self.assertEqual("NATIVE_READBACK_MISMATCH", raised.exception.code)

    def test_restart_after_completion_before_artifact_write_reuses_native_completion(self) -> None:
        dispatch = self.dispatcher().dispatch(self.request())
        delivery = AcceptedTaskAgentInvoker(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            hydration_root=self.hydration_root,
            claim_lease_port=self.claim_lease,
            executor=FakeAgentExecutor(),
        ).invoke(
            {
                "schema": "controller-task-agent-invocation-v1",
                "artifact_id": "delivery-artifact-completion-restart",
                "dispatch_artifact_id": dispatch["artifact_id"],
                "dispatch_artifact_sha256": dispatch["artifact_sha256"],
            }
        )
        port = FakeCompletionPort(self.port)

        class FailFirstPut:
            def __init__(self, delegate: SQLiteArtifactStore) -> None:
                self.delegate = delegate
                self.failed = False

            def get(self, artifact_id: str):
                return self.delegate.get(artifact_id)

            def put(self, record, *, input_value):
                if not self.failed:
                    self.failed = True
                    raise RuntimeError("injected post-completion artifact failure")
                return self.delegate.put(record, input_value=input_value)

        request = {
            "schema": "controller-kanban-completion-v1",
            "artifact_id": "completion-restart-artifact",
            "dispatch_artifact_id": dispatch["artifact_id"],
            "dispatch_artifact_sha256": dispatch["artifact_sha256"],
            "delivery_artifact_id": delivery["artifact_id"],
            "delivery_artifact_sha256": delivery["artifact_sha256"],
            "expected_snapshot_fingerprint": fingerprint_activation_snapshot(
                port.snapshot(board="m13-test")
            ),
        }
        completer = AcceptedTaskCompleter(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            completion_artifact_store=FailFirstPut(self.store),
            kanban_port=port,
        )

        with self.assertRaisesRegex(RuntimeError, "post-completion"):
            completer.complete(request)
        resumed = completer.complete(request)

        self.assertEqual(1, port.completions)
        self.assertTrue(resumed["idempotent_replay"])
        self.assertEqual("done", resumed["task_status"])

    def test_completion_replay_rejects_tampered_full_receipt(self) -> None:
        dispatch = self.dispatcher().dispatch(self.request())
        delivery = AcceptedTaskAgentInvoker(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            hydration_root=self.hydration_root,
            claim_lease_port=self.claim_lease,
            executor=FakeAgentExecutor(),
        ).invoke({
            "schema": "controller-task-agent-invocation-v1",
            "artifact_id": "delivery-completion-full-replay",
            "dispatch_artifact_id": dispatch["artifact_id"],
            "dispatch_artifact_sha256": dispatch["artifact_sha256"],
        })
        port = FakeCompletionPort(self.port)
        completer = AcceptedTaskCompleter(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            completion_artifact_store=self.store,
            kanban_port=port,
        )
        request = {
            "schema": "controller-kanban-completion-v1",
            "artifact_id": "completion-full-replay-tamper",
            "dispatch_artifact_id": dispatch["artifact_id"],
            "dispatch_artifact_sha256": dispatch["artifact_sha256"],
            "delivery_artifact_id": delivery["artifact_id"],
            "delivery_artifact_sha256": delivery["artifact_sha256"],
            "expected_snapshot_fingerprint": fingerprint_activation_snapshot(
                port.snapshot(board="m13-test")
            ),
        }
        completer.complete(request)
        record = self.store.get(request["artifact_id"])
        tampered = dict(record.payload)
        tampered["board"] = "other-board"
        self.store._connection.execute(
            "UPDATE controller_artifacts SET payload_json = ?, output_sha256 = ? WHERE artifact_id = ?",
            (
                json.dumps(tampered, sort_keys=True, separators=(",", ":")),
                canonical_json_sha256(tampered),
                request["artifact_id"],
            ),
        )
        self.store._connection.commit()

        with self.assertRaises(KanbanCompletionError) as raised:
            completer.complete(request)

        self.assertEqual("CONFLICTING_REPLAY", raised.exception.code)
        self.assertEqual(1, port.completions)

    def test_completion_recovery_rejects_out_of_band_terminal_task_edit(self) -> None:
        dispatch = self.dispatcher().dispatch(self.request())
        delivery = AcceptedTaskAgentInvoker(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            hydration_root=self.hydration_root,
            claim_lease_port=self.claim_lease,
            executor=FakeAgentExecutor(),
        ).invoke(
            {
                "schema": "controller-task-agent-invocation-v1",
                "artifact_id": "delivery-terminal-edit",
                "dispatch_artifact_id": dispatch["artifact_id"],
                "dispatch_artifact_sha256": dispatch["artifact_sha256"],
            }
        )
        port = FakeCompletionPort(self.port)

        class FailFirstPut:
            def __init__(self, delegate: SQLiteArtifactStore) -> None:
                self.delegate = delegate
                self.failed = False

            def get(self, artifact_id: str):
                return self.delegate.get(artifact_id)

            def put(self, record, *, input_value):
                if not self.failed:
                    self.failed = True
                    raise RuntimeError("injected post-completion artifact failure")
                return self.delegate.put(record, input_value=input_value)

        request = {
            "schema": "controller-kanban-completion-v1",
            "artifact_id": "completion-terminal-edit",
            "dispatch_artifact_id": dispatch["artifact_id"],
            "dispatch_artifact_sha256": dispatch["artifact_sha256"],
            "delivery_artifact_id": delivery["artifact_id"],
            "delivery_artifact_sha256": delivery["artifact_sha256"],
            "expected_snapshot_fingerprint": fingerprint_activation_snapshot(
                port.snapshot(board="m13-test")
            ),
        }
        completer = AcceptedTaskCompleter(
            graph_artifact_store=self.store,
            activation_artifact_store=self.store,
            dispatch_artifact_store=self.store,
            delivery_artifact_store=self.store,
            result_artifact_store=self.result_store,
            completion_artifact_store=FailFirstPut(self.store),
            kanban_port=port,
        )
        with self.assertRaisesRegex(RuntimeError, "post-completion"):
            completer.complete(request)
        native_task = next(
            task for task in port.dispatch_port.value["tasks"] if task["id"] == dispatch["task_id"]
        )
        native_task["title"] = "out-of-band edit"

        with self.assertRaises(KanbanCompletionError) as raised:
            completer.complete(request)

        self.assertEqual("NATIVE_READBACK_MISMATCH", raised.exception.code)

    def test_dispatch_accepts_any_graph_bound_role_not_only_initial_candidate(self) -> None:
        contract = self.request()
        graph = AcceptedTaskDispatcher._graph(self.graph_record, contract)
        review = next(card for card in graph["cards"] if card["role"] == "review")
        activation = {
            "task": {
                "task_id": "t-review-1",
                "title": review["title"],
                "body_sha256": review["body_sha256"],
                "assignee": review["assignee"],
                "status": "todo",
                "workspace_kind": "dir",
                "workspace_path": self.workspace.as_posix(),
                "skills": review["skills"],
                "idempotency_key": review["idempotency_key"],
            }
        }
        review_contract = dict(contract)
        review_contract["task_id"] = "t-review-1"

        expected = AcceptedTaskDispatcher._expected_task(graph, activation, review_contract)

        self.assertEqual("G4-Issue151-Review", expected["title"])
        self.assertEqual([], expected["skills"])
        self.assertEqual("ready", expected["status"])

        execution = AcceptedTaskAgentInvoker._execution_contract(
            graph,
            activation,
            {
                **review_contract,
                "board": "m13-test",
                "run_id": 42,
                "dispatch_key": "d" * 64,
            },
        )
        self.assertEqual("review_delivery", execution["step_id"])
        self.assertEqual(("kwc-review-delivery-runtime",), execution["skills"])
        self.assertNotIn("hermes-langchain", execution["toolsets"])

    def test_activation_task_must_match_graph_activation_candidate(self) -> None:
        payload = json.loads(json.dumps(self.activation_payload))
        payload["task"]["title"] = "G4-Issue151-Wrong"
        conflicting = self._put(
            artifact_id="activation-conflict",
            run_id="activation-run-conflict",
            step_id="task_activation",
            executor_identity="controller-task-activation",
            payload=payload,
        )
        request = self.request()
        request["activation_artifact_id"] = conflicting.artifact_id
        request["activation_artifact_sha256"] = conflicting.output_sha256

        with self.assertRaises(KanbanDispatchError) as raised:
            self.dispatcher().dispatch(request)

        self.assertEqual("ARTIFACT_IDENTITY_MISMATCH", raised.exception.code)
        self.assertEqual(0, self.port.claims)

    def test_native_claim_and_completion_replay_without_duplicates_on_disposable_board(self) -> None:
        board = f"m13-disposable-{uuid.uuid4().hex[:12]}"
        subprocess.run(
            ["hermes", "kanban", "boards", "create", board],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        try:
            store_path = self.workspace / "native-dispatch-artifacts.sqlite3"
            request_file = self.workspace / "native-dispatch-request.json"
            request_file.write_text(
                json.dumps(
                    {
                        "board": board,
                        "issue_number": 151,
                        "generation": 4,
                        "workspace_path": self.workspace.as_posix(),
                        "body": self.body,
                        "inject_completion_finalize_failure": True,
                    },
                    ensure_ascii=False,
                ),
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
                    str(_ROOT / "tests" / "controller" / "workers" / "native_dispatch.py"),
                    str(_ROOT),
                    str(store_path),
                    str(request_file),
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertFalse(result["first"]["idempotent_replay"])
            self.assertTrue(result["replay"]["idempotent_replay"])
            self.assertEqual(result["first"]["run_id"], result["replay"]["run_id"])
            self.assertEqual(1, result["matching_claim_events"])
            self.assertEqual(1, result["matching_runs"])
            self.assertTrue(result["completion_first"]["idempotent_replay"])
            self.assertTrue(result["completion_replay"]["idempotent_replay"])
            self.assertEqual(1, result["matching_completion_events"])
            self.assertTrue(result["completion_finalize_recovered"])
            self.assertTrue(result["event_payload_tamper_rejected"])
            self.assertEqual("done", result["task_status"])
        finally:
            subprocess.run(
                ["hermes", "kanban", "boards", "rm", board, "--delete"],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )

    def test_native_full_impl_review_summary_lifecycle_completes_without_duplicates(self) -> None:
        board = f"m13-lifecycle-{uuid.uuid4().hex[:12]}"
        subprocess.run(
            ["hermes", "kanban", "boards", "create", board],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        try:
            config = {
                "board": board,
                "issue_number": 151,
                "generation": 5,
                "workspace_path": self.workspace.as_posix(),
                "body": self.body,
            }
            config_path = self.workspace / "native-lifecycle-config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            hermes_executable = shutil.which("hermes")
            self.assertIsNotNone(hermes_executable)
            runtime_python = Path(str(hermes_executable)).resolve().parent / (
                "python.exe" if sys.platform == "win32" else "python"
            )
            completed = subprocess.run(
                [
                    str(runtime_python),
                    str(_ROOT / "tests" / "controller" / "workers" / "native_lifecycle.py"),
                    str(_ROOT),
                    str(self.workspace / "native-lifecycle-artifacts.sqlite3"),
                    str(config_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            result = json.loads(completed.stdout)

            self.assertEqual(
                {"implementation": "done", "review": "done", "summary": "done"},
                result["statuses"],
            )
            self.assertEqual(3, result["agent_invocations"])
            self.assertEqual(3, result["runs"])
            self.assertEqual(3, result["claim_events"])
            self.assertEqual(3, result["completion_events"])
            self.assertTrue(result["restart_replay_verified"])
        finally:
            subprocess.run(
                ["hermes", "kanban", "boards", "rm", board, "--delete"],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )

    def test_native_review_rejects_out_of_band_parent_without_controller_completion(self) -> None:
        board = f"m13-parent-fence-{uuid.uuid4().hex[:12]}"
        subprocess.run(
            ["hermes", "kanban", "boards", "create", board],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        try:
            config = {
                "board": board,
                "issue_number": 151,
                "generation": 5,
                "workspace_path": self.workspace.as_posix(),
                "body": self.body,
                "inject_out_of_band_parent": True,
            }
            config_path = self.workspace / "native-parent-fence-config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            hermes_executable = shutil.which("hermes")
            self.assertIsNotNone(hermes_executable)
            runtime_python = Path(str(hermes_executable)).resolve().parent / (
                "python.exe" if sys.platform == "win32" else "python"
            )
            completed = subprocess.run(
                [
                    str(runtime_python),
                    str(_ROOT / "tests" / "controller" / "workers" / "native_lifecycle.py"),
                    str(_ROOT),
                    str(self.workspace / "native-parent-fence-artifacts.sqlite3"),
                    str(config_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(
                {"rejected": "PREDECESSOR_EVIDENCE_INVALID"},
                json.loads(completed.stdout),
            )
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
