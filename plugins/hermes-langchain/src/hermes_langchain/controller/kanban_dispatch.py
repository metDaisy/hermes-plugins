"""Accepted-artifact fencing for one controller-owned native task claim."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
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
from .kanban_graph_materialization import AcceptedTaskGraphMaterializer

_REQUEST_SCHEMA = "controller-kanban-dispatch-v1"
_RECEIPT_SCHEMA = "controller-task-dispatch-receipt-v1"
_RECEIPT_FIELDS = {
    "schema",
    "board",
    "issue_number",
    "generation",
    "workspace_path",
    "graph_artifact_id",
    "graph_artifact_sha256",
    "activation_artifact_id",
    "activation_artifact_sha256",
    "activation_key",
    "task_id",
    "run_id",
    "dispatch_key",
}
_REQUEST_FIELDS = {
    "schema",
    "artifact_id",
    "graph_artifact_id",
    "graph_artifact_sha256",
    "activation_artifact_id",
    "activation_artifact_sha256",
    "activation_key",
    "board",
    "issue_number",
    "generation",
    "workspace_path",
    "task_id",
    "expected_snapshot_fingerprint",
}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_BOARD = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TASK_COMPARE_FIELDS = (
    "title",
    "body_sha256",
    "assignee",
    "workspace_kind",
    "workspace_path",
    "skills",
    "idempotency_key",
)


class KanbanDispatchError(ValueError):
    """Stable fail-closed error for one controller-owned dispatch claim."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class NativeKanbanDispatchPort(Protocol):
    def snapshot(self, *, board: str) -> dict[str, Any]: ...

    def claim_task(
        self,
        *,
        board: str,
        task_id: str,
        expected_snapshot_fingerprint: str,
        expected_task: Mapping[str, Any],
        activation_key: str,
        dispatch_key: str,
        graph_artifact_id: str,
        graph_artifact_sha256: str,
        activation_artifact_id: str,
        activation_artifact_sha256: str,
    ) -> dict[str, Any]: ...

    def read_claim(
        self,
        *,
        board: str,
        task_id: str,
        dispatch_key: str,
    ) -> dict[str, Any]: ...


