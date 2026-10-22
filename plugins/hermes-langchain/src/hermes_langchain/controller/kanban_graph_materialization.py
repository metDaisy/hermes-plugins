"""Accepted-artifact fencing for native Impl/Review/Summary graph materialization."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pathlib import Path
from typing import Any, Protocol

from ..authoring.graph.graph_contract import validate_graph
from .artifact_store import (
    ArtifactRecord,
    ArtifactStoreError,
    SQLiteArtifactStore,
    canonical_json_sha256,
)
from .kanban_materialization import (
    AcceptedMilestoneMaterializer,
    accepted_triage_idempotency_key,
)


_REQUEST_SCHEMA = "controller-kanban-graph-materialization-request-v1"
_PAYLOAD_SCHEMA = "controller-task-graph-v1"
_RECEIPT_SCHEMA = "controller-kanban-graph-materialization-receipt-v1"
_PROJECTION_REQUEST_SCHEMA = "controller-task-graph-projection-request-v1"
_PROJECTION_RECEIPT_SCHEMA = "controller-task-graph-projection-receipt-v1"
_PROJECTION_REQUEST_FIELDS = {
    "schema",
    "artifact_id",
    "authoring_artifact_id",
    "run_id",
    "accepted_triage_artifact_id",
    "accepted_triage_artifact_sha256",
    "source_task_id",
    "board",
    "issue_number",
    "generation",
    "workspace_path",
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
_PAYLOAD_FIELDS = {
    "schema",
    "board",
    "issue_number",
    "generation",
    "workspace_path",
    "source_task",
    "activation_candidate_key",
    "cards",
    "links",
}
_SOURCE_FIELDS = {
    "task_id",
    "title",
    "body_sha256",
    "status",
    "assignee",
    "workspace_kind",
    "workspace_path",
    "skills",
    "idempotency_key",
    "accepted_artifact_id",
    "accepted_artifact_sha256",
}
_CARD_FIELDS = {
    "key",
    "role",
    "title",
    "body_text",
    "body_sha256",
    "assignee",
    "skills",
}
_LINK_FIELDS = {"parent_key", "child_key"}
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
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_BOARD = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ROLE_ASSIGNEE = {
    "implementation": "coder",
    "review": "reviewer",
    "summary": "project-manager",
}
_ROLE_BODY_SCHEMAS = {
    "implementation": {
        "backend-implementation-card-v5",
        "backend-implementation-card-v6",
    },
    "review": {"aggregate-review-card-v5"},
    "summary": {"issue-summary-card-v3"},
}
_MAX_CARDS = 64
_MAX_LINKS = 256
_MAX_SKILLS_PER_CARD = 16
_MAX_CARD_BODY_BYTES = 200_000
_MAX_TOTAL_BODY_BYTES = 1_000_000
_MAX_AUTHORING_GRAPH_BYTES = 2_000_000
_GRAPH_AUTHORING_TOOLSETS = (
    "file",
    "terminal",
    "skills",
    "codebase-memory-mcp",
    "semble-mcp",
)
_GRAPH_AUTHORING_EXECUTOR_PREFIX = "controller-task-graph-authoring-v1:"


class _AuthoringResult(Protocol):
    agent_id: str
    output: Any


class AgentExecutor(Protocol):
    def invoke(self, invocation: Any) -> _AuthoringResult: ...


@dataclass(frozen=True)
class _AuthoringInvocation:
    run_id: str
    invocation_id: str
    step_id: str
    goal: str
    allowed_toolsets: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    output_schema: dict[str, Any]
    input: dict[str, Any]
    timeout_seconds: int
    working_directory: str
    skills: tuple[str, ...]


class KanbanGraphMaterializationError(ValueError):
    """Stable fail-closed graph materialization error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _NativeGraphCreateCommittedError(KanbanGraphMaterializationError):
    def __init__(self, task_ids: Mapping[str, str]) -> None:
        super().__init__("NATIVE_CREATE_COMMITTED", "native graph create failed after commit")
        self.created_task_ids = dict(task_ids)


class NativeKanbanGraphMaterializationPort(Protocol):
    def snapshot(self, *, board: str) -> dict[str, Any]: ...

    def materialize_graph(
        self,
        *,
        board: str,
        graph: dict[str, Any],
        expected_snapshot_fingerprint: str,
        artifact_id: str,
        artifact_sha256: str,
    ) -> dict[str, Any]: ...

    def read_graph(
        self,
        *,
        board: str,
        source_task_id: str,
        task_ids: dict[str, str],
    ) -> dict[str, Any]: ...

    def rollback_graph(
        self,
        *,
        board: str,
        task_ids: list[str],
        expected_snapshot_fingerprint: str | None,
    ) -> None: ...


