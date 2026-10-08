"""Contract tests for Kanban Workflow Controller."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch


_ROOT = Path(__file__).parent
_SPEC = importlib.util.spec_from_file_location(
    "kanban_workflow_controller_plugin", _ROOT / "__init__.py"
)
assert _SPEC and _SPEC.loader
PLUGIN = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(PLUGIN)
COMMENT_SERVICE = PLUGIN._load_sibling_module(
    "_hermes_kanban_workflow_controller_comment_service", "comment_service.py"
)

_API_SPEC = importlib.util.spec_from_file_location(
    "kanban_workflow_controller_api", _ROOT / "dashboard" / "plugin_api.py"
)
assert _API_SPEC and _API_SPEC.loader
PLUGIN_API = importlib.util.module_from_spec(_API_SPEC)
_fastapi = types.ModuleType("fastapi")


class _FakeRouter:
    def get(self, _path):
        return lambda function: function

    def post(self, _path):
        return lambda function: function


class _FakeBackgroundTasks:
    def __init__(self) -> None:
        self.tasks: list[tuple[object, tuple, dict]] = []

    def add_task(self, function, *args, **kwargs) -> None:
        self.tasks.append((function, args, kwargs))


class _FakeHTTPException(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


_fastapi.APIRouter = _FakeRouter
_fastapi.BackgroundTasks = _FakeBackgroundTasks
_fastapi.HTTPException = _FakeHTTPException
with patch.dict(sys.modules, {"fastapi": _fastapi}):
    _API_SPEC.loader.exec_module(PLUGIN_API)


class FakeContext:
    def __init__(self, settings=None, *, profile_name="project-manager") -> None:
        self.hooks: dict[str, object] = {}
        self.tools: dict[str, object] = {}
        self.tool_checks: dict[str, object] = {}
        self.settings = settings or {}
        self.profile_name = profile_name
        self.injected: list[tuple[str, str]] = []

    def register_hook(self, name: str, callback) -> None:
        self.hooks[name] = callback

    def register_tool(self, name: str, _toolset: str, _schema: dict, handler, **_kwargs) -> None:
        self.tools[name] = handler
        self.tool_checks[name] = _kwargs.get("check_fn")

    def get_config(self, key: str, default=None):
        return self.settings.get(key, default)

    def inject_message(self, content: str, *, session_key: str) -> bool:
        self.injected.append((session_key, content))
        return True


class KanbanWorkflowControllerContractTests(unittest.TestCase):
    def _seed_state(self, path: Path, *, session_key: str = "gateway-session", cursor: int = 41) -> None:
        with patch.object(PLUGIN, "_state_path", return_value=path):
            PLUGIN._bind_state("issue151", session_key, cursor)

    def _read_state(self, path: Path):
        with patch.object(PLUGIN, "_state_path", return_value=path):
            return PLUGIN._read_state("issue151")

    def test_manifests_and_changelog_share_version(self) -> None:
        manifest = (_ROOT / "plugin.yaml").read_text(encoding="utf-8")
        dashboard_manifest = json.loads(
            (_ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8")
        )
        changelog = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertRegex(manifest, r"(?m)^name:\s*kanban-workflow-controller\s*$")
        manifest_version = re.search(r"(?m)^version:\s*([^\s#]+)", manifest)
        self.assertIsNotNone(manifest_version)
        version = manifest_version.group(1)
        self.assertNotIn("  - kanban_task_commented", manifest)
        self.assertIn("  - kwc_comment", manifest)
        self.assertIn("  - kwc_reply", manifest)
        self.assertIn("  - kwc_pm_response", manifest)
        self.assertIn("  - kwc_profile_response", manifest)
        self.assertNotRegex(manifest, r"(?m)^  board:\s*$")
        self.assertEqual(dashboard_manifest["name"], "kanban-workflow-controller")
        self.assertEqual(dashboard_manifest["version"], version)
        self.assertEqual(dashboard_manifest["api"], "plugin_api.py")
        latest_release = re.search(r"(?m)^## \[([^]]+)\]", changelog)
        self.assertIsNotNone(latest_release)
        self.assertEqual(latest_release.group(1), version)

    def test_status_api_reads_the_canonical_plugin_manifest_version(self) -> None:
        self.assertTrue(hasattr(PLUGIN_API, "_plugin_version"))
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "plugin.yaml"
            manifest.write_text("name: test\nversion: 9.8.7\n", encoding="utf-8")
            self.assertEqual(PLUGIN_API._plugin_version(manifest), "9.8.7")

    def test_standalone_import_defers_service_modules_until_tool_use(self) -> None:
        code = f"""
