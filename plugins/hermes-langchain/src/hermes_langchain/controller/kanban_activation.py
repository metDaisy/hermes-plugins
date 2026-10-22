"""Accepted-artifact fencing for one native Kanban task activation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from .artifact_store import SQLiteArtifactStore, canonical_json_sha256

_REQUEST_SCHEMA = "controller-kanban-activation-v1"
_PAYLOAD_SCHEMA = "controller-task-activation-v1"
_RECEIPT_SCHEMA = "controller-kanban-activation-receipt-v1"
_REQUEST_FIELDS = {
    "schema",
    "artifact_id",
    "artifact_sha256",
    "board",
    "issue_number",
    "generation",
    "workspace_path",
    "task_id",
    "expected_snapshot_fingerprint",
}
_PAYLOAD_FIELDS = {
    "schema",
    "board",
    "issue_number",
    "generation",
    "workspace_path",
    "reason_code",
    "task",
}
_TASK_ARTIFACT_FIELDS = {
    "task_id",
    "title",
    "body_sha256",
    "assignee",
    "status",
    "workspace_kind",
    "workspace_path",
    "skills",
    "idempotency_key",
}
_SNAPSHOT_TASK_FIELDS = (
    "id",
    "title",
    "body",
    "assignee",
    "status",
    "workspace_kind",
    "workspace_path",
    "skills",
    "idempotency_key",
)
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_BOARD_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PROGRESS_STATUSES = frozenset({"ready", "running", "review", "done"})
_ACTIVATABLE_STATUSES = frozenset({"todo", "blocked"})


class KanbanActivationError(ValueError):
    """Stable fail-closed error for one accepted task activation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class NativeKanbanActivationPort(Protocol):
    def snapshot(self, *, board: str) -> dict[str, Any]: ...

    def promote_task(
        self,
        *,
        board: str,
        task_id: str,
        reason: str,
        expected_snapshot_fingerprint: str,
        expected_task: Mapping[str, Any],
        activation_key: str,
        artifact_id: str,
        artifact_sha256: str,
    ) -> dict[str, Any]: ...

    def has_activation_receipt(
        self,
        *,
        board: str,
        task_id: str,
        activation_key: str,
    ) -> bool: ...

    def has_dependency_promotion(self, *, board: str, task_id: str) -> bool: ...

    def read_task(self, *, board: str, task_id: str) -> dict[str, Any]: ...


