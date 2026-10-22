"""Typed delivery acceptance and exact native Kanban completion read-back."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from .artifact_store import (
    ArtifactRecord,
    ArtifactStoreError,
    SQLiteArtifactStore,
    canonical_json_sha256,
)
from .kanban_activation import (
    AcceptedTaskActivator,
    HermesNativeKanbanActivationPort,
    fingerprint_activation_snapshot,
)
from .kanban_dispatch import (
    AcceptedTaskDispatcher,
    HermesNativeKanbanDispatchPort,
    KanbanDispatchError,
)
from .kanban_agent_delivery import AcceptedTaskAgentInvoker

_REQUEST_SCHEMA = "controller-kanban-completion-v1"
_RECEIPT_SCHEMA = "controller-task-completion-receipt-v1"
_DELIVERY_SCHEMA = "controller-task-agent-delivery-v1"
_COMPLETION_RECEIPT_FIELDS = {
    "schema",
    "board",
    "issue_number",
    "generation",
    "workspace_path",
    "task_id",
    "run_id",
    "dispatch_key",
    "completion_key",
    "dispatch_artifact_id",
    "dispatch_artifact_sha256",
    "delivery_artifact_id",
    "delivery_artifact_sha256",
    "delivery_status",
    "result_artifacts",
    "task_status",
    "run_status",
    "run_outcome",
}
_REQUEST_FIELDS = {
    "schema",
    "artifact_id",
    "dispatch_artifact_id",
    "dispatch_artifact_sha256",
    "delivery_artifact_id",
    "delivery_artifact_sha256",
    "expected_snapshot_fingerprint",
}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class KanbanCompletionError(ValueError):
    """Stable fail-closed error for one controller-owned native completion."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class NativeKanbanCompletionPort(Protocol):
    def snapshot(self, *, board: str) -> dict[str, Any]: ...

    def complete_task(
        self,
        *,
        board: str,
        task_id: str,
        run_id: int,
        expected_snapshot_fingerprint: str,
        expected_task: Mapping[str, Any],
        completion_key: str,
        delivery_artifact_id: str,
        delivery_artifact_sha256: str,
        result_artifacts: list[dict[str, str]],
        dispatch_artifact_id: str,
        dispatch_artifact_sha256: str,
    ) -> dict[str, Any]: ...

    def read_completion(
        self,
        *,
        board: str,
        task_id: str,
        run_id: int,
        completion_key: str,
        expected_task: Mapping[str, Any],
        expected_result_text: str,
    ) -> dict[str, Any]: ...


