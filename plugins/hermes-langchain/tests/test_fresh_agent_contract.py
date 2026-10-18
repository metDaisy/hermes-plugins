"""Contract tests for KWC-owned profile-independent agents."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tests.support import load_module

CONTRACTS = load_module("runtime.fresh_agent_contract")


class FreshAgentContractTests(unittest.TestCase):
    def test_authoring_contract_has_no_behavior_dependency_on_named_profile(self) -> None:
        contract = CONTRACTS.build_contract(
            situation="impl-authoring", host_profile="main", kanban_profile="main"
        )

        self.assertEqual("kwc-fresh-agent-execution-v1", contract["schema"])
        self.assertEqual("kwc-fresh-agent", contract["actor"])
        self.assertEqual("kanban-workflow-controller", contract["behavior_source"])
        self.assertTrue(contract["ignore_rules"])
        self.assertFalse(contract["load_soul"])
        self.assertFalse(contract["load_memory"])
        self.assertEqual(
            [
                "hermes-langchain:kwc-orchestrator-contract",
                "hermes-langchain:kwc-build-task-graph",
            ],
            contract["skills"],
        )
        self.assertEqual(["file", "terminal", "hermes-langchain"], contract["toolsets"])
        self.assertIn("codebase-memory-mcp", contract["mcp_servers"])
        self.assertNotIn("codebase-memory-mcp", contract["toolsets"])
        self.assertNotIn("kanban", contract["toolsets"])
        self.assertIn("kwc_create_impl_graph", contract["allowed_tools"])
        self.assertNotIn("project-manager", json.dumps(contract, sort_keys=True))

    def test_every_pm_responsibility_has_a_kwc_owned_skill_resource(self) -> None:
        expected = {
            "kwc-orchestrator-contract",
            "kwc-create-triage",
            "kwc-build-task-graph",
            "kwc-run-workflow",
            "kwc-service-planning",
            "kwc-sync-docs",
            "kwc-update-current-state",
            "kwc-regenerate-summary",
        }
        paths = CONTRACTS.plugin_skill_paths()

        self.assertEqual(expected, set(paths))
        self.assertTrue(all(path.is_file() for path in paths.values()))

    def test_every_situation_injects_common_contract_before_situation_skill(self) -> None:
        for situation in CONTRACTS._SITUATIONS:
            with self.subTest(situation=situation):
                contract = CONTRACTS.build_contract(
                    situation=situation,
                    host_profile="main",
                    kanban_profile="project-manager",
                )
                self.assertEqual(
                    "hermes-langchain:kwc-orchestrator-contract",
                    contract["skills"][0],
                )
                self.assertEqual(2, len(contract["skills"]))

    def test_pm_capabilities_are_mapped_or_explicitly_excluded(self) -> None:
        coverage_path = (
            next(iter(CONTRACTS.plugin_skill_paths().values())).parents[1]
            / "kwc-orchestrator-contract"
            / "references"
            / "pm-responsibility-coverage.json"
        )
        coverage = json.loads(coverage_path.read_text(encoding="utf-8"))

        self.assertEqual("kwc-pm-responsibility-coverage-v1", coverage["schema"])
        self.assertTrue(coverage["comparison_baseline"]["preserved"])
        self.assertFalse(coverage["comparison_baseline"]["runtime_dependency"])
        owners = {entry["kwc_owner"] for entry in coverage["responsibilities"]}
        self.assertEqual(set(CONTRACTS.plugin_skill_paths()), owners)
        declared_situations = {
            situation
            for entry in coverage["responsibilities"]
            for situation in entry["situations"]
            if situation != "all"
        }
        self.assertEqual(set(CONTRACTS._SITUATIONS), declared_situations)
        self.assertEqual(
            {"memory", "session_search", "auto_loaded_skills", "clarify", "todo"},
            set(coverage["intentionally_excluded_profile_state"]),
        )
        skill_root = coverage_path.parents[2]
        self.assertTrue(
            all((skill_root / relative).is_file() for relative in coverage["operational_assets"])
        )

    def test_situation_capabilities_preserve_pm_research_and_verification_paths(self) -> None:
        service_planning = CONTRACTS.build_contract(
            situation="service-planning",
            host_profile="main",
            kanban_profile="project-manager",
        )
        self.assertIn("web", service_planning["toolsets"])
        self.assertIn("browser", service_planning["toolsets"])
        for situation in ("implementation-checkpoint", "summary-finalization"):
            with self.subTest(situation=situation):
                contract = CONTRACTS.build_contract(
                    situation=situation,
                    host_profile="main",
                    kanban_profile="project-manager",
                )
                self.assertIn("gradle-mcp", contract["mcp_servers"])

    def test_every_pm_distribution_owned_asset_has_a_kwc_target(self) -> None:
        plugin_root = next(iter(CONTRACTS.plugin_skill_paths().values())).parents[2]
        asset_map_path = (
            plugin_root
            / "skills"
            / "kwc-orchestrator-contract"
            / "references"
            / "pm-source-asset-map.json"
        )
        asset_map = json.loads(asset_map_path.read_text(encoding="utf-8"))
        source_assets = asset_map["source_distribution_owned"]

        self.assertEqual(asset_map["source_distribution_owned_count"], len(source_assets))
        self.assertEqual(len(source_assets), len(set(source_assets)))
        for source in source_assets:
            with self.subTest(source=source):
                targets = asset_map["root_mappings"].get(source)
                if targets is None:
                    prefix, skill_name, *tail = source.split("/")
                    self.assertEqual("skills", prefix)
                    targets = ["/".join(("skills", f"kwc-{skill_name}", *tail))]
                self.assertTrue(targets)
                self.assertTrue(all((plugin_root / target).is_file() for target in targets))

    def test_kwc_owned_skills_have_no_runtime_path_dependency_on_pm_profile(self) -> None:
        skill_root = next(iter(CONTRACTS.plugin_skill_paths().values())).parents[1]
        forbidden = ".hermes/profiles/project-manager/skills"
        for path in skill_root.rglob("*"):
            if path.is_file() and path.suffix in {".md", ".py"}:
                with self.subTest(path=path.relative_to(skill_root).as_posix()):
                    content = path.read_text(encoding="utf-8")
                    self.assertNotIn(forbidden, content)
                    if path.suffix == ".py":
                        compile(content, str(path), "exec")

    def test_workflow_activation_uses_host_profile_not_pm_profile(self) -> None:
        skill_root = next(iter(CONTRACTS.plugin_skill_paths().values())).parents[1]
        content = (skill_root / "kwc-run-workflow/scripts/activate_graph.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('[hermes_bin, "kanban", "--board"', content)
        self.assertNotIn('[hermes_bin, "--profile"', content)
        self.assertNotIn("HOST_PROFILE", content)
        self.assertNotIn('[hermes_bin, "--profile", PM_PROFILE', content)

    def test_cross_skill_helper_dependencies_resolve_inside_plugin(self) -> None:
        skill_root = next(iter(CONTRACTS.plugin_skill_paths().values())).parents[1]
        expected = {
            "kwc-run-workflow/scripts/checkpoint.py": "kwc-build-task-graph",
            "kwc-run-workflow/scripts/activate_graph.py": "kwc-create-triage",
            "kwc-build-task-graph/scripts/graph_contract.py": "kwc-run-workflow",
            "kwc-build-task-graph/scripts/build_task_graph.py": "kwc-create-triage",
        }
        for relative, marker in expected.items():
            with self.subTest(relative=relative):
                path = skill_root / relative
                self.assertTrue(path.is_file())
                self.assertIn(marker, path.read_text(encoding="utf-8"))

    def test_execution_manifest_records_exact_runtime_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            contract = CONTRACTS.build_contract(
                situation="implementation-checkpoint",
                host_profile="main",
                kanban_profile="project-manager",
            )
            result = CONTRACTS.write_manifest(
                workspace=workspace,
                execution_id="inv:checkpoint:1",
                contract=contract,
                prompt="perform checkpoint",
                context={"board": "issue-151", "task_id": "t_123abc"},
            )
            stored = json.loads(Path(result["path"]).read_text(encoding="utf-8"))

        self.assertEqual("kwc-fresh-agent-manifest-v1", stored["schema"])
        self.assertEqual(contract, stored["contract"])
        self.assertEqual("perform checkpoint", stored["prompt"])
        self.assertEqual("t_123abc", stored["context"]["task_id"])
        self.assertEqual(64, len(stored["prompt_sha256"]))

    def test_execution_manifest_redacts_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            contract = CONTRACTS.build_contract(
                situation="worker-needs-input",
                host_profile="main",
                kanban_profile="project-manager",
            )
            result = CONTRACTS.write_manifest(
                workspace=workspace,
                execution_id="inv:redaction:1",
                contract=contract,
                prompt="inspect state; api_key=not-for-artifacts",
                context={
                    "authorization": "Bearer not-for-artifacts",
                    "nested": {"password": "not-for-artifacts", "task_id": "t_safe"},
                },
            )
            stored = json.loads(Path(result["path"]).read_text(encoding="utf-8"))

        serialized = json.dumps(stored, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("not-for-artifacts", serialized)
        self.assertIn("[REDACTED]", serialized)
        self.assertEqual("t_safe", stored["context"]["nested"]["task_id"])

    def test_runtime_reasons_map_to_specialized_situations(self) -> None:
        self.assertEqual(
            "implementation-checkpoint",
            CONTRACTS.situation_for_reason("implementation_checkpoint"),
        )
        self.assertEqual(
            "worker-needs-input", CONTRACTS.situation_for_reason("worker_needs_input")
        )
        self.assertEqual(
            "route-review-findings",
            CONTRACTS.situation_for_reason("route_review_findings"),
        )

    def test_comparison_artifact_requires_same_prompt_and_grades_exact_result(self) -> None:
        prompt = "Which schema is required before aggregate Review promotion?"
        prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "comparison.json"
            result = CONTRACTS.write_comparison_artifact(
                output_path=output,
                prompt=prompt,
                expected="aggregate-review-admission-v1",
                fresh_result={
                    "profile": "main",
                    "session_id": "fresh-1",
                    "model": "model-a",
                    "text": "aggregate-review-admission-v1",
                    "duration_ms": 4663,
                    "tokens": {"total": 9327},
                    "prompt_sha256": prompt_sha256,
                },
                pm_result={
                    "profile": "project-manager",
                    "session_id": "pm-1",
                    "model": "model-a",
                    "text": "aggregate-review-result-v2",
                    "duration_ms": 10977,
                    "tokens": {"total": 17780},
                    "prompt_sha256": prompt_sha256,
                },
            )
            stored = json.loads(output.read_text(encoding="utf-8"))
            actual_sha256 = hashlib.sha256(output.read_bytes()).hexdigest()

        self.assertEqual("kwc-pm-comparison-v1", stored["schema"])
        self.assertTrue(stored["verdicts"]["kwc_fresh_exact"])
        self.assertFalse(stored["verdicts"]["pm_profile_exact"])
        self.assertEqual("kwc_fresh", stored["verdicts"]["quality_winner"])
        self.assertEqual(result["sha256"], actual_sha256)


if __name__ == "__main__":
    unittest.main()