class HermesNativeKanbanActivationPort:
    """Canonical Hermes adapter for fenced promotion and exact task read-back."""

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
            return self._snapshot_value(module, conn, board)
        finally:
            conn.close()

    def promote_task(
        self,
        *,
        board: str,
        task_id: str,
        reason: str,
        expected_snapshot_fingerprint: str,
        expected_task: Mapping[str, Any],
        activation_key: str,
        artifact_id: str,
        artifact_sha256: str,
    ) -> dict[str, Any]:
        module, connect = self._dependencies()
        conn = connect(board=board)
        try:
            with module.write_txn(conn):
                boundary_snapshot = self._snapshot_value(module, conn, board)
                before = module.get_task(conn, task_id)
                if before is None:
                    raise KanbanActivationError(
                        "NATIVE_TASK_NOT_FOUND",
                        "activation task was not found",
                    )
                before_value = self._task_value(before)
                previous_status = str(before_value.get("status") or "")
                activation_already_applied = self._has_activation_receipt(
                    conn,
                    task_id=task_id,
                    activation_key=activation_key,
                )
                if activation_already_applied:
                    if (
                        previous_status in _PROGRESS_STATUSES
                        and self._task_matches(
                            before_value,
                            expected_task,
                            _PROGRESS_STATUSES,
                        )
                    ):
                        promoted = False
                        attested = False
                    else:
                        raise KanbanActivationError(
                            "CONFLICTING_REPLAY",
                            "activation has a conflicting replay",
                        )
                else:
                    if (
                        fingerprint_activation_snapshot(boundary_snapshot)
                        != expected_snapshot_fingerprint
                    ):
                        raise KanbanActivationError(
                            "SNAPSHOT_CHANGED",
                            "canonical snapshot changed before activation mutation",
                        )
                    parents = conn.execute(
                        "SELECT t.id, t.status FROM tasks t "
                        "JOIN task_links l ON l.parent_id = t.id "
                        "WHERE l.child_id = ?",
                        (task_id,),
                    ).fetchall()
                    if any(parent["status"] not in ("done", "archived") for parent in parents):
                        raise KanbanActivationError(
                            "PARENT_DEPENDENCY_UNSATISFIED",
                            "activation task has an unfinished parent dependency",
                        )
                    dependency_attestation = (
                        previous_status == "ready"
                        and self._has_dependency_promotion(
                            module, conn, task_id=task_id
                        )
                    )
                    allowed_statuses = (
                        {"ready"} if dependency_attestation else _ACTIVATABLE_STATUSES
                    )
                    if not self._task_matches(
                        before_value,
                        expected_task,
                        allowed_statuses,
                    ):
                        raise KanbanActivationError(
                            "NATIVE_READBACK_MISMATCH",
                            "native activation task changed before mutation",
                        )
                    if dependency_attestation:
                        promoted = False
                        attested = True
                    else:
                        updated = conn.execute(
                            "UPDATE tasks SET status = 'ready' "
                            "WHERE id = ? AND status IN ('todo', 'blocked')",
                            (task_id,),
                        )
                        if updated.rowcount != 1:
                            raise KanbanActivationError(
                                "NATIVE_PROMOTION_FAILED",
                                "native activation promotion was not confirmed",
                            )
                        promoted = True
                        attested = False
                    metadata = {
                        "actor": "hermes-langchain-controller",
                        "reason": reason,
                        "controller_activation_key": activation_key,
                        "artifact_id": artifact_id,
                        "artifact_sha256": artifact_sha256,
                        "expected_snapshot_fingerprint": expected_snapshot_fingerprint,
                    }
                    if attested:
                        metadata["dependency_promotion_attested"] = True
                    module._append_event(
                        conn,
                        task_id,
                        "promoted_manual",
                        metadata,
                    )
            after = module.get_task(conn, task_id)
            current_status = str(getattr(after, "status", "") or "") if after else ""
            if current_status not in _PROGRESS_STATUSES:
                raise KanbanActivationError(
                    "NATIVE_PROMOTION_FAILED",
                    "native activation promotion was not confirmed",
                )
            return {
                "task_id": task_id,
                "previous_status": previous_status,
                "current_status": current_status,
                "promoted": promoted,
                "attested": attested,
            }
        finally:
            conn.close()

    def has_activation_receipt(
        self,
        *,
        board: str,
        task_id: str,
        activation_key: str,
    ) -> bool:
        _module, connect = self._dependencies()
        conn = connect(board=board)
        try:
            return self._has_activation_receipt(
                conn,
                task_id=task_id,
                activation_key=activation_key,
            )
        finally:
            conn.close()

    def has_dependency_promotion(self, *, board: str, task_id: str) -> bool:
        module, connect = self._dependencies()
        conn = connect(board=board)
        try:
            return self._has_dependency_promotion(module, conn, task_id=task_id)
        finally:
            conn.close()

    @staticmethod
    def _has_dependency_promotion(module: Any, conn: Any, *, task_id: str) -> bool:
        task = module.get_task(conn, task_id)
        if task is None or getattr(task, "status", None) != "ready":
            return False
        row = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return bool(
            row
            and row["kind"] == "promoted"
            and module._parents_satisfied(conn, task_id)
        )

    def read_task(self, *, board: str, task_id: str) -> dict[str, Any]:
        module, connect = self._dependencies()
        conn = connect(board=board)
        try:
            task = module.get_task(conn, task_id)
            if task is None:
                raise KanbanActivationError(
                    "NATIVE_TASK_NOT_FOUND",
                    "activation task read-back was not found",
                )
            return self._task_value(task)
        finally:
            conn.close()

    @classmethod
    def _snapshot_value(cls, module: Any, conn: Any, board: str) -> dict[str, Any]:
        links = conn.execute(
            "SELECT parent_id, child_id FROM task_links ORDER BY parent_id, child_id"
        ).fetchall()
        return {
            "board": board,
            "tasks": [
                cls._task_value(task)
                for task in module.list_tasks(conn, include_archived=True)
            ],
            "links": [
                {"parent_id": str(link["parent_id"]), "child_id": str(link["child_id"])}
                for link in links
            ],
        }

    @staticmethod
    def _has_activation_receipt(
        conn: Any,
        *,
        task_id: str,
        activation_key: str,
    ) -> bool:
        rows = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'promoted_manual' ORDER BY id DESC",
            (task_id,),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                continue
            if payload.get("controller_activation_key") == activation_key:
                return True
        return False

    @staticmethod
    def _task_matches(
        actual: Mapping[str, Any],
        expected: Mapping[str, Any],
        allowed_statuses: set[str] | frozenset[str],
    ) -> bool:
        try:
            AcceptedTaskActivator._verify_task(
                actual,
                expected,
                allowed_statuses=allowed_statuses,
            )
        except KanbanActivationError:
            return False
        return True

    @staticmethod
    def _task_value(task: Any) -> dict[str, Any]:
        return {
            "id": str(getattr(task, "id")),
            "title": getattr(task, "title", None),
            "body": getattr(task, "body", None),
            "assignee": getattr(task, "assignee", None),
            "status": getattr(task, "status", None),
            "workspace_kind": getattr(task, "workspace_kind", None),
            "workspace_path": getattr(task, "workspace_path", None),
            "skills": list(getattr(task, "skills", None) or []),
            "idempotency_key": getattr(task, "idempotency_key", None),
        }


