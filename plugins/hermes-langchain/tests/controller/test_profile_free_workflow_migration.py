from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

from tests.support import load_module


ROOT = Path(__file__).resolve().parents[2]


class _Context:
    def __init__(self) -> None:
        self.hooks: dict[str, Any] = {}
        self.tools: dict[str, dict[str, Any]] = {}
        self.skills: dict[str, dict[str, Any]] = {}

    def register_hook(self, name: str, handler: Any) -> None:
        self.hooks[name] = handler

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

    def register_skill(
        self,
        name: str,
        path: Path,
        description: str = "",
        frontmatter: dict[str, Any] | None = None,
    ) -> None:
        self.skills[name] = {
            "path": Path(path),
            "description": description,
            "frontmatter": dict(frontmatter or {}),
        }


class _Executor:
    def __init__(self) -> None:
        self.invocations: list[Any] = []

    def invoke(self, invocation: Any) -> Any:
        self.invocations.append(invocation)
        contracts = load_module("controller.agent_contracts")
        return contracts.AgentResult(
            agent_id="fresh-pm-agent",
            output={
                "schema": "kanban-workflow-orchestration-result-v1",
                "status": "running",
                "issue_board": {
                    "board": "issue-151",
                    "issue_number": 151,
                    "generation": 9,
                },
                "phase_task_run": {
                    "phase": "implementation",
                    "task_id": "t_impl",
                    "run_id": 42,
                },
                "head_final_implementation_sha": {
                    "head_sha": "a" * 40,
                    "final_implementation_sha": None,
                },
                "validation": [
                    {
                        "name": "validate-state",
                        "outcome": "passed",
                        "evidence": "workflow validator accepted the current frontier",
                    }
                ],
                "native_readback": ["exact task and run read-back completed"],
                "external_readback": [],
                "blocker": None,
                "next_transition": "dispatch implementation task",
            },
        )


