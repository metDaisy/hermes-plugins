"""Accepted-artifact fencing for native Kanban milestone materialization."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from .artifact_store import SQLiteArtifactStore, canonical_json_sha256

_REQUEST_SCHEMA = "controller-kanban-materialization-v1"
_PAYLOAD_SCHEMA = "controller-triage-milestone-v1"
_RECEIPT_SCHEMA = "controller-kanban-materialization-receipt-v1"
_PAYLOAD_FIELDS = {
    "schema",
    "board",
    "issue_number",
    "generation",
    "workspace_path",
    "title",
    "assignee",
    "status",
    "body_text",
    "frozen_digest",
}
_ACCEPTED_TRIAGE_FIELDS = {
    "stage",
    "artifact",
    "body",
    "body_text",
    "frozen_digest",
    "read_back",
    "milestone",
}
_REQUEST_FIELDS = {
    "schema",
    "artifact_id",
    "artifact_sha256",
    "board",
    "issue_number",
    "generation",
    "workspace_path",
    "expected_snapshot_fingerprint",
}
_TASK_FIELDS = (
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
_GENERATION_TITLE = re.compile(r"^G(?P<generation>[1-9][0-9]*)-Issue(?P<issue>[1-9][0-9]*)-")
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def accepted_triage_idempotency_key(
    *,
    board: str,
    issue_number: int,
    generation: int,
    artifact_sha256: str,
) -> str:
    """Return the canonical native identity shared by every Triage consumer."""
    return "controller-triage:" + canonical_json_sha256(
        {
            "board": board,
            "issue_number": issue_number,
            "generation": generation,
            "artifact_sha256": artifact_sha256,
        }
    )


class KanbanMaterializationError(ValueError):
    """Stable fail-closed error for accepted milestone materialization."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _NativeCreateCommittedError(KanbanMaterializationError):
    def __init__(self, task_id: str) -> None:
        super().__init__(
            "NATIVE_CREATE_COMMITTED",
            "native create failed after commit",
        )
        self.created_task_id = task_id


class NativeKanbanMaterializationPort(Protocol):
    def snapshot(self, *, board: str) -> dict[str, Any]: ...

    def create_triage(
        self,
        *,
        board: str,
        card: dict[str, Any],
        expected_snapshot_fingerprint: str,
    ) -> dict[str, Any]: ...

    def read_task(self, *, board: str, task_id: str) -> dict[str, Any]: ...

    def archive_task(self, *, board: str, task_id: str) -> None: ...


class HermesNativeKanbanMaterializationPort:
    """Canonical Hermes DB adapter with exact idempotency-key read-back."""

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
            return {
                "board": board,
                "tasks": [
                    self._task_value(task)
                    for task in module.list_tasks(conn, include_archived=True)
                ],
            }
        finally:
            conn.close()

    def create_triage(
        self,
        *,
        board: str,
        card: dict[str, Any],
        expected_snapshot_fingerprint: str,
    ) -> dict[str, Any]:
        module, connect = self._dependencies()
        conn = connect(board=board)
        created_task_id: str | None = None
        try:
            with module.write_txn(conn):
                existing = conn.execute(
                    "SELECT id FROM tasks WHERE idempotency_key = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (card["idempotency_key"],),
                ).fetchone()
                if existing is not None:
                    receipt = {"task_id": str(existing["id"]), "created": False}
                else:
                    locked_snapshot = {
                        "board": board,
                        "tasks": [
                            self._task_value(task)
                            for task in module.list_tasks(conn, include_archived=True)
                        ],
                    }
                    if (
                        fingerprint_native_snapshot(locked_snapshot)
                        != expected_snapshot_fingerprint
                    ):
                        raise KanbanMaterializationError(
                            "SNAPSHOT_CHANGED",
                            "canonical snapshot changed before materialization",
                        )
                    task_id = module.create_task(
                        conn,
                        title=card["title"],
                        body=card["body_text"],
                        assignee=card["assignee"],
                        created_by="hermes-langchain-controller",
                        workspace_kind="dir",
                        workspace_path=card["workspace_path"],
                        priority=0,
                        triage=True,
                        idempotency_key=card["idempotency_key"],
                        skills=[],
                        board=board,
                    )
                    created_task_id = str(task_id)
                    receipt = {"task_id": created_task_id, "created": True}
        except BaseException as exc:
            try:
                conn.close()
            except BaseException as close_exc:
                if created_task_id is not None:
                    raise _NativeCreateCommittedError(created_task_id) from close_exc
                raise close_exc from exc
            if created_task_id is not None:
                raise _NativeCreateCommittedError(created_task_id) from exc
            raise
        try:
            conn.close()
        except BaseException as exc:
            if created_task_id is not None:
                raise _NativeCreateCommittedError(created_task_id) from exc
            raise
        return receipt

    def read_task(self, *, board: str, task_id: str) -> dict[str, Any]:
        module, connect = self._dependencies()
        conn = connect(board=board)
        try:
            task = module.get_task(conn, task_id)
            if task is None:
                raise KanbanMaterializationError(
                    "NATIVE_TASK_NOT_FOUND",
                    "native task read-back was not found",
                )
            return self._task_value(task)
        finally:
            conn.close()

    def archive_task(self, *, board: str, task_id: str) -> None:
        module, connect = self._dependencies()
        conn = connect(board=board)
        try:
            if not module.archive_task(conn, task_id):
                raise KanbanMaterializationError(
                    "NATIVE_ARCHIVE_FAILED",
                    "native task archive failed",
                )
        finally:
            conn.close()

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