class HermesNativeKanbanDispatchPort:
    """Native Hermes adapter for an atomic fenced ready-task claim."""

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

    def claim_task(
        self,
        *,
        board: str,
        task_id: str,
        expected_snapshot_fingerprint: str,
        expected_task: Mapping[str, Any],
        activation_key: str,
        dispatch_key: str,
        graph_artifact_id: str,
        graph_artifact_sha256: str,
        activation_artifact_id: str,
        activation_artifact_sha256: str,
    ) -> dict[str, Any]:
        module, connect = self._dependencies()
        conn = connect(board=board)
        try:
            with module.write_txn(conn):
                existing = self._dispatch_receipt(conn, task_id=task_id, dispatch_key=dispatch_key)
                if existing is not None:
                    self._verify_existing_claim(module, conn, existing, expected_task)
                    return {
                        **self._read_claim(conn, task_id=task_id, dispatch_key=dispatch_key),
                        "previous_status": "ready",
                        "current_status": "running",
                        "claimed": False,
                    }
                snapshot = HermesNativeKanbanActivationPort._snapshot_value(module, conn, board)
                if fingerprint_activation_snapshot(snapshot) != expected_snapshot_fingerprint:
                    raise KanbanDispatchError("SNAPSHOT_CHANGED", "canonical snapshot changed before dispatch claim")
                task = module.get_task(conn, task_id)
                if task is None:
                    raise KanbanDispatchError("NATIVE_TASK_NOT_FOUND", "dispatch task was not found")
                task_value = HermesNativeKanbanActivationPort._task_value(task)
                self._verify_task(task_value, expected_task, allowed_statuses={"ready"})
                if not HermesNativeKanbanActivationPort._has_activation_receipt(
                    conn, task_id=task_id, activation_key=activation_key
                ):
                    raise KanbanDispatchError(
                        "ACTIVATION_RECEIPT_MISSING",
                        "dispatch task has no matching activation receipt",
                    )
                if not module._parents_satisfied(conn, task_id):
                    raise KanbanDispatchError(
                        "PARENT_DEPENDENCY_UNSATISFIED",
                        "dispatch task has an unfinished parent dependency",
                    )
                module._reclaim_dangling_run(
                    conn,
                    task_id,
                    statuses=("ready",),
                    now=module.time.time(),
                    note="controller dispatch invariant recovery",
                )
                now = int(module.time.time())
                run_id = module._claim_and_open_run(
                    conn,
                    task_id,
                    "ready",
                    f"controller:{dispatch_key}",
                    now + 1800,
                    now,
                    event_extra={
                        "controller_dispatch_key": dispatch_key,
                        "activation_key": activation_key,
                        "graph_artifact_id": graph_artifact_id,
                        "graph_artifact_sha256": graph_artifact_sha256,
                        "activation_artifact_id": activation_artifact_id,
                        "activation_artifact_sha256": activation_artifact_sha256,
                    },
                )
                if run_id is None:
                    raise KanbanDispatchError("NATIVE_CLAIM_FAILED", "native dispatch claim failed")
            claim = self._read_claim(conn, task_id=task_id, dispatch_key=dispatch_key)
            self._verify_task(claim["task"], expected_task, allowed_statuses={"running"})
            return {
                **claim,
                "previous_status": "ready",
                "current_status": "running",
                "claimed": True,
            }
        finally:
            conn.close()

    def read_claim(self, *, board: str, task_id: str, dispatch_key: str) -> dict[str, Any]:
        module, connect = self._dependencies()
        conn = connect(board=board)
        try:
            return self._read_claim(conn, task_id=task_id, dispatch_key=dispatch_key)
        finally:
            conn.close()

    @classmethod
    def _read_claim(cls, conn: Any, *, task_id: str, dispatch_key: str) -> dict[str, Any]:
        from hermes_cli import kanban_db

        receipt = cls._dispatch_receipt(conn, task_id=task_id, dispatch_key=dispatch_key)
        if receipt is None:
            raise KanbanDispatchError("NATIVE_CLAIM_NOT_FOUND", "native dispatch receipt was not found")
        task = kanban_db.get_task(conn, task_id)
        if task is None:
            raise KanbanDispatchError("NATIVE_TASK_NOT_FOUND", "dispatch task read-back was not found")
        run = conn.execute(
            "SELECT id, task_id, status, outcome, claim_lock FROM task_runs WHERE id = ? AND task_id = ?",
            (receipt["run_id"], task_id),
        ).fetchone()
        if run is None or str(run["status"] or "") != "running" or run["outcome"] is not None:
            raise KanbanDispatchError("NATIVE_READBACK_MISMATCH", "native dispatch run read-back mismatch")
        payload = receipt["payload"]
        claim_lock = f"controller:{dispatch_key}"
        required = {
            "lock",
            "run_id",
            "controller_dispatch_key",
            "activation_key",
            "graph_artifact_id",
            "graph_artifact_sha256",
            "activation_artifact_id",
            "activation_artifact_sha256",
        }
        if (
            not isinstance(payload, Mapping)
            or set(payload) != required | {"expires"}
            or payload.get("lock") != claim_lock
            or payload.get("run_id") != receipt["run_id"]
            or type(payload.get("expires")) is not int
            or str(run["claim_lock"] or "") != claim_lock
        ):
            raise KanbanDispatchError(
                "NATIVE_READBACK_MISMATCH", "native dispatch provenance read-back mismatch"
            )
        task_value = HermesNativeKanbanActivationPort._task_value(task)
        if task_value.get("status") != "running" or getattr(task, "current_run_id", None) != receipt["run_id"]:
            raise KanbanDispatchError("NATIVE_READBACK_MISMATCH", "native dispatch task read-back mismatch")
        return {
            "task_id": task_id,
            "run_id": receipt["run_id"],
            "dispatch_key": dispatch_key,
            "claim_lock": claim_lock,
            "activation_key": payload["activation_key"],
            "graph_artifact_id": payload["graph_artifact_id"],
            "graph_artifact_sha256": payload["graph_artifact_sha256"],
            "activation_artifact_id": payload["activation_artifact_id"],
            "activation_artifact_sha256": payload["activation_artifact_sha256"],
            "claimed": False,
            "task": task_value,
        }

    @staticmethod
    def _dispatch_receipt(conn: Any, *, task_id: str, dispatch_key: str) -> dict[str, Any] | None:
        rows = conn.execute(
            "SELECT run_id, payload FROM task_events WHERE task_id = ? AND kind = 'claimed' ORDER BY id DESC",
            (task_id,),
        ).fetchall()
        matches: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                continue
            if payload.get("controller_dispatch_key") == dispatch_key:
                run_id = row["run_id"] if row["run_id"] is not None else payload.get("run_id")
                if type(run_id) is not int or run_id < 1:
                    raise KanbanDispatchError("NATIVE_READBACK_MISMATCH", "native dispatch run identity is invalid")
                matches.append({"run_id": run_id, "payload": payload})
        if len(matches) > 1:
            raise KanbanDispatchError("CONFLICTING_REPLAY", "dispatch key has multiple native claims")
        return matches[0] if matches else None

    @classmethod
    def _verify_existing_claim(
        cls, module: Any, conn: Any, receipt: Mapping[str, Any], expected_task: Mapping[str, Any]
    ) -> None:
        task = module.get_task(conn, expected_task["task_id"])
        if task is None or getattr(task, "current_run_id", None) != receipt["run_id"]:
            raise KanbanDispatchError("CONFLICTING_REPLAY", "dispatch claim no longer owns the task")
        cls._verify_task(
            HermesNativeKanbanActivationPort._task_value(task),
            expected_task,
            allowed_statuses={"running"},
        )

    @staticmethod
    def _verify_task(
        actual: Mapping[str, Any],
        expected: Mapping[str, Any],
        *,
        allowed_statuses: set[str],
    ) -> None:
        try:
            AcceptedTaskActivator._verify_task(
                actual,
                expected,
                allowed_statuses=allowed_statuses,
            )
        except Exception as exc:
            raise KanbanDispatchError(
                "NATIVE_READBACK_MISMATCH",
                "native dispatch task did not match accepted provenance",
            ) from exc


