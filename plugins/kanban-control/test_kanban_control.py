"""Unit and contract tests for Kanban Control."""

from __future__ import annotations

import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_ROOT = Path(__file__).parent
_SPEC = importlib.util.spec_from_file_location("kanban_control_plugin", _ROOT / "__init__.py")
assert _SPEC and _SPEC.loader
PLUGIN = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(PLUGIN)


class FakeContext:
    profile_name = "main"

    def __init__(self, settings=None, tool_result=None):
        self.settings = settings or {}
        self.tool_result = tool_result or json.dumps(
            {"ok": True, "task_id": "task-123", "status": "ready", "subscribed": True}
        )
        self.commands = []
        self.dispatches = []

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_command(self, name, **kwargs):
        self.commands.append((name, kwargs))

    def dispatch_tool(self, name, args):
        self.dispatches.append((name, args))
        return self.tool_result


def configured_settings(workspace: str) -> dict[str, str]:
    return {
        "manager_profile": "pm-alpha",
        "worker_profile": "worker-beta",
        "reviewer_profile": "reviewer-gamma",
        "board": "",
        "workspace": workspace,
    }


class KanbanControlTests(unittest.TestCase):
    def test_manifest_version_matches_latest_changelog_release(self) -> None:
        manifest = (_ROOT / "plugin.yaml").read_text(encoding="utf-8")
        changelog = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        manifest_version = re.search(r"(?m)^version:\s*([0-9]+\.[0-9]+\.[0-9]+)\s*$", manifest)
        latest_release = re.search(r"(?m)^## \[([0-9]+\.[0-9]+\.[0-9]+)\]", changelog)

        self.assertIsNotNone(manifest_version)
        self.assertIsNotNone(latest_release)
        self.assertEqual(manifest_version.group(1), latest_release.group(1))

    def test_native_kanban_tool_module_is_loaded_for_plugin_dispatch(self) -> None:
        with patch("importlib.import_module") as import_module:
            PLUGIN._ensure_kanban_tool_registered()

        import_module.assert_called_once_with("tools.kanban_tools")

    def test_registers_only_kcp_text_command(self) -> None:
        ctx = FakeContext()
        PLUGIN.register(ctx)

        self.assertEqual(len(ctx.commands), 1)
        name, kwargs = ctx.commands[0]
        self.assertEqual(name, "kcp")
        self.assertEqual(kwargs["argument_mode"], "text")
        self.assertEqual(kwargs["args_hint"], "[run] [--board <slug> | --new-board <slug>] <request>")

    def test_desktop_bridge_opens_worker_session_as_tab_without_visual_hud(self) -> None:
        source = (_ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")

        self.assertNotIn("COMPOSER_AREAS.top", source)
        self.assertNotIn("KCP HUD", source)
        self.assertIn("ctx.register({", source)
        self.assertNotIn("ctx.contribute(", source)
        self.assertIn("COMPOSER_AREAS.middleware", source)
        self.assertIn("host.openSession", source)
        self.assertIn("intent: 'tab'", source)

    def test_empty_request_is_rejected_but_run_keyword_is_optional(self) -> None:
        ctx = FakeContext()
        self.assertEqual(PLUGIN._handle_kcp(ctx, ""), PLUGIN._USAGE)
        self.assertEqual(PLUGIN._handle_kcp(ctx, "run"), PLUGIN._USAGE)
        self.assertEqual(ctx.dispatches, [])

    def test_request_is_preserved_without_run_keyword(self) -> None:
        raw_request = 'Issue #151을 구현해줘.\n\n"인용문"과   공백도 유지해.'
        request, board, create_board, run_id, error = PLUGIN._parse_run_options(raw_request)

        self.assertIsNone(error)
        self.assertEqual(request, raw_request)
        self.assertEqual(board, "")
        self.assertFalse(create_board)
        self.assertEqual(run_id, "")

    def test_request_outer_whitespace_is_preserved(self) -> None:
        raw_request = "  leading  spaces\nbody\n\n"
        request, board, create_board, run_id, error = PLUGIN._parse_run_options(raw_request)

        self.assertIsNone(error)
        self.assertEqual(request, raw_request)
        self.assertEqual(board, "")
        self.assertFalse(create_board)
        self.assertEqual(run_id, "")

    def test_optional_board_prefix_is_removed_without_rewriting_request(self) -> None:
        raw_request = '첫 줄\n\n둘째 줄의   공백과 "따옴표"'
        request, board, create_board, run_id, error = PLUGIN._parse_run_options(
            f'--new-board issue-151-test-2 {raw_request}'
        )

        self.assertIsNone(error)
        self.assertEqual(request, raw_request)
        self.assertEqual(board, "issue-151-test-2")
        self.assertTrue(create_board)
        self.assertEqual(run_id, "")

    def test_legacy_run_keyword_remains_compatible(self) -> None:
        request, board, create_board, run_id, error = PLUGIN._parse_run_options(
            "run --board issue-151 요청 본문"
        )

        self.assertIsNone(error)
        self.assertEqual(request, "요청 본문")
        self.assertEqual(board, "issue-151")
        self.assertFalse(create_board)
        self.assertEqual(run_id, "")

    def test_desktop_client_run_id_is_used_for_task_identity(self) -> None:
        request, board, create_board, run_id, error = PLUGIN._parse_run_options(
            "--run-id kcp-20261003T120000Z-a1b2c3d4 --board issue-151 요청 본문"
        )

        self.assertIsNone(error)
        self.assertEqual(request, "요청 본문")
        self.assertEqual(board, "issue-151")
        self.assertFalse(create_board)
        self.assertEqual(run_id, "kcp-20261003T120000Z-a1b2c3d4")

    def test_new_board_is_created_before_manager_card_registration(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            ctx = FakeContext(configured_settings(workspace))
            profiles = {"pm-alpha", "worker-beta", "reviewer-gamma"}
            with (
                patch.object(PLUGIN, "_installed_profile_names", return_value=profiles),
                patch.object(PLUGIN, "_has_session_notification_target", return_value=True),
                patch.object(PLUGIN, "_ensure_kanban_tool_registered"),
                patch.object(PLUGIN, "_create_new_board") as create_new_board,
            ):
                result = PLUGIN._handle_kcp(
                    ctx,
                    "--new-board issue-151-test-2 Issue #151을 구현해줘",
                )

        create_new_board.assert_called_once_with("issue-151-test-2", workspace)
        self.assertEqual(ctx.dispatches[0][1]["board"], "issue-151-test-2")
        body = json.loads(ctx.dispatches[0][1]["body"])
        self.assertEqual(body["control_board"], "issue-151-test-2")
        self.assertIn("board: issue-151-test-2", result)

    def test_configured_board_is_validated_before_manager_card_registration(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            settings = configured_settings(workspace)
            settings["board"] = "issue-151"
            ctx = FakeContext(settings)
            profiles = {"pm-alpha", "worker-beta", "reviewer-gamma"}
            with (
                patch.object(PLUGIN, "_installed_profile_names", return_value=profiles),
                patch.object(PLUGIN, "_has_session_notification_target", return_value=True),
                patch.object(PLUGIN, "_validate_board_target", return_value="configured board missing") as validate,
            ):
                result = PLUGIN._handle_kcp(ctx, "요청 본문")

        validate.assert_called_once_with("issue-151", False, workspace)
        self.assertEqual(result, "configured board missing")
        self.assertEqual(ctx.dispatches, [])

    def test_progress_message_does_not_claim_live_worker_streaming(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            ctx = FakeContext(configured_settings(workspace))
            profiles = {"pm-alpha", "worker-beta", "reviewer-gamma"}
            with (
                patch.object(PLUGIN, "_installed_profile_names", return_value=profiles),
                patch.object(PLUGIN, "_has_session_notification_target", return_value=True),
                patch.object(PLUGIN, "_ensure_kanban_tool_registered"),
            ):
                result = PLUGIN._handle_kcp(ctx, "run implement issue 151")

        self.assertIn("execution: separate project-manager worker session", result)
        self.assertIn("progress: terminal lifecycle notifications in this session", result)
        self.assertIn("not a live reasoning/tool stream", result)


    def test_missing_role_bindings_fail_before_mutation(self) -> None:
        ctx = FakeContext({"manager_profile": "pm-alpha"})
        result = PLUGIN._handle_kcp(ctx, "run implement issue 151")

        self.assertIn("worker_profile", result)
        self.assertIn("reviewer_profile", result)
        self.assertEqual(ctx.dispatches, [])

    def test_unknown_profile_fails_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            ctx = FakeContext(configured_settings(workspace))
            with patch.object(PLUGIN, "_installed_profile_names", return_value={"pm-alpha", "worker-beta"}):
                result = PLUGIN._handle_kcp(ctx, "run implement issue 151")

        self.assertIn("reviewer-gamma", result)
        self.assertEqual(ctx.dispatches, [])

    def test_live_session_is_required_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            ctx = FakeContext(configured_settings(workspace))
            profiles = {"pm-alpha", "worker-beta", "reviewer-gamma"}
            with (
                patch.object(PLUGIN, "_installed_profile_names", return_value=profiles),
                patch.object(PLUGIN, "_has_session_notification_target", return_value=False),
            ):
                result = PLUGIN._handle_kcp(ctx, "run implement issue 151")

        self.assertIn("live Hermes Desktop", result)
        self.assertEqual(ctx.dispatches, [])

    def test_run_creates_manager_card_with_configured_profile_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            settings = configured_settings(workspace)
            settings["board"] = "issue-151"
            ctx = FakeContext(settings)
            profiles = {"pm-alpha", "worker-beta", "reviewer-gamma"}
            with (
                patch.object(PLUGIN, "_installed_profile_names", return_value=profiles),
                patch.object(PLUGIN, "_has_session_notification_target", return_value=True),
                patch.object(PLUGIN, "_validate_board_target", return_value=None),
                patch.object(PLUGIN, "_ensure_kanban_tool_registered"),
            ):
                result = PLUGIN._handle_kcp(ctx, "run Issue #151을 구현해줘")

            self.assertEqual(len(ctx.dispatches), 1)
            tool, args = ctx.dispatches[0]
            self.assertEqual(tool, "kanban_create")
            self.assertEqual(args["assignee"], "pm-alpha")
            self.assertEqual(args["board"], "issue-151")
            self.assertEqual(args["workspace_kind"], "dir")
            self.assertEqual(Path(args["workspace_path"]), Path(workspace).resolve())
            self.assertEqual(args["completion_contract"], "local-only")
            self.assertTrue(args["idempotency_key"].startswith("kcp-"))

            body = json.loads(args["body"])
            self.assertEqual(body["schema"], "kanban-control/run-request@1")
            self.assertEqual(body["request"], "Issue #151을 구현해줘")
            self.assertEqual(
                body["profile_bindings"],
                {"manager": "pm-alpha", "worker": "worker-beta", "reviewer": "reviewer-gamma"},
            )
            self.assertTrue(
                any("progress delivery propagates" in rule for rule in body["execution_contract"]["rules"])
            )
            self.assertTrue(
                any("never create parallel sibling cards" in rule for rule in body["execution_contract"]["rules"])
            )
            self.assertTrue(
                any("At most one card" in rule for rule in body["execution_contract"]["rules"])
            )
            self.assertTrue(
                any("aggregate-review-admission-v1" in rule for rule in body["execution_contract"]["rules"])
            )
            self.assertIn("task-123", result)
            self.assertIn("pm-alpha", result)
            self.assertIn("progress: terminal lifecycle notifications in this session", result)

    def test_project_mode_does_not_set_shared_workspace(self) -> None:
        settings = configured_settings("")
        settings["project"] = "e-commerce"
        ctx = FakeContext(settings)
        profiles = {"pm-alpha", "worker-beta", "reviewer-gamma"}
        with (
            patch.object(PLUGIN, "_installed_profile_names", return_value=profiles),
            patch.object(PLUGIN, "_has_session_notification_target", return_value=True),
            patch.object(PLUGIN, "_ensure_kanban_tool_registered"),
        ):
            PLUGIN._handle_kcp(ctx, "run implement issue 151")

        args = ctx.dispatches[0][1]
        self.assertEqual(args["project"], "e-commerce")
        self.assertNotIn("workspace_kind", args)
        self.assertNotIn("workspace_path", args)

    def test_invalid_kanban_response_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            ctx = FakeContext(configured_settings(workspace), tool_result="not-json")
            profiles = {"pm-alpha", "worker-beta", "reviewer-gamma"}
            with (
                patch.object(PLUGIN, "_installed_profile_names", return_value=profiles),
                patch.object(PLUGIN, "_has_session_notification_target", return_value=True),
                patch.object(PLUGIN, "_ensure_kanban_tool_registered"),
            ):
                result = PLUGIN._handle_kcp(ctx, "run implement issue 151")

        self.assertIn("invalid kanban_create response", result)

    def test_subscription_failure_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            tool_result = json.dumps(
                {"ok": True, "task_id": "task-123", "status": "ready", "subscribed": False}
            )
            ctx = FakeContext(configured_settings(workspace), tool_result=tool_result)
            profiles = {"pm-alpha", "worker-beta", "reviewer-gamma"}
            with (
                patch.object(PLUGIN, "_installed_profile_names", return_value=profiles),
                patch.object(PLUGIN, "_has_session_notification_target", return_value=True),
                patch.object(PLUGIN, "_ensure_kanban_tool_registered"),
            ):
                result = PLUGIN._handle_kcp(ctx, "run implement issue 151")

        self.assertIn("progress subscription failed", result)
        self.assertIn("progress: not subscribed", result)


if __name__ == "__main__":
    unittest.main()