def fingerprint_activation_snapshot(snapshot: Any) -> str:
    """Return a stable digest over activation-relevant native task state."""

    if not isinstance(snapshot, Mapping):
        raise KanbanActivationError("SNAPSHOT_INVALID", "native snapshot is invalid")
    board = _text(snapshot.get("board"), "snapshot.board")
    tasks = snapshot.get("tasks")
    if not isinstance(tasks, Sequence) or isinstance(tasks, (str, bytes)):
        raise KanbanActivationError("SNAPSHOT_INVALID", "native snapshot tasks are invalid")
    normalized: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, Mapping):
            raise KanbanActivationError("SNAPSHOT_INVALID", "native snapshot task is invalid")
        normalized.append(
            {field: _json_value(task.get(field)) for field in _SNAPSHOT_TASK_FIELDS}
        )
    normalized.sort(key=lambda task: str(task.get("id") or ""))
    links = snapshot.get("links")
    if not isinstance(links, Sequence) or isinstance(links, (str, bytes)):
        raise KanbanActivationError("SNAPSHOT_INVALID", "native snapshot links are invalid")
    normalized_links: list[dict[str, str]] = []
    for link in links:
        if not isinstance(link, Mapping):
            raise KanbanActivationError("SNAPSHOT_INVALID", "native snapshot link is invalid")
        normalized_links.append(
            {
                "parent_id": _text(link.get("parent_id"), "snapshot.link.parent_id"),
                "child_id": _text(link.get("child_id"), "snapshot.link.child_id"),
            }
        )
    normalized_links.sort(key=lambda link: (link["parent_id"], link["child_id"]))
    return canonical_json_sha256(
        {"board": board, "tasks": normalized, "links": normalized_links}
    )


