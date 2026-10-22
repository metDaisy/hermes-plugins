"""Canonical controller Triage parity acceptance tests."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tests.support import load_module


CONTROLLER = load_module("controller")
TRIAGE_WORKFLOW = load_module("authoring.triage_workflow")
TRIAGE_ACTIVITY = load_module("authoring.triage_activity")
_ROOT = Path(__file__).resolve().parents[1]


class QueueProducer:
    def __init__(self, outputs: dict[str, dict]) -> None:
        self.outputs = outputs
        self.calls = []

    def invoke(self, invocation):
        self.calls.append(invocation)
        return CONTROLLER.AgentResult(
            agent_id=f"producer:{invocation.step_id}",
            output=self.outputs[invocation.step_id],
        )


class FakeKanbanPort:
    def __init__(self) -> None:
        self.created = []
        self.archived = []

    def list_active_triage(self, _board: str):
        return []

    def create_triage(self, *, board: str, card: dict):
        self.created.append((board, card))
        return "t_123abc"

    def read_task(self, *, board: str, task_id: str):
        created_board, card = self.created[0]
        self.assert_board = board == created_board
        return {
            "id": task_id,
            "title": card["title"],
            "status": "triage",
            "assignee": card["assignee"],
            "body": card["body_file"].read_text(encoding="utf-8"),
            "skills": [],
            "workspace_kind": "dir",
            "workspace_path": card["workspace"].as_posix(),
        }

    def archive_task(self, *, board: str, task_id: str) -> None:
        self.archived.append((board, task_id))


class ControllerTriageParityTests(unittest.TestCase):
    def test_executor_requires_explicit_kanban_port(self) -> None:
        with self.assertRaisesRegex(ValueError, "TRIAGE_KANBAN_PORT_REQUIRED"):
            CONTROLLER.TriageParityExecutor(
                producer=QueueProducer({}),
                inspection_fn=lambda *_args: {},
                git_head_fn=lambda _workspace: "a" * 40,
            )

    def test_run_directories_are_unique_for_full_run_ids(self) -> None:
        workspace = Path("C:/workspace")

        colon = CONTROLLER.TriageParityExecutor._run_dir(workspace, "a:b")
        underscore = CONTROLLER.TriageParityExecutor._run_dir(workspace, "a_b")

        self.assertNotEqual(colon, underscore)

    def test_five_step_topology_and_declared_projections(self) -> None:
        workflow = CONTROLLER.triage_parity_workflow()

        self.assertEqual(
            [
                "triage_intake",
                "triage_discovery",
                "triage_source_confirmation",
                "triage_analysis",
                "triage_materialize",
            ],
            [step.step_id for step in workflow.steps],
        )
        self.assertEqual(
            [
                ("triage_intake", "triage_discovery"),
                ("triage_discovery", "triage_source_confirmation"),
                ("triage_source_confirmation", "triage_analysis"),
                ("triage_analysis", "triage_materialize"),
            ],
            [
                (transition.source_step_id, transition.target_step_id)
                for transition in workflow.transitions
            ],
        )
        assignment = {
            "schema": "kwc-assignment-v1",
            "assignment_id": "kwa_parity",
            "board": "issue-42",
            "issue_number": 42,
            "generation": 1,
            "workspace_path": "C:/workspace",
            "phase": "triage",
            "profile": "project-manager",
            "undeclared": "must-not-project",
        }
        evidence = {
            "schema": "controller-evidence-bundle-v1",
            "workspace_path": "C:/workspace",
            "git_head": "A" * 40,
            "codebase_memory": [],
            "semble": [],
            "direct_source": [{"locator": " docs\\requirement.md#Goal ", "claim": " goal "}],
        }
        expected_evidence = {
            **evidence,
            "git_head": "a" * 40,
            "direct_source": [{"locator": "docs/requirement.md#Goal", "claim": "goal"}],
        }
        initial = {"assignment": assignment, "evidence": evidence}
        artifacts = {
            step_id: {
                "artifact_id": f"run:{step_id}:1",
                "payload": {
                    "stage": step_id.removeprefix("triage_"),
                    "artifact": {"path": f".temp/{step_id}.json", "sha256": step_id},
                    "body": {"stage": step_id},
                },
            }
            for step_id in (
                "triage_intake",
                "triage_discovery",
                "triage_source_confirmation",
                "triage_analysis",
            )
        }

        projected = {
            step.step_id: step.projector(initial, artifacts)
            for step in workflow.steps
        }

        self.assertEqual({"assignment", "evidence", "predecessors"}, set(projected["triage_intake"]))
        self.assertEqual({}, projected["triage_intake"]["predecessors"])
        self.assertEqual(
            {"triage_intake"},
            set(projected["triage_discovery"]["predecessors"]),
        )
        self.assertEqual(
            {"triage_intake", "triage_discovery"},
            set(projected["triage_source_confirmation"]["predecessors"]),
        )
        self.assertEqual(
            {"triage_intake", "triage_discovery", "triage_source_confirmation"},
            set(projected["triage_analysis"]["predecessors"]),
        )
        self.assertEqual(
            {
                "triage_intake",
                "triage_discovery",
                "triage_source_confirmation",
                "triage_analysis",
            },
            set(projected["triage_materialize"]["predecessors"]),
        )
        expected_assignment = dict(assignment)
        expected_assignment.pop("undeclared")
        for value in projected.values():
            self.assertEqual(expected_assignment, value["assignment"])
            self.assertEqual(expected_evidence, value["evidence"])
            self.assertNotIn("undeclared", value)

    def test_evidence_bundle_is_bounded_and_workspace_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            raw = {
                "schema": "controller-evidence-bundle-v1",
                "workspace_path": str(workspace),
                "git_head": "A" * 40,
                "codebase_memory": [
                    {"locator": "src/main/Foo.java#run", "claim": "caller path"}
                ],
                "semble": [
                    {"locator": "src/test/FooTest.java#run", "claim": "test path"}
                ],
                "direct_source": [
                    {"locator": "docs/requirement.md#Foo", "claim": "required behavior"}
                ],
            }

            normalized = CONTROLLER.normalize_evidence_bundle(
                raw,
                workspace=workspace,
                git_head_fn=lambda _workspace: "a" * 40,
            )

            self.assertEqual(workspace.as_posix(), normalized["workspace_path"])
            self.assertEqual("a" * 40, normalized["git_head"])
            self.assertEqual(raw["codebase_memory"], normalized["codebase_memory"])
            duplicate = dict(raw)
            duplicate["semble"] = [dict(raw["codebase_memory"][0])]
            with self.assertRaisesRegex(ValueError, "EVIDENCE_LOCATOR_DUPLICATE"):
                CONTROLLER.normalize_evidence_bundle(
                    duplicate,
                    workspace=workspace,
                    git_head_fn=lambda _workspace: "a" * 40,
                )
            overflow = dict(raw)
            overflow["codebase_memory"] = [
                {"locator": f"src/main/Foo{i}.java#run", "claim": "candidate"}
                for i in range(9)
            ]
            with self.assertRaisesRegex(ValueError, "EVIDENCE_LIMIT"):
                CONTROLLER.normalize_evidence_bundle(
                    overflow,
                    workspace=workspace,
                    git_head_fn=lambda _workspace: "a" * 40,
                )
            with self.assertRaisesRegex(ValueError, "EVIDENCE_GIT_HEAD_MISMATCH"):
                CONTROLLER.normalize_evidence_bundle(
                    raw,
                    workspace=workspace,
                    git_head_fn=lambda _workspace: "b" * 40,
                )
            oversized = dict(raw)
            oversized["direct_source"] = [
                {"locator": (" " * 1025) + "docs/requirement.md#Foo", "claim": "claim"}
            ]
            with self.assertRaisesRegex(ValueError, "EVIDENCE_RECORD_INVALID"):
                CONTROLLER.normalize_evidence_bundle(
                    oversized,
                    workspace=workspace,
                    git_head_fn=lambda _workspace: "a" * 40,
                )

        cwd = Path.cwd().resolve()
        empty_workspace = {
            "schema": "controller-evidence-bundle-v1",
            "workspace_path": "",
            "git_head": "a" * 40,
            "codebase_memory": [],
            "semble": [],
            "direct_source": [],
        }
        with self.assertRaisesRegex(ValueError, "EVIDENCE_WORKSPACE_INVALID"):
            CONTROLLER.normalize_evidence_bundle(
                empty_workspace,
                workspace=cwd,
                git_head_fn=lambda _workspace: "a" * 40,
            )

    def test_controller_and_existing_triage_paths_have_body_digest_and_readback_parity(self) -> None:
        with tempfile.TemporaryDirectory() as baseline_tmp, tempfile.TemporaryDirectory() as controller_tmp:
            baseline = self._fixture(Path(baseline_tmp))
            controller = self._fixture(Path(controller_tmp))
            baseline_result = self._run_existing_path(baseline)

            producer = QueueProducer(controller["outputs"])
            port = FakeKanbanPort()
            executor = CONTROLLER.TriageParityExecutor(
                producer=producer,
                inspection_fn=lambda *_args: controller["inspection"],
                git_head_fn=lambda _workspace: "a" * 40,
                kanban_port=port,
            )
            runtime = CONTROLLER.ControllerRuntime.open(
                workflow=CONTROLLER.triage_parity_workflow(),
                checkpoint_path=controller["workspace"] / "controller.db",
                artifact_store_path=controller["workspace"] / "artifacts.db",
                executor_registry=CONTROLLER.ExecutorRegistry(
                    {"agent": executor, "materialize": executor}
                ),
            )
            try:
                result = runtime.start(
                    run_id="triage-parity",
                    initial_input={
                        "assignment": controller["assignment"],
                        "evidence": controller["evidence"],
                    },
                )
                while result["status"] == "awaiting_resume":
                    result = runtime.resume(run_id="triage-parity")
            finally:
                runtime.close()

            self.assertEqual("completed", result["status"])
            materialized = result["artifacts"][-1]["payload"]
            materialized_body = json.loads(json.dumps(materialized["body"], ensure_ascii=False))
            self.assertEqual(baseline_result["body"], materialized_body)
            self.assertEqual(baseline_result["body_sha256"], materialized["artifact"]["sha256"])
            self.assertEqual(baseline_result["report"]["frozen_digest"], materialized["frozen_digest"])
            self.assertEqual(baseline_result["report"], materialized["read_back"])
            self.assertEqual(
                {
                    "schema": "controller-triage-milestone-v1",
                    "board": controller["assignment"]["board"],
                    "issue_number": controller["assignment"]["issue_number"],
                    "generation": controller["assignment"]["generation"],
                    "workspace_path": controller["assignment"]["workspace_path"],
                    "title": baseline_result["report"]["title"],
                    "assignee": controller["assignment"]["profile"],
                    "status": "triage",
                    "body_text": materialized["body_text"],
                    "frozen_digest": materialized["frozen_digest"],
                },
                materialized["milestone"],
            )
            self.assertEqual(
                [
                    "triage_intake",
                    "triage_discovery",
                    "triage_source_confirmation",
                    "triage_analysis",
                ],
                [call.step_id for call in producer.calls],
            )
            for call in producer.calls:
                self.assertTrue(call.goal.startswith("CONTROLLER_TRIAGE_AUTHORING_V1\n"))
                self.assertIn("Do not write an artifact file", call.goal)
                self.assertIn("Return only one JSON object", call.goal)
                self.assertEqual({"assignment", "evidence", "predecessors"}, set(call.input))
                self.assertTrue(
                    all("body_text" not in payload for payload in call.input["predecessors"].values())
                )
                self.assertNotIn("hermes-langchain", call.allowed_toolsets)
                self.assertTrue(call.output_schema["additionalProperties"] is False)
            materialize_validator = CONTROLLER.triage_parity_workflow().steps[-1].validator
            self.assertTrue(materialize_validator(materialized), materialized)
            unverified = json.loads(json.dumps(materialized, ensure_ascii=False))
            unverified["read_back"]["verified"] = False
            self.assertFalse(materialize_validator(unverified))
            mismatched = json.loads(json.dumps(materialized, ensure_ascii=False))
            mismatched["read_back"]["frozen_digest"] = "0" * 64
            self.assertFalse(materialize_validator(mismatched))
            self.assertTrue(port.assert_board)

    def test_controller_and_existing_triage_paths_have_failure_parity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            bad_discovery = dict(fixture["outputs"]["triage_discovery"])
            bad_discovery["targets"] = []
            raw_path = fixture["workspace"] / "bad-discovery.json"
            raw_path.write_text(json.dumps(bad_discovery), encoding="utf-8")
            intake_metadata = self._advance_existing(fixture, "intake")
            assignment = {
                **fixture["assignment"],
                "triage_stage": "discovery",
                "stage_artifacts": {"intake": intake_metadata},
            }
            with self.assertRaisesRegex(ValueError, "TARGET_COVERAGE"):
                TRIAGE_WORKFLOW.advance_stage(
                    assignment=assignment,
                    artifact_file=raw_path,
                    git_head_fn=lambda _workspace: "a" * 40,
                )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            fixture["outputs"]["triage_discovery"]["targets"] = []
            producer = QueueProducer(fixture["outputs"])
            executor = CONTROLLER.TriageParityExecutor(
                producer=producer,
                inspection_fn=lambda *_args: fixture["inspection"],
                git_head_fn=lambda _workspace: "a" * 40,
                kanban_port=FakeKanbanPort(),
            )
            runtime = CONTROLLER.ControllerRuntime.open(
                workflow=CONTROLLER.triage_parity_workflow(),
                checkpoint_path=fixture["workspace"] / "controller.db",
                artifact_store_path=fixture["workspace"] / "artifacts.db",
                executor_registry=CONTROLLER.ExecutorRegistry(
                    {"agent": executor, "materialize": executor}
                ),
            )
            try:
                first = runtime.start(
                    run_id="triage-failure",
                    initial_input={
                        "assignment": fixture["assignment"],
                        "evidence": fixture["evidence"],
                    },
                )
                result = runtime.resume(run_id="triage-failure")
            finally:
                runtime.close()
            self.assertEqual("awaiting_resume", first["status"])
            self.assertEqual("blocked", result["status"])
            self.assertEqual(
                "agent execution failed for step triage_discovery: ValueError",
                result["error"],
            )

    def test_resume_restores_predecessor_file_from_accepted_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            producer = QueueProducer(fixture["outputs"])
            executor = CONTROLLER.TriageParityExecutor(
                producer=producer,
                inspection_fn=lambda *_args: fixture["inspection"],
                git_head_fn=lambda _workspace: "a" * 40,
                kanban_port=FakeKanbanPort(),
            )
            registry = CONTROLLER.ExecutorRegistry({"agent": executor, "materialize": executor})
            checkpoint = fixture["workspace"] / "controller.db"
            artifact_store = fixture["workspace"] / "artifacts.db"
            runtime = CONTROLLER.ControllerRuntime.open(
                workflow=CONTROLLER.triage_parity_workflow(),
                checkpoint_path=checkpoint,
                artifact_store_path=artifact_store,
                executor_registry=registry,
            )
            try:
                first = runtime.start(
                    run_id="triage-artifact-restore",
                    initial_input={
                        "assignment": fixture["assignment"],
                        "evidence": fixture["evidence"],
                    },
                )
            finally:
                runtime.close()

            accepted = first["artifacts"][0]["payload"]
            predecessor_path = fixture["workspace"] / accepted["artifact"]["path"]
            canonical_bytes = accepted["body_text"].encode("utf-8")
            self.assertEqual(
                accepted["artifact"]["sha256"],
                hashlib.sha256(canonical_bytes).hexdigest(),
            )
            self.assertEqual(
                json.loads(json.dumps(accepted["body"], ensure_ascii=False)),
                json.loads(accepted["body_text"]),
            )
            predecessor_path.write_text("{}\n", encoding="utf-8")

            runtime = CONTROLLER.ControllerRuntime.open(
                workflow=CONTROLLER.triage_parity_workflow(),
                checkpoint_path=checkpoint,
                artifact_store_path=artifact_store,
                executor_registry=registry,
            )
            try:
                resumed = runtime.resume(run_id="triage-artifact-restore")
            finally:
                runtime.close()

            self.assertEqual("awaiting_resume", resumed["status"], resumed)
            self.assertEqual(
                json.loads(json.dumps(accepted["body"], ensure_ascii=False)),
                json.loads(predecessor_path.read_text(encoding="utf-8")),
            )

    def _fixture(self, workspace: Path) -> dict:
        workspace = workspace.resolve()
        authoring = json.loads(
            (_ROOT / "tests/fixtures/valid-triage-authoring.json").read_text(encoding="utf-8")
        )
        inspection = json.loads(
            (_ROOT / "tests/fixtures/valid-current-state-inspection.json").read_text(encoding="utf-8")
        )
        self._touch_locators(workspace, authoring)
        for guide in ("docs/development-guide.md", "docs/test-guide.md"):
            path = workspace / guide
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        target = authoring["intake"]["investigation_targets"][0]
        locators = {
            target["locator"],
            authoring["analysis"]["confirmed_gaps"][0]["evidence"][0]["locator"],
            authoring["analysis"]["verification_matrix"][0]["evidence"][0]["locator"],
            authoring["analysis"]["verification_matrix"][0]["production_seam"]["locator"],
            *authoring["identity"]["requirement_locators"],
            *authoring["identity"]["current_state_locators"],
        }
        discovery = {
            "schema": "kwc-triage-discovery-v2",
            "targets": [{
                "target_id": target["id"],
                "candidates": [
                    {
                        "path": locator.split("#", 1)[0],
                        "symbol": locator.split("#", 1)[1],
                        "kind": (
                            "document" if locator.startswith("docs/") else
                            "test" if locator.startswith("src/test/") else "production"
                        ),
                        "reason": "target implementation evidence",
                    }
                    for locator in sorted(locators)
                ],
            }],
            "tool_usage": {"codebase_memory": 1, "semble": 1},
            "fallbacks": [],
            "codebase_memory": {
                "projects": [{
                    "project": "controller-triage-parity",
                    "root_path": str(workspace),
                    "root_exists": True,
                    "git_head": "a" * 40,
                }],
                "resolution": {
                    "status": "available",
                    "project": "controller-triage-parity",
                    "reason": "workspace-and-head-match",
                    "git_head": "a" * 40,
                },
            },
        }
        confirmation = {
            "schema": "kwc-triage-source-confirmation-v2",
            "targets": [{
                "target_id": target["id"],
                "disposition": "partial",
                "rationale": "direct inspection",
                "public_contract": None,
                "evidence": [
                    {
                        "locator": f'{candidate["path"]}#{candidate["symbol"]}',
                        "claim": "confirmed implementation evidence",
                        "kind": candidate["kind"],
                    }
                    for candidate in discovery["targets"][0]["candidates"]
                ],
            }],
            "open_questions": [],
        }
        assignment = {
            "schema": "kwc-assignment-v1",
            "assignment_id": "kwa_parity",
            "board": "issue-42",
            "issue_number": 42,
            "generation": 1,
            "workspace_path": str(workspace),
            "phase": "triage",
            "profile": "project-manager",
        }
        raw_evidence = {
            "schema": "controller-evidence-bundle-v1",
            "workspace_path": str(workspace),
            "git_head": "a" * 40,
            "codebase_memory": [{"locator": target["locator"], "claim": "target"}],
            "semble": [],
            "direct_source": [{
                "locator": authoring["identity"]["requirement_locators"][0],
                "claim": "requirement",
            }],
        }
        return {
            "workspace": workspace,
            "assignment": assignment,
            "inspection": inspection,
            "evidence": CONTROLLER.normalize_evidence_bundle(
                raw_evidence,
                workspace=workspace,
                git_head_fn=lambda _workspace: "a" * 40,
            ),
            "outputs": {
                "triage_intake": {
                    "schema": "kwc-triage-intake-authoring-v1",
                    "identity": authoring["identity"],
                    "intake": authoring["intake"],
                },
                "triage_discovery": discovery,
                "triage_source_confirmation": confirmation,
                "triage_analysis": {
                    "schema": "kwc-triage-analysis-authoring-v1",
                    "analysis": authoring["analysis"],
                },
            },
        }

    def _run_existing_path(self, fixture: dict) -> dict:
        artifacts = {}
        for stage in ("intake", "discovery", "source_confirmation", "analysis"):
            artifacts[stage] = self._advance_existing(fixture, stage, artifacts)
        assignment = {
            **fixture["assignment"],
            "triage_stage": "materialize",
            "stage_artifacts": artifacts,
        }
        arguments = TRIAGE_WORKFLOW.materialization_arguments(assignment)
        report_path = fixture["workspace"] / "baseline-report.json"
        report = TRIAGE_ACTIVITY.execute_triage_authoring(
            assignment=assignment,
            authoring_file=fixture["workspace"] / arguments["authoring_file"],
            current_state_inspection_file=(
                fixture["workspace"] / arguments["current_state_inspection_file"]
            ),
            output_file=report_path,
            kanban_port=FakeKanbanPort(),
        )
        body_path = fixture["workspace"] / report["frozen_body_file"]
        data = body_path.read_bytes()
        return {
            "body": json.loads(data.decode("utf-8")),
            "body_sha256": hashlib.sha256(data).hexdigest(),
            "report": report,
        }

    def _advance_existing(self, fixture: dict, stage: str, artifacts: dict | None = None) -> dict:
        step_id = f"triage_{stage}"
        raw_path = fixture["workspace"] / f"baseline-{stage}.json"
        raw_path.write_text(
            json.dumps(fixture["outputs"][step_id], ensure_ascii=False),
            encoding="utf-8",
        )
        assignment = {
            **fixture["assignment"],
            "triage_stage": stage,
            "stage_artifacts": dict(artifacts or {}),
        }
        kwargs = {"assignment": assignment, "artifact_file": raw_path}
        if stage == "intake":
            kwargs["inspection_fn"] = lambda *_args: fixture["inspection"]
        if stage == "discovery":
            kwargs["git_head_fn"] = lambda _workspace: "a" * 40
        return TRIAGE_WORKFLOW.advance_stage(**kwargs)["artifact"]

    def _touch_locators(self, workspace: Path, value) -> None:
        if isinstance(value, dict):
            locator = value.get("locator")
            if isinstance(locator, str) and not locator.startswith(("http://", "https://")):
                path = workspace / locator.split("#", 1)[0]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            for item in value.values():
                self._touch_locators(workspace, item)
        elif isinstance(value, list):
            for item in value:
                self._touch_locators(workspace, item)


if __name__ == "__main__":
    unittest.main()