def fingerprint_native_snapshot(snapshot: Any) -> str:
    """Return a stable digest over the materialization-relevant board state."""

    if not isinstance(snapshot, Mapping):
        raise KanbanMaterializationError("SNAPSHOT_INVALID", "native snapshot is invalid")
    board = _text(snapshot.get("board"), "snapshot.board")
    tasks = snapshot.get("tasks")
    if not isinstance(tasks, Sequence) or isinstance(tasks, (str, bytes)):
        raise KanbanMaterializationError("SNAPSHOT_INVALID", "native snapshot tasks are invalid")
    normalized: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, Mapping):
            raise KanbanMaterializationError("SNAPSHOT_INVALID", "native snapshot task is invalid")
        normalized.append({field: _json_value(task.get(field)) for field in _TASK_FIELDS})
    normalized.sort(key=lambda task: str(task.get("id") or ""))
    return canonical_json_sha256({"board": board, "tasks": normalized})


class AcceptedMilestoneMaterializer:
    """Materialize one accepted Triage milestone with fencing and exact read-back."""

    def __init__(
        self,
        *,
        artifact_store: SQLiteArtifactStore,
        kanban_port: NativeKanbanMaterializationPort,
    ) -> None:
        self._artifact_store = artifact_store
        self._kanban_port = kanban_port

    def materialize(self, request: Any) -> dict[str, Any]:
        contract = self._request(request)
        record = self._artifact_store.get(contract["artifact_id"])
        payload = self._accepted_payload(record, contract)
        idempotency_key = self._idempotency_key(payload, record.output_sha256)
        card = self._card(payload, idempotency_key)
        snapshot = self._kanban_port.snapshot(board=contract["board"])
        tasks = self._snapshot_tasks(snapshot)

        replay = self._matching_idempotency_task(tasks, idempotency_key)
        if replay is not None:
            task = self._kanban_port.read_task(
                board=contract["board"], task_id=_text(replay.get("id"), "task.id")
            )
            self._assert_exact_task(task, card, allow_archived=True)
            return self._receipt(
                contract=contract,
                record_sha256=record.output_sha256,
                task=task,
                idempotency_key=idempotency_key,
                idempotent_replay=True,
            )

        self._reject_generation_conflict(tasks, payload)
        if fingerprint_native_snapshot(snapshot) != contract["expected_snapshot_fingerprint"]:
            raise KanbanMaterializationError(
                "SNAPSHOT_CHANGED",
                "canonical snapshot changed before materialization",
            )

        created_id: str | None = None
        created_by_attempt = False
        try:
            created = self._kanban_port.create_triage(
                board=contract["board"],
                card=card,
                expected_snapshot_fingerprint=contract[
                    "expected_snapshot_fingerprint"
                ],
            )
            if isinstance(created, Mapping):
                receipt_task_id = created.get("task_id")
                if (
                    created.get("created") is True
                    and isinstance(receipt_task_id, str)
                    and receipt_task_id.strip()
                ):
                    created_id = receipt_task_id.strip()
                    created_by_attempt = True
            if (
                not isinstance(created, Mapping)
                or set(created) != {"task_id", "created"}
                or type(created.get("created")) is not bool
            ):
                raise KanbanMaterializationError(
                    "CREATE_RECEIPT_INVALID",
                    "native create receipt is invalid",
                )
            created_id = _text(created.get("task_id"), "create.task_id")
            created_by_attempt = bool(created["created"])
            task = self._kanban_port.read_task(board=contract["board"], task_id=created_id)
            self._assert_exact_task(task, card, allow_archived=not created_by_attempt)
            return self._receipt(
                contract=contract,
                record_sha256=record.output_sha256,
                task=task,
                idempotency_key=idempotency_key,
                idempotent_replay=not created_by_attempt,
            )
        except BaseException as exc:
            committed_task_id = getattr(exc, "created_task_id", None)
            if (
                created_id is None
                and isinstance(committed_task_id, str)
                and committed_task_id.strip()
            ):
                created_id = committed_task_id.strip()
                created_by_attempt = True
            if created_id is not None and created_by_attempt:
                self._rollback_created_task(contract["board"], created_id)
            if isinstance(exc, KanbanMaterializationError):
                raise
            raise KanbanMaterializationError(
                "NATIVE_READBACK_FAILED",
                "native task read-back failed",
            ) from exc

    @staticmethod
    def _request(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != _REQUEST_FIELDS:
            raise KanbanMaterializationError(
                "REQUEST_INVALID",
                "materialization request fields are invalid",
            )
        if value.get("schema") != _REQUEST_SCHEMA:
            raise KanbanMaterializationError(
                "REQUEST_INVALID",
                "materialization request schema is invalid",
            )
        request = dict(value)
        for field in (
            "artifact_id",
            "artifact_sha256",
            "board",
            "workspace_path",
            "expected_snapshot_fingerprint",
        ):
            request[field] = _text(request.get(field), f"request.{field}")
        for field in ("artifact_sha256", "expected_snapshot_fingerprint"):
            if not _HEX_SHA256.fullmatch(request[field]):
                raise KanbanMaterializationError(
                    "REQUEST_INVALID",
                    f"request.{field} must be a SHA-256 digest",
                )
        for field in ("issue_number", "generation"):
            if type(request.get(field)) is not int or request[field] < 1:
                raise KanbanMaterializationError(
                    "REQUEST_INVALID",
                    f"request.{field} must be a positive integer",
                )
        workspace = Path(request["workspace_path"]).resolve()
        if not workspace.is_dir() or workspace.as_posix() != request["workspace_path"]:
            raise KanbanMaterializationError(
                "WORKSPACE_INVALID",
                "materialization workspace is unavailable or non-canonical",
            )
        return request

    @staticmethod
    def _accepted_payload(record: Any, request: dict[str, Any]) -> dict[str, Any]:
        if record.validation_result != "accepted" or record.step_id != "triage_materialize":
            raise KanbanMaterializationError(
                "ARTIFACT_UNACCEPTED",
                "only an accepted Triage materialization artifact may be materialized",
            )
        if record.output_sha256 != request["artifact_sha256"]:
            raise KanbanMaterializationError(
                "ARTIFACT_DIGEST_MISMATCH",
                "accepted artifact digest does not match the request",
            )
        accepted = dict(record.payload)
        if set(accepted) != _ACCEPTED_TRIAGE_FIELDS or accepted.get("stage") != "materialize":
            raise KanbanMaterializationError(
                "ARTIFACT_PAYLOAD_INVALID",
                "accepted Triage artifact payload is invalid",
            )
        artifact = accepted.get("artifact")
        read_back = accepted.get("read_back")
        accepted_body_text = accepted.get("body_text")
        if (
            not isinstance(artifact, Mapping)
            or set(artifact) != {"path", "sha256"}
            or not isinstance(accepted_body_text, str)
            or not accepted_body_text
            or artifact.get("sha256")
            != hashlib.sha256(accepted_body_text.encode("utf-8")).hexdigest()
            or not isinstance(read_back, Mapping)
            or read_back.get("valid") is not True
            or read_back.get("verified") is not True
            or read_back.get("status") != "triage"
            or read_back.get("frozen_digest") != accepted.get("frozen_digest")
            or read_back.get("frozen_body_file") != artifact.get("path")
        ):
            raise KanbanMaterializationError(
                "ARTIFACT_PAYLOAD_INVALID",
                "accepted Triage artifact proof is invalid",
            )
        try:
            accepted_body = json.loads(accepted_body_text)
        except (TypeError, ValueError) as exc:
            raise KanbanMaterializationError(
                "ARTIFACT_PAYLOAD_INVALID",
                "accepted Triage artifact body is invalid",
            ) from exc
        if accepted.get("body") != accepted_body:
            raise KanbanMaterializationError(
                "ARTIFACT_PAYLOAD_INVALID",
                "accepted Triage artifact body is incoherent",
            )
        payload_value = accepted.get("milestone")
        if not isinstance(payload_value, Mapping):
            raise KanbanMaterializationError(
                "ARTIFACT_PAYLOAD_INVALID",
                "accepted Triage milestone is invalid",
            )
        payload = dict(payload_value)
        if set(payload) != _PAYLOAD_FIELDS or payload.get("schema") != _PAYLOAD_SCHEMA:
            raise KanbanMaterializationError(
                "ARTIFACT_PAYLOAD_INVALID",
                "accepted artifact payload is invalid",
            )
        for field in ("board", "issue_number", "generation", "workspace_path"):
            if payload.get(field) != request[field]:
                raise KanbanMaterializationError(
                    "ARTIFACT_IDENTITY_MISMATCH",
                    "accepted artifact identity does not match the request",
                )
        if payload.get("status") != "triage":
            raise KanbanMaterializationError(
                "ARTIFACT_PAYLOAD_INVALID",
                "accepted artifact status must be triage",
            )
        for field in ("title", "assignee", "frozen_digest"):
            payload[field] = _text(payload.get(field), f"artifact.{field}")
        title_match = _GENERATION_TITLE.match(payload["title"])
        if (
            title_match is None
            or int(title_match.group("generation")) != request["generation"]
            or int(title_match.group("issue")) != request["issue_number"]
        ):
            raise KanbanMaterializationError(
                "ARTIFACT_PAYLOAD_INVALID",
                "accepted artifact generation title is invalid",
            )
        body_text = payload.get("body_text")
        if not isinstance(body_text, str) or not body_text.strip():
            raise KanbanMaterializationError(
                "ARTIFACT_PAYLOAD_INVALID",
                "accepted artifact body text is required",
            )
        payload["body_text"] = body_text
        if (
            body_text != accepted_body_text
            or payload["frozen_digest"] != accepted.get("frozen_digest")
            or payload["title"] != read_back.get("title")
            or payload["status"] != read_back.get("status")
        ):
            raise KanbanMaterializationError(
                "ARTIFACT_PAYLOAD_INVALID",
                "accepted Triage milestone is incoherent",
            )
        if not _HEX_SHA256.fullmatch(payload["frozen_digest"]):
            raise KanbanMaterializationError(
                "ARTIFACT_PAYLOAD_INVALID",
                "accepted artifact frozen digest is invalid",
            )
        try:
            body = json.loads(payload["body_text"])
        except (TypeError, ValueError) as exc:
            raise KanbanMaterializationError(
                "ARTIFACT_PAYLOAD_INVALID",
                "accepted artifact body is invalid JSON",
            ) from exc
        issue = body.get("issue") if isinstance(body, dict) else None
        if (
            not isinstance(body, dict)
            or body.get("schema") != "triage-v7"
            or not isinstance(issue, dict)
            or issue.get("number") != request["issue_number"]
            or body.get("frozen_digest") != payload["frozen_digest"]
        ):
            raise KanbanMaterializationError(
                "ARTIFACT_PAYLOAD_INVALID",
                "accepted artifact body identity is invalid",
            )
        return payload

    @staticmethod
    def _idempotency_key(payload: dict[str, Any], output_sha256: str) -> str:
        return accepted_triage_idempotency_key(
            board=payload["board"],
            issue_number=payload["issue_number"],
            generation=payload["generation"],
            artifact_sha256=output_sha256,
        )

    @staticmethod
    def _card(payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return {
            "title": payload["title"],
            "body_text": payload["body_text"],
            "assignee": payload["assignee"],
            "workspace_path": payload["workspace_path"],
            "idempotency_key": idempotency_key,
        }

    @staticmethod
    def _snapshot_tasks(snapshot: Any) -> list[dict[str, Any]]:
        if not isinstance(snapshot, Mapping):
            raise KanbanMaterializationError("SNAPSHOT_INVALID", "native snapshot is invalid")
        tasks = snapshot.get("tasks")
        if not isinstance(tasks, Sequence) or isinstance(tasks, (str, bytes)):
            raise KanbanMaterializationError("SNAPSHOT_INVALID", "native snapshot tasks are invalid")
        if not all(isinstance(task, Mapping) for task in tasks):
            raise KanbanMaterializationError("SNAPSHOT_INVALID", "native snapshot task is invalid")
        return [dict(task) for task in tasks]

    @staticmethod
    def _matching_idempotency_task(
        tasks: list[dict[str, Any]], idempotency_key: str
    ) -> dict[str, Any] | None:
        matches = [task for task in tasks if task.get("idempotency_key") == idempotency_key]
        if len(matches) > 1:
            raise KanbanMaterializationError(
                "DUPLICATE_NATIVE_IDEMPOTENCY",
                "native board contains duplicate idempotency identities",
            )
        return matches[0] if matches else None

    @staticmethod
    def _reject_generation_conflict(
        tasks: list[dict[str, Any]], payload: dict[str, Any]
    ) -> None:
        for task in tasks:
            match = _GENERATION_TITLE.match(str(task.get("title") or ""))
            if match is None or int(match.group("issue")) != payload["issue_number"]:
                continue
            native_generation = int(match.group("generation"))
            if native_generation >= payload["generation"]:
                raise KanbanMaterializationError(
                    "CONFLICTING_REPLAY",
                    "native board contains a conflicting replay for this generation",
                )

    @staticmethod
    def _assert_exact_task(
        task: Any,
        card: dict[str, Any],
        *,
        allow_archived: bool = False,
    ) -> None:
        if not isinstance(task, Mapping):
            raise KanbanMaterializationError(
                "NATIVE_READBACK_MISMATCH",
                "native task read-back is invalid",
            )
        expected = {
            "title": card["title"],
            "body": card["body_text"],
            "assignee": card["assignee"],
            "workspace_kind": "dir",
            "workspace_path": card["workspace_path"],
            "skills": [],
            "idempotency_key": card["idempotency_key"],
        }
        status = task.get("status")
        status_matches = status == "triage" or (allow_archived and status == "archived")
        if not status_matches or any(
            _json_value(task.get(field)) != value for field, value in expected.items()
        ):
            replay = task.get("idempotency_key") == card["idempotency_key"]
            raise KanbanMaterializationError(
                "CONFLICTING_REPLAY" if replay else "NATIVE_READBACK_MISMATCH",
                "native task read-back is a conflicting replay"
                if replay
                else "native task read-back does not match the accepted artifact",
            )
        _text(task.get("id"), "task.id")

    def _rollback_created_task(self, board: str, task_id: str) -> None:
        try:
            self._kanban_port.archive_task(board=board, task_id=task_id)
            rolled_back = self._kanban_port.read_task(board=board, task_id=task_id)
        except BaseException as exc:
            raise KanbanMaterializationError(
                "ROLLBACK_FAILED",
                "native materialization rollback could not be verified",
            ) from exc
        if not isinstance(rolled_back, Mapping) or rolled_back.get("status") != "archived":
            raise KanbanMaterializationError(
                "ROLLBACK_FAILED",
                "native materialization rollback could not be verified",
            )

    @staticmethod
    def _receipt(
        *,
        contract: dict[str, Any],
        record_sha256: str,
        task: Mapping[str, Any],
        idempotency_key: str,
        idempotent_replay: bool,
    ) -> dict[str, Any]:
        return {
            "schema": _RECEIPT_SCHEMA,
            "ok": True,
            "verified": True,
            "board": contract["board"],
            "issue_number": contract["issue_number"],
            "generation": contract["generation"],
            "artifact_id": contract["artifact_id"],
            "artifact_sha256": record_sha256,
            "task_id": _text(task.get("id"), "task.id"),
            "idempotency_key": idempotency_key,
            "idempotent_replay": idempotent_replay,
        }


def _text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise KanbanMaterializationError("VALUE_REQUIRED", f"{field} is required")
    return text


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise KanbanMaterializationError("SNAPSHOT_INVALID", "native snapshot is not JSON-compatible")
