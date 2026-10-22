"""Plugin-boundary acceptance tests for the deterministic controller service."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def load_plugin(name: str):
    if "plugins.plugin_storage" not in sys.modules:
        plugins = types.ModuleType("plugins")
        plugins.__path__ = []
        plugin_storage = types.ModuleType("plugins.plugin_storage")

        def plugin_private_data_dir(plugin_name: str) -> Path:
            root = (
                Path(os.environ["HERMES_HOME"])
                / "private"
                / "plugin-data"
                / plugin_name
            )
            root.mkdir(parents=True, exist_ok=True)
            return root

        plugin_storage.plugin_private_data_dir = plugin_private_data_dir
        plugins.plugin_storage = plugin_storage
        sys.modules["plugins"] = plugins
        sys.modules["plugins.plugin_storage"] = plugin_storage
    spec = importlib.util.spec_from_file_location(
        name,
        PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("plugin entrypoint could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeLifecycle:
    def __init__(self, outputs: list[dict[str, str]]) -> None:
        self.outputs = list(outputs)
        self.requests: list[dict[str, Any]] = []
        self.results: dict[str, dict[str, str]] = {}

    def launch(self, request: Mapping[str, Any]) -> object:
        copied = dict(request)
        self.requests.append(copied)
        if "output_schema" in copied:
            raise ValueError("Unknown request fields: output_schema.")
        if not self.outputs:
            raise AssertionError("unexpected lifecycle launch")
        subagent_id = f"probe-agent-{len(self.requests)}"
        self.results[subagent_id] = self.outputs.pop(0)
        return SimpleNamespace(subagent_id=subagent_id)

    @staticmethod
    def wait(handle: object, *, timeout_seconds: float | None = None) -> object:
        del handle, timeout_seconds
        return SimpleNamespace(
            state="SUCCEEDED",
            completed=True,
            timed_out=False,
            diagnostic=None,
        )

    def result(self, handle: object) -> object:
        subagent_id = str(getattr(handle, "subagent_id"))
        return SimpleNamespace(
            terminal_state="SUCCEEDED",
            ready=True,
            summary=json.dumps(self.results[subagent_id], ensure_ascii=False),
            structured_payload=None,
            error_classification=None,
            error_message=None,
        )


class FakeContext:
    def __init__(self, lifecycle: FakeLifecycle) -> None:
        self.subagent_lifecycle = lifecycle
        self.tools: dict[str, dict[str, Any]] = {}

    @staticmethod
    def register_hook(_name: str, _handler: Any) -> None:
        return None

    def register_tool(
        self,
        name: str,
        group: str,
        schema: dict[str, Any],
        handler: Any,
        **metadata: Any,
    ) -> None:
        self.tools[name] = {
            "group": group,
            "schema": schema,
            "handler": handler,
            "metadata": metadata,
        }

    @staticmethod
    def register_skill(
        _name: str,
        _path: Path,
        description: str = "",
        frontmatter: dict[str, Any] | None = None,
    ) -> None:
        del description, frontmatter


class FakeNativeMaterializationPort:
    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}

    def snapshot(self, *, board: str) -> dict[str, Any]:
        return {"board": board, "tasks": list(self.tasks.values())}

    def create_triage(
        self,
        *,
        board: str,
        card: dict[str, Any],
        expected_snapshot_fingerprint: str,
    ) -> dict[str, Any]:
        del board, expected_snapshot_fingerprint
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
        task_id = "t_public_materialization"
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

    def read_task(self, *, board: str, task_id: str) -> dict[str, Any]:
        del board
        return dict(self.tasks[task_id])

    def archive_task(self, *, board: str, task_id: str) -> None:
        del board
        self.tasks[task_id]["status"] = "archived"


class FakeNativeActivationPort:
    def __init__(self, task: dict[str, Any]) -> None:
        self.task = dict(task)
        self.promotions = 0
        self.activation_keys: set[str] = set()

    def snapshot(self, *, board: str) -> dict[str, Any]:
        return {"board": board, "tasks": [dict(self.task)], "links": []}

    def promote_task(
        self,
        *,
        board: str,
        task_id: str,
        reason: str,
        expected_snapshot_fingerprint: str,
        expected_task: dict[str, Any],
        activation_key: str,
        artifact_id: str,
        artifact_sha256: str,
    ) -> dict[str, Any]:
        del board, reason, expected_snapshot_fingerprint, expected_task
        del artifact_id, artifact_sha256
        if activation_key in self.activation_keys:
            if self.task["status"] in {"ready", "running", "review", "done"}:
                return {
                    "task_id": task_id,
                    "previous_status": self.task["status"],
                    "current_status": self.task["status"],
                    "promoted": False,
                }
            raise RuntimeError("conflicting replay")
        if self.task["status"] in {"ready", "running", "review", "done"}:
            raise RuntimeError("conflicting replay")
        self.promotions += 1
        previous_status = self.task["status"]
        self.task["status"] = "ready"
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

    def read_task(self, *, board: str, task_id: str) -> dict[str, Any]:
        del board
        if task_id != self.task["id"]:
            raise KeyError(task_id)
        return dict(self.task)


class ControllerPluginIntegrationTests(unittest.TestCase):
    def _registered(
        self,
        home: Path,
        *,
        outputs: list[dict[str, str]] | None = None,
        module_name: str = "_hermes_langchain_controller_plugin_test",
    ) -> tuple[FakeLifecycle, dict[str, Any]]:
        lifecycle = FakeLifecycle(outputs or [])
        context = FakeContext(lifecycle)
        with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
            plugin = load_plugin(module_name)
            plugin.register(context)
        return lifecycle, context.tools["hermes_langchain_controller"]

    def test_registers_closed_controller_tool_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, registration = self._registered(Path(directory))

        self.assertEqual(registration["group"], "hermes-langchain")
        parameters = registration["schema"]["parameters"]
        self.assertFalse(parameters["additionalProperties"])
        self.assertEqual(
            set(parameters["properties"]),
            {"action", "workflow_id", "run_id", "input"},
        )
        self.assertEqual(parameters["required"], ["action", "workflow_id", "run_id"])
        self.assertEqual(
            parameters["properties"]["action"]["enum"],
            ["start", "resume", "status"],
        )
        self.assertEqual(
            parameters["properties"]["workflow_id"]["enum"],
            ["controller-probe-v1"],
        )
        self.assertEqual(
            parameters["properties"]["input"]["properties"]["request"]["maxLength"],
            8000,
        )
        self.assertNotIn("goal", parameters["properties"])
        self.assertNotIn("allowed_toolsets", parameters["properties"])
        self.assertNotIn("output_schema", parameters["properties"])

    def test_registers_closed_materialization_tool_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lifecycle = FakeLifecycle([])
            context = FakeContext(lifecycle)
            with patch.dict(os.environ, {"HERMES_HOME": directory}, clear=False):
                plugin = load_plugin("_hermes_langchain_materialization_contract_test")
                plugin.register(context)

        registration = context.tools["hermes_langchain_materialize"]
        self.assertEqual(registration["group"], "hermes-langchain")
        parameters = registration["schema"]["parameters"]
        self.assertFalse(parameters["additionalProperties"])
        self.assertEqual(
            set(parameters["properties"]),
            {
                "workflow_id",
                "artifact_id",
                "artifact_sha256",
                "board",
                "issue_number",
                "generation",
                "workspace_path",
                "expected_snapshot_fingerprint",
            },
        )
        self.assertEqual(
            parameters["required"],
            [
                "workflow_id",
                "artifact_id",
                "artifact_sha256",
                "board",
                "issue_number",
                "generation",
                "workspace_path",
                "expected_snapshot_fingerprint",
            ],
        )
        self.assertEqual(
            parameters["properties"]["workflow_id"]["enum"],
            ["triage-parity-v1"],
        )

    def test_plugin_materializes_an_actual_accepted_triage_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            context = FakeContext(FakeLifecycle([]))
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
                plugin = load_plugin("_hermes_langchain_materialization_e2e_test")
                plugin.register(context)
            handler = context.tools["hermes_langchain_materialize"]["handler"]
            package_root = handler.__module__.split(".plugin", 1)[0]
            service = importlib.import_module(f"{package_root}.controller.service")
            artifacts = importlib.import_module(
                f"{package_root}.controller.artifact_store"
            )
            materialization = importlib.import_module(
                f"{package_root}.controller.kanban_materialization"
            )
            body_text = json.dumps(
                {
                    "schema": "triage-v7",
                    "issue": {"number": 42},
                    "frozen_digest": "f" * 64,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ) + "\n"
            frozen_digest = "f" * 64
            milestone = {
                "schema": "controller-triage-milestone-v1",
                "board": "materialization-e2e",
                "issue_number": 42,
                "generation": 2,
                "workspace_path": home.resolve().as_posix(),
                "title": "G2-Issue42-Triage",
                "assignee": "project-manager",
                "status": "triage",
                "body_text": body_text,
                "frozen_digest": frozen_digest,
            }
            frozen_body_file = ".temp/controller-triage/frozen-triage.json"
            accepted_payload = {
                "stage": "materialize",
                "artifact": {
                    "path": frozen_body_file,
                    "sha256": hashlib.sha256(body_text.encode("utf-8")).hexdigest(),
                },
                "body": json.loads(body_text),
                "body_text": body_text,
                "frozen_digest": frozen_digest,
                "read_back": {
                    "valid": True,
                    "verified": True,
                    "task_id": "t_triage_source",
                    "title": milestone["title"],
                    "status": "triage",
                    "frozen_digest": frozen_digest,
                    "frozen_body_file": frozen_body_file,
                },
                "milestone": milestone,
            }
            record = artifacts.ArtifactRecord(
                artifact_id="artifact-public-materialization",
                run_id="run-public-materialization",
                step_id="triage_materialize",
                attempt=1,
                executor_kind="materialize",
                input_sha256=artifacts.canonical_json_sha256({"accepted": "triage"}),
                output_sha256=artifacts.canonical_json_sha256(accepted_payload),
                timeout_seconds=300,
                validation_result="accepted",
                executor_identity="controller-triage-materialize",
                payload=accepted_payload,
            )
            store = artifacts.SQLiteArtifactStore.open(
                service.controller_artifact_store_path(home, "triage-parity-v1")
            )
            try:
                store.put(record, input_value={"accepted": "triage"})
            finally:
                store.close()
            port = FakeNativeMaterializationPort()
            arguments = {
                "workflow_id": "triage-parity-v1",
                "artifact_id": record.artifact_id,
                "artifact_sha256": record.output_sha256,
                "board": milestone["board"],
                "issue_number": milestone["issue_number"],
                "generation": milestone["generation"],
                "workspace_path": milestone["workspace_path"],
                "expected_snapshot_fingerprint": materialization.fingerprint_native_snapshot(
                    port.snapshot(board=milestone["board"])
                ),
            }

            with patch.object(
                service,
                "HermesNativeKanbanMaterializationPort",
                return_value=port,
            ), patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
                first = json.loads(handler(arguments))
                replay = json.loads(handler(arguments))

            self.assertTrue(first["ok"], first)
            self.assertTrue(first["verified"])
            self.assertFalse(first["idempotent_replay"])
            self.assertTrue(replay["idempotent_replay"])
            self.assertEqual(first["task_id"], replay["task_id"])
            self.assertEqual(1, len(port.tasks))

    def test_public_activation_tool_uses_closed_sanitized_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            context = FakeContext(FakeLifecycle([]))
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
                plugin = load_plugin("_hermes_langchain_activation_public_test")
                plugin.register(context)

            registration = context.tools["hermes_langchain_activate"]
            parameters = registration["schema"]["parameters"]
            self.assertFalse(parameters["additionalProperties"])
            self.assertEqual(
                set(parameters["properties"]),
                {
                    "workflow_id",
                    "artifact_id",
                    "artifact_sha256",
                    "board",
                    "issue_number",
                    "generation",
                    "workspace_path",
                    "task_id",
                    "expected_snapshot_fingerprint",
                },
            )
            self.assertEqual(parameters["properties"]["artifact_id"]["maxLength"], 128)
            self.assertEqual(parameters["properties"]["board"]["maxLength"], 64)
            self.assertEqual(parameters["properties"]["task_id"]["maxLength"], 128)
            self.assertEqual(parameters["properties"]["workspace_path"]["maxLength"], 4096)
            handler = registration["handler"]
            package_root = handler.__module__.split(".plugin", 1)[0]
            service = importlib.import_module(f"{package_root}.controller.service")
            artifacts = importlib.import_module(
                f"{package_root}.controller.artifact_store"
            )
            activation = importlib.import_module(
                f"{package_root}.controller.kanban_activation"
            )
            body = '{"schema":"backend-implementation-card-v6"}\n'
            task = {
                "id": "t_public_activation",
                "title": "G2-Issue42-Impl-1",
                "body": body,
                "assignee": "coder",
                "status": "todo",
                "workspace_kind": "dir",
                "workspace_path": home.resolve().as_posix(),
                "skills": ["kwc-impl-authoring-runtime"],
                "idempotency_key": "controller-public-activation",
            }
            payload = {
                "schema": "controller-task-activation-v1",
                "board": "public-controller-activation",
                "issue_number": 42,
                "generation": 2,
                "workspace_path": task["workspace_path"],
                "reason_code": "accepted_graph_root",
                "task": {
                    "task_id": task["id"],
                    "title": task["title"],
                    "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                    "assignee": task["assignee"],
                    "status": task["status"],
                    "workspace_kind": task["workspace_kind"],
                    "workspace_path": task["workspace_path"],
                    "skills": task["skills"],
                    "idempotency_key": task["idempotency_key"],
                },
            }
            record = artifacts.ArtifactRecord(
                artifact_id="artifact-public-activation",
                run_id="run-public-activation",
                step_id="activate_impl_root",
                attempt=1,
                executor_kind="deterministic",
                input_sha256=artifacts.canonical_json_sha256(
                    {"accepted": "graph-root"}
                ),
                output_sha256=artifacts.canonical_json_sha256(payload),
                timeout_seconds=30,
                validation_result="accepted",
                executor_identity="controller-task-activation",
                payload=payload,
            )
            store = artifacts.SQLiteArtifactStore.open(
                service.controller_artifact_store_path(
                    home, "canonical-task-activation-v1"
                )
            )
            try:
                store.put(record, input_value={"accepted": "graph-root"})
            finally:
                store.close()
            port = FakeNativeActivationPort(task)
            arguments = {
                "workflow_id": "canonical-task-activation-v1",
                "artifact_id": record.artifact_id,
                "artifact_sha256": record.output_sha256,
                "board": payload["board"],
                "issue_number": payload["issue_number"],
                "generation": payload["generation"],
                "workspace_path": payload["workspace_path"],
                "task_id": task["id"],
                "expected_snapshot_fingerprint": activation.fingerprint_activation_snapshot(
                    port.snapshot(board=payload["board"])
                ),
            }

            with patch.object(
                service,
                "HermesNativeKanbanActivationPort",
                return_value=port,
            ), patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
                result = json.loads(handler(arguments))

            self.assertTrue(result["ok"], result)
            self.assertTrue(result["verified"])
            self.assertEqual(result["current_status"], "ready")
            self.assertEqual(1, port.promotions)

            with patch.object(
                service,
                "execute_activation_action",
                side_effect=RuntimeError("PRIVATE_ACTIVATION_DIAGNOSTIC"),
            ):
                failure = json.loads(handler(arguments))
            self.assertFalse(failure["ok"])
            self.assertEqual(failure["reason_code"], "INTERNAL_FAILURE")
            self.assertNotIn("error", failure)
            self.assertNotIn("RuntimeError", json.dumps(failure))
            self.assertNotIn("PRIVATE_ACTIVATION_DIAGNOSTIC", json.dumps(failure))

            with patch.object(
                service,
                "execute_activation_action",
                side_effect=activation.KanbanActivationError(
                    "SNAPSHOT_CHANGED",
                    "PRIVATE_SNAPSHOT_DIAGNOSTIC",
                ),
            ):
                rejected = json.loads(handler(arguments))
            self.assertEqual(rejected["reason_code"], "SNAPSHOT_CHANGED")
            self.assertNotIn("PRIVATE_SNAPSHOT_DIAGNOSTIC", json.dumps(rejected))

    def test_public_graph_materialization_tool_is_closed_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            context = FakeContext(FakeLifecycle([]))
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
                plugin = load_plugin("_hermes_langchain_graph_materialization_public_test")
                plugin.register(context)

            registration = context.tools["hermes_langchain_materialize_graph"]
            parameters = registration["schema"]["parameters"]
            self.assertFalse(parameters["additionalProperties"])
            self.assertEqual(
                set(parameters["properties"]),
                {
                    "workflow_id",
                    "artifact_id",
                    "artifact_sha256",
                    "board",
                    "issue_number",
                    "generation",
                    "workspace_path",
                    "expected_snapshot_fingerprint",
                },
            )
            self.assertEqual(
                parameters["properties"]["workflow_id"]["enum"],
                ["canonical-task-graph-materialization-v1"],
            )
            handler = registration["handler"]
            package_root = handler.__module__.split(".plugin", 1)[0]
            service = importlib.import_module(f"{package_root}.controller.service")
            artifacts = importlib.import_module(f"{package_root}.controller.artifact_store")
            graph_module = importlib.import_module(
                f"{package_root}.controller.kanban_graph_materialization"
            )
            graph_tests = importlib.import_module(
                "tests.test_controller_kanban_graph_materialization"
            )
            frozen_digest = "f" * 64
            source_body = json.dumps(
                {
                    "schema": "triage-v7",
                    "issue": {
                        "number": 42,
                        "title": "테스트",
                        "url": "https://github.com/example/repository/issues/42",
                    },
                    "baseline": {"planning_sha": "e" * 40},
                    "frozen_digest": frozen_digest,
                },
                sort_keys=True,
                separators=(",", ":"),
            ) + "\n"
            frozen_body_file = ".temp/controller-triage/frozen-triage.json"
            triage_milestone = {
                "schema": "controller-triage-milestone-v1",
                "board": "public-controller-graph",
                "issue_number": 42,
                "generation": 5,
                "workspace_path": home.as_posix(),
                "title": "G5-Issue42-Triage",
                "assignee": "project-manager",
                "status": "triage",
                "body_text": source_body,
                "frozen_digest": frozen_digest,
            }
            accepted_triage = {
                "stage": "materialize",
                "artifact": {
                    "path": frozen_body_file,
                    "sha256": hashlib.sha256(source_body.encode()).hexdigest(),
                },
                "body": json.loads(source_body),
                "body_text": source_body,
                "frozen_digest": frozen_digest,
                "read_back": {
                    "valid": True,
                    "verified": True,
                    "task_id": "t_public_graph_source",
                    "title": triage_milestone["title"],
                    "status": "triage",
                    "frozen_digest": frozen_digest,
                    "frozen_body_file": frozen_body_file,
                },
                "milestone": triage_milestone,
            }
            triage_record = artifacts.ArtifactRecord(
                artifact_id="artifact-public-graph-triage",
                run_id="run-public-graph-triage",
                step_id="triage_materialize",
                attempt=1,
                executor_kind="materialize",
                input_sha256=artifacts.canonical_json_sha256({"accepted": "triage"}),
                output_sha256=artifacts.canonical_json_sha256(accepted_triage),
                timeout_seconds=300,
                validation_result="accepted",
                executor_identity="controller-triage-materialize",
                payload=accepted_triage,
            )
            triage_store = artifacts.SQLiteArtifactStore.open(
                service.controller_artifact_store_path(home, "triage-parity-v1")
            )
            try:
                triage_store.put(triage_record, input_value={"accepted": "triage"})
            finally:
                triage_store.close()
            source = {
                "id": "t_public_graph_source",
                "title": "G5-Issue42-Triage",
                "body": source_body,
                "assignee": "project-manager",
                "status": "triage",
                "workspace_kind": "dir",
                "workspace_path": home.as_posix(),
                "skills": [],
                "idempotency_key": graph_module.accepted_triage_idempotency_key(
                    board="public-controller-graph",
                    issue_number=42,
                    generation=5,
                    artifact_sha256=triage_record.output_sha256,
                ),
            }

            def card(key, role, title, schema, assignee):
                body = json.dumps({"schema": schema}) + "\n"
                return {
                    "key": key,
                    "role": role,
                    "title": title,
                    "body_text": body,
                    "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                    "assignee": assignee,
                    "skills": [f"kwc-{role}-authoring-runtime"],
                }

            payload = {
                "schema": "controller-task-graph-v1",
                "board": "public-controller-graph",
                "issue_number": 42,
                "generation": 5,
                "workspace_path": home.as_posix(),
                "source_task": {
                    "task_id": source["id"],
                    "title": source["title"],
                    "body_sha256": hashlib.sha256(source_body.encode()).hexdigest(),
                    "status": "triage",
                    "assignee": "project-manager",
                    "workspace_kind": "dir",
                    "workspace_path": home.as_posix(),
                    "skills": [],
                    "idempotency_key": source["idempotency_key"],
                    "accepted_artifact_id": triage_record.artifact_id,
                    "accepted_artifact_sha256": triage_record.output_sha256,
                },
                "activation_candidate_key": "impl-1",
                "cards": [
                    card("impl-1", "implementation", "G5-Issue42-Impl1", "backend-implementation-card-v6", "coder"),
                    card("review", "review", "G5-Issue42-Review", "aggregate-review-card-v5", "reviewer"),
                    card("summary", "summary", "G5-Issue42-Summary", "issue-summary-card-v3", "project-manager"),
                ],
                "links": [
                    {"parent_key": "source", "child_key": "impl-1"},
                    {"parent_key": "impl-1", "child_key": "review"},
                    {"parent_key": "impl-1", "child_key": "summary"},
                    {"parent_key": "review", "child_key": "summary"},
                ],
            }
            authoring_graph = {
                "schema": "build-task-graph-v6",
                "issue": {
                    "number": 42,
                    "url": "https://github.com/example/repository/issues/42",
                },
                "generation": 5,
                "workspace": home.as_posix(),
                "triage_digest": frozen_digest,
                "planning_baseline_sha": "e" * 40,
                "ready_candidate": "impl-1",
                "cards": [
                    {
                        "key": "triage",
                        "title": source["title"],
                        "card_type": "triage",
                        "assignee": "project-manager",
                        "skills": [],
                        "status": "triage",
                        "parents": [],
                        "body": None,
                    },
                    *[
                        {
                            "key": card_value["key"],
                            "title": card_value["title"],
                            "card_type": card_value["role"],
                            "assignee": card_value["assignee"],
                            "skills": card_value["skills"],
                            "status": "todo",
                            "parents": [
                                "triage" if link["parent_key"] == "source" else link["parent_key"]
                                for link in payload["links"]
                                if link["child_key"] == card_value["key"]
                            ],
                            "body": json.loads(card_value["body_text"]),
                        }
                        for card_value in payload["cards"]
                    ],
                ],
            }
            context.subagent_lifecycle.outputs.append(authoring_graph)
            projection_request = {
                "workflow_id": "canonical-task-graph-projection-v1",
                "artifact_id": "artifact-public-task-graph",
                "authoring_artifact_id": "artifact-public-authored-graph",
                "run_id": "run-public-graph-authoring",
                "accepted_triage_artifact_id": triage_record.artifact_id,
                "accepted_triage_artifact_sha256": triage_record.output_sha256,
                "source_task_id": source["id"],
                "board": payload["board"],
                "issue_number": 42,
                "generation": 5,
                "workspace_path": home.as_posix(),
            }
            projection = context.tools["hermes_langchain_project_graph"]
            projection_parameters = projection["schema"]["parameters"]
            self.assertFalse(projection_parameters["additionalProperties"])
            with patch.object(graph_module, "validate_graph", return_value=[]), patch.dict(
                os.environ, {"HERMES_HOME": str(home)}, clear=False
            ):
                projected = json.loads(
                    projection["handler"](projection_request)
                )
            self.assertTrue(projected["ok"], projected)
            with patch.object(graph_module, "validate_graph", return_value=[]), patch.dict(
                os.environ, {"HERMES_HOME": str(home)}, clear=False
            ):
                replayed_projection = json.loads(
                    projection["handler"](projection_request)
                )
                rejected_projection = json.loads(
                    projection["handler"](
                        {
                            **projection_request,
                            "artifact_id": "artifact-public-task-graph-tampered",
                            "accepted_triage_artifact_sha256": "0" * 64,
                        }
                    )
                )
                missing_provenance = json.loads(
                    projection["handler"](
                        {
                            **projection_request,
                            "artifact_id": "artifact-public-task-graph-missing",
                            "accepted_triage_artifact_id": "artifact-missing-triage",
                        }
                    )
                )
            self.assertEqual(projected, replayed_projection)
            self.assertEqual(
                "TRIAGE_PROVENANCE_INVALID",
                rejected_projection["reason_code"],
            )
            self.assertEqual(
                "TRIAGE_PROVENANCE_INVALID",
                missing_provenance["reason_code"],
            )
            port = graph_tests.FakeGraphPort(source)
            arguments = {
                "workflow_id": "canonical-task-graph-materialization-v1",
                "artifact_id": projected["artifact_id"],
                "artifact_sha256": projected["artifact_sha256"],
                "board": payload["board"],
                "issue_number": 42,
                "generation": 5,
                "workspace_path": home.as_posix(),
                "expected_snapshot_fingerprint": graph_module.fingerprint_graph_snapshot(
                    port.snapshot(board=payload["board"])
                ),
            }

            with patch.object(
                service,
                "HermesNativeKanbanGraphMaterializationPort",
                return_value=port,
            ), patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
                result = json.loads(handler(arguments))

            self.assertTrue(result["ok"], result)
            self.assertTrue(result["verified"])
            self.assertFalse(result["idempotent_replay"])
            self.assertEqual(3, len(result["task_ids"]))
            self.assertEqual(set(result["task_ids"]), set(result["activation_artifacts"]))
            self.assertTrue(
                all(
                    set(ref) == {"artifact_id", "artifact_sha256", "task_id"}
                    for ref in result["activation_artifacts"].values()
                )
            )

            with patch.object(
                service,
                "execute_graph_materialization_action",
                side_effect=RuntimeError("PRIVATE_GRAPH_DIAGNOSTIC"),
            ):
                failure = json.loads(handler(arguments))
            self.assertEqual("INTERNAL_FAILURE", failure["reason_code"])
            self.assertNotIn("PRIVATE_GRAPH_DIAGNOSTIC", json.dumps(failure))

            with patch.object(
                service,
                "execute_graph_materialization_action",
                side_effect=graph_module.KanbanGraphMaterializationError(
                    "SNAPSHOT_CHANGED", "PRIVATE_GRAPH_SNAPSHOT"
                ),
            ):
                rejected = json.loads(handler(arguments))
            self.assertEqual("SNAPSHOT_CHANGED", rejected["reason_code"])
            self.assertNotIn("PRIVATE_GRAPH_SNAPSHOT", json.dumps(rejected))

    def test_registers_side_effect_free_probe_toolset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lifecycle = FakeLifecycle([])
            context = FakeContext(lifecycle)
            with patch.dict(os.environ, {"HERMES_HOME": directory}, clear=False):
                plugin = load_plugin("_hermes_langchain_controller_probe_toolset_test")
                plugin.register(context)

        registration = context.tools["hermes_langchain_probe_guard"]
        self.assertEqual(registration["group"], "hermes-langchain-controller-probe")
        self.assertEqual(registration["schema"]["parameters"]["properties"], {})
        self.assertFalse(registration["schema"]["parameters"]["additionalProperties"])
        self.assertEqual(json.loads(registration["handler"]({})), {"ok": True})

    def test_unexpected_controller_failure_does_not_expose_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, registration = self._registered(
                Path(directory),
                module_name="_hermes_langchain_controller_error_test",
            )
            handler = registration["handler"]
            package_root = handler.__module__.split(".plugin", 1)[0]
            service = importlib.import_module(
                f"{package_root}.controller.service"
            )
            cases = (
                (RuntimeError("PRIVATE_RUNTIME_DIAGNOSTIC_661027"), "RuntimeError"),
                (ValueError("PRIVATE_VALUE_DIAGNOSTIC_918270"), "ValueError"),
            )
            for exception, type_name in cases:
                with self.subTest(type_name=type_name), patch.object(
                    service,
                    "execute_controller_action",
                    side_effect=exception,
                ):
                    result = json.loads(
                        handler(
                            {
                                "action": "status",
                                "workflow_id": "controller-probe-v1",
                                "run_id": "error-probe-1",
                            }
                        )
                    )
                    self.assertFalse(result["ok"])
                    self.assertEqual(
                        result["error"],
                        f"controller operation failed: {type_name}",
                    )
                    self.assertNotIn("PRIVATE_", json.dumps(result))

    def test_plugin_context_executes_and_reconstructs_checkpointed_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            lifecycle, registration = self._registered(
                home,
                outputs=[
                    {"requirement": "Ship deterministic orchestration."},
                    {"confirmation": "Requirement accepted."},
                ],
                module_name="_hermes_langchain_controller_checkpoint_test",
            )
            handler = registration["handler"]
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
                started = json.loads(
                    handler(
                        {
                            "action": "start",
                            "workflow_id": "controller-probe-v1",
                            "run_id": "live-probe-1",
                            "input": {"request": "Ship deterministic orchestration."},
                        }
                    )
                )
                duplicate = json.loads(
                    handler(
                        {
                            "action": "start",
                            "workflow_id": "controller-probe-v1",
                            "run_id": "live-probe-1",
                            "input": {"request": "Ship deterministic orchestration."},
                        }
                    )
                )
                snapshot = json.loads(
                    handler(
                        {
                            "action": "status",
                            "workflow_id": "controller-probe-v1",
                            "run_id": "live-probe-1",
                        }
                    )
                )
                completed = json.loads(
                    handler(
                        {
                            "action": "resume",
                            "workflow_id": "controller-probe-v1",
                            "run_id": "live-probe-1",
                        }
                    )
                )
                read_back = json.loads(
                    handler(
                        {
                            "action": "status",
                            "workflow_id": "controller-probe-v1",
                            "run_id": "live-probe-1",
                        }
                    )
                )

            self.assertTrue(started["ok"])
            self.assertEqual(started["status"], "awaiting_resume")
            self.assertNotIn("error", started)
            self.assertFalse(duplicate["ok"])
            self.assertIn("run already exists", duplicate["error"])
            self.assertEqual(snapshot["status"], "awaiting_resume")
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(read_back, completed)
            self.assertEqual(len(lifecycle.requests), 4)
            checkpoint = (
                home
                / "plugin-data"
                / "hermes-langchain"
                / "controller"
                / "v2"
                / "controller-probe-v1.sqlite3"
            )
            self.assertTrue(checkpoint.is_file())
            artifact_store = (
                home
                / "plugin-data"
                / "hermes-langchain"
                / "controller"
                / "v2"
                / "controller-probe-v1.artifacts.sqlite3"
            )
            self.assertTrue(artifact_store.is_file())

    def test_probe_projects_only_accepted_artifact_to_fresh_second_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            lifecycle, registration = self._registered(
                home,
                outputs=[
                    {"requirement": "Ship deterministic orchestration."},
                    {"confirmation": "Requirement accepted."},
                ],
                module_name="_hermes_langchain_controller_projection_test",
            )
            handler = registration["handler"]
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
                handler(
                    {
                        "action": "start",
                        "workflow_id": "controller-probe-v1",
                        "run_id": "projection-probe-1",
                        "input": {
                            "request": "Ship deterministic orchestration.",
                            "unrelated": "must not cross the boundary",
                        },
                    }
                )
                result = json.loads(
                    handler(
                        {
                            "action": "resume",
                            "workflow_id": "controller-probe-v1",
                            "run_id": "projection-probe-1",
                        }
                    )
                )

            accepted = [request for request in lifecycle.requests if "output_schema" not in request]
            schema_requests = [
                request for request in lifecycle.requests if "output_schema" in request
            ]
            self.assertEqual(len(accepted), 2)
            self.assertEqual(len(schema_requests), 2)
            first_context = json.loads(accepted[0]["context"])
            second_context = json.loads(accepted[1]["context"])
            self.assertEqual(
                first_context["input"],
                {"request": "Ship deterministic orchestration."},
            )
            self.assertEqual(
                second_context["input"],
                {"requirement": "Ship deterministic orchestration."},
            )
            self.assertEqual(
                accepted[0]["allowed_toolsets"],
                ("hermes-langchain-controller-probe",),
            )
            self.assertEqual(
                accepted[1]["allowed_toolsets"],
                ("hermes-langchain-controller-probe",),
            )
            self.assertEqual(
                schema_requests[0]["output_schema"]["properties"]["requirement"]["maxLength"],
                8000,
            )
            self.assertEqual(
                schema_requests[1]["output_schema"]["properties"]["confirmation"]["maxLength"],
                8000,
            )
            self.assertNotEqual(
                result["invocations"][0]["agent_id"],
                result["invocations"][1]["agent_id"],
            )

    def test_oversized_start_is_rejected_before_checkpoint_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            _, registration = self._registered(
                home,
                outputs=[{"requirement": "Accepted."}],
                module_name="_hermes_langchain_controller_oversized_input_test",
            )
            handler = registration["handler"]
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
                rejected = json.loads(
                    handler(
                        {
                            "action": "start",
                            "workflow_id": "controller-probe-v1",
                            "run_id": "bounded-probe-1",
                            "input": {"request": "x" * 8001},
                        }
                    )
                )
                accepted = json.loads(
                    handler(
                        {
                            "action": "start",
                            "workflow_id": "controller-probe-v1",
                            "run_id": "bounded-probe-1",
                            "input": {"request": "Ship it."},
                        }
                    )
                )

        self.assertFalse(rejected["ok"])
        self.assertIn("at most 8000 characters", rejected["error"])
        self.assertTrue(accepted["ok"])
        self.assertEqual(accepted["status"], "awaiting_resume")

    def test_oversized_agent_output_blocks_before_artifact_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            _, registration = self._registered(
                home,
                outputs=[{"requirement": "x" * 8001}],
                module_name="_hermes_langchain_controller_oversized_output_test",
            )
            handler = registration["handler"]
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
                blocked = json.loads(
                    handler(
                        {
                            "action": "start",
                            "workflow_id": "controller-probe-v1",
                            "run_id": "bounded-probe-2",
                            "input": {"request": "Ship it."},
                        }
                    )
                )
                read_back = json.loads(
                    handler(
                        {
                            "action": "status",
                            "workflow_id": "controller-probe-v1",
                            "run_id": "bounded-probe-2",
                        }
                    )
                )

        self.assertEqual(blocked["status"], "blocked")
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["artifacts"], [])
        self.assertEqual(read_back, blocked)

    def test_maximum_probe_strings_stay_within_hermes_context_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            lifecycle, registration = self._registered(
                home,
                outputs=[
                    {"requirement": "r" * 8000},
                    {"confirmation": "Accepted."},
                ],
                module_name="_hermes_langchain_controller_maximum_context_test",
            )
            handler = registration["handler"]
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
                handler(
                    {
                        "action": "start",
                        "workflow_id": "controller-probe-v1",
                        "run_id": "bounded-probe-3",
                        "input": {"request": "q" * 8000},
                    }
                )
                completed = json.loads(
                    handler(
                        {
                            "action": "resume",
                            "workflow_id": "controller-probe-v1",
                            "run_id": "bounded-probe-3",
                        }
                    )
                )

        launched = [request for request in lifecycle.requests if "output_schema" not in request]
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(len(launched), 2)
        self.assertTrue(all(len(request["context"]) <= 32_000 for request in launched))

    def test_unknown_resume_and_status_return_stable_domain_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            _, registration = self._registered(
                home,
                module_name="_hermes_langchain_controller_unknown_run_test",
            )
            handler = registration["handler"]
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
                for action in ("resume", "status"):
                    result = json.loads(
                        handler(
                            {
                                "action": action,
                                "workflow_id": "controller-probe-v1",
                                "run_id": "missing-probe-1",
                            }
                        )
                    )
                    self.assertFalse(result["ok"])
                    self.assertEqual(
                        result["error"],
                        "controller run not found: missing-probe-1",
                    )

    def test_live_smoke_receipt_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            _, registration = self._registered(
                home,
                outputs=[
                    {"requirement": "Ship deterministic orchestration."},
                    {"confirmation": "Requirement accepted."},
                ],
                module_name="_hermes_langchain_controller_receipt_test",
            )
            handler = registration["handler"]
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
                handler(
                    {
                        "action": "start",
                        "workflow_id": "controller-probe-v1",
                        "run_id": "receipt-probe-1",
                        "input": {"request": "Ship deterministic orchestration."},
                    }
                )
                receipt = json.loads(
                    handler(
                        {
                            "action": "resume",
                            "workflow_id": "controller-probe-v1",
                            "run_id": "receipt-probe-1",
                        }
                    )
                )

        self.assertEqual(receipt["workflow_id"], "controller-probe-v1")
        self.assertEqual(receipt["run_id"], "receipt-probe-1")
        self.assertEqual(receipt["status"], "completed")
        self.assertIsNone(receipt["next_step"])
        self.assertEqual(len(receipt["artifacts"]), 2)
        self.assertEqual(len(receipt["invocations"]), 2)
        self.assertEqual(
            [artifact["step_id"] for artifact in receipt["artifacts"]],
            ["extract_requirement", "confirm_requirement"],
        )
        self.assertNotIn("error", receipt)

    def test_public_dispatch_tool_is_closed_and_sanitizes_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            context = FakeContext(FakeLifecycle([]))
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
                plugin = load_plugin("_hermes_langchain_dispatch_public_test")
                plugin.register(context)

            registration = context.tools["hermes_langchain_dispatch"]
            parameters = registration["schema"]["parameters"]
            self.assertFalse(parameters["additionalProperties"])
            self.assertEqual(
                set(parameters["properties"]),
                {
                    "workflow_id",
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
                },
            )
            handler = registration["handler"]
            package_root = handler.__module__.split(".plugin", 1)[0]
            service = importlib.import_module(f"{package_root}.controller.service")
            dispatch = importlib.import_module(
                f"{package_root}.controller.kanban_dispatch"
            )
            arguments = {
                "workflow_id": "canonical-task-dispatch-v1",
                "artifact_id": "dispatch-artifact",
                "graph_artifact_id": "graph-artifact",
                "graph_artifact_sha256": "a" * 64,
                "activation_artifact_id": "activation-artifact",
                "activation_artifact_sha256": "b" * 64,
                "activation_key": "c" * 64,
                "board": "m13-test",
                "issue_number": 42,
                "generation": 4,
                "workspace_path": home.as_posix(),
                "task_id": "t-impl-1",
                "expected_snapshot_fingerprint": "d" * 64,
            }
            with patch.object(
                service,
                "execute_dispatch_action",
                return_value={"ok": True, "workflow_id": "canonical-task-dispatch-v1"},
            ):
                accepted = json.loads(handler(arguments))
            self.assertTrue(accepted["ok"])

            with patch.object(
                service,
                "execute_dispatch_action",
                side_effect=dispatch.KanbanDispatchError(
                    "SNAPSHOT_CHANGED", "PRIVATE_DISPATCH_DIAGNOSTIC"
                ),
            ):
                rejected = json.loads(handler(arguments))
            self.assertEqual("SNAPSHOT_CHANGED", rejected["reason_code"])
            self.assertNotIn("PRIVATE_DISPATCH_DIAGNOSTIC", json.dumps(rejected))

            with patch.object(
                service,
                "execute_dispatch_action",
                side_effect=RuntimeError("PRIVATE_RUNTIME_DIAGNOSTIC"),
            ):
                failed = json.loads(handler(arguments))
            self.assertEqual("INTERNAL_FAILURE", failed["reason_code"])
            self.assertNotIn("PRIVATE_RUNTIME_DIAGNOSTIC", json.dumps(failed))

    def test_public_agent_delivery_and_completion_tools_are_closed_and_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            context = FakeContext(FakeLifecycle([]))
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
                plugin = load_plugin("_hermes_langchain_delivery_completion_public_test")
                plugin.register(context)

            delivery = context.tools["hermes_langchain_invoke_agent"]
            completion = context.tools["hermes_langchain_complete"]
            delivery_parameters = delivery["schema"]["parameters"]
            completion_parameters = completion["schema"]["parameters"]
            self.assertFalse(delivery_parameters["additionalProperties"])
            self.assertFalse(completion_parameters["additionalProperties"])
            self.assertEqual(
                {
                    "workflow_id",
                    "artifact_id",
                    "dispatch_artifact_id",
                    "dispatch_artifact_sha256",
                },
                set(delivery_parameters["properties"]),
            )
            self.assertEqual(
                {
                    "workflow_id",
                    "artifact_id",
                    "dispatch_artifact_id",
                    "dispatch_artifact_sha256",
                    "delivery_artifact_id",
                    "delivery_artifact_sha256",
                    "expected_snapshot_fingerprint",
                },
                set(completion_parameters["properties"]),
            )

            delivery_handler = delivery["handler"]
            completion_handler = completion["handler"]
            package_root = delivery_handler.__module__.split(".plugin", 1)[0]
            service = importlib.import_module(f"{package_root}.controller.service")
            agent_delivery = importlib.import_module(
                f"{package_root}.controller.kanban_agent_delivery"
            )
            kanban_completion = importlib.import_module(
                f"{package_root}.controller.kanban_completion"
            )
            delivery_arguments = {
                "workflow_id": "canonical-task-agent-delivery-v1",
                "artifact_id": "delivery-artifact",
                "dispatch_artifact_id": "dispatch-artifact",
                "dispatch_artifact_sha256": "a" * 64,
            }
            completion_arguments = {
                "workflow_id": "canonical-task-completion-v1",
                "artifact_id": "completion-artifact",
                "dispatch_artifact_id": "dispatch-artifact",
                "dispatch_artifact_sha256": "a" * 64,
                "delivery_artifact_id": "delivery-artifact",
                "delivery_artifact_sha256": "b" * 64,
                "expected_snapshot_fingerprint": "c" * 64,
            }
            with patch.object(
                service,
                "execute_agent_delivery_action",
                return_value={"ok": True, "workflow_id": "canonical-task-agent-delivery-v1"},
            ):
                self.assertTrue(json.loads(delivery_handler(delivery_arguments))["ok"])
            with patch.object(
                service,
                "execute_completion_action",
                return_value={"ok": True, "workflow_id": "canonical-task-completion-v1"},
            ):
                self.assertTrue(json.loads(completion_handler(completion_arguments))["ok"])

            with patch.object(
                service,
                "execute_agent_delivery_action",
                side_effect=agent_delivery.KanbanAgentDeliveryError(
                    "CONFLICTING_REPLAY", "PRIVATE_AGENT_DIAGNOSTIC"
                ),
            ):
                rejected_delivery = json.loads(delivery_handler(delivery_arguments))
            self.assertEqual("CONFLICTING_REPLAY", rejected_delivery["reason_code"])
            self.assertNotIn("PRIVATE_AGENT_DIAGNOSTIC", json.dumps(rejected_delivery))

            with patch.object(
                service,
                "execute_completion_action",
                side_effect=kanban_completion.KanbanCompletionError(
                    "SNAPSHOT_CHANGED", "PRIVATE_COMPLETION_DIAGNOSTIC"
                ),
            ):
                rejected_completion = json.loads(completion_handler(completion_arguments))
            self.assertEqual("SNAPSHOT_CHANGED", rejected_completion["reason_code"])
            self.assertNotIn("PRIVATE_COMPLETION_DIAGNOSTIC", json.dumps(rejected_completion))


if __name__ == "__main__":
    unittest.main()
