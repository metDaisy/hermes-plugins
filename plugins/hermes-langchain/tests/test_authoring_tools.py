"""Tool-contract tests for KWC PM card authoring."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


_ROOT = Path(__file__).resolve().parents[1]
from tests.support import load_module

PLUGIN = load_module("plugin.entrypoint")


class FakeContext:
    def __init__(self) -> None:
        self.tools = {}
        self.skills = {}
        self.schemas = {}
        self.tool_checks = {}
        self.hooks = {}
        self.profile_name = "project-manager"

    def register_hook(self, name, callback) -> None:
        self.hooks[name] = callback

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

    def get_config(self, _key, default=None):
        return default


class AuthoringToolTests(unittest.TestCase):
    def test_registers_staged_triage_and_four_materialization_tools(self) -> None:
        ctx = FakeContext()
        PLUGIN.register(ctx)
        expected = {
            "kwc_advance_triage",
            "kwc_recover_frozen_triage",
            "kwc_create_triage",
            "kwc_create_impl_graph",
            "kwc_create_review",
            "kwc_create_summary",
        }
        self.assertTrue(expected.issubset(ctx.tools))
        self.assertNotIn("kwc_create_card", ctx.tools)

    def test_recovery_tool_replaces_impl_source_after_verified_native_recovery(self) -> None:
        assignment = {
            "schema": "kwc-assignment-v1",
            "assignment_id": "kwa_impl",
            "phase": "impl",
            "profile": "project-manager",
            "workspace_path": "C:/repo",
            "issue_number": 151,
            "sources": {"triage_id": "t_oldtriage", "impl_ids": []},
        }
        service = SimpleNamespace(
            read_assignment_by_id=lambda **_kwargs: assignment,
            session_owns_assignment=lambda **_kwargs: True,
            replace_impl_triage_source=lambda **kwargs: {
                **assignment,
                "assignment_id": "kwa_impl_recovered",
                "sources": {"triage_id": kwargs["replacement_triage_id"], "impl_ids": []},
            },
        )
        activity = SimpleNamespace(
            recover_frozen_triage=lambda **_kwargs: {
                "valid": True,
                "verified": True,
                "previous_task_id": "t_oldtriage",
                "task_id": "t_newtriage",
                "frozen_digest": "digest",
            }
        )
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            PLUGIN, "_load_authoring_service", return_value=service
        ), patch.object(PLUGIN, "_load_triage_activity", return_value=activity), patch.object(
            PLUGIN, "_state_path", return_value=Path(tmp) / "state.db"
        ), patch.object(
            PLUGIN, "_current_session_identifiers", return_value=["pm-session"]
        ), patch.object(
            PLUGIN, "_current_session_profile", return_value="project-manager"
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
        self.assertEqual("t_newtriage", result["next_assignment"]["sources"]["triage_id"])

    def test_triage_tools_accept_one_stage_artifact_then_materialize_without_pm_paths(self) -> None:
        ctx = FakeContext()
        PLUGIN.register(ctx)
        parameters = ctx.schemas["kwc_create_triage"]["parameters"]

        self.assertNotIn("authoring_file", parameters["properties"])
        self.assertNotIn("frozen_body_file", parameters["properties"])
        self.assertEqual(["assignment_id"], parameters["required"])
        advance = ctx.schemas["kwc_advance_triage"]["parameters"]
        self.assertEqual(["assignment_id", "artifact_file"], advance["required"])

    def test_authoring_tool_visibility_does_not_depend_on_session_context(self) -> None:
        ctx = FakeContext()
        PLUGIN.register(ctx)

        self.assertIsNone(ctx.tool_checks["kwc_advance_triage"])
        self.assertIsNone(ctx.tool_checks["kwc_create_triage"])

    def test_advance_triage_validates_artifact_before_persisting_next_stage(self) -> None:
        assignment = {
            "schema": "kwc-assignment-v1",
            "assignment_id": "kwa_intake",
            "phase": "triage",
            "triage_stage": "intake",
            "profile": "project-manager",
        }
        workflow = SimpleNamespace(
            advance_stage=lambda **kwargs: {
                "ok": True,
                "stage": "intake",
                "artifact": {"path": "canonical-intake.json", "sha256": "abc"},
            }
        )
        service = SimpleNamespace(
            read_assignment_by_id=lambda **_kwargs: assignment,
            session_owns_assignment=lambda **_kwargs: True,
            complete_triage_stage=lambda **kwargs: {
                **assignment,
                "assignment_id": "kwa_discovery",
                "triage_stage": "discovery",
                "completed_artifact": kwargs["artifact"],
            },
        )
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            PLUGIN, "_load_authoring_service", return_value=service
        ), patch.object(PLUGIN, "_load_triage_workflow", return_value=workflow), patch.object(
            PLUGIN, "_state_path", return_value=Path(tmp) / "state.db"
        ), patch.object(PLUGIN, "_current_session_profile", return_value="project-manager"), patch.object(
            PLUGIN, "_current_session_key", return_value="pm-session"
        ):
            result = json.loads(
                PLUGIN._kwc_advance_triage_tool(
                    {"assignment_id": "kwa_intake", "artifact_file": ".temp/intake.json"}
                )
            )

        self.assertTrue(result["ok"])
        self.assertEqual("discovery", result["next_assignment"]["triage_stage"])
        self.assertEqual("canonical-intake.json", result["next_assignment"]["completed_artifact"]["path"])

    def test_phase_tool_executes_then_advances_the_fenced_assignment(self) -> None:
        assignment = {
            "schema": "kwc-assignment-v1",
            "assignment_id": "kwa_triage",
            "phase": "triage",
            "triage_stage": "materialize",
            "profile": "project-manager",
        }
        executor = SimpleNamespace(
            execute_authoring_phase=lambda **kwargs: {
                "ok": True,
                "phase": "triage",
                "created_task_ids": ["t_123abc"],
                "assignment": kwargs["assignment"],
            }
        )
        service = SimpleNamespace(
            read_assignment_by_id=lambda **_kwargs: assignment,
            session_owns_assignment=lambda **_kwargs: True,
            complete_phase=lambda **kwargs: {
                "schema": "kwc-assignment-v1",
                "assignment_id": "kwa_impl",
                "phase": "impl",
                "allowed_tools": ["kwc_create_impl_graph"],
                "completed_ids": kwargs["created_task_ids"],
            },
        )
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            PLUGIN, "_load_authoring_service", return_value=service
        ), patch.object(PLUGIN, "_load_authoring_executor", return_value=executor), patch.object(
            PLUGIN,
            "_load_triage_workflow",
            return_value=SimpleNamespace(
                materialization_arguments=lambda _assignment: {
                    "authoring_file": "canonical-authoring.json",
                    "current_state_inspection_file": "inspection.json",
                }
            ),
        ), patch.object(
            PLUGIN, "_state_path", return_value=Path(tmp) / "state.db"
        ), patch.object(PLUGIN, "_current_session_profile", return_value="project-manager"), patch.object(
            PLUGIN, "_current_session_key", return_value="pm-session"
        ):
            result = json.loads(
                PLUGIN._kwc_authoring_tool(
                    "triage",
                    {"assignment_id": "kwa_triage"},
                )
            )

        self.assertTrue(result["ok"])
        self.assertEqual(["t_123abc"], result["created_task_ids"])
        self.assertEqual("impl", result["next_assignment"]["phase"])

    def test_phase_tool_rejects_cross_phase_use_before_native_execution(self) -> None:
        assignment = {
            "schema": "kwc-assignment-v1",
            "assignment_id": "kwa_impl",
            "phase": "impl",
            "profile": "project-manager",
        }
        executor = SimpleNamespace(execute_authoring_phase=lambda **_kwargs: self.fail("must not run"))
        service = SimpleNamespace(
            read_assignment_by_id=lambda **_kwargs: assignment,
            session_owns_assignment=lambda **_kwargs: True,
        )
        with patch.object(PLUGIN, "_load_authoring_service", return_value=service), patch.object(
            PLUGIN, "_load_authoring_executor", return_value=executor
        ), patch.object(
            PLUGIN, "_state_path", return_value=Path("state.db")
        ), patch.object(PLUGIN, "_current_session_profile", return_value="project-manager"), patch.object(
            PLUGIN, "_current_session_key", return_value="pm-session"
        ):
            result = json.loads(
                PLUGIN._kwc_authoring_tool("triage", {"assignment_id": "kwa_impl"})
            )
        self.assertFalse(result["ok"])
        self.assertIn("phase", result["error"])

    def test_phase_tool_rejects_non_owner_session_before_native_execution(self) -> None:
        assignment = {
            "schema": "kwc-assignment-v1",
            "assignment_id": "kwa_triage",
            "phase": "triage",
            "triage_stage": "materialize",
            "profile": "project-manager",
        }
        executor = SimpleNamespace(execute_authoring_phase=lambda **_kwargs: self.fail("must not run"))
        service = SimpleNamespace(
            read_assignment_by_id=lambda **_kwargs: assignment,
            session_owns_assignment=lambda **_kwargs: False,
        )
        with patch.object(PLUGIN, "_load_authoring_service", return_value=service), patch.object(
            PLUGIN, "_load_authoring_executor", return_value=executor
        ), patch.object(
            PLUGIN, "_state_path", return_value=Path("state.db")
        ), patch.object(PLUGIN, "_current_session_profile", return_value=""), patch.object(
            PLUGIN, "_current_session_key", return_value="other-session"
        ):
            result = json.loads(
                PLUGIN._kwc_authoring_tool("triage", {"assignment_id": "kwa_triage"})
            )

        self.assertFalse(result["ok"])
        self.assertIn("does not own", result["error"])

    def test_phase_tool_records_current_session_when_parent_context_is_inherited(self) -> None:
        assignment = {
            "schema": "kwc-assignment-v1",
            "assignment_id": "kwa_triage",
            "phase": "triage",
            "triage_stage": "materialize",
            "profile": "project-manager",
        }
        executor = SimpleNamespace(
            execute_authoring_phase=lambda **_kwargs: {
                "ok": True,
                "phase": "triage",
                "created_task_ids": ["t_123abc"],
            }
        )
        service = SimpleNamespace(
            read_assignment_by_id=lambda **_kwargs: assignment,
            session_owns_assignment=lambda **kwargs: kwargs["caller_session_key"] == "pm-session",
            complete_phase=lambda **kwargs: {
                "phase": "impl",
                "owner": kwargs["caller_session_key"],
            },
        )
        with patch.object(PLUGIN, "_load_authoring_service", return_value=service), patch.object(
            PLUGIN, "_load_authoring_executor", return_value=executor
        ), patch.object(
            PLUGIN,
            "_load_triage_workflow",
            return_value=SimpleNamespace(materialization_arguments=lambda _assignment: {}),
        ), patch.object(
            PLUGIN, "_state_path", return_value=Path("state.db")
        ), patch.object(
            PLUGIN,
            "_current_session_identifiers",
            return_value=["parent-session", "pm-session"],
        ), patch.object(
            PLUGIN, "_current_session_profile", return_value="project-manager"
        ):
            result = json.loads(
                PLUGIN._kwc_authoring_tool("triage", {"assignment_id": "kwa_triage"})
            )

        self.assertTrue(result["ok"])
        self.assertEqual("parent-session", result["next_assignment"]["owner"])


if __name__ == "__main__":
    unittest.main()
