"""Tests for fresh PM phase launches in KWC authoring."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_ROOT = Path(__file__).resolve().parents[1]
from tests.support import load_module

LAUNCHER = load_module("authoring.authoring_launcher")


class _Process:
    pid = 4321


class AuthoringLauncherTests(unittest.TestCase):
    def test_review_prompt_requires_prefill_contract_and_exact_enums(self) -> None:
        captured = {}

        def popen(command, **kwargs):
            captured["command"] = command
            return _Process()

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            LAUNCHER.launch_assignment(
                assignment={
                    "schema": "kwc-assignment-v1",
                    "assignment_id": "kwa_review",
                    "board": "issue-151",
                    "issue_number": 151,
                    "generation": 8,
                    "phase": "review",
                    "allowed_tools": ["kwc_create_review"],
                    "next_action": "kwc_create_review",
                    "artifact_file": ".temp/task-graphs/151/review-body.json",
                    "sources": {
                        "triage_id": "t_triage",
                        "impl_ids": ["t_impl"],
                        "review_id": None,
                        "summary_id": None,
                    },
                },
                workspace=workspace,
                state_path=workspace / "state.db",
                profile="project-manager",
                host_profile="main",
                hermes_executable="C:/Hermes/hermes.exe",
                popen_fn=popen,
            )

        prompt = captured["command"][captured["command"].index("-q") + 1]
        self.assertIn("prepare-review-fast-path", prompt)
        self.assertIn("review_body_template", prompt)
        self.assertIn("behavior | implementation-convention | cross-card-seam | test-quality | preservation", prompt)
        self.assertIn("issue/card_type/generation/review_card_key", prompt)

    def test_rejects_assignment_when_langgraph_route_is_not_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            assignment = {
                "schema": "kwc-assignment-v1",
                "board": "issue-151",
                "generation": 1,
                "allowed_tools": ["kwc_advance_triage"],
                "next_action": "kwc_create_triage",
            }

            with self.assertRaisesRegex(ValueError, "KWC_LANGGRAPH_ROUTE_MISMATCH"):
                LAUNCHER.launch_assignment(
                    assignment=assignment,
                    workspace=workspace,
                    state_path=workspace / "state.db",
                    profile="project-manager",
                    hermes_executable="C:/Hermes/hermes.exe",
                    popen_fn=lambda *_args, **_kwargs: _Process(),
                )

    def test_launches_profile_independent_triage_agent_with_shared_state(self) -> None:
        captured = {}

        def popen(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            return _Process()

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            state_path = workspace / "kwc.db"
            assignment = {
                "schema": "kwc-assignment-v1",
                "assignment_id": "kwa_intake",
                "board": "issue-151-test-13",
                "issue_number": 151,
                "generation": 1,
                "phase": "triage",
                "triage_stage": "intake",
                "allowed_tools": ["kwc_advance_triage"],
                "next_action": "kwc_advance_triage",
                "artifact_file": ".temp/kwc-authoring/151/generation-1/intake-authoring.json",
            }
            inherited = {
                "HERMES_SESSION_KEY": "parent-session",
                "HERMES_SESSION_ID": "parent-id",
                "HERMES_SESSION_PROFILE": "main",
                "HERMES_SESSION_STALL_TIMEOUT": "97",
            }
            with patch.dict(os.environ, inherited, clear=False):
                result = LAUNCHER.launch_assignment(
                    assignment=assignment,
                    workspace=workspace,
                    state_path=state_path,
                    profile="project-manager",
                    host_profile="main",
                    hermes_executable="C:/Hermes/hermes.exe",
                    popen_fn=popen,
                )
                manifest = json.loads(
                    Path(result["manifest_path"]).read_text(encoding="utf-8")
                )

        self.assertEqual(4321, result["pid"])
        command = captured["command"]
        self.assertEqual(["C:/Hermes/hermes.exe", "--profile", "main", "chat"], command[:4])
        self.assertIn("--oneshot", command)
        self.assertIn("--quiet", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("--pass-session-id", command)
        self.assertIn("--toolsets", command)
        self.assertIn("--skills", command)
        self.assertEqual(
            "hermes-langchain:kwc-orchestrator-contract,hermes-langchain:kwc-triage-authoring-runtime",
            command[command.index("--skills") + 1],
        )
        toolsets = command[command.index("--toolsets") + 1].split(",")
        self.assertEqual(
            [
                "file",
                "terminal",
                "hermes-langchain",
                "codebase-memory-mcp",
                "github-mcp",
                "semble-mcp",
            ],
            toolsets,
        )
        self.assertIn("--source", command)
        self.assertIn("tool", command)
        self.assertNotIn("--continue", command)
        prompt = command[command.index("-q") + 1]
        self.assertIn("kwc-assignment-v1", prompt)
        self.assertIn("kwc_advance_triage", prompt)
        self.assertIn("assignment.next_action", prompt)
        self.assertIn("next_assignment", prompt)
        self.assertIn("phase가 triage가 아니면", prompt)
        self.assertIn("docs/current-state.md", prompt)
        self.assertIn("repo-relative", prompt)
        self.assertIn("assignment's artifact_file", prompt)
        self.assertIn(".temp/kwc-authoring/151/generation-1/intake-authoring.json", prompt)
        self.assertEqual(str(state_path.resolve()), captured["kwargs"]["env"]["HERMES_LANGCHAIN_STATE_PATH"])
        self.assertEqual("main", captured["kwargs"]["env"]["HERMES_PROFILE_NAME"])
        self.assertEqual("main", captured["kwargs"]["env"]["HERMES_PROFILE"])
        self.assertEqual("main", captured["kwargs"]["env"]["HERMES_SESSION_PROFILE"])
        self.assertEqual(
            "project-manager", captured["kwargs"]["env"]["HERMES_KWC_EXECUTION_ROLE"]
        )
        self.assertEqual(
            "project-manager", captured["kwargs"]["env"]["HERMES_KWC_KANBAN_PROFILE"]
        )
        self.assertEqual("main", captured["kwargs"]["env"]["HERMES_KWC_FRESH_HOST_PROFILE"])
        self.assertNotIn("HERMES_SESSION_KEY", captured["kwargs"]["env"])
        self.assertNotIn("HERMES_SESSION_ID", captured["kwargs"]["env"])
        self.assertEqual("97", captured["kwargs"]["env"]["HERMES_SESSION_STALL_TIMEOUT"])
        self.assertEqual(str(workspace.resolve()), captured["kwargs"]["cwd"])
        self.assertEqual("kwc-fresh-agent-manifest-v1", manifest["schema"])
        self.assertEqual("triage-authoring", manifest["contract"]["situation"])
        self.assertEqual("main", manifest["contract"]["host_profile"])
        self.assertEqual("project-manager", manifest["contract"]["kanban_profile"])
        self.assertEqual("kwc-triage-issue-151-test-13-g1", result["session_name"])


if __name__ == "__main__":
    unittest.main()