class ProfileFreeWorkflowMigrationTests(unittest.TestCase):
    def test_manifest_exposes_only_profile_free_controller_surface(self) -> None:
        manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))

        self.assertNotIn("provides_hooks", manifest)
        self.assertNotIn("config_schema", manifest)
        tools = set(manifest["provides_tools"])
        self.assertIn("hermes_langchain_run_workflow", tools)
        self.assertTrue(
            {
                "kwc_comment",
                "kwc_reply",
                "kwc_pm_response",
                "kwc_profile_response",
            }.isdisjoint(tools)
        )

    def test_plugin_registers_no_legacy_wake_or_profile_response_surface(self) -> None:
        plugin = load_module("plugin.entrypoint")
        context = _Context()

        plugin.register(context)

        self.assertEqual({}, context.hooks)
        self.assertIn("hermes_langchain_run_workflow", context.tools)
        self.assertTrue(
            {
                "kwc_comment",
                "kwc_reply",
                "kwc_pm_response",
                "kwc_profile_response",
            }.isdisjoint(context.tools)
        )

    def test_legacy_profile_runtime_modules_are_removed(self) -> None:
        forbidden = (
            "src/hermes_langchain/runtime/action_executor.py",
            "src/hermes_langchain/runtime/agent_lease_service.py",
            "src/hermes_langchain/runtime/comment_service.py",
            "src/hermes_langchain/runtime/invocation_service.py",
            "src/hermes_langchain/runtime/profile_launcher.py",
            "src/hermes_langchain/runtime/profile_worker.py",
            "src/hermes_langchain/runtime/runtime_service.py",
            "src/hermes_langchain/runtime/wake_service.py",
            "src/hermes_langchain/plugin/dispatch_service.py",
            "src/hermes_langchain/plugin/routing_policy.py",
            "src/hermes_langchain/plugin/runtime_state.py",
            "src/hermes_langchain/kanban/action_service.py",
            "src/hermes_langchain/kanban/dispatch_service.py",
            "src/hermes_langchain/kanban/kwc_core.py",
            "src/hermes_langchain/kanban/mutation_service.py",
            "src/hermes_langchain/kanban/workflow_engine.py",
        )

        self.assertEqual([], [path for path in forbidden if (ROOT / path).exists()])

    def test_pm_profile_asset_map_targets_current_plugin_owners(self) -> None:
        coverage = json.loads(
            (
                ROOT
                / "skills"
                / "kwc-orchestrator-contract"
                / "references"
                / "pm-responsibility-coverage.json"
            ).read_text(encoding="utf-8")
        )
        asset_map = json.loads(
            (
                ROOT
                / "skills"
                / "kwc-orchestrator-contract"
                / "references"
                / "pm-source-asset-map.json"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual("hermes-langchain", coverage["behavior_owner"])
        self.assertFalse(coverage["source_profile_reference"]["installed_required"])
        targets = {
            target
            for mapped_targets in asset_map["root_mappings"].values()
            for target in mapped_targets
        }
        self.assertEqual([], sorted(target for target in targets if not (ROOT / target).exists()))
        self.assertNotIn("src/hermes_langchain/runtime/fresh_agent_contract.py", targets)

    def test_pm_workflow_contract_is_direct_fresh_agent_output(self) -> None:
        orchestration = load_module("controller.workflow_orchestration")

        contract = orchestration.workflow_contract("implementation-checkpoint")

        self.assertEqual(
            ("kwc-orchestrator-contract", "kwc-run-workflow"),
            contract["skills"],
        )
        self.assertIn("gradle-mcp", contract["toolsets"])
        self.assertEqual(
            "kanban-workflow-orchestration-result-v1",
            contract["output_schema"]["properties"]["schema"]["const"],
        )
        self.assertNotIn("allowed_tools", contract)
        self.assertNotIn("project-manager", json.dumps(contract, sort_keys=True).lower())

    def test_orchestrator_stages_plugin_skills_and_persists_typed_result(self) -> None:
        orchestration = load_module("controller.workflow_orchestration")
        artifact_store = load_module("controller.artifact_store")
        executor = _Executor()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = artifact_store.SQLiteArtifactStore.open(root / "artifacts.sqlite3")
            service = orchestration.WorkflowOrchestrator(
                artifact_store=store,
                staging_root=root / "staging",
                executor=executor,
                snapshot_reader=lambda board: {
                    "schema": "kwc-workflow-snapshot-v1",
                    "board": board,
                    "tasks": [],
                },
                git_state_reader=lambda workspace: {
                    "schema": "kwc-git-state-v1",
                    "workspace_path": str(workspace),
                    "head_sha": "a" * 40,
                    "dirty_paths": [],
                },
            )
            request = {
                "schema": "kanban-workflow-orchestration-request-v1",
                "artifact_id": "workflow-result:issue-151:g9:implementation",
                "workflow_id": "kanban-workflow-orchestration-v1",
                "situation": "implementation-checkpoint",
                "board": "issue-151",
                "issue_number": 151,
                "generation": 9,
                "workspace_path": str(root),
                "task_id": "t_impl",
            }

            first = service.invoke(request)
            second = service.invoke(request)

            self.assertFalse(first["idempotent_replay"])
            self.assertTrue(second["idempotent_replay"])
            self.assertEqual("fresh-pm-agent", first["agent_id"])
            self.assertEqual(1, len(executor.invocations))
            invocation = executor.invocations[0]
            self.assertEqual("workflow_orchestration", invocation.step_id)
            self.assertEqual(
                ("kwc-orchestrator-contract", "kwc-run-workflow"),
                invocation.skills,
            )
            staged = Path(invocation.input["artifact_directory"])
            self.assertTrue((staged / "manifest.json").is_file())
            self.assertTrue((staged / "orchestrator-contract.md").is_file())
            self.assertTrue((staged / "workflow.md").is_file())
            self.assertTrue((staged / "board.json").is_file())
            self.assertTrue((staged / "git.json").is_file())
            self.assertEqual(
                "kanban-workflow-orchestration-result-v1",
                first["result"]["schema"],
            )
            store.close()


if __name__ == "__main__":
    unittest.main()
