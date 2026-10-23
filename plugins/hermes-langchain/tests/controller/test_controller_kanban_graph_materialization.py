"""Contract tests for accepted Impl/Review/Summary graph materialization."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hermes_langchain.controller.artifact_store import (
    ArtifactRecord,
    SQLiteArtifactStore,
    canonical_json_sha256,
)
from hermes_langchain.controller.kanban_graph_materialization import (
    AcceptedTaskGraphProjector,
    AcceptedTaskGraphMaterializer,
    KanbanGraphMaterializationError,
    _NativeGraphCreateCommittedError,
    fingerprint_graph_snapshot,
)
from hermes_langchain.controller.kanban_materialization import (
    AcceptedMilestoneMaterializer,
    accepted_triage_idempotency_key,
    fingerprint_native_snapshot,
)
from hermes_langchain.authoring.graph.graph_contract import validate_graph


class FakeGraphPort:
    def __init__(self, source: dict) -> None:
        self.source = dict(source)
        self.tasks: dict[str, dict] = {source["id"]: dict(source)}
        self.links: list[dict[str, str]] = []
        self.events: dict[str, list[dict]] = {}
        self.materializations = 0
        self.rollbacks = 0

    def snapshot(self, *, board: str) -> dict:
        return {
            "board": board,
            "tasks": [dict(task) for task in self.tasks.values()],
            "links": [dict(link) for link in self.links],
        }

    def materialize_graph(
        self,
        *,
        board: str,
        graph: dict,
        expected_snapshot_fingerprint: str,
        artifact_id: str,
        artifact_sha256: str,
    ) -> dict:
        expected_keys = {
            card["key"]: card["idempotency_key"] for card in graph["cards"]
        }
        existing = {
            key: next(
                (
                    task["id"]
                    for task in self.tasks.values()
                    if task.get("idempotency_key") == idempotency_key
                ),
                None,
            )
            for key, idempotency_key in expected_keys.items()
        }
        if all(existing.values()):
            return {"task_ids": existing, "created": False}
        if any(existing.values()):
            raise KanbanGraphMaterializationError(
                "CONFLICTING_REPLAY", "graph has a partial replay"
            )
        if fingerprint_graph_snapshot(self.snapshot(board=board)) != expected_snapshot_fingerprint:
            raise KanbanGraphMaterializationError("SNAPSHOT_CHANGED", "graph snapshot changed")
        self.materializations += 1
        task_ids: dict[str, str] = {}
        for ordinal, card in enumerate(graph["cards"], start=1):
            task_id = f"t_graph_{ordinal}"
            task_ids[card["key"]] = task_id
            self.tasks[task_id] = {
                "id": task_id,
                "title": card["title"],
                "body": card["body_text"],
                "assignee": card["assignee"],
                "status": "todo",
                "workspace_kind": "dir",
                "workspace_path": graph["workspace_path"],
                "skills": list(card["skills"]),
                "idempotency_key": card["idempotency_key"],
            }
            self.events[task_id] = [
                {
                    "kind": "controller_graph_materialized",
                    "payload": {
                        "artifact_id": artifact_id,
                        "artifact_sha256": artifact_sha256,
                    },
                }
            ]
        endpoint_ids = {"source": graph["source_task"]["task_id"], **task_ids}
        self.links.extend(
            {
                "parent_id": endpoint_ids[link["parent_key"]],
                "child_id": endpoint_ids[link["child_key"]],
            }
            for link in graph["links"]
        )
        return {"task_ids": task_ids, "created": True}

    def read_graph(
        self, *, board: str, source_task_id: str, task_ids: dict[str, str]
    ) -> dict:
        del board
        expected_keys = {
            self.tasks[task_id]["idempotency_key"] for task_id in task_ids.values()
        }
        selected = [
            dict(task)
            for task in self.tasks.values()
            if task.get("idempotency_key") in expected_keys
        ]
        selected_ids = {source_task_id, *task_ids.values()}
        return {
            "source_task": dict(self.tasks[source_task_id]),
            "tasks": selected,
            "events": {
                task_id: [dict(event) for event in self.events.get(task_id, [])]
                for task_id in task_ids.values()
            },
            "links": [
                dict(link)
                for link in self.links
                if link["parent_id"] in selected_ids or link["child_id"] in selected_ids
            ],
        }

    def rollback_graph(
        self,
        *,
        board: str,
        task_ids: list[str],
        expected_snapshot_fingerprint: str | None,
    ) -> None:
        self.rollbacks += 1
        task_id_set = set(task_ids)
        self.links = [
            link
            for link in self.links
            if link["parent_id"] not in task_id_set
            and link["child_id"] not in task_id_set
        ]
        for task_id in task_ids:
            self.events.pop(task_id, None)
            self.tasks.pop(task_id, None)
        if any(
            task_id in self.tasks
            or task_id in self.events
            or any(task_id in link.values() for link in self.links)
            for task_id in task_ids
        ):
            raise AssertionError("graph rollback was not exact")
        if (
            expected_snapshot_fingerprint is not None
            and fingerprint_graph_snapshot(self.snapshot(board=board))
            != expected_snapshot_fingerprint
        ):
            raise AssertionError("graph rollback did not restore the snapshot")


class ControllerKanbanGraphMaterializationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.store = SQLiteArtifactStore.open(self.root / "artifacts.sqlite3")
        self.addCleanup(self.store.close)
        source_body = '{"schema":"triage-v7","phase":"frozen"}\n'
        self.source = {
            "id": "t_triage_source",
            "title": "G3-Issue151-Triage",
            "body": source_body,
            "assignee": "project-manager",
            "status": "triage",
            "workspace_kind": "dir",
            "workspace_path": self.workspace.as_posix(),
            "skills": [],
            "idempotency_key": accepted_triage_idempotency_key(
                board="disposable-controller-graph",
                issue_number=151,
                generation=3,
                artifact_sha256="a" * 64,
            ),
        }
        impl_body = json.dumps({"schema": "backend-implementation-card-v6"}) + "\n"
        review_body = json.dumps({"schema": "aggregate-review-card-v5"}) + "\n"
        summary_body = json.dumps({"schema": "issue-summary-card-v3"}) + "\n"
        self.payload = {
            "schema": "controller-task-graph-v1",
            "board": "disposable-controller-graph",
            "issue_number": 151,
            "generation": 3,
            "workspace_path": self.workspace.as_posix(),
            "source_task": {
                "task_id": self.source["id"],
                "title": self.source["title"],
                "body_sha256": hashlib.sha256(source_body.encode()).hexdigest(),
                "status": "triage",
                "assignee": "project-manager",
                "workspace_kind": "dir",
                "workspace_path": self.workspace.as_posix(),
                "skills": [],
                "idempotency_key": self.source["idempotency_key"],
                "accepted_artifact_id": "artifact-accepted-triage",
                "accepted_artifact_sha256": "a" * 64,
            },
            "activation_candidate_key": "impl-1",
            "cards": [
                self.card("impl-1", "implementation", "G3-Issue151-Impl1", impl_body, "coder"),
                self.card("review", "review", "G3-Issue151-Review", review_body, "reviewer"),
                self.card("summary", "summary", "G3-Issue151-Summary", summary_body, "project-manager"),
            ],
            "links": [
                {"parent_key": "source", "child_key": "impl-1"},
                {"parent_key": "impl-1", "child_key": "review"},
                {"parent_key": "impl-1", "child_key": "summary"},
                {"parent_key": "review", "child_key": "summary"},
            ],
        }
        self.record = self.record_for("artifact-task-graph-1", self.payload)
        self.store.put(
            self.record,
            input_value={"accepted": self.record.artifact_id},
        )
        self.port = FakeGraphPort(self.source)


    @staticmethod
    def card(key: str, role: str, title: str, body: str, assignee: str) -> dict:
        return {
            "key": key,
            "role": role,
            "title": title,
            "body_text": body,
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "assignee": assignee,
            "skills": [f"kwc-{role}-authoring-runtime"],
        }

    @staticmethod
    def record_for(artifact_id: str, payload: dict) -> ArtifactRecord:
        return ArtifactRecord(
            artifact_id=artifact_id,
            run_id=f"run-{artifact_id}",
            step_id="task_graph_materialize",
            attempt=1,
            executor_kind="deterministic",
            input_sha256=canonical_json_sha256({"accepted": artifact_id}),
            output_sha256=canonical_json_sha256(payload),
            timeout_seconds=30,
            validation_result="accepted",
            executor_identity="controller-task-graph-projection-v1",
            payload=payload,
        )

    def request(self) -> dict:
        return {
            "schema": "controller-kanban-graph-materialization-request-v1",
            "artifact_id": self.record.artifact_id,
            "artifact_sha256": self.record.output_sha256,
            "board": self.payload["board"],
            "issue_number": self.payload["issue_number"],
            "generation": self.payload["generation"],
            "workspace_path": self.payload["workspace_path"],
            "expected_snapshot_fingerprint": fingerprint_graph_snapshot(
                self.port.snapshot(board=self.payload["board"])
            ),
        }

    def materializer(self) -> AcceptedTaskGraphMaterializer:
        return AcceptedTaskGraphMaterializer(
            artifact_store=self.store,
            kanban_port=self.port,
        )

    def test_accepted_graph_materializes_with_exact_tasks_and_links(self) -> None:
        receipt = self.materializer().materialize(self.request())

        self.assertTrue(receipt["verified"])
        self.assertFalse(receipt["idempotent_replay"])
        self.assertEqual("impl-1", receipt["activation_candidate_key"])
        self.assertEqual({"impl-1", "review", "summary"}, set(receipt["task_ids"]))
        self.assertEqual(1, self.port.materializations)
        self.assertEqual(4, len(self.port.links))

    def test_exact_replay_has_no_duplicate_and_conflicting_generation_is_rejected(self) -> None:
        first = self.materializer().materialize(self.request())
        replay = self.materializer().materialize(self.request())

        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(first["task_ids"], replay["task_ids"])
        self.assertEqual(1, self.port.materializations)

        changed_payload = dict(self.payload, activation_candidate_key="impl-other")
        changed = self.record_for("artifact-task-graph-2", changed_payload)
        self.store.put(changed, input_value={"accepted": changed.artifact_id})
        changed_request = dict(
            self.request(),
            artifact_id=changed.artifact_id,
            artifact_sha256=changed.output_sha256,
            expected_snapshot_fingerprint=fingerprint_graph_snapshot(
                self.port.snapshot(board=self.payload["board"])
            ),
        )
        with self.assertRaises(KanbanGraphMaterializationError):
            self.materializer().materialize(changed_request)
        self.assertEqual(1, self.port.materializations)

        self.port.tasks["t_archived_conflict"] = {
            **self.port.tasks[first["task_ids"]["impl-1"]],
            "id": "t_archived_conflict",
            "status": "archived",
            "idempotency_key": "controller-graph:151:3:different:archived",
        }
        with self.assertRaisesRegex(KanbanGraphMaterializationError, "different accepted bytes"):
            self.materializer().materialize(self.request())

    def test_exact_replay_rejects_source_task_drift(self) -> None:
        self.materializer().materialize(self.request())
        for field, value in (
            ("title", "drifted-source"),
            ("assignee", "coder"),
            ("workspace_kind", "worktree"),
            ("skills", ["unexpected-skill"]),
        ):
            original = self.port.tasks[self.source["id"]][field]
            self.port.tasks[self.source["id"]][field] = value
            with self.subTest(field=field):
                with self.assertRaisesRegex(KanbanGraphMaterializationError, "source"):
                    self.materializer().materialize(self.request())
            self.port.tasks[self.source["id"]][field] = original
        self.assertEqual(1, self.port.materializations)

    def test_contract_rejects_invalid_endpoints_cycles_roles_and_candidate(self) -> None:
        cases = []
        cases.append(dict(self.payload, links=[{"parent_key": "missing", "child_key": "impl-1"}]))
        cases.append(
            dict(
                self.payload,
                links=[
                    {"parent_key": "source", "child_key": "impl-1"},
                    {"parent_key": "impl-1", "child_key": "review"},
                    {"parent_key": "review", "child_key": "impl-1"},
                    {"parent_key": "review", "child_key": "summary"},
                ],
            )
        )
        bad_roles = [dict(card) for card in self.payload["cards"]]
        bad_roles[1]["role"] = "implementation"
        cases.append(dict(self.payload, cards=bad_roles))
        cases.append(dict(self.payload, activation_candidate_key="review"))
        for index, payload in enumerate(cases):
            record = self.record_for(f"artifact-invalid-graph-{index}", payload)
            self.store.put(record, input_value={"accepted": record.artifact_id})
            request = dict(
                self.request(),
                artifact_id=record.artifact_id,
                artifact_sha256=record.output_sha256,
            )
            with self.subTest(index=index):
                with self.assertRaises(KanbanGraphMaterializationError):
                    self.materializer().materialize(request)
        self.assertEqual(0, self.port.materializations)

    def test_readback_failure_rolls_back_attempt_owned_tasks_and_links(self) -> None:
        class TamperedPort(FakeGraphPort):
            def read_graph(self, **kwargs):
                value = super().read_graph(**kwargs)
                value["tasks"][0]["body"] += "tampered"
                return value

        self.port = TamperedPort(self.source)
        with self.assertRaisesRegex(KanbanGraphMaterializationError, "read-back"):
            self.materializer().materialize(self.request())

        self.assertEqual(1, self.port.rollbacks)
        self.assertEqual([], self.port.links)
        self.assertEqual({self.source["id"]}, set(self.port.tasks))

        self.port = FakeGraphPort(self.source)
        retried = self.materializer().materialize(self.request())
        self.assertTrue(retried["verified"])
        self.assertFalse(retried["idempotent_replay"])

    def test_post_commit_and_malformed_receipt_failures_roll_back(self) -> None:
        class PostCommitPort(FakeGraphPort):
            def materialize_graph(self, **kwargs):
                receipt = super().materialize_graph(**kwargs)
                raise _NativeGraphCreateCommittedError(receipt["task_ids"])

        class MalformedReceiptPort(FakeGraphPort):
            def materialize_graph(self, **kwargs):
                receipt = super().materialize_graph(**kwargs)
                return {"created": True, "task_ids": {"impl-1": receipt["task_ids"]["impl-1"]}}

        for port_type in (PostCommitPort, MalformedReceiptPort):
            self.port = port_type(self.source)
            with self.subTest(port=port_type.__name__):
                with self.assertRaises(KanbanGraphMaterializationError):
                    self.materializer().materialize(self.request())
                self.assertEqual(1, self.port.rollbacks)
                self.assertEqual({self.source["id"]}, set(self.port.tasks))
                self.assertEqual([], self.port.links)

    def test_replay_requires_unique_tasks_audit_events_and_exact_incident_links(self) -> None:
        first = self.materializer().materialize(self.request())
        first_task_id = first["task_ids"]["impl-1"]

        self.port.events[first_task_id].clear()
        with self.assertRaisesRegex(KanbanGraphMaterializationError, "event"):
            self.materializer().materialize(self.request())
        self.port.events[first_task_id] = [
            {
                "kind": "controller_graph_materialized",
                "payload": {
                    "artifact_id": self.record.artifact_id,
                    "artifact_sha256": self.record.output_sha256,
                },
            }
        ]

        duplicate = dict(self.port.tasks[first_task_id], id="t_graph_duplicate")
        self.port.tasks[duplicate["id"]] = duplicate
        with self.assertRaisesRegex(KanbanGraphMaterializationError, "identity"):
            self.materializer().materialize(self.request())
        del self.port.tasks[duplicate["id"]]

        self.port.tasks["t_external"] = {
            **self.port.tasks[first_task_id],
            "id": "t_external",
            "idempotency_key": "external-task",
        }
        self.port.links.append(
            {"parent_id": self.source["id"], "child_id": "t_external"}
        )
        with self.assertRaisesRegex(KanbanGraphMaterializationError, "link"):
            self.materializer().materialize(self.request())
        self.port.links.pop()
        self.port.links.append(
            {"parent_id": first_task_id, "child_id": "t_external"}
        )
        with self.assertRaisesRegex(KanbanGraphMaterializationError, "link"):
            self.materializer().materialize(self.request())

    def test_source_is_bound_to_issue_generation_and_triage_identity(self) -> None:
        bad_source = dict(
            self.payload["source_task"],
            title="G99-Issue999-Triage",
            idempotency_key="unrelated-source",
        )
        payload = dict(self.payload, source_task=bad_source)
        record = self.record_for("artifact-invalid-source-binding", payload)
        self.store.put(record, input_value={"accepted": record.artifact_id})
        request = dict(
            self.request(),
            artifact_id=record.artifact_id,
            artifact_sha256=record.output_sha256,
        )

        with self.assertRaisesRegex(KanbanGraphMaterializationError, "source"):
            self.materializer().materialize(request)
        self.assertEqual(0, self.port.materializations)

    def test_projection_limits_cards_links_skills_and_total_body_bytes(self) -> None:
        too_many_skills = [dict(card) for card in self.payload["cards"]]
        too_many_skills[0]["skills"] = [f"skill-{index}" for index in range(17)]
        payloads = [dict(self.payload, cards=too_many_skills)]

        large_body = json.dumps(
            {"schema": "backend-implementation-card-v6", "padding": "x" * 1_100_000}
        )
        large_cards = [dict(card) for card in self.payload["cards"]]
        large_cards[0] = self.card(
            "impl-1", "implementation", "G3-Issue151-Impl1", large_body, "coder"
        )
        payloads.append(dict(self.payload, cards=large_cards))

        for index, payload in enumerate(payloads):
            record = self.record_for(f"artifact-over-limit-{index}", payload)
            self.store.put(record, input_value={"accepted": record.artifact_id})
            request = dict(
                self.request(),
                artifact_id=record.artifact_id,
                artifact_sha256=record.output_sha256,
            )
            with self.subTest(index=index):
                with self.assertRaisesRegex(KanbanGraphMaterializationError, "limit"):
                    self.materializer().materialize(request)
        self.assertEqual(0, self.port.materializations)

    def test_projection_accepts_source_created_by_canonical_milestone_materializer(self) -> None:
        milestone = {
            "schema": "controller-triage-milestone-v1",
            "board": self.payload["board"],
            "issue_number": 151,
            "generation": 3,
            "workspace_path": self.workspace.as_posix(),
            "title": "G3-Issue151-Triage",
            "assignee": "project-manager",
            "status": "triage",
            "body_text": (
                '{"schema":"triage-v7","issue":{"number":151,"title":"테스트",'
                '"url":"https://github.com/example/repository/issues/151"},'
                '"baseline":{"planning_sha":"' + "d" * 40 + '"},'
                '"frozen_digest":"' + "c" * 64 + '"}\n'
            ),
            "frozen_digest": "c" * 64,
        }
        frozen_body_file = ".temp/controller-triage/frozen-triage.json"
        accepted_payload = {
            "stage": "materialize",
            "artifact": {
                "path": frozen_body_file,
                "sha256": hashlib.sha256(
                    milestone["body_text"].encode("utf-8")
                ).hexdigest(),
            },
            "body": json.loads(milestone["body_text"]),
            "body_text": milestone["body_text"],
            "frozen_digest": milestone["frozen_digest"],
            "read_back": {
                "valid": True,
                "verified": True,
                "task_id": "t_canonical_fixture",
                "title": milestone["title"],
                "status": "triage",
                "frozen_digest": milestone["frozen_digest"],
                "frozen_body_file": frozen_body_file,
            },
            "milestone": milestone,
        }
        triage_record = ArtifactRecord(
            artifact_id="artifact-canonical-triage-source",
            run_id="run-canonical-triage-source",
            step_id="triage_materialize",
            attempt=1,
            executor_kind="materialize",
            input_sha256=canonical_json_sha256({"accepted": "canonical-triage"}),
            output_sha256=canonical_json_sha256(accepted_payload),
            timeout_seconds=300,
            validation_result="accepted",
            executor_identity="controller-triage-materialize",
            payload=accepted_payload,
        )
        self.store.put(triage_record, input_value={"accepted": "canonical-triage"})
        class CanonicalTriagePort:
            def __init__(self) -> None:
                self.tasks: dict[str, dict] = {}

            def snapshot(self, *, board: str) -> dict:
                return {"board": board, "tasks": list(self.tasks.values())}

            def create_triage(self, *, board: str, card: dict, expected_snapshot_fingerprint: str) -> dict:
                if fingerprint_native_snapshot(self.snapshot(board=board)) != expected_snapshot_fingerprint:
                    raise AssertionError("unexpected snapshot")
                task_id = "t_canonical_source"
                self.tasks[task_id] = {
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
                return {"task_id": task_id, "created": True}

            def read_task(self, *, board: str, task_id: str) -> dict:
                del board
                return dict(self.tasks[task_id])

            def archive_task(self, *, board: str, task_id: str) -> None:
                del board
                self.tasks[task_id]["status"] = "archived"

        triage_port = CanonicalTriagePort()
        source_receipt = AcceptedMilestoneMaterializer(
            artifact_store=self.store,
            kanban_port=triage_port,
        ).materialize(
            {
                "schema": "controller-kanban-materialization-v1",
                "artifact_id": triage_record.artifact_id,
                "artifact_sha256": triage_record.output_sha256,
                "board": milestone["board"],
                "issue_number": 151,
                "generation": 3,
                "workspace_path": self.workspace.as_posix(),
                "expected_snapshot_fingerprint": fingerprint_native_snapshot(
                    triage_port.snapshot(board=milestone["board"])
                ),
            }
        )
        source = triage_port.tasks[source_receipt["task_id"]]
        authoring_cards = [
            {
                "key": "triage",
                "title": milestone["title"],
                "card_type": "triage",
                "assignee": "project-manager",
                "skills": [],
                "status": "triage",
                "parents": [],
                "body": None,
            }
        ]
        for card in self.payload["cards"]:
            authoring_cards.append(
                {
                    "key": card["key"],
                    "title": card["title"],
                    "card_type": card["role"],
                    "assignee": card["assignee"],
                    "skills": card["skills"],
                    "status": "todo",
                    "parents": [
                        "triage" if link["parent_key"] == "source" else link["parent_key"]
                        for link in self.payload["links"]
                        if link["child_key"] == card["key"]
                    ],
                    "body": json.loads(card["body_text"]),
                }
            )
        authoring_graph = {
            "schema": "build-task-graph-v6",
            "issue": {
                "number": 151,
                "url": "https://github.com/example/repository/issues/151",
            },
            "generation": 3,
            "workspace": self.workspace.as_posix(),
            "triage_digest": "c" * 64,
            "planning_baseline_sha": "d" * 40,
            "ready_candidate": "impl-1",
            "cards": authoring_cards,
        }

        class AuthoringExecutor:
            def __init__(self, output: dict) -> None:
                self.output = output
                self.invocations = []

            def invoke(self, invocation):
                self.invocations.append(invocation)
                return type(
                    "Result",
                    (),
                    {"agent_id": "agent-canonical-author", "output": self.output},
                )()

        authoring_executor = AuthoringExecutor(authoring_graph)
        projector = AcceptedTaskGraphProjector(
            triage_artifact_store=self.store,
            authoring_artifact_store=self.store,
            graph_artifact_store=self.store,
            agent_executor=authoring_executor,
        )
        projection_request = {
            "schema": "controller-task-graph-projection-request-v1",
            "artifact_id": "artifact-canonical-projected-graph",
            "authoring_artifact_id": "artifact-canonical-authored-graph",
            "run_id": "run-canonical-graph-authoring",
            "accepted_triage_artifact_id": triage_record.artifact_id,
            "accepted_triage_artifact_sha256": triage_record.output_sha256,
            "source_task_id": source["id"],
            "board": milestone["board"],
            "issue_number": 151,
            "generation": 3,
            "workspace_path": self.workspace.as_posix(),
        }
        with patch(
            "hermes_langchain.controller.kanban_graph_materialization.validate_graph",
            return_value=[],
        ) as canonical_validator:
            projected = projector.project(projection_request)
            replay = projector.project(projection_request)
        self.assertEqual(2, canonical_validator.call_count)
        self.assertTrue(
            all(call.kwargs["phase"] == "draft" for call in canonical_validator.call_args_list)
        )
        self.assertEqual(1, len(authoring_executor.invocations))
        self.assertEqual(projected, replay)
        self.assertIn(
            "kwc-build-task-graph", authoring_executor.invocations[0].goal
        )
        self.assertIn("skills", authoring_executor.invocations[0].allowed_toolsets)
        multi_root_graph = json.loads(json.dumps(authoring_graph))
        second_impl = json.loads(json.dumps(multi_root_graph["cards"][1]))
        second_impl["key"] = "impl-2"
        second_impl["title"] = "G3-Issue151-Impl2"
        multi_root_graph["cards"].insert(2, second_impl)
        multi_root_graph["cards"][-2]["parents"].append("impl-2")
        multi_root_graph["cards"][-1]["parents"].append("impl-2")
        authoring_executor.output = multi_root_graph
        multi_root_request = {
            **projection_request,
            "artifact_id": "artifact-canonical-projected-multi-root",
            "authoring_artifact_id": "artifact-canonical-authored-multi-root",
            "run_id": "run-canonical-multi-root-authoring",
        }
        with patch(
            "hermes_langchain.controller.kanban_graph_materialization.validate_graph",
            return_value=[],
        ):
            multi_root = projector.project(multi_root_request)
        multi_root_payload = self.store.get(multi_root["artifact_id"]).payload
        self.assertEqual("impl-1", multi_root_payload["activation_candidate_key"])
        self.assertEqual(
            {"impl-1", "impl-2"},
            {
                link["child_key"]
                for link in multi_root_payload["links"]
                if link["parent_key"] == "source"
            },
        )
        graph_port = FakeGraphPort(source)
        receipt = AcceptedTaskGraphMaterializer(
            artifact_store=self.store,
            kanban_port=graph_port,
        ).materialize(
            {
                "schema": "controller-kanban-graph-materialization-request-v1",
                "artifact_id": projected["artifact_id"],
                "artifact_sha256": projected["artifact_sha256"],
                "board": milestone["board"],
                "issue_number": 151,
                "generation": 3,
                "workspace_path": self.workspace.as_posix(),
                "expected_snapshot_fingerprint": fingerprint_graph_snapshot(
                    graph_port.snapshot(board=milestone["board"])
                ),
            }
        )
        self.assertTrue(receipt["verified"])

    def test_unmocked_canonical_v6_graph_projects_and_materializes(self) -> None:
        fixture = (
            Path(__file__).parents[2]
            / "skills"
            / "kwc-build-task-graph"
            / "tests"
            / "fixtures"
            / "valid-graph-draft.json"
        )
        authoring_graph = json.loads(fixture.read_text(encoding="utf-8"))

        def replace_workspace(value):
            if isinstance(value, dict):
                return {key: replace_workspace(item) for key, item in value.items()}
            if isinstance(value, list):
                return [replace_workspace(item) for item in value]
            return self.workspace.as_posix() if value == "scratch" else value

        authoring_graph = replace_workspace(authoring_graph)
        self.assertEqual([], validate_graph(authoring_graph, phase="draft"))
        frozen_digest = authoring_graph["triage_digest"]
        triage_body = {
            "schema": "triage-v7",
            "issue": authoring_graph["issue"],
            "baseline": {"planning_sha": authoring_graph["planning_baseline_sha"]},
            "frozen_digest": frozen_digest,
        }
        body_text = json.dumps(triage_body, sort_keys=True) + "\n"
        artifact_sha256 = hashlib.sha256(body_text.encode()).hexdigest()
        triage_payload = {
            "stage": "materialize",
            "artifact": {"path": ".temp/triage-v7.json", "sha256": artifact_sha256},
            "body": triage_body,
            "body_text": body_text,
            "frozen_digest": frozen_digest,
            "read_back": {
                "valid": True,
                "verified": True,
                "status": "triage",
                "title": "G1-Issue138-Triage",
                "frozen_digest": frozen_digest,
                "frozen_body_file": ".temp/triage-v7.json",
            },
            "milestone": {
                "schema": "controller-triage-milestone-v1",
                "board": "disposable-controller-graph",
                "issue_number": 138,
                "generation": 1,
                "workspace_path": self.workspace.as_posix(),
                "title": "G1-Issue138-Triage",
                "assignee": "project-manager",
                "status": "triage",
                "body_text": body_text,
                "frozen_digest": frozen_digest,
            },
        }
        triage_record = ArtifactRecord(
            artifact_id="artifact-unmocked-triage",
            run_id="run-unmocked-triage",
            step_id="triage_materialize",
            attempt=1,
            executor_kind="deterministic",
            input_sha256=canonical_json_sha256({"fixture": "triage"}),
            output_sha256=canonical_json_sha256(triage_payload),
            timeout_seconds=30,
            validation_result="accepted",
            executor_identity="controller-triage-materializer-v1",
            payload=triage_payload,
        )
        self.store.put(triage_record, input_value={"fixture": "triage"})
        source = {
            "id": "t_unmocked_triage",
            "title": "G1-Issue138-Triage",
            "body": body_text,
            "assignee": "project-manager",
            "status": "triage",
            "workspace_kind": "dir",
            "workspace_path": self.workspace.as_posix(),
            "skills": [],
            "idempotency_key": accepted_triage_idempotency_key(
                board="disposable-controller-graph",
                issue_number=138,
                generation=1,
                artifact_sha256=triage_record.output_sha256,
            ),
        }

        class AuthoringExecutor:
            def invoke(_, invocation):
                return type(
                    "Result",
                    (),
                    {"agent_id": "agent-unmocked-author", "output": authoring_graph},
                )()

        projected = AcceptedTaskGraphProjector(
            triage_artifact_store=self.store,
            authoring_artifact_store=self.store,
            graph_artifact_store=self.store,
            agent_executor=AuthoringExecutor(),
        ).project(
            {
                "schema": "controller-task-graph-projection-request-v1",
                "artifact_id": "artifact-unmocked-projected-graph",
                "authoring_artifact_id": "artifact-unmocked-authored-graph",
                "run_id": "run-unmocked-graph-authoring",
                "accepted_triage_artifact_id": triage_record.artifact_id,
                "accepted_triage_artifact_sha256": triage_record.output_sha256,
                "source_task_id": source["id"],
                "board": "disposable-controller-graph",
                "issue_number": 138,
                "generation": 1,
                "workspace_path": self.workspace.as_posix(),
            }
        )
        graph_port = FakeGraphPort(source)
        receipt = AcceptedTaskGraphMaterializer(
            artifact_store=self.store,
            kanban_port=graph_port,
        ).materialize(
            {
                "schema": "controller-kanban-graph-materialization-request-v1",
                "artifact_id": projected["artifact_id"],
                "artifact_sha256": projected["artifact_sha256"],
                "board": "disposable-controller-graph",
                "issue_number": 138,
                "generation": 1,
                "workspace_path": self.workspace.as_posix(),
                "expected_snapshot_fingerprint": fingerprint_graph_snapshot(
                    graph_port.snapshot(board="disposable-controller-graph")
                ),
            }
        )
        self.assertTrue(receipt["verified"])
        self.assertEqual(4, len(graph_port.links))

    def test_native_port_materializes_and_replays_disposable_graph(self) -> None:
        board = f"m12-disposable-{uuid.uuid4().hex[:12]}"
        subprocess.run(
            ["hermes", "kanban", "boards", "create", board],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        try:
            source_body = '{"schema":"triage-v7","phase":"frozen"}'
            source_idempotency_key = accepted_triage_idempotency_key(
                board=board,
                issue_number=151,
                generation=4,
                artifact_sha256="b" * 64,
            )
            created = subprocess.run(
                [
                    "hermes", "kanban", "--board", board, "create",
                    "G4-Issue151-Triage", "--body", source_body,
                    "--assignee", "project-manager",
                    "--workspace", f"dir:{self.workspace.as_posix()}",
                    "--triage", "--idempotency-key", source_idempotency_key,
                    "--json",
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            source = json.loads(created.stdout)
            impl_body = json.dumps({"schema": "backend-implementation-card-v6"})
            review_body = json.dumps({"schema": "aggregate-review-card-v5"})
            summary_body = json.dumps({"schema": "issue-summary-card-v3"})
            payload = {
                "schema": "controller-task-graph-v1",
                "board": board,
                "issue_number": 151,
                "generation": 4,
                "workspace_path": source["workspace_path"],
                "source_task": {
                    "task_id": source["id"],
                    "title": source["title"],
                    "body_sha256": hashlib.sha256(source["body"].encode()).hexdigest(),
                    "status": "triage",
                    "assignee": "project-manager",
                    "workspace_kind": "dir",
                    "workspace_path": source["workspace_path"],
                    "skills": [],
                    "idempotency_key": source_idempotency_key,
                    "accepted_artifact_id": "artifact-native-triage",
                    "accepted_artifact_sha256": "b" * 64,
                },
                "activation_candidate_key": "impl-1",
                "cards": [
                    self.card("impl-1", "implementation", "G4-Issue151-Impl1", impl_body, "coder"),
                    self.card("review", "review", "G4-Issue151-Review", review_body, "reviewer"),
                    self.card("summary", "summary", "G4-Issue151-Summary", summary_body, "project-manager"),
                ],
                "links": [
                    {"parent_key": "source", "child_key": "impl-1"},
                    {"parent_key": "impl-1", "child_key": "review"},
                    {"parent_key": "impl-1", "child_key": "summary"},
                    {"parent_key": "review", "child_key": "summary"},
                ],
            }
            record = self.record_for("artifact-native-task-graph", payload)
            store_path = self.root / "native-task-graph-artifacts.sqlite3"
            native_store = SQLiteArtifactStore.open(store_path)
            try:
                native_store.put(record, input_value={"accepted": record.artifact_id})
            finally:
                native_store.close()
            request = {
                "schema": "controller-kanban-graph-materialization-request-v1",
                "artifact_id": record.artifact_id,
                "artifact_sha256": record.output_sha256,
                "board": board,
                "issue_number": 151,
                "generation": 4,
                "workspace_path": source["workspace_path"],
                "expected_snapshot_fingerprint": "0" * 64,
            }
            request_file = self.root / "native-task-graph-request.json"
            request_file.write_text(json.dumps(request), encoding="utf-8")
            hermes_executable = shutil.which("hermes")
            self.assertIsNotNone(hermes_executable)
            runtime_python = Path(str(hermes_executable)).resolve().parent / (
                "python.exe" if sys.platform == "win32" else "python"
            )
            completed = subprocess.run(
                [
                    str(runtime_python),
                    str(_ROOT / "tests" / "controller" / "workers" / "native_graph_materialization.py"),
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

            self.assertTrue(result["first"]["verified"])
            self.assertFalse(result["first"]["idempotent_replay"])
            self.assertTrue(result["replay"]["idempotent_replay"])
            self.assertEqual(result["first"]["task_ids"], result["replay"]["task_ids"])
            self.assertEqual(3, len(result["graph"]["tasks"]))
            self.assertEqual(4, len(result["graph"]["links"]))
            self.assertEqual({"todo"}, {task["status"] for task in result["graph"]["tasks"]})
            created_ids = set(result["first"]["task_ids"].values())
            self.assertTrue(
                created_ids.isdisjoint(
                    task["id"] for task in result["rolled_back"]["tasks"]
                )
            )
            self.assertFalse(
                any(
                    link["parent_id"] in created_ids or link["child_id"] in created_ids
                    for link in result["rolled_back"]["links"]
                )
            )
            failure_env = dict(os.environ)
            failure_env["HERMES_LANGCHAIN_TEST_POST_COMMIT_FAILURE"] = "1"
            failed = subprocess.run(
                [
                    str(runtime_python),
                    str(_ROOT / "tests" / "controller" / "workers" / "native_graph_materialization.py"),
                    str(_ROOT),
                    str(store_path),
                    str(request_file),
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=failure_env,
            )
            self.assertEqual(0, failed.returncode, failed.stderr)
            failure_result = json.loads(failed.stdout)
            self.assertEqual("NATIVE_CREATE_COMMITTED", failure_result["failure_code"])
            self.assertEqual(failure_result["baseline"], failure_result["restored"])
            unrelated = next(
                task
                for task in failure_result["restored"]["tasks"]
                if task["id"] == failure_result["unrelated_task_id"]
            )
            self.assertEqual("todo", unrelated["status"])
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