class HermesNativeKanbanCompletionPort:
    """Native adapter for one fenced controller-owned completion transition."""

    def __init__(self, *, kanban_db_module: Any = None, connect_fn: Any = None) -> None:
        self._kanban_db_module = kanban_db_module
        self._connect_fn = connect_fn

    def _dependencies(self) -> tuple[Any, Any]:
        if self._kanban_db_module is None or self._connect_fn is None:
            from ..kanban.snapshot_service import _ensure_hermes_source_root, _import_hermes_cli

            hermes_cli = _import_hermes_cli()
            _ensure_hermes_source_root(hermes_cli)
            from hermes_cli import kanban_db, kanban_db_connect

            self._kanban_db_module = self._kanban_db_module or kanban_db
            self._connect_fn = self._connect_fn or kanban_db_connect.connect
        return self._kanban_db_module, self._connect_fn

    def snapshot(self, *, board: str) -> dict[str, Any]:
        module, connect = self._dependencies()
        conn = connect(board=board)
        try:
            return HermesNativeKanbanActivationPort._snapshot_value(module, conn, board)
        finally:
            conn.close()

    def complete_task(
        self,
        *,
        board: str,
        task_id: str,
        run_id: int,
        expected_snapshot_fingerprint: str,
        expected_task: Mapping[str, Any],
        completion_key: str,
        delivery_artifact_id: str,
        delivery_artifact_sha256: str,
        result_artifacts: list[dict[str, str]],
        dispatch_artifact_id: str,
        dispatch_artifact_sha256: str,
    ) -> dict[str, Any]:
        module, connect = self._dependencies()
        conn = connect(board=board)
        try:
            already_completed = False
            with module.write_txn(conn):
                existing = self._completion_receipt(
                    conn, task_id=task_id, completion_key=completion_key
                )
                if existing is not None:
                    self._verify_event_receipt(
                        existing,
                        run_id=run_id,
                        dispatch_artifact_id=dispatch_artifact_id,
                        dispatch_artifact_sha256=dispatch_artifact_sha256,
                        delivery_artifact_id=delivery_artifact_id,
                        delivery_artifact_sha256=delivery_artifact_sha256,
                        result_artifacts=result_artifacts,
                    )
                    already_completed = True
                else:
                    snapshot = HermesNativeKanbanActivationPort._snapshot_value(module, conn, board)
                    if fingerprint_activation_snapshot(snapshot) != expected_snapshot_fingerprint:
                        raise KanbanCompletionError(
                            "SNAPSHOT_CHANGED", "canonical snapshot changed before completion"
                        )
                    task = module.get_task(conn, task_id)
                    if task is None:
                        raise KanbanCompletionError("NATIVE_TASK_NOT_FOUND", "completion task was not found")
                    task_value = HermesNativeKanbanActivationPort._task_value(task)
                    try:
                        HermesNativeKanbanDispatchPort._verify_task(
                            task_value, expected_task, allowed_statuses={"running"}
                        )
                    except Exception as exc:
                        raise KanbanCompletionError(
                            "NATIVE_READBACK_MISMATCH", "completion task provenance mismatch"
                        ) from exc
                    if getattr(task, "current_run_id", None) != run_id:
                        raise KanbanCompletionError(
                            "NATIVE_READBACK_MISMATCH", "completion run ownership mismatch"
                        )
                    if not module._parents_satisfied(conn, task_id):
                        raise KanbanCompletionError(
                            "PARENT_DEPENDENCY_UNSATISFIED",
                            "completion task has an unfinished parent dependency",
                        )
                    now = int(module.time.time())
                    result_text = f"Accepted controller delivery artifact {delivery_artifact_id}"
                    updated = conn.execute(
                    """
                    UPDATE tasks
                       SET status = 'done', result = ?, completed_at = ?,
                           claim_lock = NULL, claim_expires = NULL, worker_pid = NULL,
                           block_kind = NULL, block_recurrences = 0
                     WHERE id = ? AND status = 'running' AND current_run_id = ?
                    """,
                    (result_text, now, task_id, run_id),
                )
                    if updated.rowcount != 1:
                        raise KanbanCompletionError(
                            "NATIVE_COMPLETION_FAILED", "native completion compare-and-set failed"
                        )
                    metadata = {
                    "controller_completion_key": completion_key,
                    "dispatch_artifact_id": dispatch_artifact_id,
                    "dispatch_artifact_sha256": dispatch_artifact_sha256,
                    "delivery_artifact_id": delivery_artifact_id,
                    "delivery_artifact_sha256": delivery_artifact_sha256,
                    "result_artifacts": result_artifacts,
                }
                    ended_run_id = module._end_run(
                    conn,
                    task_id,
                    outcome="completed",
                    status="done",
                    summary=result_text,
                    metadata=metadata,
                )
                    if ended_run_id != run_id:
                        raise KanbanCompletionError(
                            "NATIVE_COMPLETION_FAILED", "native completion closed a different run"
                        )
                    module._append_event(
                    conn,
                    task_id,
                    "completed",
                    {
                        "result": result_text,
                        "summary": result_text,
                        "metadata": metadata,
                    },
                    run_id=run_id,
                )
            module._clear_failure_counter(conn, task_id)
            module.recompute_ready(conn)
            return {
                **self._read_completion(
                    module,
                    conn,
                    task_id=task_id,
                    run_id=run_id,
                    completion_key=completion_key,
                    expected_task=expected_task,
                    expected_result_text=f"Accepted controller delivery artifact {delivery_artifact_id}",
                ),
                "completed": not already_completed,
            }
        finally:
            conn.close()

    def read_completion(
        self,
        *,
        board: str,
        task_id: str,
        run_id: int,
        completion_key: str,
        expected_task: Mapping[str, Any],
        expected_result_text: str,
    ) -> dict[str, Any]:
        module, connect = self._dependencies()
        conn = connect(board=board)
        try:
            return self._read_completion(
                module,
                conn,
                task_id=task_id,
                run_id=run_id,
                completion_key=completion_key,
                expected_task=expected_task,
                expected_result_text=expected_result_text,
            )
        finally:
            conn.close()

    @classmethod
    def _read_completion(
        cls,
        module: Any,
        conn: Any,
        *,
        task_id: str,
        run_id: int,
        completion_key: str,
        expected_task: Mapping[str, Any],
        expected_result_text: str,
    ) -> dict[str, Any]:
        receipt = cls._completion_receipt(
            conn, task_id=task_id, completion_key=completion_key
        )
        if receipt is None:
            raise KanbanCompletionError(
                "NATIVE_COMPLETION_NOT_FOUND", "native completion receipt was not found"
            )
        task = module.get_task(conn, task_id)
        run = conn.execute(
            "SELECT id, task_id, status, outcome FROM task_runs WHERE id = ? AND task_id = ?",
            (run_id, task_id),
        ).fetchone()
        task_value = HermesNativeKanbanActivationPort._task_value(task) if task is not None else {}
        completed_expected = dict(expected_task)
        completed_expected["status"] = "done"
        try:
            HermesNativeKanbanDispatchPort._verify_task(
                task_value,
                completed_expected,
                allowed_statuses={"done"},
            )
        except Exception as exc:
            raise KanbanCompletionError(
                "NATIVE_READBACK_MISMATCH", "completed task provenance mismatch"
            ) from exc
        if (
            task is None
            or getattr(task, "status", None) != "done"
            or getattr(task, "current_run_id", None) is not None
            or getattr(task, "claim_lock", None) is not None
            or getattr(task, "claim_expires", None) is not None
            or getattr(task, "worker_pid", None) is not None
            or getattr(task, "result", None) != expected_result_text
            or run is None
            or str(run["status"] or "") != "done"
            or str(run["outcome"] or "") != "completed"
            or receipt["run_id"] != run_id
        ):
            raise KanbanCompletionError(
                "NATIVE_READBACK_MISMATCH", "native completion read-back mismatch"
            )
        return {
            "task_id": task_id,
            "run_id": run_id,
            "completion_key": completion_key,
            "task_status": "done",
            "run_status": "done",
            "run_outcome": "completed",
            "dispatch_artifact_id": cls._event_metadata(receipt)["dispatch_artifact_id"],
            "dispatch_artifact_sha256": cls._event_metadata(receipt)["dispatch_artifact_sha256"],
            "delivery_artifact_id": cls._event_metadata(receipt)["delivery_artifact_id"],
            "delivery_artifact_sha256": cls._event_metadata(receipt)["delivery_artifact_sha256"],
            "result_artifacts": cls._event_metadata(receipt)["result_artifacts"],
            "completed": False,
        }

    @staticmethod
    def _event_metadata(receipt: Mapping[str, Any]) -> dict[str, Any]:
        payload = receipt.get("payload")
        metadata = payload.get("metadata") if isinstance(payload, Mapping) else None
        if (
            not isinstance(payload, Mapping)
            or set(payload) != {"result", "summary", "metadata"}
            or not isinstance(metadata, Mapping)
        ):
            raise KanbanCompletionError(
                "NATIVE_READBACK_MISMATCH", "native completion metadata is missing"
            )
        required = {
            "controller_completion_key",
            "dispatch_artifact_id",
            "dispatch_artifact_sha256",
            "delivery_artifact_id",
            "delivery_artifact_sha256",
            "result_artifacts",
        }
        result_text = f"Accepted controller delivery artifact {metadata.get('delivery_artifact_id')}"
        if (
            set(metadata) != required
            or payload.get("result") != result_text
            or payload.get("summary") != result_text
        ):
            raise KanbanCompletionError(
                "NATIVE_READBACK_MISMATCH", "native completion metadata is incomplete"
            )
        return dict(metadata)

    @classmethod
    def _verify_event_receipt(
        cls,
        receipt: Mapping[str, Any],
        *,
        run_id: int,
        dispatch_artifact_id: str,
        dispatch_artifact_sha256: str,
        delivery_artifact_id: str,
        delivery_artifact_sha256: str,
        result_artifacts: list[dict[str, str]],
    ) -> None:
        metadata = cls._event_metadata(receipt)
        if (
            receipt.get("run_id") != run_id
            or metadata.get("dispatch_artifact_id") != dispatch_artifact_id
            or metadata.get("dispatch_artifact_sha256") != dispatch_artifact_sha256
            or metadata.get("delivery_artifact_id") != delivery_artifact_id
            or metadata.get("delivery_artifact_sha256") != delivery_artifact_sha256
            or metadata.get("result_artifacts") != result_artifacts
        ):
            raise KanbanCompletionError(
                "CONFLICTING_REPLAY", "native completion event conflicts with accepted delivery"
            )

    @staticmethod
    def _completion_receipt(
        conn: Any, *, task_id: str, completion_key: str
    ) -> dict[str, Any] | None:
        rows = conn.execute(
            "SELECT run_id, payload FROM task_events WHERE task_id = ? AND kind = 'completed' ORDER BY id DESC",
            (task_id,),
        ).fetchall()
        matches: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                continue
            metadata = payload.get("metadata")
            key = metadata.get("controller_completion_key") if isinstance(metadata, Mapping) else None
            if key == completion_key:
                native_run_id = row["run_id"]
                if type(native_run_id) is not int or native_run_id < 1:
                    raise KanbanCompletionError(
                        "NATIVE_READBACK_MISMATCH", "native completion run identity is invalid"
                    )
                matches.append({"run_id": native_run_id, "payload": payload})
        if len(matches) > 1:
            raise KanbanCompletionError(
                "CONFLICTING_REPLAY", "completion key has multiple native events"
            )
        return matches[0] if matches else None


