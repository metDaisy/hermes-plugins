"""Tool-contract tests for Profile-independent KWC authoring."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.support import load_module

PLUGIN = load_module("plugin.entrypoint")


class FakeContext:
    def __init__(self) -> None:
        self.tools = {}
        self.skills = {}
        self.schemas = {}
        self.tool_checks = {}
        self.profile_name = "main"

    def register_tool(self, name, _toolset, schema, handler, **kwargs) -> None:
        self.tools[name] = handler
        self.schemas[name] = schema
        self.tool_checks[name] = kwargs.get("check_fn")

    def register_skill(self, name, path, description="", frontmatter=None) -> None:
        self.skills[name] = {
            "path": Path(path),
            "description": description,
            "frontmatter": dict(frontmatter or {}),
        }


class AuthoringToolTests(unittest.TestCase):
    def test_registers_plugin_owned_authoring_tools_and_skills(self) -> None:
        ctx = FakeContext()
        PLUGIN.register(ctx)

        expected_tools = {
            "hermes_langchain_run_workflow",
            "hermes_langchain_begin_authoring",
            "kwc_advance_triage",
            "kwc_recover_frozen_triage",
            "kwc_create_triage",
            "kwc_create_impl_graph",
            "kwc_create_review",
            "kwc_create_summary",
        }
        self.assertTrue(expected_tools.issubset(ctx.tools))
        self.assertIn("kwc-orchestrator-contract", ctx.skills)
        self.assertIn("kwc-implementation-delivery-runtime", ctx.skills)
        self.assertIn("kwc-review-delivery-runtime", ctx.skills)
        self.assertNotIn("kwc_create_card", ctx.tools)

    def test_begin_authoring_has_closed_start_contract(self) -> None:
        ctx = FakeContext()
        PLUGIN.register(ctx)

        parameters = ctx.schemas["hermes_langchain_begin_authoring"]["parameters"]
        self.assertEqual(
            ["board", "issue_number", "generation", "workspace_path"],
            parameters["required"],
        )
        self.assertFalse(parameters["additionalProperties"])

    def test_begin_authoring_persists_controller_owned_assignment_without_launcher(self) -> None:
        captured = {}
        assignment = {
            "schema": "kwc-assignment-v1",
            "assignment_id": "kwa_intake",
            "role": "orchestrator",
            "phase": "triage",
            "triage_stage": "intake",
        }

        def begin_authoring(**kwargs):
            captured.update(kwargs)
            return assignment

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            PLUGIN,
            "_load_authoring_service",
            return_value=SimpleNamespace(begin_authoring=begin_authoring),
        ), patch.object(
            PLUGIN, "_state_path", return_value=Path(tmp) / "state.db"
        ), patch.object(
            PLUGIN, "_current_session_key", return_value="controller-session"
        ):
            result = json.loads(
                PLUGIN._hermes_langchain_begin_authoring_tool(
                    {
                        "board": "issue-151",
                        "issue_number": 151,
                        "generation": 1,
                        "workspace_path": "C:/repo",
                    }
                )
            )

        self.assertTrue(result["ok"])
        self.assertEqual("kwa_intake", result["assignment_id"])
        self.assertEqual("controller-session", captured["controller_session_key"])
        self.assertNotIn("profile", captured)
        self.assertNotIn("launcher", captured)

    def test_begin_authoring_uses_deterministic_session_when_host_has_none(self) -> None:
        captured = {}

        def begin_authoring(**kwargs):
            captured.update(kwargs)
            return {"assignment_id": "kwa_intake", "role": "orchestrator"}

        with patch.object(
            PLUGIN,
            "_load_authoring_service",
            return_value=SimpleNamespace(begin_authoring=begin_authoring),
        ), patch.object(
            PLUGIN, "_state_path", return_value=Path("state.db")
        ), patch.object(
            PLUGIN, "_current_session_key", return_value=""
        ):
            result = json.loads(
                PLUGIN._hermes_langchain_begin_authoring_tool(
                    {
                        "board": "issue-151",
                        "issue_number": 151,
                        "generation": 3,
                        "workspace_path": "C:/repo",
                    }
                )
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            "kwc-authoring:issue-151:g3",
            captured["controller_session_key"],
        )

    def test_advance_triage_validates_then_persists_with_orchestrator_role(self) -> None:
        assignment = {
            "assignment_id": "kwa_intake",
            "phase": "triage",
            "triage_stage": "intake",
            "role": "orchestrator",
        }
        captured = {}
        service = SimpleNamespace(
            read_assignment_by_id=lambda **_kwargs: assignment,
            session_owns_assignment=lambda **_kwargs: True,
            complete_triage_stage=lambda **kwargs: captured.update(kwargs)
            or {**assignment, "triage_stage": "discovery"},
        )
        workflow = SimpleNamespace(
            advance_stage=lambda **_kwargs: {
                "ok": True,
                "artifact": {"path": "canonical-intake.json", "sha256": "abc"},
            }
        )
        with patch.object(PLUGIN, "_load_authoring_service", return_value=service), patch.object(
            PLUGIN, "_load_triage_workflow", return_value=workflow
        ), patch.object(
            PLUGIN, "_state_path", return_value=Path("state.db")
        ), patch.object(
            PLUGIN, "_current_session_identifiers", return_value=["controller-session"]
        ):
            result = json.loads(
                PLUGIN._kwc_advance_triage_tool(
                    {"assignment_id": "kwa_intake", "artifact_file": ".temp/intake.json"}
                )
            )

        self.assertTrue(result["ok"])
        self.assertEqual("orchestrator", captured["caller_role"])
        self.assertEqual("controller-session", captured["caller_session_key"])
        self.assertEqual("discovery", result["next_assignment"]["triage_stage"])

    def test_materialization_executes_with_host_profile_not_workflow_profile(self) -> None:
        assignment = {
            "assignment_id": "kwa_impl",
            "phase": "impl",
            "role": "orchestrator",
        }
        captured = {}
        service = SimpleNamespace(
            read_assignment_by_id=lambda **_kwargs: assignment,
            session_owns_assignment=lambda **_kwargs: True,
            complete_phase=lambda **kwargs: {
                "phase": "review",
                "created": kwargs["created_task_ids"],
            },
        )

        def execute_authoring_phase(**kwargs):
            captured.update(kwargs)
            return {"ok": True, "created_task_ids": ["t_impl"]}

        with patch.object(PLUGIN, "_load_authoring_service", return_value=service), patch.object(
            PLUGIN,
            "_load_authoring_executor",
            return_value=SimpleNamespace(execute_authoring_phase=execute_authoring_phase),
        ), patch.object(
            PLUGIN, "_state_path", return_value=Path("state.db")
        ), patch.object(
            PLUGIN, "_current_session_identifiers", return_value=["controller-session"]
        ), patch.object(
            PLUGIN, "_host_profile", return_value="main"
        ):
            result = json.loads(
                PLUGIN._kwc_authoring_tool(
                    "impl",
                    {"assignment_id": "kwa_impl", "batch_file": ".temp/batch.json"},
                )
            )

        self.assertTrue(result["ok"])
        self.assertEqual("main", captured["host_profile"])
        self.assertNotIn("profile_home", captured)
        self.assertEqual("review", result["next_assignment"]["phase"])

    def test_non_owner_session_is_rejected_before_native_execution(self) -> None:
        assignment = {
            "assignment_id": "kwa_impl",
            "phase": "impl",
            "role": "orchestrator",
        }
        service = SimpleNamespace(
            read_assignment_by_id=lambda **_kwargs: assignment,
            session_owns_assignment=lambda **_kwargs: False,
        )
        executor = SimpleNamespace(
            execute_authoring_phase=lambda **_kwargs: self.fail("must not execute")
        )
        with patch.object(PLUGIN, "_load_authoring_service", return_value=service), patch.object(
            PLUGIN, "_load_authoring_executor", return_value=executor
        ), patch.object(
            PLUGIN, "_state_path", return_value=Path("state.db")
        ), patch.object(
            PLUGIN, "_current_session_identifiers", return_value=["other-session"]
        ):
            result = json.loads(
                PLUGIN._kwc_authoring_tool(
                    "impl",
                    {"assignment_id": "kwa_impl", "batch_file": ".temp/batch.json"},
                )
            )

        self.assertFalse(result["ok"])
        self.assertIn("does not own", result["error"])

    def test_recovery_replaces_impl_source_under_same_controller_assignment(self) -> None:
        assignment = {
            "assignment_id": "kwa_impl",
            "phase": "impl",
            "role": "orchestrator",
            "board": "issue-151",
            "workspace_path": "C:/repo",
            "issue_number": 151,
            "generation": 1,
        }
        service = SimpleNamespace(
            read_assignment_by_id=lambda **_kwargs: assignment,
            session_owns_assignment=lambda **_kwargs: True,
            replace_impl_triage_source=lambda **kwargs: {
                **assignment,
                "sources": {"triage_id": kwargs["replacement_triage_id"]},
            },
        )
        activity = SimpleNamespace(
            recover_frozen_triage=lambda **_kwargs: {
                "valid": True,
                "verified": True,
                "previous_task_id": "t_old",
                "task_id": "t_new",
            }
        )
        with patch.object(PLUGIN, "_load_authoring_service", return_value=service), patch.object(
            PLUGIN, "_load_triage_activity", return_value=activity
        ), patch.object(
            PLUGIN, "_state_path", return_value=Path("state.db")
        ), patch.object(
            PLUGIN, "_current_session_identifiers", return_value=["controller-session"]
        ):
            result = json.loads(
                PLUGIN._kwc_recover_frozen_triage_tool(
                    {
                        "assignment_id": "kwa_impl",
                        "frozen_body_file": ".temp/frozen-triage.json",
                    }
                )
            )

        self.assertTrue(result["verified"])
        self.assertEqual("t_new", result["next_assignment"]["sources"]["triage_id"])


if __name__ == "__main__":
    unittest.main()