import importlib.util, json, sys
path = {str(_ROOT / '__init__.py')!r}
spec = importlib.util.spec_from_file_location('kwc_startup_probe', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(json.dumps({{
    'comment': '_hermes_kanban_workflow_controller_comment_service' in sys.modules,
    'invocation': '_hermes_kanban_workflow_controller_invocation_service' in sys.modules,
}}))
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(result.stdout), {"comment": False, "invocation": False})

    def test_desktop_status_panel_contract(self) -> None:
        desktop = (_ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
        self.assertIn("id: 'controller-status'", desktop)
        self.assertIn("area: 'statusBar.right'", desktop)
        self.assertIn("ctx.rest('/runtime/lease'", desktop)
        self.assertIn("method: 'POST'", desktop)
        self.assertIn("ctx.rest('/status'", desktop)
        self.assertIn("children: 'KWC'", desktop)
        self.assertIn("children: `(${label})`", desktop)
        self.assertRegex(desktop, r"running[\s\S]*text-\(--ui-success\)")
        self.assertRegex(desktop, r"stopped[\s\S]*text-\(--ui-danger(?:,[^)]+)?\)")
        self.assertIn("ROUTES_AREA", desktop)
        self.assertIn("SIDEBAR_NAV_AREA", desktop)
        self.assertIn("ctx.rest('/activity'", desktop)
        self.assertIn("label: 'KWC logs'", desktop)
        self.assertIn("ctx.registerMany([", desktop)
        self.assertNotIn("ctx.register([", desktop)

    def test_readme_states_native_composer_coverage_limit(self) -> None:
        readme = (_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("built-in Kanban comment composer", readme)
        self.assertIn("does not call the KWC endpoint", readme)
        self.assertIn("`review_requested` is not routed by KWC", readme)
        self.assertIn("`aggregate-review-card-v3`", readme)
        self.assertIn("Reviewer `completed`", readme)
        self.assertIn("schema-tag-matched", readme)

    def test_readme_requires_typed_response_before_terminal_handoff(self) -> None:
        readme = (_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("submit the typed response before the terminal Kanban mutation", readme)
        self.assertIn("matching canonical task/run handoff", readme)
        self.assertNotIn(
            "canonical task transition before submitting the typed response", readme
        )

    def test_activity_api_returns_privacy_safe_recent_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            with (
                patch.object(PLUGIN, "_state_path", return_value=state_path),
                patch.object(PLUGIN, "_now", return_value=1000),
            ):
                PLUGIN._record_activity(
                    board="issue151",
                    action="event_routed",
                    outcome="accepted",
                    reason="actionable_worker_event",
                    task_id="t_impl",
                    source_event_id=42,
                    event_kind="blocked",
                )
            with patch.object(PLUGIN_API, "_state_path", return_value=state_path):
                response = PLUGIN_API.activity(limit=20)
        self.assertEqual(response["total"], 1)
        self.assertEqual(response["items"][0]["action"], "event_routed")
        self.assertEqual(response["items"][0]["task_id"], "t_impl")
        self.assertNotIn("message", response["items"][0])
        self.assertNotIn("session_key", response["items"][0])
        self.assertNotIn("payload", response["items"][0])

    def test_comment_endpoint_uses_native_kwc_request_and_schedules_immediate_nudge(self) -> None:
        background = _FakeBackgroundTasks()
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(PLUGIN_API, "_state_path", return_value=Path(tmp) / "state.db"),
                patch.object(
                    PLUGIN_API,
                    "_create_kwc_comment_request",
                    return_value={
                        "ok": True,
                        "board": "issue151",
                        "task_id": "t_impl",
                        "comment_id": 12,
                        "delivery_id": 5,
                    },
                ) as create,
                patch.object(PLUGIN_API, "_nudge_dispatch") as nudge,
            ):
                response = PLUGIN_API.create_comment(
                    {
                        "board": "issue151",
                        "task_id": "t_impl",
                        "body": "@{reviewer} 확인해주세요",
                        "idempotency_key": "ui-comment-12",
                        "author": "forged-author",
                    },
                    background,
                )

        self.assertEqual(response["comment_id"], 12)
        self.assertEqual(create.call_args.kwargs["author"], "desktop")
        self.assertEqual(create.call_args.kwargs["idempotency_key"], "ui-comment-12")
        self.assertTrue(create.call_args.kwargs["route_requested"])
        self.assertEqual(len(background.tasks), 1)
        self.assertIs(background.tasks[0][0], nudge)
        self.assertEqual(background.tasks[0][1], ("issue151",))

    def test_event_hint_without_binding_records_why_it_did_nothing(self) -> None:
        ctx = FakeContext({"board": "issue151"})
        PLUGIN.register(ctx)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            with patch.object(PLUGIN, "_state_path", return_value=state_path):
                ctx.hooks["kanban_task_blocked"](
                    board="issue151", task_id="t_impl", run_id=4, profile_name="coder"
                )
                rows = PLUGIN._read_activity(limit=20)
        self.assertEqual(rows[0]["action"], "event_hint_skipped")
        self.assertEqual(rows[0]["reason"], "no_coordinator_binding")
        self.assertEqual(rows[0]["task_id"], "t_impl")

    def test_status_api_tracks_recent_dispatch_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            with (
                patch.object(PLUGIN, "_state_path", return_value=state_path),
                patch.object(PLUGIN, "_now", return_value=1000),
            ):
                PLUGIN._mark_dispatch_heartbeat("issue151")
                PLUGIN._mark_dispatch_heartbeat("issue152")
            with (
                patch.object(PLUGIN_API, "_state_path", return_value=state_path),
                patch.object(PLUGIN_API, "_now", return_value=1010),
            ):
                response = PLUGIN_API.status()
        self.assertTrue(response["dispatcher_running"])
        self.assertFalse(response["running"])
        self.assertEqual(response["version"], PLUGIN_API._plugin_version())
        self.assertEqual(set(response["boards"]), {"issue151", "issue152"})
        self.assertEqual(response["last_dispatch_tick_at"], 1000)

    def test_status_api_stops_after_dispatch_heartbeat_expires(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            with (
                patch.object(PLUGIN, "_state_path", return_value=state_path),
                patch.object(PLUGIN, "_now", return_value=1000),
            ):
                PLUGIN._mark_dispatch_heartbeat("issue151")
            with (
                patch.object(PLUGIN_API, "_state_path", return_value=state_path),
                patch.object(PLUGIN_API, "_now", return_value=1200),
            ):
                response = PLUGIN_API.status()
        self.assertFalse(response["running"])

    def test_registers_only_passive_observer_hooks(self) -> None:
        ctx = FakeContext()
        PLUGIN.register(ctx)
        self.assertEqual(
            set(ctx.hooks),
            {
                "pre_llm_call",
                "kanban_task_completed",
                "kanban_task_blocked",
                "on_kanban_worker_exited",
                "on_kanban_worker_stale_claim",
                "on_kanban_dispatch_tick",
            },
        )

    def test_cli_profile_response_falls_back_to_the_durable_session_id(self) -> None:
        gateway_module = types.ModuleType("gateway")
        session_context = types.ModuleType("gateway.session_context")
        session_context.get_session_env = lambda key, default="": {
            "HERMES_SESSION_KEY": "",
            "HERMES_SESSION_ID": "coder-session-id",
        }.get(key, default)
        with patch.dict(
            sys.modules,
            {"gateway": gateway_module, "gateway.session_context": session_context},
        ):
            self.assertEqual(PLUGIN._current_session_key(), "coder-session-id")

    def test_pm_response_tool_submits_profile_and_session_bound_result(self) -> None:
        ctx = FakeContext()
        PLUGIN.register(ctx)
        self.assertIn("kwc_pm_response", ctx.tools)
        with (
            patch.object(PLUGIN, "_state_path", return_value=Path("state.db")),
            patch.object(PLUGIN, "_current_session_profile", return_value="project-manager"),
            patch.object(PLUGIN, "_current_session_key", return_value="pm-session-key"),
            patch.object(
                PLUGIN,
                "_submit_pm_response",
                return_value={
                    "ok": True,
                    "invocation_id": "inv_pm_1",
                    "status": "accepted",
                    "idempotent_replay": False,
                },
            ) as submit,
        ):
            result = json.loads(
                ctx.tools["kwc_pm_response"](
                    {
                        "invocation_id": "inv_pm_1",
                        "outcome": "checkpoint-approved",
                        "task_id": "t_impl1",
                        "reason_code": "VERIFICATION_PASSED",
                    }
                )
            )

        self.assertTrue(result["ok"])
        submit.assert_called_once_with(
            state_path=Path("state.db"),
            invocation_id="inv_pm_1",
            caller_profile="project-manager",
            caller_session_key="pm-session-key",
            outcome="checkpoint-approved",
            task_id="t_impl1",
            reason_code="VERIFICATION_PASSED",
        )

    def test_pm_response_tool_is_visible_only_to_the_coordinator_profile(self) -> None:
        coder = FakeContext(profile_name="coder")
        PLUGIN.register(coder)
        self.assertIn("kwc_pm_response", coder.tools)
        self.assertIsNotNone(coder.tool_checks["kwc_pm_response"])
        with patch.object(PLUGIN, "_current_session_profile", return_value="coder"):
            self.assertFalse(coder.tool_checks["kwc_pm_response"]())
        with patch.object(PLUGIN, "_current_session_profile", return_value="project-manager"):
            self.assertTrue(coder.tool_checks["kwc_pm_response"]())

    def test_worker_state_path_can_be_explicitly_bound_by_the_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            expected = Path(tmp) / "kwc" / "state.db"
            with patch.dict(
                os.environ,
                {"HERMES_KWC_STATE_PATH": str(expected)},
                clear=False,
            ):
                self.assertEqual(PLUGIN._state_path(), expected)

    def test_cli_profile_identity_falls_back_to_active_profile_environment(self) -> None:
        session_context = types.ModuleType("gateway.session_context")
        session_context.get_session_env = lambda _name, default="": default
        gateway = types.ModuleType("gateway")
        gateway.session_context = session_context
        with (
            patch.dict(
                sys.modules,
                {"gateway": gateway, "gateway.session_context": session_context},
            ),
            patch.dict(os.environ, {"HERMES_PROFILE": "coder"}, clear=False),
        ):
            self.assertEqual(PLUGIN._current_session_profile(), "coder")

    def test_profile_response_tool_submits_profile_and_session_bound_result(self) -> None:
        ctx = FakeContext(profile_name="coder")
        PLUGIN.register(ctx)
        self.assertIn("kwc_profile_response", ctx.tools)
        with (
            patch.object(PLUGIN, "_state_path", return_value=Path("state.db")),
            patch.object(PLUGIN, "_current_session_profile", return_value="coder"),
            patch.object(PLUGIN, "_current_session_key", return_value="coder-session-key"),
            patch.object(
                PLUGIN,
                "_submit_profile_response",
                return_value={
                    "ok": True,
                    "invocation_id": "inv_coder_1",
                    "status": "accepted",
                    "idempotent_replay": False,
                },
            ) as submit,
        ):
            result = json.loads(
                ctx.tools["kwc_profile_response"](
                    {
                        "invocation_id": "inv_coder_1",
                        "outcome": "review-requested",
                        "task_id": "t_impl1",
                        "reason_code": "IMPLEMENTATION_VERIFIED",
                    }
                )
            )

        self.assertTrue(result["ok"])
        submit.assert_called_once_with(
            state_path=Path("state.db"),
            invocation_id="inv_coder_1",
            caller_profile="coder",
            caller_session_key="coder-session-key",
            outcome="review-requested",
            task_id="t_impl1",
            reason_code="IMPLEMENTATION_VERIFIED",
        )

    def test_profile_response_tool_is_visible_only_to_execution_profiles(self) -> None:
        ctx = FakeContext(
            {"execution_profiles": "project-manager,coder,reviewer"},
            profile_name="coder",
        )
        PLUGIN.register(ctx)
        self.assertIsNotNone(ctx.tool_checks["kwc_profile_response"])
        with patch.object(PLUGIN, "_current_session_profile", return_value="coder"):
            self.assertTrue(ctx.tool_checks["kwc_profile_response"]())
        with patch.object(PLUGIN, "_current_session_profile", return_value="reviewer"):
            self.assertTrue(ctx.tool_checks["kwc_profile_response"]())
        with patch.object(PLUGIN, "_current_session_profile", return_value="project-manager"):
            self.assertFalse(ctx.tool_checks["kwc_profile_response"]())
            self.assertTrue(ctx.tool_checks["kwc_pm_response"]())
        with patch.object(PLUGIN, "_current_session_profile", return_value="other"):
            self.assertFalse(ctx.tool_checks["kwc_profile_response"]())

    def test_dispatch_tick_delivers_durable_profile_wake_to_bound_coordinator(self) -> None:
        ctx = FakeContext({"coordinator_profile": "project-manager"})
        PLUGIN.register(ctx)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            with patch.object(PLUGIN, "_state_path", return_value=state_path):
                PLUGIN._bind_state("issue151", "pm-session", 0)
                wake_service = PLUGIN._load_wake_service()
                wake_service.enqueue_profile_wake(
                    state_path=state_path,
                    action_id="act_wake_1",
                    board="issue151",
                    task_id="t_review",
                    profile="project-manager",
                    skill="run-workflow",
                    invocation_id="inv_pm_1",
                    now_fn=lambda: 1000,
                )
                with patch.object(PLUGIN, "_now", return_value=1001):
                    PLUGIN._process_pending_profile_wakes("issue151")
                wakes = wake_service.read_profile_wakes(state_path=state_path)

        self.assertEqual([session for session, _ in ctx.injected], ["pm-session"])
        payload = json.loads(ctx.injected[0][1])
        self.assertEqual(payload["schema"], "kwc-profile-wakeup-v1")
        self.assertEqual(payload["invocation_id"], "inv_pm_1")
        self.assertEqual(payload["source_task_id"], "t_review")
        self.assertNotIn("task_body", payload)
        self.assertEqual(wakes[0]["status"], "delivered")

    def test_dispatch_tick_processes_pending_kwc_comments_without_core_comment_hook(self) -> None:
        ctx = FakeContext({"board": "issue151"})
        PLUGIN.register(ctx)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            with (
                patch.object(PLUGIN, "_state_path", return_value=state_path),
                patch.object(PLUGIN, "_process_pending_comment_deliveries") as process_comments,
            ):
                ctx.hooks["on_kanban_dispatch_tick"](board="issue151", dry_run=False)

        process_comments.assert_called_once_with("issue151")
        self.assertNotIn("kanban_task_commented", ctx.hooks)

    def test_comment_mentions_override_assignee_and_route_once_per_profile(self) -> None:
        ctx = FakeContext()
        PLUGIN.register(ctx)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            with patch.object(PLUGIN, "_state_path", return_value=state_path):
                PLUGIN._bind_profile_session("coder", "coder-session")
                PLUGIN._bind_profile_session("reviewer", "reviewer-session")
                delivery_id = PLUGIN._enqueue_comment_delivery(
                    board="issue151",
                    task_id="t_impl",
                    comment_id=7,
                    author="project-manager",
                    reply_to_comment_id=None,
                    route_requested=True,
                )
                with patch.object(
                    PLUGIN,
                    "_read_comment_context",
                    return_value={
                        "body": "@{reviewer} @{reviewer} 확인해주세요",
                        "assignee": "coder",
                    },
                ):
                    PLUGIN._process_pending_comment_deliveries("issue151")
                delivery = PLUGIN._read_comment_delivery(delivery_id)

        self.assertEqual([session for session, _ in ctx.injected], ["reviewer-session"])
        wake = json.loads(ctx.injected[0][1])
        self.assertEqual(wake["schema"], "kanban-comment-wakeup-v1")
        self.assertEqual(wake["source_comment_id"], 7)
        self.assertEqual(wake["target_profile"], "reviewer")
        self.assertEqual(delivery["status"], "delivered")

    def test_comment_without_mentions_routes_to_assignee(self) -> None:
        ctx = FakeContext()
        PLUGIN.register(ctx)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            with patch.object(PLUGIN, "_state_path", return_value=state_path):
                PLUGIN._bind_profile_session("coder", "coder-session")
                PLUGIN._enqueue_comment_delivery(
                    board="issue151",
                    task_id="t_impl",
                    comment_id=8,
                    author="project-manager",
                    reply_to_comment_id=None,
                    route_requested=True,
                )
                with patch.object(
                    PLUGIN,
                    "_read_comment_context",
                    return_value={"body": "테스트 실패 원인을 확인해주세요", "assignee": "coder"},
                ):
                    PLUGIN._process_pending_comment_deliveries("issue151")

        self.assertEqual([session for session, _ in ctx.injected], ["coder-session"])

    def test_reply_delivery_is_recorded_but_does_not_wake_another_agent(self) -> None:
        ctx = FakeContext()
        PLUGIN.register(ctx)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            with patch.object(PLUGIN, "_state_path", return_value=state_path):
                delivery_id = PLUGIN._enqueue_comment_delivery(
                    board="issue151",
                    task_id="t_impl",
                    comment_id=9,
                    author="coder",
                    reply_to_comment_id=7,
                    route_requested=False,
                )
                PLUGIN._process_pending_comment_deliveries("issue151")
                delivery = PLUGIN._read_comment_delivery(delivery_id)

        self.assertEqual(ctx.injected, [])
        self.assertEqual(delivery["status"], "suppressed")

    def test_missing_profile_binding_retries_then_delivers_exactly_once(self) -> None:
        ctx = FakeContext()
        PLUGIN.register(ctx)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            with patch.object(PLUGIN, "_state_path", return_value=state_path):
                delivery_id = PLUGIN._enqueue_comment_delivery(
                    board="issue151",
                    task_id="t_impl",
                    comment_id=10,
                    author="project-manager",
                    reply_to_comment_id=None,
                    route_requested=True,
                )
                with patch.object(
                    PLUGIN,
                    "_read_comment_context",
                    return_value={"body": "@{reviewer} 확인", "assignee": "coder"},
                ):
                    PLUGIN._process_pending_comment_deliveries("issue151")
                    pending = PLUGIN._read_comment_delivery(delivery_id)
                    PLUGIN._bind_profile_session("reviewer", "reviewer-session")
                    PLUGIN._process_pending_comment_deliveries("issue151")
                    PLUGIN._process_pending_comment_deliveries("issue151")
                    delivered = PLUGIN._read_comment_delivery(delivery_id)

        self.assertEqual(pending["status"], "retry_pending")
        self.assertEqual(delivered["status"], "delivered")
        self.assertEqual([session for session, _ in ctx.injected], ["reviewer-session"])

    def test_assignee_target_is_materialized_once_across_retries(self) -> None:
        ctx = FakeContext()
        PLUGIN.register(ctx)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            with patch.object(PLUGIN, "_state_path", return_value=state_path):
                PLUGIN._enqueue_comment_delivery(
                    board="issue151",
                    task_id="t_impl",
                    comment_id=11,
                    author="project-manager",
                    reply_to_comment_id=None,
                    route_requested=True,
                )
                with patch.object(
                    PLUGIN,
                    "_read_comment_context",
                    return_value={"body": "확인해주세요", "assignee": "coder"},
                ):
                    PLUGIN._process_pending_comment_deliveries("issue151")
                PLUGIN._bind_profile_session("coder", "coder-session")
                PLUGIN._bind_profile_session("reviewer", "reviewer-session")
                with patch.object(
                    PLUGIN,
                    "_read_comment_context",
                    return_value={"body": "확인해주세요", "assignee": "reviewer"},
                ):
                    PLUGIN._process_pending_comment_deliveries("issue151")

        self.assertEqual([session for session, _ in ctx.injected], ["coder-session"])

    def test_legacy_state_migration_commits_before_begin_immediate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            raw = __import__("sqlite3").connect(state_path)
            raw.execute(
                "CREATE TABLE binding(singleton INTEGER PRIMARY KEY, schema TEXT, board TEXT, "
                "coordinator_session_key TEXT, last_event_id INTEGER, bound_at INTEGER)"
            )
            raw.execute(
                "CREATE TABLE runtime_heartbeat(singleton INTEGER PRIMARY KEY, board TEXT, "
                "last_dispatch_tick_at INTEGER)"
            )
            raw.execute(
                "INSERT INTO binding VALUES(1, ?, 'issue151', 'legacy-session', 7, 1000)",
                (PLUGIN._STATE_SCHEMA,),
            )
            raw.commit()
            raw.close()
            with patch.object(PLUGIN, "_state_path", return_value=state_path):
                conn = PLUGIN._connect_state()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.rollback()
                finally:
                    conn.close()

    def test_dispatch_tick_records_health_before_a_session_is_bound(self) -> None:
        ctx = FakeContext({"board": "issue151"})
        PLUGIN.register(ctx)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            with (
                patch.object(PLUGIN, "_state_path", return_value=state_path),
                patch.object(PLUGIN, "_now", return_value=1000),
            ):
                ctx.hooks["on_kanban_dispatch_tick"](board="issue151", dry_run=False)
                heartbeat = PLUGIN._read_dispatch_heartbeat()
        self.assertEqual(heartbeat["board"], "issue151")
        self.assertEqual(heartbeat["last_dispatch_tick_at"], 1000)

    def test_run_workflow_binds_durable_gateway_session_key(self) -> None:
        ctx = FakeContext({"board": "issue151"})
        PLUGIN.register(ctx)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            with (
                patch.object(PLUGIN, "_state_path", return_value=state_path),
                patch.object(PLUGIN, "_latest_event_id", return_value=41),
                patch.object(PLUGIN, "_current_session_key", return_value="durable-gateway-key"),
                patch.object(PLUGIN, "_current_session_profile", return_value="project-manager"),
            ):
                ctx.hooks["pre_llm_call"](
                    session_id="ephemeral-agent-id",
                    user_message='[IMPORTANT: The user has invoked the "run-workflow" skill.]',
                )
                state = PLUGIN._read_state()
                board_state = PLUGIN._read_state("issue151")
        self.assertEqual(state["coordinator_session_key"], "durable-gateway-key")
        self.assertIsNone(board_state)

    def test_missing_durable_session_key_does_not_bind(self) -> None:
        ctx = FakeContext({"board": "issue151"})
        PLUGIN.register(ctx)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            with (
                patch.object(PLUGIN, "_state_path", return_value=state_path),
                patch.object(PLUGIN, "_current_session_key", return_value=""),
            ):
                ctx.hooks["pre_llm_call"](
                    session_id="ephemeral-agent-id", user_message="/run-workflow Issue 151"
                )
            self.assertFalse(state_path.exists())

    def test_top_level_profile_session_is_bound_for_comment_wakeups(self) -> None:
        ctx = FakeContext()
        PLUGIN.register(ctx)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            with (
                patch.object(PLUGIN, "_state_path", return_value=state_path),
                patch.object(PLUGIN, "_current_session_key", return_value="coder-session"),
                patch.object(PLUGIN, "_current_session_profile", return_value="coder"),
            ):
                ctx.hooks["pre_llm_call"](user_message="일반 작업")
                conn = PLUGIN._connect_state()
                try:
                    row = conn.execute(
                        "SELECT session_key FROM profile_session_binding WHERE profile='coder'"
                    ).fetchone()
                finally:
                    conn.close()
        self.assertEqual(row["session_key"], "coder-session")

    def test_registers_kwc_comment_reply_and_response_tools(self) -> None:
        ctx = FakeContext()
        PLUGIN.register(ctx)
        self.assertEqual(
            set(ctx.tools),
            {"kwc_comment", "kwc_reply", "kwc_pm_response", "kwc_profile_response"},
        )

    def test_kwc_comment_routes_but_kwc_reply_suppresses_follow_up_wake(self) -> None:
        ctx = FakeContext()
        PLUGIN.register(ctx)
        with (
            patch.object(PLUGIN, "_current_session_profile", return_value="coder"),
            patch.object(PLUGIN, "_create_kwc_comment") as create,
            patch.object(PLUGIN, "_schedule_comment_delivery") as schedule,
        ):
            create.side_effect = [
                {"ok": True, "board": "issue151", "comment_id": 10, "delivery_id": 3},
                {"ok": True, "board": "issue151", "comment_id": 11, "delivery_id": 4},
            ]
            comment_result = json.loads(
                ctx.tools["kwc_comment"](
                    {
                        "board": "issue151",
                        "task_id": "t_impl",
                        "body": "@{reviewer} 확인",
                        "idempotency_key": "agent-comment-10",
                    }
                )
            )
            reply_result = json.loads(
                ctx.tools["kwc_reply"](
                    {
                        "board": "issue151",
                        "task_id": "t_impl",
                        "reply_to_comment_id": 10,
                        "body": "확인했습니다",
                        "idempotency_key": "agent-reply-10-a",
                    }
                )
            )

        self.assertEqual(comment_result["comment_id"], 10)
        self.assertEqual(reply_result["comment_id"], 11)
        self.assertTrue(create.call_args_list[0].kwargs["route_requested"])
        self.assertEqual(create.call_args_list[0].kwargs["idempotency_key"], "agent-comment-10")
        self.assertFalse(create.call_args_list[1].kwargs["route_requested"])
        self.assertEqual(create.call_args_list[1].kwargs["reply_to_comment_id"], 10)
        self.assertEqual(create.call_args_list[1].kwargs["idempotency_key"], "agent-reply-10-a")
        self.assertEqual(schedule.call_args_list[0].args, ("issue151",))
        self.assertEqual(schedule.call_args_list[1].args, ("issue151",))

    def test_non_workflow_child_session_cannot_replace_binding(self) -> None:
        ctx = FakeContext({"board": "issue151"})
        PLUGIN.register(ctx)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._seed_state(state_path)
            with patch.object(PLUGIN, "_state_path", return_value=state_path):
                ctx.hooks["pre_llm_call"](
                    session_id="child-agent-id",
                    parent_session_id="parent-agent-id",
                    user_message="delegated child work",
                )
            state = self._read_state(state_path)
        self.assertEqual(state["coordinator_session_key"], "gateway-session")
        self.assertEqual(state["last_event_id"], 41)

    def test_child_workflow_marker_cannot_replace_binding(self) -> None:
        ctx = FakeContext({"board": "issue151"})
        PLUGIN.register(ctx)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._seed_state(state_path)
            with (
                patch.object(PLUGIN, "_state_path", return_value=state_path),
                patch.object(PLUGIN, "_latest_event_id", return_value=99),
                patch.object(PLUGIN, "_current_session_key", return_value="child-key"),
                patch.object(PLUGIN, "_current_session_profile", return_value="project-manager"),
            ):
                ctx.hooks["pre_llm_call"](
                    session_id="child-agent-id",
                    parent_session_id="parent-agent-id",
                    user_message='[IMPORTANT: The user has invoked the "run-workflow" skill.]',
                )
            state = self._read_state(state_path)
        self.assertEqual(state["coordinator_session_key"], "gateway-session")
        self.assertEqual(state["last_event_id"], 41)

    def test_run_workflow_detection_rejects_prefixes_and_quoted_marker(self) -> None:
        self.assertTrue(PLUGIN._is_run_workflow_invocation("/run-workflow Issue 151"))
        self.assertFalse(PLUGIN._is_run_workflow_invocation("/run-workflow-anything"))
        self.assertFalse(
            PLUGIN._is_run_workflow_invocation(
                'Please quote: The user has invoked the "run-workflow" skill'
            )
        )
        self.assertFalse(
            PLUGIN._is_run_workflow_invocation(
                '[IMPORTANT: The user has invoked the "run-workflow" skill-not-real.]'
            )
        )

    def test_non_coordinator_profile_cannot_bind(self) -> None:
        ctx = FakeContext({"board": "issue151", "coordinator_profile": "project-manager"})
        PLUGIN.register(ctx)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            with (
                patch.object(PLUGIN, "_state_path", return_value=state_path),
                patch.object(PLUGIN, "_latest_event_id", return_value=41),
                patch.object(PLUGIN, "_current_session_key", return_value="main-key"),
                patch.object(PLUGIN, "_current_session_profile", return_value="main"),
            ):
                ctx.hooks["pre_llm_call"](user_message="/run-workflow Issue 151")
            self.assertFalse(state_path.exists())

    def test_same_session_refresh_preserves_pending_cursor(self) -> None:
        ctx = FakeContext({"board": "issue151"})
        PLUGIN.register(ctx)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._seed_state(state_path, cursor=41)
            with (
                patch.object(PLUGIN, "_state_path", return_value=state_path),
                patch.object(PLUGIN, "_latest_event_id", return_value=99),
                patch.object(PLUGIN, "_current_session_key", return_value="gateway-session"),
                patch.object(PLUGIN, "_current_session_profile", return_value="project-manager"),
            ):
                ctx.hooks["pre_llm_call"](
                    parent_session_id="compressed-parent",
                    user_message='[IMPORTANT: The user has invoked the "run-workflow" skill.]',
                )
            state = self._read_state(state_path)
        self.assertEqual(state["last_event_id"], 41)

    def test_rebinding_cannot_move_cursor_behind_committed_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._seed_state(state_path, session_key="old", cursor=50)
            with patch.object(PLUGIN, "_state_path", return_value=state_path):
                PLUGIN._bind_state("issue151", "new", 41)
            state = self._read_state(state_path)
        self.assertEqual(state["coordinator_session_key"], "new")
        self.assertEqual(state["last_event_id"], 50)

    def test_dispatch_tick_routes_only_worker_and_user_decision_events(self) -> None:
        ctx = FakeContext(
            {
                "board": "issue151",
                "coordinator_profile": "project-manager",
                "decision_comment_author": "dashboard",
            }
        )
        PLUGIN.register(ctx)
        decision_body = {"schema": "policy-decision-card-v1", "decision_owner": "user"}
        implementation_body = {
            "schema": "backend-implementation-card-v3",
            "card_type": "implementation",
        }
        review_body = {"schema": "aggregate-review-card-v3"}
        events = [
            {"id": 42, "task_id": "t_impl", "kind": "commented", "payload": {"author": "coder"},
             "status": "blocked", "assignee": "coder", "event_profile": None,
             "task_body": implementation_body},
            {"id": 43, "task_id": "t_impl", "kind": "blocked", "payload": {},
             "status": "blocked", "assignee": "coder", "event_profile": "coder",
             "task_body": implementation_body},
            {"id": 44, "task_id": "t_impl", "kind": "completed", "payload": {},
             "status": "done", "assignee": "coder", "event_profile": "coder",
             "task_body": implementation_body},
            {"id": 45, "task_id": "t_impl", "kind": "review_requested", "payload": {},
             "status": "review", "assignee": "project-manager", "event_profile": "coder",
             "task_body": implementation_body},
            {"id": 46, "task_id": "t_review", "kind": "completed", "payload": {},
             "status": "done", "assignee": "reviewer", "event_profile": "reviewer",
             "task_body": review_body},
            {"id": 47, "task_id": "t_generic", "kind": "completed", "payload": {},
             "status": "done", "assignee": "reviewer", "event_profile": "reviewer",
             "task_body": {"schema": "other"}},
            {"id": 48, "task_id": "t_pm", "kind": "completed", "payload": {},
             "status": "done", "assignee": "project-manager", "event_profile": "project-manager",
             "task_body": review_body},
            {"id": 49, "task_id": "t_review", "kind": "blocked", "payload": {},
             "status": "blocked", "assignee": "reviewer", "event_profile": "reviewer",
             "task_body": review_body},
            {"id": 50, "task_id": "t_generic", "kind": "commented", "payload": {"author": "dashboard"},
             "status": "blocked", "assignee": "project-manager", "event_profile": None,
             "task_body": {"schema": "other"}},
            {"id": 51, "task_id": "t_decision", "kind": "commented", "payload": {"author": "coder"},
             "status": "blocked", "assignee": "project-manager", "event_profile": None,
             "task_body": decision_body},
            {"id": 52, "task_id": "t_decision", "kind": "commented", "payload": {"author": "dashboard"},
             "status": "blocked", "assignee": "project-manager", "event_profile": None,
             "task_body": decision_body},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._seed_state(state_path)
            with (
                patch.object(PLUGIN, "_state_path", return_value=state_path),
                patch.object(PLUGIN, "_read_events", return_value=events),
            ):
                ctx.hooks["on_kanban_dispatch_tick"](board="issue151", dry_run=False)
            state = self._read_state(state_path)
        routed = [json.loads(content) for _, content in ctx.injected]
        self.assertEqual(
            [item["event_kind"] for item in routed],
            ["blocked", "completed", "blocked", "commented"],
        )
        self.assertEqual(
            [item["source_task_id"] for item in routed],
            ["t_impl", "t_review", "t_review", "t_decision"],
        )
        self.assertTrue(all(session == "gateway-session" for session, _ in ctx.injected))
        self.assertTrue(all("comment" not in item for item in routed))
        self.assertEqual(state["last_event_id"], 52)

    def test_coordinator_profile_cannot_route_its_own_run_as_a_worker_event(self) -> None:
        ctx = FakeContext({"coordinator_profile": "coder"})
        PLUGIN.register(ctx)
        events = [
            {"id": 42, "task_id": "t_impl", "kind": "blocked", "payload": {},
             "status": "blocked", "assignee": "coder", "event_profile": "coder",
             "task_body": {"schema": "backend-implementation-card-v3",
                           "card_type": "implementation"}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._seed_state(state_path)
            with (
                patch.object(PLUGIN, "_state_path", return_value=state_path),
                patch.object(PLUGIN, "_read_events", return_value=events),
            ):
                ctx.hooks["on_kanban_dispatch_tick"](board="issue151", dry_run=False)
            state = self._read_state(state_path)

        self.assertEqual(ctx.injected, [])
        self.assertEqual(state["last_event_id"], 42)

    def test_failed_session_injection_keeps_event_for_retry(self) -> None:
        class RejectingContext(FakeContext):
            def inject_message(self, content: str, *, session_key: str) -> bool:
                self.injected.append((session_key, content))
                return False

        ctx = RejectingContext({"board": "issue151"})
        PLUGIN.register(ctx)
        events = [{"id": 9, "task_id": "t_impl", "kind": "blocked", "payload": {},
                   "status": "blocked", "assignee": "coder", "event_profile": "coder",
                   "task_body": {"schema": "backend-implementation-card-v3",
                                 "card_type": "implementation"}}]
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._seed_state(state_path, cursor=8)
            with (
                patch.object(PLUGIN, "_state_path", return_value=state_path),
                patch.object(PLUGIN, "_read_events", return_value=events),
            ):
                ctx.hooks["on_kanban_dispatch_tick"](board="issue151", dry_run=False)
            state = self._read_state(state_path)
        self.assertEqual(state["last_event_id"], 8)

    def test_stale_dispatcher_cannot_inject_into_replaced_binding(self) -> None:
        ctx = FakeContext({"board": "issue151"})
        PLUGIN.register(ctx)
        event = {"id": 9, "task_id": "t_impl", "kind": "blocked", "payload": {},
                 "status": "blocked", "assignee": "coder", "event_profile": "coder",
                 "task_body": {}}
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._seed_state(state_path, session_key="old", cursor=8)
            stale = self._read_state(state_path)
            self._seed_state(state_path, session_key="new", cursor=20)
            with patch.object(PLUGIN, "_state_path", return_value=state_path):
                processed = PLUGIN._process_event(stale, event, True, "wake")
            current = self._read_state(state_path)
        self.assertFalse(processed)
        self.assertEqual(ctx.injected, [])
        self.assertEqual(current["coordinator_session_key"], "new")
        self.assertEqual(current["last_event_id"], 20)

    def test_concurrent_dispatch_ticks_inject_event_once(self) -> None:
        ctx = FakeContext({"board": "issue151"})
        PLUGIN.register(ctx)
        event = {"id": 9, "task_id": "t_review", "kind": "completed", "payload": {},
                 "status": "done", "assignee": "reviewer", "event_profile": "reviewer",
                 "task_body": {"schema": "aggregate-review-card-v3"}}
        barrier = threading.Barrier(2)

        def read_events(_board, _after_id):
            barrier.wait(timeout=5)
            return [event]

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._seed_state(state_path, cursor=8)
            with (
                patch.object(PLUGIN, "_state_path", return_value=state_path),
                patch.object(PLUGIN, "_read_events", side_effect=read_events),
            ):
                threads = [
                    threading.Thread(
                        target=ctx.hooks["on_kanban_dispatch_tick"],
                        kwargs={"board": "issue151", "dry_run": False},
                    )
                    for _ in range(2)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
            state = self._read_state(state_path)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(ctx.injected), 1)
        self.assertEqual(state["last_event_id"], 9)

    def test_dispatch_tick_monitors_every_board_and_ignores_dry_run(self) -> None:
        ctx = FakeContext()
        PLUGIN.register(ctx)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._seed_state(state_path, cursor=1)
            with (
                patch.object(PLUGIN, "_state_path", return_value=state_path),
                patch.object(PLUGIN, "_latest_event_id", return_value=7),
                patch.object(PLUGIN, "_read_events") as read_events,
            ):
                ctx.hooks["on_kanban_dispatch_tick"](board="another", dry_run=False)
                ctx.hooks["on_kanban_dispatch_tick"](board="issue151", dry_run=True)
                another_state = PLUGIN._read_state("another")
                heartbeat = PLUGIN._read_dispatch_heartbeat()
        read_events.assert_called_once_with("another", 7)
        self.assertEqual(another_state["last_event_id"], 7)
        self.assertEqual(heartbeat["board"], "another")
        self.assertEqual(ctx.injected, [])

    def test_comment_recovery_matches_only_the_reserved_native_id(self) -> None:
        digest = COMMENT_SERVICE.sha256("same body".encode("utf-8")).hexdigest()
        comments = [
            types.SimpleNamespace(id=40, author="coder", body="same body"),
            types.SimpleNamespace(id=41, author="reviewer", body="same body"),
            types.SimpleNamespace(id=42, author="coder", body="same body"),
        ]
        self.assertIsNone(
            COMMENT_SERVICE._matching_recovery_comment_id(comments, 41, "coder", digest)
        )
        self.assertEqual(
            COMMENT_SERVICE._matching_recovery_comment_id(comments, 42, "coder", digest), 42
        )

    def test_event_hint_seeds_cursor_before_the_triggering_event(self) -> None:
        PLUGIN.register(FakeContext())
        with (
            patch.object(PLUGIN, "_read_state", side_effect=[{"coordinator_session_key": "s"}, None]),
            patch.object(PLUGIN, "_find_hint_event_id", return_value=17) as find_event,
            patch.object(PLUGIN, "_ensure_board_cursor") as ensure,
            patch.object(PLUGIN, "_process_pending_events") as process,
        ):
            PLUGIN._on_event_hint(
                board="issue151",
                task_id="t_impl",
                event_kind="completed",
                run_id=9,
            )

        find_event.assert_called_once_with("issue151", "t_impl", "completed", 9)
        ensure.assert_called_once_with("issue151", 16)
        process.assert_called_once_with("issue151")


if __name__ == "__main__":
    unittest.main()