class HermesNativeKanbanGraphMaterializationPort:
    """Hermes DB adapter for transaction-fenced task-graph creation."""

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

    def materialize_graph(
        self,
        *,
        board: str,
        graph: dict[str, Any],
        expected_snapshot_fingerprint: str,
        artifact_id: str,
        artifact_sha256: str,
    ) -> dict[str, Any]:
        module, connect = self._dependencies()
        conn = connect(board=board)
        committed_task_ids: dict[str, str] = {}
        receipt: dict[str, Any]
        try:
            with module.write_txn(conn):
                existing: dict[str, str] = {}
                for card in graph["cards"]:
                    rows = conn.execute(
                        "SELECT id, status FROM tasks WHERE idempotency_key = ? "
                        "ORDER BY created_at, id",
                        (card["idempotency_key"],),
                    ).fetchall()
                    if len(rows) > 1 or any(
                        str(row["status"]) == "archived" for row in rows
                    ):
                        raise KanbanGraphMaterializationError(
                            "CONFLICTING_REPLAY",
                            "native graph identity is duplicated or archived",
                        )
                    if rows:
                        existing[card["key"]] = str(rows[0]["id"])
                if existing:
                    if len(existing) != len(graph["cards"]):
                        raise KanbanGraphMaterializationError(
                            "CONFLICTING_REPLAY", "native graph replay is partial"
                        )
                    receipt = {"task_ids": existing, "created": False}
                else:
                    locked = self._snapshot_value(module, conn, board)
                    if fingerprint_graph_snapshot(locked) != expected_snapshot_fingerprint:
                        raise KanbanGraphMaterializationError(
                            "SNAPSHOT_CHANGED",
                            "canonical graph snapshot changed before mutation",
                        )
                    source = module.get_task(conn, graph["source_task"]["task_id"])
                    if source is None or not _source_matches(
                        self._task_value(source), graph["source_task"]
                    ):
                        raise KanbanGraphMaterializationError(
                            "SOURCE_TASK_CHANGED", "source task changed before graph mutation"
                        )
                    task_ids: dict[str, str] = {}
                    links_by_child = _links_by_child(graph["links"])
                    remaining = {card["key"]: card for card in graph["cards"]}
                    while remaining:
                        progressed = False
                        for key, card in list(remaining.items()):
                            parent_keys = links_by_child.get(key, [])
                            if any(
                                parent != "source" and parent not in task_ids
                                for parent in parent_keys
                            ):
                                continue
                            parent_ids = [
                                graph["source_task"]["task_id"]
                                if parent == "source"
                                else task_ids[parent]
                                for parent in parent_keys
                            ]
                            task_id = module.create_task(
                                conn,
                                title=card["title"],
                                body=card["body_text"],
                                assignee=card["assignee"],
                                created_by="hermes-langchain-controller",
                                workspace_kind="dir",
                                workspace_path=graph["workspace_path"],
                                priority=0,
                                parents=parent_ids,
                                triage=False,
                                idempotency_key=card["idempotency_key"],
                                skills=card["skills"],
                                initial_status="running",
                                board=board,
                            )
                            task_ids[key] = str(task_id)
                            del remaining[key]
                            progressed = True
                        if not progressed:
                            raise KanbanGraphMaterializationError(
                                "GRAPH_CYCLE", "native graph order is cyclic"
                            )
                    for task_id in task_ids.values():
                        module._append_event(
                            conn,
                            task_id,
                            "controller_graph_materialized",
                            {
                                "artifact_id": artifact_id,
                                "artifact_sha256": artifact_sha256,
                            },
                        )
                    receipt = {"task_ids": task_ids, "created": True}
                    # Preserve attempt-owned IDs before write_txn.__exit__. Hermes may
                    # commit and then fail its post-commit integrity check.
                    committed_task_ids = dict(task_ids)
            if receipt["created"]:
                committed_task_ids = dict(receipt["task_ids"])
        except BaseException as exc:
            try:
                conn.close()
            except BaseException as close_exc:
                if committed_task_ids:
                    raise _NativeGraphCreateCommittedError(committed_task_ids) from close_exc
                raise close_exc from exc
            if committed_task_ids:
                raise _NativeGraphCreateCommittedError(committed_task_ids) from exc
            raise
        try:
            conn.close()
        except BaseException as exc:
            if committed_task_ids:
                raise _NativeGraphCreateCommittedError(committed_task_ids) from exc
            raise
        return receipt

    def read_graph(
        self,
        *,
        board: str,
        source_task_id: str,
        task_ids: dict[str, str],
    ) -> dict[str, Any]:
        module, connect = self._dependencies()
        conn = connect(board=board)
        try:
            source = module.get_task(conn, source_task_id)
            if source is None:
                raise KanbanGraphMaterializationError(
                    "SOURCE_TASK_CHANGED", "source task read-back was not found"
                )
            tasks = []
            for task_id in task_ids.values():
                task = module.get_task(conn, task_id)
                if task is None:
                    raise KanbanGraphMaterializationError(
                        "NATIVE_TASK_NOT_FOUND", "graph task read-back was not found"
                    )
                tasks.append(self._task_value(task))
            idempotency_keys = [task["idempotency_key"] for task in tasks]
            placeholders = ",".join("?" for _ in idempotency_keys)
            matching_rows = conn.execute(
                f"SELECT id FROM tasks WHERE idempotency_key IN ({placeholders}) ORDER BY id",
                idempotency_keys,
            ).fetchall()
            tasks = [
                self._task_value(module.get_task(conn, str(row["id"])))
                for row in matching_rows
            ]
            selected = {source_task_id, *task_ids.values()}
            rows = conn.execute(
                "SELECT parent_id, child_id FROM task_links ORDER BY parent_id, child_id"
            ).fetchall()
            created = set(task_ids.values())
            event_placeholders = ",".join("?" for _ in created)
            event_rows = conn.execute(
                f"SELECT task_id, kind, payload FROM task_events "
                f"WHERE task_id IN ({event_placeholders}) "
                "AND kind = 'controller_graph_materialized' ORDER BY task_id, id",
                list(created),
            ).fetchall()
            events: dict[str, list[dict[str, Any]]] = {
                task_id: [] for task_id in created
            }
            for row in event_rows:
                try:
                    payload = json.loads(str(row["payload"])) if row["payload"] else None
                except (TypeError, ValueError):
                    payload = None
                events[str(row["task_id"])].append(
                    {"kind": str(row["kind"]), "payload": payload}
                )
            return {
                "source_task": self._task_value(source),
                "tasks": tasks,
                "events": events,
                "links": [
                    {"parent_id": str(row["parent_id"]), "child_id": str(row["child_id"])}
                    for row in rows
                    if str(row["parent_id"]) in selected
                    or str(row["child_id"]) in selected
                ],
            }
        finally:
            conn.close()

    def rollback_graph(
        self,
        *,
        board: str,
        task_ids: list[str],
        expected_snapshot_fingerprint: str | None,
    ) -> None:
        module, connect = self._dependencies()
        conn = connect(board=board)
        try:
            attachment_paths = self._hard_delete_attempt_tasks(
                module=module,
                conn=conn,
                task_ids=task_ids,
            )
            for stored_path in set(attachment_paths):
                referenced = conn.execute(
                    "SELECT 1 FROM task_attachments WHERE stored_path = ? LIMIT 1",
                    (stored_path,),
                ).fetchone()
                if referenced is None:
                    Path(stored_path).unlink(missing_ok=True)
            for task_id in task_ids:
                if module.get_task(conn, task_id) is not None:
                    raise KanbanGraphMaterializationError(
                        "ROLLBACK_FAILED", "native graph rollback left a task"
                    )
                if conn.execute(
                    "SELECT 1 FROM task_links WHERE parent_id = ? OR child_id = ?",
                    (task_id, task_id),
                ).fetchone():
                    raise KanbanGraphMaterializationError(
                        "ROLLBACK_FAILED", "native graph rollback left a link"
                    )
                for table in (
                    "task_comments",
                    "task_events",
                    "task_runs",
                    "task_attachments",
                    "kanban_notify_subs",
                ):
                    if conn.execute(
                        f"SELECT 1 FROM {table} WHERE task_id = ? LIMIT 1", (task_id,)
                    ).fetchone():
                        raise KanbanGraphMaterializationError(
                            "ROLLBACK_FAILED", "native graph rollback left dependent state"
                        )
            for stored_path in attachment_paths:
                if (
                    conn.execute(
                        "SELECT 1 FROM task_attachments WHERE stored_path = ? LIMIT 1",
                        (stored_path,),
                    ).fetchone()
                    is None
                    and Path(stored_path).exists()
                ):
                    raise KanbanGraphMaterializationError(
                        "ROLLBACK_FAILED", "native graph rollback left an attachment blob"
                    )
            restored = self._snapshot_value(module, conn, board)
            if (
                expected_snapshot_fingerprint is not None
                and fingerprint_graph_snapshot(restored) != expected_snapshot_fingerprint
            ):
                raise KanbanGraphMaterializationError(
                    "ROLLBACK_FAILED", "native graph rollback did not restore the board snapshot"
                )
        finally:
            conn.close()

    @staticmethod
    def _hard_delete_attempt_tasks(
        *, module: Any, conn: Any, task_ids: list[str]
    ) -> list[str]:
        """Delete attempt-owned graph state without recomputing unrelated tasks."""

        attachment_paths: list[str] = []
        with module.write_txn(conn):
            for task_id in task_ids:
                attachment_paths.extend(
                    str(attachment.stored_path)
                    for attachment in module.list_attachments(conn, task_id)
                )
                conn.execute("DELETE FROM task_attachments WHERE task_id = ?", (task_id,))
                module._delete_task_relations(conn, task_id)
                conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return attachment_paths

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
                {"parent_id": str(row["parent_id"]), "child_id": str(row["child_id"])}
                for row in links
            ],
        }

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