class AcceptedTaskCompleter:
    """Accept a typed delivery and complete its exact native task run once."""

    def __init__(
        self,
        *,
        graph_artifact_store: SQLiteArtifactStore,
        activation_artifact_store: SQLiteArtifactStore,
        dispatch_artifact_store: SQLiteArtifactStore,
        delivery_artifact_store: SQLiteArtifactStore,
        result_artifact_store: SQLiteArtifactStore,
        completion_artifact_store: SQLiteArtifactStore,
        kanban_port: NativeKanbanCompletionPort,
    ) -> None:
        self._graph_artifact_store = graph_artifact_store
        self._activation_artifact_store = activation_artifact_store
        self._dispatch_artifact_store = dispatch_artifact_store
        self._delivery_artifact_store = delivery_artifact_store
        self._result_artifact_store = result_artifact_store
        self._completion_artifact_store = completion_artifact_store
        self._kanban_port = kanban_port

    def complete(self, request: Any) -> dict[str, Any]:
        contract = self._request(request)
        dispatch_record = self._dispatch_artifact_store.get(contract["dispatch_artifact_id"])
        dispatch = self._dispatch(dispatch_record, contract)
        graph_record = self._graph_artifact_store.get(dispatch["graph_artifact_id"])
        graph = AcceptedTaskAgentInvoker._graph(graph_record, dispatch)
        activation_record = self._activation_artifact_store.get(dispatch["activation_artifact_id"])
        activation = self._activation(activation_record, dispatch)
        execution = AcceptedTaskAgentInvoker._execution_contract(graph, activation, dispatch)
        delivery_record = self._delivery_artifact_store.get(contract["delivery_artifact_id"])
        delivery = self._delivery(delivery_record, contract, dispatch, execution)
        expected_task = dict(activation["task"])
        expected_task["status"] = "running"
        completion_key = canonical_json_sha256(
            {key: contract[key] for key in sorted(_REQUEST_FIELDS - {"artifact_id"})}
        )
        input_value = dict(contract)
        try:
            existing = self._completion_artifact_store.get(contract["artifact_id"])
        except ArtifactStoreError as exc:
            if exc.code != "ARTIFACT_NOT_FOUND":
                raise
        else:
            payload = self._replay(existing, input_value, completion_key, dispatch, delivery)
            native = self._kanban_port.read_completion(
                board=dispatch["board"],
                task_id=dispatch["task_id"],
                run_id=dispatch["run_id"],
                completion_key=completion_key,
                expected_task=expected_task,
                expected_result_text=f"Accepted controller delivery artifact {delivery_record.artifact_id}",
            )
            self._verify_native(native, payload)
            return self._public(existing, payload, idempotent_replay=True)

        native = self._kanban_port.complete_task(
            board=dispatch["board"],
            task_id=dispatch["task_id"],
            run_id=dispatch["run_id"],
            expected_snapshot_fingerprint=contract["expected_snapshot_fingerprint"],
            expected_task=expected_task,
            completion_key=completion_key,
            delivery_artifact_id=delivery_record.artifact_id,
            delivery_artifact_sha256=delivery_record.output_sha256,
            result_artifacts=list(delivery["result_artifacts"]),
            dispatch_artifact_id=dispatch_record.artifact_id,
            dispatch_artifact_sha256=dispatch_record.output_sha256,
        )
        readback = self._kanban_port.read_completion(
            board=dispatch["board"],
            task_id=dispatch["task_id"],
            run_id=dispatch["run_id"],
            completion_key=completion_key,
            expected_task=expected_task,
            expected_result_text=f"Accepted controller delivery artifact {delivery_record.artifact_id}",
        )
        if readback.get("run_id") != native.get("run_id"):
            raise KanbanCompletionError(
                "NATIVE_READBACK_MISMATCH", "native completion receipt changed"
            )
        payload = {
            "schema": _RECEIPT_SCHEMA,
            "board": dispatch["board"],
            "issue_number": dispatch["issue_number"],
            "generation": dispatch["generation"],
            "workspace_path": dispatch["workspace_path"],
            "task_id": dispatch["task_id"],
            "run_id": dispatch["run_id"],
            "dispatch_key": dispatch["dispatch_key"],
            "completion_key": completion_key,
            "dispatch_artifact_id": dispatch_record.artifact_id,
            "dispatch_artifact_sha256": dispatch_record.output_sha256,
            "delivery_artifact_id": delivery_record.artifact_id,
            "delivery_artifact_sha256": delivery_record.output_sha256,
            "delivery_status": delivery["status"],
            "result_artifacts": list(delivery["result_artifacts"]),
            "task_status": "done",
            "run_status": "done",
            "run_outcome": "completed",
        }
        self._verify_native(readback, payload)
        record = ArtifactRecord(
            artifact_id=contract["artifact_id"],
            run_id=str(dispatch["run_id"]),
            step_id="task_completion",
            attempt=1,
            executor_kind="deterministic",
            input_sha256=canonical_json_sha256(input_value),
            output_sha256=canonical_json_sha256(payload),
            timeout_seconds=30,
            validation_result="accepted",
            executor_identity="controller-task-completion-v1",
            payload=payload,
        )
        stored = self._completion_artifact_store.put(record, input_value=input_value)
        return self._public(
            stored, payload, idempotent_replay=not bool(native.get("completed"))
        )

    @staticmethod
    def _request(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != _REQUEST_FIELDS:
            raise KanbanCompletionError("REQUEST_INVALID", "completion request is invalid")
        request = dict(value)
        if request.get("schema") != _REQUEST_SCHEMA:
            raise KanbanCompletionError("REQUEST_INVALID", "completion schema is invalid")
        for field in ("artifact_id", "dispatch_artifact_id", "delivery_artifact_id"):
            request[field] = _identity(request.get(field), field)
        for field in (
            "dispatch_artifact_sha256",
            "delivery_artifact_sha256",
            "expected_snapshot_fingerprint",
        ):
            digest = request.get(field)
            if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
                raise KanbanCompletionError("REQUEST_INVALID", f"{field} is invalid")
        return request

    @staticmethod
    def _dispatch(record: ArtifactRecord, contract: Mapping[str, Any]) -> dict[str, Any]:
        if (
            record.artifact_id != contract["dispatch_artifact_id"]
            or record.output_sha256 != contract["dispatch_artifact_sha256"]
            or record.validation_result != "accepted"
            or record.step_id != "task_dispatch_claim"
            or record.executor_kind != "deterministic"
            or record.executor_identity != "controller-task-dispatch-v1"
            or record.output_sha256 != canonical_json_sha256(record.payload)
        ):
            raise KanbanCompletionError("DISPATCH_ARTIFACT_INVALID", "dispatch artifact is invalid")
        try:
            return AcceptedTaskDispatcher._validate_receipt_payload(record.payload)
        except KanbanDispatchError as exc:
            raise KanbanCompletionError(
                "DISPATCH_ARTIFACT_INVALID", "dispatch payload is invalid"
            ) from exc

    def _delivery(
        self,
        record: ArtifactRecord,
        contract: Mapping[str, Any],
        dispatch: Mapping[str, Any],
        execution: Mapping[str, Any],
    ) -> dict[str, Any]:
        if (
            record.artifact_id != contract["delivery_artifact_id"]
            or record.output_sha256 != contract["delivery_artifact_sha256"]
            or record.validation_result != "accepted"
            or record.step_id != "task_agent_delivery"
            or record.executor_kind != "agent"
            or record.output_sha256 != canonical_json_sha256(record.payload)
        ):
            raise KanbanCompletionError("DELIVERY_ARTIFACT_INVALID", "delivery artifact is invalid")
        payload = dict(record.payload)
        delivery = payload.get("delivery")
        execution_contract = payload.get("execution_contract")
        accepted_artifacts = payload.get("accepted_artifacts")
        try:
            agent_id = _identity(payload.get("agent_id"), "agent_id")
        except KanbanCompletionError as exc:
            raise KanbanCompletionError(
                "DELIVERY_ARTIFACT_INVALID", "delivery Agent identity is invalid"
            ) from exc
        invocation_key = canonical_json_sha256(
            {
                "dispatch_artifact_id": contract["dispatch_artifact_id"],
                "dispatch_artifact_sha256": contract["dispatch_artifact_sha256"],
                "task_id": dispatch["task_id"],
                "run_id": dispatch["run_id"],
            }
        )
        expected_artifacts = {
            "graph": {
                "artifact_id": dispatch["graph_artifact_id"],
                "sha256": dispatch["graph_artifact_sha256"],
            },
            "activation": {
                "artifact_id": dispatch["activation_artifact_id"],
                "sha256": dispatch["activation_artifact_sha256"],
            },
            "dispatch": {
                "artifact_id": contract["dispatch_artifact_id"],
                "sha256": contract["dispatch_artifact_sha256"],
            },
        }
        expected_fields = {
            "schema",
            "invocation_key",
            "agent_id",
            "board",
            "issue_number",
            "generation",
            "workspace_path",
            "task_id",
            "run_id",
            "dispatch_key",
            "dispatch_artifact_id",
            "dispatch_artifact_sha256",
            "execution_contract",
            "accepted_artifacts",
            "delivery",
        }
        if (
            set(payload) != expected_fields
            or payload.get("schema") != "controller-task-agent-invocation-receipt-v1"
            or not isinstance(delivery, Mapping)
            or not isinstance(execution_contract, Mapping)
            or not isinstance(accepted_artifacts, Mapping)
            or agent_id != record.executor_identity
            or payload.get("invocation_key") != invocation_key
            or payload.get("board") != dispatch["board"]
            or payload.get("issue_number") != dispatch["issue_number"]
            or payload.get("generation") != dispatch["generation"]
            or payload.get("workspace_path") != dispatch["workspace_path"]
            or payload.get("task_id") != dispatch["task_id"]
            or payload.get("run_id") != dispatch["run_id"]
            or payload.get("dispatch_key") != dispatch["dispatch_key"]
            or payload.get("dispatch_artifact_id") != contract["dispatch_artifact_id"]
            or payload.get("dispatch_artifact_sha256")
            != contract["dispatch_artifact_sha256"]
            or dict(accepted_artifacts) != expected_artifacts
            or set(execution_contract)
            != {"step_id", "allowed_toolsets", "skills", "workspace_path"}
            or execution_contract.get("step_id") != execution["step_id"]
            or tuple(execution_contract.get("allowed_toolsets") or ())
            != tuple(execution["toolsets"])
            or tuple(execution_contract.get("skills") or ())
            != tuple(execution["skills"])
            or execution_contract.get("workspace_path") != dispatch["workspace_path"]
        ):
            raise KanbanCompletionError("DELIVERY_ARTIFACT_INVALID", "delivery identity is invalid")
        delivery = dict(delivery)
        expected_step_id = _identity(execution_contract.get("step_id"), "execution step")
        if (
            set(delivery)
            != {
                "schema",
                "status",
                "task_id",
                "run_id",
                "dispatch_key",
                "result_artifacts",
            }
            or delivery.get("schema") != _DELIVERY_SCHEMA
            or delivery.get("task_id") != dispatch["task_id"]
            or delivery.get("run_id") != dispatch["run_id"]
            or delivery.get("dispatch_key") != dispatch["dispatch_key"]
        ):
            raise KanbanCompletionError(
                "DELIVERY_ARTIFACT_INVALID", "delivery identity is invalid"
            )
        if delivery.get("status") != "completed":
            raise KanbanCompletionError(
                "DELIVERY_NOT_COMPLETED", "only a completed delivery may complete a task"
            )
        refs = delivery.get("result_artifacts")
        if (
            not isinstance(refs, Sequence)
            or isinstance(refs, (str, bytes))
            or not refs
            or len(refs) > 32
        ):
            raise KanbanCompletionError(
                "DELIVERY_ARTIFACT_INVALID", "delivery result artifacts are invalid"
            )
        normalized: list[dict[str, str]] = []
        for index, ref in enumerate(refs):
            if not isinstance(ref, Mapping) or set(ref) != {
                "artifact_id",
                "step_id",
                "sha256",
            }:
                raise KanbanCompletionError(
                    "DELIVERY_ARTIFACT_INVALID", "delivery result artifact ref is invalid"
                )
            artifact_id = _identity(ref.get("artifact_id"), "result artifact")
            step_id = _identity(ref.get("step_id"), "result step")
            digest = ref.get("sha256")
            if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
                raise KanbanCompletionError(
                    "DELIVERY_ARTIFACT_INVALID", "delivery result artifact digest is invalid"
                )
            try:
                result_record = self._result_artifact_store.get(artifact_id)
            except ArtifactStoreError as exc:
                raise KanbanCompletionError(
                    "RESULT_ARTIFACT_INVALID", "result artifact was not found"
                ) from exc
            result_payload = result_record.payload
            logical_id = (
                result_payload.get("logical_id")
                if isinstance(result_payload, Mapping)
                else None
            )
            payload_value = (
                result_payload.get("payload")
                if isinstance(result_payload, Mapping)
                else None
            )
            payload_sha256 = (
                result_payload.get("payload_sha256")
                if isinstance(result_payload, Mapping)
                else None
            )
            expected_input = {
                "schema": "controller-task-result-input-v1",
                "invocation_key": invocation_key,
                "task_id": dispatch["task_id"],
                "run_id": dispatch["run_id"],
                "dispatch_key": dispatch["dispatch_key"],
                "logical_id": logical_id,
                "result_index": index,
                "step_id": expected_step_id,
                "payload_sha256": payload_sha256,
            }
            if (
                artifact_id != f"task-result:{invocation_key}:{index}"
                or result_record.validation_result != "accepted"
                or result_record.executor_kind != "agent"
                or result_record.executor_identity != agent_id
                or result_record.run_id != str(dispatch["run_id"])
                or result_record.input_sha256 != canonical_json_sha256(expected_input)
                or result_record.step_id != expected_step_id
                or step_id != expected_step_id
                or result_record.output_sha256 != digest
                or result_record.output_sha256 != canonical_json_sha256(result_record.payload)
                or not isinstance(result_payload, Mapping)
                or set(result_payload)
                != {
                    "schema",
                    "invocation_key",
                    "logical_id",
                    "result_index",
                    "step_id",
                    "payload_sha256",
                    "payload",
                }
                or result_payload.get("schema") != "controller-task-result-record-v1"
                or result_payload.get("invocation_key") != invocation_key
                or result_payload.get("result_index") != index
                or _identity(logical_id, "result logical id") != logical_id
                or result_payload.get("step_id") != expected_step_id
                or not isinstance(payload_value, Mapping)
                or payload_sha256 != canonical_json_sha256(payload_value)
            ):
                raise KanbanCompletionError(
                    "RESULT_ARTIFACT_INVALID", "result artifact provenance is invalid"
                )
            normalized.append(
                {"artifact_id": artifact_id, "step_id": step_id, "sha256": digest}
            )
        delivery["result_artifacts"] = normalized
        return delivery

    @staticmethod
    def _activation(record: ArtifactRecord, dispatch: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return AcceptedTaskActivator._payload(
                record,
                {
                    "artifact_sha256": dispatch["activation_artifact_sha256"],
                    "board": dispatch["board"],
                    "issue_number": dispatch["issue_number"],
                    "generation": dispatch["generation"],
                    "workspace_path": dispatch["workspace_path"],
                    "task_id": dispatch["task_id"],
                },
            )
        except Exception as exc:
            raise KanbanCompletionError(
                getattr(exc, "code", "ACTIVATION_ARTIFACT_INVALID"),
                "activation artifact is invalid for completion",
            ) from exc

    @staticmethod
    def _verify_native(native: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
        for field in (
            "task_id",
            "run_id",
            "completion_key",
            "task_status",
            "run_status",
            "run_outcome",
            "dispatch_artifact_id",
            "dispatch_artifact_sha256",
            "delivery_artifact_id",
            "delivery_artifact_sha256",
            "result_artifacts",
        ):
            if field == "result_artifacts":
                matches = canonical_json_sha256(native.get(field)) == canonical_json_sha256(
                    payload.get(field)
                )
            else:
                matches = native.get(field) == payload.get(field)
            if not matches:
                raise KanbanCompletionError(
                    "NATIVE_READBACK_MISMATCH", "native completion identity mismatch"
                )

    @staticmethod
    def _replay(
        record: ArtifactRecord,
        input_value: Mapping[str, Any],
        completion_key: str,
        dispatch: Mapping[str, Any],
        delivery: Mapping[str, Any],
    ) -> dict[str, Any]:
        if (
            record.validation_result != "accepted"
            or record.step_id != "task_completion"
            or record.executor_kind != "deterministic"
            or record.executor_identity != "controller-task-completion-v1"
            or record.input_sha256 != canonical_json_sha256(input_value)
            or record.output_sha256 != canonical_json_sha256(record.payload)
        ):
            raise KanbanCompletionError("CONFLICTING_REPLAY", "completion replay is invalid")
        payload = dict(record.payload)
        expected = {
            "schema": _RECEIPT_SCHEMA,
            "board": dispatch["board"],
            "issue_number": dispatch["issue_number"],
            "generation": dispatch["generation"],
            "workspace_path": dispatch["workspace_path"],
            "task_id": dispatch["task_id"],
            "run_id": dispatch["run_id"],
            "dispatch_key": dispatch["dispatch_key"],
            "completion_key": completion_key,
            "dispatch_artifact_id": input_value["dispatch_artifact_id"],
            "dispatch_artifact_sha256": input_value["dispatch_artifact_sha256"],
            "delivery_artifact_id": input_value["delivery_artifact_id"],
            "delivery_artifact_sha256": input_value["delivery_artifact_sha256"],
            "delivery_status": delivery["status"],
            "result_artifacts": list(delivery["result_artifacts"]),
            "task_status": "done",
            "run_status": "done",
            "run_outcome": "completed",
        }
        if (
            set(payload) != _COMPLETION_RECEIPT_FIELDS
            or canonical_json_sha256(payload) != canonical_json_sha256(expected)
        ):
            raise KanbanCompletionError("CONFLICTING_REPLAY", "completion replay conflicts")
        if not payload["result_artifacts"]:
            raise KanbanCompletionError("CONFLICTING_REPLAY", "completion result refs are empty")
        return payload

    @staticmethod
    def _public(
        record: ArtifactRecord, payload: Mapping[str, Any], *, idempotent_replay: bool
    ) -> dict[str, Any]:
        return {
            "schema": _RECEIPT_SCHEMA,
            "artifact_id": record.artifact_id,
            "artifact_sha256": record.output_sha256,
            "task_id": payload["task_id"],
            "run_id": payload["run_id"],
            "completion_key": payload["completion_key"],
            "task_status": payload["task_status"],
            "run_status": payload["run_status"],
            "verified": True,
            "idempotent_replay": idempotent_replay,
        }


def _identity(value: Any, field: str) -> str:
    if not isinstance(value, str) or value != value.strip() or _ID.fullmatch(value) is None:
        raise KanbanCompletionError("REQUEST_INVALID", f"{field} is invalid")
    return value