class AcceptedTaskDispatcher:
    """Claim one accepted activated Implementation task and persist its receipt."""

    def __init__(
        self,
        *,
        graph_artifact_store: SQLiteArtifactStore,
        activation_artifact_store: SQLiteArtifactStore,
        dispatch_artifact_store: SQLiteArtifactStore,
        kanban_port: NativeKanbanDispatchPort,
    ) -> None:
        self._graph_artifact_store = graph_artifact_store
        self._activation_artifact_store = activation_artifact_store
        self._dispatch_artifact_store = dispatch_artifact_store
        self._kanban_port = kanban_port

    def dispatch(self, request: Any) -> dict[str, Any]:
        contract = self._request(request)
        graph_record = self._graph_artifact_store.get(contract["graph_artifact_id"])
        graph = self._graph(graph_record, contract)
        activation_record = self._activation_artifact_store.get(
            contract["activation_artifact_id"]
        )
        activation = self._activation(activation_record, contract)
        expected_task = self._expected_task(graph, activation, contract)
        dispatch_key = canonical_json_sha256(
            {key: contract[key] for key in sorted(_REQUEST_FIELDS - {"artifact_id"})}
        )
        input_value = dict(contract)
        try:
            existing = self._dispatch_artifact_store.get(contract["artifact_id"])
        except ArtifactStoreError as exc:
            if exc.code != "ARTIFACT_NOT_FOUND":
                raise
        else:
            payload = self._receipt_payload(existing, contract, input_value, dispatch_key)
            native = self._kanban_port.read_claim(
                board=contract["board"],
                task_id=contract["task_id"],
                dispatch_key=dispatch_key,
            )
            self._verify_native_claim(native, payload, expected_task)
            return self._public_receipt(existing, payload, idempotent_replay=True)

        claimed = self._kanban_port.claim_task(
            board=contract["board"],
            task_id=contract["task_id"],
            expected_snapshot_fingerprint=contract["expected_snapshot_fingerprint"],
            expected_task=expected_task,
            activation_key=contract["activation_key"],
            dispatch_key=dispatch_key,
            graph_artifact_id=graph_record.artifact_id,
            graph_artifact_sha256=graph_record.output_sha256,
            activation_artifact_id=activation_record.artifact_id,
            activation_artifact_sha256=activation_record.output_sha256,
        )
        native = self._kanban_port.read_claim(
            board=contract["board"], task_id=contract["task_id"], dispatch_key=dispatch_key
        )
        if int(native.get("run_id") or 0) != int(claimed.get("run_id") or -1):
            raise KanbanDispatchError("NATIVE_READBACK_MISMATCH", "native claim receipt changed")
        payload = self._build_payload(contract, graph_record, activation_record, dispatch_key, native)
        self._verify_native_claim(native, payload, expected_task)
        record = ArtifactRecord(
            artifact_id=contract["artifact_id"],
            run_id=graph_record.run_id,
            step_id="task_dispatch_claim",
            attempt=1,
            executor_kind="deterministic",
            input_sha256=canonical_json_sha256(input_value),
            output_sha256=canonical_json_sha256(payload),
            timeout_seconds=30,
            validation_result="accepted",
            executor_identity="controller-task-dispatch-v1",
            payload=payload,
        )
        stored = self._dispatch_artifact_store.put(record, input_value=input_value)
        return self._public_receipt(
            stored, payload, idempotent_replay=not bool(claimed.get("claimed"))
        )

    @staticmethod
    def _request(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != _REQUEST_FIELDS:
            raise KanbanDispatchError("REQUEST_INVALID", "dispatch request is invalid")
        request = dict(value)
        if request.get("schema") != _REQUEST_SCHEMA:
            raise KanbanDispatchError("REQUEST_INVALID", "dispatch request schema is invalid")
        for field in (
            "artifact_id",
            "graph_artifact_id",
            "activation_artifact_id",
            "task_id",
        ):
            request[field] = _identity(request.get(field), field, _ID, 128)
        request["board"] = _identity(request.get("board"), "board", _BOARD, 64)
        for field in (
            "graph_artifact_sha256",
            "activation_artifact_sha256",
            "activation_key",
            "expected_snapshot_fingerprint",
        ):
            request[field] = _identity(request.get(field), field, _HEX_SHA256, 64)
        for field in ("issue_number", "generation"):
            if type(request.get(field)) is not int or request[field] < 1:
                raise KanbanDispatchError("REQUEST_INVALID", f"request.{field} is invalid")
        request["workspace_path"] = _workspace(request.get("workspace_path"))
        return request

    @staticmethod
    def _graph(record: ArtifactRecord, contract: dict[str, Any]) -> dict[str, Any]:
        try:
            return AcceptedTaskGraphMaterializer._graph(
                record,
                {
                    "artifact_sha256": contract["graph_artifact_sha256"],
                    "board": contract["board"],
                    "issue_number": contract["issue_number"],
                    "generation": contract["generation"],
                    "workspace_path": contract["workspace_path"],
                },
            )
        except Exception as exc:
            raise KanbanDispatchError(
                getattr(exc, "code", "ARTIFACT_INVALID"),
                "accepted graph artifact is invalid for dispatch",
            ) from exc

    @staticmethod
    def _activation(record: ArtifactRecord, contract: dict[str, Any]) -> dict[str, Any]:
        try:
            return AcceptedTaskActivator._payload(
                record,
                {
                    "artifact_sha256": contract["activation_artifact_sha256"],
                    "board": contract["board"],
                    "issue_number": contract["issue_number"],
                    "generation": contract["generation"],
                    "workspace_path": contract["workspace_path"],
                    "task_id": contract["task_id"],
                },
            )
        except Exception as exc:
            raise KanbanDispatchError(
                getattr(exc, "code", "ARTIFACT_INVALID"),
                "accepted activation artifact is invalid for dispatch",
            ) from exc

    @staticmethod
    def _expected_task(
        graph: Mapping[str, Any], activation: Mapping[str, Any], contract: Mapping[str, Any]
    ) -> dict[str, Any]:
        task = dict(activation["task"])
        candidates = [
            card
            for card in graph["cards"]
            if isinstance(card, Mapping)
            and card.get("idempotency_key") == task.get("idempotency_key")
        ]
        if len(candidates) != 1 or candidates[0].get("role") not in {
            "implementation",
            "review",
            "summary",
        }:
            raise KanbanDispatchError(
                "ARTIFACT_IDENTITY_MISMATCH",
                "activation task does not identify one accepted graph card",
            )
        candidate = candidates[0]
        expected = {
            "title": candidate.get("title"),
            "body_sha256": candidate.get("body_sha256"),
            "assignee": candidate.get("assignee"),
            "workspace_kind": "dir",
            "workspace_path": contract["workspace_path"],
            "skills": list(candidate.get("skills") or []),
            "idempotency_key": candidate.get("idempotency_key"),
        }
        actual = {field: task.get(field) for field in _TASK_COMPARE_FIELDS}
        actual["skills"] = list(task.get("skills") or [])
        if actual != expected or task.get("task_id") != contract["task_id"]:
            raise KanbanDispatchError(
                "ARTIFACT_IDENTITY_MISMATCH",
                "activation task does not match its accepted graph card",
            )
        task["status"] = "ready"
        return task

    @staticmethod
    def _build_payload(
        contract: Mapping[str, Any],
        graph_record: ArtifactRecord,
        activation_record: ArtifactRecord,
        dispatch_key: str,
        native: Mapping[str, Any],
    ) -> dict[str, Any]:
        run_id = native.get("run_id")
        if type(run_id) is not int or run_id < 1:
            raise KanbanDispatchError("NATIVE_READBACK_MISMATCH", "native run identity is invalid")
        return {
            "schema": _RECEIPT_SCHEMA,
            "board": contract["board"],
            "issue_number": contract["issue_number"],
            "generation": contract["generation"],
            "workspace_path": contract["workspace_path"],
            "graph_artifact_id": graph_record.artifact_id,
            "graph_artifact_sha256": graph_record.output_sha256,
            "activation_artifact_id": activation_record.artifact_id,
            "activation_artifact_sha256": activation_record.output_sha256,
            "activation_key": contract["activation_key"],
            "task_id": contract["task_id"],
            "run_id": run_id,
            "dispatch_key": dispatch_key,
        }

    @staticmethod
    def _receipt_payload(
        record: ArtifactRecord,
        contract: Mapping[str, Any],
        input_value: Mapping[str, Any],
        dispatch_key: str,
    ) -> dict[str, Any]:
        if (
            record.validation_result != "accepted"
            or record.step_id != "task_dispatch_claim"
            or record.executor_kind != "deterministic"
            or record.executor_identity != "controller-task-dispatch-v1"
            or record.input_sha256 != canonical_json_sha256(input_value)
            or record.output_sha256 != canonical_json_sha256(record.payload)
        ):
            raise KanbanDispatchError("CONFLICTING_REPLAY", "dispatch artifact replay is invalid")
        payload = AcceptedTaskDispatcher._validate_receipt_payload(record.payload)
        if payload.get("dispatch_key") != dispatch_key:
            raise KanbanDispatchError("CONFLICTING_REPLAY", "dispatch artifact replay conflicts")
        expected = {
            "board": contract["board"],
            "issue_number": contract["issue_number"],
            "generation": contract["generation"],
            "workspace_path": contract["workspace_path"],
            "graph_artifact_id": contract["graph_artifact_id"],
            "graph_artifact_sha256": contract["graph_artifact_sha256"],
            "activation_artifact_id": contract["activation_artifact_id"],
            "activation_artifact_sha256": contract["activation_artifact_sha256"],
            "activation_key": contract["activation_key"],
            "task_id": contract["task_id"],
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise KanbanDispatchError("CONFLICTING_REPLAY", "dispatch artifact identity conflicts")
        if type(payload.get("run_id")) is not int or payload["run_id"] < 1:
            raise KanbanDispatchError("CONFLICTING_REPLAY", "dispatch artifact run is invalid")
        return payload

    @staticmethod
    def _validate_receipt_payload(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != _RECEIPT_FIELDS:
            raise KanbanDispatchError("CONFLICTING_REPLAY", "dispatch receipt schema is invalid")
        payload = dict(value)
        if payload.get("schema") != _RECEIPT_SCHEMA:
            raise KanbanDispatchError("CONFLICTING_REPLAY", "dispatch receipt schema is invalid")
        payload["board"] = _identity(payload.get("board"), "board", _BOARD, 64)
        for field in (
            "graph_artifact_id",
            "activation_artifact_id",
            "task_id",
        ):
            payload[field] = _identity(payload.get(field), field, _ID, 128)
        for field in (
            "graph_artifact_sha256",
            "activation_artifact_sha256",
            "activation_key",
            "dispatch_key",
        ):
            payload[field] = _identity(payload.get(field), field, _HEX_SHA256, 64)
        for field in ("issue_number", "generation", "run_id"):
            if type(payload.get(field)) is not int or payload[field] < 1:
                raise KanbanDispatchError(
                    "CONFLICTING_REPLAY", f"dispatch receipt {field} is invalid"
                )
        payload["workspace_path"] = _workspace(payload.get("workspace_path"))
        return payload

    @staticmethod
    def _verify_native_claim(
        native: Mapping[str, Any], payload: Mapping[str, Any], expected_task: Mapping[str, Any]
    ) -> None:
        if (
            native.get("task_id") != payload["task_id"]
            or native.get("dispatch_key") != payload["dispatch_key"]
            or native.get("run_id") != payload["run_id"]
            or native.get("claim_lock") != f"controller:{payload['dispatch_key']}"
            or native.get("activation_key") != payload["activation_key"]
            or native.get("graph_artifact_id") != payload["graph_artifact_id"]
            or native.get("graph_artifact_sha256") != payload["graph_artifact_sha256"]
            or native.get("activation_artifact_id") != payload["activation_artifact_id"]
            or native.get("activation_artifact_sha256")
            != payload["activation_artifact_sha256"]
        ):
            raise KanbanDispatchError("NATIVE_READBACK_MISMATCH", "native claim identity mismatch")
        task = native.get("task")
        if not isinstance(task, Mapping):
            raise KanbanDispatchError("NATIVE_READBACK_MISMATCH", "native claim task is invalid")
        HermesNativeKanbanDispatchPort._verify_task(
            task, expected_task, allowed_statuses={"running"}
        )

    @staticmethod
    def _public_receipt(
        record: ArtifactRecord, payload: Mapping[str, Any], *, idempotent_replay: bool
    ) -> dict[str, Any]:
        return {
            "schema": _RECEIPT_SCHEMA,
            "artifact_id": record.artifact_id,
            "artifact_sha256": record.output_sha256,
            "board": payload["board"],
            "issue_number": payload["issue_number"],
            "generation": payload["generation"],
            "workspace_path": payload["workspace_path"],
            "task_id": payload["task_id"],
            "run_id": payload["run_id"],
            "dispatch_key": payload["dispatch_key"],
            "verified": True,
            "idempotent_replay": idempotent_replay,
        }


def _identity(value: Any, field: str, pattern: re.Pattern[str], maximum: int) -> str:
    if not isinstance(value, str) or value != value.strip() or not value or len(value) > maximum:
        raise KanbanDispatchError("REQUEST_INVALID", f"request.{field} is invalid")
    if pattern.fullmatch(value) is None:
        raise KanbanDispatchError("REQUEST_INVALID", f"request.{field} is invalid")
    return value


def _workspace(value: Any) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise KanbanDispatchError("REQUEST_INVALID", "request.workspace_path is invalid")
    path = Path(value)
    if not path.is_absolute() or path.resolve().as_posix() != path.as_posix() or not path.is_dir():
        raise KanbanDispatchError("REQUEST_INVALID", "request.workspace_path is unavailable")
    return path.as_posix()