def fingerprint_graph_snapshot(snapshot: Any) -> str:
    if not isinstance(snapshot, Mapping):
        raise KanbanGraphMaterializationError("SNAPSHOT_INVALID", "graph snapshot is invalid")
    board = _identity(snapshot.get("board"), "snapshot.board", _BOARD, 64)
    tasks = snapshot.get("tasks")
    links = snapshot.get("links")
    if not _sequence(tasks) or not _sequence(links):
        raise KanbanGraphMaterializationError("SNAPSHOT_INVALID", "graph snapshot shape is invalid")
    normalized_tasks = []
    for task in tasks:
        if not isinstance(task, Mapping):
            raise KanbanGraphMaterializationError("SNAPSHOT_INVALID", "graph snapshot task is invalid")
        normalized_tasks.append({field: _json_value(task.get(field)) for field in _TASK_FIELDS})
    normalized_tasks.sort(key=lambda item: str(item.get("id") or ""))
    normalized_links = []
    for link in links:
        if not isinstance(link, Mapping):
            raise KanbanGraphMaterializationError("SNAPSHOT_INVALID", "graph snapshot link is invalid")
        normalized_links.append(
            {
                "parent_id": _identity(link.get("parent_id"), "snapshot.parent_id", _ID, 128),
                "child_id": _identity(link.get("child_id"), "snapshot.child_id", _ID, 128),
            }
        )
    normalized_links.sort(key=lambda item: (item["parent_id"], item["child_id"]))
    return canonical_json_sha256(
        {"board": board, "tasks": normalized_tasks, "links": normalized_links}
    )