class AcceptedTaskActivator:
    """Promote one artifact-bound task and verify exact native read-back."""

    def __init__(
        self,
        *,
        artifact_store: SQLiteArtifactStore,
        kanban_port: NativeKanbanActivationPort,
    ) -> None:
        self._artifact_store = artifact_store
        self._kanban_port = kanban_port

    def activate(self, request: Any) -> dict[str, Any]:
        contract = self._request(request)
        record = self._artifact_store.get(contract["artifact_id"])
        payload = self._payload(record, contract)
        activation_key = canonical_json_sha256(
            {
                "schema": _REQUEST_SCHEMA,
                "artifact_id": record.artifact_id,
                "artifact_sha256": record.output_sha256,
                "board": contract["board"],
                "issue_number": contract["issue_number"],
                "generation": contract["generation"],
                "workspace_path": contract["workspace_path"],
                "task_id": contract["task_id"],
                "expected_snapshot_fingerprint": contract[
                    "expected_snapshot_fingerprint"
                ],
            }
        )
        snapshot = self._kanban_port.snapshot(board=contract["board"])
        task = self._find_task(snapshot, contract["task_id"])
        current_status = str(task.get("status") or "")
        if current_status in _PROGRESS_STATUSES:
            try:
                self._verify_task(
                    task,
                    payload["task"],
                    allowed_statuses=_PROGRESS_STATUSES,
                )
            except KanbanActivationError as exc:
                raise KanbanActivationError(
                    "CONFLICTING_REPLAY",
                    "activation has a conflicting replay",
                ) from exc
            has_receipt = self._kanban_port.has_activation_receipt(
                board=contract["board"],
                task_id=contract["task_id"],
                activation_key=activation_key,
            )
            dependency_promoted = (
                current_status == "ready"
                and self._kanban_port.has_dependency_promotion(
                    board=contract["board"],
                    task_id=contract["task_id"],
                )
            )
            if not has_receipt and not dependency_promoted:
                raise KanbanActivationError(
                    "CONFLICTING_REPLAY",
                    "activation has a conflicting replay",
                )
        else:
            if fingerprint_activation_snapshot(snapshot) != contract[
                "expected_snapshot_fingerprint"
            ]:
                raise KanbanActivationError(
                    "SNAPSHOT_CHANGED",
                    "canonical snapshot changed before activation",
                )
            self._verify_task(
                task,
                payload["task"],
                allowed_statuses={payload["task"]["status"]},
            )

        result = self._kanban_port.promote_task(
            board=contract["board"],
            task_id=contract["task_id"],
            reason=payload["reason_code"],
            expected_snapshot_fingerprint=contract["expected_snapshot_fingerprint"],
            expected_task=payload["task"],
            activation_key=activation_key,
            artifact_id=record.artifact_id,
            artifact_sha256=record.output_sha256,
        )
        read_back = self._kanban_port.read_task(
            board=contract["board"],
            task_id=contract["task_id"],
        )
        self._verify_task(read_back, payload["task"], allowed_statuses=_PROGRESS_STATUSES)
        if not self._kanban_port.has_activation_receipt(
            board=contract["board"],
            task_id=contract["task_id"],
            activation_key=activation_key,
        ):
            raise KanbanActivationError(
                "NATIVE_PROMOTION_FAILED",
                "native activation receipt was not confirmed",
            )
        current_status = _text(read_back.get("status"), "task.status")
        return self._receipt(
            record=record,
            contract=contract,
            activation_key=activation_key,
            previous_status=_text(
                result.get("previous_status"), "activation.previous_status"
            ),
            current_status=current_status,
            idempotent_replay=not bool(
                result.get("promoted") or result.get("attested")
            ),
        )

    @staticmethod
    def _receipt(
        *,
        record: Any,
        contract: dict[str, Any],
        activation_key: str,
        previous_status: str,
        current_status: str,
        idempotent_replay: bool,
    ) -> dict[str, Any]:
        return {
            "schema": _RECEIPT_SCHEMA,
            "artifact_id": record.artifact_id,
            "artifact_sha256": record.output_sha256,
            "board": contract["board"],
            "issue_number": contract["issue_number"],
            "generation": contract["generation"],
            "workspace_path": contract["workspace_path"],
            "task_id": contract["task_id"],
            "activation_key": activation_key,
            "previous_status": previous_status,
            "current_status": current_status,
            "verified": True,
            "idempotent_replay": idempotent_replay,
        }

    @staticmethod
    def _request(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != _REQUEST_FIELDS:
            raise KanbanActivationError("REQUEST_INVALID", "activation request is invalid")
        request = dict(value)
        if request.get("schema") != _REQUEST_SCHEMA:
            raise KanbanActivationError("REQUEST_INVALID", "activation request schema is invalid")
        request["artifact_id"] = _identity(
            request.get("artifact_id"),
            "request.artifact_id",
            pattern=_ARTIFACT_ID,
            max_length=128,
        )
        request["board"] = _identity(
            request.get("board"),
            "request.board",
            pattern=_BOARD_SLUG,
            max_length=64,
        )
        request["task_id"] = _identity(
            request.get("task_id"),
            "request.task_id",
            pattern=_TASK_ID,
            max_length=128,
        )
        request["workspace_path"] = _workspace_path(
            request.get("workspace_path"),
            "request.workspace_path",
        )
        for field in (
            "artifact_sha256",
            "expected_snapshot_fingerprint",
        ):
            request[field] = _identity(
                request.get(field),
                f"request.{field}",
                pattern=_HEX_SHA256,
                max_length=64,
            )
        for field in ("artifact_sha256", "expected_snapshot_fingerprint"):
            if not _HEX_SHA256.fullmatch(request[field]):
                raise KanbanActivationError("REQUEST_INVALID", f"request.{field} is invalid")
        for field in ("issue_number", "generation"):
            value = request.get(field)
            if type(value) is not int or value < 1:
                raise KanbanActivationError("REQUEST_INVALID", f"request.{field} is invalid")
        return request

    @staticmethod
    def _payload(record: Any, contract: dict[str, Any]) -> dict[str, Any]:
        if record.validation_result != "accepted":
            raise KanbanActivationError("ARTIFACT_NOT_ACCEPTED", "activation artifact is not accepted")
        if (
            record.executor_kind != "deterministic"
            or record.executor_identity != "controller-task-activation"
        ):
            raise KanbanActivationError(
                "ARTIFACT_PRODUCER_INVALID",
                "activation artifact producer is invalid",
            )
        if record.output_sha256 != contract["artifact_sha256"]:
            raise KanbanActivationError("ARTIFACT_DIGEST_MISMATCH", "activation artifact digest mismatch")
        payload = record.payload
        if not isinstance(payload, Mapping) or set(payload) != _PAYLOAD_FIELDS:
            raise KanbanActivationError("ARTIFACT_INVALID", "activation artifact payload is invalid")
        payload = dict(payload)
        if payload.get("schema") != _PAYLOAD_SCHEMA:
            raise KanbanActivationError("ARTIFACT_INVALID", "activation artifact schema is invalid")
        payload["board"] = _identity(
            payload.get("board"),
            "artifact.board",
            pattern=_BOARD_SLUG,
            max_length=64,
            code="ARTIFACT_INVALID",
        )
        payload["workspace_path"] = _workspace_path(
            payload.get("workspace_path"),
            "artifact.workspace_path",
            code="ARTIFACT_INVALID",
        )
        for field in ("board", "workspace_path"):
            if payload[field] != contract[field]:
                raise KanbanActivationError("ARTIFACT_IDENTITY_MISMATCH", "activation artifact identity mismatch")
        for field in ("issue_number", "generation"):
            value = payload.get(field)
            if type(value) is not int or value < 1:
                raise KanbanActivationError(
                    "ARTIFACT_INVALID",
                    "activation artifact numeric identity is invalid",
                )
            if value != contract[field]:
                raise KanbanActivationError("ARTIFACT_IDENTITY_MISMATCH", "activation artifact identity mismatch")
        task = payload.get("task")
        if not isinstance(task, Mapping) or set(task) != _TASK_ARTIFACT_FIELDS:
            raise KanbanActivationError("ARTIFACT_INVALID", "activation task contract is invalid")
        task = dict(task)
        if task.get("task_id") != contract["task_id"]:
            raise KanbanActivationError("ARTIFACT_IDENTITY_MISMATCH", "activation task identity mismatch")
        if task.get("workspace_path") != contract["workspace_path"]:
            raise KanbanActivationError("ARTIFACT_IDENTITY_MISMATCH", "activation workspace mismatch")
        task["task_id"] = _identity(
            task.get("task_id"),
            "artifact.task.task_id",
            pattern=_TASK_ID,
            max_length=128,
            code="ARTIFACT_INVALID",
        )
        task["title"] = _bounded_text(
            task.get("title"),
            "artifact.task.title",
            max_length=512,
            code="ARTIFACT_INVALID",
        )
        task["assignee"] = _identity(
            task.get("assignee"),
            "artifact.task.assignee",
            pattern=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"),
            max_length=64,
            code="ARTIFACT_INVALID",
        )
        task["workspace_kind"] = _identity(
            task.get("workspace_kind"),
            "artifact.task.workspace_kind",
            pattern=re.compile(r"^[a-z][a-z0-9_-]{0,31}$"),
            max_length=32,
            code="ARTIFACT_INVALID",
        )
        task["workspace_path"] = _workspace_path(
            task.get("workspace_path"),
            "artifact.task.workspace_path",
            code="ARTIFACT_INVALID",
        )
        task["idempotency_key"] = _identity(
            task.get("idempotency_key"),
            "artifact.task.idempotency_key",
            pattern=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$"),
            max_length=256,
            code="ARTIFACT_INVALID",
        )
        skills = task.get("skills")
        if (
            not isinstance(skills, Sequence)
            or isinstance(skills, (str, bytes))
            or any(
                not isinstance(skill, str)
                or skill != skill.strip()
                or not skill
                or len(skill) > 128
                for skill in skills
            )
        ):
            raise KanbanActivationError(
                "ARTIFACT_INVALID",
                "activation task skills are invalid",
            )
        body_sha256 = _text(task.get("body_sha256"), "artifact.task.body_sha256")
        if not _HEX_SHA256.fullmatch(body_sha256):
            raise KanbanActivationError("ARTIFACT_INVALID", "activation body digest is invalid")
        payload["reason_code"] = _text(payload.get("reason_code"), "artifact.reason_code")
        if not _REASON_CODE.fullmatch(payload["reason_code"]):
            raise KanbanActivationError(
                "ARTIFACT_INVALID",
                "activation reason code is invalid",
            )
        if task.get("status") not in _ACTIVATABLE_STATUSES:
            raise KanbanActivationError(
                "ARTIFACT_INVALID",
                "activation task status is invalid",
            )
        payload["task"] = task
        return payload

    @staticmethod
    def _find_task(snapshot: Mapping[str, Any], task_id: str) -> Mapping[str, Any]:
        for task in snapshot.get("tasks", []):
            if isinstance(task, Mapping) and task.get("id") == task_id:
                return task
        raise KanbanActivationError("NATIVE_TASK_NOT_FOUND", "activation task was not found")

    @staticmethod
    def _verify_task(
        actual: Mapping[str, Any],
        expected: Mapping[str, Any],
        *,
        allowed_statuses: set[str] | frozenset[str],
    ) -> None:
        body = actual.get("body")
        body_sha256 = (
            hashlib.sha256(body.encode("utf-8")).hexdigest()
            if isinstance(body, str)
            else ""
        )
        exact = {
            "task_id": actual.get("id"),
            "title": actual.get("title"),
            "body_sha256": body_sha256,
            "assignee": actual.get("assignee"),
            "workspace_kind": actual.get("workspace_kind"),
            "workspace_path": actual.get("workspace_path"),
            "skills": list(actual.get("skills") or []),
            "idempotency_key": actual.get("idempotency_key"),
        }
        expected_exact = {
            key: expected.get(key)
            for key in exact
        }
        expected_exact["skills"] = list(expected.get("skills") or [])
        if exact != expected_exact or actual.get("status") not in allowed_statuses:
            raise KanbanActivationError(
                "NATIVE_READBACK_MISMATCH",
                "native activation task did not match the accepted artifact",
            )


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KanbanActivationError("VALUE_INVALID", f"{field} must be non-empty text")
    return value.strip()


def _bounded_text(
    value: Any,
    field: str,
    *,
    max_length: int,
    code: str,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > max_length
    ):
        raise KanbanActivationError(code, f"{field} is invalid")
    return value


def _identity(
    value: Any,
    field: str,
    *,
    pattern: re.Pattern[str],
    max_length: int,
    code: str = "REQUEST_INVALID",
) -> str:
    text = _bounded_text(
        value,
        field,
        max_length=max_length,
        code=code,
    )
    if not pattern.fullmatch(text):
        raise KanbanActivationError(code, f"{field} is invalid")
    return text


def _workspace_path(
    value: Any,
    field: str,
    *,
    code: str = "REQUEST_INVALID",
) -> str:
    text = _bounded_text(value, field, max_length=4096, code=code)
    path = Path(text)
    if not path.is_absolute() or not path.is_dir():
        raise KanbanActivationError(code, f"{field} is invalid")
    canonical = path.resolve().as_posix()
    if text != canonical:
        raise KanbanActivationError(code, f"{field} is invalid")
    return text


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