class AcceptedTaskGraphProjector:
    """Validate a graph against accepted Triage provenance and persist it immutably."""

    def __init__(
        self,
        *,
        triage_artifact_store: SQLiteArtifactStore,
        authoring_artifact_store: SQLiteArtifactStore,
        graph_artifact_store: SQLiteArtifactStore,
        agent_executor: AgentExecutor,
    ) -> None:
        self._triage_artifact_store = triage_artifact_store
        self._authoring_artifact_store = authoring_artifact_store
        self._graph_artifact_store = graph_artifact_store
        self._agent_executor = agent_executor

    def project(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, Mapping) or set(request) != _PROJECTION_REQUEST_FIELDS:
            raise KanbanGraphMaterializationError(
                "PROJECTION_REQUEST_INVALID", "graph projection request is invalid"
            )
        if request.get("schema") != _PROJECTION_REQUEST_SCHEMA:
            raise KanbanGraphMaterializationError(
                "PROJECTION_REQUEST_INVALID", "graph projection schema is invalid"
            )
        artifact_id = _identity(
            request.get("artifact_id"),
            "projection.artifact_id",
            _ID,
            128,
            "PROJECTION_REQUEST_INVALID",
        )
        authoring_artifact_id = _identity(
            request.get("authoring_artifact_id"),
            "projection.authoring_artifact_id",
            _ID,
            128,
            "PROJECTION_REQUEST_INVALID",
        )
        run_id = _identity(
            request.get("run_id"),
            "projection.run_id",
            _ID,
            128,
            "PROJECTION_REQUEST_INVALID",
        )
        triage_artifact_id = _identity(
            request.get("accepted_triage_artifact_id"),
            "projection.accepted_triage_artifact_id",
            _ID,
            128,
            "TRIAGE_PROVENANCE_INVALID",
        )
        triage_artifact_sha256 = _identity(
            request.get("accepted_triage_artifact_sha256"),
            "projection.accepted_triage_artifact_sha256",
            _HEX_SHA256,
            64,
            "TRIAGE_PROVENANCE_INVALID",
        )
        source_task_id = _identity(
            request.get("source_task_id"),
            "projection.source_task_id",
            _ID,
            128,
            "PROJECTION_REQUEST_INVALID",
        )
        board = _identity(
            request.get("board"),
            "projection.board",
            _BOARD,
            64,
            "PROJECTION_REQUEST_INVALID",
        )
        issue_number = request.get("issue_number")
        generation = request.get("generation")
        if type(issue_number) is not int or issue_number < 1:
            raise KanbanGraphMaterializationError(
                "PROJECTION_REQUEST_INVALID", "projection issue number is invalid"
            )
        if type(generation) is not int or generation < 1:
            raise KanbanGraphMaterializationError(
                "PROJECTION_REQUEST_INVALID", "projection generation is invalid"
            )
        workspace_path = _workspace(
            request.get("workspace_path"),
            "projection.workspace_path",
            "PROJECTION_REQUEST_INVALID",
        )

        try:
            triage_record = self._triage_artifact_store.get(triage_artifact_id)
        except ArtifactStoreError as exc:
            raise KanbanGraphMaterializationError(
                "TRIAGE_PROVENANCE_INVALID", "accepted Triage provenance is unavailable"
            ) from exc
        triage_request = {
            "artifact_id": triage_artifact_id,
            "artifact_sha256": triage_artifact_sha256,
            "board": board,
            "issue_number": issue_number,
            "generation": generation,
            "workspace_path": workspace_path,
        }
        try:
            milestone = AcceptedMilestoneMaterializer._accepted_payload(
                triage_record, triage_request
            )
        except BaseException as exc:
            raise KanbanGraphMaterializationError(
                "TRIAGE_PROVENANCE_INVALID", "accepted Triage provenance is invalid"
            ) from exc
        authoring_input = {
            "accepted_triage_artifact_id": triage_record.artifact_id,
            "accepted_triage_artifact_sha256": triage_record.output_sha256,
            "source_task_id": source_task_id,
            "board": board,
            "issue_number": issue_number,
            "generation": generation,
            "workspace_path": workspace_path,
        }
        authoring_record = self._accepted_authoring_artifact(
            artifact_id=authoring_artifact_id,
            run_id=run_id,
            input_value=authoring_input,
            triage_record=triage_record,
            milestone=milestone,
        )
        authoring_graph = json.loads(
            json.dumps(authoring_record.payload, ensure_ascii=False)
        )
        graph = self._derive_graph(
            authoring_graph=authoring_graph,
            source_task_id=source_task_id,
            board=board,
            issue_number=issue_number,
            generation=generation,
            workspace_path=workspace_path,
            triage_record=triage_record,
            milestone=milestone,
        )
        output_sha256 = canonical_json_sha256(graph)
        input_value = {
            "accepted_triage_artifact_id": triage_record.artifact_id,
            "accepted_triage_artifact_sha256": triage_record.output_sha256,
            "accepted_authoring_artifact_id": authoring_record.artifact_id,
            "accepted_authoring_artifact_sha256": authoring_record.output_sha256,
            "accepted_authoring_executor_identity": authoring_record.executor_identity,
            "source_card_sha256": {
                card["key"]: card["body_sha256"] for card in graph["cards"]
            },
            "graph_payload_sha256": output_sha256,
        }
        record = ArtifactRecord(
            artifact_id=artifact_id,
            run_id=f"graph-project:{artifact_id}",
            step_id="task_graph_materialize",
            attempt=1,
            executor_kind="deterministic",
            input_sha256=canonical_json_sha256(input_value),
            output_sha256=output_sha256,
            timeout_seconds=30,
            validation_result="accepted",
            executor_identity="controller-task-graph-projection-v1",
            payload=graph,
        )
        contract = {
            "artifact_id": artifact_id,
            "artifact_sha256": output_sha256,
            "board": board,
            "issue_number": issue_number,
            "generation": generation,
            "workspace_path": workspace_path,
        }
        AcceptedTaskGraphMaterializer._graph(record, contract)
        try:
            stored = self._graph_artifact_store.put(record, input_value=input_value)
        except ArtifactStoreError as exc:
            raise KanbanGraphMaterializationError(
                "GRAPH_ARTIFACT_CONFLICT", "graph artifact persistence was rejected"
            ) from exc
        return {
            "schema": _PROJECTION_RECEIPT_SCHEMA,
            "artifact_id": stored.artifact_id,
            "artifact_sha256": stored.output_sha256,
            "accepted_triage_artifact_id": triage_record.artifact_id,
            "accepted_triage_artifact_sha256": triage_record.output_sha256,
            "accepted_authoring_artifact_id": authoring_record.artifact_id,
            "accepted_authoring_artifact_sha256": authoring_record.output_sha256,
            "verified": True,
        }

    def _accepted_authoring_artifact(
        self,
        *,
        artifact_id: str,
        run_id: str,
        input_value: dict[str, Any],
        triage_record: ArtifactRecord,
        milestone: dict[str, Any],
    ) -> ArtifactRecord:
        triage_body = triage_record.payload.get("body")
        if not isinstance(triage_body, Mapping):
            raise KanbanGraphMaterializationError(
                "TRIAGE_PROVENANCE_INVALID", "accepted Triage body is unavailable"
            )
        try:
            existing = self._authoring_artifact_store.get(artifact_id)
        except ArtifactStoreError as exc:
            if exc.code != "ARTIFACT_NOT_FOUND":
                raise KanbanGraphMaterializationError(
                    "AUTHORING_ARTIFACT_INVALID",
                    "accepted authoring artifact is unavailable",
                ) from exc
        else:
            return self._validate_authoring_artifact(
                existing,
                run_id=run_id,
                input_value=input_value,
                milestone=milestone,
                triage_body=triage_body,
            )
        invocation = _AuthoringInvocation(
            run_id=run_id,
            invocation_id=f"{run_id}:task_graph_author:1",
            step_id="task_graph_author",
            goal=(
                "Load and follow the installed kwc-build-task-graph skill. Author one complete "
                "build-task-graph-v6 draft from the exact accepted "
                "frozen Triage in input.accepted_triage. Inspect the named workspace only "
                "when the Triage requires source confirmation. Include exactly one external "
                "Triage card with null body, one or more backend-implementation-card-v6 "
                "cards, exactly one aggregate-review-card-v5 card, and exactly one "
                "issue-summary-card-v3 card. Preserve the accepted issue, Triage digest, "
                "planning baseline, generation, and workspace exactly. Return only the JSON object."
            ),
            allowed_toolsets=_GRAPH_AUTHORING_TOOLSETS,
            acceptance_criteria=(
                "The result passes canonical build-task-graph-v6 draft validation.",
                "The graph identity and provenance exactly match the accepted Triage artifact.",
                "Every implementation, review, and summary body uses the current canonical schema.",
            ),
            output_schema={"type": "object"},
            input={
                **input_value,
                "accepted_triage": json.loads(
                    json.dumps(triage_body, ensure_ascii=False)
                ),
            },
            timeout_seconds=600,
            working_directory=milestone["workspace_path"],
            skills=("kwc-build-task-graph",),
        )
        try:
            result = self._agent_executor.invoke(invocation)
        except BaseException as exc:
            raise KanbanGraphMaterializationError(
                "AUTHORING_FAILED", "canonical graph authoring failed"
            ) from exc
        output = json.loads(json.dumps(result.output, ensure_ascii=False))
        output_sha256 = canonical_json_sha256(output)
        agent_id = _identity(
            result.agent_id,
            "authoring.agent_id",
            _ID,
            128,
            "AUTHORING_FAILED",
        )
        record = ArtifactRecord(
            artifact_id=artifact_id,
            run_id=run_id,
            step_id="task_graph_author",
            attempt=1,
            executor_kind="agent",
            input_sha256=canonical_json_sha256(input_value),
            output_sha256=output_sha256,
            timeout_seconds=600,
            validation_result="accepted",
            executor_identity=f"{_GRAPH_AUTHORING_EXECUTOR_PREFIX}{agent_id}",
            payload=output,
        )
        self._validate_authoring_artifact(
            record,
            run_id=run_id,
            input_value=input_value,
            milestone=milestone,
            triage_body=triage_body,
        )
        try:
            return self._authoring_artifact_store.put(record, input_value=input_value)
        except ArtifactStoreError:
            try:
                concurrent = self._authoring_artifact_store.get(artifact_id)
            except ArtifactStoreError as exc:
                raise KanbanGraphMaterializationError(
                    "AUTHORING_ARTIFACT_CONFLICT",
                    "canonical authoring artifact persistence was rejected",
                ) from exc
            return self._validate_authoring_artifact(
                concurrent,
                run_id=run_id,
                input_value=input_value,
                milestone=milestone,
                triage_body=triage_body,
            )

    @staticmethod
    def _validate_authoring_artifact(
        record: ArtifactRecord,
        *,
        run_id: str,
        input_value: dict[str, Any],
        milestone: dict[str, Any],
        triage_body: Mapping[str, Any],
    ) -> ArtifactRecord:
        if (
            record.validation_result != "accepted"
            or record.step_id != "task_graph_author"
            or record.executor_kind != "agent"
            or not record.executor_identity.startswith(_GRAPH_AUTHORING_EXECUTOR_PREFIX)
            or not _ID.fullmatch(
                record.executor_identity[len(_GRAPH_AUTHORING_EXECUTOR_PREFIX) :]
            )
            or record.run_id != run_id
            or record.input_sha256 != canonical_json_sha256(input_value)
        ):
            raise KanbanGraphMaterializationError(
                "AUTHORING_ARTIFACT_INVALID", "authoring artifact provenance is invalid"
            )
        try:
            graph = json.loads(json.dumps(record.payload, ensure_ascii=False))
            encoded = json.dumps(graph, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise KanbanGraphMaterializationError(
                "AUTHORING_GRAPH_INVALID", "authoring graph artifact is invalid"
            ) from exc
        if not encoded or len(encoded) > _MAX_AUTHORING_GRAPH_BYTES:
            raise KanbanGraphMaterializationError(
                "AUTHORING_GRAPH_INVALID", "authoring graph artifact exceeds its limit"
            )
        try:
            errors = validate_graph(graph, phase="draft")
        except BaseException as exc:
            raise KanbanGraphMaterializationError(
                "AUTHORING_GRAPH_INVALID", "canonical authoring graph validation failed"
            ) from exc
        if errors:
            raise KanbanGraphMaterializationError(
                "AUTHORING_GRAPH_INVALID", "canonical authoring graph was rejected"
            )
        issue = graph.get("issue")
        baseline = triage_body.get("baseline")
        accepted_issue = triage_body.get("issue")
        if (
            not isinstance(issue, Mapping)
            or not isinstance(accepted_issue, Mapping)
            or not isinstance(baseline, Mapping)
            or issue.get("number") != milestone["issue_number"]
            or issue.get("url") != accepted_issue.get("url")
            or graph.get("generation") != milestone["generation"]
            or graph.get("workspace") != milestone["workspace_path"]
            or graph.get("triage_digest") != milestone["frozen_digest"]
            or graph.get("planning_baseline_sha") != baseline.get("planning_sha")
        ):
            raise KanbanGraphMaterializationError(
                "AUTHORING_GRAPH_IDENTITY_MISMATCH",
                "authoring graph does not match accepted Triage provenance",
            )
        return record

    @staticmethod
    def _derive_graph(
        *,
        authoring_graph: Any,
        source_task_id: str,
        board: str,
        issue_number: int,
        generation: int,
        workspace_path: str,
        triage_record: ArtifactRecord,
        milestone: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(authoring_graph, Mapping):
            raise KanbanGraphMaterializationError(
                "AUTHORING_GRAPH_INVALID", "authoring graph artifact is invalid"
            )
        issue = authoring_graph.get("issue")
        cards = authoring_graph.get("cards")
        if (
            not isinstance(issue, Mapping)
            or issue.get("number") != issue_number
            or authoring_graph.get("generation") != generation
            or authoring_graph.get("workspace") != workspace_path
            or not _sequence(cards)
        ):
            raise KanbanGraphMaterializationError(
                "AUTHORING_GRAPH_IDENTITY_MISMATCH",
                "authoring graph identity does not match the accepted request",
            )
        triage_cards = [
            card
            for card in cards
            if isinstance(card, Mapping) and card.get("card_type") == "triage"
        ]
        if len(triage_cards) != 1:
            raise KanbanGraphMaterializationError(
                "AUTHORING_GRAPH_INVALID", "authoring graph Triage cardinality is invalid"
            )
        triage = triage_cards[0]
        if (
            triage.get("key") != "triage"
            or triage.get("title") != milestone["title"]
            or triage.get("assignee") != milestone["assignee"]
            or triage.get("status") != milestone["status"]
            or list(triage.get("skills") or []) != []
            or triage.get("body") is not None
        ):
            raise KanbanGraphMaterializationError(
                "TRIAGE_PROVENANCE_INVALID",
                "authoring graph Triage card does not match the accepted artifact",
            )
        projected_cards: list[dict[str, Any]] = []
        links: list[dict[str, str]] = []
        for raw_card in cards:
            if not isinstance(raw_card, Mapping):
                raise KanbanGraphMaterializationError(
                    "AUTHORING_GRAPH_INVALID", "authoring graph card is invalid"
                )
            key = raw_card.get("key")
            if key == "triage":
                continue
            role = raw_card.get("card_type")
            if role not in _ROLE_ASSIGNEE:
                raise KanbanGraphMaterializationError(
                    "AUTHORING_GRAPH_INVALID", "authoring graph role is invalid"
                )
            body_text = json.dumps(
                raw_card.get("body"), ensure_ascii=False, indent=2, sort_keys=True
            ) + "\n"
            projected_cards.append(
                {
                    "key": key,
                    "role": role,
                    "title": raw_card.get("title"),
                    "body_text": body_text,
                    "body_sha256": hashlib.sha256(body_text.encode("utf-8")).hexdigest(),
                    "assignee": raw_card.get("assignee"),
                    "skills": list(raw_card.get("skills") or []),
                }
            )
            parents = raw_card.get("parents")
            if not _sequence(parents) or not parents:
                raise KanbanGraphMaterializationError(
                    "AUTHORING_GRAPH_INVALID", "authoring graph card parents are invalid"
                )
            for parent in parents:
                links.append(
                    {
                        "parent_key": "source" if parent == "triage" else parent,
                        "child_key": key,
                    }
                )
        root_impls = [
            card["key"]
            for card in projected_cards
            if card["role"] == "implementation"
            and any(
                link["parent_key"] == "source" and link["child_key"] == card["key"]
                for link in links
            )
        ]
        ready_candidate = authoring_graph.get("ready_candidate")
        if ready_candidate not in root_impls:
            raise KanbanGraphMaterializationError(
                "AUTHORING_GRAPH_INVALID",
                "authoring graph ready candidate must be a source-root implementation",
            )
        return {
            "schema": _PAYLOAD_SCHEMA,
            "board": board,
            "issue_number": issue_number,
            "generation": generation,
            "workspace_path": workspace_path,
            "source_task": {
                "task_id": source_task_id,
                "title": milestone["title"],
                "body_sha256": hashlib.sha256(
                    milestone["body_text"].encode("utf-8")
                ).hexdigest(),
                "status": milestone["status"],
                "assignee": milestone["assignee"],
                "workspace_kind": "dir",
                "workspace_path": milestone["workspace_path"],
                "skills": [],
                "idempotency_key": accepted_triage_idempotency_key(
                    board=milestone["board"],
                    issue_number=milestone["issue_number"],
                    generation=milestone["generation"],
                    artifact_sha256=triage_record.output_sha256,
                ),
                "accepted_artifact_id": triage_record.artifact_id,
                "accepted_artifact_sha256": triage_record.output_sha256,
            },
            "activation_candidate_key": ready_candidate,
            "cards": projected_cards,
            "links": links,
        }


class AcceptedTaskGraphMaterializer:
    def __init__(
        self,
        *,
        artifact_store: SQLiteArtifactStore,
        kanban_port: NativeKanbanGraphMaterializationPort,
    ) -> None:
        self._artifact_store = artifact_store
        self._kanban_port = kanban_port

    def materialize(self, request: Any) -> dict[str, Any]:
        contract = self._request(request)
        record = self._artifact_store.get(contract["artifact_id"])
        graph = self._graph(record, contract)
        snapshot = self._kanban_port.snapshot(board=contract["board"])
        self._reject_generation_conflict(snapshot, graph)
        prior_attempt_ids = self._attempt_task_ids(snapshot, graph)
        task_ids: dict[str, str] = {}
        created = False
        try:
            receipt = self._kanban_port.materialize_graph(
                board=contract["board"],
                graph=graph,
                expected_snapshot_fingerprint=contract["expected_snapshot_fingerprint"],
                artifact_id=record.artifact_id,
                artifact_sha256=record.output_sha256,
            )
            created = isinstance(receipt, Mapping) and receipt.get("created") is True
            task_ids = self._receipt_task_ids(receipt, graph)
            read_back = self._kanban_port.read_graph(
                board=contract["board"],
                source_task_id=graph["source_task"]["task_id"],
                task_ids=task_ids,
            )
            self._verify_read_back(
                read_back,
                graph,
                task_ids,
                artifact_id=record.artifact_id,
                artifact_sha256=record.output_sha256,
            )
        except BaseException as exc:
            known_ids: set[str] = set()
            if isinstance(exc, _NativeGraphCreateCommittedError):
                known_ids.update(exc.created_task_ids.values())
            if created:
                known_ids.update(task_ids.values())
            try:
                expected_count = len(graph["cards"])
                if known_ids:
                    self._kanban_port.rollback_graph(
                        board=contract["board"],
                        task_ids=sorted(known_ids),
                        expected_snapshot_fingerprint=(
                            contract["expected_snapshot_fingerprint"]
                            if len(known_ids) == expected_count
                            else None
                        ),
                    )
                if created and len(known_ids) != expected_count:
                    discovered = self._new_attempt_task_ids(
                        board=contract["board"],
                        graph=graph,
                        prior_ids=prior_attempt_ids,
                    )
                    remaining = discovered - known_ids
                    if remaining:
                        # A second exact rollback is fenced against the same pre-attempt board.
                        # It succeeds only after every attempt-owned task has been removed.
                        self._kanban_port.rollback_graph(
                            board=contract["board"],
                            task_ids=sorted(remaining),
                            expected_snapshot_fingerprint=contract[
                                "expected_snapshot_fingerprint"
                            ],
                        )
                    elif not known_ids:
                        raise KanbanGraphMaterializationError(
                            "ROLLBACK_FAILED", "native graph committed ids were unavailable"
                        )
            except BaseException as rollback_exc:
                if isinstance(rollback_exc, KanbanGraphMaterializationError) and (
                    rollback_exc.code == "ROLLBACK_FAILED"
                ):
                    raise
                raise KanbanGraphMaterializationError(
                    "ROLLBACK_FAILED", "native graph rollback failed"
                ) from rollback_exc
            if isinstance(exc, KanbanGraphMaterializationError):
                raise
            raise KanbanGraphMaterializationError(
                "NATIVE_READBACK_FAILED", "native graph read-back failed"
            ) from exc
        return {
            "schema": _RECEIPT_SCHEMA,
            "artifact_id": record.artifact_id,
            "artifact_sha256": record.output_sha256,
            "board": contract["board"],
            "issue_number": contract["issue_number"],
            "generation": contract["generation"],
            "workspace_path": contract["workspace_path"],
            "source_task_id": graph["source_task"]["task_id"],
            "activation_candidate_key": graph["activation_candidate_key"],
            "task_ids": task_ids,
            "verified": True,
            "idempotent_replay": not created,
        }

    @staticmethod
    def _attempt_task_ids(snapshot: Any, graph: dict[str, Any]) -> set[str]:
        expected = {card["idempotency_key"] for card in graph["cards"]}
        result: set[str] = set()
        for task in snapshot.get("tasks", []) if isinstance(snapshot, Mapping) else []:
            if not isinstance(task, Mapping) or task.get("idempotency_key") not in expected:
                continue
            task_id = task.get("id")
            if isinstance(task_id, str) and _ID.fullmatch(task_id):
                result.add(task_id)
        return result

    def _new_attempt_task_ids(
        self,
        *,
        board: str,
        graph: dict[str, Any],
        prior_ids: set[str],
    ) -> set[str]:
        current = self._kanban_port.snapshot(board=board)
        return self._attempt_task_ids(current, graph) - prior_ids

    @staticmethod
    def _request(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != _REQUEST_FIELDS:
            raise KanbanGraphMaterializationError("REQUEST_INVALID", "graph request is invalid")
        request = dict(value)
        if request.get("schema") != _REQUEST_SCHEMA:
            raise KanbanGraphMaterializationError("REQUEST_INVALID", "graph request schema is invalid")
        request["artifact_id"] = _identity(request.get("artifact_id"), "request.artifact_id", _ID, 128)
        request["artifact_sha256"] = _identity(request.get("artifact_sha256"), "request.artifact_sha256", _HEX_SHA256, 64)
        request["board"] = _identity(request.get("board"), "request.board", _BOARD, 64)
        request["expected_snapshot_fingerprint"] = _identity(
            request.get("expected_snapshot_fingerprint"),
            "request.expected_snapshot_fingerprint",
            _HEX_SHA256,
            64,
        )
        for field in ("issue_number", "generation"):
            if type(request.get(field)) is not int or request[field] < 1:
                raise KanbanGraphMaterializationError("REQUEST_INVALID", f"request.{field} is invalid")
        request["workspace_path"] = _workspace(request.get("workspace_path"), "request.workspace_path")
        return request

    @classmethod
    def _graph(cls, record: Any, contract: dict[str, Any]) -> dict[str, Any]:
        if (
            record.validation_result != "accepted"
            or record.step_id != "task_graph_materialize"
            or record.executor_kind != "deterministic"
            or record.executor_identity != "controller-task-graph-projection-v1"
        ):
            raise KanbanGraphMaterializationError("ARTIFACT_UNACCEPTED", "graph artifact is not accepted")
        if record.output_sha256 != contract["artifact_sha256"]:
            raise KanbanGraphMaterializationError("ARTIFACT_DIGEST_MISMATCH", "graph artifact digest mismatch")
        if not isinstance(record.payload, Mapping) or set(record.payload) != _PAYLOAD_FIELDS:
            raise KanbanGraphMaterializationError("ARTIFACT_INVALID", "graph artifact payload is invalid")
        graph = json.loads(json.dumps(record.payload, ensure_ascii=False))
        if graph.get("schema") != _PAYLOAD_SCHEMA:
            raise KanbanGraphMaterializationError("ARTIFACT_INVALID", "graph artifact schema is invalid")
        for field in ("issue_number", "generation"):
            if type(graph.get(field)) is not int or graph[field] < 1 or graph[field] != contract[field]:
                raise KanbanGraphMaterializationError("ARTIFACT_IDENTITY_MISMATCH", "graph numeric identity mismatch")
        graph["board"] = _identity(graph.get("board"), "artifact.board", _BOARD, 64, "ARTIFACT_INVALID")
        graph["workspace_path"] = _workspace(graph.get("workspace_path"), "artifact.workspace_path", "ARTIFACT_INVALID")
        if graph["board"] != contract["board"] or graph["workspace_path"] != contract["workspace_path"]:
            raise KanbanGraphMaterializationError("ARTIFACT_IDENTITY_MISMATCH", "graph identity mismatch")
        graph["source_task"] = cls._source(graph.get("source_task"), contract)
        cards = graph.get("cards")
        links = graph.get("links")
        if not _sequence(cards) or not cards or not _sequence(links) or not links:
            raise KanbanGraphMaterializationError("ARTIFACT_INVALID", "graph cards or links are invalid")
        if len(cards) > _MAX_CARDS or len(links) > _MAX_LINKS:
            raise KanbanGraphMaterializationError(
                "ARTIFACT_LIMIT_EXCEEDED", "graph cardinality limit exceeded"
            )
        graph["cards"] = [cls._card(card, contract, record.output_sha256) for card in cards]
        graph["links"] = [cls._link(link) for link in links]
        if sum(len(card["body_text"].encode("utf-8")) for card in graph["cards"]) > _MAX_TOTAL_BODY_BYTES:
            raise KanbanGraphMaterializationError(
                "ARTIFACT_LIMIT_EXCEEDED", "graph body limit exceeded"
            )
        graph["activation_candidate_key"] = _identity(
            graph.get("activation_candidate_key"),
            "artifact.activation_candidate_key",
            _ID,
            128,
            "ARTIFACT_INVALID",
        )
        cls._validate_topology(graph)
        return graph

    @staticmethod
    def _source(value: Any, contract: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != _SOURCE_FIELDS:
            raise KanbanGraphMaterializationError("ARTIFACT_INVALID", "graph source task is invalid")
        source = dict(value)
        source["task_id"] = _identity(source.get("task_id"), "artifact.source.task_id", _ID, 128, "ARTIFACT_INVALID")
        source["title"] = _text(source.get("title"), "artifact.source.title", 512, "ARTIFACT_INVALID")
        expected_title = f"G{contract['generation']}-Issue{contract['issue_number']}-Triage"
        if source["title"] != expected_title:
            raise KanbanGraphMaterializationError(
                "ARTIFACT_IDENTITY_MISMATCH", "graph source title identity mismatch"
            )
        source["body_sha256"] = _identity(source.get("body_sha256"), "artifact.source.body_sha256", _HEX_SHA256, 64, "ARTIFACT_INVALID")
        if source.get("status") != "triage":
            raise KanbanGraphMaterializationError("ARTIFACT_INVALID", "graph source status is invalid")
        if source.get("assignee") != "project-manager":
            raise KanbanGraphMaterializationError("ARTIFACT_INVALID", "graph source assignee is invalid")
        if source.get("workspace_kind") != "dir":
            raise KanbanGraphMaterializationError("ARTIFACT_INVALID", "graph source workspace kind is invalid")
        source["workspace_path"] = _workspace(source.get("workspace_path"), "artifact.source.workspace_path", "ARTIFACT_INVALID")
        if source["workspace_path"] != contract["workspace_path"]:
            raise KanbanGraphMaterializationError("ARTIFACT_IDENTITY_MISMATCH", "graph source workspace mismatch")
        source["accepted_artifact_id"] = _identity(
            source.get("accepted_artifact_id"),
            "artifact.source.accepted_artifact_id",
            _ID,
            128,
            "ARTIFACT_INVALID",
        )
        source["accepted_artifact_sha256"] = _identity(
            source.get("accepted_artifact_sha256"),
            "artifact.source.accepted_artifact_sha256",
            _HEX_SHA256,
            64,
            "ARTIFACT_INVALID",
        )
        skills = source.get("skills")
        if not _sequence(skills) or list(skills) != []:
            raise KanbanGraphMaterializationError("ARTIFACT_INVALID", "graph source skills are invalid")
        source["skills"] = []
        source["idempotency_key"] = _identity(
            source.get("idempotency_key"),
            "artifact.source.idempotency_key",
            _ID,
            128,
            "ARTIFACT_INVALID",
        )
        expected_key = accepted_triage_idempotency_key(
            board=contract["board"],
            issue_number=contract["issue_number"],
            generation=contract["generation"],
            artifact_sha256=source["accepted_artifact_sha256"],
        )
        if source["idempotency_key"] != expected_key:
            raise KanbanGraphMaterializationError(
                "ARTIFACT_IDENTITY_MISMATCH", "graph source provenance mismatch"
            )
        return source

    @staticmethod
    def _card(value: Any, contract: dict[str, Any], artifact_sha256: str) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != _CARD_FIELDS:
            raise KanbanGraphMaterializationError("ARTIFACT_INVALID", "graph card is invalid")
        card = dict(value)
        card["key"] = _identity(card.get("key"), "artifact.card.key", _ID, 128, "ARTIFACT_INVALID")
        role = card.get("role")
        if role not in _ROLE_ASSIGNEE or card.get("assignee") != _ROLE_ASSIGNEE[role]:
            raise KanbanGraphMaterializationError("ARTIFACT_INVALID", "graph card role is invalid")
        card["title"] = _text(card.get("title"), "artifact.card.title", 512, "ARTIFACT_INVALID")
        expected_prefix = f"G{contract['generation']}-Issue{contract['issue_number']}-"
        if not card["title"].startswith(expected_prefix):
            raise KanbanGraphMaterializationError("ARTIFACT_INVALID", "graph card title identity is invalid")
        body = card.get("body_text")
        if not isinstance(body, str) or not body:
            raise KanbanGraphMaterializationError("ARTIFACT_INVALID", "graph card body is invalid")
        if len(body.encode("utf-8")) > _MAX_CARD_BODY_BYTES:
            raise KanbanGraphMaterializationError(
                "ARTIFACT_LIMIT_EXCEEDED", "graph card body limit exceeded"
            )
        digest = _identity(card.get("body_sha256"), "artifact.card.body_sha256", _HEX_SHA256, 64, "ARTIFACT_INVALID")
        if hashlib.sha256(body.encode("utf-8")).hexdigest() != digest:
            raise KanbanGraphMaterializationError("ARTIFACT_INVALID", "graph card body digest mismatch")
        try:
            body_value = json.loads(body)
        except (TypeError, ValueError) as exc:
            raise KanbanGraphMaterializationError("ARTIFACT_INVALID", "graph card body JSON is invalid") from exc
        if (
            not isinstance(body_value, Mapping)
            or body_value.get("schema") not in _ROLE_BODY_SCHEMAS[role]
        ):
            raise KanbanGraphMaterializationError("ARTIFACT_INVALID", "graph card body schema is invalid")
        skills = card.get("skills")
        if not _sequence(skills) or any(not isinstance(skill, str) or not skill or len(skill) > 128 for skill in skills):
            raise KanbanGraphMaterializationError("ARTIFACT_INVALID", "graph card skills are invalid")
        if len(skills) > _MAX_SKILLS_PER_CARD:
            raise KanbanGraphMaterializationError(
                "ARTIFACT_LIMIT_EXCEEDED", "graph card skill limit exceeded"
            )
        card["skills"] = list(skills)
        key_digest = hashlib.sha256(card["key"].encode("utf-8")).hexdigest()[:16]
        card["idempotency_key"] = (
            f"controller-graph:{contract['issue_number']}:{contract['generation']}:"
            f"{artifact_sha256[:16]}:{key_digest}"
        )
        return card

    @staticmethod
    def _link(value: Any) -> dict[str, str]:
        if not isinstance(value, Mapping) or set(value) != _LINK_FIELDS:
            raise KanbanGraphMaterializationError("ARTIFACT_INVALID", "graph link is invalid")
        return {
            "parent_key": _identity(value.get("parent_key"), "artifact.link.parent_key", _ID, 128, "ARTIFACT_INVALID"),
            "child_key": _identity(value.get("child_key"), "artifact.link.child_key", _ID, 128, "ARTIFACT_INVALID"),
        }

    @staticmethod
    def _validate_topology(graph: dict[str, Any]) -> None:
        cards = graph["cards"]
        keys = [card["key"] for card in cards]
        if len(keys) != len(set(keys)):
            raise KanbanGraphMaterializationError("GRAPH_INVALID", "graph card keys are duplicated")
        by_key = {card["key"]: card for card in cards}
        roles = [card["role"] for card in cards]
        if roles.count("implementation") < 1 or roles.count("review") != 1 or roles.count("summary") != 1:
            raise KanbanGraphMaterializationError("GRAPH_INVALID", "graph role cardinality is invalid")
        candidate = graph["activation_candidate_key"]
        if candidate not in by_key or by_key[candidate]["role"] != "implementation":
            raise KanbanGraphMaterializationError("GRAPH_INVALID", "graph activation candidate is invalid")
        endpoints = {"source", *keys}
        pairs = [(link["parent_key"], link["child_key"]) for link in graph["links"]]
        if len(pairs) != len(set(pairs)) or any(parent == child for parent, child in pairs):
            raise KanbanGraphMaterializationError("GRAPH_INVALID", "graph links are duplicated or self-referential")
        if any(parent not in endpoints or child not in by_key for parent, child in pairs):
            raise KanbanGraphMaterializationError("GRAPH_INVALID", "graph link endpoint is invalid")
        parents = _links_by_child(graph["links"])
        if "source" not in parents.get(candidate, []):
            raise KanbanGraphMaterializationError("GRAPH_INVALID", "activation candidate is not a source root")
        impl_keys = {key for key, card in by_key.items() if card["role"] == "implementation"}
        review_key = next(key for key, card in by_key.items() if card["role"] == "review")
        summary_key = next(key for key, card in by_key.items() if card["role"] == "summary")
        for key in impl_keys:
            if not parents.get(key):
                raise KanbanGraphMaterializationError("GRAPH_INVALID", "implementation card has no parent")
            if any(parent != "source" and parent not in impl_keys for parent in parents[key]):
                raise KanbanGraphMaterializationError("GRAPH_INVALID", "implementation parent role is invalid")
        if set(parents.get(review_key, [])) != impl_keys:
            raise KanbanGraphMaterializationError("GRAPH_INVALID", "review parents do not close implementation set")
        if set(parents.get(summary_key, [])) != {review_key, *impl_keys}:
            raise KanbanGraphMaterializationError("GRAPH_INVALID", "summary parent is invalid")
        indegree = {key: 0 for key in keys}
        children = {key: [] for key in keys}
        for parent, child in pairs:
            if parent == "source":
                continue
            indegree[child] += 1
            children[parent].append(child)
        queue = [key for key, degree in indegree.items() if degree == 0]
        seen = 0
        while queue:
            current = queue.pop()
            seen += 1
            for child in children[current]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        if seen != len(keys):
            raise KanbanGraphMaterializationError("GRAPH_CYCLE", "graph contains a cycle")

    @staticmethod
    def _reject_generation_conflict(snapshot: Any, graph: dict[str, Any]) -> None:
        expected = {card["idempotency_key"] for card in graph["cards"]}
        prefix = f"controller-graph:{graph['issue_number']}:{graph['generation']}:"
        for task in snapshot.get("tasks", []) if isinstance(snapshot, Mapping) else []:
            key = task.get("idempotency_key") if isinstance(task, Mapping) else None
            if isinstance(key, str) and key.startswith(prefix) and key not in expected:
                raise KanbanGraphMaterializationError("GENERATION_CONFLICT", "graph generation already has different accepted bytes")

    @staticmethod
    def _receipt_task_ids(receipt: Any, graph: dict[str, Any]) -> dict[str, str]:
        if not isinstance(receipt, Mapping) or set(receipt) != {"task_ids", "created"} or type(receipt.get("created")) is not bool:
            raise KanbanGraphMaterializationError("CREATE_RECEIPT_INVALID", "native graph create receipt is invalid")
        task_ids = receipt.get("task_ids")
        expected_keys = {card["key"] for card in graph["cards"]}
        if not isinstance(task_ids, Mapping) or set(task_ids) != expected_keys:
            raise KanbanGraphMaterializationError("CREATE_RECEIPT_INVALID", "native graph task ids are invalid")
        result = {
            key: _identity(task_id, "receipt.task_id", _ID, 128, "CREATE_RECEIPT_INVALID")
            for key, task_id in task_ids.items()
        }
        if len(set(result.values())) != len(result):
            raise KanbanGraphMaterializationError("CREATE_RECEIPT_INVALID", "native graph task ids are duplicated")
        return result

    @staticmethod
    def _verify_read_back(
        read_back: Any,
        graph: dict[str, Any],
        task_ids: dict[str, str],
        *,
        artifact_id: str,
        artifact_sha256: str,
    ) -> None:
        if not isinstance(read_back, Mapping) or set(read_back) != {
            "source_task",
            "tasks",
            "links",
            "events",
        }:
            raise KanbanGraphMaterializationError("NATIVE_READBACK_MISMATCH", "native graph read-back is invalid")
        source = read_back.get("source_task")
        if not isinstance(source, Mapping) or not _source_matches(
            source, graph["source_task"]
        ):
            raise KanbanGraphMaterializationError(
                "SOURCE_TASK_CHANGED", "source task read-back mismatch"
            )
        tasks = read_back.get("tasks")
        links = read_back.get("links")
        events = read_back.get("events")
        if not _sequence(tasks) or not _sequence(links) or not isinstance(events, Mapping):
            raise KanbanGraphMaterializationError("NATIVE_READBACK_MISMATCH", "native graph read-back shape is invalid")
        by_id = {task.get("id"): task for task in tasks if isinstance(task, Mapping)}
        cards = {card["key"]: card for card in graph["cards"]}
        if set(by_id) != set(task_ids.values()):
            raise KanbanGraphMaterializationError("NATIVE_READBACK_MISMATCH", "native graph task identity mismatch")
        for key, task_id in task_ids.items():
            task = by_id[task_id]
            card = cards[key]
            body = task.get("body")
            exact = {
                "title": task.get("title"),
                "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest() if isinstance(body, str) else "",
                "assignee": task.get("assignee"),
                "status": task.get("status"),
                "workspace_kind": task.get("workspace_kind"),
                "workspace_path": task.get("workspace_path"),
                "skills": list(task.get("skills") or []),
                "idempotency_key": task.get("idempotency_key"),
            }
            expected = {
                "title": card["title"],
                "body_sha256": card["body_sha256"],
                "assignee": card["assignee"],
                "status": "todo",
                "workspace_kind": "dir",
                "workspace_path": graph["workspace_path"],
                "skills": card["skills"],
                "idempotency_key": card["idempotency_key"],
            }
            if exact != expected:
                raise KanbanGraphMaterializationError("NATIVE_READBACK_MISMATCH", "native graph task read-back mismatch")
            task_events = events.get(task_id)
            expected_event = {
                "kind": "controller_graph_materialized",
                "payload": {
                    "artifact_id": artifact_id,
                    "artifact_sha256": artifact_sha256,
                },
            }
            if not _sequence(task_events) or list(task_events) != [expected_event]:
                raise KanbanGraphMaterializationError(
                    "NATIVE_READBACK_MISMATCH", "native graph event read-back mismatch"
                )
        endpoint_ids = {"source": graph["source_task"]["task_id"], **task_ids}
        expected_links = {
            (endpoint_ids[link["parent_key"]], endpoint_ids[link["child_key"]])
            for link in graph["links"]
        }
        actual_links = {
            (link.get("parent_id"), link.get("child_id"))
            for link in links
            if isinstance(link, Mapping)
        }
        if actual_links != expected_links:
            raise KanbanGraphMaterializationError("NATIVE_READBACK_MISMATCH", "native graph link read-back mismatch")


def _links_by_child(links: list[dict[str, str]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for link in links:
        result.setdefault(link["child_key"], []).append(link["parent_key"])
    return result


def _source_matches(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    body = actual.get("body")
    return {
        "task_id": actual.get("id"),
        "title": actual.get("title"),
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest() if isinstance(body, str) else "",
        "status": actual.get("status"),
        "assignee": actual.get("assignee"),
        "workspace_kind": actual.get("workspace_kind"),
        "workspace_path": actual.get("workspace_path"),
        "skills": list(actual.get("skills") or []),
        "idempotency_key": actual.get("idempotency_key"),
    } == {
        "task_id": expected.get("task_id"),
        "title": expected.get("title"),
        "body_sha256": expected.get("body_sha256"),
        "status": expected.get("status"),
        "assignee": expected.get("assignee"),
        "workspace_kind": expected.get("workspace_kind"),
        "workspace_path": expected.get("workspace_path"),
        "skills": list(expected.get("skills") or []),
        "idempotency_key": expected.get("idempotency_key"),
    }


def _sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def _text(value: Any, field: str, max_length: int, code: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > max_length:
        raise KanbanGraphMaterializationError(code, f"{field} is invalid")
    return value


def _identity(
    value: Any,
    field: str,
    pattern: re.Pattern[str],
    max_length: int,
    code: str = "REQUEST_INVALID",
) -> str:
    text = _text(value, field, max_length, code)
    if not pattern.fullmatch(text):
        raise KanbanGraphMaterializationError(code, f"{field} is invalid")
    return text


def _workspace(value: Any, field: str, code: str = "REQUEST_INVALID") -> str:
    text = _text(value, field, 4096, code)
    path = Path(text)
    if not path.is_absolute() or not path.is_dir() or path.resolve().as_posix() != text:
        raise KanbanGraphMaterializationError(code, f"{field} is invalid")
    return text


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)
